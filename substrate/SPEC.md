# Substrate contract — v1

The permanent layer under every research loop. Loops, models and job types churn;
this does not.

**A loop may only reach the substrate through `cli.py`.** Nothing else writes
events or blobs.

## Layout

```
substrate/
  events.sqlite              the event log. Append-only. The truth.
  blobs/sha256/ab/cd/<hash>  content-addressed, zstd-compressed: code, circuits, logs, rationale.
  schema/<type>/vN.json      JSON Schema per type-version. Never deleted.
  upcasters.py               converts old payload versions to the latest.
  cli.py                     the only API.
```

## Events

An event is an **envelope** wrapping a **payload**. The envelope is the same for
every event; the payload depends on the type.

```json
{
  "v":       1,
  "id":      "01JC7KQF8R2M4VX1A0BZPQ",
  "ts":      "2026-11-11T19:40:12.418Z",
  "type":    "candidate.measured",
  "type_v":  1,
  "subject": "prog:sha256:7f2a…",
  "actor":   {"role": "lab_assistant", "model": "qwen3.8-flash-next-fp4",
              "harness": "loops/v1@a3f9c21", "host": "spark-9a7e"},
  "cause":   "01JC7K9W3N5TQ0Z8B2XHVFD4RA",
  "payload": { … }
}
```

| Field | Rule |
|---|---|
| `v` | Envelope version. Never moves or renames; readers check it first. |
| `id` | ULID. The event's identity. |
| `ts` | RFC3339 UTC, millisecond precision. |
| `type` | What happened. Dotted lowercase name; selects the payload schema. |
| `type_v` | Payload version, for this type only. |
| `subject` | Which thing it happened to, as `prefix:rest`. |
| `actor` | Who wrote it. Required; contents free (`role`, `model`, `harness`, `host` by convention). |
| `cause` | Optional. Id of the event that prompted this one. |
| `payload` | Type-specific body. |

Schema: `schema/envelope/v1.json`.

### Type and subject

`type` is the verb, `subject` the noun. One subject accumulates many types of
event; one type occurs on many subjects. `history(subject)` reads a row;
filtering by `type` reads a column.

| Prefix | Form | Refers to |
|---|---|---|
| `prog` | `prog:sha256:<hex>` | A program; `<hex>` is its blob digest. |
| `bench` | `bench:<name>` | A competition task: `invsqrt`, `dqi`, `dfthcsos`. |
| `pool` | `pool:<name>` | A resource pool: `claude`, `qwen`, `cpu`. |
| `dir` | `dir:<ulid>` | A direction; `<ulid>` is the id of its `direction` event. |
| `hyp` | `hyp:<ulid>` | A hypothesis; `<ulid>` is the id of its `hypothesis.proposed` event. |

A direction or hypothesis is created with a `bench:` subject; its event id then
becomes the `dir:` / `hyp:` subject for everything said about it afterwards. New
prefixes may be added; the `prefix:rest` shape is fixed.

### Types

**Declared** types have a validated payload and a subject derived from it. Each
schema's `x-subject` template says how; `append` fills the subject in, or rejects
a caller-supplied one that doesn't match.

| Type | Means | `x-subject` |
|---|---|---|
| `program.committed` | code that builds circuits exists | `prog:{blob}` |
| `candidate.measured` | a circuit was built and scored | `prog:{program}` |
| `budget.spent` | a resource was consumed | `pool:{pool}` |

All other types are **free**: payload not validated, subject set by the caller,
still documented by a schema file. Examples: `direction`, `hypothesis.proposed`,
`decision.made`, `note`, `human.instructed`, `schema.registered`.

### Programs and candidates

A **program** is the code a loop edits to build circuits, stored as one blob and
identified by its hash. Bundle every file that affects the output.

A **candidate** is one circuit built from a program, identified by the circuit's
hash. One program has many candidates. Candidates have no subject of their own,
so `history(prog:…)` returns the code and every score from it:

```
subject: prog:sha256:7f2a…
  program.committed    the code exists
  candidate.measured   circuit sha256:91c4…  config {margin: 2} → cost 1.33e13
  candidate.measured   circuit sha256:d03e…  config {margin: 3} → cost 1.36e13
```

Re-measuring a circuit (more seeds, a new verifier) appends another event with the
same circuit hash. `config` records how a circuit was built; it does not promise
the circuit can be rebuilt.

`passes` means the verifier accepted the circuit. Shot-level errors that the
verifier tolerates and prices (dqi) go in `metrics`.

Two relations, easy to confuse:

- `parent` (payload of `program.committed`) — the program this code was edited
  from, or null for a baseline. Follow it to rebuild the search tree.
- `cause` (envelope, any event) — the event that prompted this one: a direction
  for a program, a program for a measurement. Follow it for attribution.

## Storage

```sql
CREATE TABLE events (
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,  -- commit order; the cursor
  v        INTEGER NOT NULL,
  id       TEXT    NOT NULL UNIQUE,            -- ULID
  ts       TEXT    NOT NULL,
  type     TEXT    NOT NULL,
  type_v   INTEGER NOT NULL,
  subject  TEXT    NOT NULL,
  actor    TEXT    NOT NULL,                   -- JSON
  cause    TEXT,                               -- event id, nullable
  payload  TEXT    NOT NULL                    -- JSON, as written
);
CREATE INDEX events_subject ON events(subject, seq);
CREATE INDEX events_type    ON events(type, seq);
CREATE INDEX events_cause   ON events(cause);
```

