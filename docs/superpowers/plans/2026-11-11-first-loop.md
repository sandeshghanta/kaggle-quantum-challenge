# First Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the smallest end-to-end research loop running. pi lab assistants on Qwen edit invsqrt generator programs, a verifier service scores them, and everything is recorded in the substrate. It is packaged as a NixOS flake and proven in a local NixOS VM.

**Architecture:** There is no central orchestrator process in this version. Two kinds of independent, drain-on-stop services share the substrate:
- **Workers** (`loop.worker`). Each picks an island and a parent, announces, runs one pi job, and commits the result.
- **One verifier** (`loop.verifier`). It tails the event log, builds each new program, and runs the quick check, then the full check for candidates that beat their island.

Parent selection uses `attempt.announced` events as the dispatch record, so concurrent workers spread out without coordination. Loop code runs from a git checkout. The Nix flake provides only the toolchain and the service definitions.

**Tech Stack:** Python 3.12 + uv, kickmix 1.0.0 (PyPI), the substrate (`substrate/cli.py`), pi `@earendil-works/pi-coding-agent` 1.5.x (Node ≥ 22.19), Nix flakes / NixOS 26.05, Lima VM `devbox-sim` for NixOS tests.

**Spec:** `docs/superpowers/specs/2026-11-11-research-loop-design.md`. Contract: `substrate/SPEC.md`.

