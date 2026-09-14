#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预检：把除 LLM 之外的每一环在真实数据上走一遍，不发任何请求。"""
from __future__ import annotations

import llm as _llm
import prompts
from context import build_ctx, missing_materials
from pipeline import parse_verdicts, verify_quotes
from rubric import THEMES, TIP_BY_CODE, group_label, groups
from score import score
from scorers import objective


def run(turns: list[dict], truth: dict, a) -> int:
    ok = True

    def chk(name, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"　{detail}" if detail else ""))

    print("预检（不发任何请求）\n")
    print("[模型配置]")
    print(_llm.status())
    print()

    chk("读到成功轮次", bool(turns), f"{len(turns)} 轮")
    if not turns:
        return 1
    need = ("case_id", "query", "voice", "raw_answer", "kb_hits", "kb_count")
    miss = {k for t in turns for k in need if t.get(k) is None and k != "kb_top_score"}
    chk("必需字段齐全", not miss, f"缺 {miss}" if miss else "、".join(need))
    no_tool = sum(1 for t in turns if t.get("tool_node_titles") is None
                  and t.get("tool_names") is None)
    chk("工具记录字段", True,
        f"{len(turns) - no_tool}/{len(turns)} 轮有 —— 没有时 R9/R10 不触发，neg_3_1 以 LLM 为主")

    themes = THEMES
    grps = groups(a.group, themes)
    chk(f"判据分组（--group {a.group}）", True,
        f"{len(grps)} 组：" + " | ".join(group_label(g) for g in grps))
    seen, total_chars = set(), 0
    for grp in grps:
        ctx = build_ctx(turns[0], truth, a.evidence_chars)
        try:
            sysp, usr = prompts.build(grp, ctx)
            total_chars += len(sysp) + len(usr)
            body = [t.code for th in grp for t in th.tips]
            seen |= set(body)
            bad = [c for c in body if f"`{c}`" not in sysp]
            cond = [t for th in grp for t in th.tips if t.conditional]
            typed = [t for th in grp for t in th.tips if t.type_field]
            extra = []
            if cond and "applies" not in sysp:
                extra.append("缺 applies 说明")
            for t in typed:
                if t.type_field not in sysp:
                    extra.append(f"缺 {t.type_field}")
            chk(f"  [{group_label(grp)}]", not bad and not extra and len(usr) > 20,
                f"system {len(sysp)} + user {len(usr)} 字，{len(body)} 条判据"
                f"（条件型 {len(cond)}）" + (f"　{bad or extra}" if bad or extra else ""))
        except Exception as exc:
            chk(f"  [{group_label(grp)}]", False, f"{type(exc).__name__}: {exc}")
    want = {t.code for th in themes for t in th.tips}
    chk("分组没漏判据", seen == want,
        f"{len(seen)}/{len(want)}" + ("（T1 已按 --no-truth 摘掉）"
                                      if len(want) < len(TIP_BY_CODE) else ""))

    m = _llm.MockClient()
    probe = True
    for grp in grps:
        sysp, usr = prompts.build(grp, build_ctx(turns[0], truth, a.evidence_chars))
        vs, err = parse_verdicts(m.complete(sysp, usr)[0],
                                 [t.code for th in grp for t in th.tips])
        if err:
            probe = False
            print(f"       解析失败 [{group_label(grp)}]：{err}")
    chk("解析器能吃下所有组的输出", probe)
    junk = ['```json\n{"verdicts":[]}\n```', '废话{"verdicts":[]}废话', "不是 JSON", ""]
    chk("解析器容忍代码块/废话/空回复",
        all(isinstance(parse_verdicts(j, [])[0], list) for j in junk))

    vs = [{"code": "neg_2_1", "hit": True, "quote": "不存在的引用"}]
    chk("引用校验能翻回过判", verify_quotes(vs, "真实回答") == ["neg_2_1"])

    rc = objective.check_contract(turns[0])
    rs = objective.check_scored(turns[0])
    chk("规则层可算", isinstance(rc, list) and isinstance(rs, list),
        f"契约命中 {[h.rule for h in rc]}　计分命中 {[h.rule for h in rs]}")
    cl = objective.check_cluster(turns)
    chk("簇级检查可算", isinstance(cl, list), f"检出 {len(cl)} 处口径冲突")

    chk("门禁：hard_fail 直接判死",
        score([{"code": "neg_1_1", "hit": True, "fact_type": "入口/功能存在性"}])["verdict"] == "不可上线")
    chk("门禁：主题内取最大值",
        score([{"code": "neg_2_1", "hit": True}, {"code": "neg_2_3", "hit": True}])["deduct"] == 1.4,
        "同主题两条只记最重的那条")
    chk("门禁：缺材料标不完整",
        score([], undecidable=["fact"])["verdict"] == "判定不完整")

    if getattr(a, "no_truth", False):
        print("  OK   权威口径　不读（--no-truth）：T1 以召回切片为口径，14 条判据照判，"
              "无召回的轮次 T1/T5 一起不判")
    else:
        have_truth = sum(1 for t in turns if str(t.get("case_id")) in truth)
        chk("权威口径覆盖", True,
            f"{have_truth}/{len(turns)} 轮 —— 其余轮 T1 输出「无法判定」，不算 0 分")
    no_ev = sum(1 for t in turns
                if "evidence" in missing_materials(build_ctx(t, truth, a.evidence_chars)))
    chk("证据覆盖", True, f"{len(turns) - no_ev}/{len(turns)} 轮有召回 —— 其余轮 T1/T5 不判")

    n_calls = len(turns) * len(grps)
    print(f"\n  接上模型后：约 {n_calls} 次调用（{len(turns)} 轮 × {len(grps)} 组），"
          f"单次 prompt 平均 {total_chars // max(len(grps), 1):,} 字")
    print(f"  换 --group theme 是 {len(turns) * len(THEMES)} 次，--group one 是 {len(turns)} 次")

    ready = any(c().ready for c in (_llm.OpenAICompatClient, _llm.DifyClient))
    print("\n" + ("预检有 FAIL，先修上面那些" if not ok else
                  "预检通过，模型也配好了 —— 换 run 子命令开始判" if ready else
                  "预检通过。**除模型凭证外全部就绪** —— 填 config.env 后换 run 子命令开跑"))
    return 0 if ok else 1
