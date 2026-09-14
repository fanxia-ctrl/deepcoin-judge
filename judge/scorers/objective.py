#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""规则层 —— 不调模型，判定确定可复现。

分三类：
  契约卡点 R1–R7   命中即不可上线，不进质量分
  计分规则 R8–R10  不需要 LLM，直接进扣分
  簇级检查 R11     写进 run 报告，不进单轮分

边界很重要：[pause:0.4] 是 gateway 消费的控制标记，要在 gateway 之后查；
27B 契约写明 detail 默认为空，所以查的是「分层违约」而不是「detail 为空」。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

VOICE_MAX = 120
LOW_SCORE = 0.45
TURN_TIMEOUT_MS = 20_000
REPEAT_SIM = 0.92

TTS_MARKS = re.compile(r"\[(pause|emphasis|break)(:[\d.]+)?\]")
VERBATIM = re.compile(
    r"https?://|www\.[a-z0-9-]+\.|[a-z0-9._%-]+@[a-z0-9.-]+\.[a-z]{2,}|"
    r"\b0x[0-9a-fA-F]{16,}\b|\b\d{12,}\b", re.I)
ENVELOPE_LEAK = re.compile(r'\{\s*"voice"\s*:|"input_request"\s*:')
THINKING_LEAK = re.compile(r"用户表达|客服(应|需|要)(共情|安抚)|本轮应当|判断[:：]|"
                           r"思考过程|首先我需要")
END_PUNCT = ("。", "！", "？", "…", ".", "!", "?", "」", "）", ")")
ENVELOPE_KEYS = ("voice", "detail", "panel", "input_request")

FALLBACK_HINTS = ("暂时无法确认", "这个问题我这边还不确定", "为您转接", "暂未收录")
DETAIL_POINTER = re.compile(r"详情(见|在)|见下方|下方(有|为)|参见下面|具体见")

CJK = re.compile(r"[一-鿿]")
LATIN = re.compile(r"[A-Za-z]")

ACCOUNT_FACT = re.compile(
    r"您(目前|现在|当前)?的?(账户|仓位|持仓|订单|余额|保证金|返佣)[^。；]{0,24}"
    r"(是|有|为|显示|已|共)|"
    r"您在\s*[A-Z]{2,8}(USDT|USD)?|杠杆(约|为)\s*\d|开仓价(约|为)\s*\d|"
    r"共\s*\d+(\.\d+)?\s*(U|USDT|张|笔)")
EMPTY_AS_ERROR = re.compile(r"账户异常|数据(丢失|异常|错误)|系统(故障|异常)|记录(丢失|被清)")
PERSONAL_DATA = re.compile(
    r"我的|我这(单|笔|个)|帮我(看|查|核)|为什么我|怎么我的|我(开|平|下)的")


@dataclass
class Hit:
    rule: str
    name: str
    kind: str          # contract / scored / cluster
    weight: float
    quote: str
    why: str


def _voice(turn: dict) -> str:
    return str(turn.get("voice") or "")


def _detail(turn: dict) -> str:
    return str(turn.get("detail") or "")


# ── 契约卡点 ──────────────────────────────────────────────
def check_contract(turn: dict, *, downstream: bool = False) -> list[Hit]:
    """downstream=True 表示传入的是 gateway 之后的文本，此时才查 TTS 标记。"""
    out: list[Hit] = []
    voice, detail = _voice(turn), _detail(turn)
    raw = str(turn.get("raw_answer") or "")

    # R1 信封
    reasons = []
    if not turn.get("envelope_ok"):
        reasons.append("信封解析失败")
    else:
        try:
            obj = json.loads(raw) if raw.strip().startswith("{") else {}
            miss = [k for k in ENVELOPE_KEYS if k not in obj]
            if miss:
                reasons.append("缺键 " + "、".join(miss))
        except json.JSONDecodeError as exc:
            reasons.append(f"JSON 不合法：{exc.msg}")
    if reasons:
        out.append(Hit("R1", "信封解析失败", "contract", 0, raw[:40], "；".join(reasons)))

    # R2 voice 长度
    if not voice.strip():
        out.append(Hit("R2", "voice 为空或超 120 字", "contract", 0, "", "voice 为空"))
    elif len(voice) > VOICE_MAX:
        out.append(Hit("R2", "voice 为空或超 120 字", "contract", 0, voice[:40],
                       f"{len(voice)} 字，超 {VOICE_MAX}"))

    # R3 逐字内容出现在 voice
    m = VERBATIM.search(voice)
    if m:
        out.append(Hit("R3", "voice 含逐字内容", "contract", 0, m.group(0)[:60],
                       "URL/邮箱/长数字等应放 detail"))

    # R4 TTS 标记 —— 只在 gateway 之后查
    if downstream:
        m = TTS_MARKS.search(voice + detail)
        if m:
            out.append(Hit("R4", "TTS 标记泄漏", "contract", 0, m.group(0),
                           "gateway 之后仍有控制标记"))

    # R5 detail 分层违约（detail 为空本身不违约）
    d = []
    if DETAIL_POINTER.search(voice) and not detail.strip():
        d.append("voice 说详情见下方，detail 却为空")
    if detail.strip() and detail.strip() == voice.strip():
        d.append("detail 与 voice 重复")
    if any(h in voice for h in FALLBACK_HINTS) and detail.strip():
        d.append("用了固定兜底，detail 却非空")
    if d:
        out.append(Hit("R5", "detail 分层违约", "contract", 0, voice[:40], "；".join(d)))

    # R6 信封或思考过程泄漏
    for pat, why in ((ENVELOPE_LEAK, "信封裸露"), (THINKING_LEAK, "思考过程被当答案")):
        m = pat.search(voice)
        if m:
            out.append(Hit("R6", "信封或思考过程泄漏", "contract", 0, m.group(0)[:50], why))
            break

    # R7 截断/重复/超时/空答
    r = []
    if turn.get("error") or turn.get("http_status") not in (200, None):
        r.append(f"传输失败：{str(turn.get('error') or '')[:60]}")
    elif not raw.strip():
        r.append("空回答")
    if voice.strip() and not voice.rstrip().endswith(END_PUNCT):
        r.append("没有终止标点，疑似截断")
    el = turn.get("elapsed_ms")
    if el and el > TURN_TIMEOUT_MS:
        r.append(f"整轮 {el}ms 超 {TURN_TIMEOUT_MS}ms")
    prev = str(turn.get("prev_voice") or "")
    if prev and voice and _sim(prev, voice) >= REPEAT_SIM:
        r.append("与上一轮几乎重复")
    if r:
        out.append(Hit("R7", "截断、重复、超时或空答", "contract", 0, voice[-30:],
                       "；".join(r)))
    return out


