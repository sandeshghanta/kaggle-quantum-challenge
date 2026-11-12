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
| Query results | `sudo -u rl-verifier env PYTHONPATH=/var/lib/research-loop/repo /var/lib/research-loop/repo/.venv/bin/python -c "from substrate.client import RemoteSubstrate as R; print(R('/run/substrate/api.sock').frontier('invsqrt'))"` |

Data lives in `/var/lib/research-loop/substrate` (event log + blobs). Back it up with
`substrate.cli export`.

## 5. Open the progress page

The page is served on the devbox itself, only to the tailnet. From any device on the tailnet
(laptop or phone with Tailscale on) open:

    http://devbox:8080

The module runs two small units under `research-loop.target`: `research-loop-live` (user `rl-live`)
rewrites `live.json` every 15 s from the substrate and the worker heartbeats, and
`research-loop-statuspage` (user `rl-status`) serves the page. The page also reads
`researcher.json`, written only by the researcher service; until the first researcher visit it shows
"No researcher visit yet". The two files have separate writers and never touch each other's output.

The port binds on all interfaces, but the firewall trusts only `tailscale0` and does not open it, so
nothing outside the tailnet can reach it. Options:

```nix
services.research-loop.statusPage = {
  enable = true;            # default
  port = 8080;              # default
  tailscaleServe = false;   # true: also https://devbox.<tailnet>.ts.net via `tailscale serve`
};
```

Files live in `/var/lib/research-loop/status/{live,workers,researcher}`. Check with
`systemctl status research-loop-live research-loop-statuspage` and
`curl -s localhost:8080/live.json | jq .score` on the devbox. `rl-live` is a read-only role in
`loop/policy.json`, so after updating the loop code restart `substrated` (`research-loop-update` does).

## Security model

- Each role runs as its own Unix user: `substrate`, `rl-deploy`, `rl-worker`, `rl-verifier`, `rl-seed`, `rl-live` (read-only), `rl-status` (files only).
- Only `substrated` (user `substrate`) can open the event log and blobs in
  `/var/lib/research-loop/substrate` (mode 0700). Everyone else talks to it over
  `/run/substrate/api.sock`; the server identifies each caller by its Unix user and enforces
  `loop/policy.json` (a worker cannot record measurements, the verifier cannot submit programs).
- pi jobs and generator builds run inside bubblewrap sandboxes that can write only their own job
  directory and cannot see the substrate socket. Builds have no network.
- The loop code checkout is owned by `rl-deploy` and read-only for every other role.
