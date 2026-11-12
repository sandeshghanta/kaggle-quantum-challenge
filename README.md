# kaggle-quantum-challenge

An auto-research loop for the Kaggle competition *Google Quantum AI: Verifiable Quantum Circuit
Compilation*. A scarce, strong model sets direction, many free local-model agents run experiments
around the clock, and an exact local verifier decides which attempts were good.

The first target is the `invsqrt` task: submit one circuit that computes an inverse square root,
and the score is its cost, Q x (Toffolis + T/4), lower is better. The goal is to beat the best
public result (15.2x the reference, cost 136,796, Q = 82) and push toward the claimed top of the
leaderboard (about 20x). The human submits to Kaggle; the loop never does.

## The system at a glance

| Part | Job |
|---|---|
| **Researcher** | Claude Opus, every 45 minutes within budget: reads what happened, sets direction. |
| **Lab assistants** | Qwen agents, nonstop: edit circuit generators and submit them. |
| **Verifier** | Scores every submission exactly with the competition's own checker. |
| **Substrate** | Append-only event log plus stored programs and circuits; the only channel between parts. |
| **Status page** | Live view of progress, machines and the researcher's reasoning, over Tailscale. |
| **Human** | Watches the status page, steers the researcher, submits to Kaggle. |

![Logical components of the research loop](docs/diagrams/system.svg)

Parts never call each other, so there is no orchestrator and any part can be stopped or replaced alone.

### Islands and parent selection

Every candidate solution is a **program**: generator code that builds a circuit. Programs belong to
**islands**, each island pursuing one idea (one line of attack) and carrying instructions written by
the researcher for the assistants. Islands exist only in the loop, derived from events; the substrate knows nothing about
them. A lab assistant picks an island by weight, then a parent within it, favouring good programs
that few others are already working from. There is no central assignment: every announcement lowers
the parent's weight for the next assistant that reads the log, so concurrent assistants spread out
on their own. The researcher drives island weights, migration, restarts from a chosen program, and
retirement.

### Researcher

The researcher is the only part that spends the scarce strong-model budget, so it runs a few times
a day and never does individual experiments. Each visit it reads a digest prepared by code (how each
island is trending, past directions and how they turned out, recent failure patterns, budget state,
new human instructions) and records its decisions as events:

- a **direction** per decision, with the reason and a checkable prediction;
- the **island changes** that carry the direction out;
- **lessons** (what works, what does not, open questions) that every lab assistant reads;
- **questions** for the human.

Predictions are marked met or missed by code from later events, not by the model. A budget gate
decides whether a visit may happen at all.

### Lab assistants

Lab assistants are many cheap agents working in parallel, one job at a time each. A job chooses an
island and a parent program in it, reads a brief (the parent, the island's instructions, the lessons
so far, what others on the island are trying now, what recently worked and what failed), declares
the one idea it will try, then edits the generator and runs a fast sample check, fixing its change
until the check passes or its budget (time, tokens, checks) runs out. Only a program that passes the
same check again afterwards, with an estimate close to the island's best, is submitted; anything
else is recorded as abandoned, with the reason, so later assistants avoid it. An assistant's
behaviour (prompt, tools, limits) is a versioned role, and every event it writes names that
version, so results are attributable to the behaviour that produced them.

A **program** is a bundle holding a circuit generator; building it yields a circuit, and a
**candidate** is one such circuit. One program has many candidates. The first program is Google's
reference generator, and further islands are seeded from the strongest public generators, one
approach per island.

### Verifier

The verifier is plain code with no model. It scores every new program using the competition's own
checker, in tiers, so the expensive check runs only on candidates that might matter:

- **Circuit**: score the circuit the assistant built and uploaded with its program. The verifier
  never runs an assistant's generator; it builds only the seed generators.
- **Duplicate rejection**: a circuit identical to one already measured is rejected, since
  re-measuring buys nothing.
- **Full-domain sweep**: run the circuit once on every input. This catches circuits that are wrong
  anywhere in the domain and yields a cost estimate.
- **Gate**: reject as "not better" only if the estimate is more than 5% worse than the island's
  best; a random 5% of those rejections go to the full check anyway, as an audit of the gate.
- **Full check**: many shots per input, one salt at a time, stopping at the first failing salt.

