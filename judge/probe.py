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

    theme = [t for t in THEMES if t.key == "fact"][0]
    ctx = build_ctx(FAKE, TRUTH)
    system, user = prompts.build(theme, ctx)
    codes = [t.code for t in theme.tips]

    for json_mode in (True, False):
        if hasattr(client, "json_mode"):
            client.json_mode = json_mode
        label = "开 JSON mode" if json_mode else "关 JSON mode"
        t0 = time.perf_counter()
        try:
            raw, usage = client.complete(system, user)
        except Exception as exc:
            print(f"  {label}：调用失败 —— {type(exc).__name__}: {exc}")
            if json_mode:
                print("     （很可能是这个端点不支持 response_format，继续试关掉的情况）")
                continue
            print("\n端点不通或凭证不对。检查 config.env 里的 JUDGE_BASE_URL / JUDGE_API_KEY / JUDGE_MODEL。")
            return 1
        ms = round((time.perf_counter() - t0) * 1000)
        vs, err = parse_verdicts(raw, codes)
        ok = not err and len(vs) == len(codes)
        print(f"  {label}：{ms}ms　token 入 {usage.prompt_tokens} 出 {usage.completion_tokens}"
              f"　解析{'成功' if ok else '失败：' + err}")
        if not ok:
            print(f"     模型原文前 200 字：{raw[:200]!r}")
        if ok and json_mode:
            print(f"     判据 {len(vs)} 条：" + "、".join(
                f"{v['code']}={'命中' if v['hit'] else '未命中'}" for v in vs))
            print("\n  → 这个端点支持 JSON mode，config.env 保持 JUDGE_JSON_MODE=1")
            _summary(ms, usage)
            return 0
        if ok and not json_mode:
            print("\n  → 这个端点不支持 JSON mode，但裸输出能解析。"
                  "把 config.env 的 JUDGE_JSON_MODE 改成 0")
            _summary(ms, usage)
            return 0
    print("\n两种都解析不出来。模型没按 JSON 输出 —— 把上面的原文发我，我调 prompt。")
    return 1


def _summary(ms: int, usage) -> None:
    per = usage.prompt_tokens + usage.completion_tokens
    print(f"\n  单次约 {ms}ms / {per} token。94 轮 × 4 组 = 376 次调用，"
          f"串行约 {376 * ms / 1000 / 60:.0f} 分钟，--workers 4 约 {376 * ms / 4000 / 60:.0f} 分钟，"
          f"合计约 {376 * per / 10000:.0f} 万 token。")
    print("\n下一步：")
    print("  python3 cli.py run <跑批目录> --limit 5     # 先判 5 条看质量")
    print("  python3 cli.py run <跑批目录>               # 全量")


if __name__ == "__main__":
    raise SystemExit(run())
