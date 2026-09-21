#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自洽投票：多次同配置 run 的逐判据多数表决，看能不能把抖动换成质量。

    python3 tools/vote.py data/runs/A/judge.jsonl data/runs/B/judge.jsonl ...
      [--k 2]   命中票数 >= k 才算命中（默认过半）
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))
sys.path.insert(0, str(ROOT / "tools"))

from cmp import gt, prf                      # noqa: E402
import review as R                           # noqa: E402


def load(p):
    out = {}
    for l in Path(p).read_text(encoding="utf-8").splitlines():
        if l.strip():
            o = json.loads(l)
            out[o["case_id"]] = {R.ALIAS.get(c, c) for c in (o.get("neg_hits") or [])}
    return out


def ev(H, G):
    TP = FP = FN = 0
    per = defaultdict(lambda: [0, 0, 0])
    for cid, (pos, _, _) in G.items():
        h = H.get(cid)
        if h is None:
            continue
        for c in h:
            if c in pos:
                TP += 1; per[c][0] += 1
            else:
                FP += 1; per[c][1] += 1
        for c in pos - h:
            FN += 1; per[c][2] += 1
    return TP, FP, FN, per


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    kflag = next((a for a in sys.argv[1:] if a.startswith("--k")), None)
    G = gt()
    runs = [load(p) for p in args]
    n = len(runs)
    print(f"{n} 次 run，GT {len(G)} 轮\n")
    for p, H in zip(args, runs):
        tp, fp, fn, _ = ev(H, G)
        pp, rr, ff = prf(tp, fp, fn)
        print(f"  单次 {Path(p).parent.name:<22} TP{tp:>4} FP{fp:>3} FN{fn:>3}  "
              f"P{pp:>6.1%} R{rr:>6.1%} F1{ff:>6.1%}")
    # 逐 (case, code) 投票
    votes = defaultdict(int)
    cases = set.intersection(*[set(h) for h in runs])
    for H in runs:
        for cid in cases:
            for c in H[cid]:
                votes[(cid, c)] += 1
    print()
    ks = [int(kflag.split("=")[1])] if kflag and "=" in kflag else range(1, n + 1)
    best = None
    for k in ks:
        H = {cid: {c for (i, c), v in votes.items() if i == cid and v >= k} for cid in cases}
        tp, fp, fn, per = ev(H, G)
        pp, rr, ff = prf(tp, fp, fn)
        tag = {1: "并集", n: "全票"}.get(k, "多数" if k * 2 > n else "")
        print(f"  投票 >={k}/{n} {tag:<4}          TP{tp:>4} FP{fp:>3} FN{fn:>3}  "
              f"P{pp:>6.1%} R{rr:>6.1%} F1{ff:>6.1%}")
        if best is None or ff > best[0]:
            best = (ff, k, per)
    # 抖动统计
    unstable = sum(1 for v in votes.values() if 0 < v < n)
    print(f"\n  票数不一致的 (轮, 判据) 对：{unstable} / 总命中对 {len(votes)}"
          f"　—— 这就是同配置下的抖动量")
    print(f"\n  最佳阈值 k={best[1]} 的逐判据：")
    for c, (tp, fp, fn) in sorted(best[2].items()):
        p, r, f = prf(tp, fp, fn)
        print(f"    {c}  TP{tp:>3} FP{fp:>3} FN{fn:>3}  P{p:>5.0%} R{r:>5.0%} F1{f:>5.0%}")


main()
