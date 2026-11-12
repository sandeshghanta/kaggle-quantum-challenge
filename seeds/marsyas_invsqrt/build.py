"""Generator for our invsqrt circuit (see model.py for the arithmetic it implements)."""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

import numpy as np
import kickmix as km

import model as M


@dataclass
class Opts:
    WA: int | None = None  # step A window top (bits of T below it are the window)
    WB: int | None = None
    gA: int = 2            # guard qubits
    gB: int = 2
    streamA: bool = False  # streamed product temps (recompute) instead of cached
    streamB: bool = False
    cap: int | None = None
    xext: bool = False
    fuse: bool = False
    px21: bool = False
    splitA: int | None = None
    splitB: int | None = None
    angles: list | None = None   # per shift-register position, units of 2^-64 turns


class B:
    def __init__(self):
        self.b = km.CircuitBuilder()
        self.peak = (0, '')
        self.label = ''
        self.garbage = {}

    def note(self, where=''):
        n = self.b.num_allocated_qubits
        if n > self.peak[0]:
            self.peak = (n, self.label + where)

    def mk(self, s, q):
        b = self.b
        if s[0] == 'and':
            b.init_and(s[1], s[2], q)
        else:  # ('sel', one, a, two, bb): q = one&a ^ two&bb
            _, one, a, two, bb = s
            b.reset(q)
            if a is not None:
                b.ccx(one, a, q)
            if bb is not None:
                b.ccx(two, bb, q)

    def unmk(self, s, q):
        b = self.b
        if s[0] == 'and':
            b.del_and(s[1], s[2], q)
            return
        _, one, a, two, bb = s
        m = b.alloc_bits(1)[0]
        b.hmr(q, m)
        b.push_condition(m)
        if a is not None:
            b.cz(one, a)
        if bb is not None:
            b.cz(two, bb)
        b.pop_condition()
        b.free(km.array([m]))

    # ---------------- adder -----------------
    cap = None   # qubit cap used to decide how many product temps to cache
    phasefix = True

    def add_ip(self, srcs, tgt, cin, k):
        """tgt += srcs + cin with a k-position Cuccaro (in-place MAJ) prefix: saves k carry
        ancillas (k-1 if cin is None) for +k Toffolis. srcs[0..k-1] must be plain qubits."""
        b = self.b
        n = len(tgt)
        srcs = list(srcs) + [None] * (n - len(srcs))
        assert all(isinstance(s_, km.q) for s_ in srcs[:k]) and k < n - 1
        own = cin is None
        c0 = b.alloc_qubits(1)[0] if own else cin
        cs = [c0] + list(srcs[:k])
        for i in range(k):
            c, bb, a = cs[i], tgt[i], srcs[i]
            b.cx(a, bb); b.cx(a, c); b.ccx(c, bb, a)
        self.add(srcs[k:], list(tgt[k:]), cin=srcs[k - 1])
        for i in reversed(range(k)):
            c, bb, a = cs[i], tgt[i], srcs[i]
            b.ccx(c, bb, a); b.cx(a, c); b.cx(c, bb)
        if own:
            b.free(km.array([c0]))

    def add(self, srcs, tgt, subtract=False, stream=False, cin=None, cout=None):
        """tgt += sum_i src_i 2^i + cin  (mod 2^len(tgt)); if cout is given, the carry out of
        the top position is XORed into cout.  srcs[i] is None (zero), a qubit, or
        ('and', c, q) / ('sel', one, a, two, b) computed temps.  Repeated qubits allowed."""
        b = self.b
        n = len(tgt)
        srcs = list(srcs) + [None] * (n - len(srcs))
        assert len(srcs) == n
        lo = 0
        if cin is None:
            while lo < n and srcs[lo] is None:
                lo += 1
        if lo == n:
            return
        srcs = srcs[lo:]
        tgt = list(tgt[lo:])
        n = len(tgt)
        if subtract:
            b.x(km.array(tgt))
        comp = lambda i: srcs[i] is not None and not isinstance(srcs[i], km.q)
        K = n - 1 if cout is not None else n - 2   # internal carries c_1..c_K
        K = max(K, 0)
        rev_used = [i for i in range(K) if comp(i)]
        if stream:
            ncache = 0
        elif self.cap is None:
            ncache = len(rev_used)
        else:
            ncache = max(0, min(len(rev_used), self.cap - b.num_allocated_qubits - K - 1))
        order = sorted(rev_used, key=lambda i: 0 if srcs[i][0] == 'sel' else 1)
        cache_set = set(order[:ncache])
        live = {}

        def get(i):
            s_ = srcs[i]
            if s_ is None or isinstance(s_, km.q):
                return s_
            if i not in live:
                q = b.alloc_qubits(1)[0]
                self.mk(s_, q)
                live[i] = q
            return live[i]

        def drop(i):
            if i in live:
                q = live.pop(i)
                self.unmk(srcs[i], q)
                b.free(q)

        carries = b.alloc_qubits(K) if K else []

        def cin_of(i):
            if i == 0:
                return cin
            return carries[i - 1] if i - 1 < K else None

        for i in range(n):
            a = get(i)
            bb = tgt[i]
            c = cin_of(i)
            if i < K:
                dest, init = carries[i], True
            elif cout is None and i == n - 2:
                dest, init = tgt[n - 1], False
            elif cout is not None and i == n - 1:
                dest, init = cout, False
            else:
                dest = None   # top position without carry out: only the sum
            if dest is None:
                if a is not None:
                    b.cx(a, bb)
                if c is not None and (cout is not None or n == 1):
                    b.cx(c, bb)
                self.note(f' add n={n} cached={len(live)}')
                drop(i)
                continue
            if a is None:
                # MAJ(0,b,c) = b&c ; sum = b^c
                if init:
                    b.init_and(bb, c, dest)
                else:
                    b.ccx(bb, c, dest)
                b.cx(c, bb)
            else:
                if c is not None:
                    b.cx(c, a); b.cx(c, bb)
                if init:
                    b.init_and(a, bb, dest)
                else:
                    b.ccx(a, bb, dest)
                if c is not None:
                    b.cx(c, dest)
                if c is not None:
                    b.cx(c, a)
                b.cx(a, bb)
            self.note(f' add n={n} cached={len(live)}')
            if i not in cache_set:
                drop(i)
        for i in reversed(range(K)):
            bb = tgt[i]
            c = cin_of(i)
            if comp(i) and i not in cache_set and self.phasefix:
                s_ = srcs[i]
                if c is not None:
                    b.cx(c, carries[i])
                m = b.alloc_bits(1)[0]
                b.hmr(carries[i], m)
                b.push_condition(m)
                if c is not None:
                    b.cz(c, bb)
                    b.cx(c, bb)
                b.x(bb)
                if s_[0] == 'and':
                    b.ccz(s_[1], s_[2], bb)
                else:
                    _, one, qa, two, qb = s_
                    if qa is not None:
                        b.ccz(one, qa, bb)
                    if qb is not None:
                        b.ccz(two, qb, bb)
                b.x(bb)
                if c is not None:
                    b.cx(c, bb)
                b.pop_condition()
                b.free(km.array([m]))
                continue
            a = get(i)
            if a is None:
                b.cx(c, bb)
                b.del_and(bb, c, carries[i])
                b.cx(c, bb)
            else:
                if c is not None:
                    b.cx(c, carries[i])
                b.cx(a, bb)
                if c is not None:
                    b.cx(c, a)
                b.del_and(a, bb, carries[i])
                if c is not None:
                    b.cx(c, a)
                b.cx(a, bb)
            drop(i)
        if carries:
            b.free(carries)
        assert not live
        if subtract:
            b.x(km.array(tgt))


