"""The substrate's only public surface.

Python callers:   from substrate.cli import Substrate
Other callers:    uv run python -m substrate.cli [--root DIR] <command> ...   (JSON on stdout)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from substrate import _projections
from substrate._blobs import BlobNotFound, Blobs
from substrate._schemas import SchemaViolation, UnknownVersion, event_types, latest_version, schema_digest
from substrate._store import Busy, Store
from substrate.types import BudgetState, Candidate, Event, Node

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "var" / "substrate"
class Forbidden(PermissionError):
    """The authorize hook refused this write."""


_ERRORS = (SchemaViolation, UnknownVersion, BlobNotFound, Busy, Forbidden, ValueError, OSError, sqlite3.Error)


@dataclass(frozen=True)
class Stats:
    events: int = 0
    registered: int = 0


def resolve_root(root: Path | str | None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("SUBSTRATE_ROOT")
    return Path(env) if env else DEFAULT_ROOT


class Substrate:
    """In-process substrate. `authorize(principal, type, subject, payload)` may raise Forbidden.

    The substrate knows nothing about roles: the hook and the principal names come from the caller
    (e.g. substrated's policy file). With no hook, or no principal, writes are unrestricted.
    """

    def __init__(self, root: Path | str | None = None, *, authorize=None):
        self._authorize = authorize
        self.root = resolve_root(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._store = Store(self.root / "events.sqlite")
        self._blobs = Blobs(self.root)

    # ── write ──────────────────────────────────────────────────────────
    def _check(self, principal: str | None, type: str, subject: str, payload: dict) -> None:
        if self._authorize is not None and principal is not None:
            self._authorize(principal, type, subject, payload)

    def put_blob(self, data: bytes, *, principal: str | None = None) -> str:
        self._check(principal, "blob", "", {})
        return self._blobs.put(data)

    def append(self, type: str, payload: dict, *, subject: str, actor: dict,
               cause: str | None = None, type_v: int | None = None, principal: str | None = None,
               id: str | None = None) -> str:
        self._check(principal, type, subject, payload)
        return self._store.append(type, payload, subject=subject, actor=actor, cause=cause, type_v=type_v, id=id)

    # ── projections ────────────────────────────────────────────────────
    def frontier(self, benchmark: str, *, k: int = 20, verifier: str | None = None,
                 cost_basis: str | None = None) -> list[Candidate]:
        return _projections.frontier(self._store, benchmark, k=k, verifier=verifier, cost_basis=cost_basis)

    def lineage(self, ref: str, *, direction: str = "up") -> list[Node]:
        return _projections.lineage(self._store, ref, direction=direction)

    def history(self, subject: str, *, types: list[str] | None = None) -> list[Event]:
        return self._store.history(subject, types=types)

    def budget(self, pool: str, *, window: str = "7d", now: datetime | None = None) -> BudgetState:
        return _projections.budget(self._store, pool, window=window, now=now)

    # ── raw ────────────────────────────────────────────────────────────
    def since(self, cursor: int | None, *, types: list[str] | None = None,
              limit: int = 1000) -> tuple[list[Event], int]:
        return self._store.since(cursor, types=types, limit=limit)

    def get_blob(self, digest: str) -> bytes:
        return self._blobs.get(digest)

    # ── admin ──────────────────────────────────────────────────────────
    def init(self) -> Stats:
        """Append schema.registered for every schema version not yet registered. Idempotent."""
        seen = {ev.payload.get("digest") for ev in self._store.raw_rows(types=["schema.registered"])}
        registered = 0
        for t in event_types():
            for v in range(1, latest_version(t) + 1):
                try:
                    digest = schema_digest(t, v)
                except UnknownVersion:
                    continue
                if digest in seen:
                    continue
                self.append("schema.registered", {"type": t, "type_v": v, "status": "active", "digest": digest},
                            subject=f"schema:{t}", actor={"role": "substrate"})
                registered += 1
        return Stats(registered=registered)

    def export(self, path: Path | str) -> Stats:
        """Write every event, exactly as stored (no upcasting, no seq), one JSON object per line."""
        rows = self._store.raw_rows()
        with open(path, "w") as f:
            for ev in rows:
                env = {k: v for k, v in dataclasses.asdict(ev).items() if k != "seq"}
                f.write(json.dumps(env, sort_keys=True) + "\n")
        return Stats(events=len(rows))


# ── command line ───────────────────────────────────────────────────────
def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="substrate", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", help="data root (default: $SUBSTRATE_ROOT or <repo>/var/substrate)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    a = sub.add_parser("append")
    a.add_argument("--type", required=True)
    a.add_argument("--subject", required=True)
    a.add_argument("--actor", required=True, help="JSON object")
    a.add_argument("--payload", required=True, help="JSON object")
    a.add_argument("--cause")
    a.add_argument("--type-v", type=int)
    sub.add_parser("put-blob").add_argument("file")
    g = sub.add_parser("get-blob")
    g.add_argument("digest")
    g.add_argument("--out", required=True)
    h = sub.add_parser("history")
    h.add_argument("subject")
    h.add_argument("--types", nargs="+")
    s = sub.add_parser("since")
    s.add_argument("--cursor", type=int)
    s.add_argument("--types", nargs="+")
    s.add_argument("--limit", type=int, default=1000)
    f = sub.add_parser("frontier")
    f.add_argument("benchmark")
    f.add_argument("--k", type=int, default=20)
    f.add_argument("--verifier")
    f.add_argument("--cost-basis")
    l = sub.add_parser("lineage")
    l.add_argument("ref")
    l.add_argument("--direction", default="up", choices=["up", "down"])
    b = sub.add_parser("budget")
    b.add_argument("pool")
    b.add_argument("--window", default="7d")
    sub.add_parser("export").add_argument("path")
    return p


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _loads(text: str):
    return json.loads(text, parse_constant=_reject_constant)


def _run(args: argparse.Namespace) -> object:
    s = Substrate(args.root)
    asdicts = lambda items: [dataclasses.asdict(x) for x in items]  # noqa: E731
    match args.command:
        case "init":
            return dataclasses.asdict(s.init())
        case "append":
            return {"id": s.append(args.type, _loads(args.payload), subject=args.subject,
                                   actor=_loads(args.actor), cause=args.cause, type_v=args.type_v)}
        case "put-blob":
            return {"digest": s.put_blob(Path(args.file).read_bytes())}
        case "get-blob":
            Path(args.out).write_bytes(s.get_blob(args.digest))
            return {"out": args.out}
        case "history":
            return asdicts(s.history(args.subject, types=args.types))
        case "since":
            events, cursor = s.since(args.cursor, types=args.types, limit=args.limit)
            return {"events": asdicts(events), "cursor": cursor}
        case "frontier":
            return asdicts(s.frontier(args.benchmark, k=args.k, verifier=args.verifier,
                                      cost_basis=args.cost_basis))
        case "lineage":
            return asdicts(s.lineage(args.ref, direction=args.direction))
        case "budget":
            return dataclasses.asdict(s.budget(args.pool, window=args.window))
        case "export":
            return dataclasses.asdict(s.export(args.path))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except _ERRORS as e:
        name = next(cls.__name__ for cls in _ERRORS if isinstance(e, cls))
        print(json.dumps({"error": name, "message": str(e)}), file=sys.stderr)
        return 2
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
