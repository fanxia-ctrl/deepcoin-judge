#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bench：一条命令跑完「体检 → 三粒度 → 逐判据」。

    python3 tools/bench.py data/runs/shane-fx-0908-v8/judge.jsonl [...]
    python3 tools/bench.py --closed ...      # 不打 GT 补丁，看封闭世界下界

口径一次定死，迭代期间不再改（改了旧 run 的数就不能比）：

  数据    data/in/shane-fx-0908，94 轮
  GT      复核-shane-fx-0908-产品.xlsx + gt-补丁.jsonl，171 条正例，3 条存疑两边不计
  归一    review.ALIAS 把 neg_2_3/2_4 并到 neg_2_2（记哪条的分歧不算错）
  粒度    判据（主口径）/ 判据·开放 / 主题 / 整轮
  协议    每个配置 3 采样 2/3 表决才算数；单跑只定方向，赢面 < 6 分不作数

体检先跑：端点掉线跑出来的 run 不是「判得差」，是没判。v13 掉了 19 次连接、
21 轮判定不完整，它的 F1 64.9 拿去和别人比是错的 —— 这种 run 直接标作废。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))
sys.path.insert(0, str(ROOT / "tools"))

import cmp as C                                      # noqa: E402
from rubric import TIP_BY_CODE, THEME_BY_TIP         # noqa: E402

# 组失败是端点故障，和判定质量无关。整个 run 作废太粗 —— 端点一直在抖，
# 那样大部分 run 都得扔。默认改成「公共子集」：只在参比各方都判完整的轮次上比，
# 掉线的轮次两边一起剔除。作废只留给烂到没法比的（v13 掉了 19 次，94 轮废 21 轮）。
MAX_BROKEN_RATE = 0.15


def health(path: Path) -> dict:
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    h = {
        "n": len(rows),
        "组失败": sum(len(r.get("groups_failed") or []) for r in rows),
        "不完整": sum(1 for r in rows if not r.get("complete")),
        "未判": sum(len(r.get("unjudged") or []) for r in rows),
        "报错": sum(len(r.get("judge_errors") or []) for r in rows),
    }
    h["坏轮"] = {r["case_id"] for r in rows
                if not r.get("complete") or r.get("groups_failed")}
    rate = len(h["坏轮"]) / max(h["n"], 1)
    h["作废"] = (f"{len(h['坏轮'])}/{h['n']} 轮没判完（>{MAX_BROKEN_RATE:.0%}）"
                if rate > MAX_BROKEN_RATE else "")
    return h


def levels(path: Path, G: dict, drop: set[str] = frozenset()) -> dict:
    """四个粒度的 TP/FP/FN。存疑格子（C.SKIP）两边都不计。"""
    H = C.hits_of(path)
    out = {}
    for level in ("判据", "判据·开放", "主题", "整轮"):
        TP = FP = FN = 0
        for cid, (pos, neg, _) in G.items():
            if cid not in H or cid in drop:
                continue
            skip = {c for (x, c) in C.SKIP if x == cid}
            h, p_ = H[cid] - skip, pos - skip
            if level.startswith("判据"):
                a, b = h, p_
                if level.endswith("开放"):        # 人没被问到的命中不算 FP
                    a = h & (p_ | neg)
            elif level == "主题":
                a = {THEME_BY_TIP[c].no for c in h}
                b = {THEME_BY_TIP[c].no for c in p_}
            else:
                a = {"有问题"} if h else set()
                b = {"有问题"} if p_ else set()
            TP += len(a & b); FP += len(a - b); FN += len(b - a)
        out[level] = (TP, FP, FN) + C.prf(TP, FP, FN)
    return out


def per_code(path: Path, G: dict, drop: set[str] = frozenset()) -> dict:
    """逐判据 TP/FP/FN/新FP。新FP = 判出来了、人既没说对也没说错（封闭世界的代价）。"""
    from collections import Counter
    H = C.hits_of(path)
    TP, FP, FN, NEW = Counter(), Counter(), Counter(), Counter()
    for cid, (pos, neg, _) in G.items():
        if cid not in H or cid in drop:
            continue
        skip = {c for (x, c) in C.SKIP if x == cid}
        h, p_ = H[cid] - skip, pos - skip
        for c in h:
            (TP if c in p_ else FP)[c] += 1
            if c not in p_ and c not in neg:
                NEW[c] += 1
        for c in p_ - h:
            FN[c] += 1
    return {c: (TP[c], FP[c], FN[c], NEW[c]) for c in TIP_BY_CODE
            if TP[c] or FP[c] or FN[c]}


def main(argv):
    closed = "--closed" in argv
    paths = [Path(a) for a in argv if not a.startswith("-")]
    if not paths:
        paths = sorted((ROOT / "data/runs").glob("*/judge.jsonl"))
    G = C.gt(patch=None if closed else C.PATCH)

    print("## 体检")
    print(f"{'run':<22}{'轮':>4}{'组失败':>7}{'不完整':>7}{'未判':>6}{'报错':>6}  结论")
    ok, drop = [], set()
    for p in paths:
        h = health(p)
        print(f"{p.parent.name:<22}{h['n']:>4}{h['组失败']:>7}{h['不完整']:>7}"
              f"{h['未判']:>6}{h['报错']:>6}  {'✗ 作废 · ' + h['作废'] if h['作废'] else '✓'}")
        if not h["作废"]:
            ok.append(p)
            drop |= h["坏轮"]

    tot = sum(len(v[0]) for cid, v in G.items() if cid not in drop)
    print(f"\nGT：{'封闭世界（无补丁）' if closed else '复核表 + gt-补丁'}"
          f"　正例 {tot} 条 · {len(G) - len(drop & set(G))} 轮"
          f"（剔除 {len(drop & set(G))} 轮没判完的）· 存疑 {len(C.SKIP)} 格不计")

    print("\n## 三粒度（公共子集：参比各方都判完整的轮次）")
    print(f"{'run':<22}{'粒度':<11}{'TP':>5}{'FP':>5}{'FN':>5}{'P':>8}{'R':>8}{'F1':>8}")
    for p in ok:
        L = levels(p, G, drop)
        for i, (lv, (tp, fp, fn, pp, rr, ff)) in enumerate(L.items()):
            print(f"{p.parent.name if i == 0 else '':<22}{lv:<11}"
                  f"{tp:>5}{fp:>5}{fn:>5}{pp:>8.1%}{rr:>8.1%}{ff:>8.1%}")
        print()

    print("## 逐判据（主口径 · 判据级）")
    PC = {p: per_code(p, G, drop) for p in ok}
    codes = sorted({c for d in PC.values() for c in d})
    print(f"{'code':<9}{'名称':<16}" + "".join(f"{p.parent.name[-11:]:>26}" for p in ok))
    for c in codes:
        cells = []
        for p in ok:
            tp, fp, fn, new = PC[p].get(c, (0, 0, 0, 0))
            pp, rr, _ = C.prf(tp, fp, fn)
            cells.append(f"{tp:>3}/{fp:>2}/{fn:>2} P{pp:>4.0%} R{rr:>4.0%} 新{new:>2}")
        print(f"{c:<9}{TIP_BY_CODE[c].name[:14]:<16}" + "".join(f"{x:>26}" for x in cells))


if __name__ == "__main__":
    main(sys.argv[1:])
