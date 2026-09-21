#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一批 run，在三个粒度上看人机一致：逐判据 / 逐主题 / 逐轮。

再加一个开放世界读法：复核表没提到的命中不算 FP（人只核了机器当时列出的命中，
没被问到的判据人没机会说「该命中」）。封闭世界是下界，开放世界是上界，真值在中间。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))
sys.path.insert(0, str(ROOT / "tools"))

from cmp import gt, hits_of, prf              # noqa: E402
from rubric import THEME_BY_TIP               # noqa: E402


def run(paths):
    G = gt()
    print(f"{'run':<24}{'粒度':<10}{'TP':>5}{'FP':>5}{'FN':>5}{'P':>8}{'R':>8}{'F1':>8}")
    for p in paths:
        H = hits_of(p)
        name = Path(p).parent.name
        for level in ("判据", "判据·开放", "主题", "整轮"):
            TP = FP = FN = 0
            for cid, (pos, neg, _) in G.items():
                if cid not in H:
                    continue
                h = H[cid]
                if level.startswith("判据"):
                    a, b = h, pos
                    if level.endswith("开放"):          # 人没提到的命中不算 FP
                        a = h & (pos | neg)
                elif level == "主题":
                    a = {THEME_BY_TIP[c].no for c in h}
                    b = {THEME_BY_TIP[c].no for c in pos}
                else:
                    a = {"有问题"} if h else set()
                    b = {"有问题"} if pos else set()
                TP += len(a & b); FP += len(a - b); FN += len(b - a)
            pp, rr, ff = prf(TP, FP, FN)
            print(f"{name if level=='判据' else '':<24}{level:<10}"
                  f"{TP:>5}{FP:>5}{FN:>5}{pp:>8.1%}{rr:>8.1%}{ff:>8.1%}")
        print()


run(sys.argv[1:])
