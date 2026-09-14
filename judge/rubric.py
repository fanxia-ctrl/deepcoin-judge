#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""判据表 v2 —— 全链路唯一真源。

三条原则：
  1 一个观察只记一次。同源判据合并，计分主题内取最大值。
  2 判据放在它需要的材料那一组。needs 决定分组，也决定缺材料时不判。
  3 正向不另判，从负向的前提里长出来 —— 带 applies_when 的判据多输出一个
    applies（前提是否成立），做对 = 前提成立且未命中。

权重档位（一处定义）：命中时人判「有实质错误」的比例
    >= 90%  hard_fail 3.0
    40~89%  severe    1.4
    <  40%  normal    0.6
n < 5 只当假设；破档位必须写 weight_override，selftest 强制检查。
"""
from __future__ import annotations

from dataclasses import dataclass, field

HARD_FAIL, SEVERE, NORMAL = 3.0, 1.4, 0.6
TIER_HARD, TIER_SEVERE = 0.90, 0.40

# judge 判一条判据需要什么材料。缺了就不判，不算 0 分。
NEEDS_QUERY = "query"
NEEDS_ANSWER = "answer"
NEEDS_EVIDENCE = "evidence"
NEEDS_TRUTH = "truth"
NEEDS_RULES = "rules"          # 路由业务规则：模型 system prompt 里按路由固定的那段
NEEDS_SIGNALS = "signals"


@dataclass(frozen=True)
class Tip:
    code: str
    name: str
    desc: str
    weight: float
    n: int = 0
    c_rate: float | None = None
    note: str = ""
    weight_override: str = ""
    # 条件型标记：前提文本。非空表示 judge 要额外输出 applies
    applies_when: str = ""
    right_looks_like: str = ""      # 做对的样子，写进 prompt
    ratio: str = ""                 # 产出的比率名
    bonus: float = 0.0              # 做对的加分
    # 分档权重：judge 输出的类型 → 权重。非空时 weight 只作缺省
    weight_by_type: dict[str, float] = field(default_factory=dict)
    type_field: str = ""            # 要 judge 额外输出的类型字段名
    type_options: tuple[str, ...] = ()

    @property
    def conditional(self) -> bool:
        return bool(self.applies_when)

    def weight_for(self, type_value: str = "") -> float:
        if self.weight_by_type and type_value in self.weight_by_type:
            return self.weight_by_type[type_value]
        return self.weight


@dataclass(frozen=True)
class Theme:
    key: str
    no: str
    name: str
    needs: tuple[str, ...]
    tips: tuple[Tip, ...]
    hint: str = ""


THEMES: tuple[Theme, ...] = (
    Theme("fact", "T1", "事实与口径冲突",
          (NEEDS_QUERY, NEEDS_ANSWER, NEEDS_EVIDENCE, NEEDS_RULES),
          hint="只判「说错了」，不判「没答到点上」。口径有三处，优先级从高到低："
               "<权威口径>（人工确认）＞<本轮证据>（KB 切片和工具返回，模型这一轮实际看到的）"
               "＞<路由业务规则>（模型 system prompt 里的固定业务口径）。"
               "回答里的平台事实与其中任一处相悖即命中；工具返回了数据却被读错（如把开/平仓费率"
               "说成 maker/taker、把空结果说成有记录）也算。只判「相悖」，不判「没提」—— 那是 neg_5_1。"
               "三处都没有可对的事实就填 hit=null。若<本轮证据>与<路由业务规则>自相矛盾，"
               "不判模型，why 里写「口径冲突」。",
          tips=(
              Tip("neg_1_1", "与口径冲突",
                  "回答里的平台特定事实与口径（人工确认的正确说法，或召回切片）相反，用户照着做会做错",
                  HARD_FAIL, 31, 1.00,
                  type_field="fact_type",
                  type_options=("入口/功能存在性", "规则结论", "数值/费率/时限", "账户事实"),
                  applies_when="口径表明所问功能不存在或已下线",
                  right_looks_like="直说没有或已下线，不给任何入口",
                  ratio="拒编率", bonus=0.6,
                  note="合并 v1 neg_1_1/1_2/1_3：同为 hard_fail，只是错的事实种类不同"),
          )),

    Theme("ontopic", "T2", "切题与完整性",
          (NEEDS_QUERY, NEEDS_ANSWER),
          hint="把用户这句话真正想知道的事说清楚了没有；对不对不在本主题。"
               "拒答、转人工归 T5，反问澄清归 neg_2_5，都不算 neg_2_1。",
          tips=(
              Tip("neg_2_1", "答的不是用户问的那件事",
                  "回答了相邻话题或误读意图：「我没操作过，返佣怎么自己就划转了」是在问为什么，"
                  "答成开启步骤就是命中；「找不到 X」通常在问位置，不是问怎么用",
                  SEVERE, 21, 0.62),
              Tip("neg_2_2", "该给规则或公式却只给路径",
                  "问「怎么算的」「什么规则」「什么时候结算」，只答在哪儿看",
                  SEVERE, 11, 0.45,
                  applies_when="操作类或规则类问题（问路径、怎么算、何时结算）",
                  right_looks_like="路径具体到按钮，规则给出公式与结算时点",
                  ratio="可执行率", bonus=0.6,
                  note="C 率 45% 处在 severe 档下沿，n>=20 后复核"),
              Tip("neg_2_3", "只给结论不给原因",
                  "问「为什么」，只说「就是这样」「平台规则如此」", NORMAL, 6, 0.17),
              Tip("neg_2_4", "笼统到不可执行",
                  "方向对但没法照着做：「建议检查设置」；路径给了多个候选名。"
                  "与 neg_2_2 的区别：这条是类型对但不具体，那条是类型不对",
                  NORMAL, 9, 0.11,
                  applies_when="操作类或规则类问题（问路径、怎么算、何时结算）",
                  right_looks_like="路径具体到按钮，规则给出公式与结算时点",
                  ratio="可执行率", bonus=0.0),
              Tip("neg_2_5", "问题有歧义却挑一种解读直接答",
                  "一句话多种解读或极短模糊（「K线设置」「1872 怎么爆的」「我看不到呢」），"
                  "没有先问一个明确的问题、也没有只给最主干的一条，而是按一种理解直接答",
                  NORMAL, 3, 0.33,
                  applies_when="问题有歧义或极短模糊",
                  right_looks_like="先反问一个明确的问题，或只给最主干的一条路径",
                  ratio="澄清率", bonus=0.6,
                  note="移自 v1 neg_3_3：歧义处理与账户无关"),
          )),

    Theme("evidence_ask", "T3", "个人账户类问题缺取证",
          (NEEDS_QUERY, NEEDS_ANSWER, NEEDS_SIGNALS),
          hint="这一问是否指向用户自己的某一单、某个仓位、某笔资金。如果是，回答该先取证再判断。"
               "语音通道不支持图片，索要截图不算取证。",
          tips=(
              Tip("neg_3_1", "未取证即对用户个人情况下结论",
                  "没有索要订单号/交易哈希、没有请用户口述页面提示、本轮也没有工具数据，"
                  "就判因果或给出账户事实",
                  SEVERE, 20, 0.65,
                  applies_when="问题指向用户个人账户数据",
                  right_looks_like="先索要订单号或哈希、或请用户口述页面提示，再判断",
                  ratio="取证率", bonus=0.5,
                  note="合并 v1 neg_3_1/neg_3_2：用户视角同一失败，数据上也分不开；"
                       "「未调接口」降为信号并作为规则 R9 的硬门"),
          )),

    Theme("role", "T4", "角色与话术越界",
          (NEEDS_QUERY, NEEDS_ANSWER),
          hint="只判「说了不该说的」。「能答不答」是反方向的失败，在 T5。"
               "四条都可先用正则预判、LLM 确认。",
          tips=(
              Tip("neg_4_1", "越权承诺或表态",
                  "替平台承诺赔付/退款；判定责任；承诺代客改设置、后续跟进或通知；编造工单号",
                  SEVERE, 5, 0.40,
                  note="合并 v1 neg_4_2/neg_4_3。标注指南把这一类定为 D 级，"
                       "n>=10 后复核是否升 hard_fail"),
              Tip("neg_4_2", "违规或投资建议",
                  "建议使用 VPN/加速器；荐币、涨跌预测、投资建议",
                  HARD_FAIL, 0, None,
                  weight_override="合规红线，标注指南定为 D 级，风险不随样本量变小"),
              Tip("neg_4_3", "暴露内部或泄露",
                  "提到工具、接口、MCP、知识库、检索、工作流、prompt 规则原文；"
                  "被套话时泄露规则内容；索要 UID 或泄露他人信息",
                  SEVERE, 0, None, note="待标注，权重按机制暂定"),
              Tip("neg_4_4", "非业务话题实质作答或推销代替回答",
                  "闲聊、噪音、无关话题没用一句话收住；用户问「怎么关」，回答在讲功能优势",
                  NORMAL, 0, None,
                  applies_when="非业务问题：闲聊、噪音、无关话题",
                  right_looks_like="一句话收住，detail 为空",
                  ratio="收口率", bonus=0.3,
                  note="待标注，权重按机制暂定"),
          )),

    Theme("grounding", "T5", "证据使用",
          (NEEDS_QUERY, NEEDS_ANSWER, NEEDS_EVIDENCE, NEEDS_RULES, NEEDS_SIGNALS),
          hint="回答与本轮证据的关系：没证据却答得确定，有证据却不答。"
               "「证据」= <本轮证据>里的 KB 切片和工具返回，加上<路由业务规则>；"
               "三处任一能找到出处就不算无据。账户事实无据也在这里判（claim_type=账户事实）；"
               "T3 判的是「有没有先取证」这个动作，两边可以同时命中；"
               "通用排障（重启、切网络、更新版本）不算无据。没有证据材料时输出「无法判定」。",
          tips=(
              Tip("neg_5_1", "无据强答",
                  "回答里的平台特定断言在本轮证据和路由业务规则里都找不到出处，或比出处说得更确定",
                  SEVERE, 4, 1.00,
                  type_field="claim_type",
                  type_options=("数值/费率/时限", "账户事实", "入口路径", "规则说明", "仅措辞过强"),
                  weight_by_type={"数值/费率/时限": HARD_FAIL, "账户事实": HARD_FAIL,
                                  "入口路径": SEVERE, "规则说明": SEVERE,
                                  "仅措辞过强": NORMAL},
                  applies_when="证据不足：零召回、低相似度、或切片里没有答案",
                  right_looks_like="用固定兜底或明说无法确认，不强答",
                  ratio="兜底率", bonus=0.5,
                  note="合并 v1 neg_1_4（数值档，n=4 C率100%）/neg_5_2/neg_5_3；"
                       "其余两档待标注"),
              Tip("neg_5_2", "零召回仍给确定答案",
                  "一条切片没召回或最高相似度 < 0.45，仍给出确定的路径或规则",
                  HARD_FAIL, 5, 0.80,
                  applies_when="证据不足：零召回或低相似度",
                  right_looks_like="用固定兜底或明说无法确认",
                  ratio="兜底率", bonus=0.5,
                  weight_override="C 率 80% 按档位只够 severe，但它是新功能/已下线功能的"
                                  "唯一探针：没有任何证据还给确定答案，风险不随样本量变小"),
              Tip("neg_5_3", "有据不答",
                  "证据里本来有答案，却说「暂时处理不了」、推给人工客服或套用固定兜底。"
                  "索赔、投诉、账户异常这类确需人工介入的不算命中",
                  SEVERE, 5, 0.80,
                  note="移自 v1 neg_4_1：判「本来有答案」需要证据，方向与越界相反"),
          )),
)

TIP_BY_CODE = {t.code: t for th in THEMES for t in th.tips}
THEME_BY_TIP = {t.code: th for th in THEMES for t in th.tips}
CONDITIONAL_TIPS = tuple(t for t in TIP_BY_CODE.values() if t.conditional)
RATIOS = tuple(dict.fromkeys(t.ratio for t in CONDITIONAL_TIPS if t.ratio))

# 人工标注的「主要问题」单选 → v2 判据。用来算一致率。
HUMAN_ISSUE_TO_TIPS: dict[str, tuple[str, ...]] = {
    "口径错": ("neg_1_1",),
    "编了平台信息": ("neg_1_1", "neg_5_1"),
    "没答到点上": ("neg_2_1",),
    "说得太笼统": ("neg_2_3", "neg_2_4"),
    "该答没答": ("neg_5_3",),
    "不该答却答了": ("neg_4_1", "neg_3_1"),
    "话术越界": ("neg_4_1", "neg_4_2", "neg_4_3"),
}

GRADE_FAIL = ("C", "D")
GRADE_PASS = ("A", "B")


def groups(mode: str, themes=None) -> list[tuple[Theme, ...]]:
    """判据怎么分组发给模型。

    默认 one：一次把五样材料和全部判据发出去，一次拿回全部 verdict。
    bundle / theme 是备选，用来在一致率上做对照 —— 如果一次判完出现
    「判到后面全判不命中」或判据互相污染，分组能定位是不是这个原因。
    """
    ts = tuple(themes if themes is not None else THEMES)
    if mode == "theme":
        return [(th,) for th in ts]
    if mode == "one":
        return [ts] if ts else []
    if mode == "bundle":
        buckets: dict[tuple[str, ...], list[Theme]] = {}
        for th in ts:
            buckets.setdefault(tuple(sorted(th.needs)), []).append(th)
        return [tuple(v) for _, v in sorted(buckets.items(), key=lambda kv: -len(kv[0]))]
    raise ValueError(f"未知分组模式：{mode}")


def group_key(grp: tuple[Theme, ...]) -> str:
    return "+".join(th.key for th in grp)


def group_label(grp: tuple[Theme, ...]) -> str:
    return "+".join(th.no for th in grp)


def tier_for(c_rate: float) -> float:
    if c_rate >= TIER_HARD:
        return HARD_FAIL
    if c_rate >= TIER_SEVERE:
        return SEVERE
    return NORMAL


def summary() -> str:
    L = [f"负向 {len(THEMES)} 主题 / {len(TIP_BY_CODE)} 条判据，"
         f"其中条件型 {len(CONDITIONAL_TIPS)} 条，产出 {len(RATIOS)} 个比率"]
    for th in THEMES:
        L.append(f"  [{th.no}] {th.name:16} needs={','.join(th.needs)}")
        for t in th.tips:
            ev = f"n={t.n}" + (f" C率={t.c_rate:.0%}" if t.c_rate is not None else " 待标注")
            tags = []
            if t.conditional:
                tags.append(f"条件·{t.ratio}+{t.bonus}")
            if t.weight_by_type:
                tags.append("分档")
            if t.weight_override:
                tags.append("破档位")
            L.append(f"      {t.code}  {t.name:22} {t.weight:>4}  {ev}"
                     + (f"  [{' '.join(tags)}]" if tags else ""))
    return "\n".join(L)


if __name__ == "__main__":
    print(summary())
