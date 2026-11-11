# Substrate contract — v1

The permanent layer. Everything above it (loops, researcher, lab assistants, job
types) is disposable and expected to churn. This document is the part that isn't.

## The one rule

> **A loop may only reach the substrate through `cli.py`.**

Nothing else writes to the ledger. Ever. That single restriction is what lets a
future loop — different language, different harness, different architecture — pick
up from a cursor and keep going.

## Layout

```
substrate/
  ledger/events-NNNN.jsonl   append-only. one JSON object per line. THE TRUTH.
  blobs/sha256/ab/cd/<hash>  content-addressed. code, circuits, logs, rationale.
  schema/<kind>/vN.json      JSON Schema per kind-version. never deleted.
  index.sqlite               DERIVED. delete it, replay the log, it's back.
  cli.py                     the only API.
```

Rotate `events-NNNN.jsonl` at ~100 MB. Rotated segments may be gzipped; readers
handle both.

## Envelope

Every event, every kind:

```json
{
  "id":      "01JC7KQF8R2M4VX1A0BZPQ",
  "ts":      "2026-11-11T19:40:12.418Z",
  "kind":    "candidate.measured",
  "schema":  1,
  "subject": "prog:sha256:7f2a…",
  "actor":   {"role":"lab_assistant","model":"qwen3.8-flash-next-fp4",
              "harness":"loops/v1@a3f9c21","host":"spark-9a7e"},
  "idem":    "sha256:…",
  "payload": { … }
}
```

`id` is a ULID — time-prefixed and lexically sortable, so the log is naturally
ordered and a cursor is just a string comparison. No index needed to tail it.

`schema` is **per-kind**, not global. Bumping `candidate.measured` to v2 leaves
every other kind untouched.

`actor` is required as a key; its *contents* are free. Add fields whenever a new
loop architecture needs them. Convention: `role`, `model`, `harness`, `host`.

`subject` — what this event is *about*. Grammar:

```
prog:sha256:<64hex>    a program (and every measurement of it)
bench:<name>           a benchmark, e.g. bench:invsqrt
pool:<name>            a resource pool, e.g. pool:claude
dir:<ulid>             a direction issued by the researcher
hyp:<ulid>             a hypothesis
```

New subject types may be added freely. The `type:rest` shape is the contract.

### `subject` vs `actor`

Easy to conflate, so: **`subject` is what the event is about; `actor` is who wrote
it.** If the log were a table, `subject` is the foreign key and `actor` is the audit
column. One subject accumulates events from many actors:

```
subject: prog:sha256:7f2a…
  program.committed    actor: lab_assistant / qwen     "wrote this code"
  candidate.measured   actor: verifier / kickmix       "scored it: 1402100"
  candidate.measured   actor: verifier / kickmix       "other config: 1511000"
  note                 actor: researcher / opus        "branch looks promising"
```

They serve different queries — `history(subject)` asks *what happened to this
thing*; grouping by `actor.model` asks *which model is earning its budget*.

There is deliberately **no `cand:` subject type**. A candidate is not an
independent entity with a lifecycle; it is one `(program, config)` run. So
`candidate.measured` takes the *program* as its subject, which is what makes
`history("prog:…")` return the commit and all its measurements together rather
than requiring a second query on `payload.program`.

## Stability: accessors, not frozen fields

Nothing is truly frozen. Pretending otherwise produces the worst failure mode of
all — a field keeps its name while its meaning drifts, and every consumer reaches
a confident wrong conclusion. So the substrate does not promise that fields never
change. It makes change **cheap and loud** instead.

The thing that actually breaks is a *query*, not a field. So queries declare what
they need semantically, and field names are an implementation detail of exactly
one file:

```python
# substrate/accessors.py — THE ONLY place in the system that names fields
def cost(ev)        -> float | None      # frontier() needs this
def passed(ev)      -> bool  | None
def program_ref(ev) -> str   | None
def parent_ref(ev)  -> str   | None      # lineage() needs this
def cause_ref(ev)   -> str   | None
def benchmark(ev)   -> str   | None
def resource(ev)    -> Resource | None   # budget() needs this
```

