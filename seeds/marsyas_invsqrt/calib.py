"""LP recalibration of the Z_POW angles (idea from lightningv08's notebook, reimplemented).

The phase is sum_j theta_j * bit_j(x) over the shifted register positions. Exact angles are
theta_j = 2^(j - R0 - 10) turns. We re-optimise the non-Clifford/non-T angles (and may drop
the lowest positions) to minimise max |error| / tol over all x, by cutting-plane LP.
Angles are integers in units of 2^-64 turns, so the final check is exact.
"""
from __future__ import annotations

import json
import sys

import numpy as np
from scipy.optimize import linprog

import model as M


def register_bits(tables, prm, xs):
    t, xn, p, s = M.decompose(xs)
    Y = M.model_Yp(xn, p, tables, prm, check=False) & ((1 << (prm.F0 - 1)) - 1)
    R0 = prm.F0 - prm.G0
    V = ((Y >> prm.G0) + (1 << (R0 - 1))) << s
    return V, R0


def exact_angles(R0):
    """units of 2^-64 turns for positions 0..R0+9."""
    return np.array([(1 << (64 + j - R0 - 10)) for j in range(R0 + 10)], dtype=object)


def phases(V, ang):
    acc = np.zeros(len(V), dtype=np.uint64)
    for j, a in enumerate(ang):
        a = int(a) & M.MASK64
        if a == 0:
            continue
        acc += ((V >> j) & 1).astype(np.uint64) * np.uint64(a)
    return acc


def check(tables, prm, ang):
    target, tol = M.reference()
    xs = np.arange(1, M.MAX_X + 1, dtype=np.int64)
    V, R0 = register_bits(tables, prm, xs)
    ph = phases(V, ang)
    err = (ph - target).view(np.int64)
    used = np.abs(err).astype(np.float64)
    ratio = used / tol
    margin = (tol.astype(np.float64) - used).min()
    return float(ratio.max()), float(margin), err, tol, V


def calibrate(tables, prm, drop=0, iters=12, verbose=True, mode='margin'):
    R0 = prm.F0 - prm.G0
    ang0 = exact_angles(R0)
    rot = [j for j in range(R0 + 7)]  # rotation positions (T at R0+7, S, Z above)
    dropped = rot[:drop]
    var = rot[drop:]
    ang = ang0.copy()
    for j in dropped:
        ang[j] = 0
    worst, margin, err, tol, V = check(tables, prm, ang)
    if verbose:
        print('start worst', worst, 'margin', margin, flush=True)
    bits = np.stack([((V >> j) & 1).astype(np.float32) for j in var], axis=1)
    tolf = tol.astype(np.float64)
    errf = err.astype(np.float64)
    act = np.argsort(-np.abs(errf) / tolf)[:20000] if mode == 'ratio' else np.argsort(tolf - np.abs(errf))[:20000]
    best = None
    for it in range(iters):
        S = 2.0 ** 20
        A = bits[act].astype(np.float64)
        e = errf[act] / S
        tl = tolf[act] / S
        n = len(var)
        if mode == 'ratio':
            # vars: delta_j (units), lam ; minimise lam ; |e + A d| <= lam * tl
            c = np.zeros(n + 1); c[-1] = 1
            Aub = np.r_[np.c_[A, -tl], np.c_[-A, -tl]]
            bub = np.r_[-e, e]
        else:
            # maximise m: |e + A d| + m <= tl   (m = -lam)
            c = np.zeros(n + 1); c[-1] = -1
            one = np.ones_like(tl)
            Aub = np.r_[np.c_[A, one], np.c_[-A, one]]
            bub = np.r_[tl - e, tl + e]
        r = linprog(c, A_ub=Aub, b_ub=bub, bounds=[(-2**20, 2**20)] * n + [((0, None) if mode == 'ratio' else (None, None))], method='highs')
        if r.x is None:
            print('LP failed', r.status, r.message, flush=True)
            break
        d = np.round(r.x[:n] * S).astype(np.int64)
        tot = errf + bits @ d.astype(np.float64)
        ratio = np.abs(tot) / tolf
        slack = tolf - np.abs(tot)
        w = ratio.max() if mode == 'ratio' else (2.0 if slack.min() <= 0 else 1.0 / slack.min())
        if verbose:
            print(f'it {it} lp {r.x[-1]:.4g} full {w:.4g} active {len(act)} worst {ratio.max():.4f} minslack {slack.min():.4g}', flush=True)
        if best is None or w < best[0]:
            best = (w, d.copy())
        new = np.argsort(-ratio)[:5000] if mode == 'ratio' else np.argsort(slack)[:5000]
        act2 = np.union1d(act, new)
        if len(act2) == len(act):
            break
        act = act2
    w, d = best
    for k, j in enumerate(var):
        ang[j] = int(ang0[j]) + int(d[k])
    worst, margin, _, _, _ = check(tables, prm, ang)
    return ang, worst, margin, len(var)


if __name__ == '__main__':
    import run
    prm, kv = run.parse(sys.argv[1:])
    tables = run.get_tables(prm)
    for drop in [int(v) for v in kv.get('drops', '0').split(',')]:
        ang, worst, margin, nrot = calibrate(tables, prm, drop=drop, mode=kv.get('mode', 'margin'))
        res = {'drop': drop, 'worst': worst, 'margin': margin, 'nrot': nrot}
        if worst < 1 and margin > 0:
            res['rotC'] = M.rotation_cost(nrot, margin) / 4
        print(json.dumps(res), flush=True)
        np.save(f'{run.WORK}/ang_{run.tag(prm)}_d{drop}.npy', np.array([int(a) for a in ang], dtype=object), allow_pickle=True)

