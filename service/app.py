# -*- coding: utf-8 -*-
"""HTTP 接口。标准库 ThreadingHTTPServer，JSON 进 JSON 出。接口说明见 docs/service.md。

    POST /v1/jobs                    提交一批轮次 → 202 + 任务 id
    GET  /v1/jobs                    我提交过的任务
    GET  /v1/jobs/{id}               状态、进度、排队位置、汇总
    GET  /v1/jobs/{id}/results       逐轮结果（默认精简，?full=1 原样，?format=jsonl 逐行）
    GET  /v1/jobs/{id}/report        report.md
    GET  /v1/jobs/{id}/sheet         复核表.xlsx
    POST /v1/jobs/{id}/cancel        取消
    GET  /v1/rubric                  判据表 + 判据指纹
    GET  /healthz[?upstream=1]       存活；带 upstream=1 真打一次模型
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import llm
import rubric
from rubric import THEMES, TIP_BY_CODE

from . import intake
from .runner import Runner, summary
from .store import Store

MAX_BODY = 64 * 1024 * 1024
JOB_PATH = re.compile(r"^/v1/jobs/([\w-]+)(?:/(results|report|sheet|cancel))?$")


def parse_keys(spec: str) -> dict[str, str]:
    """JUDGE_SERVICE_KEYS="alice:k1,bob:k2" → {k1: alice, k2: bob}。只写 key 不写名字也行。"""
    out = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, key = part.rpartition(":")
        out[key] = name or key[:6]
    return out


def iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def compact(r: dict | None) -> dict | None:
    """给调用方看的逐轮结果：结论、扣分、命中了哪几条、各自的引文和理由。
    证据、路由规则这些原样回传的大字段去掉，要全量用 ?full=1。"""
    if r is None:
        return None
    hits = []
    for v in r.get("verdicts") or []:
        if v.get("hit") is True:
            h = {k: x for k, x in v.items() if k != "votes"}
            h["name"] = TIP_BY_CODE[v["code"]].name if v["code"] in TIP_BY_CODE else ""
            hits.append(h)
    keep = ("case_id", "verdict", "complete", "deduct", "hard_fail", "neg_hits",
            "contract_ok", "contract_hits", "scored_rules", "undecidable", "unjudged",
            "judge_errors", "elapsed_ms")
    return {**{k: r.get(k) for k in keep}, "hits": hits}


def rubric_view() -> dict:
    return {"fingerprint": rubric.fingerprint(), "themes": [
        {"no": th.no, "name": th.name, "tips": [
            {"code": t.code, "name": t.name, "desc": t.desc, "weight": t.weight,
             "applies_when": t.applies_when or None, "right_looks_like": t.right_looks_like or None}
            for t in th.tips]} for th in THEMES]}


def coerce(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    for kind in (int, float):
        try:
            return kind(v)
        except ValueError:
            pass
    return v


class App:
    def __init__(self, data_dir: Path, client: llm.LLMClient, *, workers: int = 4,
                 keys: dict[str, str] | None = None):
        self.dir = data_dir
        self.store = Store(data_dir / "jobs.sqlite")
        self.runner = Runner(self.store, client, data_dir, workers=workers)
        self.client = client
        self.keys = keys or {}
        self.started = time.time()

    # ---- 视图 ----------------------------------------------------------

    def job_view(self, job: dict) -> dict:
        jid = job["id"]
        meta = job["meta"]
        rows = self.store.results(jid) if job["status"] == "running" else None
        base = f"/v1/jobs/{jid}"
        return {
            "id": jid, "name": job["options"].get("name") or None, "status": job["status"],
            "progress": {"done": job["n_done"], "total": job["n_turns"]},
            "queue_position": self.store.queue_position(jid),
            "created": iso(job["created"]), "started": iso(job["started"]),
            "finished": iso(job["finished"]),
            "options": job["options"], "warnings": job["warnings"], "error": job["error"],
            "note": meta.get("note"),
            "rubric": meta.get("rubric") or rubric.fingerprint(),
            "endpoint": meta.get("fingerprints") or [],
            "summary": summary(rows) if rows is not None else meta.get("summary"),
            "links": {"self": base, "results": base + "/results", "report": base + "/report",
                      "sheet": base + "/sheet", "cancel": base + "/cancel"},
        }

    # ---- 路由 ----------------------------------------------------------

    def handle(self, method: str, path: str, query: dict, body: bytes, ctype: str,
               owner: str | None):
        """返回 (状态码, 载荷, 额外响应头)。载荷是 dict 走 JSON，(bytes, type) 原样。"""
        if method == "GET" and path == "/v1/rubric":
            return 200, rubric_view(), {}
        if method == "POST" and path == "/v1/jobs":
            return self.create(query, body, ctype, owner)
        if method == "GET" and path == "/v1/jobs":
            try:
                limit = max(1, min(int(query.get("limit", 20)), 200))
            except ValueError:
                return 400, {"error": "limit 要是整数"}, {}
            jobs = self.store.list(None if owner is None else owner, limit)
            return 200, {"jobs": [self.job_view(j) for j in jobs]}, {}

        m = JOB_PATH.match(path)
        if not m:
            return 404, {"error": f"没有这个接口：{method} {path}"}, {}
        jid, sub = m.group(1), m.group(2)
        job = self.store.get(jid)
        if not job or (owner is not None and job["owner"] != owner):
            return 404, {"error": f"没有这个任务：{jid}"}, {}

        if sub is None and method == "GET":
            return 200, self.job_view(job), {}
        if sub == "cancel" and method == "POST":
            st = self.store.request_cancel(jid)
            self.runner.poke()
            return 202, {"id": jid, "status": st,
                         "note": "排队中的已取消；在跑的会在当前几轮判完后停下，已判完的保留"}, {}
        if sub == "results" and method == "GET":
            rows = self.store.results(jid)
            full = str(query.get("full", "")).lower() in ("1", "true")
            rows = rows if full else [compact(r) for r in rows]
            if query.get("format") == "jsonl":
                blob = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows if r)
                return 200, (blob.encode(), "application/x-ndjson; charset=utf-8"), {}
            return 200, {"id": jid, "status": job["status"],
                         "progress": {"done": job["n_done"], "total": job["n_turns"]},
                         "results": rows}, {}
        if sub in ("report", "sheet") and method == "GET":
            f = self.runner.job_dir(jid) / ("report.md" if sub == "report" else "复核表.xlsx")
            if not f.exists():
                why = ("任务还没结束" if job["status"] not in ("done", "failed", "cancelled")
                       else "没有产出（一轮都没判完，或服务端没装 openpyxl）")
                return 409 if job["status"] in ("queued", "running") else 404, {"error": why}, {}
            kind = ("text/markdown; charset=utf-8" if sub == "report" else
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            head = {} if sub == "report" else {
                "Content-Disposition": f"attachment; filename=\"{jid}.xlsx\""}
            return 200, (f.read_bytes(), kind), head
        return 405, {"error": f"{path} 不支持 {method}"}, {}

    def create(self, query: dict, body: bytes, ctype: str, owner: str | None):
        opts = {k: coerce(v) for k, v in query.items()}
        try:
            text = body.decode("utf-8")
            if "ndjson" in ctype or "jsonl" in ctype:
                raw = [json.loads(l) for l in text.splitlines() if l.strip()]
            else:
                raw = json.loads(text) if text.strip() else None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return 400, {"error": f"请求体解析失败：{exc}"}, {}
        if isinstance(raw, dict):
            opts.update(raw.get("options") or {})
            raw = raw.get("turns")
        try:
            o = intake.options(opts)
            turns, truth, warns = intake.turns(raw)
        except intake.BadRequest as exc:
            return 400, {"error": str(exc)}, {}
        jid = self.store.create(owner or "anonymous", turns, o, warns)
        self.runner.poke()
        job = self.store.get(jid)
        return 202, self.job_view(job), {"Location": f"/v1/jobs/{jid}"}

    def health(self, upstream: bool) -> dict:
        out = {"ok": True, "llm": self.client.name, "rubric": rubric.fingerprint(),
               "workers": self.runner.workers, "running": self.runner.current,
               "paused_until": iso(self.runner.paused_until) if self.runner.paused_until > time.time() else None,
               "last_outage": self.runner.last_outage,
               "jobs": self.store.counts(), "uptime_s": round(time.time() - self.started)}
        if upstream:
            res: dict = {}

            def probe():
                t = time.time()
                try:
                    self.client.complete("你是连通性探针。", "只回复 OK 两个字母。", max_tokens=8)
                    res.update(ok=True)
                except Exception as exc:
                    res.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                res["latency_ms"] = round((time.time() - t) * 1000)

            th = threading.Thread(target=probe, daemon=True)
            th.start()
            th.join(30)
            out["upstream"] = res or {"ok": False, "error": "30 秒没返回"}
            out["ok"] = bool(out["upstream"].get("ok"))
        return out


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "deepcoin-judge"
        protocol_version = "HTTP/1.1"

        def _send(self, code: int, payload, headers: dict | None = None) -> None:
            if isinstance(payload, tuple):
                data, kind = payload
            else:
                data = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
                kind = "application/json; charset=utf-8"
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _owner(self) -> tuple[bool, str | None]:
            """(放行, 调用方名字)。没配 key 就是开放模式，所有人互相看得见任务。"""
            if not app.keys:
                return True, None
            auth = self.headers.get("Authorization", "")
            key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            for k, name in app.keys.items():
                if key and hmac.compare_digest(key, k):
                    return True, name
            return False, None

        def _dispatch(self, method: str) -> None:
            u = urlparse(self.path)
            query = {k: v[-1] for k, v in parse_qs(u.query).items()}
            try:
                if method == "GET" and u.path == "/healthz":
                    up = query.get("upstream") in ("1", "true")
                    h = app.health(up)
                    return self._send(200 if h["ok"] else 503, h)
                ok, owner = self._owner()
                if not ok:
                    return self._send(401, {"error": "缺少或错误的 API key："
                                                     "Authorization: Bearer <key>"})
                body = b""
                if method == "POST":
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        return self._send(413, {"error": f"请求体超过 {MAX_BODY >> 20}MB"})
                    body = self.rfile.read(n)
                code, payload, head = app.handle(method, u.path, query, body,
                                                 self.headers.get("Content-Type", ""), owner)
                self._send(code, payload, head)
            except Exception as exc:
                self._send(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def _unsupported(self):
            self._send(405, {"error": f"不支持 {self.command}，只有 GET / POST"},
                       {"Allow": "GET, POST"})

        do_PUT = do_PATCH = do_DELETE = _unsupported

        def log_message(self, fmt, *args):
            if os.environ.get("JUDGE_SERVICE_QUIET"):
                return
            print(f"{time.strftime('%H:%M:%S')} {self.address_string()} {fmt % args}")

    return Handler


def serve(host: str, port: int, data_dir: Path, client: llm.LLMClient, *, workers: int = 4) -> int:
    keys = parse_keys(os.environ.get("JUDGE_SERVICE_KEYS", ""))
    app = App(data_dir, client, workers=workers, keys=keys)
    app.runner.start()
    srv = ThreadingHTTPServer((host, port), make_handler(app))
    srv.daemon_threads = True
    print(f"judge 服务 http://{host}:{port}　模型 {client.name}　并发 {workers}"
          f"　判据指纹 {rubric.fingerprint()}　数据 {data_dir}")
    if keys:
        print(f"鉴权：{len(keys)} 个 API key（{'、'.join(sorted(set(keys.values())))}）")
    else:
        print("！ 没配 JUDGE_SERVICE_KEYS —— 开放模式，谁都能提交、谁都能看所有任务"
              + ("。现在监听的不是本机地址，别这样对外暴露" if host not in ("127.0.0.1", "localhost") else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。在跑的任务下次启动时接着跑（已判完的轮次不重判）")
    finally:
        app.runner.stop()
        srv.server_close()
    return 0
