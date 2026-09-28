# -*- coding: utf-8 -*-
"""后台执行：一个调度线程按提交顺序取任务，任务里的轮次丢进共享线程池。

任务之间串行、任务内按轮并行。并行度就是全局上限 —— 上游只有一台 vLLM，
并发 4 时它都会掉连接（v13 掉了 19 次），所以不按任务数叠加。
单轮 3 采样仍是串行：异步场景要的是总吞吐，而总吞吐卡在上游，按轮并行已经能打满它。
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import llm
import pipeline
import report
import rubric
from rubric import THEMES, groups

from .store import Store

ROOT = Path(__file__).resolve().parent.parent

MAX_PASSES = 3      # 一个任务最多判几遍：偶发调用失败的轮次在后几遍补判，最后一遍才认「判定不完整」
BREAKER = 5         # 连续这么多轮都是上游调用失败 → 判定上游挂了，任务放回队列、退避
BACKOFF = (60, 120, 300, 600)


def upstream_failed(r: dict) -> bool:
    """这一轮没判成是因为模型没调通（断连、超时），而不是判定本身出了问题。"""
    return bool(r.get("groups_failed")) and any(
        "调用失败" in str(e) for e in (r.get("judge_errors") or []))


def builtin_truth(path: Path = ROOT / "data/labels/truth.jsonl") -> dict[str, str]:
    """仓库里人工确认的权威口径，按 case_id 查。外部数据的 id 对不上就等于没有。"""
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[r["case_id"]] = r.get("truth") or ""
    return out


def summary(rows: list[dict | None]) -> dict:
    done = [r for r in rows if r]
    return {
        "verdicts": dict(Counter(r.get("verdict") for r in done)),
        "incomplete": sum(1 for r in done if not r.get("complete")),
        "hits": dict(Counter(c for r in done for c in (r.get("neg_hits") or [])).most_common()),
    }


class Runner:
    def __init__(self, store: Store, client: llm.LLMClient, data_dir: Path, *, workers: int = 4):
        self.store, self.client, self.dir = store, client, data_dir
        self.workers = workers
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="judge")
        self.cache = pipeline.Cache(data_dir / "cache.jsonl")
        self.truth = builtin_truth()
        self.wake = threading.Event()
        self.current: str | None = None
        self.backoff = BACKOFF
        self.paused_until = 0.0
        self.outages = 0                  # 连续熔断次数，决定退避多久
        self.last_outage: str | None = None
        self._stop = False
        self.thread = threading.Thread(target=self._loop, name="dispatcher", daemon=True)

    def start(self) -> None:
        back = self.store.requeue_interrupted()
        if back and not os.environ.get("JUDGE_SERVICE_QUIET"):
            print(f"上次没跑完的 {len(back)} 个任务放回队列：{'、'.join(back)}")
        self.thread.start()

    def stop(self) -> None:
        self._stop = True
        self.wake.set()

    def poke(self) -> None:
        self.wake.set()

    def job_dir(self, jid: str) -> Path:
        return self.dir / "jobs" / jid

    def _loop(self) -> None:
        while not self._stop:
            if time.time() < self.paused_until:        # 上游刚挂过，先别打它
                self.wake.wait(min(5, self.paused_until - time.time()))
                self.wake.clear()
                continue
            jid = self.store.next_queued()
            if not jid:
                self.wake.wait(5)
                self.wake.clear()
                continue
            self.current = jid
            try:
                self._run(jid)
            except Exception as exc:        # 调度线程不能死：记下来，接着跑下一个
                traceback.print_exc()
                self.store.finish(jid, "failed", error=f"{type(exc).__name__}: {exc}")
            finally:
                self.current = None

    def _run(self, jid: str) -> None:
        job = self.store.get(jid)
        o = job["options"]
        if self.store.cancel_requested(jid):
            self.store.finish(jid, "cancelled", job["meta"], error="提交方取消")
            return
        self.store.mark_running(jid)
        # 端点指纹是进程级集合，常驻服务里会跨任务越攒越多；任务串行，开头清一次就是这个任务的
        llm.SEEN_FINGERPRINTS.clear()
        t0 = time.time()

        turns = self.store.turns(jid)
        truth = dict(self.truth) if o["builtin_truth"] else {}
        truth.update({t["case_id"]: t["_truth"] for t in turns if t.get("_truth")})
        grps = groups(o["group"], THEMES)

        cancelled = fatal = tripped = None
        passes = job["meta"].get("passes", 0)
        while not (cancelled or fatal or tripped):
            pending = self.store.pending(jid)
            if not pending:
                break
            last = passes >= MAX_PASSES - 1
            passes += 1
            futs = {self.pool.submit(pipeline.judge_turn, self.client, t, truth,
                                     o["evidence_chars"], grps, self.cache, o["retries"], False,
                                     o["samples"], o["vote_k"], o["temperature"]): i
                    for i, t in pending}
            streak, deferred = 0, {}
            for f in as_completed(futs):
                i = futs[f]
                if f.cancelled():
                    continue
                try:
                    r = f.result()
                except Exception as exc:
                    fatal = (f"第 {i + 1} 轮（{turns[i].get('case_id')}）判定时出错："
                             f"{type(exc).__name__}: {exc}")
                    break
                if r.get("fatal"):
                    fatal = r["fatal"]
                    break
                if upstream_failed(r):
                    # 模型没调通 ≠ 判完了。存进去就再也不会重判（v13 就是这样废掉 21 轮的），
                    # 先暂存；连着好几轮都这样就是上游挂了，别再空转
                    deferred[i] = r
                    streak += 1
                    if streak >= BREAKER:
                        tripped = r["judge_errors"][0]
                        break
                    continue
                streak = 0
                self.store.save_result(jid, i, r)
                if not cancelled and self.store.cancel_requested(jid):
                    cancelled = True
                    break
            if cancelled or fatal or tripped:
                for g in futs:
                    g.cancel()               # 还没开始的不跑了；在跑的让它跑完（结果丢弃）
            elif last:
                for i, r in deferred.items():  # 判了 MAX_PASSES 遍还是调不通：认了，按不完整落盘
                    self.store.save_result(jid, i, r)

        if tripped:
            wait = self.backoff[min(self.outages, len(self.backoff) - 1)]
            self.outages += 1
            self.paused_until = time.time() + wait
            note = (f"上游连续 {BREAKER} 轮调用失败，暂停 {wait:.0f}s 后接着跑"
                    f"（已判完的保留）：{tripped[:160]}")
            self.last_outage = note
            # 被熔断打断的这一遍不算额度：是上游挂了，不是这几轮自己判不出来
            self.store.requeue(jid, {**job["meta"], "passes": passes - 1, "note": note})
            if not os.environ.get("JUDGE_SERVICE_QUIET"):
                print(f"任务 {jid} 暂停：{note}")
            return
        self.outages = 0

        rows = self.store.results(jid)
        meta = {"rubric": rubric.fingerprint(),
                "fingerprints": sorted(llm.SEEN_FINGERPRINTS),
                "llm": self.client.name,
                "elapsed_s": round(time.time() - t0, 1),
                "passes": passes, "note": None,
                "summary": summary(rows)}
        if any(rows):
            self._write(jid, job, turns, rows, meta)
        status = "failed" if fatal else "cancelled" if cancelled else "done"
        self.store.finish(jid, status, {**job["meta"], **meta}, error=fatal or
                          ("提交方取消，已判完的轮次保留" if cancelled else None))
        if not os.environ.get("JUDGE_SERVICE_QUIET"):
            print(f"任务 {jid} {status}：{sum(1 for r in rows if r)}/{len(rows)} 轮，"
                  f"{meta['elapsed_s']:.0f}s，{passes} 遍")

    def _write(self, jid, job, turns, rows, meta) -> None:
        """和 cli.py run 一样的三件产物，只写判完的轮次。"""
        d = self.job_dir(jid)
        d.mkdir(parents=True, exist_ok=True)
        done = [r for r in rows if r]
        (d / "judge.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in done), encoding="utf-8")
        o = job["options"]
        report.write(done, turns, d / "report.md", {
            "source": o["name"] or jid, "llm": meta["llm"], "group": o["group"],
            "samples": o["samples"], "vote_k": o["vote_k"], "temp": o["temperature"],
            "elapsed": meta["elapsed_s"], "fingerprints": meta["fingerprints"],
            "rubric": meta["rubric"]})
        try:
            import sheet
            sheet.build(done, d / "复核表.xlsx")
        except ImportError:
            pass                                # 没装 openpyxl：少一张表，判定结果不受影响