def row_sources(ctrl, opreg, e, width):
    """Sources for adding ctrl * (opreg >> e) into a register of `width` bits (LSB aligned)."""
    srcs = [None] * width
    for k in range(len(opreg)):
        pos = k - e
        if 0 <= pos < width:
            srcs[pos] = ('and', ctrl, opreg[k])
        elif pos >= width:
            raise ValueError('operand exceeds window')
    return srcs


def horner_step(bb: B, x, rows, op, acc, e_of, W, guard, subtract_dir, stream, two=None, **kw):
    """acc -= sum_{i<13} b_i (op>>e_i) ; acc += (1-b13)(op>>e_13); window [0,W) + guard.
    subtract_dir=False runs the exact inverse."""
    b = bb.b
    win = list(acc[:W]) + list(guard)
    seq = [i for i in rows]
    ops = []
    for i in seq:
        e = e_of(i)
        srcs = row_sources(x[i], op, e, len(win))
        if all(s is None for s in srcs):
            continue
        ops.append((i, srcs))
    high = list(acc[W:])
    gsrc = [guard[0]] + [guard[1]] * (len(high) - 1) if len(guard) == 2 else None
    if subtract_dir:
        for i, srcs in ops:
            if i == 13:
                b.x(x[13]); bb.add(srcs, win, subtract=False, stream=stream); b.x(x[13])
            else:
                bb.add(srcs, win, subtract=True, stream=stream)
        if high:
            bb.add(gsrc, high, subtract=False)
    else:
        if high:
            bb.add(gsrc, high, subtract=True)
        for i, srcs in reversed(ops):
            if i == 13:
                b.x(x[13]); bb.add(srcs, win, subtract=True, stream=stream); b.x(x[13])
            else:
                bb.add(srcs, win, subtract=False, stream=stream)


