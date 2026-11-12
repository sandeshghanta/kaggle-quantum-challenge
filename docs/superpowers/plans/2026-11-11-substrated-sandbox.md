# Substrate Service, Sandboxing and Sweep Tier — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the loop safe to leave unattended:
- Only the substrate service can touch the event log and blobs.
- Every write is attributed to a role by the OS, not by the caller.
- Model-written code (pi edits, `gen.py` builds) runs in a sandbox.

Also replace the quick check, which covers about 2% of the domain, with a full-domain one-shot sweep and a margin gate.

**Architecture:**
- **`substrated`** runs as its own Unix user. It owns the data dir (mode 0700) and serves the existing `Substrate` API as JSON over HTTP on a Unix socket.
- **Caller identity:** it reads each caller's uid with `SO_PEERCRED`, maps it to a role, and stamps `actor.role` itself.
- **Policy:** a generic `authorize(principal, type, subject, payload)` hook in the substrate core enforces a role table that lives in loop config. This keeps the SPEC's "no knowledge of loop roles".
- **Client:** loop code talks to it through `RemoteSubstrate`, which has the same methods as `Substrate`.
- **Sandbox:** pi and builds run under bubblewrap, with only their job directory writable and the socket not mounted.

**Tech Stack:** Python 3.12 stdlib (`socketserver.ThreadingUnixStreamServer`, `http.server`, `http.client`), bubblewrap (`pkgs.bubblewrap`), NixOS 26.05, Lima VM `devbox-sim` for the Linux-only tests.

**Spec:**
- `docs/superpowers/specs/2026-11-11-research-loop-design.md`, plus the addendum at the end of this plan
- `substrate/SPEC.md`
- the schema stress-test findings, on branch `sandesh/_claude/schema-stress`: `docs/superpowers/specs/2026-11-11-schema-stress-findings.md`, items #1, #5 and #6 apply here

## Design addendum (approved by the human, 2026-11-11)

1. **Substrate as a service.**
   - The `substrate` user owns `${dataDir}/substrate` (mode 0700). Nothing else can open the SQLite file or blobs.
   - The socket is `/run/substrate/api.sock`, mode 0660, group `substrate-clients`. Every loop role user is in that group.
   - One writer process, which also removes SQLite `Busy` contention between 9+ processes.
2. **Attestation.**
   - Each role runs as its own user: `rl-worker`, `rl-verifier`, `rl-seed`, and later `rl-researcher` (the researcher service, which also writes `quota.observed`, Claude `budget.spent` and `loop.config_changed`) and `rl-admin`.
   - The server maps peer uid → user name → role via the policy file, and overwrites `actor.role`. The client still supplies `model` and `harness`.
3. **Policy** (`loop/policy.json`). Everyone may read. Each role may append only:

   | Role | May append |
   |---|---|
   | `worker` | `attempt.announced`; `program.committed`; `island.joined` with `via ∈ {submit}`; `budget.spent` with `pool ∈ {qwen}`; `put_blob` |
   | `verifier` | `candidate.measured`; `candidate.rejected`; `budget.spent` with `pool ∈ {cpu}`; `put_blob` |
   | `seed` | `island.created`; `program.committed`; `island.joined` with `via ∈ {seed}`; `put_blob` |
   | `substrate` | `schema.registered` (only the server itself, via `init`) |

   The researcher and admin rows are added with their plans.
4. **Sandboxing.**
   - **pi** runs under `bwrap`:
     - read-only `/nix/store` and the repo
     - read-write `workroot/<job>` only
     - a private `/tmp` and `HOME`
     - network kept (Qwen)
     - **no** `/run/substrate`
   - **`gen.py` builds** run under `bwrap` the same way, but read-write is the build dir only, plus `--unshare-net`.
   - **On macOS**, where bwrap doesn't exist, both run unsandboxed and log a warning, so development and tests still work there.
5. **Verification tiers.**
   - **T0:** load and register check, folded into the sweep's own validation.
   - **T2 sweep:** every x, 1 shot, `--shots-per-x 1 --batch-size 292` (~28 CPU-s). It replaces `--max-x 65536`.
   - **T3 gate:** `not_better` only if `sweep_cost > island_best_sweep × (1 + δ)`, with δ = 0.05 from config. A seeded 5% audit of would-be rejections proceeds to the full check anyway.
   - **Full check:** salt 1 first, then the remaining salts, **stopping at the first failing salt**.
   - **`not_better` means "not fully verified", not "dropped":** the program remains a possible parent at reduced weight. That's already true in `pick_parent`.
   - **Additive optional fields on `candidate.rejected`:** `estimate: {cost, Q, T}` and `tier: "build"|"sweep"|"gate"|"duplicate"`. No version bump.
   - `measured.metrics.quick_cost` is renamed `sweep_cost`. No data written before this needs preserving.
