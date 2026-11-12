# Deploying the research loop

The loop is a Docker Compose project in this repo. The host (the devbox) needs only a container
runtime and Tailscale; nothing about the loop lives in the host's own configuration. The design is
in [`docs/design/compose.md`](design/compose.md).

## 1. Host prerequisites (devbox, NixOS)

In the devbox's `configuration.nix` (DotFiles), enable Docker and let your user run it:

```nix
virtualisation.docker.enable = true;
users.users.<you>.extraGroups = [ "docker" ];
```

Rootless Podman works too (`virtualisation.podman.enable = true; dockerCompat = true;` plus the user
socket); set `CONTAINER_SOCKET` in `.env` to `/run/user/<uid>/podman/podman.sock`.

## 2. Configure

```sh
git clone https://github.com/sandeshghanta/kaggle-quantum-challenge.git && cd kaggle-quantum-challenge
cp .env.example .env          # then edit it
scripts/init-secrets          # one random token per role in secrets/ (git-ignored); appends SECRETS_DIR to .env
```

Settings in `.env`:

| Variable | Meaning |
|---|---|
| `SPARK_ADDR` | `host:port` of Qwen on the Spark, e.g. `100.95.29.126:8000`; the only address job containers can reach |
| `MODEL_NAME`, `MODEL_CONTEXT_WINDOW` | the served model |
| `WORKERS` | lab-assistant job slots (8 matches the Spark's sweet spot) |
| `VERIFIER_MAX_WORKERS` | verifier processes (leave a few of the 16 cores free) |
| `STATUS_BIND` | the devbox's Tailscale IP (`tailscale ip -4`), so only the tailnet reaches the status page |
| `GIT_SHA` | stamped on every event; `export GIT_SHA=$(git rev-parse --short HEAD)` before building |

## 3. Start

```sh
export GIT_SHA=$(git rev-parse --short HEAD)
docker compose up -d --build
docker compose run --rm researcher scripts/researcher.sh login    # once: Claude Pro login
docker compose restart researcher
```

The login is kept in the `researcher-home` volume. Never set `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`
or `ANTHROPIC_AUTH_TOKEN` for the researcher: it must bill the Pro plan, and it refuses to start if one
is set. To run without the researcher, `docker compose up -d --scale researcher=0`.

Open the status page from any device on the tailnet: `http://devbox:8080`.

## 4. Operate

| Task | Command |
|---|---|
| Status | `docker compose ps`; job containers: `docker ps --filter label=research-loop.job` |
| Logs | `docker compose logs -f verifier launcher researcher`; the launcher also logs each finished job's last lines |
| Update | `git pull && export GIT_SHA=$(git rev-parse --short HEAD) && docker compose up -d --build` |
| Pause / resume | `docker compose stop` / `docker compose start` |
| Change worker count | edit `WORKERS` in `.env`, then `docker compose up -d launcher` |
| Query results | `docker compose exec substrate python -m substrate.cli --root /data frontier invsqrt` |
| Back up | `docker compose exec substrate python -m substrate.cli --root /data export /data/export.jsonl`, then `docker compose cp substrate:/data/export.jsonl .` |

Stopping is safe at any time. A stopped lab-assistant job is abandoned (its announcement stays as an
orphan), the verifier redoes the program it was checking, and the researcher finishes its visit
first (up to 16 minutes).

## 5. Moving from the NixOS module

The loop used to run as `services.research-loop` systemd units. To move with the existing history:

```sh
sudo systemctl stop research-loop.target
docker compose build
docker volume create research-loop_substrate-data
docker run --rm --user 0 -v /var/lib/research-loop/substrate:/src:ro -v research-loop_substrate-data:/data \
  research-loop:local sh -c 'cp -a /src/. /data/ && chown -R 10001:10001 /data'
```

Then remove the `research-loop` flake input, its module import and the `services.research-loop`
block from the devbox config, enable Docker (step 1), `nixos-rebuild switch`, and continue at step 2.
Log the researcher in again (step 3). Programs committed before the move carry no circuit, and the
containerised verifier does not run assistants' generators, so any of them still unjudged are
rejected as `build_failed`; the next jobs replace them.

## Security model

- Identity: each role has its own token (`secrets/<role>`, mounted as a Compose secret). The substrate
  maps the token to the role, stamps it on every event, and enforces `loop/policy.json`: a worker
  cannot record measurements, the verifier cannot submit programs.
- Data: only the `substrate` service mounts the event log and blobs (the live updater mounts them
  read-only for disk usage).
- Model-written code runs only in lab-assistant job containers: one job per container, read-only
  root, tmpfs work directory, no capabilities, `no-new-privileges`, CPU, memory and process limits.
  They join only the internal `substrate` and `qwen` networks; `qwen-proxy` forwards one port to the
  Spark and nothing else is reachable.
- The verifier scores uploaded circuits and never runs an assistant's generator.
- The launcher is the only service with the container runtime socket. It runs no model code, starts
  one fixed image with fixed arguments, and takes no input from jobs. With rootless Podman that socket
  carries one unprivileged user's rights.
- The status page is published only on `STATUS_BIND` (the Tailscale IP).