def booth_step(bb: B, x, rows, op, acc, e_of, W, guard, fwd, stream, two=None, split=None, gkey=None):
    """fwd: acc -= sum_j d_j (|d_j| op >> e) ; else the exact inverse. x[13] must hold c13."""
    b = bb.b
    win = list(acc[:W]) + list(guard) if len(guard) != 1 else list(acc[:W + 1]) + list(guard)
    high = list(acc[W:])
    gsrc = [guard[0]] + [guard[1]] * (len(high) - 1) if len(guard) == 2 else None
    own_two = two is None
    if own_two:
        two = b.alloc_qubits(1)[0]

    def flags(j, on):
        hi, mid = x[2 * j + 1], x[2 * j]
        lo = x[2 * j - 1] if j else None
        # two = (hi ^ mid) & (mid == lo)
        b.cx(mid, hi)
        if lo is not None:
            b.cx(lo, mid)
        b.x(mid)
        (b.init_and if on else b.del_and)(hi, mid, two)
        b.x(mid)
        if lo is not None:
            b.cx(lo, mid)
        b.cx(mid, hi)
        # leave mid holding one = mid ^ lo while the row runs
        if lo is not None:
            b.cx(lo, mid)

    def unflags(j):
        lo = x[2 * j - 1] if j else None
        if lo is not None:
            b.cx(lo, x[2 * j])
        flags_del(j)

    def flags_del(j):
        hi, mid = x[2 * j + 1], x[2 * j]
        lo = x[2 * j - 1] if j else None
        b.cx(mid, hi)
        if lo is not None:
            b.cx(lo, mid)
        b.x(mid)
        b.del_and(hi, mid, two)
        b.x(mid)
        if lo is not None:
            b.cx(lo, mid)
        b.cx(mid, hi)

    def row(j, sub):
        e = e_of(2 * j)
        one = x[2 * j]
        neg = x[2 * j + 1]
        srcs = []
        for pos in range(len(win)):
            ka, kb = pos + e, pos + e - 1
            qa = op[ka] if 0 <= ka < len(op) else None
            qb = op[kb] if 0 <= kb < len(op) else None
            if ka >= len(op) + 1:
                pass
            srcs.append(None if qa is None and qb is None else ('sel', one, qa, two, qb))
        assert all(0 > k or k < len(op) for k in [len(win) - 1 + e - 1]) or True
        top_needed = len(op) - e + 1  # highest operand position + 1
        assert top_needed <= len(win), ('window too small', top_needed, len(win))
        if all(s is None for s in srcs):
            return
        if split is not None:
            # exact functional inverse pair; mask = complement when neg == 0 in both directions
            L = split
            b.x(neg)
            for q in win:
                b.cx(neg, q)
            b.x(neg)
            lo_w, hi_w = win[:L], win[L:]
            lo_s, hi_s = srcs[:L], srcs[L:]
            if fwd:
                g = b.alloc_qubits(1)[0]
                bb.add(lo_s, lo_w, stream=stream, cout=g)
                bb.add(hi_s, hi_w, stream=stream, cin=g)
                bb.garbage.setdefault(gkey, []).append(g)
            else:
                g = bb.garbage[gkey].pop()
                bb.add(hi_s, hi_w, subtract=True, stream=stream, cin=g)
                bb.add(lo_s, lo_w, subtract=True, stream=stream, cout=g)
                b.free(km.array([g]))
            b.x(neg)
            for q in win:
                b.cx(neg, q)
            b.x(neg)
            return
        # sub: acc -= d*v  => complement when neg == 0 ; else acc += d*v => complement when neg == 1
        if sub:
            b.x(neg)
        for q in win:
            b.cx(neg, q)
        bb.add(srcs, win, subtract=False, stream=stream)
        for q in win:
            b.cx(neg, q)
        if sub:
            b.x(neg)

    def nonempty(j):
        e = e_of(2 * j)
        return any((0 <= p + e < len(op)) or (0 <= p + e - 1 < len(op)) for p in range(len(win)))

    order = [j for j in (list(rows) if fwd else list(reversed(rows))) if nonempty(j)]
    if len(guard) == 1:
        # offset trick: window = acc[:W+1] read as (u - 2^W) in two's complement, 1 sign guard
        top = acc[W]
        hi1 = list(acc[W + 1:])
        g = guard[0]
        if fwd:
            b.x(top); b.cx(top, g)
            for j in order:
                flags(j, True); row(j, fwd); unflags(j)
            if hi1:
                bb.add([top], hi1)
                bb.add([g], hi1, subtract=True)
            b.x(top)
        else:
            b.x(top)
            if hi1:
                bb.add([g], hi1)
                bb.add([top], hi1, subtract=True)
            for j in order:
                flags(j, True); row(j, fwd); unflags(j)
            b.cx(top, g); b.x(top)
        if own_two:
            b.free(km.array([two]))
        return
    if not fwd and high:
        bb.add(gsrc, high, subtract=True)
    for j in order:
        flags(j, True)
        row(j, fwd)
        unflags(j)
    if fwd and high:
        bb.add(gsrc, high, subtract=False)
    if own_two:
        b.free(km.array([two]))


