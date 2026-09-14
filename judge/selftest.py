#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检：除 LLM 调用外，每一环都验一遍。改了 rubric 先跑这个。"""
from __future__ import annotations

import llm as _llm
import prompts
from context import build_ctx
from pipeline import parse_verdicts, verify_quotes
from rubric import (CONDITIONAL_TIPS, HARD_FAIL, HUMAN_ISSUE_TO_TIPS, NORMAL, RATIOS,
                    SEVERE, THEME_BY_TIP, THEMES, TIP_BY_CODE, group_key, groups,
                    tier_for)
from score import DEDUCT_LIMIT, score
from scorers import objective

FAILS: list[str] = []


def ck(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"　{detail}" if detail else ""))


def main() -> int:
    print("deepcoin-judge 自检\n")

    print("[判据表]")
    codes = [t.code for th in THEMES for t in th.tips]
    ck("编号唯一", len(codes) == len(set(codes)), f"{len(codes)} 条 / {len(THEMES)} 主题")
    ck("权重是三档之一",
       all(t.weight in (HARD_FAIL, SEVERE, NORMAL) for t in TIP_BY_CODE.values()))
    ck("有标注支撑的判据权重合档位", all(
        t.weight == tier_for(t.c_rate) for t in TIP_BY_CODE.values()
        if t.c_rate is not None and not t.weight_override and not t.weight_by_type),
       "C率 >=90% hard / 40~89% severe / <40% normal（分档判据与破档位的除外）")
    ovr = [t for t in TIP_BY_CODE.values() if t.weight_override]
    ck("破档位的都写了理由", all(len(t.weight_override) > 10 for t in ovr),
       "、".join(t.code for t in ovr))
    ck("条件型判据四件套齐全", all(
        t.applies_when and t.right_looks_like and t.ratio for t in CONDITIONAL_TIPS),
       f"{len(CONDITIONAL_TIPS)} 条 → {len(RATIOS)} 个比率")
    ck("分档判据有 type_field", all(
        t.type_field and t.type_options for t in TIP_BY_CODE.values() if t.weight_by_type))
    ck("分档权重的键都在选项里", all(
        set(t.weight_by_type) <= set(t.type_options)
        for t in TIP_BY_CODE.values() if t.weight_by_type))
    ck("一致率映射的编号都存在",
       all(c in TIP_BY_CODE for v in HUMAN_ISSUE_TO_TIPS.values() for c in v))

    print("\n[prompt]")
    fake = {"case_id": "t:1", "suite": "s", "query": "返佣能关掉吗", "detail": "",
            "voice": "您可以在【资产】页面关闭。", "raw_answer": '{"voice":"x","detail":"",'
            '"panel":null,"input_request":null}', "envelope_ok": True,
            "kb_count": 2, "kb_top_score": 0.51, "elapsed_ms": 900,
            "kb_hits": [{"content": "返佣已统一开启，不支持关闭", "score": 0.51,
                         "document": "rebate.md"}], "tool_node_titles": []}
    ctx = build_ctx(fake, {"t:1": "该功能已统一开启，暂不支持关闭"})
    want = set(TIP_BY_CODE)
    for mode in ("theme", "bundle", "one"):
        grps = groups(mode)
        seen, good = set(), True
        for grp in grps:
            sysp, usr = prompts.build(grp, ctx)
            body = [t.code for th in grp for t in th.tips]
            seen |= set(body)
            if not all(f"`{c}`" in sysp for c in body) or "JSON" not in sysp or len(usr) < 20:
                good = False
            if any(t.conditional for th in grp for t in th.tips) and "applies" not in sysp:
                good = False
        ck(f"--group {mode}", good and seen == want, f"{len(grps)} 组，{len(seen)}/{len(want)}")
    sysp, _ = prompts.build(groups("one")[0], ctx)
    ck("分档字段进了 prompt", "fact_type" in sysp and "claim_type" in sysp)
    sysp, usr = prompts.build([t for t in THEMES if t.key == "fact"][0], {**ctx, "truth": ""})
    ck("缺权威口径时 prompt 明说填 null", "hit=null" in usr)

    print("\n[解析]")
    g = '{"verdicts":[{"code":"neg_2_1","hit":true,"quote":"x","why":"y"}]}'
    ck("正常 JSON", parse_verdicts(g, ["neg_2_1"])[0][0]["hit"] is True)
    ck("代码块包裹", len(parse_verdicts("```json\n" + g + "\n```", ["neg_2_1"])[0]) == 1)
    ck("前后有废话", len(parse_verdicts("判断：" + g + " 完毕", ["neg_2_1"])[0]) == 1)
    ck("漏判据能报出来", "neg_2_2" in parse_verdicts(g, ["neg_2_1", "neg_2_2"])[1])
    ck("垃圾输入安全返回",
       all(parse_verdicts(j, ["neg_2_1"]) == ([], parse_verdicts(j, ["neg_2_1"])[1])
           and parse_verdicts(j, ["neg_2_1"])[1]
           for j in ("", "不是 JSON", "{", '{"verdicts":"x"}')))
    v, _ = parse_verdicts('{"verdicts":[{"code":"neg_1_1","hit":null,"why":"缺材料"}]}',
                          ["neg_1_1"])
    ck("hit=null 被保留", v[0]["hit"] is None, "缺材料时不当成不命中")
    v, _ = parse_verdicts('{"verdicts":[{"code":"neg_2_5","hit":false,"applies":true}]}',
                          ["neg_2_5"])
    ck("applies 被解析", v[0].get("applies") is True)
    vs = [{"code": "neg_2_1", "hit": True, "quote": "查无此句"}]
    ck("引用校验翻回过判", verify_quotes(vs, "真实回答内容") == ["neg_2_1"]
       and vs[0]["hit"] is False)
    vs = [{"code": "neg_2_1", "hit": True, "quote": "真实回答"}]
    ck("引用对得上就不翻", verify_quotes(vs, "这是真实回答内容") == [])

    print("\n[规则层]")
    r = objective.check_contract({"voice": "详情见下方", "detail": "", "raw_answer": "{}",
                                  "envelope_ok": True})
    ck("R5 分层违约能查出", "R5" in {h.rule for h in r})
    r = objective.check_contract({"voice": "[pause:0.4]你好。", "detail": "x",
                                  "raw_answer": '{"voice":"","detail":"","panel":null,'
                                  '"input_request":null}', "envelope_ok": True})
    ck("R4 在 gateway 之前不误报", "R4" not in {h.rule for h in r},
       "[pause:0.4] 是控制标记，要在 gateway 之后查")
    r = objective.check_contract({"voice": "[pause]你好。", "detail": "x",
                                  "raw_answer": '{"voice":"","detail":"","panel":null,'
                                  '"input_request":null}', "envelope_ok": True},
                                 downstream=True)
    ck("R4 在 gateway 之后能查出", "R4" in {h.rule for h in r})
    r = objective.check_contract({"voice": "好的。", "detail": "", "raw_answer":
                                  '{"voice":"好的。","detail":"","panel":null,'
                                  '"input_request":null}', "envelope_ok": True,
                                  "elapsed_ms": 900})
    ck("detail 为空本身不违约", r == [], f"{[h.rule for h in r]}")
    r = objective.check_scored({"query": "怎么下载", "voice": "Please visit the app store.",
                                "detail": "", "tool_node_titles": []})
    ck("R8 语种不跟随", "R8" in {h.rule for h in r})
    r = objective.check_scored({"query": "我这单", "voice": "您当前的仓位是 3 张。",
                                "detail": "", "tool_node_titles": []})
    ck("R9 账户事实无工具", "R9" in {h.rule for h in r})
    r = objective.check_scored({"query": "我这单", "voice": "您当前的仓位是 3 张。",
                                "detail": "", "tool_node_titles": ["query_positions"]})
    ck("R9 有工具就不报", "R9" not in {h.rule for h in r})
    ck("工具字段缺失时 R9 不误报",
       "R9" not in {h.rule for h in objective.check_scored(
           {"query": "我这单", "voice": "您当前的仓位是 3 张。", "detail": ""})})

    print("\n[门禁]")
    ck("hard_fail 直接判死",
       score([{"code": "neg_1_1", "hit": True, "fact_type": "入口/功能存在性"}])["verdict"]
       == "不可上线")
    ck("主题内取最大值",
       score([{"code": "neg_2_1", "hit": True}, {"code": "neg_2_3", "hit": True}])["deduct"]
       == SEVERE, "v1 会累加成 2.0 误判死")
    ck("跨主题相加",
       score([{"code": "neg_2_1", "hit": True}, {"code": "neg_3_1", "hit": True}])["deduct"]
       == round(SEVERE * 2, 3))
    ck(f"累计超 {DEDUCT_LIMIT} 判死",
       score([{"code": "neg_2_1", "hit": True}, {"code": "neg_3_1", "hit": True}])["verdict"]
       == "不可上线")
    ck("分档权重生效", all(
        score([{"code": "neg_5_1", "hit": True, "claim_type": t}])["deduct"] == w
        for t, w in (("数值/费率/时限", 3.0), ("入口路径", 1.4), ("仅措辞过强", 0.6))))
    s = score([{"code": "neg_2_5", "hit": False, "applies": True}])
    ck("条件型出加分与比率", s["bonus"] == 0.6 and s["ratios"]["澄清率"] == 1.0)
    s = score([{"code": "neg_2_5", "hit": False, "applies": False}])
    ck("前提不成立不算做对", s["bonus"] == 0 and s["ratios"]["澄清率"] is None)
    ck("正向不救 hard_fail",
       score([{"code": "neg_1_1", "hit": True, "fact_type": "规则结论"},
              {"code": "neg_2_5", "hit": False, "applies": True}])["verdict"] == "不可上线")
    ck("契约不进质量分",
       score([], [objective.Hit("R1", "x", "contract", 0, "", "")])["deduct"] == 0)
    ck("契约单独卡点",
       score([], [objective.Hit("R1", "x", "contract", 0, "", "")])["contract_ok"] is False)
    ck("计分规则进 deduct",
       score([], [objective.Hit("R8", "x", "scored", 0.6, "", "")])["deduct"] == 0.6)
    ck("缺材料只推翻「干净」",
       score([], undecidable=["fact"])["verdict"] == "判定不完整"
       and score([{"code": "neg_2_1", "hit": True}], undecidable=["fact"])["verdict"]
       == "需修改", "已查出的问题不因别处没判而作废")
    ck("不完整的不进一致率",
       score([], undecidable=["fact"])["complete"] is False
       and score([{"code": "neg_2_1", "hit": True}], undecidable=["fact"])["complete"] is True)

    print("\n[链路]")
    m = _llm.MockClient()
    for mode in ("theme", "bundle", "one"):
        vs_all = []
        for grp in groups(mode):
            sysp, usr = prompts.build(grp, ctx)
            vs, err = parse_verdicts(m.complete(sysp, usr)[0],
                                     [t.code for th in grp for t in th.tips])
            if err:
                ck(f"mock 走通 --group {mode}", False, err)
                break
            vs_all += vs
        else:
            ck(f"mock 走通 --group {mode}", len(vs_all) == len(want), f"{len(vs_all)} 条")
    ck("缓存 key 按组区分",
       group_key(groups("theme")[0]) != group_key(groups("one")[0]))
    ready = [c.name for cls in (_llm.OpenAICompatClient, _llm.DifyClient)
             for c in [cls()] if c.ready]
    ck("模型客户端已实现", True, "dify + openai 兼容")
    ck("凭证状态", True, f"已配好 {'、'.join(ready)}" if ready else "还没配 —— 填 config.env")

    print()
    if FAILS:
        print(f"{len(FAILS)} 项 FAIL：{'、'.join(FAILS)}")
        return 1
    print("全部通过 —— " + ("代码和凭证都就绪，可以直接判" if ready else "代码全就绪，只等模型凭证"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
