"""Convert stored payloads to the latest version of their type, on read.

Add one function per step: UPCASTERS[(type, from_v)] maps a v`from_v` payload to
v`from_v + 1`. Stored events are never rewritten.
"""

from __future__ import annotations

from collections.abc import Callable

from substrate._schemas import SchemaViolation, UnknownVersion, latest_version

UPCASTERS: dict[tuple[str, int], Callable[[dict], dict]] = {}


def upcast(type: str, type_v: int, payload: dict,
           registry: dict[tuple[str, int], Callable[[dict], dict]] | None = None) -> tuple[int, dict]:
    steps = UPCASTERS if registry is None else registry
    try:
        latest = latest_version(type)
    except SchemaViolation as e:  # a type this reader has no schema for (written by a newer loop)
        raise UnknownVersion(f"unknown event type on read: {type}") from e
    if type_v > latest:
        raise UnknownVersion(f"{type} v{type_v} is newer than this reader (latest v{latest})")
    while type_v < latest:
        step = steps.get((type, type_v))
        if step is None:
            raise UnknownVersion(f"no upcaster for {type} v{type_v} -> v{type_v + 1}")
        payload = step(payload)
        type_v += 1
    return type_v, payload
