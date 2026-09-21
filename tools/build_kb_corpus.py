#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 voice_agent_v3 导出的知识库切片压成一份 judge 侧的核对语料。

    python3 tools/build_kb_corpus.py [--src ../voice_agent_v3/docs/kb_raw]

为什么要这一份：judge 现在只看得到被测 agent 那一轮召回的切片（中位数 959 字），
而人工标注看的是产品真相。`neg_1_1` 漏判的 11 条里，事实全都在知识库里 ——
「仓位合并仅支持全仓-分仓模式」「模拟交易 2026-07-24 下线」都是逐字有的，
只是不在那一轮的召回里。judge 拿这份语料自己再检一次，才对得上人的口径。

这不是拿 GT 训练：语料来自产品自己的知识库，与复核表无关。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 归档、待删、导出中间产物不进语料
SKIP = re.compile(r"(^_|_archive|_to_delete|before_|\.before)")


def clean(s: str) -> str:
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s or "")        # 图片
    s = re.sub(r"https?://\S+", "", s)                      # 裸链接
    s = re.sub(r"[ \t]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path,
                    default=Path.home() / "codes/voice_agent_v3/docs/kb_raw")
    ap.add_argument("-o", "--out", type=Path, default=ROOT / "data/kb_corpus.jsonl")
    ap.add_argument("--min-chars", type=int, default=12)
    ap.add_argument("--max-chars", type=int, default=1200)
    a = ap.parse_args()

    seen: set[str] = set()
    rows: list[dict] = []
    for f in sorted(a.src.rglob("*.segments.jsonl")):
        rel = f.relative_to(a.src)
        if any(SKIP.search(p) for p in rel.parts):
            continue
        dataset = rel.parts[0]
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            q, ans = clean(str(o.get("content") or "")), clean(str(o.get("answer") or ""))
            text = (f"Q: {q}\nA: {ans}" if ans else q)[:a.max_chars]
            if len(text) < a.min_chars:
                continue
            key = re.sub(r"\s+", "", text)[:200]
            if key in seen:                     # 多个库反复导同一条，只留一份
                continue
            seen.add(key)
            rows.append({"id": o.get("id") or f"{dataset}:{len(rows)}",
                         "dataset": dataset, "document": f.name.split(".segments")[0],
                         "text": text})
    a.out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                     encoding="utf-8")
    from collections import Counter
    c = Counter(r["dataset"] for r in rows)
    print(f"OK {a.out}　{len(rows)} 条切片（去重后），{a.out.stat().st_size / 1e6:.1f} MB")
    for d, n in c.most_common(12):
        print(f"   {n:>6}  {d}")
    return 0


raise SystemExit(main())