def sd_step(bb: B, x, rows, op, acc, e_of, W, guard, fwd, stream, two=None, split=None, gkey=None):
    """Signed-digit rows: fwd: acc -= sum_i (2b_i-1) rd(op, e_i+1); else the exact inverse."""
    b = bb.b
    g1 = len(guard) == 1
    win = (list(acc[:W + 1]) if g1 else list(acc[:W])) + list(guard)
    high = list(acc[W + 1:]) if g1 else list(acc[W:])
    gsrc = [guard[0]] + [guard[1]] * (len(high) - 1) if len(guard) == 2 else None

    def row_spec(i):
        e = e_of(i) + 1
        srcs = [op[p + e] if 0 <= p + e < len(op) else None for p in range(len(win))]
        assert len(op) - e <= len(win), ('window too small', len(op) - e, len(win))
        cin = op[e - 1] if 1 <= e <= len(op) else None
        return srcs, cin

    def row(i):
        srcs, cin = row_spec(i)
        if all(q is None for q in srcs) and cin is None:
            return
        ctl = x[i]
        if not fwd:
            b.x(ctl)
        for q in win:
            b.cx(ctl, q)
        if split is not None:
            L = split
            lo_w, hi_w = win[:L], win[L:]
            lo_s, hi_s = srcs[:L], srcs[L:]
            if fwd:
                g = b.alloc_qubits(1)[0]
                bb.add(lo_s, lo_w, cin=cin, cout=g)
                bb.add(hi_s, hi_w, cin=g)
                bb.garbage.setdefault(gkey, []).append(g)
            else:
                # exact inverse of the forward pair, in the forward's complement frame
                pass
        else:
            bb.add(srcs, win, cin=cin)
        for q in win:
            b.cx(ctl, q)
        if not fwd:
            b.x(ctl)

    def row_inv_split(i):
        srcs, cin = row_spec(i)
        if all(q is None for q in srcs) and cin is None:
            return
        ctl = x[i]
        for q in win:
            b.cx(ctl, q)
        L = split
        lo_w, hi_w = win[:L], win[L:]
        lo_s, hi_s = srcs[:L], srcs[L:]
        g = bb.garbage[gkey].pop()
        bb.add(hi_s, hi_w, subtract=True, cin=g)
        # low: (low - v - cin) with borrow-out into g:  ~(~low + v + cin), carry-out = borrow
        bb.add(lo_s, lo_w, subtract=True, cin=cin, cout=g)
        b.free(km.array([g]))
        for q in win:
            b.cx(ctl, q)

    order = list(rows) if fwd else list(reversed(rows))

    def run_rows():
        for i in order:
            if split is not None and not fwd:
                row_inv_split(i)
            else:
                row(i)

    if g1:
        top = acc[W]
        g = guard[0]
        if fwd:
            b.x(top); b.cx(top, g)
            run_rows()
            if high:
                bb.add([top], high)
                bb.add([g], high, subtract=True)
            b.x(top)
        else:
            b.x(top)
            if high:
                bb.add([g], high)
                bb.add([top], high, subtract=True)
            run_rows()
            b.cx(top, g); b.x(top)
        return
    if not fwd and high:
        bb.add(gsrc, high, subtract=True)
    run_rows()
    if fwd and high:
        bb.add(gsrc, high, subtract=False)


