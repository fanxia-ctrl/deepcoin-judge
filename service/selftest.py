# -*- coding: utf-8 -*-
"""服务自检：进程内起一个真的 HTTP 服务（mock 模型、临时目录、随机端口），走一遍接口。

由 cli.py selftest 调用，几秒跑完，不打真实端点。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import llm
import pipeline
from rubric import THEMES, groups

from .app import App, make_handler

# 本机系统代理会把 127.0.0.1 也代理出去，测试请求一律直连
_OPEN = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


class CountingMock(llm.MockClient):
    """会数调用次数、可以放慢的 mock —— 取消和续跑要靠它观察。"""

    def __init__(self, delay: float = 0.0):
        self.delay, self.calls, self.lock = delay, 0, threading.Lock()

    def complete(self, *a, **k):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.calls += 1
        return super().complete(*a, **k)


class FlakyMock(CountingMock):
    """前 n 次调用直接抛错（模拟端点挂掉），或者只要 prompt 里带某个标记就一直抛错。"""

    def __init__(self, fail_first: int = 0, fail_marker: str | None = None):
        super().__init__()
        self.fail_first, self.fail_marker = fail_first, fail_marker

    def complete(self, system, user, **k):
        with self.lock:
            self.calls += 1
            n = self.calls
        if n <= self.fail_first or (self.fail_marker and self.fail_marker in user):
            raise RuntimeError("RemoteDisconnected: Remote end closed connection without response")
        return llm.MockClient.complete(self, system, user, **k)


def _turn(i: int, **kw) -> dict:
    t = {"case_id": f"st:{i:03d}", "query": "返佣可以关闭吗",
         "voice": "您可以在【资产】页面关闭该功能。", "detail": "",
         "raw_answer": '{"voice":"x","detail":"","panel":null,"input_request":null}',
         "envelope_ok": True, "kb_count": 1, "kb_top_score": 0.6,
         "kb_hits": [{"score": 0.6, "content": "返佣实时到账，不可关闭。"}],
         "evidence_text": "【知识库】返佣实时到账，不可关闭。", "route_rules": "返佣类问题按口径回答。"}
    t.update(kw)
    return t


class _Svc:
    def __init__(self, d: Path, client, *, workers=2, keys=None, start=True):
        self.app = App(d, client, workers=workers, keys=keys)
        if start:
            self.app.runner.start()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        self.srv.daemon_threads = True
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_port}"

    def call(self, method, path, body=None, key=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        hd = {"Content-Type": "application/json"}
        if key:
            hd["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=hd)
        try:
            r = _OPEN(req, timeout=20)
            code, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            code, raw = e.code, e.read()
        try:
            return code, json.loads(raw)
        except ValueError:
            return code, raw

    def wait(self, jid, key=None, timeout=20.0):
        end = time.time() + timeout
        while time.time() < end:
            _, j = self.call("GET", f"/v1/jobs/{jid}", key=key)
            if j.get("status") in ("done", "failed", "cancelled"):
                return j
            time.sleep(0.05)
        return j

    def close(self):
        self.app.runner.stop()
        self.srv.shutdown()
        self.srv.server_close()


def run(ck) -> None:
    os.environ["JUDGE_SERVICE_QUIET"] = "1"
    tmp = Path(tempfile.mkdtemp(prefix="judge-svc-"))

    # 1 跑通，且和直接调 pipeline 判出来的一样 —— 服务只是一层壳
    s = _Svc(tmp / "a", CountingMock())
    turns = [_turn(i) for i in range(3)]
    code, j = s.call("POST", "/v1/jobs", {"turns": turns, "options": {"samples": 1}})
    ck("服务：提交返回 202 和任务 id", code == 202 and bool(j.get("id")), str(code))
    j = s.wait(j["id"])
    ck("服务：任务跑完", j["status"] == "done" and j["progress"] == {"done": 3, "total": 3},
       f"{j['status']} {j['progress']}")
    _, res = s.call("GET", f"/v1/jobs/{j['id']}/results?full=1")
    direct = pipeline.judge_turn(llm.MockClient(), dict(turns[0]), {}, 4000,
                                 groups("one", THEMES), pipeline.Cache(None), 1, False, 1, 1, 0.0)
    got = res["results"][0]
    ck("服务：结果与直接调 pipeline 一致",
       (got["verdict"], got["neg_hits"], got["deduct"]) ==
       (direct["verdict"], direct["neg_hits"], direct["deduct"]),
       f"{got['verdict']} {got['neg_hits']}")
    code, rep = s.call("GET", f"/v1/jobs/{j['id']}/report")
    ck("服务：report 可取", code == 200 and b"judge" in rep)

    # 2 只带 query + answer 的外部格式：不能被契约规则（信封 / 空答）误伤
    code, j2 = s.call("POST", "/v1/jobs", {"turns": [{"query": "返佣在哪看",
                                                      "answer": "您可以在【资产】-【返佣】查看。"}],
                                           "options": {"samples": 1}})
    j2 = s.wait(j2["id"])
    _, r2 = s.call("GET", f"/v1/jobs/{j2['id']}/results")
    r2 = r2["results"][0]
    ck("服务：通用格式不触发信封/空答契约", r2["contract_ok"] is True, str(r2["contract_hits"]))
    ck("服务：缺证据时提交就给警告", any("evidence_text" in w for w in j2["warnings"]))

    # 3 校验
    bad = [s.call("POST", "/v1/jobs", b)[0] for b in
           ({"turns": []}, {"turns": [{"query": "x"}]},
            {"turns": [{"query": "x", "answer": "y"}], "options": {"samples": 9}})]
    ck("服务：非法输入回 400", bad == [400, 400, 400], str(bad))
    ck("服务：未知接口 404 / 方法 405",
       (s.call("GET", "/v1/nope")[0], s.call("DELETE", "/v1/jobs")[0]) == (404, 405))
    s.close()

    # 4 鉴权：没 key 进不来，别人的任务看不见
    s = _Svc(tmp / "b", CountingMock(), keys={"k-alice": "alice", "k-bob": "bob"})
    ck("服务：没带 key 回 401", s.call("GET", "/v1/jobs")[0] == 401)
    ck("服务：错 key 回 401", s.call("GET", "/v1/jobs", key="k-eve")[0] == 401)
    code, ja = s.call("POST", "/v1/jobs", {"turns": [_turn(0)], "options": {"samples": 1}},
                      key="k-alice")
    ck("服务：对的 key 能提交", code == 202, str(code))
    ck("服务：别人的任务看不见",
       (s.call("GET", f"/v1/jobs/{ja['id']}", key="k-bob")[0],
        s.call("GET", f"/v1/jobs/{ja['id']}", key="k-alice")[0]) == (404, 200))
    ck("服务：healthz 不需要 key", s.call("GET", "/healthz")[0] == 200)
    s.close()

    # 5 取消：跑到一半停下，已判完的保留
    s = _Svc(tmp / "c", CountingMock(delay=0.03), workers=1)
    _, jc = s.call("POST", "/v1/jobs", {"turns": [_turn(i) for i in range(40)],
                                        "options": {"samples": 1}})
    time.sleep(0.15)
    s.call("POST", f"/v1/jobs/{jc['id']}/cancel")
    jc = s.wait(jc["id"])
    ck("服务：取消后停在半路", jc["status"] == "cancelled" and jc["progress"]["done"] < 40,
       f"{jc['status']} {jc['progress']}")
    s.close()

    # 6 重启续跑：上次跑到一半的任务回到队列，只补跑没结果的轮次
    d = tmp / "d"
    s = _Svc(d, CountingMock(), start=False)              # 执行器不启动 = 模拟跑到一半时进程没了
    _, jr = s.call("POST", "/v1/jobs", {"turns": [_turn(i) for i in range(6)],
                                        "options": {"samples": 3}})
    st = s.app.store
    st.mark_running(jr["id"])
    for i, t in st.pending(jr["id"])[:2]:
        st.save_result(jr["id"], i, pipeline.judge_turn(
            llm.MockClient(), t, {}, 4000, groups("one", THEMES), pipeline.Cache(None),
            1, False, 3, 2, 0.0))
    s.close()
    counter = CountingMock()
    s = _Svc(d, counter)                                  # 重新起服务，同一个数据目录
    jr = s.wait(jr["id"])
    ck("服务：重启后接着跑完", jr["status"] == "done" and jr["progress"]["done"] == 6,
       f"{jr['status']} {jr['progress']}")
    ck("服务：已判完的轮次不重判", counter.calls == 4 * 3, f"调了 {counter.calls} 次（应为 4 轮 × 3 采样）")
    s.close()

    # 7 上游挂一阵：熔断暂停、恢复后接着跑，不能有一轮被记成「判定不完整」
    s = _Svc(tmp / "e", FlakyMock(fail_first=12), workers=1)
    s.app.runner.backoff = (0.1,)
    _, jo = s.call("POST", "/v1/jobs", {"turns": [_turn(i) for i in range(8)],
                                        "options": {"samples": 1, "retries": 0}})
    jo = s.wait(jo["id"])
    inc = (jo.get("summary") or {}).get("incomplete")
    ck("服务：上游挂了会熔断暂停", bool(s.call("GET", "/healthz")[1].get("last_outage")))
    ck("服务：恢复后跑完且没有不完整的轮次",
       jo["status"] == "done" and jo["progress"]["done"] == 8 and inc == 0,
       f"{jo['status']} {jo['progress']} 不完整 {inc}")
    s.close()

    # 8 只有一轮一直调不通：判满几遍才认，其余轮次不受影响
    s = _Svc(tmp / "f", FlakyMock(fail_marker="永远调不通"), workers=1)
    bad_turn = _turn(99, query="永远调不通的问题")
    _, jf = s.call("POST", "/v1/jobs", {"turns": [_turn(i) for i in range(4)] + [bad_turn],
                                        "options": {"samples": 1, "retries": 0}})
    jf = s.wait(jf["id"])
    _, rf = s.call("GET", f"/v1/jobs/{jf['id']}/results")
    flags = [r["complete"] for r in rf["results"]]
    from .runner import MAX_PASSES
    ck("服务：个别轮次调不通，判满几遍后按不完整落盘",
       jf["status"] == "done" and flags == [True] * 4 + [False],
       f"{jf['status']} {flags}")
    ck("服务：调不通的那轮重试了 MAX_PASSES 遍",
       s.app.runner.client.calls == 4 + MAX_PASSES, f"共调 {s.app.runner.client.calls} 次")
    s.close()
