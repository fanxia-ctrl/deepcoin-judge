#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""复核表（人核过的 复核表.xlsx）→ 逐判据精确率/召回率 + 漏判清单 + 权威口径候选。

    python3 cli.py review data/labels/复核-xxx.xlsx [-o agreement.md] [--truth-out truth.jsonl]

口径：机器命中且人判「对」= TP；机器命中人判「错」= FP；人标漏判 = FN。
人填得很随意也能吃：是/否/✅/❌，漏判列里写代码加理由也行。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

from rubric import THEMES, THEME_BY_TIP, TIP_BY_CODE

OK = {"是", "对", "✅", "√", "y", "yes", "1"}
BAD = {"否", "错", "❌", "×", "x", "n", "no", "0"}
CODE = re.compile(r"neg_\d_\d")
WHY = re.compile(r"理由[:：]\s*(.+?)(?=\s*neg_\d_\d|\s*理由[:：]|$)", re.S)


def _norm(v) -> str | None:
    s = str(v or "").strip().lower()
    if not s:
        return None
    if s in OK:
        return "对"
    if s in BAD:
        return "错"
    return None


def read(xlsx: Path) -> list[dict]:
    try:
        from openpyxl import load_workbook
    except ImportError:
        sys.exit("需要 openpyxl：pip3 install openpyxl")
    ws = load_workbook(xlsx, data_only=True)["标注"]
    hdr = [str(c.value or "") for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(hdr)}
    need = ("case_id", "有漏判吗", "漏了哪条", "漏判说明", "备注")
    miss = [n for n in need if n not in col]
    if miss:
        sys.exit(f"复核表缺列：{'、'.join(miss)}")
    rows = []
    for r in range(2, ws.max_row + 1):
        cid = ws.cell(r, col["case_id"]).value
        if not cid:
            continue
        g = lambda n: ws.cell(r, col[n]).value if n in col else None
        hits = []
        for i in range(1, 9):
            cell = g(f"命中{i}")
            if not cell:
                continue
            code = str(cell).split()[0]
            if code not in TIP_BY_CODE:
                continue
            hits.append({"code": code, "mark": _norm(g(f"命中{i} 判对吗")),
                         "raw": str(g(f"命中{i} 判对吗") or ""), "text": str(cell)})
        free = " ".join(str(x) for x in (g("有漏判吗"), g("漏了哪条"), g("漏判说明"), g("备注")) if x)
        missed = sorted(set(CODE.findall(free)) & set(TIP_BY_CODE))
        # 只写了名字没写代码的
        if not missed:
            for t in TIP_BY_CODE.values():
                if t.name and t.name in free:
                    missed.append(t.code)
        whys = [w.strip() for w in WHY.findall(free)] or ([free.strip()] if missed else [])
        rows.append({"case_id": str(cid), "suite": str(g("suite") or ""),
                     "query": str(g("问题") or ""), "answer": str(g("回答") or ""),
                     "verdict": str(g("机器门禁") or ""), "hits": hits,
                     "missed": missed, "miss_text": free, "whys": whys,
                     "miss_why": str(g("漏判说明") or "").strip(),
                     "note": str(g("备注") or "")})
    return rows


def metrics(rows: list[dict]) -> dict:
    TP, FP, FN = Counter(), Counter(), Counter()
    unmarked = 0
    for r in rows:
        for h in r["hits"]:
            if h["mark"] == "对":
                TP[h["code"]] += 1
            elif h["mark"] == "错":
                FP[h["code"]] += 1
            else:
                unmarked += 1
        for c in r["missed"]:
            FN[c] += 1
    return {"TP": TP, "FP": FP, "FN": FN, "unmarked": unmarked}


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = (2 * p * r / (p + r)) if (p and r) else (0.0 if (p is not None and r is not None) else None)
    return p, r, f


def _pct(x):
    return "—" if x is None else f"{x:.0%}"


