import pytest

from loop import policy
from substrate.cli import Forbidden

P = policy.load(policy.DEFAULT_PATH)


def test_users_map_to_roles():
    assert P.role_for_user("rl-worker") == "worker" and P.role_for_user("rl-live") == "live" and P.role_for_user("nobody") is None
    assert P.role_for_user("rl-researcher") == "researcher"


@pytest.mark.parametrize("principal,type,payload,ok", [
    ("worker", "candidate.measured", {}, False),
    ("worker", "island.joined", {"via": "migration"}, False),
    ("worker", "island.joined", {"via": "submit"}, True),
    ("worker", "budget.spent", {"pool": "cpu"}, False),
    ("worker", "budget.spent", {"pool": "qwen"}, True),
    ("verifier", "candidate.rejected", {}, True),
    ("verifier", "blob", {}, True),
    ("verifier", "program.committed", {}, False),
    ("seed", "island.joined", {"via": "seed"}, True),
    ("substrate", "schema.registered", {}, True),
    ("live", "budget.spent", {"pool": "cpu"}, False),
    ("live", "blob", {}, False),
    ("researcher", "researcher.visited", {}, True),
    ("researcher", "direction", {}, True),
    ("researcher", "island.created", {}, True),
    ("researcher", "island.updated", {}, True),
    ("researcher", "island.joined", {"via": "restart"}, True),
    ("researcher", "island.joined", {"via": "migration"}, True),
    ("researcher", "island.joined", {"via": "submit"}, False),
    ("researcher", "island.joined", {"via": "seed"}, False),
    ("researcher", "lessons.published", {}, True),
    ("researcher", "question.asked", {}, True),
    ("researcher", "quota.observed", {}, True),
    ("researcher", "loop.config_changed", {}, True),
    ("researcher", "budget.spent", {"pool": "claude"}, True),
    ("researcher", "budget.spent", {"pool": "qwen"}, False),
    ("researcher", "candidate.measured", {}, False),
    ("researcher", "human.instructed", {}, False),
    ("researcher", "blob", {}, True),
    ("worker", "direction", {}, False),
    ("unknown-role", "question.asked", {}, False),
    (None, "candidate.measured", {}, True),
])
def test_authorize_table(principal, type, payload, ok):
    if ok:
        P.authorize(principal, type, "x:y", payload)
    else:
        with pytest.raises(Forbidden):
            P.authorize(principal, type, "x:y", payload)


def test_groups_grant_roles_to_unlisted_users():
    assert P.role_for("alice", ["users", "substrate-viewers"]) == "viewer"
    assert P.role_for("alice", ["users"]) is None
    assert P.role_for("rl-worker", ["substrate-viewers"]) == "worker"  # a listed user name wins
    assert "sandeshg" not in P.users  # people are added by the deployment, not the repo


def test_viewer_can_write_nothing():
    for t in ("candidate.measured", "direction", "blob"):
        with pytest.raises(Forbidden):
            P.authorize("viewer", t, "x:y", {})
