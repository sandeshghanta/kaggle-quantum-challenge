"""High/low split of the output (idea from anazemcev's public notebook, reimplemented):
Y' = H 2^S + L with H a pure table word and L = u + D (D = the Horner correction) kept in
[0, 2^LB). H is phased right after the lookup and freed by measurement-unlookup, so step B
accumulates into L only (no guard, no fold). Requires sd mode.
"""
from __future__ import annotations

import json
import sys
from fractions import Fraction

import numpy as np
from scipy.optimize import linprog
import kickmix as km

import model as M
import fit as Fm
import build as Bd
import calib


class Layout:
    pass


def layout(tables, prm, LB, S):
    if Fm._D is None:
        Fm._D = Fm.domains()
    a, p, xn, bounds = Fm._D
    seg = a
    b = xn & 0x3FFF
    C0, C1, C2 = tables[seg, 0], tables[seg, 1], tables[seg, 2]
    T = M.eval_T(C1, C2, b, prm)
    D = M.eval_Y(C0, T, b, prm) - C0
    dmin = np.minimum.reduceat(D, bounds[:-1])
    dmax = np.maximum.reduceat(D, bounds[:-1])
    u = np.zeros(256, np.int64); H = np.zeros(256, np.int64)
    for k in range(256):
        c0 = int(tables[k, 0])
        base = c0 % (1 << S)
        j = 0
        while base + j * (1 << S) + dmin[k] < 0:
            j += 1
        uk = base + j * (1 << S)
        if uk + dmax[k] >= (1 << LB) or c0 - uk < 0:
            return None
        u[k] = uk; H[k] = (c0 - uk) >> S
    lay = Layout()
    lay.LB, lay.S, lay.u, lay.H = LB, S, u, H
    lay.nHf = prm.F0 - 1 - S          # H register span up to the const bit
    lay.nH = max(int(H.max()).bit_length(), 1)
    assert lay.nH <= lay.nHf
    return lay


def find_layout(tables, prm):
    for LB in range(10, prm.F0):
        for S in range(LB, max(prm.G0, 1) - 1, -1):
            lay = layout(tables, prm, LB, S)
            if lay is not None:
                return lay
    return None


def regs(tables, prm, lay, xs):
    """[(absolute shifted value, offset)] for the H and L phase registers."""
    t, xn, p, s = M.decompose(xs)
    a = (p << 7) | ((xn >> 14) & 127)
    Y = M.model_Yp(xn, p, tables, prm, check=False)
    L = lay.u[a] + (Y - tables[a, 0])
    VH = (lay.H[a] + (1 << lay.nHf)) << s
    VL = (L >> prm.G0) << s
    offH = lay.S - prm.G0
    return [(VH << offH, offH, lay.nHf), (VL, 0, lay.LB - prm.G0 - 1)]


def phases_multi(R, angs):
    acc = np.zeros(len(R[0][0]), dtype=np.uint64)
    for (V, off, h0), ang in zip(R, angs):
        acc += calib.phases(V, ang)
    return acc


def exact(prm):
    R0 = prm.F0 - prm.G0
    return [int(1 << (64 + j - R0 - 10)) for j in range(R0 + 10)]


def spans(prm, R):
    R0 = prm.F0 - prm.G0
    out = []
    for V, off, h0 in R:
        out.append([pp for pp in range(off, off + h0 + 11) if pp <= R0 + 6])
    return out


def check_multi(tables, prm, lay, angs):
    target, tol = M.reference()
    xs = np.arange(1, M.MAX_X + 1, dtype=np.int64)
    R = regs(tables, prm, lay, xs)
    ph = phases_multi(R, angs)
    err = (ph - target).view(np.int64)
    used = np.abs(err).astype(np.float64)
    return float((used / tol).max()), float((tol.astype(np.float64) - used).min()), err, tol, R


