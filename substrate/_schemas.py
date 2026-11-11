"""Schema registry: loads substrate/schema/<type>/v<N>.json and validates payloads."""

from __future__ import annotations

import functools
import hashlib
import json
import re
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
_VERSION_FILE = re.compile(r"^v(\d+)\.json$")


class SchemaViolation(ValueError):
    """A payload or envelope does not match its schema, or the type is unknown."""


class UnknownVersion(LookupError):
    """No schema exists for this version; the reader refuses rather than guesses."""


@functools.cache
def _versions() -> dict[str, dict[int, Path]]:
    out: dict[str, dict[int, Path]] = {}
    for type_dir in sorted(SCHEMA_DIR.iterdir()):
        if not type_dir.is_dir():
            continue
        for f in type_dir.iterdir():
            m = _VERSION_FILE.match(f.name)
            if m:
                out.setdefault(type_dir.name, {})[int(m.group(1))] = f
    return out


@functools.cache
def _validator(type: str, type_v: int) -> Draft202012Validator:
    path = _versions().get(type, {}).get(type_v)
    if path is None:
        raise UnknownVersion(f"no schema for {type} v{type_v}")
    return Draft202012Validator(json.loads(path.read_text()))


def event_types() -> list[str]:
    return sorted(t for t in _versions() if t != "envelope")


def latest_version(type: str) -> int:
    if type == "envelope" or type not in _versions():
        raise SchemaViolation(f"unknown event type: {type}")
    return max(_versions()[type])


def _check(validator: Draft202012Validator, instance: dict, label: str) -> None:
    error = best_match(validator.iter_errors(instance))
    if error is not None:
        path = "/".join(str(p) for p in error.absolute_path) or "<root>"
        raise SchemaViolation(f"{label}: {path}: {error.message}")


def validate_payload(type: str, type_v: int, payload: dict) -> None:
    latest_version(type)  # raises SchemaViolation for an unknown type
    _check(_validator(type, type_v), payload, f"{type} v{type_v}")


def validate_envelope(env: dict) -> None:
    _check(_validator("envelope", 1), env, "envelope v1")


def schema_digest(type: str, type_v: int) -> str:
    path = _versions().get(type, {}).get(type_v)
    if path is None:
        raise UnknownVersion(f"no schema for {type} v{type_v}")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
