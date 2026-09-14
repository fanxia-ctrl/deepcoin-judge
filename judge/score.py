#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""计分：门禁在前，累计在后。

  1 任一 hard_fail 命中 → 直接不可上线（LLM 判据与规则 R9/R10 一视同仁）
  2 否则 deduct = Σ主题 max(该主题命中判据的权重)
        > 2.0 不可上线 · > 0 需修改 · = 0 可直接发
  3 契约规则 R1–R7 独立卡点，不进 deduct
  4 条件型标记出加分与比率；加分只排序，不抵扣负向
  5 材料缺失的主题不算 0 分，标 complete=false
"""
from __future__ import annotations

from collections import defaultdict

from rubric import HARD_FAIL, RATIOS, THEME_BY_TIP, TIP_BY_CODE

DEDUCT_LIMIT = 2.0


def score(verdicts: list[dict], rule_hits: list | None = None,
          undecidable: list[str] | None = None) -> dict:
    """verdicts: [{code, hit, applies?, fact_type?/claim_type?}]
    rule_hits:  objective.Hit 列表
    undecidable: 因缺材料没判的主题 key
    """
    rule_hits = rule_hits or []
    undecidable = undecidable or []

    hit_weight: dict[str, float] = {}
    pos_hits: list[str] = []
    ratio_num: dict[str, int] = defaultdict(int)
    ratio_den: dict[str, int] = defaultdict(int)

    for v in verdicts:
        tip = TIP_BY_CODE.get(str(v.get("code") or ""))
        if tip is None:
            continue
        applies = v.get("applies")
        hit = v.get("hit")
        if tip.conditional and tip.ratio:
            if applies is True:
                ratio_den[tip.ratio] += 1
                if hit is False:
                    ratio_num[tip.ratio] += 1
                    if tip.bonus:
                        pos_hits.append(tip.code)
        if hit is True:
            tv = str(v.get(tip.type_field) or "") if tip.type_field else ""
            hit_weight[tip.code] = tip.weight_for(tv)

    # 主题内取最大值，止住同源双计
    by_theme: dict[str, float] = defaultdict(float)
    for code, w in hit_weight.items():
        th = THEME_BY_TIP[code].key
        by_theme[th] = max(by_theme[th], w)

    scored_rules = [h for h in rule_hits if getattr(h, "kind", "") == "scored"]
    contract = [h for h in rule_hits if getattr(h, "kind", "") == "contract"]
    for h in scored_rules:
        by_theme[f"rule:{h.rule}"] = max(by_theme[f"rule:{h.rule}"], h.weight)

    hard = [c for c, w in hit_weight.items() if w >= HARD_FAIL]
    hard += [h.rule for h in scored_rules if h.weight >= HARD_FAIL]

    deduct = round(sum(by_theme.values()), 3)
    bonus = round(sum(TIP_BY_CODE[c].bonus for c in pos_hits), 3)

    if hard:
        verdict, reason = "不可上线", f"命中 hard_fail：{'、'.join(hard)}"
    elif deduct > DEDUCT_LIMIT:
        verdict, reason = "不可上线", f"扣分 {deduct} 超过 {DEDUCT_LIMIT}"
    elif deduct > 0:
        verdict, reason = "需修改", f"扣分 {deduct}"
    else:
        verdict, reason = "可直接发", "无负向命中"

    # 缺材料只推翻「干净」这个结论：没判的主题可能藏着问题，所以不能说可直接发；
    # 但已经查出来的问题是成立的，不因为别处没判就作废。
    partial = bool(undecidable)
    if partial and verdict == "可直接发":
        verdict, reason = "判定不完整", f"缺材料未判：{'、'.join(undecidable)}"
    elif partial:
        reason += f"（{'、'.join(undecidable)} 缺材料未判，实际可能更差）"

    ratios = {r: (round(ratio_num[r] / ratio_den[r], 3) if ratio_den[r] else None)
              for r in RATIOS}

    return {
        "verdict": verdict, "reason": reason,
        "deduct": deduct, "bonus": bonus,
        "rank_score": round(bonus - deduct, 3),
        "hard_fail": hard,
        "neg_hits": sorted(hit_weight),
        "neg_weights": {c: hit_weight[c] for c in sorted(hit_weight)},
        "theme_deduct": {k: v for k, v in sorted(by_theme.items())},
        "pos_hits": pos_hits,
        "ratio_num": dict(ratio_num), "ratio_den": dict(ratio_den),
        "ratios": ratios,
        "contract_hits": [h.rule for h in contract],
        "contract_ok": not contract,
        "scored_rules": [h.rule for h in scored_rules],
        "undecidable": undecidable,
        "partial": partial,
        # complete=False 表示这条不该进一致率：要么缺材料且结论是「干净」，要么判据组失败
        "complete": not (partial and verdict == "判定不完整"),
    }