def write_report(rows: list[dict], m: dict, out: Path, src: str) -> None:
    TP, FP, FN = m["TP"], m["FP"], m["FN"]
    n = len(rows)
    L = [f"# 复核结果 · {src}", "",
         f"{n} 轮 · 机器命中 {sum(TP.values()) + sum(FP.values())} 条"
         f"（人核 {sum(TP.values()) + sum(FP.values())}，未核 {m['unmarked']}）· "
         f"人标漏判 {sum(FN.values())} 条", ""]

    # 二分一致
    mb = [bool(r["hits"]) for r in rows]
    hb = [any(h["mark"] == "对" for h in r["hits"]) or bool(r["missed"]) for r in rows]
    c = Counter(zip(mb, hb))
    po = (c[(True, True)] + c[(False, False)]) / n
    pa, pb = sum(mb) / n, sum(hb) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    k = (po - pe) / (1 - pe) if pe < 1 else 0
    L += ["## 有没有问题（二分）", "",
          f"机器有·人有 {c[(True, True)]} · 机器有·人无 {c[(True, False)]} · "
          f"机器无·人有 {c[(False, True)]} · 机器无·人无 {c[(False, False)]}",
          f"一致率 **{po:.0%}** · kappa **{k:.2f}**", ""]
    full = sum(1 for r in rows if all(h["mark"] == "对" for h in r["hits"]) and not r["missed"])
    L += [f"逐条完全一致的轮次 {full}/{n}", ""]

    L += ["## 逐判据", "", "| 判据 | 名称 | 命中 | TP | FP | FN | 精确率 | 召回率 | F1 |",
          "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    tt = tf = tn = 0
    for th in THEMES:
        for t in th.tips:
            tp, fp, fn = TP[t.code], FP[t.code], FN[t.code]
            tt, tf, tn = tt + tp, tf + fp, tn + fn
            p, r, f = _prf(tp, fp, fn)
            L.append(f"| `{t.code}` | {t.name} | {tp + fp} | {tp} | {fp} | {fn} | "
                     f"{_pct(p)} | {_pct(r)} | {_pct(f)} |")
    p, r, f = _prf(tt, tf, tn)
    L.append(f"| **合计** | | **{tt + tf}** | **{tt}** | **{tf}** | **{tn}** | "
             f"**{_pct(p)}** | **{_pct(r)}** | **{_pct(f)}** |")

    L += ["", "## 按主题", "", "| 主题 | 精确率 | 召回率 | TP | FP | FN |", "|---|---:|---:|---:|---:|---:|"]
    for th in THEMES:
        cs = [t.code for t in th.tips]
        tp, fp, fn = sum(TP[c] for c in cs), sum(FP[c] for c in cs), sum(FN[c] for c in cs)
        p, r, _ = _prf(tp, fp, fn)
        L.append(f"| {th.no} {th.name} | {_pct(p)} | {_pct(r)} | {tp} | {fp} | {fn} |")

    relabel, pure = split_fp(rows)
    L += ["", f"## 人判「错」的 {len(relabel) + len(pure)} 条", "",
          f"分两种：**换条** {len(relabel)} 条 —— 同一轮人又标了别的漏判，问题在、条目不对；"
          f"**纯过判** {len(pure)} 条 —— 人认为这里没问题。", ""]

    def _fp(items, title):
        L.append(f"### {title} × {len(items)}")
        L.append("")
        for r, h in items:
            first = h["text"].split("\n")[0]
            L.append(f"- **{r['case_id']}** `{first}`"
                     + (f" → 人改判 {'、'.join(r['missed'])}" if r["missed"] else ""))
            L.append(f"  - Q：{r['query'][:80]}")
            L.append(f"  - A：{r['answer'][:120].replace(chr(10), ' ')}")
            for ln in h["text"].split("\n")[1:]:
                L.append(f"  - {ln[:140]}")
            if r["note"]:
                L.append(f"  - 人的备注：{r['note'][:160]}")
        if not items:
            L.append("没有。")
        L.append("")
    _fp(relabel, "换条")
    _fp(pure, "纯过判")

    L += ["", "## 漏判（人说该命中没命中）", ""]
    by = defaultdict(list)
    for r in rows:
        for c in r["missed"]:
            by[c].append(r)
    for c, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        L.append(f"### `{c}` {TIP_BY_CODE[c].name} × {len(rs)}")
        L.append("")
        for r in rs:
            why = r["whys"][0][:120] if r["whys"] else r["miss_text"][:120]
            hit = "、".join(h["code"] for h in r["hits"]) or "无"
            L.append(f"- **{r['case_id']}**（机器 {r['verdict']}，已命中 {hit}）：{why}")
        L.append("")
    out.write_text("\n".join(L) + "\n", encoding="utf-8")


# 完整性类的评语不是事实，不能当口径
NOT_FACT = re.compile(r"^(可以给|可以告知|需要|没有帮助|没有告知|应该|建议|可以先|需给出|需告知|只说)")


def split_fp(rows: list[dict]) -> tuple[list, list]:
    """人判「错」有两种：换条（同一轮又标了别的漏判 —— 问题在但条目不对）和纯过判。"""
    relabel, pure = [], []
    for r in rows:
        for h in r["hits"]:
            if h["mark"] == "错":
                (relabel if r["missed"] else pure).append((r, h))
    return relabel, pure


def truth_candidates(rows: list[dict]) -> list[dict]:
    """从复核里抽权威口径，只取像事实的：
    - 人标 neg_1_1 漏判且理由是在陈述平台事实（不是「可以告知用户…」这类完整性评语）
    - 人把 neg_1_1/neg_5_1/neg_5_2 的命中判「错」、同一轮没标任何漏判、且写了备注
      → 备注就是对那句断言的确认（如「其他平台的 vip 不能迁移到我们平台」）
    判「错」但同一轮又标了别的漏判的，是换条不是确认属实，不抽。"""
    out = []
    for r in rows:
        parts = []
        if "neg_1_1" in r["missed"]:
            cands = list(r["whys"])
            if r["miss_why"] and not CODE.search(r["miss_why"]):
                cands.append(r["miss_why"])          # 「漏判说明」列单独写的一句
            for w in cands:
                w = re.sub(r"neg_\d_\d\s*(T\d\s*)?[^：:\s]*[：:]?", "", w).strip(" /\n")
                w = re.sub(r"[，,]?\s*AI回复错误[。.]?$", "", w).strip()
                if len(w) >= 6 and not NOT_FACT.match(w):
                    parts.append(w)
        if not r["missed"] and r["note"]:
            for h in r["hits"]:
                if h["mark"] == "错" and h["code"] in ("neg_1_1", "neg_5_1", "neg_5_2"):
                    parts.append(r["note"].strip())
                    break
        parts = [p.strip("有 /") for p in parts if p]
        parts = [p for p in dict.fromkeys(parts)
                 if len(p) >= 6 and not any(p != q and p in q for q in parts)]   # 去掉被包含的碎片
        if parts:
            out.append({"case_id": r["case_id"], "query": r["query"],
                        "truth": "；".join(parts), "source": "product_review"})
    return out


def merge_truth(cands: list[dict], truth_path: Path) -> tuple[int, int]:
    old = {}
    if truth_path.exists():
        for l in truth_path.read_text(encoding="utf-8").splitlines():
            if l.strip():
                o = json.loads(l)
                old[o["case_id"]] = o
    added = updated = 0
    for c in cands:
        if c["case_id"] in old:
            o = old[c["case_id"]]
            if c["truth"] not in (o.get("truth") or ""):
                o["truth"] = (o.get("truth") or "").rstrip("。") + "。" + c["truth"]
                o["source"] = f"{o.get('source', '')}+product_review".strip("+")
                updated += 1
        else:
            old[c["case_id"]] = c
            added += 1
    truth_path.write_text("".join(json.dumps(v, ensure_ascii=False) + "\n"
                                  for v in old.values()), encoding="utf-8")
    return added, updated


def main(review_xlsx: Path, out: Path | None = None, truth_out: Path | None = None) -> int:
    review_xlsx = Path(review_xlsx)
    rows = read(review_xlsx)
    if not rows:
        print("复核表里没有 case")
        return 1
    m = metrics(rows)
    dest = Path(out) if out else review_xlsx.with_suffix(".agreement.md")
    write_report(rows, m, dest, review_xlsx.name)
    TP, FP, FN = m["TP"], m["FP"], m["FN"]
    p, r, f = _prf(sum(TP.values()), sum(FP.values()), sum(FN.values()))
    print(f"OK {dest}\n   {len(rows)} 轮 · 命中 {sum(TP.values()) + sum(FP.values())} · "
          f"TP {sum(TP.values())} FP {sum(FP.values())} FN {sum(FN.values())} · "
          f"精确率 {_pct(p)} 召回率 {_pct(r)} F1 {_pct(f)}")
    if m["unmarked"]:
        print(f"   {m['unmarked']} 条命中没核（判对吗 为空或看不懂）")
    if truth_out:
        cands = truth_candidates(rows)
        added, updated = merge_truth(cands, Path(truth_out))
        print(f"OK {truth_out}\n   从复核抽出权威口径 {len(cands)} 条：新增 {added}，并入已有 {updated}")
    return 0