Every outcome is recorded as an event, and "not better" means not fully verified, not dropped.

### Substrate

**Purpose.** The substrate is the system's permanent memory: an append-only record of everything
that happened (programs written, circuits measured, decisions taken, resources spent) plus the
files those facts refer to. Every other part reads and writes only through it, and the research
loop itself is disposable: it can be rewritten, or replaced by a different design, and pick up from
the same history.

**Design.** It is event-sourced. Each fact is an immutable event; current state (the best circuit
so far, an island's members, a program's lineage) is computed from events when asked, never stored
and updated. An event is an *envelope* that every event shares (identity, time, type, the thing it
is about, who wrote it, what prompted it) wrapping a type-specific *payload*.

**Versioning.** Event shapes evolve, so the design makes change safe rather than forbidding it:

- The envelope and each event type carry independent version numbers, so one type can change
  without touching any other.
- Every event type has a JSON Schema per version, and every write is validated against it; an
  invalid or unknown event is rejected before anything is stored.
- Nothing already written is ever rewritten. Readers convert old payloads to the current shape on
  read (upcasting) and refuse versions newer than they understand.
- A field whose meaning changes gets a new name, so old and new data can never be confused.

The contract is [`substrate/SPEC.md`](substrate/SPEC.md); every event type and its fields are in
[`substrate/schema/`](substrate/schema/).

### Admin chat

A Qwen-backed agent with read-only access to the substrate. It answers status questions and
explanations itself. Steering and ideas need the researcher, so it shows the current Claude budget
and asks the human to confirm; once confirmed, the message is recorded as an instruction and the
researcher visits immediately if the budget allows, or at its next scheduled visit otherwise. The
strong model is used only after that explicit confirmation.

### Status page

A local page on the devbox, reachable only over the tailnet and laid out for both a phone and a large
screen. From the top:

- **Where we are:** the best ratio against the reference, whether the loop is running, and the
  researcher's current direction.
- **Progress:** the best ratio over time, a plain-rule verdict (improving, slowing or plateau) and
  improvements per day.
- **Alerts:** threshold checks, shown only while their condition holds: a worker or Qwen down, the
  verifier backed up, the researcher not visiting, a full disk, and the Spark, devbox CPU or Claude
  plan going underused.
- **Hardware and Claude:** devbox CPU, memory and disk, Qwen throughput, requests and cache, and
  Claude plan usage, each with 24-hour or 7-day history.
- **Effort by island** and **recent activity.**
- **Researcher:** the current direction with its reasoning and prediction, the decision log with
  outcomes, lessons, open questions and questions for the human.

Two services feed it and never write each other's files. The live updater computes everything except
the researcher's view from the substrate, the worker heartbeats and the machines; the researcher
writes its own view after each visit. Hardware samples and alerts stay out of the substrate.

## Why this shape

The competition's scoring function runs locally, exactly, and for free. So the scarce input is not
the correctness of any single attempt but the volume of attempts, and the loop routes each kind of
work to the cheapest resource that can do it.

| Resource | Cost | Used for |
|---|---|---|
| Local Qwen on a DGX Spark | free | lab assistants: running experiments that follow the researcher's directions |
| Local CPU (16-core NixOS VM) | free | building circuits and verifying them exactly |
| Claude Opus on a $20 Pro plan | scarce: a 5-hour window plus a weekly cap | a few decisions a day: which ideas to pursue, what has been learned |
| The human | scarcest | steering, and submitting to Kaggle |

Consequences that shape the research loop:

- **Volume beats precision.** Scoring is exact and free, so a cheap model's edit only has to be
  right occasionally: the verifier finds the good ones. Nothing counts until it is verified.
- **Strong-model time is the scarcest input.** Opus runs every 45 minutes at most, only while plan usage is below 80%, so it never does
  individual experiments. It reads a prepared summary and decides direction: which lines of attack
  get effort, what has been learned, what to try next.
- **Many assistants must not duplicate each other.** Work is not assigned one by one; programs are
  grouped into islands, and parent selection favours good programs that few others are already
  working from.
- **Full verification is expensive, so it is spent on contenders.** Cheap checks stop most bad
  attempts in seconds; the full multi-salt check runs only on candidates that might beat their
  island.

