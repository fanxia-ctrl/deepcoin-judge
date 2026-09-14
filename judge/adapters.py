#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把各个 agent 的跑批产物读成统一的 Turn 形状。

judge 不该知道 voice_agent_v3 的目录结构。加一个新数据源 = 在这里加一个 loader。
"""
from __future__ import annotations

import json
from pathlib import Path

# judge 需要的字段。缺的用 None，缺材料的主题会标「无法判定」而不是算 0 分。
FIELDS = ("case_id", "suite", "query", "voice", "detail", "raw_answer", "envelope_ok",
          "kb_hits", "kb_count", "kb_top_score", "route_case",
          # 模型实际看到的证据与工具返回（extract_run 新字段；老数据没有就是 None）
          "evidence_text", "route_rules", "tool_calls",
          "tool_node_titles", "tool_names", "elapsed_ms", "http_status", "error")


def from_voice_run(run_dir: Path) -> list[dict]:
    """voice agent 跑批目录（bench/runs/<run_id>/turns.jsonl）。"""
    f = Path(run_dir) / "turns.jsonl"
    if not f.exists():
        raise FileNotFoundError(f"{f} 不存在 —— 先在 voice 仓库跑 extract_run.py")
    rows = [json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [{k: r.get(k) for k in FIELDS} | {"_raw": r} for r in rows]


def from_jsonl(path: Path) -> list[dict]:
    """通用 jsonl：每行一条，字段名和 FIELDS 对齐即可。"""
    rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines()
            if l.strip()]
    return [{k: r.get(k) for k in FIELDS} | {"_raw": r} for r in rows]


LOADERS = {"voice_run": from_voice_run, "jsonl": from_jsonl}


def load(source: str, path: Path) -> list[dict]:
    if source == "auto":
        source = "voice_run" if (Path(path) / "turns.jsonl").exists() else "jsonl"
    return LOADERS[source](Path(path))


def ok(t: dict) -> bool:
    return (t.get("http_status") in (200, None)
            and bool(str(t.get("raw_answer") or "").strip())
            and not t.get("error"))