**Scope cut, agreed with the human.** Not in this plan:
- the researcher
- the admin chat and the progress view
- multiple islands per run (the code supports them; we seed one)
- MAP-Elites buckets
- the near-duplicate gate
- level-2 summaries
- porting public notebooks (the seed is Google's reference generator, 1×)

## Global Constraints

- Every write goes through `substrate.cli.Substrate`. Loop code never touches `events.sqlite` or `blobs/` directly.
- Every event's `actor` is `{"role": <role>, "harness": "<role>/<version>@<git short sha>", "host": socket.gethostname()}`, plus `"model"` for pi runs.
- Benchmark `invsqrt`. Cost basis `"invsqrt/QxC/v1"`. Verifier tag `"kickmix@<kickmix.__version__>+invsqrt@<first 12 hex of sha256 of vendored verify_submission_invsqrt.py>"`.
- **Program bundle:** a directory with entry `gen.py`, run as `python -I gen.py <out.kmb>` with the bundle directory as cwd. It is stored as one blob: a deterministic tar with sorted members, mtime 0, uid/gid 0, mode 0644, no directories.
- **Quick check:** vendored verifier with `--max-x 65536` and otherwise default flags. It measured about 3 s and 27 CPU-s on the reference circuit.
- **Full check:** default flags (64 shots per x over the whole domain), salts `["s1", "s2", "s3", "s4"]`. That is about 1,300 CPU-s per salt. `--max-workers` is taken from config.
- **Qwen access:** an OpenAI-compatible endpoint. Production uses `http://spark-9a7e.stalk-rudd.ts.net:8000/v1` with model `nvidia/Qwen3.8-Flash-Next-FP4-Local`, at most 8 concurrent requests. Tests use a local fake server. A **LiteLLM** connectivity check (`https://litellm-proxy.ml.scale.com`, `fireworks_ai/qwen3p8-2p4t-a95b`, key `$LITELLM_API_KEY`) is limited to hello-world requests with `max_tokens ≤ 64`, because the budget is $5.
- Services stop gracefully. On SIGTERM a worker or the verifier finishes its current job and then exits. systemd `TimeoutStopSec=900`.
- **Tests:** `uv run pytest`. Tests needing `pi` are marked `@pytest.mark.pi` and skip when `pi` is not on PATH; they run in the VM via `nix develop`. Tests needing kickmix run on the Mac and in the VM.

## Review Focus

1. **A generator that hangs, crashes, or writes no `.kmb`** (assistants will produce many). The verifier records `candidate.rejected` (`build_failed`, with stderr) within the build timeout and moves on. Test in Task 3.
2. **A worker killed mid-job** (deploy, OOM). Nothing half-written. The orchestrator-free design tolerates an orphan `attempt.announced`, and the next pick still works. Test in Task 6.
3. **pi exits non-zero, times out, or changes nothing.** No `program.committed`; `budget.spent` is still recorded. Test in Task 6.
4. **The verifier restarts.** It resumes from its persisted cursor and never re-verifies an already-measured or already-rejected program. Test in Task 4.
5. **Two workers pick concurrently from a one-program island.** Both proceed (same parent is allowed); the children count increments for each. Test in Task 5.

---

### Task 1: Dependencies and vendored verifier

**Files:**
- Modify: `pyproject.toml`: add `kickmix==1.0.0` and `numpy` to dependencies; add a `markers = ["pi: needs the pi CLI"]` pytest entry.
- Create: `verifiers/invsqrt/verify_submission_invsqrt.py`, `verifiers/invsqrt/nr_reference_circuit.py`, `verifiers/invsqrt/__init__.py`, `verifiers/LICENSE.txt`. Copy these from `~/workplace/temp/quantum/data/comp/verifiers/` (Apache-2.0), unmodified.
- Create: `loop/__init__.py`, `loop/config.py`
- Test: `tests/loop/test_config.py`

**Interfaces:**
- Produces, in `loop/config.py`:
  - `REPO: Path`
  - `VERIFIER_DIR: Path` (= `REPO / "verifiers"`)
  - `BENCHMARK = "invsqrt"`
  - `COST_BASIS = "invsqrt/QxC/v1"`
  - `def verifier_tag() -> str`
  - `def actor(role: str, version: str, model: str | None = None) -> dict`
  - `def git_sha() -> str`, the short sha, or `"nogit"`

- [ ] **Step 1: Write the failing test.**
  - `verifier_tag()` matches `^kickmix@1\.0\.0\+invsqrt@[0-9a-f]{12}$`.
  - `actor("lab_assistant", "v1", "m")` has keys `role, harness, host, model`, and `harness` starts with `"lab_assistant/v1@"`.
- [ ] **Step 2: Run** `uv run pytest tests/loop/test_config.py -q`. Expected: FAIL (`ModuleNotFoundError: loop`).
- [ ] **Step 3: Implement.** Copy the verifier files, add the dependencies (`uv sync`), and write `loop/config.py`.
- [ ] **Step 4: Run.** Same command. Expected: PASS. Also run `uv run python -I verifiers/invsqrt/nr_reference_circuit.py /tmp/nr.kmb`; expected output contains `7,735 Toffolis, 260 qubits`.
- [ ] **Step 5: Commit** with message `loop: kickmix dependency, vendored invsqrt verifier, config`.

---

### Task 2: Program bundles and builds

**Files:**
- Create: `loop/programs.py`
- Test: `tests/loop/test_programs.py`

**Interfaces:**
- Produces:
  - `pack(bundle_dir: Path) -> bytes`: the deterministic tar from Global Constraints. Raises `ValueError` if `gen.py` is missing.
  - `unpack(data: bytes, dest: Path) -> None`: refuses absolute paths or `..` members (`ValueError`).
  - `@dataclass(frozen=True) class BuildResult` with fields `ok: bool, kmb: Path | None, stderr: str, seconds: float`.
  - `build(bundle_dir: Path, out_kmb: Path, *, timeout: float = 120) -> BuildResult`: runs `[sys.executable, "-I", "gen.py", str(out_kmb)]` in `bundle_dir`. It is `ok` only if the exit code is 0 and `out_kmb` exists. A timeout kills the process group and gives `ok=False` with `stderr` ending `"build timed out after <timeout>s"`. `stderr` is truncated to the last 4000 chars.

- [ ] **Step 1: Write the failing tests.**
  - `pack` is byte-identical across two calls and after `touch` changes mtimes; a different file content gives different bytes.
  - `unpack(pack(d))` round-trips the files.
  - `unpack` of a tar containing `../evil` raises `ValueError`.
  - `build` on a bundle whose `gen.py` copies a fixed byte string to `argv[1]` gives `ok=True`.
  - `gen.py` running `import time; time.sleep(10)` with `timeout=0.5` gives `ok=False` and `"timed out"` in stderr.
  - `gen.py` running `raise SystemExit(3)` gives `ok=False`.
  - A `gen.py` that exits 0 but writes nothing gives `ok=False`.
- [ ] **Step 2: Run** `uv run pytest tests/loop/test_programs.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** with `tarfile` (`format=PAX_FORMAT`), `subprocess.Popen(start_new_session=True)` and `os.killpg` on timeout.
- [ ] **Step 4: Run.** Same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `loop: program bundles and sandboxed builds`.

---

### Task 3: Verifier checks and recording

**Files:**
- Create: `loop/verify.py`
- Test: `tests/loop/test_verify.py`

**Interfaces:**
- Consumes:
  - `build`, `unpack`, `BuildResult` (Task 2)
  - `config.*` (Task 1)
  - `Substrate.append`, `put_blob`, `get_blob`
- Produces:
  - `@dataclass(frozen=True) class CheckResult` with fields `passes: bool, cost: float | None, metrics: dict, shots: int, seeds: list, error: str | None, raw: dict`.
  - `quick_check(kmb: Path, *, max_workers: int | None = None) -> CheckResult`
  - `full_check(kmb: Path, *, salts: list[str], max_workers: int | None = None, max_x: int | None = None) -> CheckResult`. It runs one verifier process per salt. `cost` and `metrics` come from the highest-cost salt, and `passes` only if every salt passes. `max_x` is for tests only.
  - Both run `[sys.executable, "-I", "invsqrt/verify_submission_invsqrt.py", kmb, ...]` with `cwd=VERIFIER_DIR` and parse the JSON on stdout (from the first `{`). The fields are `metrics = {"Q": num_qubits, "T": toffoli_count, "seconds": elapsed}` and `shots = total shots run`. `seeds` holds the salt strings.
  - `@dataclass(frozen=True) class Outcome` with fields `kind: str` (`"rejected"` or `"measured"`), `event_id: str`, `cost: float | None`.
  - `evaluate(sub: Substrate, program: str, *, island_best: float | None, salts: list[str], max_workers: int | None, workdir: Path, cause: str, full_max_x: int | None = None) -> Outcome`:
    1. Unpack the program blob into `workdir` and build it. On a failed build, append `candidate.rejected` with `reason="build_failed"` and `detail=stderr`.
    2. `put_blob` the `.kmb` to get the circuit digest, then run the quick check. On a fail, append `candidate.rejected` with `reason="quick_check_failed"` and `circuit` set.
    3. If `island_best` is not None and the quick cost is ≥ `island_best`, append `candidate.rejected` with `reason="not_better"`.
    4. Otherwise run the full check and append `candidate.measured` with `program`, `circuit`, `cost` (worst salt, or the quick cost when the full check failed), `passes`, `benchmark`, `metrics`, `shots`, `seeds`, `verifier=verifier_tag()`, `cost_basis=COST_BASIS`, and `failure={"detail": error}` when failing.
    - Every event has subject `"prog:" + program`, `cause=cause`, and actor role `"verifier"`.
    - Also append `budget.spent` (pool `"cpu"`, `cpu_seconds` and `wall_seconds` measured with `resource.getrusage(RUSAGE_CHILDREN)` deltas and `time.monotonic`), with `cause` = the measurement or rejection id.

- [ ] **Step 1: Write the failing tests.** Use a `sub` fixture with `Substrate(tmp_path / "s")`, and a helper `commit_bundle(sub, files: dict[str, str]) -> program digest`.
  - `quick_check` on the reference circuit (built by `nr_reference_circuit.py`) gives `passes=True`, `metrics["Q"] == 260`, and `metrics["T"] == 7735`.
  - `evaluate` on a bundle whose `gen.py` raises gives one `candidate.rejected` with `reason == "build_failed"` and `"Traceback"` in `detail`, plus one `budget.spent`. *(Review Focus 1)*
  - `evaluate` on a bundle whose `gen.py` sleeps past the build timeout (pass `build_timeout=1` through a module constant monkeypatch) gives `build_failed` with `"timed out"`.
  - `evaluate` on a bundle whose `gen.py` writes a valid but wrong circuit, e.g. an identity circuit on the 22-qubit register built with kickmix, gives `quick_check_failed`.
  - `evaluate` on the reference bundle with `island_best=1.0` gives `not_better`.
  - `evaluate` on the reference bundle with `island_best=None`, `salts=["a", "b"]` and `full_max_x=4096` gives `candidate.measured` with `passes=True`, `seeds == ["a", "b"]`, the `verifier_tag()` verifier, and `cost_basis == "invsqrt/QxC/v1"`.
- [ ] **Step 2: Run** `uv run pytest tests/loop/test_verify.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** `BUILD_TIMEOUT = 120` is a module constant.
- [ ] **Step 4: Run.** Same command. Expected: PASS (about 30 s, dominated by the quick checks).
- [ ] **Step 5: Commit** with message `loop: quick/full verification and event recording`.

---

### Task 4: Verifier service

**Files:**
- Create: `loop/verifier.py`, `loop/state.py`
- Test: `tests/loop/test_verifier_service.py`

**Interfaces:**
- Consumes: `evaluate` (Task 3); `Substrate.since`, `history`; `island_best` from Task 5's `pool.island_best` (Task 4 ships a local stub returning `None`, which Task 5 replaces; record this as a ledger ruling).
- Produces:
  - `loop/state.py`:
    - `load_cursor(path: Path) -> int`: 0 if the file is missing.
    - `save_cursor(path: Path, cursor: int) -> None`: atomic write (tmp + `os.replace`).
  - `loop/verifier.py`:
    - `pending(sub, cursor: int) -> tuple[list[Event], int]`: `program.committed` events after `cursor` whose program has no `candidate.measured` or `candidate.rejected` yet.
    - `run_once(sub, *, state_path: Path, workdir: Path, salts, max_workers, full_max_x=None) -> int`: evaluates every pending program in commit order, saves the cursor after **each** one, and returns the count evaluated.
    - `main(argv=None)` loops `run_once`, then sleeps `--poll` seconds (default 5). On SIGTERM it sets a stop flag that is checked between programs, so the current evaluation finishes and then the process exits 0.
    - CLI: `python -m loop.verifier --root DIR --state FILE --workdir DIR [--salts s1 s2 s3 s4] [--max-workers N] [--poll S]`.
  - `evaluate` is called with `island_best` from the program's island (the `island.joined` with the lowest seq for that program; `None` if unknown) and `cause` = the `program.committed` id.

- [ ] **Step 1: Write the failing tests** (use cheap failing bundles so the tests stay fast).
  - Two committed broken bundles → `run_once` returns 2, giving 2 `candidate.rejected` events. A second `run_once` returns 0. *(Review Focus 4)*
  - With `run_once` interrupted after the first program (monkeypatch `evaluate` to raise on its second call), the cursor file equals the first program's seq. The next `run_once` evaluates only the second.
  - A program that already has a `candidate.rejected` written by someone else is skipped.
- [ ] **Step 2: Run** `uv run pytest tests/loop/test_verifier_service.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run.** Same command. Expected: PASS.
- [ ] **Step 5: Commit** with message `loop: verifier service with persisted cursor and drain-on-stop`.

---

### Task 5: Seeding, islands and parent selection

**Files:**
- Create: `loop/seed.py`, `loop/pool.py`
- Modify: `loop/verifier.py`: replace the `island_best` stub with `pool.island_best`.
- Test: `tests/loop/test_pool.py`, `tests/loop/test_seed.py`

**Interfaces:**
- Produces:
  - `loop/seed.py`:
    - `seed_reference(sub, *, island: str = "main") -> str`. It packs a bundle whose `gen.py` is `verifiers/invsqrt/nr_reference_circuit.py` copied verbatim (its CLI already takes the output path as its first argument).
    - If no `island.created` exists for `island`, it appends one with idea `"Newton-Raphson reference (paper baseline)"`, instructions read from `roles/lab_assistant/v1/island_main.md`, weight 1.0, and subject `bench:invsqrt`.
    - It appends `program.committed` (`parent=None`, `note="Google reference generator (1x)"`) and `island.joined` (`via="seed"`), each only if absent. It is idempotent and returns the program digest.
    - CLI: `python -m loop.seed --root DIR`.
  - `loop/pool.py`:
    - `islands(sub) -> dict[str, dict]`: island id → latest `{idea, instructions, weight, status}`, folding `island.created` then `island.updated` in seq order.
    - `members(sub, island) -> list[str]`: program digests with an `island.joined` for that island, in first-join order.
    - `children(sub, program) -> int`: the number of `attempt.announced` events with subject `"prog:" + program`.
    - `island_best(sub, island) -> float | None`: the lowest cost among passing `frontier("invsqrt", cost_basis=COST_BASIS, k=10_000)` candidates whose program is a member.
    - `pick_island(sub, rng: random.Random) -> str`: weighted by `weight` over `status == "active"` islands. Raises `LookupError` if there are none.
    - `pick_parent(sub, island, rng, *, lam: float = 10.0) -> str`: members with a passing candidate get weight `sigmoid(lam * z) / (1 + children)`, where `z = (median - cost) / (1.4826 * MAD)`. If MAD is 0, z = 0 for all. Members with no passing candidate get weight `0.05 / (1 + children)`. If no member has a passing candidate, it picks uniformly among members. It raises `LookupError` for an empty island.

- [ ] **Step 1: Write the failing tests.**
  - `seed_reference` twice gives the same digest, and exactly one each of `island.created`, `program.committed` and `island.joined`.
  - `islands()` reflects an `island.updated` weight change.
  - `pick_parent` with `rng=random.Random(0)` over 3 members with costs 100, 200, 300 picks the cheapest most often across 1,000 draws, but each at least once.
  - After 50 `attempt.announced` on the cheapest member, that member is no longer the most picked.
  - Two "workers" picking from a one-member island both get that member, and `children` is 2 after both announce. *(Review Focus 5)*
  - `pick_parent` on an empty island raises `LookupError`.
- [ ] **Step 2: Run** `uv run pytest tests/loop/test_pool.py tests/loop/test_seed.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement.** Also write `roles/lab_assistant/v1/island_main.md`, two or three sentences: "Improve the Newton-Raphson reference. Lower qubits × (Toffolis + T/4). Keep x restored and scratch at |0>."
- [ ] **Step 4: Run.** Same command, then `uv run pytest -q`. Expected: PASS.
- [ ] **Step 5: Commit** with message `loop: reference seed, islands and parent selection`.

---

### Task 6: Lab assistant worker (pi)

**Files:**
- Create: `roles/lab_assistant/v1/system.md`, `roles/lab_assistant/v1/task.md`, `roles/lab_assistant/v1/role.json`
- Create: `loop/pi_runner.py`, `loop/worker.py`
- Create: `tests/fakes/openai_server.py`
- Test: `tests/loop/test_pi_runner.py` (`@pytest.mark.pi`), `tests/loop/test_worker.py`

**Interfaces:**
- **`role.json`:** `{"version": "v1", "tools": ["read", "edit", "grep", "find", "ls"], "max_output_tokens": 4096, "thinking_budget": 1024, "timeout_s": 900}`.
- **`system.md`:**
  - the task (invsqrt phase oracle, its contract, the cost formula)
  - house rules: edit with the `edit` tool, small targeted changes, keep reasoning short, never touch files outside the bundle
  - output format: the final message's first line is `SUMMARY: <one line of what you changed and why>`
- **`task.md`:** a template with `{parent_cost}`, `{island_instructions}` and `{lessons}`.
- **`loop/pi_runner.py`:**
  - `write_agent_dir(dest: Path, *, base_url: str, model: str, api_key_env: str, context_window: int, max_tokens: int) -> Path`. It writes `models.json` with provider `"qwen"`, `api: "openai-completions"`, `apiKey: "$<api_key_env>"`, the model entry with `contextWindow` / `maxTokens`, and `compat.thinkingFormat: "qwen"`.
  - `@dataclass(frozen=True) class PiRun` with fields `ok: bool, exit_code: int | None, final_text: str, tokens_in: int, tokens_out: int, tokens_cached: int, seconds: float, error: str | None`.
  - `run_pi(workdir: Path, agent_dir: Path, *, prompt: str, system_append: Path, tools: list[str], model: str, timeout_s: float) -> PiRun`. It runs `pi --mode json --no-session --no-approve -nc --provider qwen --model qwen/<model> --tools <csv> --append-system-prompt <file> <prompt>`, with `cwd=workdir` and env `PI_CODING_AGENT_DIR=<agent_dir>`. If `tools` is empty, it passes `-nt` instead of `--tools`.
    - It reads stdout line by line (LF-split). The final text comes from the last `message_end`, and usage from the last `usage` seen.
    - On a timeout it kills the process group and returns `ok=False` with `error="timeout"`.
    - *Implementation check:* if `PI_CODING_AGENT_DIR` is not honoured by pi 1.5.x, set `HOME` to a temp directory containing `.pi/agent/models.json` instead. Record this as a ledger ruling.
- **`loop/worker.py`:**
  - `run_job(sub, *, rng, cfg: WorkerConfig, workroot: Path) -> str`. It returns the outcome: `"committed"`, `"unchanged"` or `"failed"`.
    1. `pick_island`, then `pick_parent`.
    2. Append `attempt.announced` (subject `prog:<parent>`, payload `{island, plan: "pending"}`).
    3. Unpack the parent into a fresh `workroot/<announcement id>/`, render `task.md`, and call `run_pi`.
    4. Always append `budget.spent` (pool `"qwen"`, tokens, `wall_seconds`, `cause` = the announcement).
    5. If the run is ok and the bundle bytes changed, `pack` and `put_blob`, then append `program.committed` (`parent`, `benchmark`, and a `note` holding the text after `SUMMARY:`, or the first 200 chars of the final text) with `cause` = the announcement, plus `island.joined` (`via="submit"`).
    6. Delete the job directory.
  - `@dataclass class WorkerConfig` with fields `base_url, model, api_key_env, context_window, max_tokens, role_dir: Path, worker_id: int`.
  - `main(argv=None)` loops `run_job`. On SIGTERM it finishes the current job, then exits 0. If no island exists, it sleeps and retries.
  - CLI: `python -m loop.worker --root DIR --id N --base-url URL --model M --api-key-env VAR --workroot DIR [--context-window 32768] [--max-tokens 4096]`.
- **`tests/fakes/openai_server.py`:** `FakeOpenAI(script: list[dict])`, a context manager running a stdlib `ThreadingHTTPServer` on `127.0.0.1:0`.
  - It serves `POST /v1/chat/completions`, both streaming (SSE `chat.completion.chunk`, ending with `data: [DONE]`) and non-streaming.
  - Each request consumes the next script item, either `{"text": str}` or `{"tool_calls": [{"name": str, "arguments": dict}]}`.
  - It reports `usage` on the last chunk, and records the requests received in `.requests`.

- [ ] **Step 1: Write the failing tests.**
  - **`test_worker.py`** (no pi; monkeypatch `loop.worker.run_pi`):
    - When the fake run edits `gen.py` and returns `ok` → outcome `"committed"`. Exactly one `attempt.announced`, `budget.spent`, `program.committed` and `island.joined`. `program.committed.payload.parent` is the seed, its `cause` is the announcement id, and `note` is taken from `SUMMARY:`.
    - A run that changes nothing → `"unchanged"`: no `program.committed`, but `budget.spent` is present. *(Review Focus 3)*
    - A run with `ok=False, error="timeout"` → `"failed"`, with `budget.spent` present. *(Review Focus 3)*
    - A job interrupted after the announcement (fake `run_pi` raises `KeyboardInterrupt`) leaves an orphan announcement, and the next `run_job` still succeeds. *(Review Focus 2)*
  - **`test_pi_runner.py`** (`@pytest.mark.pi`, uses `FakeOpenAI`):
    - A script of `[tool_call edit gen.py "old"→"new", text "SUMMARY: changed x"]` produces a `gen.py` containing `"new"`, `final_text` starting `"SUMMARY:"`, and `tokens_out > 0`.
    - A script that never answers in time (`timeout_s=2` with a server that sleeps) gives `ok=False, error="timeout"`.
- [ ] **Step 2: Run** `uv run pytest tests/loop/test_worker.py -q`. Expected: FAIL. The pi tests skip on the Mac.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** `uv run pytest -q` on the Mac. Expected: PASS, with pi tests skipped. In the VM (after Task 7's dev shell): run `limactl shell devbox-sim -- bash -lc 'cd <repo copy> && nix develop -c uv run pytest -q -m pi'`. Expected: the pi tests PASS.
- [ ] **Step 5: LiteLLM connectivity (manual, once).** Run `write_agent_dir` against `https://litellm-proxy.ml.scale.com/v1` with model `fireworks_ai/qwen3p8-2p4t-a95b` and `api_key_env=LITELLM_API_KEY`, then `run_pi` with prompt `"Reply with exactly: hello"`, `tools=[]` and `max_tokens=64`. Expected: `ok=True`. Record the token counts in the ledger. Do not repeat this.
- [ ] **Step 6: Commit** with message `loop: pi lab assistant worker, fake OpenAI server for tests`.

---

### Task 7: Nix flake: dev shell and NixOS module, proven in the VM

**Files:**
- Create: `flake.nix`, `flake.lock`
- Create: `nix/module.nix`
- Create: `nix/vm-test/flake.nix`, the VM's own system config: imports `nixos-lima` (`services.lima.enable = true`) plus `../module.nix`, with a fake model service.
- Create: `scripts/research-loop-update`
- Create: `docs/deploy.md`, the devbox steps for the human.

**Interfaces:**
- **`flake.nix`:**
  - inputs: `nixpkgs` (`nixos-26.05`) and `pi` (`github:earendil-works/pi/stable`)
  - outputs:
    - `devShells.<system>.default`: python312, uv, nodejs_22, the pi package, gcc and pkg-config (for kickmix's sdist on aarch64-linux), git
    - `nixosModules.default = import ./nix/module.nix`
  - systems: `x86_64-linux`, `aarch64-linux`, `aarch64-darwin`
- **`services.research-loop` options:**
  - `enable`
  - `repo.url`, `repo.ref` (default `"main"`), `repo.sshKeyFile` (nullable)
  - `dataDir` (default `/var/lib/research-loop`)
  - `workers` (default 8)
  - `model.baseUrl`, `model.name`, `model.apiKeyFile` (nullable; loaded via `LoadCredential`)
  - `verifier.maxWorkers` (default 12)
- **Units and settings defined by the module:**
  - User/group `research-loop`. `programs.nix-ld.enable = true`, so uv's manylinux kickmix wheel loads on x86_64.
  - `research-loop-checkout.service`: oneshot. Clones `repo.url@repo.ref` into `${dataDir}/repo` only if it is absent, then runs `uv sync --frozen`.
  - `research-loop-seed.service`: oneshot, after checkout. Runs `uv run python -m loop.seed --root ${dataDir}/substrate`.
  - `research-loop-verifier.service`.
  - `research-loop-worker@.service`: a template. Instances `1..workers` are wanted by `research-loop.target`.
  - Each long-running unit has `WorkingDirectory=${dataDir}/repo`, `KillSignal=SIGTERM`, `TimeoutStopSec=900`, `Restart=on-failure`, `MemoryMax` (worker 2G, verifier 8G), `CPUQuota` for workers (50%), `ProtectSystem=strict`, `ReadWritePaths=${dataDir}`, and `PrivateTmp=true`.
- **`scripts/research-loop-update`:** runs `git -C ${dataDir}/repo pull --ff-only && uv sync --frozen`, then `systemctl restart research-loop-verifier`, then restarts workers one at a time. Each worker drains its current job first.

- [ ] **Step 1: Evaluate on the Mac** via the nix container: `docker run --rm -v "$PWD":/src nixos/nix sh -c 'cd /src && nix --extra-experimental-features "nix-command flakes" flake check --no-build'`. Expected: no evaluation errors.
- [ ] **Step 2: Rebuild the VM** with `nix/vm-test`. The repo is visible read-only at its host path inside the VM; copy it to `/tmp/lima/repo` and point `repo.url` at that path (`file://`). Run `limactl shell devbox-sim -- sudo nixos-rebuild switch --flake /tmp/lima/repo/nix/vm-test#devbox-sim`. Expected: the switch succeeds.
- [ ] **Step 3: Smoke test in the VM.** The fake model service returns one edit and then `SUMMARY`. With `workers = 2`, wait up to 5 minutes, then run `uv run python -m substrate.cli --root /var/lib/research-loop/substrate since --types program.committed candidate.rejected candidate.measured`. Expected:
  - the seed `program.committed`
  - at least one worker `program.committed`
  - a verifier `candidate.*` event for it
  - `systemctl is-active research-loop-verifier research-loop-worker@1` reports both `active`
- [ ] **Step 4: Drain test.** Run `systemctl restart research-loop-worker@1` during a job. Expected: the unit stops only after the job's events are written (check the journal for "finished current job"), and the restarted worker continues.
- [ ] **Step 5: Run the pi tests in the VM dev shell** (Task 6, Step 4). Expected: PASS.
- [ ] **Step 6: Write `docs/deploy.md`:** the devbox flake input, the module options with real values (Spark endpoint, model id, workers 8), the deploy key, `nixos-rebuild switch`, and `research-loop-update`.
- [ ] **Step 7: Commit** with message `nix: dev shell and NixOS module; proven in local NixOS VM`.
