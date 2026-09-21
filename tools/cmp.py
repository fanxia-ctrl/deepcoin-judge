#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线对比多个 judge.jsonl 在复核表 GT 上的表现。

    python3 tools/cmp.py data/runs/*/judge.jsonl

口径与 review.against_judge 完全一致（封闭世界）：
  GT 正例 H+ = 人判「对」的命中 ∪ 人标的漏判；其余一律不该命中。
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))

from rubric import THEMES, TIP_BY_CODE          # noqa: E402
import review as R                              # noqa: E402

GT_XLSX = ROOT / "data/labels/复核-shane-fx-0908-产品.xlsx"


def gt(xlsx=GT_XLSX):
    """case_id → (正例集合, 人明确判错的集合)"""
    out = {}
    for r in R.read(Path(xlsx)):
        pos = {h["code"] for h in r["hits"] if h["mark"] == "对"} | set(r["missed"])
        neg = {h["code"] for h in r["hits"] if h["mark"] == "错"} - pos
        out[r["case_id"]] = (pos, neg, r)
    return out


def hits_of(path):
    out = {}
    for l in Path(path).read_text(encoding="utf-8").splitlines():
        if l.strip():
            o = json.loads(l)
            out[o["case_id"]] = {R.ALIAS.get(c, c) for c in (o.get("neg_hits") or [])}
    return out


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def score(path, G=None):
    G = G or gt()
    H = hits_of(path)
    TP, FP, FN, NEW = Counter(), Counter(), Counter(), Counter()
    cases = {}
    for cid, (pos, neg, _) in G.items():
        h = H.get(cid)
        if h is None:
            continue
        for c in h:
            (TP if c in pos else FP)[c] += 1
            if c not in pos and c not in neg:
                NEW[c] += 1
        for c in pos - h:
            FN[c] += 1
        cases[cid] = {"tp": sorted(h & pos), "fp": sorted(h - pos), "fn": sorted(pos - h)}
    tt, tf, tn = sum(TP.values()), sum(FP.values()), sum(FN.values())
    p, r, f = prf(tt, tf, tn)
    return {"path": str(path), "n": len(cases), "TP": tt, "FP": tf, "FN": tn,
            "new_fp": sum(NEW.values()), "P": p, "R": r, "F1": f,
            "per_code": {c: (TP[c], FP[c], FN[c], NEW[c])
                         for c in TIP_BY_CODE if TP[c] or FP[c] or FN[c]},
            "cases": cases}


def main(paths):
    G = gt()
    rs = [score(p, G) for p in paths]
    w = max(len(Path(r["path"]).parent.name) for r in rs)
    print(f"{'run':<{w}}  {'TP':>4}{'FP':>4}{'FN':>4}  {'P':>6}{'R':>6}{'F1':>6}  新FP")
    for r in rs:
        print(f"{Path(r['path']).parent.name:<{w}}  {r['TP']:>4}{r['FP']:>4}{r['FN']:>4}  "
              f"{r['P']:>6.1%}{r['R']:>6.1%}{r['F1']:>6.1%}  {r['new_fp']:>4}")
    codes = sorted({c for r in rs for c in r["per_code"]})
    print(f"\n{'code':<9}{'名称':<14}" + "".join(f"{Path(r['path']).parent.name[-12:]:>22}" for r in rs))
    for c in codes:
        name = TIP_BY_CODE[c].name[:12]
        cells = []
        for r in rs:
            tp, fp, fn, _ = r["per_code"].get(c, (0, 0, 0, 0))
            p, rr, _ = prf(tp, fp, fn)
            cells.append(f"{tp:>3}/{fp:>2}/{fn:>2} P{p:>4.0%} R{rr:>4.0%}")
        print(f"{c:<9}{name:<14}" + "".join(f"{x:>22}" for x in cells))
    # 正例总数
    tot = sum(len(v[0]) for v in G.values())
    print(f"\nGT 正例 {tot} 条 · {len(G)} 轮")


if __name__ == "__main__":
    main(sys.argv[1:] or sorted(str(p) for p in (ROOT / "data/runs").glob("*/judge.jsonl")))
