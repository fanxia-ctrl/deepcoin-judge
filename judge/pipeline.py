#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge 主流程：跑批产物 → judge.jsonl + report.md"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import adapters
import prompts
from context import build_ctx, missing_materials
from llm import LLMClient
from rubric import (NEEDS_EVIDENCE, NEEDS_TRUTH, THEMES, TIP_BY_CODE,
                    group_key, group_label, groups)
from score import score
from scorers import objective

RETRY_HINT = ("\n\n上一次的回复不是合法 JSON 或漏了判据。只输出 JSON 对象，"
              "不要代码块、不要解释，verdicts 覆盖每一条判据。")


class Cache:
    def __init__(self, path: Path | None):
        self.path, self.mem, self.hits = path, {}, 0
        self.lock = threading.Lock()
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    self.mem[r["k"]] = r["raw"]

    @staticmethod
    def key(case_id: str, gk: str, system: str, user: str) -> str:
        h = hashlib.sha256((system + "\x00" + user).encode()).hexdigest()[:16]
        return f"{case_id}|{gk}|{h}"

    def get(self, k):
        v = self.mem.get(k)
        if v is not None:
            with self.lock:
                self.hits += 1
        return v

    def put(self, k, raw):
        if self.path is None:
            return
        with self.lock:
            self.mem[k] = raw
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"k": k, "raw": raw}, ensure_ascii=False) + "\n")


VERDICT_OBJ = re.compile(r"\{[^{}]*?\"code\"\s*:\s*\"(neg_\d_\d)\"[^{}]*\}", re.S)


def _norm(v: dict) -> dict | None:
    c = str(v.get("code") or "")
    tip = TIP_BY_CODE.get(c)
    if tip is None:
        return None
    rec = {"code": c, "hit": v.get("hit") if v.get("hit") in (True, False) else None,
           "quote": str(v.get("quote") or "")[:200],
           "why": str(v.get("why") or "")[:300]}
    if tip.conditional:
        rec["applies"] = v.get("applies") if v.get("applies") in (True, False) else None
    if tip.type_field and rec["hit"]:
        rec[tip.type_field] = str(v.get(tip.type_field) or "")
    return rec


