#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条 Turn 组装成喂给 judge 的五样材料。"""
from __future__ import annotations

from scorers import objective


def build_ctx(turn: dict, truth: dict[str, str], evidence_chars: int = 4000) -> dict:
    voice = str(turn.get("voice") or "")
    detail = str(turn.get("detail") or "")
    answer = voice + (f"\n\n【详情】{detail}" if detail.strip() else "")

    hits = turn.get("kb_hits") or []
    ev, used = [], 0
    for i, h in enumerate(hits, 1):
        body = str(h.get("content") or "")
        piece = f"[{i}] 相似度 {h.get('score')} · {h.get('document')}\n{body}"
        if used + len(piece) > evidence_chars:
            ev.append(f"…（其余 {len(hits) - i + 1} 条切片未传入）")
            break
        ev.append(piece)
        used += len(piece)

    return {
        "query": str(turn.get("query") or ""),
        "answer": answer,
        "evidence": "\n\n".join(ev),
        "truth": truth.get(str(turn.get("case_id") or ""), ""),
        "signals": {**objective.retrieval_signals(turn),
                    **objective.account_signals(turn)},
    }


def missing_materials(ctx: dict) -> set[str]:
    """哪些材料是空的 —— 需要它的主题会被标为无法判定。"""
    out = set()
    if not (ctx.get("truth") or "").strip():
        out.add("truth")
    if not (ctx.get("evidence") or "").strip():
        out.add("evidence")
    return out
