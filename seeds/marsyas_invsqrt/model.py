"""Exact integer model of our invsqrt circuit (single-bit normalization, direct quadratic).

Notation
  x in [1, MAX_X], 22-bit. t = 21 - floor(log2 x) (leading zeros), x_n = x << t in [2^21, 2^22).
  t = 16 f16 + 8 f8 + 4 f4 + 2 f2 + f1,  s = t >> 1,  p = f1.
  1/sqrt(x) = 2^(s-10) * Y,  Y = (p ? 1 : 2^-1/2) / sqrt(x_n / 2^21)  in (1/2, 1].
  Segment address a = (p << 7) | ((x_n >> 14) & 127); delta_int = x_n & 0x3FFF (14 bits).
  Centered delta: dc = delta_int - 8192 (units 2^-21); bit 13 is the sign row (weight -2^-8 when 0).

Arithmetic (all integer, column-truncated rows):
  T  = C1 - sum_{i<13} b_i (C2 >> eA_i) + (1-b13) (C2 >> eA_13)          (step A)
  Y' = C0 - sum_{i<13} b_i (T  >> eB_i) + (1-b13) (T  >> eB_13)  mod 2^(F0-1)   (step B)
  with eA_i = 21 + F2 - FT - i, eB_i = 21 + FT - F0 - i (negative => left shift).
  Phase (turns) = 2^(s-10) * (Y' 2^-F0 + 1/2).
Rows whose operand is identically zero (or listed in drop sets) are skipped.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field

import numpy as np

MAX_X = 3133452
WIDTH = 22
MASK64 = (1 << 64) - 1
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, '..', '..', 'work', 'invsqrt', 'ref.npz')


def shr(v, e):
    """v * 2^-e floored (e may be negative)."""
    return (v >> e) if e >= 0 else (v << -e)


@dataclass(frozen=True)
class Params:
    F0: int = 27          # Y LSB weight 2^-F0; register holds Y - 1/2 in F0-1 bits
    FT: int = 21          # t LSB 2^-FT; t register FT bits (t <= 1/2)
    F2: int = 14          # c2 LSB 2^-F2; register F2-1 bits (c2 < 1/2)
    G0: int = 0           # unrotated guard LSBs of Y' (phase uses Y' >> G0)
    rowsA: tuple = tuple(range(14))   # delta bits used in step A (13 = sign row); booth: digits 0..6
    rowsB: tuple = tuple(range(14))
    booth: bool = False
    tcap: bool = False    # T < 2^(FT-1): T register FT-1 bits
    sd: bool = False      # all +-1 signed digits, d' = sum (2b_i-1) 2^(i-22), rounded rows

    @property
    def nT(self):
        return self.FT - 1 if self.tcap else self.FT

    def eA(self, i):
        return 21 + self.F2 - self.FT - i

    def eB(self, i):
        return 21 + self.FT - self.F0 - i


def decompose(xs):
    xs = np.asarray(xs, dtype=np.int64)
    lg = np.floor(np.log2(xs.astype(np.float64))).astype(np.int64)
    # fix any float rounding
    lg = np.where((1 << (lg + 1)) <= xs, lg + 1, lg)
    lg = np.where((1 << lg) > xs, lg - 1, lg)
    t = 21 - lg
    xn = xs << t
    p = t & 1
    s = t >> 1
    return t, xn, p, s


_REF = None


def reference(force=False):
    global _REF
    """target (uint64 units 2^-64) and tol for all x, cached."""
    if _REF is not None and not force:  # NOTICE: in-memory cache (was an on-disk ref.npz)
        return _REF
    rs = [math.isqrt((1 << 130) // x) for x in range(1, MAX_X + 1)]
    target = np.array([((r + 1) >> 1) & MASK64 for r in rs], dtype=np.uint64)
    tol = np.array([min(r >> 25, 1 << 63) for r in rs], dtype=np.uint64)
    _REF = (target, tol)
    return target, tol


def booth_digits(b):
    """radix-4 digits of centered delta (two's complement bits c = b ^ 2^13)."""
    c = b ^ (1 << 13)
    out = []
    for j in range(7):
        hi = (c >> (2 * j + 1)) & 1
        mid = (c >> (2 * j)) & 1
        lo = (c >> (2 * j - 1)) & 1 if j else np.zeros_like(c)
        out.append(-2 * hi + mid + lo)
    return out


def booth_apply(acc, op, b, rows, e_of):
    ds = booth_digits(b)
    for j in rows:
        d = ds[j]
        e = e_of(2 * j)
        mag = np.abs(d)
        v = np.where(mag == 2, shr(op, e - 1), np.where(mag == 1, shr(op, e), 0))
        acc = acc - np.sign(d) * v
    return acc


def rd(v, s):
    if s <= 0:
        return v << (-s)
    return (v >> s) + ((v >> (s - 1)) & 1)


def sd_apply(acc, op, b, rows, e_of):
    for i in rows:
        sig = 2 * ((b >> i) & 1) - 1
        acc = acc - sig * rd(op, e_of(i) + 1)
    return acc


def eval_T(C1, C2, b, prm: Params):
    """C1, C2 arrays (per element), b = delta_int array. Returns T."""
    if prm.sd:
        return sd_apply(C1.copy(), C2, b, prm.rowsA, prm.eA)
    if prm.booth:
        return booth_apply(C1.copy(), C2, b, prm.rowsA, prm.eA)
    T = C1.copy()
    for i in prm.rowsA:
        op = shr(C2, prm.eA(i))
        bit = (b >> i) & 1
        if i == 13:
            T = T + (1 - bit) * op
        else:
            T = T - bit * op
    return T


def eval_Y(C0, T, b, prm: Params):
    if prm.sd:
        return sd_apply(C0.copy(), T, b, prm.rowsB, prm.eB)
    if prm.booth:
        return booth_apply(C0.copy(), T, b, prm.rowsB, prm.eB)
    Y = C0.copy()
    for i in prm.rowsB:
        op = shr(T, prm.eB(i))
        bit = (b >> i) & 1
        if i == 13:
            Y = Y + (1 - bit) * op
        else:
            Y = Y - bit * op
    return Y


def model_Yp(xn, p, tables, prm: Params, check=True):
    """Y' (before mod) for normalized inputs. tables: int64 array (256,3) [C0, C1, C2]."""
    a = (p << 7) | ((xn >> 14) & 127)
    b = xn & 0x3FFF
    C0, C1, C2 = tables[a, 0], tables[a, 1], tables[a, 2]
    T = eval_T(C1, C2, b, prm)
    if check:
        if (T < 0).any() or (T >= (1 << prm.nT)).any() or (C1 >= (1 << prm.nT)).any():
            raise OverflowError('T out of register')
    Y = eval_Y(C0, T, b, prm)
    if check and ((Y < 0).any() or (Y >= (1 << (prm.F0 - 1))).any()):
        raise OverflowError("Y' out of register")
    return Y


ANGLES = None  # optional custom angles (units 2^-64 turns per shift-register position)
PHASE_FN = None  # optional override: xs -> uint64 phase units


def phase_units(xs, tables, prm: Params, check=True):
    if PHASE_FN is not None:
        return PHASE_FN(np.asarray(xs, dtype=np.int64))
    if ANGLES is not None:
        t, xn, p, s = decompose(xs)
        Y = model_Yp(xn, p, tables, prm, check=check) & ((1 << (prm.F0 - 1)) - 1)
        R0 = prm.F0 - prm.G0
        V = ((Y >> prm.G0) + (1 << (R0 - 1))) << s
        acc = np.zeros(len(V), dtype=np.uint64)
        for j, a in enumerate(ANGLES):
            a = int(a) % (1 << 64)
            if a:
                acc += ((V >> j) & 1).astype(np.uint64) * np.uint64(a)
        return acc
    t, xn, p, s = decompose(xs)
    Y = model_Yp(xn, p, tables, prm, check=check)
    Y &= (1 << (prm.F0 - 1)) - 1
    # phase turns = 2^(s-10) (Y 2^-F0 + 1/2) = (Y + 2^(F0-1)) 2^(s - 10 - F0)
    R0 = prm.F0 - prm.G0
    v = ((Y >> prm.G0) + (1 << (R0 - 1))).astype(np.uint64)
    sh = (64 - 10 - R0 + s).astype(np.uint64)  # all >= 0 since F0+10 <= 64
    return v << sh  # uint64 wraps mod 2^64 = mod 1 turn


def full_check(tables, prm: Params, chunk=1 << 20):
    target, tol = reference()
    worst = 0.0
    min_margin = 1 << 64
    worst_x = 0
    for st in range(1, MAX_X + 1, chunk):
        xs = np.arange(st, min(st + chunk, MAX_X + 1), dtype=np.int64)
        ph = phase_units(xs, tables, prm)
        tg = target[xs - 1]
        tl = tol[xs - 1]
        err = np.abs((ph - tg).view(np.int64)).view(np.uint64)
        ratio = err / tl.astype(np.float64)
        k = int(np.argmax(ratio))
        if ratio[k] > worst:
            worst = float(ratio[k]); worst_x = int(xs[k])
        bad = err > tl
        if bad.any():
            i = int(np.flatnonzero(bad)[0])
            return {'pass': False, 'first_bad_x': int(xs[i]), 'worst': worst}
        min_margin = min(min_margin, int((tl - err).min()))
    return {'pass': True, 'worst': worst, 'worst_x': worst_x, 'min_margin': min_margin}


def rotation_cost(n_rot, min_margin_units):
    eps = math.pi * min_margin_units / 2**64 / n_rot
    return n_rot * (0.56 * math.log2(1 / eps) + 5.3)


def window_ranges(tables, prm: Params, xn, p):
    """For steps A and B: min/max of (acc_low + signed row sum) as a function of W,
    returns the minimal W such that the result fits a 2-bit signed guard."""
    a = (p << 7) | ((xn >> 14) & 127)
    b = xn & 0x3FFF
    C0, C1, C2 = tables[a, 0], tables[a, 1], tables[a, 2]
    T = eval_T(C1, C2, b, prm)
    dA = T - C1
    Y = eval_Y(C0, T, b, prm)
    dB = Y - C0
    out = {}
    for name, acc, d, n in (('A', C1, dA, prm.nT), ('B', C0, dB, prm.F0 - 1)):
        W = None
        for w in range(1, n + 1):
            r = (acc & ((1 << w) - 1)) + d
            if r.min() >= -(1 << (w + 1)) and r.max() < (1 << (w + 1)):
                W = w
                break
        out[name] = W if W is not None else n
    return out


def window_ranges1(tables, prm: Params, xn, p):
    """1-guard offset scheme: minimal W with (acc mod 2^(W+1)) - 2^W + d in [-2^(W+1), 2^(W+1))."""
    a = (p << 7) | ((xn >> 14) & 127)
    b = xn & 0x3FFF
    C0, C1, C2 = tables[a, 0], tables[a, 1], tables[a, 2]
    T = eval_T(C1, C2, b, prm)
    Y = eval_Y(C0, T, b, prm)
    out = {}
    for name, acc, d, n in (('A', C1, T - C1, prm.nT), ('B', C0, Y - C0, prm.F0 - 1)):
        W = None
        for w in range(1, n - 1):
            r = (acc & ((1 << (w + 1)) - 1)) - (1 << w) + d
            if r.min() >= -(1 << (w + 1)) and r.max() < (1 << (w + 1)):
                W = w
                break
        out[name] = W
    return out

