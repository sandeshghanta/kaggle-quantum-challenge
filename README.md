# kaggle-quantum-challenge

An auto-research loop for the Kaggle competition *Google Quantum AI: Verifiable Quantum Circuit
Compilation*. A scarce, strong model sets direction, many free local-model agents run experiments
around the clock, and an exact local verifier decides which attempts were good.

The first target is the `invsqrt` task: submit one circuit that computes an inverse square root,
and the score is its cost, Q x (Toffolis + T/4), lower is better. The goal is to beat the best
public result (15.2x the reference, cost 136,796, Q = 82) and push toward the claimed top of the
leaderboard (about 20x). The human submits to Kaggle; the loop never does.

## The system at a glance

Five logical parts, each with one job:

| Part | Job |
|---|---|
| **Researcher** | A strong model (Claude Opus) that visits a few times a day, reads what happened, and sets direction: which ideas to pursue, what has been learned. |
| **Lab assistants** | Many cheap agents (Qwen) that run experiments nonstop, following the researcher's directions: each one edits a circuit generator and submits it. |
| **Verifier** | Exact, local scoring of every submission against the competition's own checker. Nothing counts until it says so. |
| **Substrate** | The permanent record: an append-only, validated event log plus stored programs and circuits. Every other part reads and writes only through it. |
| **Human** | Watches a progress dashboard, steers through an admin chat, and submits to Kaggle. |

![Logical components of the research loop](docs/diagrams/system.svg)

The researcher directs the lab assistants through the substrate, the verifier judges what they submit, and the human steers the researcher and reads its dashboard.

There is no orchestrator process. Each part is an independent systemd service that coordinates with
the others only by appending to and reading from the substrate, so any one can be restarted alone.

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
- **Strong-model time is the scarcest input.** Opus runs a few times a day, so it never does
  individual experiments. It reads a prepared summary and decides direction: which lines of attack
  get effort, what has been learned, what to try next.
- **Many assistants must not duplicate each other.** Work is not assigned one by one; programs are
  grouped into islands, and parent selection favours good programs that few others are already
  working from.
- **Full verification is expensive, so it is spent on contenders.** Cheap checks stop most bad
  attempts in seconds; the full multi-salt check runs only on candidates that might beat their
  island.

## Components

### Substrate

**Purpose.** The substrate is the system's memory: an append-only record of everything that
happened (programs written, circuits measured, decisions taken, resources spent) plus the files those
facts refer to. Every other part reads and writes only through it, and the research loop itself is
treated as disposable: it can be rewritten, or replaced by a different design, and pick up from the
same history. The contract is [`substrate/SPEC.md`](substrate/SPEC.md).

**Design.** It is event-sourced. Each fact is an immutable event; current state (the best circuit
so far, an island's members, a program's lineage) is computed from events when asked, never stored
and updated. An event is an *envelope* that every event shares (identity, time, type, the thing it
is about, who wrote it, what prompted it) wrapping a type-specific *payload*.

**Versioning.** Event shapes do evolve, so the design makes change safe rather than forbidding it:

- The envelope and each event type carry independent version numbers, so one type can change
  without touching any other.
- Every event type has a JSON Schema per version, and every write is validated against it; an
  invalid or unknown event is rejected before anything is stored.
- Nothing already written is ever rewritten. Readers convert old payloads to the current shape on
  read (upcasters), ignore fields they do not know, and refuse versions newer than they understand
  rather than guess.
- A field whose meaning changes gets a new name, so old and new data can never be confused.

The full list of event types and their fields lives in [`substrate/schema/`](substrate/schema/).

**Implementation.** Events live in a single SQLite database (WAL mode) whose commit-order sequence
number is the cursor every consumer resumes from; updates and deletes are blocked by triggers.
Programs and circuits are stored as files named by their SHA-256 hash, compressed with zstd. The
query layer (best candidates, lineage, budget totals) is a set of read-only projections over the
events.

