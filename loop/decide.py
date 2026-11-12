"""The researcher's output contract: parse and validate its JSON, then record it as events.

The model returns ONE JSON object (optionally inside a ```json fence):

    {"directions": [{"brief", "why", "prediction": {metric, island, threshold, by_attempts},
                     "islands": [...], "island_ops": [...]}],
     "lessons": {"working": [...], "not_working": [...], "open_questions": [...]} | null,
     "questions": ["..."]}

`parse_output` validates the shape. `apply` checks the output against the substrate (islands and programs
it names must exist) and only then writes, so invalid output writes nothing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from loop import config, pool
from substrate.cli import Substrate

SUBJECT = "bench:" + config.BENCHMARK
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
METRICS = ("island_best_cost", "bench_best_cost")
STATUSES = ("active", "retired")


class OutputError(ValueError):
    """The researcher's output is not a valid decision set."""


@dataclass(frozen=True)
class Direction:
    brief: str
    why: str
    prediction: dict
    islands: list[str] = field(default_factory=list)
    island_ops: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Decisions:
    directions: list[Direction] = field(default_factory=list)
    lessons: dict | None = None
    questions: list[str] = field(default_factory=list)


# ── parsing ────────────────────────────────────────────────────────────
def _extract(text: str) -> str:
    fences = re.findall(r"```(?:json)?\s*(.*?)```", text, re.S)
    for body in reversed(fences):
        if body.strip().startswith("{"):
            return body.strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise OutputError("no JSON object found in the output")
    return text[start:end + 1]


def _str(obj: dict, key: str, where: str, *, required: bool = True) -> str | None:
    v = obj.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or not v.strip():
        raise OutputError(f"{where}: '{key}' must be a non-empty string")
    return v


def _num(v, where: str, *, minimum: float | None = None) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v in (float("inf"), float("-inf")):
        raise OutputError(f"{where} must be a finite number, got {v!r}")
    if minimum is not None and v < minimum:
        raise OutputError(f"{where} must be >= {minimum}, got {v!r}")
    return v