## Deployment and security

### Storage

Events live in a single SQLite database (WAL mode) whose commit-order sequence number (`seq`) is
the cursor every consumer resumes from; triggers block updates and deletes. Programs and circuits
are stored as files named by their SHA-256 hash, compressed with zstd. The query layer (best
candidates, lineage, budget totals) is a set of read-only projections over the events.

### The substrate service

The substrate runs as `substrated`, the only process that mounts the database and files. Loop
components call it over HTTP on an internal network. Each role has its own random token, delivered
as a container secret; the service maps the token to the role rather than trusting what the caller
claims, and stamps the role on every event itself. A policy file (`loop/policy.json`) limits each
role to the event types it needs:

| Role | May write | May not |
|---|---|---|
| Lab assistant worker | announcements, program submissions, its own spend | measurements |
| Verifier | measurements, rejections, verifier spend | program submissions |
| Researcher | directions, island changes, lessons, questions, quota observations | measurements |

Clients retry with backoff, so `substrated` can be restarted without losing writes.

### Isolation

Models write the code that runs. That code runs only inside lab-assistant job containers, and each
container lives for exactly one job: a read-only root, a size-capped in-memory work directory, no
capabilities, CPU, memory and process limits, and the worker token. When the job ends the container
and everything it wrote are deleted, and the next job starts from the image again. A job container
can reach only the substrate and, through a one-port forwarder, the Qwen endpoint; both networks are
internal, so nothing else is reachable. The worst a job can do with its token is append worker
events; it cannot record a score.

The verifier never runs an assistant's generator: it scores the circuit the job uploaded, so
model-written code never runs where the verifier's token is. A program's circuit could in principle
not match its generator; that only breaks lineage, since children built from that generator fail
their own checks.

Outside containers (development), generator builds and pi jobs are confined with bubblewrap on
Linux and run unsandboxed with a warning on macOS.

### Agents

Every agent runs in the pi coding-agent harness, headless, one fresh process per job. Lab assistants
use a self-hosted Qwen endpoint with a read/edit/search tool allowlist and a capped output budget;
their brief is ordered stable-first so the model server's prefix cache covers it. The researcher
and its digest use Claude Opus through pi-claude-bridge, which drives Claude Code through the
official Agent SDK on a Claude Pro login. The plan is used only by the loop, and the rule is: at
most 80% of either Pro plan limit; the last 20% is a backup. Plan usage arrives only with a
response, so after every Claude run the researcher service records the observed usage, and before a
visit it reads the latest observation: a visit is allowed only if both the five-hour and weekly
figures are under the limit, or the observation is past its reset time. Workers back off
exponentially after repeated failures, so an unreachable endpoint never causes a hot loop.

### Processes

The loop is a Docker Compose project (`compose.yaml`), built from one image. Each part is an
independent service: the substrate, exactly one verifier, the researcher, the live updater, the
status page, and a launcher that keeps N lab-assistant job containers running and replaces each one
when its job ends. The launcher is the only service with access to the container runtime; it starts
one fixed image with fixed arguments and never touches the substrate. Services coordinate only
through the substrate, so any one can be restarted alone. Exactly one verifier runs because nothing
claims a program before verifying it, so a second instance would score everything twice.

The host needs only a container runtime; nothing about the loop lives in its own configuration.
Tool versions (pi, Claude Code, the bridge, uv, Python packages via `uv.lock`) are pinned in the
image. See [`docs/deploy.md`](docs/deploy.md).

## Repository layout

