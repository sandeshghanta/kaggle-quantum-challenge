"""Role policy for substrated: which Unix user is which role, and what each role may write."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from substrate.cli import Forbidden

DEFAULT_PATH = Path(__file__).with_name("policy.json")


@dataclass(frozen=True)
class Policy:
    users: dict[str, str]
    roles: dict[str, dict]
    groups: dict[str, str] = field(default_factory=dict)

    def role_for_user(self, user: str) -> str | None:
        return self.users.get(user)

    def role_for(self, user: str | None, groups: list[str]) -> str | None:
        """A listed user name wins; otherwise the first of the caller's groups that maps to a role."""
        if user and user in self.users:
            return self.users[user]
        return next((self.groups[g] for g in groups if g in self.groups), None)

    def authorize(self, principal: str | None, type: str, subject: str, payload: dict) -> None:
        if principal is None:
            return
        role = self.roles.get(principal)
        if role is None:
            raise Forbidden(f"unknown role {principal!r}")
        if type == "blob":
            if not role.get("blob"):
                raise Forbidden(f"{principal} may not store blobs")
            return
        rules = role.get("append", {})
        if type not in rules:
            raise Forbidden(f"{principal} may not append {type}")
        for field, allowed in rules[type].items():
            if payload.get(field) not in allowed:
                raise Forbidden(f"{principal} may append {type} only with {field} in {allowed}")


def load(path: Path = DEFAULT_PATH) -> Policy:
    data = json.loads(Path(path).read_text())
    return Policy(users=data["users"], roles=data["roles"], groups=data.get("groups", {}))
