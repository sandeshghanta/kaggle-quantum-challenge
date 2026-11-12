"""The substrate: append-only event log, blobs and queries. See SPEC.md."""


def connect(target: str):
    """`unix:/path/api.sock` -> RemoteSubstrate (via substrated); anything else -> in-process Substrate(dir)."""
    if target.startswith("unix:"):
        from substrate.client import RemoteSubstrate
        return RemoteSubstrate(target[len("unix:"):])
    from substrate.cli import Substrate
    return Substrate(target)
