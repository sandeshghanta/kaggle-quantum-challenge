# Research loop — design

Date: 2026-11-11. Builds on `substrate/SPEC.md` (the permanent event log), which is
not yet implemented and is part of this work.

## Intent

Get a good score in the Kaggle competition *Google Quantum AI — Verifiable Quantum
Circuit Compilation*. The bar: clearly above the ~11.7 anyone can get by combining
the best public notebooks, pushing toward the top of the leaderboard (~20).

The competition's scoring function runs locally, exactly and for free (kickmix
verifiers in `~/workplace/temp/quantum/data/comp/verifiers`). That makes volume of
attempts, not correctness of any single attempt, the scarce input. So the system
pairs:

- one **researcher**: a strong model (Opus) used sparingly to set direction, and
- many **lab assistants**: a weaker free model (self-hosted Qwen) that runs
  experiments nonstop, with the verifier deciding which attempts were good.

**Start with one task: invsqrt.** It has the cheapest verification (a ~0.5 MB circuit),
a single-circuit submission, and a simple cost. Other tasks come later by reusing
the machinery.

## Goals

1. Beat the best public invsqrt result (15.2×, cost 136,796, Q=82) and push toward
   the claimed top (~20.6×).
2. Run unattended for days: survive crashes, never exhaust the Claude budget, never
   lock the human out of their own Claude usage.
3. Give the human a bird's-eye view of progress and of the researcher's decisions,
   and a chat to steer it.
4. Stay modular: new kinds of guidance (e.g. a "common pitfalls" note — already in
   v1 as the lessons note), new tasks, or a rewritten loop must not require
   redesigning the substrate.

## Non-goals (v1)

- dqi and dfthcsos.
- Submitting to Kaggle automatically. The human submits.
- Multi-machine operation, or verification on the DGX Spark.
- Blob garbage collection.

## Anti-goals (failure even if it "works")

- Agents all converging on one idea, so the search stops widening.
- Burning a 5-hour Claude window in one go, or the loop starving the human's own
  Claude Code / claude.ai use.
- "Improvements" that pass local seeds but fail on the leaderboard's secret salt.
- An agent writing to the event log or blob store outside `cli.py`, or damaging
  the machine.

## Constraints

**Hardware.** Everything except Qwen runs on one NixOS VM: Intel Core Ultra 7 265K,
16 cores (KVM), 25 GiB RAM. The DGX Spark is reserved exclusively for serving Qwen
(`qwen3.8-flash-next`) over an OpenAI-compatible endpoint on the LAN. Qwen is free
and available 24/7.

