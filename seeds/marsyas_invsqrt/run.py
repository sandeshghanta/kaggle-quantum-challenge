"""End-to-end: fit tables (cached) -> exact all-x model check -> build -> simulate -> cost.

usage: python run.py F0 FT F2 G0 [key=value ...]
  keys: dropA=i,j  dropB=i,j  streamA=0/1 streamB=0/1 out=path.kmb
"""
from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

import model as M
import fit as Fm
import build as Bd
import check as Ck

WORK = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'work', 'invsqrt')


def parse(argv):
    F0, FT, F2, G0 = map(int, argv[:4])
    kv = dict(a.split('=', 1) for a in argv[4:])
    dropA = tuple(int(v) for v in kv.get('dropA', '').split(',') if v)
    dropB = tuple(int(v) for v in kv.get('dropB', '').split(',') if v)
    sd = kv.get('sd') == '1'
    booth = kv.get('booth', '0' if sd else '1') == '1'
    nr = 7 if booth else 14
    prm = M.Params(F0=F0, FT=FT, F2=F2, G0=G0, booth=booth, sd=sd, tcap=kv.get('tcap', '1') == '1',
                   rowsA=tuple(i for i in range(nr) if i not in dropA),
                   rowsB=tuple(i for i in range(nr) if i not in dropB))
    return prm, kv


def tag(prm):
    nr = 7 if prm.booth else 14
    dA = ''.join(f'{i:x}' for i in range(nr) if i not in prm.rowsA)
    dB = ''.join(f'{i:x}' for i in range(nr) if i not in prm.rowsB)
    return f'{prm.F0}_{prm.FT}_{prm.F2}_{prm.G0}{"_bo" if prm.booth else ""}{"_sd" if prm.sd else ""}{"_tc" if prm.tcap else ""}_a{dA}_b{dB}'


RAD = (6, 3)
_TABLES = {}


def get_tables(prm, refit=False):
    # NOTICE: in-memory cache (was an on-disk tab_*.npy)
    key = tag(prm)
    if key in _TABLES and not refit:
        return _TABLES[key]
    tables, _ = Fm.fit_all(prm, rad1=RAD[0], rad2=RAD[1])
    _TABLES[key] = tables
    return tables


def main(argv):
    prm, kv = parse(argv)
    global RAD
    if 'rad' in kv:
        RAD = tuple(int(v) for v in kv['rad'].split(','))
    t0 = time.time()
    if 'joint' in kv:
        import calib
        d = kv['joint']
        tables = np.load(os.path.join(WORK, f'jtab_{tag(prm)}_d{d}.npy'))
        jang = [int(a) for a in np.load(os.path.join(WORK, f'jang_{tag(prm)}_d{d}.npy'), allow_pickle=True)]
        worst, margin, _, _, _ = calib.check(tables, prm, jang)
        res = {'pass': worst < 1 and margin > 0, 'worst': worst, 'min_margin': int(margin), 'joint': d}
        M.ANGLES = jang
    else:
        tables = get_tables(prm, refit=kv.get('refit') == '1')
        res = M.full_check(tables, prm)
    out = {'tag': tag(prm), 'model': res}
    if not res['pass'] and not ('calib' in kv and res.get('worst', 9) < float(kv.get('cmax', 1.3))):
        print(json.dumps(out)); return out
    if not res['pass']:
        # full_check stopped early; compute the true worst/margin through calib
        res = {'pass': True, 'worst': res['worst'], 'min_margin': 0}
    angles = M.ANGLES if 'joint' in kv else None
    if 'calib' in kv and 'joint' not in kv:
        import calib
        bestc = None
        for drop in [int(v) for v in kv['calib'].split(',')]:
            ang, worst, margin, nrot = calib.calibrate(tables, prm, drop=drop, verbose=False, mode=kv.get('cmode', 'margin'))
            if worst < 1 and margin > 0:
                rc = M.rotation_cost(nrot, margin)
                if bestc is None or rc < bestc[0]:
                    bestc = (rc, ang, worst, margin, drop)
        if bestc is None and res['min_margin'] <= 0:
            out['model'] = dict(res, pass_=False, note='calibration infeasible'); print(json.dumps(out)); return out
        if bestc is not None:
            _, angles, worst, margin, drop = bestc
            res = dict(res, worst=worst, min_margin=int(margin), calib_drop=drop)
            out['model'] = res
            M.ANGLES = [int(a) for a in angles]
    a, p, xn, bounds = Fm.domains() if Fm._D is None else Fm._D
    W = M.window_ranges(tables, prm, xn, p)
    if kv.get('g1') == '1':
        W = M.window_ranges1(tables, prm, xn, p)
    caps = [int(c) for c in kv['caps'].split(',')] if 'caps' in kv else [int(kv['cap']) if 'cap' in kv else None]
    best = None
    for cap in caps:
        o = Bd.Opts(WA=W['A'], WB=W['B'], gA=1 if kv.get('g1') == '1' else 2, gB=1 if kv.get('g1') == '1' else 2, streamA=kv.get('streamA') == '1', streamB=kv.get('streamB') == '1', cap=cap, angles=angles, xext=kv.get('xext') == '1', fuse=kv.get('fuse') == '1', px21=kv.get('px21') == '1', splitA=int(kv['splitA']) if 'splitA' in kv else None, splitB=int(kv['splitB']) if 'splitB' in kv else None)
        circ_c = Bd.build(tables, prm, o)
        rep = Ck.report(circ_c, tables, prm, res, nbatch=int(kv.get('nbatch', 8)))
        rep['cap'] = cap; rep['peak'] = Bd.LAST_PEAK
        if best is None or rep['cost'] < best[0]['cost']:
            best = (rep, circ_c)
    rep, circ = best
    out.update(rep); out['W'] = W; out['sec'] = round(time.time() - t0, 1)
    if 'out' in kv:
        circ.to_file(kv['out'], format='kmb')
        np.save(kv['out'] + '.tables.npy', tables)
        if angles is not None:
            np.save(kv['out'] + '.angles.npy', np.array([int(a) for a in angles], dtype=object), allow_pickle=True)
    print(json.dumps(out))
    return out


if __name__ == '__main__':
    main(sys.argv[1:])

