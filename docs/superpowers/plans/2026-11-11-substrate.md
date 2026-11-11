# Substrate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the substrate: the validated, append-only event log plus blob store and query API that every research loop reads and writes through.

**Architecture:** A Python package at `substrate/` (next to the existing `SPEC.md` and `schema/`). It has a `Substrate` class over one SQLite file (WAL) and a content-addressed, zstd-compressed blob directory. `substrate/cli.py` is the only public surface: it re-exports the class for Python callers and provides a JSON command line for non-Python callers (pi extensions). Read types are frozen dataclasses computed on every query, never stored.

**Tech Stack:** Python 3.12, uv, `jsonschema` (Draft 2020-12), `zstandard`, stdlib `sqlite3`, pytest.

**Spec:** `substrate/SPEC.md` and `substrate/schema/*/v1.json` (the contract). Context: `docs/superpowers/specs/2026-11-11-research-loop-design.md`.

**This is plan 1 of 6.** The later plans, each written when its predecessor lands:
2. the verifier on the devbox, plus a Nix flake dev shell
3. seeding invsqrt from public notebooks
4. lab assistants and the orchestrator core, with a fixed hand-written direction
5. the researcher and the budget gate
6. the admin chat and the progress view

## Global Constraints

- Python `>=3.12,<3.13`. Run everything with `uv run`. No other runtimes.
- **Dependencies:** `jsonschema>=4.23`, `zstandard>=0.23`. Dev: `pytest>=8`. Nothing else.
- The only writer of events and blobs is `substrate/cli.py`. Internals are private modules (leading `_`).
- **Events table:** exactly the `CREATE TABLE events` from SPEC §Storage, plus triggers that abort any `UPDATE` or `DELETE` on `events`.
- Cursors are `seq` (int), never `id`.
- Envelope `v` is `1`. `id` is a 26-char Crockford ULID matching `^[0-9A-HJKMNP-TV-Z]{26}$`. `ts` is RFC3339 UTC with milliseconds and a trailing `Z`.
- **Digests** are `"sha256:" + hex(sha256(raw bytes))`. Blobs are stored zstd-compressed at `blobs/sha256/<h[0:2]>/<h[2:4]>/<h>` and written via temp file + `os.replace`.
- **Schemas** load from `substrate/schema/<type>/v<N>.json`. `envelope` is not an event type. `LATEST` for a type is its highest `N`. JSON Schema `format` keywords are not enforced in v1.
- **Data root:** the `root` argument, else env `SUBSTRATE_ROOT`, else `<repo>/var/substrate`. `var/` is gitignored. (This is a deliberate deviation from the SPEC's layout listing, which shows `events.sqlite` inside `substrate/`. Data stays out of the source tree.)
- The subject is writer-supplied. `append` checks only the `^[a-z]+:.+$` shape.

## Review Focus

1. **Many processes appending at once** (8 lab assistants, the orchestrator, the verifier): no lost events, no `database is locked` errors, `seq` unique and gap-free. Test in Task 2.
2. **Missing or truncated blobs** (a crash mid-write, or a digest from another machine): `get_blob` raises `BlobNotFound`. No half-written file is ever visible under the final path. Test in Task 3.
3. **An event written by a newer loop** (`v=2`, or a `type_v` with no schema here): reads raise `UnknownVersion`, never a silent misread. Test in Task 4.
4. **Measurements under two verifiers or two cost bases** for the same benchmark: `frontier()` never ranks them together. Test in Task 5.
5. **The same program blob committed twice** (two assistants write identical code): one `Node`, lineage has no duplicates. Test in Task 6.

---

### Task 1: Package scaffold, schema registry, fixtures

**Files:**
- Create: `pyproject.toml`
- Create: `substrate/__init__.py` (empty)
- Create: `substrate/_schemas.py`
- Create: `substrate/fixtures/<type>/v1.json` for all 15 event types
- Create: `tests/substrate/test_schemas.py`
- Modify: `.gitignore` (add `var/`, `.venv/`, `__pycache__/`)

**Interfaces:**
- Produces:
  - `class SchemaViolation(ValueError)`
  - `class UnknownVersion(LookupError)`
  - `event_types() -> list[str]`: sorted; excludes `envelope`.
  - `latest_version(type: str) -> int`: raises `SchemaViolation` for an unknown type.
  - `validate_payload(type: str, type_v: int, payload: dict) -> None`: raises `SchemaViolation` (message includes the type and the jsonschema error path) or `UnknownVersion` when there is no `v<type_v>.json` for that type.
  - `validate_envelope(env: dict) -> None`: raises `SchemaViolation`.
  - `schema_digest(type: str, type_v: int) -> str`: `sha256:` of the schema file bytes.

- [ ] **Step 1: Write `pyproject.toml`.** Project `kaggle-quantum-challenge`, `requires-python = ">=3.12,<3.13"`, dependencies as in Global Constraints, `[dependency-groups] dev = ["pytest>=8"]`, `[tool.uv] package = false`, `[tool.pytest.ini_options] testpaths = ["tests"]`. Then run `uv sync`. Expected: `.venv` created, no errors.

- [ ] **Step 2: Write one fixture per event type.** Each fixture is a minimal valid payload in `substrate/fixtures/<type>/v1.json`. Use `sha256:` + 64 hex chars wherever a digest is required. For example, `direction` is `{"brief": "...", "why": "...", "prediction": {"metric": "island:3/best_cost", "threshold": 125000, "by_attempts": 300}}`.

- [ ] **Step 3: Write the failing tests**

```python
from pathlib import Path
import json, pytest
from substrate import _schemas as s

FIX = Path("substrate/fixtures")

def test_every_type_has_a_fixture_and_vice_versa():
    assert sorted(p.name for p in FIX.iterdir()) == s.event_types()
    assert len(s.event_types()) == 15 and "envelope" not in s.event_types()

@pytest.mark.parametrize("t", s.event_types())
def test_fixture_validates(t):
    s.validate_payload(t, 1, json.loads((FIX / t / "v1.json").read_text()))

def test_missing_required_field_is_rejected():
    with pytest.raises(s.SchemaViolation, match="direction"):
        s.validate_payload("direction", 1, {"brief": "x", "why": "y"})

def test_unknown_type_is_rejected():
    with pytest.raises(s.SchemaViolation):
        s.latest_version("no.such.type")

def test_unknown_version_raises():
    with pytest.raises(s.UnknownVersion):
        s.validate_payload("direction", 99, {})

def test_extra_payload_fields_are_allowed():   # tolerant reader
    p = json.loads((FIX / "question.asked" / "v1.json").read_text()) | {"new_field": 1}
    s.validate_payload("question.asked", 1, p)

def test_envelope_rejects_unknown_top_level_field():
    env = {"v": 1, "id": "01JC7KQF8R2M4VX1A0BZPQ3XYZ", "ts": "2026-11-11T19:40:12.418Z",
           "type": "question.asked", "type_v": 1, "subject": "bench:invsqrt",
           "actor": {}, "payload": {"question": "q"}, "extra": 1}
    with pytest.raises(s.SchemaViolation):
        s.validate_envelope(env)
```

- [ ] **Step 4: Run the tests.** `uv run pytest tests/substrate/test_schemas.py -v`. Expected: FAIL (`ModuleNotFoundError: substrate._schemas`).

- [ ] **Step 5: Implement `substrate/_schemas.py`.** Load the schema files once (`functools.cache`) and build validators with `jsonschema.Draft202012Validator`. Report the first error by `best_match`.

- [ ] **Step 6: Run the tests.** Same command. Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock .gitignore substrate/__init__.py substrate/_schemas.py substrate/fixtures tests/substrate/test_schemas.py
git commit -m "substrate: schema registry, validation, fixtures for every type"
```

---

### Task 2: Event store: append, history, since

**Files:**
- Create: `substrate/_ulid.py`
- Create: `substrate/types.py`
- Create: `substrate/_store.py`
- Test: `tests/substrate/test_store.py`

**Interfaces:**
- Consumes (Task 1): `validate_payload`, `validate_envelope`, `latest_version`, `SchemaViolation`.
- Produces:
  - `new_ulid() -> str` and `utc_now_iso() -> str`, both in `_ulid.py`.
  - `@dataclass(frozen=True) class Event` with fields `seq: int, v: int, id: str, ts: str, type: str, type_v: int, subject: str, actor: dict, cause: str | None, payload: dict`, in `types.py`.
  - `class Store(path: Path)` in `_store.py`. It opens SQLite with `journal_mode=WAL`, `busy_timeout=10000`, and creates the table, indexes and the two abort triggers if missing. Methods:
    - `append(type: str, payload: dict, *, subject: str, actor: dict, cause: str | None = None, type_v: int | None = None) -> str`
    - `history(subject: str, *, types: list[str] | None = None) -> list[Event]`
    - `since(cursor: int | None, *, types: list[str] | None = None, limit: int = 1000) -> tuple[list[Event], int]`
    - `raw_rows(types: list[str] | None = None) -> list[Event]`: payloads as stored, used by later tasks.
  - `since` returns the input cursor (or `0` when `None`) as the next cursor when there are no new events.

- [ ] **Step 1: Write the failing tests**

```python
import multiprocessing as mp, sqlite3, pytest
from substrate._store import Store
from substrate._schemas import SchemaViolation

A = {"role": "test"}
Q = {"question": "q"}

def test_append_returns_ulid_and_history_returns_it(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    eid = st.append("question.asked", Q, subject="bench:invsqrt", actor=A)
    [ev] = st.history("bench:invsqrt")
    assert ev.id == eid and len(eid) == 26 and ev.seq == 1 and ev.v == 1 and ev.type_v == 1
    assert ev.ts.endswith("Z") and ev.payload == Q and ev.cause is None

def test_invalid_payload_writes_nothing(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    with pytest.raises(SchemaViolation):
        st.append("question.asked", {}, subject="bench:invsqrt", actor=A)
    assert st.since(None) == ([], 0)

def test_bad_subject_shape_rejected(tmp_path):
    with pytest.raises(SchemaViolation):
        Store(tmp_path / "e.sqlite").append("question.asked", Q, subject="invsqrt", actor=A)

def test_history_filters_types_and_keeps_commit_order(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    st.append("question.asked", Q, subject="bench:x", actor=A)
    st.append("human.instructed", {"text": "t", "kind": "idea"}, subject="bench:x", actor=A)
    st.append("question.asked", Q, subject="bench:y", actor=A)
    assert [e.type for e in st.history("bench:x")] == ["question.asked", "human.instructed"]
    assert [e.type for e in st.history("bench:x", types=["human.instructed"])] == ["human.instructed"]

def test_since_pages_and_never_skips(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    for _ in range(5): st.append("question.asked", Q, subject="bench:x", actor=A)
    page, c = st.since(None, limit=3)
    assert [e.seq for e in page] == [1, 2, 3] and c == 3
    st.append("question.asked", Q, subject="bench:x", actor=A)
    page, c = st.since(c)
    assert [e.seq for e in page] == [4, 5, 6] and c == 6
    assert st.since(c) == ([], 6)

def test_update_and_delete_are_blocked(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    st.append("question.asked", Q, subject="bench:x", actor=A)
    db = sqlite3.connect(tmp_path / "e.sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("UPDATE events SET subject='bench:z'"); db.commit()
    with pytest.raises(sqlite3.DatabaseError):
        db.execute("DELETE FROM events"); db.commit()

def _writer(path, n):
    st = Store(path)
    for _ in range(n): st.append("question.asked", Q, subject="bench:x", actor=A)

def test_concurrent_appends_from_processes(tmp_path):        # Review Focus 1
    path = tmp_path / "e.sqlite"; Store(path)
    procs = [mp.get_context("spawn").Process(target=_writer, args=(path, 50)) for _ in range(8)]
    for p in procs: p.start()
    for p in procs: p.join(); assert p.exitcode == 0
    evs, _ = Store(path).since(None, limit=10_000)
    assert [e.seq for e in evs] == list(range(1, 401)) and len({e.id for e in evs}) == 400
```

- [ ] **Step 2: Run the tests.** `uv run pytest tests/substrate/test_store.py -v`. Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `_ulid.py`, `types.py` and `_store.py`.**
  - ULID: 48-bit millisecond timestamp plus 80 bits from `secrets`, Crockford base32.
  - `append`:
    - build the envelope
    - run `validate_payload` and `validate_envelope`, plus the subject regex
    - do one `INSERT` inside `BEGIN IMMEDIATE`
  - JSON is stored with `json.dumps(sort_keys=True, separators=(",", ":"))`.
  - `_store` calls `_ulid.utc_now_iso()` and `_ulid.new_ulid()` through the module (`from substrate import _ulid`), not via from-imports. Task 7's tests patch them.
  - The triggers are `CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append-only'); END;` and the same for `DELETE`.

- [ ] **Step 4: Run the tests.** Same command. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add substrate/_ulid.py substrate/types.py substrate/_store.py tests/substrate/test_store.py
git commit -m "substrate: append-only event store with history and since"
```

---

### Task 3: Blob store

**Files:**
- Create: `substrate/_blobs.py`
- Test: `tests/substrate/test_blobs.py`

**Interfaces:**
- Produces:
  - `class BlobNotFound(LookupError)`
  - `class Blobs(root: Path)` with `put(data: bytes) -> str` and `get(digest: str) -> bytes`. `get` raises `BlobNotFound` for an absent digest and `ValueError` for a malformed digest.

- [ ] **Step 1: Write the failing tests**

```python
import hashlib, zstandard, pytest
from substrate._blobs import Blobs, BlobNotFound

def test_put_returns_digest_of_raw_bytes_and_roundtrips(tmp_path):
    b = Blobs(tmp_path); data = b"circuit" * 1000
    d = b.put(data)
    h = hashlib.sha256(data).hexdigest()
    assert d == f"sha256:{h}" and b.get(d) == data
    stored = (tmp_path / "blobs/sha256" / h[:2] / h[2:4] / h).read_bytes()
    assert stored != data and zstandard.decompress(stored) == data

def test_put_is_idempotent(tmp_path):
    b = Blobs(tmp_path)
    assert b.put(b"x") == b.put(b"x")

def test_missing_blob_raises(tmp_path):                       # Review Focus 2
    with pytest.raises(BlobNotFound):
        Blobs(tmp_path).get("sha256:" + "0" * 64)

def test_no_temp_files_left_and_partial_never_at_final_path(tmp_path):
    b = Blobs(tmp_path); b.put(b"y" * 10_000)
    files = [p for p in (tmp_path / "blobs").rglob("*") if p.is_file()]
    assert len(files) == 1 and not files[0].name.startswith(".")
```

- [ ] **Step 2: Run the tests.** `uv run pytest tests/substrate/test_blobs.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `_blobs.py`.** Write to a temp file in the same directory (prefix `.tmp-`), `fsync`, then `os.replace`. Skip the write if the final path exists. Use zstd level 3.

- [ ] **Step 4: Run the tests.** Same command. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add substrate/_blobs.py tests/substrate/test_blobs.py
git commit -m "substrate: content-addressed zstd blob store"
```

---

### Task 4: Upcasters and version-safe reads

**Files:**
- Create: `substrate/upcasters.py`
- Modify: `substrate/_store.py` (read path)
- Test: `tests/substrate/test_upcasters.py`

**Interfaces:**
- Consumes: `latest_version`, `UnknownVersion` (Task 1); `Store` (Task 2).
- Produces:
  - `UPCASTERS: dict[tuple[str, int], Callable[[dict], dict]]`: key `(type, from_v)`, maps a payload to version `from_v + 1`. Empty in v1.
  - `upcast(type: str, type_v: int, payload: dict, registry=UPCASTERS) -> tuple[int, dict]`: applies the steps up to `latest_version(type)`. Raises `UnknownVersion` if `type_v > latest`, or if a needed step is missing.
  - `Store.history` and `Store.since` return upcast payloads, with `type_v` set to the latest. `raw_rows` does not upcast.
  - Reading a row with `v != 1` raises `UnknownVersion`.

- [ ] **Step 1: Write the failing tests**

```python
import json, sqlite3, pytest
from substrate import upcasters as u
from substrate._schemas import UnknownVersion
from substrate._store import Store

def test_upcast_is_identity_at_latest():
    assert u.upcast("question.asked", 1, {"question": "q"}) == (1, {"question": "q"})

def test_upcast_chains_steps(monkeypatch):
    monkeypatch.setattr(u, "latest_version", lambda t: 3)
    reg = {("t", 1): lambda p: p | {"b": 1}, ("t", 2): lambda p: p | {"c": 1}}
    assert u.upcast("t", 1, {"a": 1}, reg) == (3, {"a": 1, "b": 1, "c": 1})

def test_future_type_version_refused():                       # Review Focus 3
    with pytest.raises(UnknownVersion):
        u.upcast("question.asked", 2, {})

def test_future_envelope_version_refused_on_read(tmp_path):
    st = Store(tmp_path / "e.sqlite")
    st.append("question.asked", {"question": "q"}, subject="bench:x", actor={})
    db = sqlite3.connect(tmp_path / "e.sqlite")
    db.execute("INSERT INTO events (v,id,ts,type,type_v,subject,actor,cause,payload) "
               "VALUES (2,'01JC7KQF8R2M4VX1A0BZPQ3XYZ','2026-11-11T00:00:00.000Z',"
               "'question.asked',1,'bench:x','{}',NULL,'{}')"); db.commit()
    with pytest.raises(UnknownVersion):
        st.history("bench:x")

@pytest.mark.parametrize("t", __import__("substrate._schemas", fromlist=["x"]).event_types())
def test_every_fixture_upcasts_to_a_valid_latest_payload(t):
    from substrate._schemas import validate_payload, latest_version
    p = json.loads(open(f"substrate/fixtures/{t}/v1.json").read())
    v, out = u.upcast(t, 1, p)
    validate_payload(t, v, out); assert v == latest_version(t)
```

- [ ] **Step 2: Run the tests.** `uv run pytest tests/substrate/test_upcasters.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `upcasters.py`, and route the `Store` read paths through `upcast`.** `upcasters.py` imports `latest_version` into its own namespace, which is what the monkeypatch targets.

- [ ] **Step 4: Run the tests.** Run `uv run pytest tests/substrate -v`. Expected: all PASS, including Tasks 1–3.

- [ ] **Step 5: Commit**

```bash
git add substrate/upcasters.py substrate/_store.py tests/substrate/test_upcasters.py
git commit -m "substrate: upcasters on read; refuse unknown versions"
```

---

### Task 5: Candidate projection and `frontier()`

**Files:**
- Create: `substrate/_projections.py`
- Modify: `substrate/types.py` (add `Candidate`)
- Create: `tests/substrate/conftest.py`
  - fixture `st` returning `Store(tmp_path / "e.sqlite")`
  - constants `P, A, B, C, D, C1, C2, C3, C4`, each a distinct `"sha256:" + 64 hex` string
  - helper `measure(st, circuit, cost, passes=True, verifier="kickmix@1", basis="invsqrt/QxC/v1", program=P)`
  - Tasks 6 and 7 add their helpers here.
- Test: `tests/substrate/test_frontier.py`

**Interfaces:**
- Consumes: `Store.since` / `Store.history` (upcast reads).
- Produces:
  - `@dataclass(frozen=True) class Candidate` with fields `circuit, program, benchmark: str; cost: float; passes: bool; metrics: dict; verifier: str | None; cost_basis: str | None; measurements: int; first_seq, last_seq: int; parent: str | None; cause: str | None; note: str | None`.
  - `candidates(store, benchmark: str) -> list[Candidate]`
  - `frontier(store, benchmark: str, *, k: int = 20, verifier: str | None = None) -> list[Candidate]`
- **Rules (SPEC §Read types):**
  - Group `candidate.measured` events by `circuit`.
  - Keep only measurements whose `verifier` equals the verifier of that circuit's highest-`seq` measurement.
  - `passes` is the logical AND across the kept measurements.
  - `cost`, `metrics` and `cost_basis` come from the kept measurement with the highest cost.
  - `measurements` is the number of kept measurements; `first_seq` and `last_seq` are taken over the kept ones.
  - `parent`, `note` and `cause` come from the earliest `program.committed` whose `payload.blob == program`. `cause` is that event's envelope `cause`. All three are `None` if there is none.
  - `frontier`:
    - The default `verifier` is the verifier of the benchmark's highest-`seq` measurement.
    - The basis is the `cost_basis` of the highest-`seq` measurement under that verifier.
    - Keep only candidates matching both that have `passes`.
    - Sort by `(cost, first_seq)` and take the first `k`.

- [ ] **Step 1: Write the failing tests.** Use a helper `measure(st, circuit, cost, passes=True, verifier="kickmix@1", basis="invsqrt/QxC/v1", program=P)` that appends `candidate.measured` with subject `prog:` + program.

```python
def test_worst_measurement_wins_and_counts(st):
    measure(st, C1, 100); measure(st, C1, 120)
    [c] = candidates(st, "invsqrt")
    assert c.cost == 120 and c.measurements == 2 and c.first_seq == 1 and c.last_seq == 2

def test_one_failing_measurement_fails_candidate(st):
    measure(st, C1, 100); measure(st, C1, 100, passes=False)
    assert candidates(st, "invsqrt")[0].passes is False and frontier(st, "invsqrt") == []

def test_only_latest_verifier_counts(st):
    measure(st, C1, 50, verifier="kickmix@1"); measure(st, C1, 90, verifier="kickmix@2")
    [c] = candidates(st, "invsqrt"); assert c.cost == 90 and c.measurements == 1

def test_frontier_never_mixes_verifiers_or_bases(st):        # Review Focus 4
    measure(st, C1, 10, verifier="kickmix@1")
    measure(st, C2, 20, verifier="kickmix@2")
    measure(st, C3, 5, verifier="kickmix@2", basis="invsqrt/QxC/v2")
    measure(st, C4, 30, verifier="kickmix@2")
    assert [c.circuit for c in frontier(st, "invsqrt")] == [C2, C4]
    assert [c.circuit for c in frontier(st, "invsqrt", verifier="kickmix@1")] == [C1]

def test_ties_go_to_earliest_and_k_limits(st):
    measure(st, C1, 10); measure(st, C2, 10); measure(st, C3, 5)
    assert [c.circuit for c in frontier(st, "invsqrt", k=2)] == [C3, C1]

def test_candidate_joins_program_commit(st):
    pid = st.append("program.committed", {"blob": P, "parent": None, "benchmark": "invsqrt", "note": "n"},
                    subject="prog:" + P, actor={}, cause=None)
    measure(st, C1, 10)
    c = candidates(st, "invsqrt")[0]; assert c.note == "n" and c.parent is None
```

- [ ] **Step 2: Run the tests.** `uv run pytest tests/substrate/test_frontier.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `candidates` and `frontier` in `_projections.py`.** Read `candidate.measured` and `program.committed` events through `Store`, and group in Python.

- [ ] **Step 4: Run the tests.** Same command. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add substrate/_projections.py substrate/types.py tests/substrate/conftest.py tests/substrate/test_frontier.py
git commit -m "substrate: Candidate projection and frontier"
```

---

### Task 6: Node projection and `lineage()`

**Files:**
- Modify: `substrate/_projections.py`, `substrate/types.py` (add `Node`)
- Modify: `tests/substrate/conftest.py`. Add `commit(st, blob, parent)`, which appends `program.committed` with `benchmark="invsqrt"` and subject `"prog:" + blob`.
- Test: `tests/substrate/test_lineage.py`

**Interfaces:**
- Consumes: `candidates()` (Task 5).
- Produces:
  - `@dataclass(frozen=True) class Node` with fields `program: str; parent: str | None; cause: str | None; benchmark: str; note: str | None; seq: int; best_cost: float | None`.
  - `node(store, program: str) -> Node | None`: built from the earliest `program.committed` for that blob. `best_cost` is the lowest `cost` among passing `Candidate`s with that `program`, or `None`.
  - `lineage(store, ref: str, *, direction: str = "up") -> list[Node]`:
    - `ref` is a digest, with or without a `prog:` prefix.
    - `"up"`: ancestors, nearest first, excluding `ref` itself. It stops at a `null` parent or an unknown digest.
    - `"down"`: all descendants, excluding `ref`, ordered by `seq`, each once.
    - Any other direction raises `ValueError`.

- [ ] **Step 1: Write the failing tests.** Use a helper `commit(st, blob, parent)`.

```python
def test_up_returns_ancestors_nearest_first(st):
    commit(st, A, None); commit(st, B, A); commit(st, C, B)
    assert [n.program for n in lineage(st, C)] == [B, A]
    assert [n.program for n in lineage(st, "prog:" + C)] == [B, A]

def test_down_returns_all_descendants_once(st):
    commit(st, A, None); commit(st, B, A); commit(st, C, A); commit(st, D, B)
    assert [n.program for n in lineage(st, A, direction="down")] == [B, C, D]

def test_duplicate_commit_is_one_node(st):                    # Review Focus 5
    commit(st, A, None); commit(st, B, A); commit(st, B, A)
    assert [n.program for n in lineage(st, A, direction="down")] == [B]
    assert node(st, B).seq == 2

def test_best_cost_from_passing_candidates(st):
    commit(st, A, None); measure(st, C1, 50, program=A); measure(st, C2, 40, program=A, passes=False)
    assert node(st, A).best_cost == 50 and node(st, "sha256:" + "f" * 64) is None
```

- [ ] **Step 2: Run the tests.** `uv run pytest tests/substrate/test_lineage.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `node` and `lineage`.**

- [ ] **Step 4: Run the tests.** Same command. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add substrate/_projections.py substrate/types.py tests/substrate/conftest.py tests/substrate/test_lineage.py
git commit -m "substrate: Node projection and lineage"
```

---

### Task 7: `BudgetState` and `budget()`

**Files:**
- Modify: `substrate/_projections.py`, `substrate/types.py` (add `BudgetState`)
- Modify: `tests/substrate/conftest.py`. Add `spend(st, pool, ts, monkeypatch, **fields)`, which patches `substrate._ulid.utc_now_iso` to return `ts` for one append.
- Test: `tests/substrate/test_budget.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class BudgetState` with fields `pool, window: str; tokens_in, tokens_out, tokens_cached: int; cpu_seconds, usd: float; events: int`.
  - `budget(store, pool: str, *, window: str = "7d", now: datetime | None = None) -> BudgetState`:
    - Sums `budget.spent` events with `payload.pool == pool` and `ts >= now - window`.
    - Missing or null numeric fields count as 0.
    - `window` matches `^\d+[mhd]$`; anything else raises `ValueError`.

- [ ] **Step 1: Write the failing tests.** Use a helper `spend(st, pool, ts, **fields)` that appends `budget.spent`. To control `ts`, the helper monkeypatches `substrate._ulid.utc_now_iso`.

```python
def test_sums_within_window_only(st, monkeypatch):
    spend(st, "claude", "2026-11-11T10:00:00.000Z", monkeypatch, tokens_in=100, tokens_out=10, tokens_cached=50)
    spend(st, "claude", "2026-11-11T14:00:00.000Z", monkeypatch, tokens_in=1, tokens_out=1, usd=0.5)
    spend(st, "qwen",   "2026-11-11T14:00:00.000Z", monkeypatch, tokens_in=999, tokens_out=999)
    b = budget(st, "claude", window="5h", now=datetime(2026, 11, 11, 15, tzinfo=UTC))
    assert (b.tokens_in, b.tokens_out, b.tokens_cached, b.usd, b.events) == (1, 1, 0, 0.5, 1)

def test_bad_window_rejected(st):
    with pytest.raises(ValueError): budget(st, "claude", window="week")
```

- [ ] **Step 2: Run the tests.** `uv run pytest tests/substrate/test_budget.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `budget`.**

- [ ] **Step 4: Run the tests.** Same command. Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add substrate/_projections.py substrate/types.py tests/substrate/conftest.py tests/substrate/test_budget.py
git commit -m "substrate: budget projection"
```

---

### Task 8: The public API: `cli.py`, init, export

**Files:**
- Create: `substrate/cli.py`
- Test: `tests/substrate/test_cli.py`
- Modify: `substrate/SPEC.md`. In §Layout, note that data lives under the data root (`SUBSTRATE_ROOT`), not inside `substrate/`.

**Interfaces:**
- Consumes: everything above.
- Produces:
  - `class Substrate(root: Path | None = None)`. Root resolution follows Global Constraints. The class creates `root/events.sqlite` and `root/blobs/`. Methods, with signatures exactly as in SPEC §API:
    - `put_blob`, `get_blob`, `append`, `history`, `since`, `frontier`, `lineage`, `budget`
    - `export(path) -> Stats`
    - `init() -> Stats`
  - `@dataclass(frozen=True) class Stats` with fields `events: int = 0, registered: int = 0`.
  - `Substrate.init()` appends `schema.registered` (subject `schema:<type>`, payload `{type, type_v, status, digest}`) for each schema file whose digest has no matching `schema.registered` event yet. It is idempotent.
  - `export(path)` writes one JSON object per line: each envelope as stored, with no upcasting, no `seq`, and keys sorted.
  - **Command line:** `uv run python -m substrate.cli [--root DIR] <command>`. Every command prints JSON on stdout and exits 0. On `SchemaViolation`, `UnknownVersion`, `BlobNotFound` or `ValueError` it prints `{"error": "<Class>", "message": "..."}` on stderr and exits 2.
  - **Commands:**
    - `init`
    - `append --type T --subject S --actor JSON --payload JSON [--cause ID] [--type-v N]`: prints `{"id": ...}`.
    - `put-blob FILE`: prints `{"digest": ...}`.
    - `get-blob DIGEST --out FILE`
    - `history SUBJECT [--types T ...]`
    - `since [--cursor N] [--types T ...] [--limit N]`: prints `{"events": [...], "cursor": N}`.
    - `frontier BENCH [--k N] [--verifier V]`
    - `lineage REF [--direction up|down]`
    - `budget POOL [--window W]`
    - `export PATH`

- [ ] **Step 1: Write the failing tests**

```python
import json, subprocess, sys
from substrate.cli import Substrate

def run(root, *args):
    return subprocess.run([sys.executable, "-m", "substrate.cli", "--root", str(root), *args],
                          capture_output=True, text=True)

def test_init_registers_every_schema_once(tmp_path):
    s = Substrate(tmp_path)
    assert s.init().registered == 15 and s.init().registered == 0
    assert len(s.history("schema:direction")) == 1

def test_cli_append_and_history_roundtrip(tmp_path):
    r = run(tmp_path, "append", "--type", "question.asked", "--subject", "bench:invsqrt",
            "--actor", '{"role":"t"}', "--payload", '{"question":"q"}')
    assert r.returncode == 0 and len(json.loads(r.stdout)["id"]) == 26
    h = json.loads(run(tmp_path, "history", "bench:invsqrt").stdout)
    assert h[0]["payload"] == {"question": "q"} and h[0]["seq"] == 1

def test_cli_rejects_bad_payload_and_bad_json(tmp_path):
    r = run(tmp_path, "append", "--type", "question.asked", "--subject", "bench:x",
            "--actor", "{}", "--payload", "{}")
    assert r.returncode == 2 and json.loads(r.stderr)["error"] == "SchemaViolation"
    r = run(tmp_path, "append", "--type", "question.asked", "--subject", "bench:x",
            "--actor", "{}", "--payload", "{not json")
    assert r.returncode == 2 and json.loads(r.stderr)["error"] == "ValueError"
    assert json.loads(run(tmp_path, "since").stdout) == {"events": [], "cursor": 0}

def test_blob_roundtrip_via_cli(tmp_path):
    f = tmp_path / "in.bin"; f.write_bytes(b"abc")
    d = json.loads(run(tmp_path, "put-blob", str(f)).stdout)["digest"]
    out = tmp_path / "out.bin"
    assert run(tmp_path, "get-blob", d, "--out", str(out)).returncode == 0 and out.read_bytes() == b"abc"

def test_export_writes_events_as_stored(tmp_path):
    s = Substrate(tmp_path); s.append("question.asked", {"question": "q"}, subject="bench:x", actor={})
    assert s.export(tmp_path / "e.jsonl").events == 1
    [line] = (tmp_path / "e.jsonl").read_text().splitlines()
    ev = json.loads(line); assert "seq" not in ev and ev["payload"] == {"question": "q"} and ev["v"] == 1
```

- [ ] **Step 2: Run the tests.** `uv run pytest tests/substrate/test_cli.py -v`. Expected: FAIL.

- [ ] **Step 3: Implement `substrate/cli.py`** as a thin layer over `Store`, `Blobs` and `_projections`. Read types are emitted with `dataclasses.asdict`.

- [ ] **Step 4: Run the full suite.** `uv run pytest -v`. Expected: all PASS.

- [ ] **Step 5: Update SPEC §Layout and commit**

```bash
git add substrate/cli.py substrate/SPEC.md tests/substrate/test_cli.py
git commit -m "substrate: public API and JSON command line"
```