6. **Exactly one verifier.** It is documented in the module, and the verifier logs its backlog (pending count and the oldest pending age) on every loop.
7. **Explicitly deferred to the researcher plan:**
   - the cause chain to the direction
   - a `researcher.visited` event
   - `append_many`
   - a fixed vocabulary for the prediction fields
   - required `verifier` / `cost_basis`
   - stress-test #7 (an unknown type breaks unfiltered reads)

## Global Constraints

- **Unchanged:** substrate core semantics, the read types and every existing test. `Substrate` (in-process) stays the API for tests and single-user tools.
- **The socket protocol is JSON over HTTP/1.1.**
  - Requests: `POST /v1/<method>` with a JSON object of the method's keyword arguments.
  - Blobs: `POST /v1/put_blob` with a raw body, returning `{"digest": ...}`, and `GET /v1/blob/<digest>`, returning raw bytes.
  - Errors: HTTP 400/403/404/409/503 with `{"error": <class>, "message": ...}`. The client re-raises them as `SchemaViolation` (400), `Forbidden` (403), `BlobNotFound` (404), `UnknownVersion` (409) and `Busy` (503).
- **Read results** are the read types serialized with `dataclasses.asdict`. The client rebuilds them as the same dataclasses.
- **Running modes:** loop entry points take `--substrate <target>`, where the target is a directory path (in-process) or `unix:/run/substrate/api.sock`. The old `--root` stays as an alias for a directory.
- **bwrap is required on Linux.** If it's missing there, raise an error at startup rather than silently running unsandboxed.

## Review Focus

1. **A worker forging `actor.role="verifier"`, or writing `candidate.measured`.** The server stamps role `worker` and returns 403. Test in Task 2.
2. **A pi job trying to open `/run/substrate/api.sock` or write outside its job dir.** Both fail inside the sandbox. Test in Task 5 (VM).
3. **substrated restarts while workers are mid-job.** Clients retry with backoff (connection refused / 503 for up to 60 s), and no event is lost or duplicated by the retry. Test in Task 3.
4. **A huge blob or payload.** Bodies over 64 MB get 413, and the server stays up. Test in Task 2.
5. **A circuit wrong only at large x.** The sweep tier rejects it without a full check. Test in Task 4.

---

### Task 1: Authorization hook in the substrate core

**Files:**
- Modify: `substrate/cli.py`. `Substrate.__init__(root=None, *, authorize=None)`; `append(..., principal: str | None = None)` and `put_blob(data, principal=None)` call `authorize(principal, type, subject, payload)` (put_blob uses `type="blob"`, `subject=""`, `payload={}`) before writing. It raises `Forbidden(PermissionError)`.
- Modify: `substrate/SPEC.md` §API. Document the hook and state that the substrate stays role-agnostic.
- Create: `loop/policy.py`, `loop/policy.json`
- Test: `tests/substrate/test_authorize.py`, `tests/loop/test_policy.py`

**Interfaces:**
- Produces:
  - `substrate.cli.Forbidden`
  - `loop.policy.load(path: Path) -> Policy`
  - `Policy.role_for_user(user: str) -> str | None`
  - `Policy.authorize(principal: str | None, type: str, subject: str, payload: dict) -> None`. It raises `Forbidden` and accepts any write when `principal is None`, which keeps in-process use unrestricted.
- `policy.json` shape: `{"users": {"rl-worker": "worker", ...}, "roles": {"worker": {"append": {"attempt.announced": {}, "island.joined": {"via": ["submit"]}, "budget.spent": {"pool": ["qwen"]}, ...}, "blob": true}, ...}}`. A constraint `{"field": [allowed values]}` checks `payload[field] in values`.

- [ ] **Step 1: Failing tests.**
  - A worker appending `candidate.measured` raises `Forbidden`.
  - A worker appending `island.joined` with `via="migration"` raises `Forbidden`.
  - `via="submit"` is allowed.
  - A verifier `put_blob` is allowed.
  - Without a principal everything is allowed.
  - An unknown user maps to `None` and is refused by the server (Task 2).