def fused_phase(bb: B, R, h0, angfn, flags4):
    """Phase sum_j ang(j) * bit_j(R << s), s = f2 + 2 f4 + 4 f8 + 8 f16 (s <= 10).
    R: data qubits R[0..h0] followed by 3 clean ext qubits. f2, f4 in place (CSWAP);
    f8/f16 fused: Z_j = R_j ^ f8 (R_j ^ R_{j-4}) ^ f16 (R_j ^ R_{j-8}) into one temp,
    Z_POW, then HMR with Clifford fix-up.  angfn(j) -> Fraction half-turns or None."""
    b = bb.b
    f2, f4, f8, f16 = flags4
    assert len(R) == h0 + 4
    hiR = h0 + 3
    shifts = [(1, f2, h0), (2, f4, h0 + 1)]
    # targets above every earlier write are known-zero: move with an AND (uncomputed for free)
    empty = {(1, h0), (2, h0 + 1), (2, h0)}
    for k, f, hi in shifts:
        for i in range(hi, -1, -1):
            if (k, i) in empty:
                b.init_and(f, R[i], R[i + k]); b.cx(R[i + k], R[i])
            else:
                b.cswap(f, R[i], R[i + k])
    z = b.alloc_qubits(1)[0]
    bb.note(' fused phase')

    def term(f, j, k, act):
        qa = R[j] if j <= hiR else None
        qb = R[j - k] if 0 <= j - k <= (hiR if k == 4 else h0 + 2) else None
        if qa is None and qb is None:
            return False
        if qa is not None and qb is not None:
            b.cx(qb, qa); act(f, qa); b.cx(qb, qa)
        else:
            act(f, qa if qa is not None else qb)
        return True

    for j in range(h0 + 11):
        ex = angfn(j)
        if ex is None or ex % 2 == 0:
            continue
        b.reset(z)
        has_r = j <= hiR
        if has_r:
            b.cx(R[j], z)
        t8 = term(f8, j, 4, lambda f, q: b.ccx(f, q, z))
        t16 = term(f16, j, 8, lambda f, q: b.ccx(f, q, z))
        if not (has_r or t8 or t16):
            continue
        b.z_pow(z, ex)
        m = b.alloc_bits(1)[0]
        b.hmr(z, m)
        b.push_condition(m)
        if has_r:
            b.z(R[j])
        term(f8, j, 4, lambda f, q: b.cz(f, q))
        term(f16, j, 8, lambda f, q: b.cz(f, q))
        b.pop_condition()
        b.free(km.array([m]))
    b.free(km.array([z]))
    for k, f, hi in reversed(shifts):
        for i in range(0, hi + 1):
            if (k, i) in empty:
                b.cx(R[i + k], R[i]); b.del_and(f, R[i], R[i + k])
            else:
                b.cswap(f, R[i], R[i + k])