**Qwen serving (measured 2026-11-11 from the devbox over Tailscale; full numbers in
the Spark session's `docs/spark-throughput.md`).**
- Endpoint `http://spark-9a7e.stalk-rudd.ts.net:8000/v1`, model id
  `nvidia/Qwen3.8-Flash-Next-FP4-Local`, vLLM, prefix caching on.
- KV cache ~918k tokens, so memory is not a constraint at 16–22k contexts.
- **Decode bandwidth is the constraint.**
  - Aggregate throughput saturates at ~137–147 tok/s.
  - 1 stream: 61.6 tok/s. 8 streams: ~20 tok/s each. 16 streams is worse end-to-end
    than 8.
  - **Optimum is 8 concurrent requests** (matches vLLM `--max-num-seqs 8`).
- Prefill is cheap, and prefix caching makes a repeated 22k prompt ~16× faster. A
  stable standing brief is nearly free after the first call.
- **A program's cost is almost entirely its output tokens.** Measured on single-shot
  calls:

  | Output per program | Programs per hour |
  |---|---|
  | 3,000 tokens (full file) | ~176 |
  | 1,000 tokens | ~530 |
  | 300 tokens (diff) | ~1,870 |

  Agentic pi runs take several turns, so expect fewer programs per hour than these
  figures.
- Max context length (`max_model_len`) can be read from `/v1/models`.

**Claude budget.** A personal $20 Claude **Pro** plan. Facts (official docs,
checked 2026-11-11):
- Opus 5.5 is available on Pro. There is also a separate "Opus limit".
- No published quota. Pro gets a 5-hour window (starts at the first message, resets
  5 h later) plus a weekly cap.
- One pool, shared with the human's claude.ai and interactive Claude Code use.
- Remaining usage is readable: `five_hour` / `seven_day` used-percentage and reset
  times arrive as `rate_limit_event`s, which pi-claude-bridge forwards as
  `anthropic-ratelimit-unified-*` headers.
- Hitting a limit appears as result text `You've hit your … limit · resets <time>`,
  not a reliable exit code.
- `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` must be unset,
  or calls silently bill the API. Usage credits stay off, so limits are a hard stop.

**Terms.** Per Anthropic's support article (updated 2026-10-07), Agent SDK, `claude -p`
and third-party app usage draw from subscription limits. The researcher therefore
runs through **pi-claude-bridge**, which drives Claude Code via the official Agent SDK
and does not reuse OAuth tokens directly. Unattended use is a grey area in the Consumer
Terms ("ordinary, individual usage"). Keeping the researcher to a few calls a day
reduces the risk; the human accepts what remains.

**Harness.** pi (`@earendil-works/pi-coding-agent`, v1.5.x, MIT) for **every** agent.
Facts:
- Headless via `--mode json` (one-shot JSONL events) or `--mode rpc` (long-running,
  JSONL over stdio). No Python SDK, so the orchestrator spawns pi processes and parses
  JSONL.
- Qwen is configured as a provider in `models.json` (`api: "openai-completions"`,
  `baseUrl`, Qwen thinking format). Set `contextWindow` and `maxTokens` to the server's
  real values.
- Tool allowlists via `--tools`. Custom tools are TypeScript extensions.
- Per-run token usage is reported in events.
- **No sandbox.** Isolation is ours to provide.
- Running many instances at once is undocumented. Give each its own session dir and
  working dir, and load-test.

**Substrate.** All durable state lives in the substrate (`substrate/SPEC.md`):
- events in `events.sqlite`, blobs on disk
- written and read only through `cli.py`
- every event type has a schema in `substrate/schema/` and is validated on write
- read types (`Candidate`, `Node`, `BudgetState`) are projections computed from events

The loop is disposable; the substrate is not.

**Verifier facts (invsqrt).**
- Every x in [1, 3,133,452] is checked, 64 shots each by default (~200M shots).
- Shots probe the seeded outcomes of measurement-based uncomputation. Seeds come from
  the circuit's SHA-256, merged with `--salt`.
- `--shots-per-x` lowers the shot count for cheaper checks.
- Cost = Q × (Toffolis + T/4).

**Development and deployment.**
- Code is built on the human's Mac and pushed to GitHub. The devbox pulls it and runs it.
- The devbox is a Proxmox VM running NixOS 26.05. Its config is a flake in the DotFiles
  repo: `~/workplace/DotFiles/personal/nixos/devbox/`, rebuilt with
  `sudo nixos-rebuild switch --flake …#devbox`.
- Read `devbox/AGENTS.md` before touching it. It lists settings that must not change
  (display manager, the Tailscale/DNS/firewall sync list, `hardware-configuration.nix`,
  `stateVersion`).
- No Docker/Podman or bubblewrap is configured today.
- Tests that need Linux (sandboxing, systemd limits, kickmix's Linux build) run on the
  devbox. Everything else should run on the Mac.

## Design (decided)

### Roles

| Role | Model / harness | Runs | Job |
|---|---|---|---|
| Researcher | Opus 5.5 via pi + pi-claude-bridge, Pro login | a few times a day, budget permitting; or on human-confirmed request | set direction at the island level; maintain the lessons note; log decisions with predictions |
| Lab assistant | Qwen via pi, OpenAI-compatible endpoint | continuously, N in parallel | edit generator programs, submit candidates |
| Admin chat | Qwen via pi, read-only tools | on demand | answer status questions; route steering and ideas to the researcher after confirmation |
| Orchestrator | plain Python, no model | always | schedule, launch, supervise, verify, pace, record |

### Orchestrator

- **Process model:** systemd keeps the orchestrator and long-lived services running
  and enforces CPU and memory limits. A small Python scheduler runs inside it.
- **Packaging (my call):** this repo ships a `flake.nix` with two outputs:
  - a dev shell with Python/uv, Node + pi, and kickmix able to run on NixOS
  - a NixOS module (e.g. `services.research-loop`) defining the systemd units and the
    sandbox tooling

  The devbox flake imports the module, a small, additive change to DotFiles that leaves
  the do-not-touch list alone.
- **Qwen concurrency:** keep at most 8 requests in flight to the Spark (my call, from the
  measurements). Lab assistant count N can exceed 8 only if assistants spend real time
  off-model, building and verifying. Start with N = 8.
- **State:** queues and loop state are derived from substrate events where possible.
  Anything that can't be is kept in a loop-owned table or file, never in the event log.
  Durable-step libraries (DBOS) are allowed but not required. Temporal is out (too
  heavy for one VM).
- **Responsibilities:**
  - wake the researcher on schedule if the budget allows
  - keep N lab assistants running and hand them jobs
  - run the verification queue under resource limits
  - record `budget.spent` for every agent run
  - relay admin chat
  - kill runaway processes

### The program pool: islands (camps)

- Programs (generator source bundles; see the SPEC) belong to **islands**. Each island
  pursues one idea and carries **island instructions** written by the researcher.
- Within an island, the pool keeps the **best program per bucket** (MAP-Elites), not
  just one best. The first bucket dimension is qubit count. The builder picks bin edges.
  Fitness is the competition cost, lower is better.
- **Migration:** occasionally copy an island's best into another island, as the
  researcher directs. The researcher can also restart an island from a chosen program,
  or create and retire islands.
- **Island membership** is its own event, `island.joined {program, island, via}`,
  not a field on `program.committed`. A program can join several islands (migration,
  restart), and its single `program.committed` stays unambiguous. `submit` writes
  `program.committed` + `island.joined` together. Island lifecycle is `island.created`
  and `island.updated`. All types are defined in `substrate/schema/`.
- **Island and bucket projections live in the loop**, computed from `since()` /
  `history()`. The substrate's generic `frontier()` stays as specified; islands are a loop
  concept, not a substrate one. *(My call.)*

### What a program is

- A program is a Python generator that builds the circuit with kickmix's
  `CircuitBuilder` and writes a `.kmb` file, like `~/workplace/temp/quantum/work/dqi/gen_dqi.py`.
  Builds of invsqrt-sized circuits take well under a second.
- **Contract (my call):** a program bundle is a directory with an entry script
  `gen.py <out.kmb> [--config config.json]`, plus any modules and data files it reads.
  It is stored as one blob (a deterministic tarball). The verifier input is the `.kmb`.
- Lab assistants edit files in the bundle. The researcher's island instructions can narrow
  the edit surface (e.g. "only change `newton.py`") if hit rates are low.
- The public notebooks contain real `CircuitBuilder` generator source (e.g.
  `quantum-circuit-compilation-from-source`, `quantum-circuits-source-exact-compilation`),
  so seeds are ported generators, not opaque circuits.

### Lab assistant cycle

1. **Get a job.** The orchestrator picks an island by the researcher's allocation
   weights. It then picks a parent in that island with weight
   `sigmoid(λ·z(score)) × 1/(1 + children)`, where z is a robust z-score (median/MAD)
   within the island, after ShinkaEvolve. `children` counts jobs **handed out** from that
   parent, not completed ones, so concurrent assistants spread out.
2. **Announce.** The assistant appends `attempt.announced` with a one-line plan
   ("trying: merge the two table lookups in the Newton step"). The resulting
   `program.committed` has this announcement as its `cause`.
3. **Read its brief:**
   - parent source and score
   - 2 inspiration programs from the island
   - island instructions
   - the researcher's lessons note
4. **Level-2 visibility.** A read-only pi tool, scoped to the assistant's own island,
   returns short summaries: recent attempts with outcomes and common errors, and what
   others in the island are trying now (their announcements). No access to other
   islands' details or raw history.
5. **Edit and submit.** The assistant edits a copy of the program in its own sandboxed
   working directory using pi's tools, then submits through a `submit` tool. That
   records `program.committed` (with `parent`, `island`, `cause`) and enqueues
   verification.
6. **Novelty gate.** Before any verification, reject candidates that are near-duplicates
   of programs already in the island. The builder picks the method (e.g. identical
   circuit hash after build, embedding similarity of source, or both). A rejection is
   recorded as `candidate.rejected` (`duplicate`).
7. **Tiered verification:**
   - **quick** check first (invsqrt: `--shots-per-x 1`), then the **full** check
     (default 64 shots per x, 4 salts, configurable) only if the quick check passes
     **and** the candidate would beat its bucket's best. The ladder is loop code, not
     part of the substrate contract, and can change freely.
   - A full check writes `candidate.measured` (with `shots`, `seeds`, `verifier`,
     `cost_basis`; failures carry the error in `failure`).
   - Anything that stops earlier writes `candidate.rejected` with a reason
     (`build_failed`, `quick_check_failed`, `duplicate`, `not_better`) and the error
     text. These feed level-2 summaries and hit-rate statistics.
   - **Why salts matter:** measurement-based uncomputation outcomes are random per shot,
     seeded from the circuit hash plus salt. A correct circuit passes for every outcome; a
     buggy correction path fails only on some outcomes. More salts mean more outcome
     coverage. The leaderboard uses a secret salt.

8. **Feedback.** Failures and their errors appear in later assistants' level-2 summaries.

**Output discipline (from the Spark measurements).** Output tokens are the scarce
resource, so assistants make targeted edits with pi's `edit` tool (search/replace), not
full-file `write`s. The brief tells them to keep reasoning short, and per-run output
tokens are capped. Brief content is ordered stable-first (role prompt, island
instructions, lessons note) so the Spark's prefix cache covers it.

Then the assistant asks for the next job. Whether each job is a fresh pi process
(`--mode json`) or one long-lived RPC session is the builder's call. Fresh processes are
simpler and avoid context build-up.

### Researcher visit

- **When:** on a schedule (default every ~3 hours, my call) and only if the 5-hour and
  weekly used-percentages are below thresholds that keep a reserve for the human (default
  reserve: 30% of each window, my call; configurable). Also immediately on a
  human-confirmed request from the admin chat, subject to the same check.
- **Reading the budget (my call):** usage percentages only arrive with a response, so
  there is no free pre-check. After every Claude run the orchestrator appends
  `quota.observed`. The gate reads the latest one:
  - If the last observation is past its reset time, the window is assumed fresh and one
    visit is allowed.
  - Otherwise, a visit is allowed only if the last observed value is below the threshold.
  - After every visit, the new values are recorded.
  - The human's own use is invisible until the next observation. The reserve is the
    margin for that.
- **Input:** a prepared digest built by code, not by the model reading the repo:
  - frontier and trend per island
  - the decision log: past directions, predictions, outcomes
  - recent failure patterns
  - lab assistant hit rate
  - budget state
  - new human instructions and ideas since the last visit

  The researcher prompt is fixed, so caching works. It runs single-shot or with a small
  read-only tool set; no repo-wide agentic exploration.
- **Output** (structured, recorded as events), any of:
  - one `direction` per decision: `brief`, `why`, and a checkable `prediction`
    (`metric`, `threshold`, `by_attempts`)
  - the island changes that carry each decision out (`island.created`,
    `island.updated`, `island.joined` for migration or restart), each with `cause` = the
    direction
  - `lessons.published` (working / not working / open questions)
  - `question.asked` for the human
- **Outcome tracking:** code, not the model, closes each prediction (met / missed / open)
  from events, for the decision log.
- **Failure handling:** if a limit is hit, parse the reset time and do not retry before
  it. If the researcher is unavailable, assistants keep working on the last direction.

### Admin chat

- A Qwen-backed pi agent with read-only substrate tools.
- **Classification:** Qwen classifies each message:
  - **Answerable by Qwen** (status, explanations, what's in the log): it answers
    directly.
  - **Needs the researcher** (steering, ideas, judgement calls): it says so, shows
    current Claude budget, and asks the human to confirm. On confirm, the message is
    recorded as `human.instructed` and the researcher runs **immediately** (if the budget
    check passes; otherwise it says why and queues it for the next visit). The result is
    reported back in the chat.
- **Where it lives:** the UI is the builder's call. It can be part of the progress view.

### Progress view

A local web page for one task (invsqrt). Layout: the "Scoreboard + researcher
insights" mockup in `docs/superpowers/specs/assets/2026-11-11-progress-view-mockup.html`
(numbers in it are made up). Refreshes by polling (my call).

- **Where we stand:**
  - ratio vs reference
  - best cost (with Q, T)
  - gap to public best (15.2×) and to the claimed top (~20.6×)
- **Chart:** best cost over time, marked with each researcher direction, with a
  public-best reference line.
- **Health:**
  - researcher budget (5 h / 7 d bars, reserve)
  - lab assistants (count, attempts today, hit rate, smoke-fail rate)
  - verifier (queue, cores, RAM, timeouts)
- **Researcher:**
  - current direction (why / expect / explore share)
  - island table (idea, assistants, best, trend, researcher's call)
  - decision log with predictions and outcomes, plus tokens per improvement
  - current beliefs (= lessons note)
  - open questions for the human

All numbers come from substrate read types and projections. The view never writes
events, except through the admin chat path.

**Attribution:** every `budget.spent` event sets envelope `cause` to the event the work
served (the researcher visit's trigger, a lab assistant's `attempt.announced`, the
program a verifier run checked). Who spent it is in `actor`. "Tokens per improvement"
follows improvement → program → `cause` chain → spend.

### Seeding

- **Day one:** load the best public invsqrt circuits/generators as island seeds,
  re-verified locally on ≥4 seeds. These are the 23 downloaded notebooks in
  `~/workplace/temp/quantum/notes/lit/kaggle_notebooks/`; see `literature_review.md` §1
  and §3. The notebooks are untrusted: read and port them, don't execute them as-is.
- Each seed is a root `program.committed` with `parent: null`.
- Different public approaches go in different islands.

### Safety

- Each lab assistant runs as a sandboxed process with its own working directory and
  CPU/memory limits (systemd-run and/or bubblewrap on NixOS).
- No direct access to `events.sqlite` or `blobs/`; writes happen only via the
  `submit` tool → `cli.py`.
- Verification jobs run under per-task resource limits. For invsqrt this is mostly a
  CPU-slot limit; the verifier uses multiple cores per run.

### Extensibility and versioning

Goal: adding or changing what the researcher, lab assistants or admin chat do is a
local, versioned change, and every result can be attributed to the exact behaviour
version that produced it.

**Roles are versioned packages.**
- Each role lives in `loops/roles/<role>/v<N>/`:
  - prompt templates
  - pi tool allowlist and extensions
  - its output schema (researcher, admin)
  - role config
- A version is immutable once it has run. A change means a new `v<N+1>` directory.
- Every event an agent writes carries `actor.harness = "<role>/v<N>@<git-sha>"`, so
  the substrate can answer "did lab_assistant v3 beat v2 per Qwen token?".

**The orchestrator selects versions by config, and can split traffic.**
- Config (in git) names the active version per role, optionally with weights, e.g.
  80% of lab assistants on v2 and 20% on v3, for A/B comparison.
- Activating or changing a version appends a `loop.config_changed` event, so the log records when each behaviour was live.

**Behaviour is composed from registered, typed parts, each a small module.**
- **Researcher actions:** the researcher's output is a list of typed actions
  (`island.create`, `island.restart`, `island.set_weight`, `lessons.publish`,
  `question.ask`, …). Each action type has a payload schema and a handler that turns it
  into events. A new capability (e.g. "publish a pitfalls checklist", "request an
  ablation run") = a new action type + handler + schema, plus a role version that's
  allowed to use it.
- **Brief sections:** a lab assistant's brief is assembled from registered sections
  (parent, inspirations, island instructions, lessons note, level-2 summary, …). New
  guidance for assistants = a new section, enabled per role version.
- **Digest sections:** likewise for what the researcher reads.
- **Job kinds:** lab assistant work is typed: `mutate` in v1; later e.g. `fix` (repair a
  failing program), `crossover` (combine two programs), `tune` (parameter sweep without
  a model). Kinds are chosen by configured weights.

**Data stays versioned by the substrate rules.**
- A new event type = a new schema file (validated like every other type).
- Changing a type's payload follows `type_v` / upcasters.
- Read types version with `cli.py`.

Rule of thumb: a new feature touches one role version, one or more registered parts,
and (if it writes new data) one schema file. It never touches the substrate core or
other roles' existing versions.

## Left to the builder

- **Setup:** making kickmix run on NixOS (FHS wrapper or source build), and the
  pi / vLLM configuration.
- **Concurrency and verification:** final N (start at 8), verification slot counts,
  quick-check shot count, and timeouts. Measure on the VM. At the measured Qwen rates,
  invsqrt verification is not expected to bind; confirm.
- **Pool:** exact bin edges, migration cadence defaults, the novelty-gate method and
  threshold, and λ.
- **Researcher:** digest format and size, and the structured output schema. Keep the
  "why" and "prediction" fields.
- **UI:** the web stack for the progress view and the admin chat.
- **Build order.** Suggested:
  1. substrate
  2. oracle on the VM
  3. seeding
  4. lab assistants with a fixed hand-written direction
  5. researcher
  6. admin chat
  7. full progress view

## Open risks to watch

- **Qwen hit rate** on real generator edits. Measure in week one. If it's near zero,
  narrow edit surfaces (single functions or parameters).
- **Verifier throughput** on the VM. It decides N.
- **pi with 10–20 concurrent instances** is undocumented.
- **Anthropic policy changes** for Agent SDK use on subscriptions ("for now").