Each dispatches on `(kind, schema)` and is about ten lines. `frontier()` never
names a field; it calls `cost(ev)`. Rename `cost` to `cost_ratio` in v2 and you add
one branch to one accessor — nothing else in the system notices.

This also turns "what breaks if I change this field?" from a promise into a real
dependency graph: grep the accessors.

**Three mechanisms make breakage loud rather than silent:**

1. **Validate on write.** Declared kinds are checked against their schema version;
   `append` hard-rejects a mismatch. A registry nobody enforces drifts in a week.
2. **Conformance fixtures.** One sample event per kind-version, committed to the
   repo. CI asserts every accessor resolves on every fixture, so adding v2 without
   updating an accessor fails CI instead of production.
3. **Semantic tags.** Record what a value *means*, not just what it is called —
   `cost_basis: "invsqrt/QxC/v1"`, `verifier: "kickmix@1.2.0"`. This is the only
   real defence against silent misinterpretation, because a consumer comparing
   across two bases can detect that it must not.

**Migration is optional and cheap.** The log stays heterogeneous forever — that is
the point of event sourcing, not a defect. If accessors get ugly at four versions,
rewriting a few million JSONL lines takes minutes and the original is kept. A
normal Tuesday, not a crisis.

### What is actually irreducible

Two structural properties, and neither is a field name:

1. **`id` is a lexically sortable ULID.** Cursors and therefore resumption depend
   on it. Break this and a new loop can no longer pick up where the last stopped.
2. **Append-only.** The moment anything updates in place, history stops being
   recoverable and the whole premise is gone.

Everything else is soft, including every field listed below.

## Kinds

Three **declared** kinds — payloads validated on write, accessors depend on them.
See `schema/<kind>/v1.json` for field-level documentation.

| Kind | Means | Subject |
|---|---|---|
| `program.committed` | a piece of code now exists | `prog:…` |
| `candidate.measured` | code was built and scored | `prog:…` |
| `budget.spent` | a resource was consumed | `pool:…` |

Everything else is a **free** kind: `direction`, `hypothesis.proposed`,
`decision.made`, `run.started`, `candidate.rejected`, `note`, `human.instructed`,
`schema.registered`, and whatever the next loop invents.

Free kinds still get a schema file with field descriptions — they just aren't
validated. You will want to know what a field meant six weeks from now.

### Two relations that are easy to confuse

- **`parent`** — *what code this came from*. Program → program. Following it
  reconstructs the search tree.
- **`cause`** — *why someone wrote it*. Program → the event that prompted it
  (usually a `direction`).

A program can inherit from `prog:b2…` while being caused by a direction that said
"shrink the table to 2^10." Both are null for a baseline.

`cause` is the most durable of the two because it is **shape-independent** — it is
only an event id, so the join survives any churn in what a `direction` looks like.
It is what answers *did the expensive model earn its budget?*

## API

Ten calls. Nothing else is public.

```python
# ── WRITE ────────────────────────────────────────────────────────────────
put_blob(data: bytes, *, kind: str) -> str
    # kind ∈ {program, circuit, rationale, rollout, log}  (GC policy differs)
    # returns "sha256:…". Idempotent: same bytes → same digest, no-op rewrite.

append(kind: str, payload: dict, *, subject: str, actor: dict,
       schema: int = LATEST, idem: str | None = None) -> str
    # The ONLY writer. Returns the event ULID.
    # Frozen kinds: payload is validated against schema/<kind>/v<schema>.json.
    #   Invalid payload raises SchemaViolation. Nothing is written.
    # Duplicate `idem` returns the existing event id without re-appending.

# ── READ: projections ────────────────────────────────────────────────────
frontier(benchmark: str, *, k: int = 20) -> list[Candidate]
    # Lowest `cost` among candidates with passes == true.
    # Ties broken by earliest `ts` — prefer the one found first, for repeatability.

lineage(ref: str, *, direction: str = "up") -> list[Node]
    # "up"   → ancestors via program.parent, nearest first, ending at a root.
    # "down" → all descendants.

history(subject: str, *, kinds: list[str] | None = None) -> list[Event]
    # Every event with this subject, oldest first.

budget(pool: str, *, window: str = "7d") -> BudgetState
    # Sums tokens_in/tokens_out over the window. Caps live in loop config,
    # not here — the substrate reports spend, it does not enforce policy.

# ── READ: raw ────────────────────────────────────────────────────────────
since(cursor: str | None, *, kinds=None, limit: int = 1000) -> (list[Event], str)
    # Events strictly after `cursor`, oldest first, plus a new cursor.
    # cursor=None starts from the beginning. THIS is how a new loop resumes.

get_blob(digest: str) -> bytes

# ── ADMIN ────────────────────────────────────────────────────────────────
rebuild() -> Stats       # rm index.sqlite, replay the log. Always safe.
gc(policy: GCPolicy) -> Stats
```

