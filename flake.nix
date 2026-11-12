{
  description = "Auto-research loop for the Google Verifiable Quantum Circuit Compilation challenge";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    pi.url = "github:earendil-works/pi/stable";
    pi.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, pi, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      toolchain = pkgs: system: {
        python = pkgs.python312;
        packages = [
          pkgs.python312 pkgs.uv pkgs.nodejs_22 pkgs.git pkgs.gcc pkgs.pkg-config pkgs.bubblewrap pkgs.bash
          pi.packages.${system}.default
        ];
        # manylinux wheels (numpy, kickmix on x86_64) need these on NixOS
        libraryPath = pkgs.lib.makeLibraryPath [ pkgs.stdenv.cc.cc.lib pkgs.zlib ];
      };
    in {
      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          tc = toolchain pkgs system;
        in {
          default = pkgs.mkShell {
            packages = tc.packages;
            env = {
              UV_PYTHON = "${tc.python}/bin/python3.12";
              UV_PYTHON_DOWNLOADS = "never";
            } // pkgs.lib.optionalAttrs pkgs.stdenv.isLinux { LD_LIBRARY_PATH = tc.libraryPath; };
          };
        });
    };
}
