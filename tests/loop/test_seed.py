from loop.seed import seed_reference
from substrate.cli import Substrate


def test_seed_is_idempotent(tmp_path):
    sub = Substrate(tmp_path / "s")
    a, b = seed_reference(sub), seed_reference(sub)
    assert a == b
    for t, subject in [("island.created", "bench:invsqrt"), ("program.committed", "prog:" + a),
                       ("island.joined", "prog:" + a)]:
        assert len(sub.history(subject, types=[t])) == 1
    commit = sub.history("prog:" + a, types=["program.committed"])[0]
    assert commit.payload["parent"] is None