- [ ] **Step 2:** `uv run pytest tests/substrate/test_authorize.py tests/loop/test_policy.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement. Step 4: PASS, and the full suite stays green. Step 5: Commit `substrate: authorize hook; loop: role policy`.

---

### Task 2: `substrated` — the server

**Files:**
- Create: `substrate/server.py`
- Test: `tests/substrate/test_server.py`

**Interfaces:**
- Produces: `serve(socket_path: Path, root: Path, *, policy: Policy | None, uid_to_user=pwd_lookup, max_body: int = 64 << 20)`. A `ThreadingUnixStreamServer` with an HTTP handler.
  - **Per request:**
    - `SO_PEERCRED` (`struct.unpack("3i", sock.getsockopt(SOL_SOCKET, SO_PEERCRED, 12))`) gives the uid, then the user, then the role. On macOS, where `SO_PEERCRED` is absent, the principal is `None` (dev only).
    - An unknown user with a policy loaded gets 403.
    - It sets `actor["role"] = role` before `append`.
  - **Lifecycle:** it removes a stale socket on start, then `chmod 0660`.
  - **CLI:** `python -m substrate.server --socket PATH --root DIR [--policy FILE]`. `init()` runs at startup.
- Test approach: run the server in a thread on a tmp socket. Inject `uid_to_user` to simulate roles, which works on macOS too.

- [ ] **Step 1: Failing tests.**
  - An append round-trips through `http.client` over the Unix socket.
  - A forged `actor.role` is overwritten. *(Review Focus 1)*
  - A worker posting `candidate.measured` gets 403 `Forbidden`.
  - An unknown user gets 403.
  - A body over `max_body` gets 413, and the next request still works. *(Review Focus 4)*
  - `GET /v1/blob/<missing>` gets 404.
- [ ] **Step 2:** `uv run pytest tests/substrate/test_server.py -q`. Expected: FAIL. Step 3: implement. Step 4: PASS. Step 5: commit `substrate: substrated unix-socket service with peer attestation`.

---

### Task 3: `RemoteSubstrate` client and `--substrate` targets

**Files:**
- Create: `substrate/client.py`
- Modify: `loop/worker.py`, `loop/verifier.py`, `loop/seed.py`. Use `--substrate`, with `--root` as an alias.
- Test: `tests/substrate/test_client.py`

**Interfaces:**
- Produces:
  - `RemoteSubstrate(socket_path)` with the same public methods and signatures as `Substrate`, minus `export` and `init`.
  - `substrate.connect(target: str) -> Substrate | RemoteSubstrate`
  - **Retries:** connection errors and 503 are retried with exponential backoff up to `retry_s=60`. Only idempotent reads and `put_blob` retry blindly.
  - **Retry-safe append:** the client generates the event id (ULID) itself and sends it. The server treats a duplicate id as success and returns the existing id. This requires `Store.append(..., id=None)` to accept an id and to return the existing one on an `id` UNIQUE conflict. *(Review Focus 3)*

- [ ] **Step 1: Failing tests.**
  - The client reproduces `test_frontier`'s key assertions against a live server.
  - An append retried after the server restarts mid-request appears exactly once.
  - `connect("dir")` gives a `Substrate`, and `connect("unix:/x")` gives a `RemoteSubstrate`.
- [ ] **Step 2–4:** red, implement, green, plus the full suite. Step 5: commit `substrate: remote client with retry-safe appends; loop uses --substrate`.

---

### Task 4: Sweep tier and margin gate

**Files:**
- Modify: `loop/verify.py`
  - `quick_check` → `sweep_check`, with args `--shots-per-x 1 --batch-size 292`.
  - `evaluate(..., island_best_sweep, delta=0.05, audit_rate=0.05, rng)`.
  - Full check stops at the first failing salt.
  - Rejections record the optional `estimate` and `tier`.
- Modify: `loop/pool.py` (`island_best_quick` → `island_best_sweep`) and `loop/verifier.py` (backlog log line, `--delta`, `--audit-rate`).
- Modify: `substrate/schema/candidate.rejected/v1.json`. Add optional `estimate` (object `{cost, Q, T}`) and `tier` (enum). No version bump.
- Test: update `tests/loop/test_verify.py` and `tests/loop/test_pool.py`.

- [ ] **Step 1: Failing tests.**
  - A bundle wrapping the reference, but whose circuit writes a wrong phase only for x > 3,000,000 (wrap `nr_reference_circuit` and flip a phase gate controlled on the top bit), is rejected at `tier="sweep"` with no `candidate.measured`. *(Review Focus 5)*
  - A sweep cost 3% worse than the island best passes the gate (δ = 5%). 10% worse gives `not_better` with an `estimate`.
  - With `audit_rate=1.0`, a would-be rejection is fully checked.
  - A failing salt 1 means no salt 2 run: `seeds == [1]`.
- [ ] **Step 2–4:** red, green, and the full suite. Step 5: commit `loop: full-domain sweep tier, margin gate with audit, early-stop salts`.

---

### Task 5: Sandboxing pi and builds

**Files:**
- Create: `loop/sandbox.py`
- Modify: `loop/programs.py` (`build` wraps with the sandbox) and `loop/pi_runner.py` (`run_pi` wraps with the sandbox).
- Test: `tests/loop/test_sandbox.py`. The Linux-only tests use `skipif(sys.platform != "linux" or not shutil.which("bwrap"))` and run in the VM.

**Interfaces:**
- Produces: `wrap(cmd: list[str], *, rw: list[Path], ro: list[Path], network: bool) -> list[str]`.
  - **On Linux:**
    - `bwrap --die-with-parent --unshare-all [--share-net] --ro-bind /nix/store /nix/store`
    - `--ro-bind` for each `ro` path, `--bind` for each `rw` path
    - `--proc /proc --dev /dev --tmpfs /tmp`
    - `--setenv HOME /tmp`, `--chdir <rw[0]>`
    - then `--` and the command
  - **On non-Linux:** returns `cmd` unchanged and logs a warning once.
  - **Linux without bwrap** raises `RuntimeError`.
- **Build sandbox:**
  - `rw=[bundle_dir]`
  - `ro=[repo .venv, python interpreter's store path]`
  - `network=False`
- **pi sandbox:**
  - `rw=[job_dir]` (bundle and agent dir)
  - `ro=[pi package store path, repo roles dir]`
  - `network=True`

- [ ] **Step 1: Failing tests** (VM).
  - Inside a build sandbox, a `gen.py` writing to `/var/lib/...` or `../outside` fails, and the build is `ok=False` with `"Permission denied"` or `"Read-only"` in stderr.
  - A `gen.py` opening a TCP connection fails.
  - Inside the pi sandbox, `test -S /run/substrate/api.sock` is false. *(Review Focus 2)*
  - The existing pi tests pass when sandboxed.
- [ ] **Step 2–4:** red and green in the VM (`nix develop -c uv run pytest tests/loop/test_sandbox.py -q`). The Mac suite stays green with the sandbox skipped. Step 5: commit `loop: bubblewrap sandbox for pi and builds`.

---

### Task 6: NixOS module — per-role users, substrated unit, one verifier

**Files:**
- Modify: `nix/module.nix`, `nix/vm-test/configuration.nix`, `flake.nix` (add `pkgs.bubblewrap` to the toolchain), `docs/deploy.md`

**Interfaces:**
- **Users and group:**
  - users `substrate`, `rl-worker`, `rl-verifier`, `rl-seed`
  - group `substrate-clients` (all `rl-*`)
  - user `rl-deploy` owns `${dataDir}/repo`, which is world-readable and writable only by `rl-deploy`
- **Data dirs:**
  - `${dataDir}/substrate`: owner `substrate`, mode 0700
  - `${dataDir}/work/<role>`: owner the role user
- **`substrated.service`:**
  - `User=substrate`, `RuntimeDirectory=substrate`, `RuntimeDirectoryMode=0750`, `Group=substrate-clients`
  - runs `python -m substrate.server --socket /run/substrate/api.sock --root ${dataDir}/substrate --policy ${repo}/loop/policy.json`
  - `ReadWritePaths=${dataDir}/substrate`
- **Role units:**
  - Worker, verifier and seed units run as their role user with `--substrate unix:/run/substrate/api.sock`.
  - `ReadWritePaths` covers only their own work dir. No unit except substrated can reach `${dataDir}/substrate`.
- **Verifier:** the module comment and `docs/deploy.md` state that exactly one verifier instance runs.
- **`research-loop-update`:** runs as `rl-deploy`, then restarts substrated last, after the clients drain.

- [ ] **Step 1:** Rebuild the VM and wipe `/var/lib/research-loop` for a clean start. All units are active, and the smoke flow from the first-loop plan passes.
- [ ] **Step 2: Attestation in the VM.**
  - `sudo -u rl-worker` POSTing `candidate.measured` to the socket gets 403.
  - `sudo -u rl-worker cat ${dataDir}/substrate/events.sqlite` gives permission denied.
  - Events written by workers carry `actor.role == "worker"`.
- [ ] **Step 3: Restart test.** Restart `substrated` while workers run. No worker crashes, and no duplicate events appear (Review Focus 3). Check via `since` counts against announcements.
- [ ] **Step 4:** Update `docs/deploy.md` and remove the "known gaps" bullet about write access. Commit `nix: per-role users, substrated service, sandboxed workers`.