def build(tables, prm: M.Params, o: Opts):
    bb = B()
    bb.cap = o.cap
    b = bb.b
    x = b.create_quantum_register(22, name='x')
    flags = list(b.alloc_qubits(5))  # f1 f2 f4 f8 f16
    f1, f2, f4, f8, f16 = flags
    F = {'f1': f1}

    def thermo_compute():
        f1 = F['f1']
        th = b.alloc_qubits(21)
        b.x(x[1:22])
        b.cx(x[21], th[0])
        for k in range(1, 21):
            b.init_and(th[k - 1], x[21 - k], th[k])
        for k in range(1, 22):
            q = th[k - 1]
            b.cx(q, f1)
            if k % 2 == 0: b.cx(q, f2)
            if k % 4 == 0: b.cx(q, f4)
            if k % 8 == 0: b.cx(q, f8)
            if k == 16: b.cx(q, f16)
        for k in range(20, 0, -1):
            b.del_and(th[k - 1], x[21 - k], th[k])
        b.cx(x[21], th[0])
        b.x(x[1:22])
        b.free(th)

    def norm(fwd):
        stages = [(16, f16), (8, f8), (4, f4), (2, f2), (1, F['f1'])]
        for k, f in (stages if fwd else reversed(stages)):
            idx = range(21 - k, -1, -1) if fwd else range(0, 22 - k)
            for i in idx:
                b.cswap(f, x[i], x[i + k])

    thermo_compute()
    norm(True)

    nY = prm.F0 - 1
    nT = prm.nT
    n2 = prm.F2 - 1
    Yr = b.alloc_qubits(nY)
    Tr = b.alloc_qubits(nT)
    C2r = b.alloc_qubits(n2)
    target = km.array(list(Yr) + list(Tr) + list(C2r))
    packed = [int(tables[a, 0]) | (int(tables[a, 1]) << nY) | (int(tables[a, 2]) << (nY + nT))
              for a in range(256)]
    if o.px21:
        # x_n[21] == 1 after normalization: store the parity flag there and free f1
        b.cx(f1, x[21]); b.x(x[21]); b.cx(x[21], f1)
        b.free(km.array([f1]))
        address = km.array(list(x[14:21]) + [x[21]])
    else:
        address = km.array(list(x[14:21]) + [f1])
    b.init_lookup(table=packed, address=address, target=target)

    WA = o.WA if o.WA is not None else nT
    WB = o.WB if o.WB is not None else nY
    step = sd_step if prm.sd else (booth_step if prm.booth else horner_step)
    b.x(x[21])  # x_n has bit 21 set: now a clean zero usable as the Booth 'two' temp
    if prm.booth:
        b.x(x[13])
    gA = b.alloc_qubits(o.gA) if WA < nT else []
    bb.label = 'A'
    step(bb, x, prm.rowsA, C2r, Tr, prm.eA, WA, gA, True, o.streamA, x[21], split=o.splitA, gkey='A')
    gB = b.alloc_qubits(o.gB) if WB < nY else []
    bb.label = 'B'
    step(bb, x, prm.rowsB, Tr, Yr, prm.eB, WB, gB, True, o.streamB, x[21], split=o.splitB, gkey='B')
    bb.label = 'shift'
    if prm.booth:
        b.x(x[13])
    b.x(x[21])  # back to 1: the constant 1/2 bit of the shift register
    if o.px21:
        cq = b.alloc_qubits(1)[0]
        b.x(cq)
    else:
        cq = x[21]

    R0 = prm.F0 - prm.G0
    def ang_ex(j):
        if o.angles is not None:
            a = int(o.angles[j]) % (1 << 64)
            return None if a == 0 else Fraction(2 * a, 1 << 64)
        return Fraction(2) ** (j - R0 - 9)

    if o.fuse:
        # stages f2, f4 in place; the exclusive f8/f16 stages are fused into the phasing:
        # Z_j = R_j ^ f8 (R_j ^ R_{j-4}) ^ f16 (R_j ^ R_{j-8}) in one temp, Z_POW, HMR + Clifford fixup
        ext = b.alloc_qubits(3)
        R = list(Yr[prm.G0:]) + [cq] + list(ext)
        hiR = len(R) - 1
        top0 = R0 - 1
        shifts = [(1, f2, top0), (2, f4, top0 + 1)]
        for k, f, hi in shifts:
            for i in range(hi, -1, -1):
                b.cswap(f, R[i], R[i + k])
        z = b.alloc_qubits(1)[0]
        bb.note(' fused phase')

        def term(f, j, k, act):
            # act(f, q): apply to the XOR of R[j] (if any) and R[j-k] (if any)
            qa = R[j] if j <= hiR else None
            qb = R[j - k] if 0 <= j - k <= (hiR if k == 4 else min(hiR, R0 + 1)) else None
            if qa is None and qb is None:
                return False
            if qa is not None and qb is not None:
                b.cx(qb, qa); act(f, qa); b.cx(qb, qa)
            else:
                act(f, qa if qa is not None else qb)
            return True

        for j in range(R0 + 10):
            ex = ang_ex(j)
            if ex is None or ex % 2 == 0:
                continue
            b.reset(z)
            has_r = j <= hiR
            if has_r:
                b.cx(R[j], z)
            t8 = term(f8, j, 4, lambda f, q: b.ccx(f, q, z))
            t16 = term(f16, j, 8, lambda f, q: b.ccx(f, q, z))
            if not (has_r or t8 or t16):
                continue
            b.z_pow(z, ex)
            m = b.alloc_bits(1)[0]
            b.hmr(z, m)
            b.push_condition(m)
            if has_r:
                b.z(R[j])
            term(f8, j, 4, lambda f, q: b.cz(f, q))
            term(f16, j, 8, lambda f, q: b.cz(f, q))
            b.pop_condition()
            b.free(km.array([m]))
        b.free(km.array([z]))
        for k, f, hi in reversed(shifts):
            for i in range(0, hi + 1):
                b.cswap(f, R[i], R[i + k])
        b.free(ext)
        b.x(x[21])
    elif o.xext:
        # the top 3 shift positions are only reached when f16 = 1 (x < 64), where x_n[0:16] = 0:
        # borrow x_n[0..2]; pre-cancel the T/S/Z they receive when they stay in place.
        ext = b.alloc_qubits(7)
        R = list(Yr[prm.G0:]) + [cq] + list(ext) + [x[0], x[1], x[2]]
        b.z_pow(x[0], Fraction(-1, 4)); b.z_pow(x[1], Fraction(-1, 2)); b.z_pow(x[2], Fraction(-1))
    else:
        ext = b.alloc_qubits(10)
        R = list(Yr[prm.G0:]) + [cq] + list(ext)
    if not o.fuse:
      bb.note(' shiftreg')
      top0 = R0 - 1  # index of the 'one' qubit
      # stages: f2 (1), f4 (2), f8 (4), f16 (8); hi = highest possibly nonzero source index
      shifts = [(1, f2, top0), (2, f4, top0 + 1), (4, f8, top0 + 3), (8, f16, top0 + 2)]
      for k, f, hi in shifts:
          for i in range(hi, -1, -1):
              b.cswap(f, R[i], R[i + k])
      for j in range(len(R)):
          if o.angles is not None:
              a = int(o.angles[j]) % (1 << 64)
              if a == 0:
                  continue
              ex = Fraction(2 * a, 1 << 64)
          else:
              ex = Fraction(2) ** (j - R0 - 9)
          if ex % 2 != 0:
              b.z_pow(R[j], ex)
      for k, f, hi in reversed(shifts):
          for i in range(0, hi + 1):
              b.cswap(f, R[i], R[i + k])
      b.free(ext)
      b.x(x[21])

    if o.px21:
        b.x(cq); b.free(km.array([cq]))
    if prm.booth:
        b.x(x[13])
    step(bb, x, prm.rowsB, Tr, Yr, prm.eB, WB, gB, False, o.streamB, x[21], split=o.splitB, gkey='B')
    if gB: b.free(gB)
    step(bb, x, prm.rowsA, C2r, Tr, prm.eA, WA, gA, False, o.streamA, x[21], split=o.splitA, gkey='A')
    if gA: b.free(gA)
    if prm.booth:
        b.x(x[13])
    b.x(x[21])
    b.del_lookup(table=packed, address=address, target=target)
    b.free(target)
    if o.px21:
        nf1 = b.alloc_qubits(1)[0]
        b.cx(x[21], nf1); b.x(x[21]); b.cx(nf1, x[21])
        F['f1'] = nf1
    norm(False)
    thermo_compute()
    b.free(km.array([F['f1'], f2, f4, f8, f16]))
    global LAST_PEAK
    LAST_PEAK = bb.peak
    return b.finish_circuit()


LAST_PEAK = None

