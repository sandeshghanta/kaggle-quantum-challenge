"""Entry point: python -I gen.py <out.kmb>

Builds the Marsyas invsqrt circuit (fit tables -> H/L-split signed-digit Horner generator -> exact-domain
post-pass), exactly as the source notebook's build cell does, and writes it to argv[1]. Deterministic, no
network, writes only the output path.
"""
import os
import sys

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import exact  # noqa: E402
import hsplit  # noqa: E402

ARGS = ['28', '19', '8', '1', 'sd=1', 'dropA=5', 'LB=21', 'S=20', 'mdel=1',
        'wins=17:11,10;15:9,8;13:7,6;11:5,4;9:3,2;7:1,0']


def main(out: str) -> None:
    # exact.py's temp file goes through the system temp dir (private /tmp in the sandbox) and is removed.
    stage = out + ".stage.kmb"
    try:
        hsplit.main(ARGS + [f"out={stage}"])
        exact.run(stage, out)
    finally:
        if os.path.exists(stage):
            os.remove(stage)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: gen.py <out.kmb>")
    main(sys.argv[1])