def _strs(v, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise OutputError(f"{where} must be a list of strings")
    return v


def _prediction(p, where: str) -> dict:
    if not isinstance(p, dict):
        raise OutputError(f"{where}: prediction must be an object")
    if p.get("metric") not in METRICS:
        raise OutputError(f"{where}: prediction.metric must be one of {METRICS}")
    island = p.get("island")
    if p["metric"] == "island_best_cost" and not (isinstance(island, str) and island):
        raise OutputError(f"{where}: prediction.island is required for island_best_cost")
    if p["metric"] == "bench_best_cost":
        island = None
    threshold = _num(p.get("threshold"), f"{where}: prediction.threshold")
    by = p.get("by_attempts")
    if isinstance(by, bool) or not isinstance(by, int) or by < 1:
        raise OutputError(f"{where}: prediction.by_attempts must be an integer >= 1")
    return {"metric": p["metric"], "island": island, "threshold": threshold, "by_attempts": by}


def _op(op, where: str) -> dict:
    if not isinstance(op, dict):
        raise OutputError(f"{where} must be an object")
    kind = op.get("op")
    if kind == "create":
        seed = op.get("seed_from")
        if seed is not None and not (isinstance(seed, str) and DIGEST.match(seed)):
            raise OutputError(f"{where}: seed_from must be a program digest or null")
        return {"op": "create", "island": _str(op, "island", where), "idea": _str(op, "idea", where),
                "instructions": _str(op, "instructions", where),
                "weight": _num(op.get("weight", 1.0), f"{where}: weight", minimum=0), "seed_from": seed}
    if kind == "update":
        out = {"op": "update", "island": _str(op, "island", where)}
        if "instructions" in op:
            out["instructions"] = _str(op, "instructions", where)
        if "weight" in op:
            out["weight"] = _num(op["weight"], f"{where}: weight", minimum=0)
        if "status" in op:
            if op["status"] not in STATUSES:
                raise OutputError(f"{where}: status must be one of {STATUSES}")
            out["status"] = op["status"]
        if len(out) == 2:
            raise OutputError(f"{where}: update changes nothing (give instructions, weight or status)")
        return out
    if kind in ("restart", "migrate"):
        target = "island" if kind == "restart" else "to_island"
        program = op.get("program")
        if not (isinstance(program, str) and DIGEST.match(program)):
            raise OutputError(f"{where}: program must be a program digest")
        return {"op": kind, target: _str(op, target, where), "program": program}
    raise OutputError(f"{where}: unknown op {kind!r}")


def parse_output(text: str) -> Decisions:
    try:
        obj = json.loads(_extract(text))
    except ValueError as e:
        raise OutputError(f"output is not valid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise OutputError("output must be a JSON object")
    directions = []
    for i, d in enumerate(obj.get("directions") or []):
        where = f"directions[{i}]"
        if not isinstance(d, dict):
            raise OutputError(f"{where} must be an object")
        ops = d.get("island_ops") or []
        if not isinstance(ops, list):
            raise OutputError(f"{where}: island_ops must be a list")
        directions.append(Direction(
            brief=_str(d, "brief", where), why=_str(d, "why", where),
            prediction=_prediction(d.get("prediction"), where),
            islands=_strs(d.get("islands") or [], f"{where}: islands"),
            island_ops=[_op(o, f"{where}.island_ops[{j}]") for j, o in enumerate(ops)]))
    lessons = obj.get("lessons")
    if lessons is not None:
        if not isinstance(lessons, dict):
            raise OutputError("lessons must be an object or null")
        lessons = {"working": _strs(lessons.get("working", []), "lessons.working"),
                   "not_working": _strs(lessons.get("not_working", []), "lessons.not_working"),
                   "open_questions": _strs(lessons.get("open_questions", []), "lessons.open_questions")}
    return Decisions(directions, lessons, _strs(obj.get("questions") or [], "questions"))


# ── applying ───────────────────────────────────────────────────────────
def _check_against_substrate(sub: Substrate, decisions: Decisions) -> None:
    known = set(pool.islands(sub))
    for d in decisions.directions:
        for op in d.island_ops:
            if op["op"] == "create":
                if op["island"] in known:
                    raise OutputError(f"island {op['island']!r} already exists")
                known.add(op["island"])
    for d in decisions.directions:
        for op in d.island_ops:
            targets = [op.get("island"), op.get("to_island")]
            if any(t is not None and t not in known for t in targets):
                raise OutputError(f"op {op['op']} names an unknown island: {targets}")
            for program in filter(None, [op.get("program"), op.get("seed_from")]):
                if not sub.history("prog:" + program, types=["program.committed"]):
                    raise OutputError(f"unknown program {program}")
        pr = d.prediction
        if pr["island"] is not None and pr["island"] not in known:
            raise OutputError(f"prediction names an unknown island {pr['island']!r}")


def apply(sub: Substrate, decisions: Decisions, *, visit_id: str, actor: dict) -> list[str]:
    """Record the decisions. Raises OutputError, before writing anything, if they name unknown islands/programs."""
    _check_against_substrate(sub, decisions)
    ids: list[str] = []

    def put(type_: str, payload: dict, cause: str, subject: str = SUBJECT) -> str:
        eid = sub.append(type_, payload, subject=subject, actor=actor, cause=cause)
        ids.append(eid)
        return eid

    for d in decisions.directions:
        did = put("direction", {"brief": d.brief, "why": d.why, "prediction": d.prediction,
                                "islands": d.islands}, visit_id)
        for op in d.island_ops:
            joined = lambda program, island, via: put(  # noqa: E731
                "island.joined", {"program": program, "island": island, "via": via}, did, "prog:" + program)
            match op["op"]:
                case "create":
                    put("island.created", {k: op[k] for k in ("island", "idea", "instructions", "weight")}, did)
                    if op["seed_from"]:
                        joined(op["seed_from"], op["island"], "restart")
                case "update":
                    put("island.updated", {k: v for k, v in op.items() if k != "op"}, did)
                case "restart":
                    joined(op["program"], op["island"], "restart")
                case "migrate":
                    joined(op["program"], op["to_island"], "migration")
    if decisions.lessons is not None:
        put("lessons.published", decisions.lessons, visit_id)
    for q in decisions.questions:
        put("question.asked", {"question": q}, visit_id)
    return ids


def parse_and_apply(sub: Substrate, text: str, *, visit_id: str, actor: dict) -> tuple[list[str], str | None]:
    """(event ids, None) on success; ([], error message) if the output is invalid. Nothing is written on error."""
    try:
        return apply(sub, parse_output(text), visit_id=visit_id, actor=actor), None
    except OutputError as e:
        return [], str(e)
