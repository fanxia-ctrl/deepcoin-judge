#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""错误分析：某个 run 在某条判据上的 FN / FP 逐条明细，带 judge 自己的理由和人的理由。

    python3 tools/err.py <judge.jsonl> <code> [fn|fp|both] [--full]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))
sys.path.insert(0, str(ROOT / "tools"))

from cmp import gt, hits_of          # noqa: E402


def rows_of(path):
    out = {}
    for l in Path(path).read_text(encoding="utf-8").splitlines():
        if l.strip():
            o = json.loads(l)
            out[o["case_id"]] = o
    return out


def main():
    path, code = sys.argv[1], sys.argv[2]
    which = sys.argv[3] if len(sys.argv) > 3 and not sys.argv[3].startswith("-") else "both"
    full = "--full" in sys.argv
    W = 100000 if full else 300
    G, J = gt(), rows_of(path)
    H = hits_of(path)
    for cid, (pos, neg, r) in G.items():
        if cid not in H:
            continue
        h = H[cid]
        kind = ("FN" if (code in pos and code not in h) else
                "FP" if (code in h and code not in pos) else None)
        if not kind or (which != "both" and which.upper() != kind):
            continue
        j = J[cid]
        v = next((x for x in j.get("verdicts", []) if x["code"] == code), {})
        print(f"\n{'='*100}\n[{kind}] {cid}   机器门禁={j.get('verdict')}  本轮命中={sorted(h)}")
        print(f"Q: {j.get('query','')[:W]}")
        print(f"A: {j.get('answer','')[:W]}")
        print(f"judge.{code}: hit={v.get('hit')} applies={v.get('applies')} "
              f"type={ {k:x for k,x in v.items() if k.endswith('_type')} }")
        print(f"   quote: {v.get('quote','')[:200]}")
        print(f"   why  : {v.get('why','')[:300]}")
        print(f"人: 判对={[ (x['code'],x['mark']) for x in r['hits'] ]} 漏判={r['missed']}")
        if r["miss_text"].strip():
            print(f"   人的理由: {r['miss_text'][:W]}")
        if full:
            print(f"--- 证据 ---\n{(j.get('evidence') or '')[:2500]}")


main()
