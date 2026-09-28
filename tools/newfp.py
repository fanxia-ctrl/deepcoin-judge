#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出「新FP」明细 —— judge 判了、人既没说对也没说错的格子。

    python3 tools/newfp.py <judge.jsonl> [code]

封闭世界把这些一律算误报，开放世界一律算判对，两边差 20 多个点。
真值要靠逐条看证据定。这里把裁定需要的东西一次摆齐：
问答原文、judge 自己的理由、以及知识库复核（BM25 全量语料，模型当时没看到）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "judge"))
sys.path.insert(0, str(ROOT / "tools"))

import cmp as C                       # noqa: E402
import kbcheck                        # noqa: E402
from rubric import TIP_BY_CODE        # noqa: E402

path = Path(sys.argv[1])
only = sys.argv[2] if len(sys.argv) > 2 else None
G, H = C.gt(), C.hits_of(path)
rows = {json.loads(l)["case_id"]: json.loads(l)
        for l in path.read_text(encoding="utf-8").splitlines() if l.strip()}

n = 0
for cid, (pos, neg, r) in G.items():
    if cid not in H:
        continue
    for c in sorted(H[cid] - pos - neg):          # 人既没说对也没说错
        if only and c != only:
            continue
        n += 1
        j = rows[cid]
        v = next((x for x in j.get("verdicts", []) if x["code"] == c), {})
        print(f"\n{'='*100}\n[{n}] {cid}　{c} {TIP_BY_CODE[c].name}　本轮命中={sorted(H[cid])}")
        print(f"Q: {j.get('query','')}")
        print(f"A: {j.get('answer','')}")
        print(f"judge: quote={v.get('quote','')[:160]}")
        print(f"       why  ={v.get('why','')[:320]}")
        if r["miss_text"].strip():
            print(f"人在这一行写过: {r['miss_text'][:300]}")
        kb = kbcheck.lookup(j.get("query", ""), j.get("answer") or "", k=3, max_chars=900)
        print(f"知识库复核:\n{kb[:900]}")
print(f"\n共 {n} 条新FP")
