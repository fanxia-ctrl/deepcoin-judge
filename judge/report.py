#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run 级报告：门禁分布、判据命中、六个比率、簇级检查。"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from rubric import RATIOS, TIP_BY_CODE
from scorers import objective


def write(rows: list[dict], turns: list[dict], out: Path, meta: dict) -> None:
    n = len(rows) or 1
    L = ["# judge 结果", "",
         f"来源 `{meta['source']}` · 模型 `{meta['llm']}` · 分组 `{meta['group']}` · "
         f"{len(rows)} 轮 · {meta['elapsed']:.0f}s · "
         f"LLM 调用 {sum(r.get('llm_calls', 0) for r in rows)} 次", ""]
    if meta["llm"] == "mock":
        L += ["> **mock 的判定没有意义**，这份报告只用来确认链路通。", ""]

    vc = Counter(r["verdict"] for r in rows)
    L += ["## 门禁", "", "| 结论 | 条数 | 占比 |", "|---|---:|---:|"]
    for k in ("可直接发", "需修改", "不可上线", "判定不完整"):
        if vc.get(k):
            L.append(f"| {k} | {vc[k]} | {vc[k] / n:.0%} |")
    bad_contract = sum(1 for r in rows if not r["contract_ok"])
    L += ["", f"契约卡点命中 **{bad_contract}** 条（不进质量分，但不能发）。",
          f"超出知识库覆盖 **{sum(1 for r in rows if r.get('out_of_coverage'))}** 条 —— 进待标池。",
          f"缺权威口径、T1 未判 **{sum(1 for r in rows if not r.get('has_truth'))}** 条。", ""]

    L += ["## 六个比率", "",
          "做对 = 前提成立且未命中。分母是前提成立的轮次 —— 比「加分总和」可解释。", "",
          "| 比率 | 做对 / 该做对 | 达成 |", "|---|---:|---:|"]
    for r_name in RATIOS:
        num = sum(x["ratio_num"].get(r_name, 0) for x in rows)
        den = sum(x["ratio_den"].get(r_name, 0) for x in rows)
        L.append(f"| {r_name} | {num} / {den} | {f'{num / den:.0%}' if den else '—'} |")

    hits = Counter(c for r in rows for c in r["neg_hits"])
    L += ["", "## 判据命中", "", "| 判据 | 名称 | 权重 | 命中 |", "|---|---|---:|---:|"]
    for c, k in hits.most_common():
        t = TIP_BY_CODE[c]
        L.append(f"| `{c}` | {t.name} | {t.weight} | {k} |")
    unhit = [c for c in TIP_BY_CODE if c not in hits]
    if unhit:
        L += ["", f"一次都没命中：{'、'.join(f'`{c}`' for c in unhit)}", ""]

    rc = Counter(d["rule"] for r in rows for d in r.get("rule_detail", []))
    if rc:
        L += ["", "## 规则命中", "", "| 规则 | 名称 | 类型 | 命中 |", "|---|---|---|---:|"]
        seen = {}
        for r in rows:
            for d in r.get("rule_detail", []):
                seen[d["rule"]] = d
        for rule, k in rc.most_common():
            d = seen[rule]
            kind = {"contract": "契约卡点", "scored": "计分"}.get(d["kind"], d["kind"])
            L.append(f"| {rule} | {d['name']} | {kind} | {k} |")

    clusters = objective.check_cluster(turns)
    L += ["", "## 簇级检查 · R11 口径冲突", ""]
    if clusters:
        L.append("同一簇近重复问题出现相反结论 —— **标为知识库问题，不惩罚模型**。\n")
        for c in clusters:
            L.append(f"- **{c['suite']}**（{c['n']} 条）：{c['why']}")
            L.append(f"  - 说可以：{'、'.join(c['say_yes'])}")
            L.append(f"  - 说不行：{'、'.join(c['say_no'])}")
    else:
        L.append("没有检出。")

    over = sum(len(r.get("overclaimed") or []) for r in rows)
    errs = [e for r in rows for e in r.get("judge_errors", [])]
    if over or errs:
        L += ["", "## judge 自身的问题", "",
              f"引用校验翻回 **{over}** 条命中（quote 引不出回答原文）。"]
        for e, k in Counter(errs).most_common(8):
            L.append(f"- {e}　×{k}")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
