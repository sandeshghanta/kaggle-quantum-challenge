# kaggle-quantum-challenge

An auto-research loop for the Kaggle competition *Google Quantum AI: Verifiable Quantum Circuit
Compilation*. A scarce, strong model sets direction, many free local-model agents run experiments
around the clock, and an exact local verifier decides which attempts were good.

The first target is the `invsqrt` task: submit one circuit that computes an inverse square root,
and the score is its cost, Q x (Toffolis + T/4), lower is better. The goal is to beat the best
public result (15.2x the reference, cost 136,796, Q = 82) and push toward the claimed top of the
leaderboard (about 20x). The human submits to Kaggle; the loop never does.

Status: the event-log substrate and a first working loop (seed, lab assistant workers, verifier)
are built, tested, and proven end to end in a local NixOS VM. The researcher, admin chat and
progress dashboard are designed but not built. The loop is **not yet safe to leave unattended**;
see [Current status](#current-status).

## Why this shape

The competition's scoring function runs locally, exactly, and for free. So the scarce input is not
the correctness of any single attempt but the volume of attempts, and the loop routes each kind of
work to the cheapest resource that can do it.

| Resource | Cost | Used for |
|---|---|---|
| Local Qwen on a DGX Spark | free, 24/7, decode-bandwidth bound | thousands of small program edits |
| Local CPU (16-core NixOS VM) | free | building circuits and verifying them exactly |
| Claude Opus on a $20 Pro plan | scarce: a 5-hour window plus a weekly cap, shared with the human | a few decisions a day: which ideas to pursue, what has been learned |
| The human | scarcest | steering, and submitting to Kaggle |

Consequences that shape everything below:

- Judgement is never delegated to a model that can be wrong. A candidate counts only if the
  verifier says so, and the verifier is exact.
- Output tokens are the scarce Qwen resource (aggregate decode saturates around 140 tokens/s, best
  at 8 concurrent requests), so assistants make targeted search/replace edits instead of rewriting
  files.
- The Claude budget is a hard stop, not a soft one. The researcher runs a few times a day and only
  when a reserve is left for the human's own Claude use.
- The loop is disposable; the event log is not. All durable state lives in the substrate, so the
  loop can be rewritten without losing history.

## System overview

```
                         +----------------------+
                         |        human         |
                         +----------+-----------+
                steers / asks |     ^ reads, submits to Kaggle
                              v     |
  +--------------------+  +---------+----------+     publishes snapshot
  |     admin chat     |  |  progress dashboard|<-----------------------+
  | Qwen, read-only    |  |  claude.ai artifact|                        |
  | (planned)          |  |  (planned)         |                        |
  +---------+----------+  +--------------------+                        |
            | confirmed steer                                           |
            v                                                           |
  +---------+---------------------------------------------------+       |
  |  researcher service (planned): schedule, quota gate, digest |-------+
  |  +--> Opus visit via pi + pi-claude-bridge (Claude Pro)     |
  +---------+---------------------------------------------------+
            | directions, island events, lessons note
            v
  +---------------------------------------------------------------------+
  |              substrate: append-only event log + blobs               |
  |   events.sqlite (validated JSON events)   blobs/ (programs, circuits)|
  |             reached only through substrate/cli.py                   |
  +--------+---------------------------------------------+--------------+
           ^ attempt.announced, island.joined,           ^ candidate.measured,
           | program.committed, budget.spent             | candidate.rejected, budget.spent
           |                                             |
  +--------+------------------+                 +--------+---------------+
  | lab assistant workers x N |                 |  verifier service x 1  |
  | pi + Qwen on the DGX Spark|                 |  build, check, record  |
  | (built)                   |                 |  exact kickmix checker |
  +---------------------------+                 +------------------------+
```

There is no orchestrator process. Each box is an independent systemd service that coordinates with
the others only by appending to and reading from the substrate, so any one can be restarted alone.

## Components

### Substrate (built)

The permanent layer under every loop: an append-only event log plus a content-addressed blob store.
Loops, models and job types change; this does not. The contract is in
[`substrate/SPEC.md`](substrate/SPEC.md).

- **Events** are an envelope (`v`, ULID `id`, `ts`, `type`, `type_v`, `subject`, `actor`, optional
  `cause`) wrapping a type-specific payload. `type` is the verb, `subject` the noun
  (`prog:sha256:...`, `bench:invsqrt`, `pool:qwen`), so `history(subject)` reads a row and filtering
  by type reads a column.
- **Every type is validated.** Each type-version has a JSON Schema in `substrate/schema/<type>/v<N>.json`
  and a fixture. `append` rejects unknown types and non-conforming payloads, and nothing is written.
  Schema descriptions are read by the models, so they are written as prompts.
- **Storage** is SQLite (`events.sqlite`) with a `seq` autoincrement column as the cursor, and
  blobs on disk by SHA-256, zstd-compressed, written by temp-file-and-rename.
- **Read types** (`Candidate`, `Node`, `BudgetState`) are projections computed on every query and
  never stored. A `Candidate` combines all measurements of one circuit under its latest verifier:
  it passes only if every measurement passed, and its cost is the worst measurement, so a lucky
  seed cannot flatter it.
- **Invariants**, each with a test: append-only (no update or delete in the API), events stored as
  written, status derived and never stored, validation enforced, jobs safe to kill, cursors never
  skip.

Event types in use:

| Type | Meaning |
|---|---|
| `program.committed` | code that builds circuits exists (`blob`, `parent`, `note`) |
| `candidate.measured` | a circuit got a full verification (cost, passes, shots, seeds, verifier, cost basis) |
| `candidate.rejected` | an attempt stopped before a full verification, with a reason |
| `attempt.announced` | a lab assistant's one-line plan; also the unit of "children" for parent selection |
| `island.created` / `island.updated` / `island.joined` | island lifecycle and membership |
| `budget.spent` | tokens, CPU or wall time consumed from a pool (`qwen`, `cpu`, `claude`) |
| `quota.observed`, `direction`, `lessons.published`, `question.asked`, `human.instructed`, `loop.config_changed` | defined and validated now, written by the planned researcher and admin chat |
| `schema.registered` | a schema version came into use |

### Programs and candidates (built)

A **program** is a bundle: a directory with an entry script `gen.py <out.kmb>` plus any modules or
data it reads, stored as one deterministic tarball blob (`loop/programs.py`). It builds a circuit
with kickmix's `CircuitBuilder` and writes a `.kmb` file. A **candidate** is one circuit built
from a program, identified by the circuit's hash; one program has many candidates.

Two relations are easy to confuse. `parent` (payload of `program.committed`) is the program this
one was edited from, and rebuilds the search tree. `cause` (envelope, any event) is the event that
prompted this one, and drives attribution, for example from spend back to the announcement it
served.

Seeds are ported generators, not opaque circuits, so the first program is Google's reference
generator (about 1x), loaded by `loop/seed.py`. Seeding from the strongest public generators is
planned.

### Lab assistant workers (built)

`loop.worker` is the lab assistant: Qwen driven by the pi coding-agent harness, N processes in
parallel (start at 8, matching the Spark's optimum). One cycle:

1. Pick an island by weight, then a parent within it (see islands below).
2. Append `attempt.announced` with the island and a plan.
3. Unpack the parent into a scratch directory and run one headless pi job (`--mode json`, fresh
   process per job, no session) with read, edit, grep, find and ls tools and a capped output
   budget. The brief is ordered stable-first (role prompt, island instructions, lessons note) so
   the Spark's prefix cache covers it.
4. Record `budget.spent` for the Qwen tokens.
5. If the bundle changed, store it as a blob and append `island.joined` then `program.committed`
   (join first, so a crash leaves a placed-but-unjudged program, never an orphan).
6. Repeat, with exponential backoff after repeated failures so a down endpoint does not cause a
   hot loop.

The role is a versioned package under `roles/lab_assistant/v1/` (system prompt, task template,
island instructions, tool allowlist, timeouts). Every event an agent writes carries
`actor.harness = "<role>/<version>@<git-sha>"`, so results are attributable to the exact behaviour
that produced them.

### Verifier (built)

`loop.verifier` is a plain-Python service with no model. It tails `program.committed` with a
persisted cursor and, for each program that has no verdict yet, builds it and runs the competition's
own checker (`verifiers/`, vendored from the competition's official invsqrt verifier by Google, Apache-2.0). Every outcome is
an event: `candidate.measured` or `candidate.rejected`, plus a `budget.spent` for the CPU used.

The pipeline is tiered so the expensive check runs only on candidates that might matter:

| Stage | Check | Rejection reason |
|---|---|---|
| Build | run `gen.py` with a timeout, in a fresh process | `build_failed` |
| Novelty gate | reject a circuit identical (by hash) to one already measured | `duplicate` |
| Quick check | exact verifier restricted to x <= 65,536, one pass | `quick_check_failed` |
| Gate | quick cost must beat the island's best quick cost | `not_better` |
| Full check | full domain, 64 shots per x, once per salt (default 4 salts) | `candidate.measured` with `passes: false` and a `failure` |

An uncaught error while evaluating one program is recorded as `candidate.rejected` with reason
`verify_error` and the loop moves on.

Why salts: measurement-based uncomputation draws random outcomes per shot, seeded from the circuit
hash and a salt. A correct circuit passes every outcome; a buggy correction path fails only on
some. More salts mean more outcome coverage, and the leaderboard uses a secret salt, so passing
local seeds is necessary, not sufficient. The reported cost is the worst across salts.

Why identical circuits are rejected: the verifier is deterministic given circuit and salt, so
re-measuring an identical circuit buys nothing and the duplicate would only waste the slowest
step. Models also propose no-op or cosmetic edits constantly, and rejecting them keeps the island
from filling with copies.

There is exactly one verifier. Nothing claims a program before verifying it, so a second instance
would score everything twice.

### Islands and parent selection (built)

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
parent's weight for the next worker that reads the log. Island weights, migration, restarting an
island from a chosen program, and retirement are meant to be driven by the researcher; today there
is a single hand-seeded island.

### Researcher (planned)

Opus 5.5, run through pi with the pi-claude-bridge extension, which drives Claude Code through the
official Agent SDK on a Claude Pro login. It is the only component that spends the scarce budget,
so it runs a few times a day, never more than a fixed schedule allows.

- **Budget gate.** Plan usage arrives only with a response, so there is no free pre-check. After
  every Claude run the researcher service appends `quota.observed`; before a visit it reads the
  latest one. If that observation is past its reset time the window is assumed fresh; otherwise a
  visit is allowed only if the five-hour and weekly used-percentages are below thresholds that
  keep a reserve (default 30%) for the human.
- **Digest.** Input is a digest built by code, not by the model exploring the repo: frontier and
  trend per island, past directions and outcomes, recent failure patterns, assistant hit rate,
  budget state, and new human instructions.
- **Output** is structured and recorded as events: one `direction` per decision with `brief`,
  `why` and a checkable `prediction` (`metric`, `threshold`, `by_attempts`); the island changes
  that carry it out, each with `cause` set to the direction; `lessons.published` (working, not
  working, open questions); and `question.asked` for the human.
- **Outcomes** are closed by code, not by the model: each prediction is marked met, missed or
  open from the events.
- **Lessons note.** The latest `lessons.published` is already read by workers into their brief.

Schemas for all of these event types exist; the service that writes them does not.

### Admin chat (planned)

A Qwen-backed pi agent with read-only substrate tools. Qwen classifies each message. Status
questions and explanations it answers itself. Steering and ideas need the researcher, so it shows
the current Claude budget and asks the human to confirm; on confirmation the message is recorded
as `human.instructed` and the researcher runs immediately if the budget gate allows, otherwise the
request waits for the next scheduled visit. Opus is only used after that explicit confirmation.

### Progress view (planned)

A private claude.ai artifact, laid out as in
[`docs/superpowers/specs/assets/2026-11-11-progress-view-mockup.html`](docs/superpowers/specs/assets/2026-11-11-progress-view-mockup.html)
(the numbers in the mockup are invented). It shows where the score stands against the public best
and the claimed top, best cost over time marked with each researcher direction, system health
(budget bars, assistant hit rate, verifier queue), the island table, the decision log with
predictions and outcomes, and open questions.

Only the researcher updates it. At each visit, code builds `snapshot.json` from the substrate and
the researcher publishes it as the artifact's data file, passed by path so a publish costs a few
hundred tokens. The page therefore refreshes about every three hours, not live. An open check is
whether pi-claude-bridge exposes Claude Code's Artifact tool; if not, a small `claude -p` call
after each visit does the publish.

## Key design decisions

| Decision | Why | Trade-off |
|---|---|---|
| Event sourcing: an append-only, validated log is the truth | History is the research record; attribution, replay and new analyses need no migration; status derived from events cannot drift | Every read is a projection over events; no in-place correction, only a new event |
| SQLite with `seq` cursors | One file, transactional appends, no server; `seq` is commit order, so cursors never skip even when ULIDs from concurrent writers commit out of order | Single-host; one writer at a time (to be mediated by a service, see status) |
| Envelope `v` plus per-type `type_v`, with upcasters | The envelope almost never changes; payloads evolve independently. Readers upcast to the latest shape, so projections know only one version, and the stored bytes never change | Each breaking payload change needs an upcaster and a fixture |
| Every event type validated by a schema | Models write events; a bad write must fail loudly at the boundary, not corrupt later projections. Descriptions double as prompts | Adding a type means adding a schema and fixture first |
| Writer-supplied subjects; verifying them is deferred | The shape (`prefix:rest`) is checked and conventions are in each schema; deriving subjects from payloads adds machinery without a current threat | A buggy writer can mis-file an event until attestation lands |
| No central orchestrator: independent, drain-on-stop services | Each service is restartable alone and the substrate is the only coupling; a crash loses at most one job | Coordination rules must be expressible through events (children counts, cursors) |
| Parent selection without task assignment | The `1/(1 + children)` discount on announced work spreads concurrent workers with no scheduler and no lock | Spreading is statistical, not exact |
| Quick check, then full check only if it could win | The full check is ~200M shots per salt; most candidates are broken or no better than their island, and are cheaply refuted first | The quick check covers only about 2% of the domain, so large-x bugs pass it; a full-domain sweep tier is planned |
| Identical circuits rejected before checking | The verifier is deterministic given circuit and salt; repeating it adds nothing | Hash equality catches only exact copies, not near-duplicates |
| pi for every agent; pi-claude-bridge for the Claude subscription | One harness and one JSONL event format for Qwen and Opus; Anthropic documents Agent SDK use as drawing from subscription limits, and the bridge uses that official path rather than reusing OAuth tokens | pi has no sandbox, so isolation is ours; unattended use is a grey area in the consumer terms, mitigated by a few calls a day |
| Nix flake provides toolchain and services; loop code runs from a git checkout | Reproducible Python, Node, pi and systemd units, while a code update is `git pull` plus `uv sync` with no rebuild | Two update paths: flake changes need `nixos-rebuild`, loop code does not |
| `KillMode=mixed` with long `TimeoutStopSec` | systemd sends SIGTERM to the main process only; workers and the verifier finish the current job and record it before exiting | A stop can take up to a job's length (15 minutes for workers, 4 hours for the verifier) |
| The verifier runs exactly once per program, as a single instance | Nothing claims a program before verifying it, so concurrent verifiers would duplicate work and double-count spend | Verification throughput is bounded by one service; it uses many cores per check |
| Islands live in the loop, not the substrate | The substrate stays generic (`frontier`, `lineage`, `history`); islands can change freely | Each loop re-derives island state from events |
| Versioned roles, actions and brief sections | A new behaviour is a new role version plus registered parts and, if it writes new data, one schema; results are attributable per version | More directories and conventions than one script would need |

## Repository layout

| Path | Contents |
|---|---|
| `substrate/` | The event log: `cli.py` (the only API), `_store.py`, `_blobs.py`, `_projections.py`, `_schemas.py`, `upcasters.py`, `types.py`, plus `SPEC.md` |
| `substrate/schema/`, `substrate/fixtures/` | One JSON Schema and one sample payload per event type-version; never deleted |
| `loop/` | The loop: `worker.py`, `verifier.py`, `verify.py`, `pool.py`, `seed.py`, `pi_runner.py`, `programs.py`, `config.py`, `state.py` |
| `roles/lab_assistant/v1/` | Versioned lab assistant role: system prompt, task template, island instructions, `role.json` |
| `verifiers/` | Vendored competition verifier and reference generator (Apache-2.0) |
| `tests/` | `substrate/` and `loop/` tests; `fakes/openai_server.py` is a fake model endpoint for smoke tests |
| `flake.nix`, `nix/module.nix` | Dev shell and the `services.research-loop` NixOS module |
| `nix/vm-test/` | A NixOS VM configuration used to prove the module end to end |
| `docs/` | Design spec, implementation plans, deploy guide; `architecture.html` is the original proposal, superseded by the spec |

## Current status

Built and tested:

- **Substrate.** Append-only validated log, blobs, projections, upcasters, CLI, and the invariant
  tests.
- **First loop.** Seed, N lab assistant workers (pi + Qwen), and the single verifier with the
  quick and full tiers, duplicate rejection, per-island gating, and spend accounting. Proven
  end to end in a local NixOS VM: fresh clone from GitHub, seed, two workers against a fake model,
  verifier, drain-on-stop, and `research-loop-update`.
- **Deployment.** A flake with a dev shell and a NixOS module, described in
  [`docs/deploy.md`](docs/deploy.md).

Next, in order:

1. **`substrated` service and sandboxing.** The substrate behind its own Unix-user service on a
   socket, with per-role attestation (the server reads the caller's uid and stamps `actor.role`,
   and a policy table limits what each role may append), and bubblewrap sandboxes for pi edits and
   `gen.py` builds. See [the plan](docs/superpowers/plans/2026-11-11-substrated-sandbox.md).
2. **Sweep verification tier.** A full-domain one-shot sweep (about 28 CPU-seconds) replacing the
   2%-of-domain quick check, with a margin gate and a seeded audit of rejections.
3. **Researcher, then admin chat, then the dashboard.**
4. **Seeding from the strongest public generators**, one approach per island.

Be clear about the gap: today the lab assistants (pi) and the model-written `gen.py` builds run as
the same Unix user that owns the event log, so an agent can in principle write the log or blobs
directly. Until step 1 lands, run the loop only where that is acceptable and supervised. Out of
scope for v1: the other tasks (`dqi`, `dfthcsos`), automatic Kaggle submission, multi-machine
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

All commands take a data root, which holds `events.sqlite` and `blobs/`. The worker needs `pi`
(use `nix develop`) and an OpenAI-compatible endpoint.

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

The devbox imports this repo's NixOS module, which defines the checkout, seed, verifier, and
worker units under one `research-loop.target`. Follow [`docs/deploy.md`](docs/deploy.md) for the
flake input, the `services.research-loop` options, the operating commands, and the known gaps.

## Further reading

- [Research loop design](docs/superpowers/specs/2026-11-11-research-loop-design.md): intent, goals,
  non-goals, constraints, roles, islands, the researcher, versioning.
- [Substrate contract](substrate/SPEC.md): envelope, event types, storage, API, invariants.
- Plans: [substrate](docs/superpowers/plans/2026-11-11-substrate.md) (built),
  [first loop](docs/superpowers/plans/2026-11-11-first-loop.md) (built),
  [substrated, sandbox and sweep tier](docs/superpowers/plans/2026-11-11-substrated-sandbox.md)
  (next, not built).
- [Deploy guide](docs/deploy.md).
- [`docs/architecture.html`](docs/architecture.html): the original architecture proposal (historical; superseded by the design spec).