`seq` is assigned by the store, not part of the envelope. Cursors use `seq`, not
`id`: ULIDs from concurrent writers can commit out of order, `seq` cannot.

Blobs are written to a temp file and renamed into place, so a blob either exists
whole or not at all. Digests are of the raw bytes; zstd is storage only.

## API

```python
# write
put_blob(data: bytes) -> str
    # Returns "sha256:…" of the raw bytes. Idempotent: same bytes, same blob.
append(type: str, payload: dict, *, actor: dict, subject: str | None = None,
       cause: str | None = None, type_v: int = LATEST) -> str
    # The only event writer. Returns the event id.
    # Declared types: invalid payload or mismatched subject raises SchemaViolation;
    #   nothing is written. Subject may be omitted and is derived.
    # Free types: subject is required.

# projections — read payloads upcast to the latest version
frontier(benchmark: str, *, k: int = 20, verifier: str | None = None) -> list[Candidate]
    # Lowest-cost passing Candidates under one verifier (default: the most recently
    # used for this benchmark); ties go to the lowest first_seq.
lineage(ref: str, *, direction: str = "up") -> list[Node]
    # "up": ancestors via parent, nearest first. "down": all descendants.
history(subject: str, *, types: list[str] | None = None) -> list[Event]
    # All events for a subject, in commit order.
budget(pool: str, *, window: str = "7d") -> BudgetState
    # Spend over the window. Reports only; caps live in loop config.

# raw
since(cursor: int | None, *, types=None, limit: int = 1000) -> (list[Event], int)
    # Events after cursor (a seq), in commit order, plus the next cursor.
get_blob(digest: str) -> bytes

# admin
export(path) -> Stats    # dump all events, as written, to JSONL
```

## Read types

Events are what is stored. Read types are what the API returns: projections
computed from events on every query, never stored. They can change without
touching events; they version with `cli.py`, not with `type_v`.

| Type | Built from | Fields |
|---|---|---|
| `Event` | one event, payload upcast to latest | envelope fields, `seq`, `payload` |
| `Candidate` | all `candidate.measured` for one circuit, joined with its `program.committed` | `circuit`, `program`, `benchmark`, `cost`, `passes`, `metrics`, `verifier`, `cost_basis`, `measurements` (count), `first_seq`, `last_seq`, `parent`, `cause`, `note` |
| `Node` | one `program.committed` | `program`, `parent`, `cause`, `benchmark`, `note`, `seq`, `best_cost` (lowest passing `Candidate.cost`, or null) |
| `BudgetState` | `budget.spent` for one pool within the window | `pool`, `window`, `tokens_in`, `tokens_out`, `tokens_cached`, `cpu_seconds`, `usd`, `events` (count) |

**Combining measurements into a `Candidate`.** Only measurements under the
circuit's latest `verifier` count; older ones stay in `history()`. Of those:

- `passes` is true only if every measurement passed.
- `cost` and `metrics` come from the worst (highest-cost) measurement, so a
  lucky seed can't flatter a candidate.

`frontier()` ranks `Candidate`s, and only compares candidates with the same
`cost_basis` and `verifier`.

## Invariants

Each one has a test.

1. **Append-only.** The API has no update or delete, for events or blobs.
2. **Events are stored as written.** Upcasting happens on read; the stored payload
   never changes.
3. **Status is derived, never stored.** Change state by appending an event.
4. **Validation is enforced.** `append` rejects an invalid declared-type payload
   or subject.
5. **Jobs are safe to kill.** `append` is one transaction; `put_blob` is atomic and
   idempotent. A retried job may append a duplicate event; at worst it inflates
   measurement counts or budget totals.
6. **Cursors never skip.** An event committed after `since()` returned cursor `c`
   has `seq > c`.

## Versioning

- **Two independent versions.** `v` for the envelope, `type_v` per type. Bumping
  one never bumps another.
- **The envelope only grows.** It may gain optional fields. A breaking change means
  `v = 2` and a migration; expect never to need it.
- **Tolerant reader.** Readers ignore unknown fields in the envelope and payload,
  and refuse an unknown `v` or `type_v`.
- **Bump `type_v`** when a field is removed, changes type, or becomes required.
  Don't bump for a new optional field.
- **Rename a field if its meaning changes.** A version bump is not enough.
- **Upcasters.** `upcasters.py` holds one function per step (`v1 → v2`, `v2 → v3`).
  On read, every payload is upcast to the latest version, so projections only know
  the current shape. Every type-version has a fixture; CI upcasts each one and
  validates the result against the latest schema.
- **Tag meaning.** `cost_basis` and `verifier` say what a value means; values are
  comparable only when their tags match.
- **Rewriting is the last resort.** If upcasters get unwieldy, copy the log into a
  new one with every payload upcast, and keep the original.
- Schemas carry `status: active | deprecated | retired`. A new version appends a
  `schema.registered` event.

Schema `description` strings are read by the models in the loop. Write them as prompts.

## Out of scope

No update, delete or status setter. No scheduling. No model calls. No knowledge of
loop roles.

No blob GC in v1: every blob is kept. Add GC when disk use calls for it; events
already say which blobs are circuits and which are programs, so it needs no new
fields.
