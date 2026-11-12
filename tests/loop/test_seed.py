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


def test_seed_marsyas_is_idempotent(tmp_path):
    from loop.seed import seed_marsyas
    sub = Substrate(tmp_path / "s")
    a, b = seed_marsyas(sub), seed_marsyas(sub)
    assert a == b
    created = sub.history("bench:invsqrt", types=["island.created"])
    assert [e.payload["island"] for e in created] == ["marsyas"]
    for t in ("program.committed", "island.joined"):
        assert len(sub.history("prog:" + a, types=[t])) == 1


def test_seed_reference_and_marsyas_coexist(tmp_path):
    from loop.seed import seed_marsyas
    sub = Substrate(tmp_path / "s")
    assert seed_reference(sub) != seed_marsyas(sub)
    islands = {e.payload["island"] for e in sub.history("bench:invsqrt", types=["island.created"])}
    assert islands == {"main", "marsyas"}


def test_marsyas_bundle_builds_and_passes_sweep(tmp_path):
    from loop import config
    from loop.programs import build
    from loop.verify import sweep_check
    bundle = config.REPO / "seeds" / "marsyas_invsqrt"
    built = build(bundle, tmp_path / "circuit.kmb", timeout=600)
    assert built.ok, built.stderr
    r = sweep_check(built.kmb)
    assert r.passes, r.error
    assert abs(r.metrics["Q"] - 92) <= 5
    assert r.cost < 300_000
