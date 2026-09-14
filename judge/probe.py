#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次真调用，验端点通不通、JSON mode 支不支持、判据格式吐不吐得对。"""
from __future__ import annotations

import json
import os
import time

import llm as _llm
import prompts
from context import build_ctx
from pipeline import parse_verdicts
from rubric import THEMES


FAKE = {
    "case_id": "probe:1", "suite": "probe",
    "query": "怎么设置返佣不自动转到合约账户",
    "voice": "您可以在APP中进入【资产】-【返佣】页面，关闭实时到账功能。",
    "detail": "", "raw_answer": '{"voice":"x","detail":"","panel":null,"input_request":null}',
    "envelope_ok": True, "kb_count": 2, "kb_top_score": 0.51, "elapsed_ms": 900,
    "kb_hits": [{"content": "返佣自动划转已为代理用户统一开启，暂不支持关闭。",
                 "score": 0.51, "document": "rebate.md"}],
    "tool_node_titles": [],
}
TRUTH = {"probe:1": "该功能已为代理用户统一开启，暂不支持关闭。"}


def run() -> int:
    print("探活：一次真调用\n")
    print(_llm.status(), "\n")
    try:
        client = _llm.get_client("auto")
    except NotImplementedError as exc:
        print(exc)
        return 1

    # 用真实的全量 prompt（14 条判据一次判），探活结果才代表全量
    from rubric import groups
    grp = groups("one")[0]
    ctx = build_ctx(FAKE, TRUTH)
    system, user = prompts.build(grp, ctx)
    codes = [t.code for th in grp for t in th.tips]

    def call(label: str) -> tuple[bool, str, object, int]:
        t0 = time.perf_counter()
        try:
            raw, usage = client.complete(system, user)
        except Exception as exc:
            print(f"  {label}：调用失败 —— {type(exc).__name__}: {exc}")
            return False, "", None, 0
        ms = round((time.perf_counter() - t0) * 1000)
        vs, err = parse_verdicts(raw, codes)
        ok = not err
        fr = getattr(usage, "finish_reason", "") or "?"
        rc = getattr(usage, "reasoning_chars", 0)
        print(f"  {label}：{ms}ms　token 入 {usage.prompt_tokens} 出 {usage.completion_tokens}"
              f"　finish={fr}　思考 {rc} 字　解析{'成功' if ok else '失败：' + err}")
        if not ok:
            print(f"     模型原文前 200 字：{raw[:200]!r}")
        return ok, err, usage, ms

    ok, err, usage, ms = call("开 JSON mode")
    if not ok and hasattr(client, "json_mode"):
        print("     （试关掉 JSON mode）")
        client.json_mode = False
        ok, err, usage, ms = call("关 JSON mode")
        if ok:
            print("\n  → 这个端点不支持 JSON mode，把 config.env 的 JUDGE_JSON_MODE 改成 0")
    if not ok:
        print("\n两种都解析不出来。把上面的原文发我。")
        return 1

    # 思考模型：思考也吃 max_tokens，是截断的根源。试着关掉。
    rc = getattr(usage, "reasoning_chars", 0)
    think_ratio = usage.completion_tokens / max(1, len(codes) * 60)   # 14 条判据大约 800 token
    if (rc or think_ratio > 2.5) and hasattr(client, "extra") and not client.extra:
        print(f"\n  模型在思考（reasoning {rc} 字，出 token 是判定本身的 {think_ratio:.1f} 倍）。"
              "试关思考：")
        for cand in ({"chat_template_kwargs": {"enable_thinking": False}},
                     {"thinking": {"type": "disabled"}},
                     {"reasoning": {"enabled": False}}):
            client.extra = cand
            ok2, _, u2, ms2 = call(f"    {json.dumps(cand, ensure_ascii=False)}")
            if ok2 and not getattr(u2, "reasoning_chars", 0) \
                    and u2.completion_tokens < usage.completion_tokens * 0.7:
                print(f"\n  → 有效。写进 config.env：\n"
                      f"     JUDGE_EXTRA_BODY={json.dumps(cand, ensure_ascii=False)}")
                _summary(ms2, u2)
                return 0
        client.extra = {}
        print("\n  → 三种都没关掉。不影响跑：max_tokens 已放到 8000，截断也会救回已判的条目。")
    _summary(ms, usage)
    return 0


def _summary(ms: int, usage) -> None:
    per = usage.prompt_tokens + usage.completion_tokens
    n = 94
    print(f"\n  单次约 {ms}ms / {per} token（已是全量 14 条判据的 prompt）。"
          f"全量 {n} 轮 × 1 组 = {n} 次调用，"
          f"--workers 4 约 {n * ms / 4000 / 60:.0f} 分钟，约 {n * per / 10000:.0f} 万 token。")
    print("\n下一步：")
    print("  python3 cli.py run data/in/shane-fx-0908 --no-truth --limit 5   # 先判 5 条看质量")
    print("  python3 cli.py run data/in/shane-fx-0908 --no-truth             # 全量")


if __name__ == "__main__":
    raise SystemExit(run())