# ── 计分规则 ──────────────────────────────────────────────
def check_scored(turn: dict) -> list[Hit]:
    out: list[Hit] = []
    voice, detail = _voice(turn), _detail(turn)
    text = voice + "\n" + detail
    query = str(turn.get("query") or "")

    # R8 语种不跟随
    q_cjk, q_lat = len(CJK.findall(query)), len(LATIN.findall(query))
    a_cjk, a_lat = len(CJK.findall(text)), len(LATIN.findall(text))
    # 问题只要 3 个有效字符就够判语种；回答短于 8 个字符不判，避免「好的。」这类误报
    if q_cjk + q_lat >= 3 and a_cjk + a_lat >= 8:
        q_zh, a_zh = q_cjk > q_lat, a_cjk > a_lat
        if q_zh != a_zh:
            out.append(Hit("R8", "语种不跟随", "scored", 0.6, text[:40],
                           f"提问{'中文' if q_zh else '英文'}，回答{'中文' if a_zh else '英文'}"))

    tools = turn.get("tool_node_titles") or turn.get("tool_names") or []
    has_tools = bool(tools)
    tools_known = turn.get("tool_node_titles") is not None or turn.get("tool_names") is not None

    # R9 账户事实无工具调用
    m = ACCOUNT_FACT.search(text)
    if m and tools_known and not has_tools:
        out.append(Hit("R9", "账户事实无工具调用", "scored", 3.0, m.group(0)[:50],
                       "回答给出具体账户数据，本轮没有任何工具记录"))

    # R10 空结果被说成异常
    m = EMPTY_AS_ERROR.search(text)
    if m and tools_known and not has_tools:
        out.append(Hit("R10", "空结果被说成异常", "scored", 3.0, m.group(0),
                       "没有工具返回却断言账户/系统异常"))
    return out


# ── 簇级检查 ──────────────────────────────────────────────
def check_cluster(turns: list[dict], *, sim_threshold: float = 0.6) -> list[dict]:
    """同一簇近重复问题出现相反结论 —— KB 口径冲突，不惩罚模型。"""
    POS = re.compile(r"可以(关闭|设置|调整)|支持(关闭|修改)|您可以在")
    NEG = re.compile(r"无法(关闭|修改|调整)|不支持(关闭|修改)|已(统一|全面)开启|暂不支持")
    by_suite: dict[str, list[dict]] = {}
    for t in turns:
        by_suite.setdefault(str(t.get("suite") or ""), []).append(t)
    out = []
    for suite, rows in by_suite.items():
        pos = [r for r in rows if POS.search(_voice(r)) and not NEG.search(_voice(r))]
        neg = [r for r in rows if NEG.search(_voice(r))]
        if pos and neg and len(rows) >= 4:
            out.append({
                "rule": "R11", "name": "口径冲突", "kind": "cluster",
                "suite": suite, "n": len(rows),
                "say_yes": [r.get("case_id") for r in pos][:6],
                "say_no": [r.get("case_id") for r in neg][:6],
                "why": f"{suite} 里 {len(pos)} 条说可以、{len(neg)} 条说不行 —— KB 口径冲突，"
                       "标为知识库问题，不惩罚模型",
            })
    return out


# ── 信号 ──────────────────────────────────────────────────
def retrieval_signals(turn: dict, low_score: float = LOW_SCORE) -> dict:
    kb = int(turn.get("kb_count") or 0)
    top = turn.get("kb_top_score")
    top = float(top) if top is not None else None
    low = kb == 0 or (top is not None and top < low_score)
    return {"kb_count": kb, "kb_top_score": top, "zero_recall": kb == 0,
            "out_of_coverage": low}


def account_signals(turn: dict) -> dict:
    tools = turn.get("tool_node_titles") or turn.get("tool_names") or []
    known = turn.get("tool_node_titles") is not None or turn.get("tool_names") is not None
    return {"personal_query": bool(PERSONAL_DATA.search(str(turn.get("query") or ""))),
            "called_account_api": bool(tools),
            "tool_record_available": known,
            "tools_called": list(tools)[:6]}


def _sim(a: str, b: str) -> float:
    import difflib
    return difflib.SequenceMatcher(None, a, b).ratio()
