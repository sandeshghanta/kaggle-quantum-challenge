# NixOS module: the research loop as drain-on-stop systemd services, one Unix user per role.
#
# Only `substrated` (user `substrate`) can open the event log and blobs (${dataDir}/substrate, mode
# 0700). Every other role talks to it over /run/substrate/api.sock and is identified by its Unix user,
# so the role on each event is attested by the OS, not claimed by the caller. Loop code runs from a git
# checkout owned by `rl-deploy`; services run its virtualenv read-only.
{ pi, toolchain }:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.research-loop;
  tc = toolchain pkgs pkgs.stdenv.hostPlatform.system;
  repo = "${cfg.dataDir}/repo";
  python = "${repo}/.venv/bin/python";
  socket = "/run/substrate/api.sock";
  target = "unix:${socket}";
  uvEnv = {
    HOME = "${cfg.dataDir}/cache";
    UV_CACHE_DIR = "${cfg.dataDir}/cache/uv";
    UV_PYTHON = "${tc.python}/bin/python3.12";
    UV_PYTHON_DOWNLOADS = "never";
    LD_LIBRARY_PATH = tc.libraryPath;
  };
  runEnv = { LD_LIBRARY_PATH = tc.libraryPath; PYTHONDONTWRITEBYTECODE = "1"; };
  gitSsh = lib.optionalString (cfg.repo.sshKeyFile != null)
    "export GIT_SSH_COMMAND='ssh -i ${cfg.repo.sshKeyFile} -o StrictHostKeyChecking=accept-new'";
  path = tc.packages ++ [ pkgs.openssh pkgs.coreutils ];
  roleUnit = { user, writable, extra ? { } }: {
    User = user;
    Group = user;
    SupplementaryGroups = [ "substrate-clients" ];
    WorkingDirectory = repo;
    KillSignal = "SIGTERM";
    KillMode = "mixed";       # SIGTERM only the main process so it can finish its job; children are left running
    Restart = "on-failure";
    RestartSec = 10;
    ProtectSystem = "strict";
    ProtectHome = true;
    ReadWritePaths = writable;
    PrivateTmp = true;
  } // extra;
  rhome = "${cfg.dataDir}/researcher";   # the researcher's persistent home: Claude login, npm prefix, pi agent dir
  statusDir = "${cfg.dataDir}/status/researcher";   # researcher.json; read by the status page (group status-readers)
  claudeBin = "${rhome}/npm/node_modules/.bin";
  # The one human step: log the researcher user in to the Claude Pro plan (see docs/deploy.md).
  researcherLogin = pkgs.writeShellScriptBin "research-loop-researcher-login" ''
    set -euo pipefail
    exec sudo -u rl-researcher -H env HOME=${rhome} PATH=${claudeBin}:${lib.makeBinPath [ pkgs.nodejs_22 pkgs.coreutils ]}:$PATH \
      claude auth login "$@"
  '';
  update = pkgs.writeShellScriptBin "research-loop-update" ''
    set -euo pipefail
    sudo -u rl-deploy env ${lib.concatStringsSep " " (lib.mapAttrsToList (k: v: "${k}=${v}") uvEnv)} \
      PATH=${lib.makeBinPath path}:$PATH ${lib.optionalString (cfg.repo.sshKeyFile != null)
        "GIT_SSH_COMMAND='ssh -i ${cfg.repo.sshKeyFile}'"} \
      bash -c 'cd ${repo} && git pull --ff-only && uv sync --frozen'
    systemctl restart research-loop-verifier.service
    for i in $(seq 1 ${toString cfg.workers}); do
      systemctl restart "research-loop-worker@$i.service"   # each drains its current job first
    done
    ${lib.optionalString cfg.researcher.enable "systemctl restart research-loop-researcher.service   # drains the current visit first"}
    systemctl restart substrated.service                   # last: clients retry across the restart
  '';