| Path | Contents |
|---|---|
| `substrate/` | The event log: `cli.py` (the in-process API), `server.py` (`substrated`), `_store.py`, `_blobs.py`, `_projections.py`, `_schemas.py`, `upcasters.py`, `types.py`, plus `SPEC.md` |
| `substrate/schema/`, `substrate/fixtures/` | One JSON Schema and one sample payload per event type-version; never deleted |
| `loop/` | The loop: `worker.py`, `launcher.py`, `check.py`, `verifier.py`, `verify.py`, `pool.py`, `seed.py`, `researcher.py`, `live.py`, `statuspage.py`, `pi_runner.py`, `programs.py`, `policy.py`, `policy.json`, `config.py` |
| `roles/` | Versioned roles: lab assistant (`v2`: prompts, `tools.ts` with the plan and check tools, island instructions) and researcher |
| `verifiers/` | Vendored competition verifier and reference generator (Apache-2.0) |
| `tests/` | `substrate/` and `loop/` tests; `fakes/openai_server.py` is a fake model endpoint for smoke tests |
| `Dockerfile`, `compose.yaml`, `compose.test.yaml` | The image, the deployment, and a local end-to-end run against the fake model |
| `scripts/` | `init-secrets` (role tokens), `researcher.sh` (researcher entrypoint and login) |
| `flake.nix` | Dev shell (Python, uv, Node, pi, bubblewrap) for running the pi tests on Linux |
| `docs/` | Design spec and notes, implementation plans, deploy guide |

Out of scope: the other tasks (`dqi`, `dfthcsos`), automatic Kaggle submission, multi-machine
operation, and blob garbage collection.

## Getting started

Development needs Python 3.12 and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
```

Tests that need the `pi` CLI are marked `pi` and skip when it is absent. To run them you need
Linux and the Nix dev shell, which provides Python, uv, Node, and pi:

```sh
nix develop -c uv run pytest -m pi
```

### Running locally

All commands take a data root, which holds `events.sqlite` and `blobs/`. Here they open it
in-process (`--root`, an alias for `--substrate <directory>`); in a deployment the same entry
points use `--substrate http://substrate:7070` with a role token. The worker needs `pi` (use `nix develop`)
and an OpenAI-compatible endpoint.

```sh
ROOT=./var/substrate

# 1. Create the log, register schemas, seed the reference generator
uv run python -m loop.seed --root $ROOT

# 2. The verifier (exactly one)
uv run python -m loop.verifier --root $ROOT --state ./var/verifier.state \
    --workdir ./var/verifier-work --salts 1 2 3 4 --max-workers 8

# 3. One or more lab assistants; --api-key-env names an environment variable holding the key
export QWEN_API_KEY=none        # placeholder for a keyless endpoint
uv run python -m loop.worker --root $ROOT --id 1 \
    --base-url http://localhost:8000/v1 --model <model-id> \
    --api-key-env QWEN_API_KEY --workroot ./var/work/worker-1 \
    --context-window 32768 --max-tokens 4096
```

Run the verifier and each worker in its own terminal. `SIGTERM` makes each finish its current job
and exit. `loop/worker.py` and `loop/verifier.py` document the remaining flags (`--poll`,
`--role-dir`).

Inspect results through the substrate CLI. Subcommands are `init`, `append`, `put-blob`,
`get-blob`, `history`, `since`, `frontier`, `lineage`, `budget` and `export`:

```sh
uv run python -m substrate.cli --root $ROOT frontier invsqrt
uv run python -m substrate.cli --root $ROOT since --types candidate.rejected --limit 20
uv run python -m substrate.cli --root $ROOT budget qwen --window 7d
uv run python -m substrate.cli --root $ROOT export events.jsonl
```

### Deploying

```sh
cp .env.example .env                            # Spark address, model, worker count, Tailscale IP
scripts/init-secrets                            # once: one token per role, in secrets/
docker compose up -d --build
docker compose run --rm researcher scripts/researcher.sh login   # once: Claude Pro login
```

To try the whole stack locally against the fake model, set `SPARK_ADDR=fake-qwen:8099` and
`MODEL_NAME=fake-model` in `.env` and run `docker compose -f compose.yaml -f compose.test.yaml up -d
--build`. [`docs/deploy.md`](docs/deploy.md) covers operating, updating and moving existing data.

## Further reading

- [Research loop design](docs/superpowers/specs/2026-11-11-research-loop-design.md): intent, goals,
  non-goals, constraints, roles, islands, the researcher, versioning.
- [Substrate contract](substrate/SPEC.md): envelope, event types, storage, API, invariants.
- Plans: [substrate](docs/superpowers/plans/2026-11-11-substrate.md),
  [first loop](docs/superpowers/plans/2026-11-11-first-loop.md),
  [substrated, sandbox and sweep tier](docs/superpowers/plans/2026-11-11-substrated-sandbox.md).
- [Deploy guide](docs/deploy.md).