**As a service.** In deployment the substrate runs as `substrated`, the only process allowed to open
the database and files. Loop components call it over a local Unix socket. It identifies each caller
by its Unix user rather than trusting what the caller claims, and a policy file
(`loop/policy.json`) limits each role to the event types it needs: workers can submit programs but
cannot record measurements, the verifier can record measurements but cannot submit programs.

### Sandboxing

Models write the code that runs, so both places where model output executes are confined with
bubblewrap (`bwrap`):

- **pi jobs** see `/nix/store` and the repo read-only, can write only their own job directory under
  the work root, and get a private `/tmp` and `HOME`. The network stays reachable (the model
  endpoint), but `/run/substrate` is not mounted, so an agent cannot reach the socket. Its only
  route to the log is the worker process that supervises it.
- **`gen.py` builds** run the same way, except the only writable path is the build directory and
  the network is unshared.

On Linux, `bwrap` is required and startup fails if it is missing, rather than silently running
unconfined. On macOS, where it does not exist, both run unsandboxed with a logged warning so that
development and tests still work.

### Programs and candidates

A **program** is a bundle: a directory with an entry script `gen.py <out.kmb>` plus any modules or
data it reads, stored as one deterministic tarball blob (`loop/programs.py`). It builds a circuit
with kickmix's `CircuitBuilder` and writes a `.kmb` file. A **candidate** is one circuit built
from a program, identified by the circuit's hash; one program has many candidates.

Two relations are easy to confuse. `parent` (payload of `program.committed`) is the program this
one was edited from, and rebuilds the search tree. `cause` (envelope, any event) is the event that
prompted this one, and drives attribution, for example from spend back to the announcement it
served, or from an island change back to the researcher's direction.

Seeds are ported generators, not opaque circuits. The first program is Google's reference
generator (about 1x), loaded by `loop/seed.py`, and further islands are seeded from the strongest
public generators, one approach per island.

### Lab assistants

`loop.worker` is the lab assistant: Qwen driven by the pi coding-agent harness, N processes in
parallel. One cycle:

1. Pick an island by weight, then a parent within it (see islands below).
2. Append `attempt.announced` with the island and a plan.
3. Unpack the parent into a scratch directory and run one headless pi job in the sandbox (`--mode
   json`, fresh process per job, no session) with read, edit, grep, find and ls tools and a capped
   output budget. The brief is ordered stable-first (role prompt, island instructions, lessons
   note) so the model server's prefix cache covers it.
4. Record `budget.spent` for the Qwen tokens.
5. If the bundle changed, store it as a blob and append `island.joined` then `program.committed`
   (join first, so a crash leaves a placed-but-unjudged program, never an orphan).
6. Repeat, with exponential backoff after repeated failures so a down endpoint does not cause a
   hot loop.

The role is a versioned package under `roles/lab_assistant/v1/` (system prompt, task template,
island instructions, tool allowlist, timeouts). Every event an agent writes carries
`actor.harness = "<role>/<version>@<git-sha>"`, so results are attributable to the exact behaviour
that produced them.

### Verifier

`loop.verifier` is a plain-Python service with no model. It tails `program.committed` with a
persisted cursor and, for each program that has no verdict yet, runs the tiers below using the
competition's own checker (`verifiers/`, vendored from the competition's official invsqrt verifier
by Google, Apache-2.0). Every outcome is an event, `candidate.measured` or `candidate.rejected`,
plus a `budget.spent` for the CPU used. It logs its backlog (pending count and oldest pending age)
on every loop.

The pipeline is tiered so the expensive check runs only on candidates that might matter:

| Stage | Check | Outcome |
|---|---|---|
| Build | run `gen.py` in the sandbox with a timeout, in a fresh process | `build_failed` |
| Novelty gate | reject a circuit identical (by hash) to one already measured | `duplicate` |
| Sweep | every x in the domain, 1 shot each (about 28 CPU-s); validates the circuit and yields a cost estimate | `quick_check_failed` (tier `sweep`), or an estimate `{cost, Q, T}` |
| Gate | reject as `not_better` only if the estimate is more than 5% worse than the island's best; a random 5% of such rejections go to the full check anyway | `not_better` |
| Full check | 64 shots per x, on salt 1 first, then the remaining salts, stopping at the first failing salt | `candidate.measured`, with `passes: false` and a `failure` if a salt fails |

`not_better` means "not fully verified", not "dropped": the program stays a possible parent at
reduced weight. The 5% audit keeps the gate honest, because its rejections that would have won are
visible and counted. An uncaught error while evaluating one program is recorded as
`candidate.rejected` with reason `verify_error` and the loop moves on.

Why a sweep before the full check: the full check is far more expensive, most candidates are broken
or no better than their island, and the sweep covers every x, so a circuit that is wrong only at
large x is caught without paying for the full check.

Why salts: measurement-based uncomputation draws random outcomes per shot, seeded from the circuit
hash and a salt. A correct circuit passes every outcome; a buggy correction path fails only on
some. More salts mean more outcome coverage, and the leaderboard uses a secret salt, so passing
local seeds is necessary, not sufficient. The reported cost is the worst across salts.

Why identical circuits are rejected: the verifier is deterministic given circuit and salt, so
re-measuring an identical circuit buys nothing. Models also propose no-op or cosmetic edits
constantly, and rejecting them keeps the island from filling with copies.

There is exactly one verifier instance. Nothing claims a program before verifying it, so a second
instance would score everything twice.

### Islands and parent selection

Programs belong to **islands**, each pursuing one idea and carrying instructions written for the
assistants. Island state is derived from events (`island.created`, `island.updated`,
`island.joined`) in `loop/pool.py`; the substrate knows nothing about islands. Membership is its own
event rather than a field on `program.committed`, so one program can join several islands (for
migration or restart) without making its single commit ambiguous.

Workers pick an island by weight, then a parent with weight

```
sigmoid(lambda * z(score)) / (1 + children)
```

where `z` is a robust z-score (median and MAD) of cost within the island and `children` counts
`attempt.announced` events on that parent, that is, jobs handed out, not jobs finished. Programs
not yet judged get a small fixed weight, and rejected programs are never parents.

There is no central assignment: concurrent workers spread out because each announcement lowers the
parent's weight for the next worker that reads the log. The researcher drives island weights,
migration, restarting an island from a chosen program, and retirement.

### Researcher

Opus, run through pi with the pi-claude-bridge extension, which drives Claude Code through the
official Agent SDK on a Claude Pro login. It is the only component that spends the scarce budget,
so it runs a few times a day, never more than a fixed schedule allows.

- **Budget gate.** Plan usage arrives only with a response, so there is no free pre-check. After
  every Claude run the researcher service appends `quota.observed`; before a visit it reads the
  latest one. If that observation is past its reset time the window is assumed fresh; otherwise a
  visit is allowed only if the five-hour and weekly used-percentages are both below 80%; the last
  20% is kept as a backup.
- **Digest.** Input is a digest built by code, not by the model exploring the repo: frontier and
  trend per island, past directions and outcomes, recent failure patterns, assistant hit rate,
  budget state, and new human instructions.
- **Output** is structured and recorded as events: one `direction` per decision with `brief`,
  `why` and a checkable `prediction` (`metric`, `threshold`, `by_attempts`); the island changes
  that carry it out, each with `cause` set to the direction; `lessons.published` (working, not
  working, open questions); and `question.asked` for the human.
- **Outcomes** are closed by code, not by the model: each prediction is marked met, missed or
  open from the events.
- **Lessons note.** The latest `lessons.published` is read by workers into their brief.

The researcher runs as its own Unix user, so the policy lets only it write these event types.

### Admin chat

