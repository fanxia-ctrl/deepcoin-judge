#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一条 Turn 组装成喂给 judge 的五样材料。"""
from __future__ import annotations

from scorers import objective


EVIDENCE_HEADERS = ("Knowledge evidence:", "Live account evidence:")


def _evidence_from_hits(hits: list[dict], limit: int) -> str:
    """老数据没有 evidence_text 时的兜底：用 knowledge-retrieval 节点输出拼一份。"""
    ev, used = [], 0
    for i, h in enumerate(hits, 1):
        body = str(h.get("content") or "")
        piece = f"[{i}] 相似度 {h.get('score')} · {h.get('document')}\n{body}"
        if used + len(piece) > limit:
            ev.append(f"…（其余 {len(hits) - i + 1} 条切片未传入）")
            break
        ev.append(piece)
        used += len(piece)
    return "\n\n".join(ev)


def _clip(text: str, limit: int, what: str) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…（{what}超长，截去 {len(text) - limit} 字）"


def build_ctx(turn: dict, truth: dict[str, str], evidence_chars: int = 4000,
              rules_chars: int = 9000) -> dict:
    voice = str(turn.get("voice") or "")
    detail = str(turn.get("detail") or "")
    answer = voice + (f"\n\n【详情】{detail}" if detail.strip() else "")

    # 证据以模型实际看到的原文为准（final_context.evidence：KB 切片 + 工具返回）。
    # 只有标题没有内容的算空。
    ev = str(turn.get("evidence_text") or "").strip()
    if ev:
        stripped = ev
        for h in EVIDENCE_HEADERS:
            stripped = stripped.replace(h, "")
        if not stripped.strip():
            ev = ""
    if not ev:
        ev = _evidence_from_hits(turn.get("kb_hits") or [], evidence_chars)
    else:
        ev = _clip(ev, evidence_chars, "证据")

    rules = _clip(str(turn.get("route_rules") or "").strip(), rules_chars, "路由业务规则")

    return {
        "query": str(turn.get("query") or ""),
        "answer": answer,
        "evidence": ev,
        "rules": rules,
        "tool_calls": turn.get("tool_calls") or [],
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