def calibrate_multi(tables, prm, lay, drop=1, iters=12, verbose=False):
    R0 = prm.F0 - prm.G0
    ex = exact(prm)
    xs = np.arange(1, M.MAX_X + 1, dtype=np.int64)
    R = regs(tables, prm, lay, xs)
    sp = spans(prm, R)
    angs = [list(ex) for _ in R]
    # zero positions outside each register's reachable span (no qubit there)
    var = []
    for r, (V, off, h0) in enumerate(R):
        for pp in range(R0 + 10):
            if not (off <= pp <= off + h0 + 10):
                angs[r][pp] = 0
        pos = sp[r]
        if r == 1:   # drop lowest L positions
            for pp in pos[:drop]:
                angs[r][pp] = 0
            pos = pos[drop:]
        var += [(r, pp) for pp in pos]
    worst, margin, err, tol, _ = check_multi(tables, prm, lay, angs)
    if verbose:
        print('start', worst, margin, flush=True)
    bits = np.stack([((R[r][0] >> pp) & 1).astype(np.float32) for r, pp in var], axis=1)
    tolf = tol.astype(np.float64); errf = err.astype(np.float64)
    act = np.argsort(tolf - np.abs(errf))[:20000]
    best = None
    S_ = 2.0 ** 20
    for it in range(iters):
        A = bits[act].astype(np.float64); e = errf[act] / S_; tl = tolf[act] / S_
        n = len(var)
        c = np.zeros(n + 1); c[-1] = -1
        one = np.ones_like(tl)
        Aub = np.r_[np.c_[A, one], np.c_[-A, one]]
        bub = np.r_[tl - e, tl + e]
        res = linprog(c, A_ub=Aub, b_ub=bub, bounds=[(-2**20, 2**20)] * n + [(None, None)], method='highs')
        if res.x is None:
            break
        d = np.round(res.x[:n] * S_).astype(np.int64)
        tot = errf + bits @ d.astype(np.float64)
        slack = tolf - np.abs(tot)
        w = 2.0 if slack.min() <= 0 else 1.0 / slack.min()
        if verbose:
            print(f'it {it} minslack {slack.min():.4g}', flush=True)
        if best is None or w < best[0]:
            best = (w, d.copy())
        act2 = np.union1d(act, np.argsort(slack)[:5000])
        if len(act2) == len(act):
            break
        act = act2
    if best is None:
        return None
    d = best[1]
    for k, (r, pp) in enumerate(var):
        angs[r][pp] = int(angs[r][pp]) + int(d[k])
    worst, margin, _, _, _ = check_multi(tables, prm, lay, angs)
    nrot = len(var)
    return angs, worst, margin, nrot


def mdel(b, address, groups):
    """Measurement-based unlookup fix-up for several already-measured registers sharing one
    8-bit address: applies prod_g (-1)^{m_g . table_g[addr]} with a 16-way one-hot of the low
    nibble (14 ANDs, freed by measurement) and a unary iteration over the high nibble."""
    lo, hi = list(address[:4]), list(address[4:8])
    oh = [None] * 16
    q0 = b.alloc_qubits(1)[0]; q1 = b.alloc_qubits(1)[0]
    b.x(q0); b.cx(lo[0], q1); b.cx(q1, q0)
    oh[0], oh[1] = q0, q1
    made = []
    for j in range(1, 4):
        for e in range(1 << j):
            n = b.alloc_qubits(1)[0]
            b.init_and(oh[e], lo[j], n)
            b.cx(n, oh[e])
            oh[e + (1 << j)] = n
            made.append((j, e, n))
    with b.unary_iteration(km.array(hi)) as it:
        for v, mu in it:
            for l in range(16):
                a = v * 16 + l
                for bits, tab in groups:
                    val = int(tab[a])
                    k = 0
                    while val:
                        if val & 1:
                            b.push_condition(bits[k])
                            b.cz(mu, oh[l])
                            b.pop_condition()
                        val >>= 1; k += 1
    for j, e, n in reversed(made):
        b.cx(n, oh[e])
        b.del_and(oh[e], lo[j], n)
        b.free(km.array([n]))
    b.cx(q1, q0); b.cx(lo[0], q1); b.x(q0)
    b.free(km.array([q0, q1]))


def measure(b, qs):
    bits = b.alloc_bits(len(qs))
    b.hmr(km.array(list(qs)), bits)
    return bits


