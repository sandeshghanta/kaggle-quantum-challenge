# Running the loop with Docker Compose

The loop runs as a Compose project from this repo. The host only needs a container runtime (Docker,
or Podman with its Docker-compatible socket); nothing about the loop lives in the host's own config.

## Services

| Service | Job | Networks | Mounts | Secret |
|---|---|---|---|---|
| `substrate` | the event log and blobs, over HTTP on port 7070 | `substrate` | `substrate-data` (rw) | all role tokens |
| `seed` | one-shot: seeds islands and programs | `substrate` | | `seed` |
| `verifier` | scores every committed circuit | `substrate` | | `verifier` |
| `launcher` | keeps N lab-assistant job containers running | none | container runtime socket, `status` (rw) | none |
| job containers | one lab-assistant job each, then discarded | `substrate`, `qwen` | tmpfs only | `worker` |
| `qwen-proxy` | forwards to the Spark's Qwen endpoint, nothing else | `qwen`, default | | none |
| `researcher` | visits every 45 minutes | `substrate`, default | `researcher-home` (rw), `status` (rw) | `researcher` |
| `live` | writes live.json and history.json | `substrate`, `qwen` | `status` (rw), `substrate-data` (ro, disk usage) | `live` |
| `statuspage` | serves the page | default (published on the Tailscale IP only) | `status` (ro) | none |

`substrate` and `qwen` are internal networks: nothing on them can reach the internet. The only way out
from a job container is `qwen-proxy`, which forwards one TCP port to the Spark.

## Identity

Each role has a random token, generated once by `scripts/init-secrets` into `secrets/<role>.token`
(git-ignored) and mounted as a Compose secret at `/run/secrets/<role>`. Clients send it as
`Authorization: Bearer <token>`; the substrate maps it to the role and stamps `actor.role` itself, as it
did with Unix users. The write policy (`loop/policy.json`) is unchanged.

## Lab-assistant jobs

The launcher runs `docker run --rm` for each job with a read-only root, a size-capped tmpfs for `/work`
and `/tmp`, no capabilities, `no-new-privileges`, CPU, memory and process limits, and the worker token.
The harness inside picks an island and a parent from the substrate, runs one job and exits; the
container and everything it wrote are then deleted, and the launcher starts a fresh one. The launcher
also kills a job that outlives its budget by a margin. Model-written code runs in the job container
itself; there is no inner sandbox. The worst it can do with the worker token is append worker events.

The launcher holds the container runtime socket. It is our code only, starts one fixed image with fixed
arguments, and takes no input from jobs. With rootless Podman the socket carries one unprivileged
user's rights, not root's.

## The verifier scores circuits, not generators

Building `gen.py` runs model-written code. Doing that inside the verifier's container would expose the
verifier token, the one that records scores. So the job uploads the circuit its final check built, and
`program.committed` carries it (`circuit`); the verifier scores that circuit. Seed programs (role
`seed`, our own code) are still built by the verifier. A worker could submit a circuit its generator
does not produce; that only breaks lineage, and children built from that generator fail their own
checks.

## Deploy

```
cp .env.example .env            # Spark address, model, worker count, Tailscale IP
scripts/init-secrets            # once
docker compose up -d --build
docker compose run --rm researcher login    # once: Claude Pro login, kept in researcher-home
```

Update: `git pull && docker compose up -d --build`. Services drain on SIGTERM as before
(`stop_grace_period` covers a job or a visit).
