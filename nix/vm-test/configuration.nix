{ config, modulesPath, pkgs, lib, ... }:
{
  imports = [ (modulesPath + "/profiles/qemu-guest.nix") ];

  # Lima VM basics (from nixos-lima-config-sample)
  networking.hostName = "devbox-sim";
  users.mutableUsers = true;
  services.lima.enable = true;
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  services.openssh.enable = true;
  security.sudo.wheelNeedsPassword = false;
  boot.loader.grub = { device = "nodev"; efiSupport = true; efiInstallAsRemovable = true; };
  fileSystems."/boot" = { device = lib.mkForce "/dev/vda1"; fsType = "vfat"; };
  fileSystems."/" = { device = "/dev/disk/by-label/nixos"; autoResize = true; fsType = "ext4";
                      options = [ "noatime" "nodiratime" "discard" ]; };
  environment.systemPackages = [ pkgs.gitMinimal ];
  system.stateVersion = "26.05";

  # The system under test
  services.research-loop = {
    enable = true;
    repo.url = "https://github.com/sandeshghanta/kaggle-quantum-challenge.git";
    repo.ref = "sandesh/_claude/first-loop";
    workers = 2;
    model.baseUrl = "http://127.0.0.1:8099/v1";
    model.name = "fake-model";
    verifier.maxWorkers = 6;
    verifier.salts = [ 1 ];
  };

  # Fake model for smoke tests (served from the loop's own checkout)
  systemd.services.fake-model = {
    after = [ "research-loop-checkout.service" ];
    requires = [ "research-loop-checkout.service" ];
    wantedBy = [ "multi-user.target" ];
    before = [ "research-loop.target" ];
    path = [ pkgs.uv pkgs.python312 ];
    environment = { HOME = "/var/lib/research-loop"; UV_PYTHON_DOWNLOADS = "never";
                    UV_PYTHON = "${pkgs.python312}/bin/python3.12";
                    UV_CACHE_DIR = "/var/lib/research-loop/.cache/uv"; };
    serviceConfig = { User = "research-loop"; WorkingDirectory = "/var/lib/research-loop/repo"; };
    script = "exec uv run python -m tests.fakes.openai_server --port 8099";
  };
}