def parse_verdicts(raw: str, codes: list[str]) -> tuple[list[dict], str]:
    """返回 (verdicts, err)。err 为空 = 齐全。
    整体 JSON 坏了（多半是被 max_tokens 截断）就逐个对象救，能救几条算几条 ——
    err 里写明「截断，救回 n/m」，调用方决定重试还是接受。"""
    txt = (raw or "").strip()
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-z]*\n|\n```$", "", txt)
    out, seen = [], set()
    m = re.search(r"\{.*\}", txt, re.S)
    whole_ok = False
    if m:
        try:
            obj = json.loads(m.group(0))
            vs = obj.get("verdicts") if isinstance(obj, dict) else None
            if isinstance(vs, list):
                whole_ok = True
                for v in vs:
                    if isinstance(v, dict):
                        rec = _norm(v)
                        if rec and rec["code"] not in seen:
                            seen.add(rec["code"])
                            out.append(rec)
        except json.JSONDecodeError:
            pass
    if not whole_ok:
        for mm in VERDICT_OBJ.finditer(txt):
            try:
                rec = _norm(json.loads(mm.group(0)))
            except json.JSONDecodeError:
                continue
            if rec and rec["code"] not in seen:
                seen.add(rec["code"])
                out.append(rec)
    missing = [c for c in codes if c not in seen]
    if not txt:
        return [], "空回复（思考模型把 max_tokens 全花在思考上，content 为空）"
    if not out:
        return [], "没找到 JSON"
    if missing:
        how = "截断" if not whole_ok else "漏判"
        return out, f"{how}，救回 {len(out)}/{len(codes)}，缺 {'、'.join(missing)}"
    return out, ""


def verify_quotes(verdicts: list[dict], answer: str) -> list[str]:
    """quote 引不出原文的命中一律当过判，翻回 false。最便宜的降过判手段。"""
    flat = re.sub(r"\s+", "", answer)
    bad = []
    for v in verdicts:
        if v.get("hit") is not True:
            continue
        q = re.sub(r"\s+", "", str(v.get("quote") or ""))
        if len(q) < 4 or q not in flat:
            v["hit"] = False
            v["why"] = f"[引用校验未通过] {v.get('why', '')}"[:300]
            bad.append(v["code"])
    return bad


def judge_turn(client: LLMClient, turn: dict, truth: dict, evidence_chars: int,
               grps, cache: Cache, retries: int = 1, downstream: bool = False) -> dict:
    cid = str(turn.get("case_id") or "")
    ctx = build_ctx(turn, truth, evidence_chars)
    missing = missing_materials(ctx)

    rule_hits = objective.check_contract(turn, downstream=downstream)
    rule_hits += objective.check_scored(turn)

    verdicts: list[dict] = []
    errs, done, failed, undecidable = [], [], [], []
    unjudged: list[str] = []          # 救回部分结果时没判到的判据
    thinking_notes: list[str] = []
    calls = pt = ct = 0
    t0 = time.perf_counter()

    for grp in grps:
        need = {n for th in grp for n in th.needs}
        blocked = [th.key for th in grp
                   if (NEEDS_TRUTH in th.needs and "truth" in missing)
                   or (NEEDS_EVIDENCE in th.needs and "evidence" in missing)]
        if blocked and len(blocked) == len(grp):
            undecidable += blocked
            continue
        codes = [t.code for th in grp for t in th.tips]
        system, user = prompts.build(grp, ctx)
        gk, glabel = group_key(grp), group_label(grp)
        k = Cache.key(cid, gk, system, user)
        raw, vs, err = cache.get(k), [], "缓存里没有"
        best: tuple[list[dict], str] = ([], "")
        for attempt in range(retries + 1):
            if raw is None:
                try:
                    raw, usage = client.complete(system, user + (RETRY_HINT if attempt else ""))
                    calls += 1
                    pt += getattr(usage, "prompt_tokens", 0) or 0
                    ct += getattr(usage, "completion_tokens", 0) or 0
                    fr = getattr(usage, "finish_reason", "")
                    rc = getattr(usage, "reasoning_chars", 0)
                    if fr == "length" or rc:
                        thinking_notes.append(f"finish={fr or '?'} 思考{rc}字")
                except NotImplementedError as exc:
                    return {"case_id": cid, "fatal": str(exc)}
                except Exception as exc:
                    err, raw = f"调用失败：{type(exc).__name__}: {exc}", None
                    continue
            vs, err = parse_verdicts(raw, codes)
            if not err:
                cache.put(k, raw)
                break
            if len(vs) > len(best[0]):
                best = (vs, err)
            if raw is not None and cache.path is not None:
                fdir = cache.path.parent / "failed"
                fdir.mkdir(exist_ok=True)
                (fdir / f"{cid.replace(':', '_')}.{attempt}.txt").write_text(
                    f"# {err}\n\n{raw}", encoding="utf-8")
            raw = None
        if err and best[0]:
            # 重试后还是不齐：接受救回来的，缺的标 null。一条判据没判不该让整轮作废。
            vs, err = best
            have = {v["code"] for v in vs}
            for c in codes:
                if c not in have:
                    vs.append({"code": c, "hit": None, "quote": "",
                               "why": "[未判] 模型输出截断或漏判"})
                    unjudged.append(c)
            errs.append(f"[{glabel}] {err}（已接受部分结果）")
            done.append(gk)
        elif err:
            errs.append(f"[{glabel}] {err}")
            failed.append(gk)
        else:
            done.append(gk)
        undecidable += [b for b in blocked if b not in undecidable]
        verdicts += vs

    overclaimed = verify_quotes(verdicts, ctx["answer"])
    if overclaimed:
        errs.append(f"引用校验翻回 {len(overclaimed)} 条：{'、'.join(overclaimed)}")

    sc = score(verdicts, rule_hits, undecidable=sorted(set(undecidable)))
    if failed:
        sc["verdict"] = "判定不完整"
        sc["reason"] = f"判据组 {'、'.join(failed)} 没判成"
        sc["complete"] = False
    elif unjudged and sc["verdict"] == "可直接发":
        # 同「缺材料」的原则：没判到的判据可能藏着问题，不能说干净
        sc["verdict"] = "判定不完整"
        sc["reason"] = f"{len(unjudged)} 条判据未判：{'、'.join(unjudged)}"
        sc["complete"] = False
    elif unjudged:
        sc["reason"] += f"（{'、'.join(unjudged)} 未判）"

    return {
        "case_id": cid, "suite": turn.get("suite"),
        "query": ctx["query"], "answer": ctx["answer"], "voice": turn.get("voice"),
        "evidence": ctx["evidence"], "tool_calls": ctx.get("tool_calls") or [],
        "rules": re.sub(r"【当前时间】[^\n]*", "【当前时间】（略）", ctx.get("rules") or ""),
        "kb_count": turn.get("kb_count"), "kb_top_score": turn.get("kb_top_score"),
        "out_of_coverage": ctx["signals"]["out_of_coverage"],
        "has_truth": bool(ctx["truth"]), "route_case": turn.get("route_case"),
        **sc,
        "rule_detail": [{"rule": h.rule, "name": h.name, "kind": h.kind,
                         "weight": h.weight, "quote": h.quote, "why": h.why}
                        for h in rule_hits],
        "verdicts": verdicts,
        "overclaimed": overclaimed,
        "unjudged": unjudged,
        "thinking": thinking_notes,
        "judge_errors": errs,
        "groups_done": done, "groups_failed": failed,
        "llm_calls": calls, "prompt_tokens": pt, "completion_tokens": ct,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000),
    }


def run(turns, client, truth, grps, *, workers=4, cache_path=None, retries=1,
        evidence_chars=4000, downstream=False, progress=print):
    cache = Cache(cache_path)
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(judge_turn, client, t, truth, evidence_chars, grps,
                            cache, retries, downstream): t for t in turns}
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r.get("fatal"):
                raise NotImplementedError(r["fatal"])
            rows.append(r)
            # 小批量逐条报，大批量每 10 条报 —— 否则 --limit 5 会看起来像卡住
            if len(turns) <= 20 or i % 10 == 0 or i == len(turns):
                progress(f"  {i}/{len(turns)}  {r.get('case_id', '')}  {r.get('verdict', '')}"
                         f"  {r.get('elapsed_ms', 0) / 1000:.0f}s")
    rows.sort(key=lambda r: (r.get("suite") or "", r.get("case_id") or ""))
    return rows, cache