## Invariants

These are testable. Write the tests.

1. **Append-only.** No call updates or deletes an event. `gc` touches blobs only.
2. **The log is the truth.** `rebuild()` from an empty index reproduces every
   projection byte-identically. If it doesn't, the index has state the log lacks —
   a bug.
3. **Lock-free concurrent writes.** Event lines stay under 4 KB so a single
   `O_APPEND` write is atomic. That's why blobs are *references*, never inline.
   Twelve lab assistants append with no lock, no daemon, no SQLite write path.
4. **Readers tolerate a torn tail.** A reader may see a partial final line; it
   skips it and picks it up next read. This is the only concession to (3).
5. **Every job is safe to kill.** `put_blob` is idempotent by construction;
   `append` is idempotent under `idem`. Crash mid-job, retry, no double-count.
6. **Status is never stored.** It is derived from the latest event about a subject.
   To mark a hypothesis refuted you *append* that fact.
7. **Validation is enforced, not advisory.** `append` hard-rejects an invalid
   frozen payload. A registry nobody enforces drifts within a week; a lab assistant
   losing one mutation costs seconds and the oracle regenerates it.

## Versioning

Schemas live in `schema/<kind>/vN.json`, are never deleted, and carry
`status: active | deprecated | retired`. Adding a version appends a
`schema.registered` event, so the log records when each version came into use.

**Bump a version when:** a field's type changes, a required field is added, or a
field is removed.
**Don't bump when:** an optional field is added.

> **If a field's *meaning* changes, change its *name* — don't just bump the version.**

Structural change and semantic change are different failures. A version bump
handles the first. For the second, consider `cost` silently changing from
`Q × (Toffolis + T/4)` to a normalized ratio: same name, same type, different
meaning. Code with a v1 adapter survives it. An LLM reading only the current
schema will misinterpret every historical event and reach a confident wrong
conclusion. Renaming to `cost_ratio` makes the discontinuity unmissable to both,
and costs nothing.

### Who reads what

| Task | Reader | Needs canonical shape |
|---|---|---|
| best cost so far, budget remaining | code | yes |
| what does this field mean | LLM | no — reads the schema |
| why did this branch stall | LLM | no — reads events + schema |

**LLMs interpret; code aggregates.** Never hand the researcher two million events
across three schema versions and ask for arithmetic — slow, expensive, subtly
wrong. It receives already-aggregated digests plus a handful of raw events for
colour.

A consequence worth internalising: the `description` strings in the schema files
are **prompt material**, not documentation. The researcher reads them and acts on
them. Write them with that in mind.

Multi-version handling in code starts as `if schema == 1: … elif schema == 2: …`.
That is fine and correct at N ≤ 2. At N = 3, extract one normalize-on-read adapter
per version mapping payload → canonical, so projections only ever see canonical.
Don't build that layer before it hurts.

## Garbage collection

`gc` **never** touches events. Blobs only, by kind:

| Blob kind | Policy |
|---|---|
| `program` | never dropped. Tiny, irreplaceable. |
| `rationale` | never dropped. |
| `circuit` | keep frontier + last 48 h + lineage ancestors of the frontier. |
| `rollout` | keep 14 days, then gzip, then drop. |
| `log` | keep 7 days. |

Circuits are regenerable: `(program, config)` reproduces one deterministically, so
dropping one costs seconds, not information. Without this policy circuit blobs
reach 100 GB+; with it, ~10 GB steady-state.

## Deliberately absent

No `update`. No `delete`. No status setter. No job scheduling. No model calls. No
notion of "researcher" or "lab assistant" — the substrate does not know your
architecture exists, which is the entire point.
