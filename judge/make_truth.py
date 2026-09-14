#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从回收的标注表抽「权威口径」，产出 judge 用的 truth.jsonl。

    python3 bench/judge/make_truth.py bench/labels/2026-09-08-shane-fx-95.xlsx

标注员在 J 列写了「正确说法应是：…」，那就是这一问的权威口径。
Theme 1（事实与口径）没有它判不准 —— 模型不知道平台真实规则。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

LEAD = re.compile(r"(?:目前)?正确(?:的)?说法(?:应该)?(?:是|应是)[：:]?|"
                  r"正确的说法应该是[：:]?|正确说法应是[：:]?")
# 纯过程描述，不是可用的口径
META = re.compile(r"^(需要先|先协助|不了解用户|不理解用户|应该反问|可以反问|需要反问|"
                  r"AI并未|由于不确定|用户应该是|不确定用户|可引导用户|没有给用户解释|"
                  r"用户咨询|咨询为什么|自动划转是|返佣划转到|柱形图应该是|平台支持|"
                  r"BTC合约手续费率可以)")
# 像一句能直接发给用户的答案
ANSWERISH = re.compile(r"^(您好|您可以|目前|如果您|麻烦您|抱歉|很抱歉|非常抱歉|请问|建议|"
                       r"【|由于|感谢|明白|了解到|关于)")


def main(labels_xlsx=None, out=None, min_chars: int = 8) -> int:
    if labels_xlsx is None:
        ap = argparse.ArgumentParser()
        ap.add_argument("labels_xlsx", type=Path)
        ap.add_argument("-o", "--out", type=Path, default=None)
        ap.add_argument("--min-chars", type=int, default=8)
        a = ap.parse_args()
        labels_xlsx, out, min_chars = a.labels_xlsx, a.out, a.min_chars
    labels_xlsx = Path(labels_xlsx)
    a = type("A", (), {"labels_xlsx": labels_xlsx, "out": out, "min_chars": min_chars})
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("需要 openpyxl：pip3 install openpyxl")

    ws = load_workbook(a.labels_xlsx, data_only=True)["标注"]
    rows, skipped = [], []
    for r in range(4, ws.max_row + 1):
        cid = ws.cell(r, 1).value
        if not cid:
            continue
        why = str(ws.cell(r, 10).value or "").strip()
        if not why:
            continue
        m = LEAD.search(why)
        if m:
            truth, src = why[m.end():].strip(" ：:\n"), "human_label"
        else:
            # 有一批 J 列没写引导语，直接就是正确答案（「您好，增强版体验金是无法…」）。
            # 排掉纯过程描述（「需要先核实」「应该反问用户」这类），剩下的按宽松规则收，
            # source 标成 loose 以便回看时能分辨。
            if META.match(why) or not ANSWERISH.match(why):
                skipped.append((str(cid), why[:38]))
                continue
            truth, src = why, "human_label_loose"
        if len(truth) < a.min_chars:
            skipped.append((str(cid), why[:38]))
            continue
        rows.append({"case_id": str(cid), "truth": truth, "source": src,
                     "human_grade": str(ws.cell(r, 8).value or "")[:1],
                     "query": str(ws.cell(r, 3).value or "")})

    out = a.out or a.labels_xlsx.parent / "truth.jsonl"
    out.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                   encoding="utf-8")
    print(f"OK {out}  {len(rows)} 条权威口径")
    from collections import Counter
    by_src = Counter(r["source"] for r in rows)
    print(f"   其中带「正确说法」引导语 {by_src.get('human_label', 0)} 条，"
          f"宽松收进来的 {by_src.get('human_label_loose', 0)} 条（J 列直接就是答案、没写引导语）")
    print(f"   判定为过程描述、没收的：{len(skipped)} 条")
    for cid, head in skipped[:5]:
        print(f"     {cid:34} {head}")
    print(f"\n下一步：cli.py run 会自动读 data/labels/truth.jsonl；换路径用 --truth {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
