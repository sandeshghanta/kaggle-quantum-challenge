import pytest

from loop import policy
from substrate.cli import Forbidden

P = policy.load(policy.DEFAULT_PATH)


def test_users_map_to_roles():
    assert P.role_for_user("rl-worker") == "worker" and P.role_for_user("rl-live") == "live" and P.role_for_user("nobody") is None


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
    ("unknown-role", "question.asked", {}, False),
    (None, "candidate.measured", {}, True),
])
def test_authorize_table(principal, type, payload, ok):
    if ok:
        P.authorize(principal, type, "x:y", payload)
    else:
        with pytest.raises(Forbidden):
            P.authorize(principal, type, "x:y", payload)
