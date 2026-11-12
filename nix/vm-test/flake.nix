# Local NixOS (Lima) VM that runs the research-loop module end to end against a fake model.
{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    nixos-lima = { url = "github:nixos-lima/nixos-lima/master"; inputs.nixpkgs.follows = "nixpkgs"; };
    research-loop.url = "path:../..";
  };

  outputs = { nixpkgs, nixos-lima, research-loop, ... }: {
    nixosConfigurations.devbox-sim = nixpkgs.lib.nixosSystem {
      system = "aarch64-linux";
      modules = [
        nixos-lima.nixosModules.lima
        research-loop.nixosModules.default
        ./configuration.nix
      ];
    };
  };
}
