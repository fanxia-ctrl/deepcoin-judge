#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按判据组拼 prompt。

输出 schema 比 v1 多两样：
  applies    条件型判据必填，前提是否成立。做对 = applies && !hit
  <type>     neg_1_1 要 fact_type，neg_5_1 要 claim_type，决定分档权重
"""
from __future__ import annotations

from dataclasses import dataclass

from rubric import (NEEDS_ANSWER, NEEDS_EVIDENCE, NEEDS_QUERY, NEEDS_SIGNALS,
                    NEEDS_TRUTH, Theme)


@dataclass(frozen=True)
class _Merged:
    needs: tuple[str, ...]


BASE = """你在评估一个加密货币交易所的**语音客服**的单轮回答。

你只做一件事：逐条判断下面列出的判据是否命中。不要给总分，不要改写回答。

判定纪律：
- 只根据给你的材料判断。材料里没有的事实，不要用你自己的知识补。
- 命中就必须能从回答里引出一段原文作为证据；引不出来就是不命中。
- 判据之间互不影响，可以命中多条，也可以一条都不命中。
- 拿不准就判不命中，并在理由里写清为什么拿不准。
- 逐条独立判。不要因为已经判了几条命中，就倾向于把剩下的判成不命中。
- **材料缺失时不要猜**：判据需要的材料没给，把 hit 设为 null、why 写「缺材料」。

输出严格的 JSON，不要 markdown 代码块，不要多余文字：
{"verdicts":[{"code":"判据编号","hit":true/false,"quote":"回答里的原文片段","why":"一句话理由"}]}
verdicts 必须覆盖下面每一条判据，顺序一致。"""

APPLIES_NOTE = """
**标了「前提」的判据还要填 `applies`**：这一轮是否满足那个前提。
- `applies` 与 `hit` 是两件事：前提成立且没犯错（applies=true, hit=false）才算做对。
- 前提不成立（applies=false）时，hit 必然是 false。
- 这一项用来统计「该做对的时候做对了多少」，请如实填。"""

TYPE_NOTE = """
**标了「分类」的判据命中时还要填那个字段**，从给定选项里选一个，它决定扣分档位。"""


def build(themes, ctx: dict) -> tuple[str, str]:
    if isinstance(themes, Theme):
        themes = (themes,)
    themes = tuple(themes)

    body_tips = [t for th in themes for t in th.tips]
    has_cond = any(t.conditional for t in body_tips)
    has_type = any(t.type_field for t in body_tips)

    lines = [BASE]
    if has_cond:
        lines.append(APPLIES_NOTE)
    if has_type:
        lines.append(TYPE_NOTE)
    lines.append("")

    for th in themes:
        lines.append(f"## 判据组：{th.name}")
        if th.hint:
            lines.append(th.hint)
        lines.append("")
        for t in th.tips:
            row = f"- `{t.code}` **{t.name}** —— {t.desc}"
            lines.append(row)
            if t.note and "合并" not in t.note and "移自" not in t.note:
                lines.append(f"    说明：{t.note}")
            if t.conditional:
                lines.append(f"    前提（applies）：{t.applies_when}")
                lines.append(f"    做对的样子：{t.right_looks_like}")
            if t.type_field:
                lines.append(f"    分类（{t.type_field}）：{' / '.join(t.type_options)}")
        lines.append("")

    lines.append("## 输出字段")
    fields = ['"code"', '"hit"', '"quote"', '"why"']
    if has_cond:
        fields.insert(2, '"applies"')
    for t in body_tips:
        if t.type_field:
            fields.append(f'"{t.type_field}"（仅 {t.code} 命中时）')
    lines.append("每条 verdict：" + "、".join(fields))

    system = "\n".join(lines).rstrip()

    theme = _Merged(tuple({n for th in themes for n in th.needs}))
    u = []
    if NEEDS_QUERY in theme.needs:
        u.append(f"<原始问题>\n{ctx['query']}\n</原始问题>")
    if NEEDS_ANSWER in theme.needs:
        u.append(f"<回答>\n{ctx['answer']}\n</回答>")
    if NEEDS_TRUTH in theme.needs:
        tr = (ctx.get("truth") or "").strip()
        u.append(f"<权威口径>\n{tr or '（没有提供 —— 需要权威口径的判据一律填 hit=null）'}\n</权威口径>")
    if NEEDS_EVIDENCE in theme.needs:
        ev = (ctx.get("evidence") or "").strip()
        u.append(f"<本轮证据>\n{ev or '（本轮一条切片都没召回）'}\n</本轮证据>")
    if NEEDS_SIGNALS in theme.needs:
        s = ctx.get("signals") or {}
        u.append("<机器信号>\n" + "\n".join(f"{k}: {v}" for k, v in s.items()) + "\n</机器信号>")
    return system, "\n\n".join(u)
