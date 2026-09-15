#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""deepcoin-judge 命令行。

    python3 cli.py probe                            一次真调用，验端点与 JSON mode
    python3 cli.py check   <run>                    预检，不发请求
    python3 cli.py run     <run> [--llm auto]       判一遍
    python3 cli.py sheet   <run|judge.jsonl>        导出人工复核 xlsx
    python3 cli.py review  <复核表.xlsx>            复核结果：逐判据精确率/召回率、漏判清单、抽权威口径
    python3 cli.py agree   <judge.jsonl> <labels>   人机一致率（老标注格式）
    python3 cli.py truth   <labels.xlsx>            抽权威口径
    python3 cli.py rubric                           打印判据表
    python3 cli.py selftest                         自检
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "judge"))


def load_config() -> None:
    import os
    for name in ("config.env", "judge/config.env"):
        cfg = ROOT / name
        if not cfg.exists():
            continue
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _load(a) -> tuple[list[dict], dict]:
    import adapters
    turns = [t for t in adapters.load(a.source, a.run) if adapters.ok(t)]
    if a.limit:
        turns = turns[:a.limit]
    truth = {}
    if getattr(a, "no_truth", False):
        return turns, truth
    tp = a.truth or (ROOT / "data/labels/truth.jsonl")
    if tp and Path(tp).exists():
        for line in Path(tp).read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                truth[r["case_id"]] = r.get("truth") or ""
    return turns, truth


def cmd_run(a) -> int:
    load_config()
    import llm, pipeline, report, sheet
    from rubric import groups
    turns, truth = _load(a)
    from rubric import THEMES as themes
    grps = groups(a.group, themes)
    client = llm.get_client(a.llm)
    out_dir = Path(a.out or ROOT / "data/runs" / Path(a.run).name)
    out_dir.mkdir(parents=True, exist_ok=True)
    n_tips = sum(len(th.tips) for th in themes)
    print(f"judge: {len(turns)} 轮 × {len(grps)} 组（--group {a.group}）= "
          f"约 {len(turns) * len(grps)} 次调用，模型 {client.name}，"
          f"判据 {n_tips} 条，权威口径 "
          f"{'不用（--no-truth，T1 以召回切片为口径）' if getattr(a, 'no_truth', False) else str(len(truth)) + ' 条'}")
    t0 = time.perf_counter()
    rows, cache = pipeline.run(
        turns, client, truth, grps, workers=a.workers,
        cache_path=None if a.no_cache else out_dir / "cache.jsonl",
        retries=a.retries, evidence_chars=a.evidence_chars, downstream=a.downstream)
    (out_dir / "judge.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    report.write(rows, turns, out_dir / "report.md",
                 {"source": Path(a.run).name, "llm": client.name, "group": a.group,
                  "elapsed": time.perf_counter() - t0})
    print(f"\nOK {out_dir / 'judge.jsonl'}\nOK {out_dir / 'report.md'}")
    sheet.build(rows, out_dir / "复核表.xlsx")
    print(f"OK {out_dir / '复核表.xlsx'}　← 发给标注的就是这张")
    print(f"   调用 {sum(r.get('llm_calls', 0) for r in rows)} 次（缓存命中 {cache.hits}）"
          f"　token 入 {sum(r.get('prompt_tokens', 0) for r in rows):,}"
          f" 出 {sum(r.get('completion_tokens', 0) for r in rows):,}")
    bad = [r for r in rows if not r.get("complete")]
    if bad:
        print(f"   {len(bad)} 条判定不完整（缺材料或判据组失败），不计入结论")
    return 0


def cmd_check(a) -> int:
    load_config()
    import preflight
    turns, truth = _load(a)
    return preflight.run(turns, truth, a)


def cmd_sheet(a) -> int:
    import sheet
    return sheet.main(a.judge, a.out)


def cmd_review(a) -> int:
    import review
    return review.main(a.review, a.out, a.truth_out)


def cmd_agree(a) -> int:
    import agreement
    return agreement.main(a.judge_jsonl, a.labels, a.out)


def cmd_truth(a) -> int:
    import make_truth
    return make_truth.main(a.labels, a.out or ROOT / "data/labels/truth.jsonl")


def cmd_probe(a) -> int:
    load_config()
    import probe
    return probe.run()


def cmd_rubric(a) -> int:
    import rubric
    print(rubric.summary())
    return 0


def cmd_selftest(a) -> int:
    import selftest
    return selftest.main()


def main() -> int:
    ap = argparse.ArgumentParser(prog="deepcoin-judge")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_run_args(p):
        p.add_argument("run", type=Path, help="跑批产物目录或 jsonl")
        p.add_argument("--source", default="auto", choices=("auto", "voice_run", "jsonl"))
        p.add_argument("--truth", type=Path, default=None)
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--group", default="one", choices=("one", "bundle", "theme"),
                       help="one 一次输入判全部判据（默认）· bundle 按材料需求合并 · "
                            "theme 一主题一次，调 prompt 时用")
        p.add_argument("--evidence-chars", type=int, default=4000)
        p.add_argument("--no-truth", action="store_true",
                       help="不读 truth.jsonl；T1 改以召回切片为口径，14 条判据照判")

    p = sub.add_parser("run"); add_run_args(p)
    p.add_argument("--llm", default="auto", choices=("auto", "mock", "dify", "openai"))
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--retries", type=int, default=1)
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--downstream", action="store_true",
                   help="传入的是 gateway 之后的文本，此时才查 TTS 标记（R4）")
    p.add_argument("-o", "--out", type=Path, default=None)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("check"); add_run_args(p); p.set_defaults(fn=cmd_check)

    p = sub.add_parser("sheet")
    p.add_argument("judge", type=Path, help="judge 输出目录或 judge.jsonl")
    p.add_argument("-o", "--out", type=Path, default=None)
    p.set_defaults(fn=cmd_sheet)

    p = sub.add_parser("review")
    p.add_argument("review", type=Path, help="人核过的复核表 xlsx")
    p.add_argument("-o", "--out", type=Path, default=None)
    p.add_argument("--truth-out", type=Path, default=None,
                   help="把复核里的事实抽成权威口径并入这个 truth.jsonl（不给就不抽）")
    p.set_defaults(fn=cmd_review)

    p = sub.add_parser("agree")
    p.add_argument("judge_jsonl", type=Path)
    p.add_argument("labels", type=Path)
    p.add_argument("-o", "--out", type=Path, default=None)
    p.set_defaults(fn=cmd_agree)

    p = sub.add_parser("truth")
    p.add_argument("labels", type=Path)
    p.add_argument("-o", "--out", type=Path, default=None)
    p.set_defaults(fn=cmd_truth)

    sub.add_parser("probe").set_defaults(fn=cmd_probe)
    sub.add_parser("rubric").set_defaults(fn=cmd_rubric)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