A Qwen-backed pi agent with read-only substrate tools. Qwen classifies each message. Status
questions and explanations it answers itself. Steering and ideas need the researcher, so it shows
the current Claude budget and asks the human to confirm; on confirmation the message is recorded
as `human.instructed` and the researcher runs immediately if the budget gate allows, otherwise the
request waits for the next scheduled visit. Opus is only used after that explicit confirmation.

### Progress dashboard

A private claude.ai artifact the researcher publishes, laid out as in
[`docs/superpowers/specs/assets/2026-11-11-progress-view-mockup.html`](docs/superpowers/specs/assets/2026-11-11-progress-view-mockup.html)
(the numbers in the mockup are invented). It shows where the score stands against the public best
and the claimed top, best cost over time marked with each researcher direction, system health
(budget bars, assistant hit rate, verifier queue), the island table, the decision log with
predictions and outcomes, and open questions.

Only the researcher updates it. At each visit, code builds `snapshot.json` from the substrate and
the researcher publishes it as the artifact's data file, passed by path so a publish costs a few
hundred tokens. The page therefore refreshes with each visit rather than live. If pi-claude-bridge
does not expose Claude Code's Artifact tool, a small `claude -p` call after each visit does the
publish.

## Key design decisions

| Decision | Why | Trade-off |
|---|---|---|
| Event sourcing: an append-only, validated log is the truth | History is the research record; attribution, replay and new analyses need no migration; status derived from events cannot drift | Every read is a projection over events; no in-place correction, only a new event |
| SQLite with `seq` cursors, behind one writer service | One file, transactional appends, no server database; `seq` is commit order, so cursors never skip even when ULIDs from concurrent writers commit out of order; a single writer avoids `Busy` contention | Single-host; all access goes through `substrated` |
| `substrated` is the only process that can touch the log | Agents run model-written code, so they must not be able to write the log or blobs directly; the data directory is owned by the `substrate` user (0700) | A service to run, and clients must tolerate its restarts (they retry with backoff) |
| OS-level attestation by Unix user (`SO_PEERCRED`) | The server stamps `actor.role` from the caller's uid, so a role cannot be forged from inside a payload | Needs one Unix user per role; Linux only (macOS dev runs unattested) |
| Per-role write policy in `loop/policy.json` | Each role may append only the event types and field values it needs, so a compromised worker cannot write `candidate.measured` or spend from the wrong pool | The policy must grow with each new role; the substrate core stays role-agnostic |
| bubblewrap sandboxes for pi and `gen.py` builds | pi has no sandbox of its own; confining writes to the job directory and hiding the socket (and, for builds, the network) limits what bad model output can do | `bwrap` is required on Linux; macOS runs unsandboxed with a warning |
| The loop uses at most 80% of either Pro plan limit | Plan usage is only visible with a response, so the researcher gates each visit on its last observation; the final 20% is a backup | Fewer researcher visits than the plan could allow |
| Envelope `v` plus per-type `type_v`, with upcasters | The envelope almost never changes; payloads evolve independently. Readers upcast to the latest shape, so projections know only one version, and the stored bytes never change | Each breaking payload change needs an upcaster and a fixture |
| Every event type validated by a schema | Models write events; a bad write must fail loudly at the boundary, not corrupt later projections. Descriptions double as prompts | Adding a type means adding a schema and fixture first |
| No central orchestrator: independent, drain-on-stop services | Each service is restartable alone and the substrate is the only coupling; a crash loses at most one job | Coordination rules must be expressible through events (children counts, cursors) |
| Parent selection without task assignment | The `1/(1 + children)` discount on announced work spreads concurrent workers with no scheduler and no lock | Spreading is statistical, not exact |
| Full-domain sweep, then the full check only if it could win | The full check is ~200M shots per salt; the one-shot sweep covers every x for about 28 CPU-s, catching broken circuits (including large-x bugs) and giving a cost estimate to gate on | A 5% margin lets near-ties through, and a seeded 5% audit of gate rejections checks the gate is not discarding winners |
| Full check stops at the first failing salt | A failing circuit is dead; the remaining salts would only spend CPU | A failure report names one salt, not all that would fail |
| Identical circuits rejected before checking | The verifier is deterministic given circuit and salt; repeating it adds nothing | Hash equality catches only exact copies, not near-duplicates |
| pi for every agent; pi-claude-bridge for the Claude subscription | One harness and one JSONL event format for Qwen and Opus; Anthropic documents Agent SDK use as drawing from subscription limits, and the bridge uses that official path rather than reusing OAuth tokens | Unattended use is a grey area in the consumer terms, mitigated by a few calls a day |
| Nix flake provides toolchain and services; loop code runs from a git checkout | Reproducible Python, Node, pi and systemd units, while a code update is `git pull` plus `uv sync` with no rebuild | Two update paths: flake changes need `nixos-rebuild`, loop code does not |
| `KillMode=mixed` with long `TimeoutStopSec` | systemd sends SIGTERM to the main process only; workers and the verifier finish the current job and record it before exiting | A stop can take up to a job's length (15 minutes for workers, 4 hours for the verifier) |
| The verifier runs exactly once per program, as a single instance | Nothing claims a program before verifying it, so concurrent verifiers would duplicate work and double-count spend | Verification throughput is bounded by one service; it uses many cores per check |
| Islands live in the loop, not the substrate | The substrate stays generic (`frontier`, `lineage`, `history`); islands can change freely | Each loop re-derives island state from events |
| Versioned roles, actions and brief sections | A new behaviour is a new role version plus registered parts and, if it writes new data, one schema; results are attributable per version | More directories and conventions than one script would need |

