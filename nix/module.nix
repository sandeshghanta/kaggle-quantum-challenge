# NixOS module: runs the research loop from a git checkout as drain-on-stop systemd services.
{ pi, toolchain }:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.research-loop;
  tc = toolchain pkgs pkgs.stdenv.hostPlatform.system;
  repo = "${cfg.dataDir}/repo";
  root = "${cfg.dataDir}/substrate";
  env = {
    HOME = cfg.dataDir;
    UV_CACHE_DIR = "${cfg.dataDir}/.cache/uv";
    UV_PYTHON = "${tc.python}/bin/python3.12";
    UV_PYTHON_DOWNLOADS = "never";
    LD_LIBRARY_PATH = tc.libraryPath;
  };
  gitSsh = lib.optionalString (cfg.repo.sshKeyFile != null)
    "export GIT_SSH_COMMAND='ssh -i ${cfg.repo.sshKeyFile} -o StrictHostKeyChecking=accept-new'";
  common = {
    path = tc.packages ++ [ pkgs.openssh pkgs.coreutils ];
    environment = env;
  };
  longRunning = extra: {
    User = "research-loop";
    Group = "research-loop";
    WorkingDirectory = repo;
    KillSignal = "SIGTERM";
    KillMode = "mixed";       # SIGTERM only the main process so it can finish its job; children are left running
    Restart = "on-failure";
    RestartSec = 10;
    ProtectSystem = "strict";
    ReadWritePaths = [ cfg.dataDir ];
    PrivateTmp = true;
  } // extra;
  update = pkgs.writeShellScriptBin "research-loop-update" ''
    set -euo pipefail
    sudo -u research-loop env ${lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "${k}=${v}") env)} \
      PATH=${lib.makeBinPath tc.packages}:$PATH \
      bash -c 'cd ${repo} && git pull --ff-only && uv sync --frozen'
    systemctl restart research-loop-verifier.service
    for i in $(seq 1 ${toString cfg.workers}); do
      systemctl restart "research-loop-worker@$i.service"   # each drains its current job first
    done
  '';
in {
  options.services.research-loop = {
    enable = lib.mkEnableOption "the quantum circuit research loop";
    repo.url = lib.mkOption { type = lib.types.str; description = "Git URL of this repository."; };
    repo.ref = lib.mkOption { type = lib.types.str; default = "main"; };
    repo.sshKeyFile = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null;
      description = "Deploy key for a private repository (readable by research-loop)."; };
    dataDir = lib.mkOption { type = lib.types.path; default = "/var/lib/research-loop"; };
    workers = lib.mkOption { type = lib.types.ints.positive; default = 8; };
    model.baseUrl = lib.mkOption { type = lib.types.str; };
    model.name = lib.mkOption { type = lib.types.str; };
    # str, not path: a Nix path literal would copy the secret into the world-readable store
    model.apiKeyFile = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null; };
    model.contextWindow = lib.mkOption { type = lib.types.int; default = 32768; };
    verifier.maxWorkers = lib.mkOption { type = lib.types.int; default = 12; };
    verifier.salts = lib.mkOption { type = lib.types.listOf lib.types.int; default = [ 1 2 3 4 ]; };
  };

  config = lib.mkIf cfg.enable {
    users.groups.research-loop = { };
    users.users.research-loop = { isSystemUser = true; group = "research-loop"; home = cfg.dataDir; createHome = true; };
    programs.nix-ld.enable = true;
    environment.systemPackages = [ update ];

    systemd.targets.research-loop = {
      wantedBy = [ "multi-user.target" ];
      wants = [ "research-loop-verifier.service" ]
        ++ map (i: "research-loop-worker@${toString i}.service") (lib.range 1 cfg.workers);
    };

    systemd.services.research-loop-checkout = common // {
      description = "Clone the research loop repository (only if absent)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      serviceConfig = { Type = "oneshot"; RemainAfterExit = true; User = "research-loop"; Group = "research-loop"; };
      script = ''
        ${gitSsh}
        if [ ! -d ${repo}/.git ]; then
          git clone --branch ${cfg.repo.ref} ${cfg.repo.url} ${repo}
        fi
        cd ${repo} && uv sync --frozen
      '';
    };

    systemd.services.research-loop-seed = common // {
      description = "Seed the program pool (idempotent)";
      after = [ "research-loop-checkout.service" ];
      requires = [ "research-loop-checkout.service" ];
      serviceConfig = { Type = "oneshot"; RemainAfterExit = true; User = "research-loop"; Group = "research-loop";
                        WorkingDirectory = repo; };
      script = "uv run python -m loop.seed --root ${root}";
    };

    systemd.services.research-loop-verifier = common // {
      description = "Research loop verifier";
      after = [ "research-loop-seed.service" ];
      requires = [ "research-loop-seed.service" ];
      partOf = [ "research-loop.target" ];
      # exactly one verifier: nothing claims a program before verifying it, so a second one would double-score.
      # TimeoutStopSec covers a full-check salt in progress; OOMPolicy=continue records an OOM-killed check
      # as a failure instead of stopping the service.
      serviceConfig = longRunning { MemoryMax = "8G"; TimeoutStopSec = 4 * 3600; OOMPolicy = "continue"; };
      script = ''
        exec uv run python -m loop.verifier --root ${root} --state ${cfg.dataDir}/state/verifier.cursor \
          --workdir ${cfg.dataDir}/work/verifier --max-workers ${toString cfg.verifier.maxWorkers} \
          --salts ${lib.concatMapStringsSep " " toString cfg.verifier.salts}
      '';
    };

    systemd.services."research-loop-worker@" = common // {
      description = "Research loop lab assistant %i";
      after = [ "research-loop-seed.service" ];
      requires = [ "research-loop-seed.service" ];
      partOf = [ "research-loop.target" ];
      # stop timeout outlasts the pi job timeout (900 s) so a stopping worker always records its result
      serviceConfig = longRunning ({ MemoryMax = "2G"; CPUQuota = "50%"; TimeoutStopSec = 1000; }
        // lib.optionalAttrs (cfg.model.apiKeyFile != null) { LoadCredential = [ "apikey:${cfg.model.apiKeyFile}" ]; });
      script = ''
        if [ -n "''${CREDENTIALS_DIRECTORY:-}" ] && [ -f "$CREDENTIALS_DIRECTORY/apikey" ]; then
          export RESEARCH_LOOP_API_KEY="$(cat "$CREDENTIALS_DIRECTORY/apikey")"
        else
          export RESEARCH_LOOP_API_KEY=none
        fi
        exec uv run python -m loop.worker --root ${root} --id "$1" --base-url ${cfg.model.baseUrl} \
          --model ${cfg.model.name} --api-key-env RESEARCH_LOOP_API_KEY \
          --context-window ${toString cfg.model.contextWindow} --workroot ${cfg.dataDir}/work/worker-"$1"
      '';
      scriptArgs = "%i";
    };
  };
}
