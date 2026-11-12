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
    repo.ref = "main";
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
    serviceConfig = { User = "rl-deploy"; WorkingDirectory = "/var/lib/research-loop/repo";
                      ExecStart = "/var/lib/research-loop/repo/.venv/bin/python -m tests.fakes.openai_server --port 8099"; };
  };
}
