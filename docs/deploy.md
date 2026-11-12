# Deploying the research loop to the devbox

The devbox (NixOS 26.05, x86_64, config in `DotFiles/personal/nixos/devbox/`) imports this
repo's NixOS module. Nix provides the toolchain and systemd units; the loop code runs from a
git checkout under `/var/lib/research-loop/repo`, so code updates never need a rebuild.

The same module was proven end to end in a local NixOS VM (`nix/vm-test/`): fresh clone from
GitHub, seed, two workers, verifier, drain-on-stop, `research-loop-update`.

## 1. Add the flake input (DotFiles, devbox)

`personal/nixos/devbox/flake.nix`:

```nix
inputs = {
  nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
  research-loop.url = "github:sandeshghanta/kaggle-quantum-challenge";
  research-loop.inputs.nixpkgs.follows = "nixpkgs";
};

outputs = { self, nixpkgs, research-loop, ... }: {
  nixosConfigurations.devbox = nixpkgs.lib.nixosSystem {
    system = "x86_64-linux";
    modules = [ ./configuration.nix research-loop.nixosModules.default ];
  };
};
```

This is additive; it touches nothing on the do-not-touch list in `devbox/AGENTS.md`.

## 2. Enable the service (`configuration.nix`)

```nix
services.research-loop = {
  enable = true;
  repo.url = "https://github.com/sandeshghanta/kaggle-quantum-challenge.git";
  repo.ref = "main";
  workers = 8;                       # Spark's sweet spot: 8 concurrent requests
  model.baseUrl = "http://spark-9a7e.stalk-rudd.ts.net:8000/v1";
  model.name = "nvidia/Qwen3.8-Flash-Next-FP4-Local";
  model.contextWindow = 32768;
  verifier.maxWorkers = 12;          # leave a few of the 16 cores for workers and the OS
  verifier.salts = [ 1 2 3 4 ];
};
```

The Spark endpoint needs no key, so `model.apiKeyFile` stays unset. The repo is public, so no
deploy key is needed.

**Run exactly one verifier.** The module defines a single `research-loop-verifier` unit on
purpose: nothing claims a program before verifying it, so a second instance would score every
program twice.

## 3. Rebuild

```sh
cd ~/workplace/DotFiles && git pull
sudo nixos-rebuild switch --flake ~/workplace/DotFiles/personal/nixos/devbox#devbox
git commit -am "devbox: research loop" && git push
```

On first activation: `research-loop-checkout` clones the repo and runs `uv sync`,
`research-loop-seed` registers schemas and seeds the reference generator, then the verifier and
`research-loop-worker@1..8` start under `research-loop.target`.

## 4. Operate

| Task | Command |
|---|---|
| Status | `systemctl status 'research-loop-*'` |
| Logs | `journalctl -u research-loop-verifier -f`, `journalctl -u 'research-loop-worker@*' -f` |
| Update loop code | `sudo research-loop-update` (pull + `uv sync`, then restarts the verifier and each worker; each finishes its current job first) |
| Pause / resume | `sudo systemctl stop research-loop.target` / `start` |
| Fewer workers now | `sudo systemctl stop research-loop-worker@8` (persist by changing `workers`) |
| Query results | `sudo -u research-loop bash -c 'cd /var/lib/research-loop/repo && uv run python -m substrate.cli --root /var/lib/research-loop/substrate frontier invsqrt'` |

Data lives in `/var/lib/research-loop/substrate` (event log + blobs). Back it up with
`substrate.cli export`.

## Known gaps before leaving it unattended

- Lab assistants (pi) and model-written `gen.py` builds run as the `research-loop` user, which
  can write the event log directly. Sandboxing both, and moving the substrate behind its own
  service, is the next piece of work.
- The quick check covers only x ≤ 65,536 (about 2% of the domain), so circuits that are wrong
  only at large x pass it and fail at the full check. A full-domain one-shot sweep (~28 CPU-s)
  is the proposed replacement.