## Repository layout

| Path | Contents |
|---|---|
| `substrate/` | The event log: `cli.py` (the in-process API), `server.py` (`substrated`), `_store.py`, `_blobs.py`, `_projections.py`, `_schemas.py`, `upcasters.py`, `types.py`, plus `SPEC.md` |
| `substrate/schema/`, `substrate/fixtures/` | One JSON Schema and one sample payload per event type-version; never deleted |
| `loop/` | The loop: `worker.py`, `verifier.py`, `verify.py`, `pool.py`, `seed.py`, `pi_runner.py`, `programs.py`, `policy.py`, `policy.json`, `config.py`, `state.py` |
| `roles/lab_assistant/v1/` | Versioned lab assistant role: system prompt, task template, island instructions, `role.json` |
| `verifiers/` | Vendored competition verifier and reference generator (Apache-2.0) |
| `tests/` | `substrate/` and `loop/` tests; `fakes/openai_server.py` is a fake model endpoint for smoke tests |
| `docs/diagrams/` | The system diagram |
| `flake.nix`, `nix/module.nix` | Dev shell and the `services.research-loop` NixOS module |
| `nix/vm-test/` | A NixOS VM configuration used to prove the module end to end |
| `docs/` | Design spec, implementation plans, deploy guide |

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
points use `--substrate unix:/run/substrate/api.sock`. The worker needs `pi` (use `nix develop`)
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

The devbox imports this repo's NixOS module, which defines the checkout, `substrated`, seed,
verifier, and worker units under one `research-loop.target`, along with the per-role Unix users.
Follow [`docs/deploy.md`](docs/deploy.md) for the flake input, the `services.research-loop`
options, and the operating commands.

## Further reading

- [Research loop design](docs/superpowers/specs/2026-11-11-research-loop-design.md): intent, goals,
  non-goals, constraints, roles, islands, the researcher, versioning.
- [Substrate contract](substrate/SPEC.md): envelope, event types, storage, API, invariants.
- Plans: [substrate](docs/superpowers/plans/2026-11-11-substrate.md),
  [first loop](docs/superpowers/plans/2026-11-11-first-loop.md),
  [substrated, sandbox and sweep tier](docs/superpowers/plans/2026-11-11-substrated-sandbox.md).
- [Deploy guide](docs/deploy.md).
