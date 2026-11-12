import re

from loop import config


def test_verifier_tag_names_kickmix_and_verifier_hash():
    assert re.fullmatch(r"kickmix@1\.0\.0\+invsqrt@[0-9a-f]{12}", config.verifier_tag())


def test_actor_has_provenance_fields():
    a = config.actor("lab_assistant", "v1", "m")
    assert set(a) == {"role", "harness", "host", "model"}
    assert a["harness"].startswith("lab_assistant/v1@") and a["model"] == "m"
    assert "model" not in config.actor("verifier", "v1")
