"""The substrate: append-only event log, blobs and queries. See SPEC.md."""


def connect(target: str):
    """`unix:/path/api.sock` or `http://host:port` -> RemoteSubstrate (via substrated); anything else -> in-process
    Substrate(dir). Over HTTP the role token is read from the file named by $SUBSTRATE_TOKEN_FILE."""
    if target.startswith("unix:"):
        from substrate.client import RemoteSubstrate
        return RemoteSubstrate(target[len("unix:"):])
    if target.startswith("http://"):
        import os
        from pathlib import Path
        from substrate.client import RemoteSubstrate
        token_file = os.environ.get("SUBSTRATE_TOKEN_FILE")
        token = Path(token_file).read_text().strip() if token_file else None
        return RemoteSubstrate(url=target, token=token)
    from substrate.cli import Substrate
    return Substrate(target)