def build_hsplit(tables, prm, o, lay, angs):
    """sd-mode circuit with the H/L output split (px21 + fused phasing always on)."""
    bb = Bd.B()
    bb.cap = o.cap
    b = bb.b
    x = b.create_quantum_register(22, name='x')
    f1, f2, f4, f8, f16 = list(b.alloc_qubits(5))
    F = {'f1': f1}

    def thermo_compute():
        f1_ = F['f1']
        th = b.alloc_qubits(21)
        b.x(x[1:22]); b.cx(x[21], th[0])
        for k in range(1, 21):
            b.init_and(th[k - 1], x[21 - k], th[k])
        for k in range(1, 22):
            q = th[k - 1]
            b.cx(q, f1_)
            if k % 2 == 0: b.cx(q, f2)
            if k % 4 == 0: b.cx(q, f4)
            if k % 8 == 0: b.cx(q, f8)
            if k == 16: b.cx(q, f16)
        for k in range(20, 0, -1):
            b.del_and(th[k - 1], x[21 - k], th[k])
        b.cx(x[21], th[0]); b.x(x[1:22])
        b.free(th)

    def norm(fwd):
        stages = [(16, f16), (8, f8), (4, f4), (2, f2), (1, F['f1'])]
        for k, f in (stages if fwd else reversed(stages)):
            idx = range(21 - k, -1, -1) if fwd else range(0, 22 - k)
            for i in idx:
                b.cswap(f, x[i], x[i + k])

    thermo_compute()
    norm(True)
    b.cx(f1, x[21]); b.x(x[21]); b.cx(x[21], f1)
    b.free(km.array([f1]))
    address = km.array(list(x[14:21]) + [x[21]])

    nT, n2, LB, nH = prm.nT, prm.F2 - 1, lay.LB, lay.nH
    Lr = b.alloc_qubits(LB); Hr = b.alloc_qubits(nH); Tr = b.alloc_qubits(nT); C2r = b.alloc_qubits(n2)
    target = km.array(list(Lr) + list(Hr) + list(Tr) + list(C2r))
    packed = [int(lay.u[a]) | (int(lay.H[a]) << LB) | (int(tables[a, 1]) << (LB + nH)) | (int(tables[a, 2]) << (LB + nH + nT))
              for a in range(256)]
    b.init_lookup(table=packed, address=address, target=target)
    bb.note(' lookup')
    flags4 = (f2, f4, f8, f16)

    def angfn(ang, off):
        def g(j):
            a = int(ang[off + j]) % (1 << 64) if off + j < len(ang) else 0
            return None if a == 0 else Fraction(2 * a, 1 << 64)
        return g

    # --- phase H (+ the constant 1/2 bit) right away, then free H by measurement-unlookup
    pad = b.alloc_qubits(lay.nHf - nH) if lay.nHf > nH else []
    cq = b.alloc_qubits(1)[0]; b.x(cq)
    ext = b.alloc_qubits(3)
    RH = list(Hr) + list(pad) + [cq] + list(ext)
    bb.label = 'Hphase'
    Bd.fused_phase(bb, RH, lay.nHf, angfn(angs[0], lay.S - prm.G0), flags4)
    b.free(ext); b.x(cq); b.free(km.array([cq]))
    if len(pad):
        b.free(pad)
    Htab = [int(lay.H[a]) for a in range(256)]
    if o.mdel:
        mH = measure(b, Hr)
        b.free(Hr)
    else:
        zpad = b.alloc_qubits(max(0, 12 - nH)) if nH < 12 else []
        b.del_lookup(table=Htab, address=address, target=km.array(list(Hr) + list(zpad)))
        b.free(Hr)
        if len(zpad):
            b.free(zpad)

    # --- Horner
    WA = o.WA if o.WA is not None else nT
    gA = b.alloc_qubits(o.gA) if WA < nT else []
    bb.label = 'A'
    Bd.sd_step(bb, x, prm.rowsA, C2r, Tr, prm.eA, WA, gA, True, False)
    bb.label = 'B'
    if o.wins:
        wstore = []
        win_step(bb, x, prm, Tr, Lr, o.wins, o.wcb, True, wstore, o.kbig, o.qcap)
    else:
        Bd.sd_step(bb, x, prm.rowsB, Tr, Lr, prm.eB, LB, [], True, False)
    # --- phase L
    ext = b.alloc_qubits(3)
    RL = list(Lr[prm.G0:]) + list(ext)
    bb.label = 'Lphase'
    Bd.fused_phase(bb, RL, LB - prm.G0 - 1, angfn(angs[1], 0), flags4)
    b.free(ext)
    if o.wins:
        win_step(bb, x, prm, Tr, Lr, o.wins, o.wcb, False, wstore, o.kbig, o.qcap)
    else:
        Bd.sd_step(bb, x, prm.rowsB, Tr, Lr, prm.eB, LB, [], False, False)
    Bd.sd_step(bb, x, prm.rowsA, C2r, Tr, prm.eA, WA, gA, False, False)
    if gA:
        b.free(gA)
    utab = [int(lay.u[a]) | (int(tables[a, 1]) << LB) | (int(tables[a, 2]) << (LB + nT)) for a in range(256)]
    tgt2 = km.array(list(Lr) + list(Tr) + list(C2r))
    if o.mdel:
        mR = measure(b, tgt2)
        b.free(tgt2)
        mdel(b, address, [(mH, Htab), (mR, utab)])
    else:
        b.del_lookup(table=utab, address=address, target=tgt2)
        b.free(tgt2)
    nf1 = b.alloc_qubits(1)[0]
    b.cx(x[21], nf1); b.x(x[21]); b.cx(nf1, x[21])
    F['f1'] = nf1
    # stage-wise un-normalization; each flag equals [top d bits zero] of the restored state,
    # so it is cleared by HMR + a conditional multi-controlled Z (avg half the Toffolis)
    for d, f in [(1, nf1), (2, f2), (4, f4), (8, f8), (16, f16)]:
        for i in range(0, 22 - d):
            b.cswap(f, x[i], x[i + d])
        top = list(x[22 - d:22])
        if d == 1:
            b.x(f); b.cx(x[21], f)
            continue
        m = b.alloc_bits(1)[0]
        b.hmr(f, m)
        b.push_condition(m)
        b.x(km.array(top))
        if d == 2:
            b.cz(top[0], top[1])
        else:
            anc = b.alloc_qubits(d - 2)
            b.init_and(top[0], top[1], anc[0])
            for j in range(1, d - 2):
                b.init_and(anc[j - 1], top[j + 1], anc[j])
            b.cz(anc[d - 3], top[d - 1])
            for j in reversed(range(1, d - 2)):
                b.del_and(anc[j - 1], top[j + 1], anc[j])
            b.del_and(top[0], top[1], anc[0])
            b.free(anc)
        b.x(km.array(top))
        b.pop_condition()
        b.free(km.array([m]))
    b.free(km.array([nf1, f2, f4, f8, f16]))
    Bd.LAST_PEAK = bb.peak
    return b.finish_circuit()


