# -*- coding: utf-8 -*-
"""提交进来的东西先过这里：校验、补默认值、归一成 pipeline 认的 Turn 形状。

纯函数，不碰网络和磁盘。出错抛 BadRequest，消息直接回给调用方。
"""
from __future__ import annotations

import json

from adapters import FIELDS
from rubric import groups

MAX_TURNS = 2000


class BadRequest(ValueError):
    pass


# 默认值就是 bench 协议：3 采样 2/3 表决。docs/bench.md 里 91% 那个数就是这么量出来的，
# 单采样同配置重跑 F1 能摆 6 分。要快可以传 samples=1，但结果别拿去和 bench 比。
DEFAULTS = {"samples": 3, "vote_k": None, "group": "one", "temperature": 0.0,
            "evidence_chars": 4000, "retries": 1, "builtin_truth": True, "name": ""}


def options(raw: dict | None) -> dict:
    raw = dict(raw or {})
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        raise BadRequest(f"不认识的选项：{'、'.join(sorted(unknown))}"
                         f"（可用：{'、'.join(DEFAULTS)}）")
    o = {**DEFAULTS, **raw}

    def num(k, lo, hi, kind):
        v = o[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or \
                (kind is int and int(v) != v) or not lo <= v <= hi:
            raise BadRequest(f"{k} 要在 {lo}~{hi} 之间，收到 {v!r}")
        o[k] = kind(v)

    num("samples", 1, 5, int)
    if o["vote_k"] is None:
        o["vote_k"] = o["samples"] // 2 + 1
    num("vote_k", 1, o["samples"], int)
    num("temperature", 0, 1, float)
    num("evidence_chars", 500, 20000, int)
    num("retries", 0, 3, int)
    try:
        groups(o["group"])
    except ValueError:
        raise BadRequest(f"group 只能是 one / bundle / theme，收到 {o['group']!r}") from None
    if not isinstance(o["builtin_truth"], bool):
        raise BadRequest("builtin_truth 要是 true / false")
    o["name"] = str(o["name"] or "")[:80]
    return o


STR_FIELDS = ("case_id", "suite", "query", "voice", "detail", "raw_answer", "route_case",
              "evidence_text", "route_rules", "error", "group", "expect", "gold_intent")
LIST_FIELDS = ("kb_hits", "tool_calls", "tool_names", "tool_node_titles", "gold_segment_ids")
NUM_FIELDS = ("kb_count", "kb_top_score", "elapsed_ms", "http_status")


def turns(raw) -> tuple[list[dict], dict[str, str], list[str]]:
    """返回 (归一后的轮次, 调用方随轮带来的权威口径, 警告)。"""
    if not isinstance(raw, list) or not raw:
        raise BadRequest("turns 要是非空数组")
    if len(raw) > MAX_TURNS:
        raise BadRequest(f"一次最多 {MAX_TURNS} 轮，收到 {len(raw)}；拆成多个任务提交")

    out, truth, errs, seen = [], {}, [], set()
    no_env = no_ev = no_rules = 0
    for i, r in enumerate(raw):
        where = f"第 {i + 1} 轮"
        if not isinstance(r, dict):
            errs.append(f"{where}不是对象")
            continue
        r = dict(r)
        # 通用接入的人多半只有「回答」这一个字段，没有 voice/detail 的分层
        if not r.get("voice") and isinstance(r.get("answer"), str):
            r["voice"] = r.pop("answer")
        bad = [k for k in STR_FIELDS if r.get(k) is not None and not isinstance(r[k], (str, int))]
        bad += [k for k in LIST_FIELDS if r.get(k) is not None and not isinstance(r[k], list)]
        bad += [k for k in NUM_FIELDS if r.get(k) is not None
                and (isinstance(r[k], bool) or not isinstance(r[k], (int, float)))]
        if bad:
            errs.append(f"{where}字段类型不对：{'、'.join(bad)}")
            continue
        if not str(r.get("query") or "").strip():
            errs.append(f"{where}缺 query")
            continue
        if not (str(r.get("voice") or "") + str(r.get("detail") or "")).strip():
            errs.append(f"{where}缺回答（voice / detail / answer 至少一个）")
            continue

        cid = str(r.get("case_id") or f"t{i + 1:04d}")
        if cid in seen:
            errs.append(f"{where} case_id 重复：{cid}")
            continue
        seen.add(cid)

        t = {k: r.get(k) for k in FIELDS}
        t["case_id"] = cid
        # 契约规则 R1（信封）和 R7（空答）查的是 voice agent 自己的 JSON 信封。
        # 没带 raw_answer 就说明调用方没有这层 —— 不合成的话每一轮都会被判「信封解析失败」
        # 加「空回答」，契约卡点一命中这一轮就不能发。合成一个，等于跳过信封检查。
        if not str(t.get("raw_answer") or "").strip():
            t["raw_answer"] = json.dumps({"voice": t.get("voice") or "",
                                          "detail": t.get("detail") or ""}, ensure_ascii=False)
            if t.get("envelope_ok") is None:
                t["envelope_ok"] = True
            no_env += 1
        elif t.get("envelope_ok") is None:
            try:
                t["envelope_ok"] = isinstance(json.loads(t["raw_answer"]), dict)
            except json.JSONDecodeError:
                t["envelope_ok"] = False
        if isinstance(r.get("truth"), str) and r["truth"].strip():
            truth[cid] = r["truth"]
            t["_truth"] = r["truth"]        # 跟着轮次落库，执行时并进口径表
        if not str(t.get("evidence_text") or "").strip() and not t.get("kb_hits"):
            no_ev += 1
        if not str(t.get("route_rules") or "").strip():
            no_rules += 1
        out.append(t)

    if errs:
        more = f"……另有 {len(errs) - 10} 处" if len(errs) > 10 else ""
        raise BadRequest("；".join(errs[:10]) + more)

    n = len(out)
    warns = []
    if no_env:
        warns.append(f"{no_env}/{n} 轮没带 raw_answer：按 voice/detail 合成了信封，"
                     "信封检查（R1）等于跳过")
    if no_ev:
        warns.append(f"{no_ev}/{n} 轮没带 evidence_text 也没带 kb_hits：T1 事实冲突、"
                     "T5 证据使用两组会标「无法判定」")
    if no_rules:
        warns.append(f"{no_rules}/{n} 轮没带 route_rules：判据少一处口径来源，结论会偏弱")
    return out, truth, warns
