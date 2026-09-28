# -*- coding: utf-8 -*-
"""任务存储：一个 SQLite 文件。

每轮的输入和结果都落在库里，所以服务重启不丢任务：跑到一半的任务回到队列，
只重跑还没结果的轮次（已经问过模型的那些，缓存里也有，不会再花调用）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
    id        TEXT PRIMARY KEY,
    owner     TEXT NOT NULL,
    status    TEXT NOT NULL,          -- queued / running / done / failed / cancelled
    cancel    INTEGER NOT NULL DEFAULT 0,
    created   REAL NOT NULL,
    started   REAL,
    finished  REAL,
    n_turns   INTEGER NOT NULL,
    n_done    INTEGER NOT NULL DEFAULT 0,
    options   TEXT NOT NULL,
    warnings  TEXT NOT NULL DEFAULT '[]',
    meta      TEXT NOT NULL DEFAULT '{}',
    error     TEXT
);
CREATE TABLE IF NOT EXISTS turns(
    job_id  TEXT NOT NULL,
    idx     INTEGER NOT NULL,
    turn    TEXT NOT NULL,
    result  TEXT,
    PRIMARY KEY(job_id, idx)
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, created);
"""

TERMINAL = ("done", "failed", "cancelled")


def _dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False)


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    def _q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            rows = self.db.execute(sql, args).fetchall()
            self.db.commit()
            return rows

    # ---- 提交与查询 -------------------------------------------------------

    def create(self, owner: str, turns: list[dict], options: dict, warnings: list[str]) -> str:
        jid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        with self.lock:
            self.db.execute(
                "INSERT INTO jobs(id, owner, status, created, n_turns, options, warnings)"
                " VALUES(?, ?, 'queued', ?, ?, ?, ?)",
                (jid, owner, time.time(), len(turns), _dumps(options), _dumps(warnings)))
            self.db.executemany(
                "INSERT INTO turns(job_id, idx, turn) VALUES(?, ?, ?)",
                [(jid, i, _dumps(t)) for i, t in enumerate(turns)])
            self.db.commit()
        return jid

    def get(self, jid: str) -> dict | None:
        rows = self._q("SELECT * FROM jobs WHERE id = ?", (jid,))
        return self._job(rows[0]) if rows else None

    def list(self, owner: str | None, limit: int = 20) -> list[dict]:
        if owner is None:
            rows = self._q("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))
        else:
            rows = self._q("SELECT * FROM jobs WHERE owner = ? ORDER BY created DESC LIMIT ?",
                           (owner, limit))
        return [self._job(r) for r in rows]

    def queue_position(self, jid: str) -> int | None:
        """排在它前面还有几个任务（含正在跑的）。不在队列里返回 None。"""
        job = self.get(jid)
        if not job or job["status"] != "queued":
            return None
        rows = self._q("SELECT COUNT(*) AS n FROM jobs WHERE id != ? AND (status = 'running'"
                       " OR (status = 'queued' AND created < ?))", (jid, job["created"]))
        return rows[0]["n"]

    def counts(self) -> dict:
        rows = self._q("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    def turns(self, jid: str) -> list[dict]:
        rows = self._q("SELECT turn FROM turns WHERE job_id = ? ORDER BY idx", (jid,))
        return [json.loads(r["turn"]) for r in rows]

    def results(self, jid: str) -> list[dict | None]:
        """按提交顺序；还没判完的那一轮是 None。"""
        rows = self._q("SELECT result FROM turns WHERE job_id = ? ORDER BY idx", (jid,))
        return [json.loads(r["result"]) if r["result"] else None for r in rows]

    @staticmethod
    def _job(r: sqlite3.Row) -> dict:
        d = dict(r)
        for k in ("options", "warnings", "meta"):
            d[k] = json.loads(d[k])
        d["cancel"] = bool(d["cancel"])
        return d

    # ---- 执行侧 ----------------------------------------------------------

    def next_queued(self) -> str | None:
        rows = self._q("SELECT id FROM jobs WHERE status = 'queued' ORDER BY created LIMIT 1")
        return rows[0]["id"] if rows else None

    def mark_running(self, jid: str) -> None:
        self._q("UPDATE jobs SET status = 'running', started = COALESCE(started, ?) WHERE id = ?",
                (time.time(), jid))

    def pending(self, jid: str) -> list[tuple[int, dict]]:
        rows = self._q("SELECT idx, turn FROM turns WHERE job_id = ? AND result IS NULL"
                       " ORDER BY idx", (jid,))
        return [(r["idx"], json.loads(r["turn"])) for r in rows]

    def save_result(self, jid: str, idx: int, row: dict) -> None:
        with self.lock:
            self.db.execute("UPDATE turns SET result = ? WHERE job_id = ? AND idx = ?",
                            (_dumps(row), jid, idx))
            self.db.execute("UPDATE jobs SET n_done = (SELECT COUNT(*) FROM turns"
                            " WHERE job_id = ? AND result IS NOT NULL) WHERE id = ?", (jid, jid))
            self.db.commit()

    def finish(self, jid: str, status: str, meta: dict | None = None,
               error: str | None = None) -> None:
        assert status in TERMINAL
        self._q("UPDATE jobs SET status = ?, finished = ?, meta = ?, error = ? WHERE id = ?",
                (status, time.time(), _dumps(meta or {}), error, jid))

    def requeue(self, jid: str, meta: dict) -> None:
        """上游挂了：任务放回队列等恢复，已判完的轮次留着。"""
        self._q("UPDATE jobs SET status = 'queued', meta = ? WHERE id = ?", (_dumps(meta), jid))

    def request_cancel(self, jid: str) -> str | None:
        """排队中的直接取消；跑着的打标记，由执行侧在轮与轮之间停下。返回新状态。"""
        job = self.get(jid)
        if not job or job["status"] in TERMINAL:
            return job["status"] if job else None
        if job["status"] == "queued":
            self.finish(jid, "cancelled", error="提交方取消")
            return "cancelled"
        self._q("UPDATE jobs SET cancel = 1 WHERE id = ?", (jid,))
        return "running"

    def cancel_requested(self, jid: str) -> bool:
        rows = self._q("SELECT cancel FROM jobs WHERE id = ?", (jid,))
        return bool(rows and rows[0]["cancel"])

    def requeue_interrupted(self) -> list[str]:
        """服务上次退出时还在跑的任务 —— 放回队列，只补跑没结果的轮次。"""
        rows = self._q("SELECT id FROM jobs WHERE status = 'running'")
        ids = [r["id"] for r in rows]
        if ids:
            self._q("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
        return ids
