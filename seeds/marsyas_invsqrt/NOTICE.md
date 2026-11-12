# Notice

This bundle is derived from the Kaggle notebook "VQCC invsqrt 13.94x - full source" by Marsyas:
https://www.kaggle.com/code/marsyas/vqcc-invsqrt-13-94x-full-source
(licence stated in the notebook: Apache License 2.0; the standard text is in `LICENSE`).
The notebook credits further public ideas (signed-digit rows, H/L split, merged unlookup, exact-domain pass,
nested windows, angle calibration); see its description.

`model.py`, `fit.py`, `build.py`, `calib.py`, `check.py`, `hsplit.py`, `exact.py`, `run.py` are the notebook's
module cells with the `%%writefile` line removed. Changes:

- `gen.py` is new: it runs what the notebook's build cell runs (`hsplit.py 28 19 8 1 sd=1 dropA=5 LB=21 S=20
  mdel=1 wins=...` followed by `exact.py`) and writes the result to `argv[1]`.
- `model.py`: the reference-table cache (`../../work/invsqrt/ref.npz`) is now an in-memory cache.
- `run.py`: the fitted-table cache (`tab_*.npy` under `../../work/invsqrt`) is now an in-memory cache.
- `hsplit.py`: the on-disk calibration cache (`hang_*.pkl`) is removed; calibration always runs.
- Not imported: the notebook's setup, verifier, and packaging cells (they need the competition input directory).
- Behaviour differences: none intended. Unchanged: `exact.py` still stages the circuit through a temporary
  `.kmx` file in the system temp dir (private `/tmp` inside the sandbox) and deletes it.