in {
  options.services.research-loop = {
    enable = lib.mkEnableOption "the quantum circuit research loop";
    repo.url = lib.mkOption { type = lib.types.str; description = "Git URL of this repository."; };
    repo.ref = lib.mkOption { type = lib.types.str; default = "main"; };
    repo.sshKeyFile = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null;
      description = "Deploy key for a private repository (readable by rl-deploy)."; };
    dataDir = lib.mkOption { type = lib.types.str; default = "/var/lib/research-loop"; };
    workers = lib.mkOption { type = lib.types.ints.positive; default = 8; };
    model.baseUrl = lib.mkOption { type = lib.types.str; };
    model.name = lib.mkOption { type = lib.types.str; };
    # str, not path: a Nix path literal would copy the secret into the world-readable store
    model.apiKeyFile = lib.mkOption { type = lib.types.nullOr lib.types.str; default = null; };
    model.contextWindow = lib.mkOption { type = lib.types.int; default = 32768; };
    verifier.maxWorkers = lib.mkOption { type = lib.types.int; default = 12; };
    verifier.salts = lib.mkOption { type = lib.types.listOf lib.types.int; default = [ 1 2 3 4 ]; };
    researcher.enable = lib.mkOption { type = lib.types.bool; default = true;
      description = "Run the researcher service (needs a one-time Claude login: see docs/deploy.md)."; };
    researcher.intervalMin = lib.mkOption { type = lib.types.int; default = 45; };
    researcher.capPct = lib.mkOption { type = lib.types.int; default = 80;
      description = "No visit while the 5-hour or weekly plan usage is at or above this percentage."; };
    researcher.model = lib.mkOption { type = lib.types.str; default = "claude-opus-5-5"; };
  };

  config = lib.mkIf cfg.enable (lib.mkMerge [ {
    users.groups = lib.genAttrs [ "substrate" "substrate-clients" "rl-deploy" "rl-worker" "rl-verifier" "rl-seed" ] (_: { });
    users.users = lib.genAttrs [ "substrate" "rl-deploy" "rl-worker" "rl-verifier" "rl-seed" ]
      (u: { isSystemUser = true; group = u; }
          // lib.optionalAttrs (lib.hasPrefix "rl-" u && u != "rl-deploy") { extraGroups = [ "substrate-clients" ]; });
    programs.nix-ld.enable = true;
    environment.systemPackages = [ update ];

    systemd.tmpfiles.rules = [
      "d ${cfg.dataDir} 0755 root root -"
      "d ${repo} 0755 rl-deploy rl-deploy -"
      "d ${cfg.dataDir}/cache 0755 rl-deploy rl-deploy -"
      "d ${cfg.dataDir}/substrate 0700 substrate substrate -"
      "d ${cfg.dataDir}/work 0755 root root -"
      "d ${cfg.dataDir}/work/verifier 0700 rl-verifier rl-verifier -"
      "d ${cfg.dataDir}/work/workers 0700 rl-worker rl-worker -"
      "d ${cfg.dataDir}/state 0700 rl-verifier rl-verifier -"
    ];

    systemd.targets.research-loop = {
      wantedBy = [ "multi-user.target" ];
      wants = [ "substrated.service" "research-loop-verifier.service" ]
        ++ map (i: "research-loop-worker@${toString i}.service") (lib.range 1 cfg.workers);
    };

    systemd.services.research-loop-checkout = {
      description = "Clone the research loop repository (only if absent)";
      after = [ "network-online.target" "systemd-tmpfiles-setup.service" ];
      wants = [ "network-online.target" ];
      inherit path;
      environment = uvEnv;
      serviceConfig = { Type = "oneshot"; RemainAfterExit = true; User = "rl-deploy"; Group = "rl-deploy";
                        Restart = "on-failure"; RestartSec = 30; };
      script = ''
        ${gitSsh}
        if [ ! -d ${repo}/.git ]; then
          git clone --branch ${cfg.repo.ref} ${cfg.repo.url} ${repo}
        fi
        cd ${repo} && uv sync --frozen
      '';
    };

    # The only process that can open the event log and blobs.
    systemd.services.substrated = {
      description = "Substrate service (event log + blobs)";
      after = [ "research-loop-checkout.service" ];
      requires = [ "research-loop-checkout.service" ];
      partOf = [ "research-loop.target" ];
      environment = runEnv;
      serviceConfig = {
        User = "substrate";
        Group = "substrate-clients";          # socket and runtime dir are group-accessible to roles
        UMask = "0077";
        RuntimeDirectory = "substrate";
        RuntimeDirectoryMode = "0750";
        WorkingDirectory = repo;
        ExecStart = "${python} -m substrate.server --socket ${socket} --root ${cfg.dataDir}/substrate --policy ${repo}/loop/policy.json";
        Restart = "on-failure";
        RestartSec = 2;
        ProtectSystem = "strict";
        ProtectHome = true;
        ReadWritePaths = [ "${cfg.dataDir}/substrate" ];
        PrivateTmp = true;
      };
    };

    systemd.services.research-loop-seed = {
      description = "Seed the program pool (idempotent)";
      after = [ "substrated.service" ];
      requires = [ "substrated.service" ];
      environment = runEnv;
      serviceConfig = roleUnit { user = "rl-seed"; writable = [ ]; extra = {
        Type = "oneshot"; RemainAfterExit = true; Restart = "on-failure"; RestartSec = 10;
        ExecStart = "${python} -m loop.seed --substrate ${target}"; }; };
    };

    # Exactly one verifier: nothing claims a program before verifying it, so a second one would
    # double-score. TimeoutStopSec covers a full-check salt in progress; OOMPolicy=continue records an
    # OOM-killed check as a failure instead of stopping the service.
    systemd.services.research-loop-verifier = {
      description = "Research loop verifier";
      after = [ "research-loop-seed.service" ];
      requires = [ "research-loop-seed.service" ];
      partOf = [ "research-loop.target" ];
      inherit path;
      environment = runEnv;
      serviceConfig = roleUnit {
        user = "rl-verifier";
        writable = [ "${cfg.dataDir}/work/verifier" "${cfg.dataDir}/state" ];
        extra = { MemoryMax = "8G"; TimeoutStopSec = 4 * 3600; OOMPolicy = "continue"; };
      };
      script = ''
        exec ${python} -m loop.verifier --substrate ${target} --state ${cfg.dataDir}/state/verifier.cursor \
          --workdir ${cfg.dataDir}/work/verifier/job --max-workers ${toString cfg.verifier.maxWorkers} \
          --salts ${lib.concatMapStringsSep " " toString cfg.verifier.salts}
      '';
    };

    systemd.services."research-loop-worker@" = {
      description = "Research loop lab assistant %i";
      after = [ "research-loop-seed.service" ];
      requires = [ "research-loop-seed.service" ];
      partOf = [ "research-loop.target" ];
      inherit path;
      environment = runEnv // { HOME = "${cfg.dataDir}/work/workers"; };
      # stop timeout outlasts the pi job timeout (900 s) so a stopping worker always records its result
      serviceConfig = roleUnit {
        user = "rl-worker";
        writable = [ "${cfg.dataDir}/work/workers" ];
        extra = { MemoryMax = "2G"; CPUQuota = "50%"; TimeoutStopSec = 1000; }
          // lib.optionalAttrs (cfg.model.apiKeyFile != null) { LoadCredential = [ "apikey:${cfg.model.apiKeyFile}" ]; };
      };
      script = ''
        if [ -n "''${CREDENTIALS_DIRECTORY:-}" ] && [ -f "$CREDENTIALS_DIRECTORY/apikey" ]; then
          export RESEARCH_LOOP_API_KEY="$(cat "$CREDENTIALS_DIRECTORY/apikey")"
        else
          export RESEARCH_LOOP_API_KEY=none
        fi
        exec ${python} -m loop.worker --substrate ${target} --id "$1" --base-url ${lib.escapeShellArg cfg.model.baseUrl} \
          --model ${lib.escapeShellArg cfg.model.name} --api-key-env RESEARCH_LOOP_API_KEY \
          --context-window ${toString cfg.model.contextWindow} --workroot ${cfg.dataDir}/work/workers/worker-"$1"
      '';
      scriptArgs = "%i";
    };
  } (lib.mkIf cfg.researcher.enable {
    users.groups.rl-researcher = { };
    users.groups.status-readers = { };
    users.users.rl-researcher = { isSystemUser = true; group = "rl-researcher"; home = rhome;
                                  extraGroups = [ "substrate-clients" ]; };
    environment.systemPackages = [ researcherLogin ];
    systemd.tmpfiles.rules = [
      "d ${rhome} 0700 rl-researcher rl-researcher -"
      "d ${cfg.dataDir}/status 0755 root root -"
      "d ${statusDir} 0750 rl-researcher status-readers -"
    ];
    systemd.targets.research-loop.wants = [ "research-loop-researcher.service" ];

    # Installs Claude Code and the pi-claude-bridge extension into the researcher's home (idempotent).
    # Not verifiable without the real npm registry: see docs/deploy.md.
    systemd.services.research-loop-researcher-setup = {
      description = "Install Claude Code and pi-claude-bridge for the researcher";
      after = [ "network-online.target" "systemd-tmpfiles-setup.service" ];
      wants = [ "network-online.target" ];
      inherit path;
      environment = { HOME = rhome; PI_CODING_AGENT_DIR = "${rhome}/pi-agent"; };
      serviceConfig = { Type = "oneshot"; RemainAfterExit = true; User = "rl-researcher"; Group = "rl-researcher";
                        Restart = "on-failure"; RestartSec = 60; };
      script = ''
        mkdir -p ${rhome}/npm ${rhome}/pi-agent
        npm install --prefix ${rhome}/npm @anthropic-ai/claude-code
        export PATH=${claudeBin}:$PATH
        pi install npm:pi-claude-bridge
      '';
    };

    # Plain Python, no model of its own. The pi run inside is deliberately not sandboxed: it has no tools, and the
    # bridge needs the Claude login in HOME. The environment must never carry an Anthropic API key or base URL:
    # the researcher bills the Pro plan login, and loop.researcher refuses to start if one is set.
    systemd.services.research-loop-researcher = {
      description = "Research loop researcher (one Claude Opus visit every ${toString cfg.researcher.intervalMin} minutes, budget permitting)";
      after = [ "research-loop-seed.service" "research-loop-researcher-setup.service" ];
      requires = [ "research-loop-seed.service" "research-loop-researcher-setup.service" ];
      partOf = [ "research-loop.target" ];
      inherit path;
      environment = runEnv // { HOME = rhome; PI_CODING_AGENT_DIR = "${rhome}/pi-agent"; };
      # stop timeout outlasts the visit timeout (900 s) so a stopping researcher always records its result
      serviceConfig = roleUnit {
        user = "rl-researcher";
        writable = [ rhome statusDir ];
        extra = { TimeoutStopSec = 1000; MemoryMax = "2G"; };
      };
      script = ''
        export PATH=${claudeBin}:$PATH
        exec ${python} -m loop.researcher --substrate ${target} --interval-min ${toString cfg.researcher.intervalMin} \
          --cap-pct ${toString cfg.researcher.capPct} --provider claude-bridge --model ${lib.escapeShellArg cfg.researcher.model} \
          --agent-dir ${rhome}/pi-agent --status-out ${statusDir}/researcher.json
      '';
    };
  }) ]);
}