def main(argv):
    import run
    import check as Ck
    prm, kv = run.parse(argv)
    assert prm.sd
    tables = run.get_tables(prm)
    lay = layout(tables, prm, int(kv['LB']), int(kv['S'])) if 'LB' in kv else find_layout(tables, prm)
    print('layout LB', lay.LB, 'S', lay.S, 'nH', lay.nH, 'nHf', lay.nHf, flush=True)
    # NOTICE: the on-disk calibration cache (hang_*.pkl) is removed; always calibrate in memory.
    angs, worst, margin, nrot = calibrate_multi(tables, prm, lay, drop=int(kv.get('drop', 1)))
    print(json.dumps({'worst': worst, 'margin': margin, 'nrot': nrot}), flush=True)
    if not (worst < 1 and margin > 0):
        return
    a, p, xn, bounds = Fm._D
    W = M.window_ranges1(tables, prm, xn, p) if kv.get('g1', '1') == '1' else M.window_ranges(tables, prm, xn, p)
    g = 1 if kv.get('g1', '1') == '1' else 2
    M.PHASE_FN = lambda xs: phases_multi(regs(tables, prm, lay, xs), angs)
    o = Bd.Opts(WA=W['A'], WB=None, gA=g)
    o.mdel = kv.get('mdel') == '1'
    o.wins = parse_plan(kv['wins']) if 'wins' in kv else None
    o.wcb = None
    o.kbig = int(kv.get('kbig', 0))
    o.qcap = int(kv['qcap']) if 'qcap' in kv else None
    if o.wins:
        o.wcb = [int(v) for v in kv['wcb'].split(',')] if 'wcb' in kv else choose_cbits(tables, prm, lay, o.wins)
        print('window counter bits', o.wcb, flush=True)
        assert o.wcb is not None
    circ = build_hsplit(tables, prm, o, lay, angs)
    rep = Ck.report(circ, tables, prm, {'pass': True, 'min_margin': margin}, nbatch=int(kv.get('nbatch', 4)))
    rep['peak'] = Bd.LAST_PEAK
    print(json.dumps(rep), flush=True)
    if 'out' in kv:
        circ.to_file(kv['out'], format='kmb')



# ---------------------------------------------------------------- nested windows for step B
def parse_plan(spec):
    """'15:9,8,7;12:6,5,4;9:3,2,1,0' -> [(K, rows)] outer -> inner."""
    out = []
    for part in spec.split(';'):
        K, rows = part.split(':')
        out.append((int(K), [int(r) for r in rows.split(',')]))
    return out


def sim_windows(tables, prm, lay, wins, cbits):
    """bit-exact simulation of big rows + nested windows + folds; True if L ends correct."""
    if Fm._D is None:
        Fm._D = Fm.domains()
    a, p, xn, bounds = Fm._D
    b = xn & 0x3FFF
    C0, C1, C2 = tables[a, 0], tables[a, 1], tables[a, 2]
    T = M.eval_T(C1, C2, b, prm)
    Ltrue = lay.u[a] + (M.eval_Y(C0, T, b, prm) - C0)
    LB = lay.LB
    m = lambda k: (1 << k) - 1
    L = lay.u[a].copy()
    winrows = set(r for _, rs in wins for r in rs)
    vals = {}
    for i in prm.rowsB:
        sig = 2 * ((b >> i) & 1) - 1
        vals[i] = -sig * M.rd(T, prm.eB(i) + 1)
    for i in prm.rowsB:
        if i not in winrows:
            L = (L + vals[i]) & m(LB)
    cnt = []
    for (K, rows), c in zip(wins, cbits):
        cb = np.zeros_like(L)
        for i in rows:
            v = (L & m(K)) + (cb << K) + vals[i]
            v &= m(K + c)
            L = (L & ~m(K)) | (v & m(K))
            cb = v >> K
        cnt.append(cb)
    for idx in reversed(range(len(wins))):
        K, _ = wins[idx]; c = cbits[idx]
        sval = cnt[idx] - ((cnt[idx] >> (c - 1)) & 1) * (1 << c)   # signed
        if idx > 0:
            Kp, _ = wins[idx - 1]; cp = cbits[idx - 1]
            t = ((L >> K) & m(Kp - K)) + (cnt[idx - 1] << (Kp - K))
            t = (t + sval) & m(Kp - K + cp)
            L = (L & ~(m(Kp - K) << K)) | ((t & m(Kp - K)) << K)
            cnt[idx - 1] = t >> (Kp - K)
        else:
            t = ((L >> K) + sval) & m(LB - K)
            L = (L & m(K)) | (t << K)
    return bool(np.array_equal(L, Ltrue))


def choose_cbits(tables, prm, lay, wins, cmax=3):
    n = len(wins)
    cb = [2] * n
    while not sim_windows(tables, prm, lay, wins, cb):
        if min(cb) >= cmax:
            return None
        cb = [c + 1 for c in cb]
    for k in range(n):
        if cb[k] > 1:
            t = list(cb); t[k] -= 1
            if sim_windows(tables, prm, lay, wins, t):
                cb = t
    return cb


def win_step(bb, x, prm, op, L, wins, cbits, fwd, store, kbig=0, qcap=None):
    b = bb.b
    LB = len(L)

    def row(i, win, k=0):
        e = prm.eB(i) + 1
        srcs = [op[q + e] if 0 <= q + e < len(op) else None for q in range(len(win))]
        assert len(op) - e <= len(win), ('window too small', i, len(op) - e, len(win))
        cin = op[e - 1] if 1 <= e <= len(op) else None
        ctl = x[i]
        if not fwd:
            b.x(ctl)
        for q in win:
            b.cx(ctl, q)
        if qcap:
            need = b.num_allocated_qubits + len(win) - 2
            k = max(0, need - qcap)
            if k and cin is None:
                k += 1
        if k and all(isinstance(s_, km.q) for s_ in srcs[:k]) and k < len(win) - 1:
            bb.add_ip(srcs, win, cin, k)
        else:
            bb.add(srcs, win, cin=cin)
        for q in win:
            b.cx(ctl, q)
        if not fwd:
            b.x(ctl)

    winrows = set(r for _, rs in wins for r in rs)
    big = [i for i in prm.rowsB if i not in winrows]

    def fold(idx, sub):
        K, _ = wins[idx]; cq = store[idx]; c = len(cq)
        if idx > 0:
            Kp, _ = wins[idx - 1]
            tgt = list(L[K:Kp]) + list(store[idx - 1])
        else:
            tgt = list(L[K:])
        src = list(cq) + [cq[-1]] * (len(tgt) - c)
        bb.add(src[:len(tgt)], tgt, subtract=sub)

    if fwd:
        for i in big:
            row(i, list(L), kbig)
        for idx, (K, rows) in enumerate(wins):
            cq = list(b.alloc_qubits(cbits[idx]))
            store.append(cq)
            for i in rows:
                row(i, list(L[:K]) + cq)
        for idx in reversed(range(len(wins))):
            fold(idx, False)
    else:
        for idx in range(len(wins)):
            fold(idx, True)
        for idx in reversed(range(len(wins))):
            K, rows = wins[idx]
            for i in reversed(rows):
                row(i, list(L[:K]) + store[idx])
            b.free(km.array(store[idx]))
        store.clear()
        for i in reversed(big):
            row(i, list(L), kbig)


if __name__ == '__main__':
    main(sys.argv[1:])

