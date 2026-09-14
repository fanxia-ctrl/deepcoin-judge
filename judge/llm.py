#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 客户端。两种真实现都写好了，**只差凭证**。

在 config.env 里填任意一组，然后 --llm auto 就能跑：

  # 走 Dify 应用
  JUDGE_DIFY_API_KEY=app-xxxx
  JUDGE_URL=https://ngrok.deeptest.cc/v1/chat-messages   # 不填则用 BENCH_URL

  # 或走任何 OpenAI 兼容端点
  JUDGE_BASE_URL=https://xxx/v1
  JUDGE_API_KEY=sk-xxxx
  JUDGE_MODEL=qwen3.6-72b

两者都强制 temperature=0；OpenAI 兼容那条还会请求 JSON mode。
"""
from __future__ import annotations

import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    finish_reason: str = ""        # "length" = 被 max_tokens 截断
    reasoning_chars: int = 0       # 思考模型吐在 reasoning_content 里的字数，它也吃 max_tokens


class LLMClient:
    """判定用的最小接口。"""

    name = "base"

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 8000) -> tuple[str, Usage]:
        """返回 (模型原文, usage)。原文应当是 JSON 字符串。"""
        raise NotImplementedError


def _post(url: str, headers: dict, payload: dict, timeout: int) -> dict:
    """POST JSON，重试 429/5xx。线程安全：每次调用自己建连接。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    last = ""
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read().decode('utf-8', 'ignore')[:300]}"
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last) from None
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
            if attempt < 2:
                time.sleep(2)
                continue
            raise RuntimeError(last) from None
    raise RuntimeError(last)


class DifyClient(LLMClient):
    """经 Dify 应用调 judge 模型。blocking 模式，answer 字段就是模型原文。

    应用的 Start 变量若有必填项，在 JUDGE_DIFY_INPUTS 里给一份 JSON。
    """

    name = "dify"

    def __init__(self) -> None:
        self.url = (os.environ.get("JUDGE_URL")
                    or os.environ.get("BENCH_URL", "")).strip()
        self.key = os.environ.get("JUDGE_DIFY_API_KEY", "").strip()
        self.timeout = int(os.environ.get("JUDGE_TIMEOUT", "120"))
        try:
            self.inputs = json.loads(os.environ.get("JUDGE_DIFY_INPUTS", "{}"))
        except json.JSONDecodeError:
            self.inputs = {}
        self.ready = bool(self.url and self.key)
        self.why = "" if self.ready else "缺 JUDGE_DIFY_API_KEY 或 JUDGE_URL/BENCH_URL"

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 8000) -> tuple[str, Usage]:
        if not self.ready:
            raise NotImplementedError(self.why)
        # Dify 应用没有 system 槽位，两段拼一起发；判定纪律都在 system 里，放前面
        payload = {"query": system + "\n\n" + user, "user": "judge",
                   "response_mode": "blocking", "conversation_id": "",
                   "inputs": self.inputs, "files": []}
        obj = _post(self.url, {"Authorization": f"Bearer {self.key}"},
                    payload, self.timeout)
        u = (obj.get("metadata") or {}).get("usage") or {}
        return str(obj.get("answer") or ""), Usage(
            int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0))


class OpenAICompatClient(LLMClient):
    """任何 OpenAI 兼容的 /chat/completions 端点。温度 0 + JSON mode。"""

    name = "openai"

    def __init__(self) -> None:
        base = os.environ.get("JUDGE_BASE_URL", "").strip().rstrip("/")
        self.url = base + "/chat/completions" if base else ""
        self.key = os.environ.get("JUDGE_API_KEY", "").strip()
        self.model = os.environ.get("JUDGE_MODEL", "").strip()
        self.timeout = int(os.environ.get("JUDGE_TIMEOUT", "120"))
        self.json_mode = os.environ.get("JUDGE_JSON_MODE", "1") != "0"
        # 额外请求字段，JSON。典型用途：关思考模型的思考，例如
        #   JUDGE_EXTRA_BODY={"chat_template_kwargs":{"enable_thinking":false}}
        #   JUDGE_EXTRA_BODY={"thinking":{"type":"disabled"}}
        # 用 probe 试哪个对这个端点有效。
        self.extra: dict = {}
        raw_extra = os.environ.get("JUDGE_EXTRA_BODY", "").strip()
        if raw_extra:
            try:
                self.extra = json.loads(raw_extra)
            except json.JSONDecodeError:
                self.extra = {}
        self.ready = bool(self.url and self.model)
        self.why = "" if self.ready else "缺 JUDGE_BASE_URL 或 JUDGE_MODEL"

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 8000) -> tuple[str, Usage]:
        if not self.ready:
            raise NotImplementedError(self.why)
        payload = {
            "model": self.model, "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if self.json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.extra)
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        obj = _post(self.url, headers, payload, self.timeout)
        ch = (obj.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        txt = msg.get("content") or ""
        u = obj.get("usage") or {}
        return str(txt), Usage(int(u.get("prompt_tokens") or 0),
                               int(u.get("completion_tokens") or 0),
                               str(ch.get("finish_reason") or ""),
                               len(str(msg.get("reasoning_content") or msg.get("reasoning") or "")))


class MockClient(LLMClient):
    """不调模型，用关键词凑一份合法输出 —— 只为把链路跑通。判定无意义。"""

    name = "mock"
    HINTS = {
        "neg_1_1": ("可以关闭", "关闭该功能", "模拟交易", "点击.*关闭"),
        "neg_2_1": ("返佣比例", "建议您查看"),
        "neg_2_2": ("在哪里查看", "页面查看"),
        "neg_2_3": ("就是这样",),
        "neg_2_4": ("建议检查", "相关页面", "类似"),
        "neg_2_5": (),
        "neg_3_1": ("可能是", "可能由于", "应该是", "您的账户", "您的仓位"),
        "neg_4_1": ("赔偿", "赔付", "帮您关闭", "会为您"),
        "neg_4_2": ("VPN", "加速器", "建议买入"),
        "neg_4_3": ("接口", "知识库", "工作流", "提示词"),
        "neg_4_4": (),
        "neg_5_1": ("一般", "通常", "大约"),
        "neg_5_2": (),
        "neg_5_3": ("人工客服", "联系客服", "暂时处理不了"),
    }
    APPLIES = {
        "neg_1_1": ("不支持", "无法", "已下线"),
        "neg_2_2": ("怎么算", "多少", "在哪", "怎么设置"),
        "neg_2_4": ("怎么算", "多少", "在哪", "怎么设置"),
        "neg_2_5": (),
        "neg_3_1": ("我的", "我这", "帮我"),
        "neg_4_4": ("在吗", "听得到"),
        "neg_5_1": (),
        "neg_5_2": (),
    }
    TYPES = {"fact_type": "入口/功能存在性", "claim_type": "入口路径"}

    def complete(self, system: str, user: str, *, temperature: float = 0.0,
                 max_tokens: int = 8000) -> tuple[str, Usage]:
        codes = list(dict.fromkeys(re.findall(r"`(neg_\d_\d)`", system)))
        answer = q = ""
        m = re.search(r"<回答>\n(.*?)\n</回答>", user, re.S)
        if m:
            answer = m.group(1)
        m = re.search(r"<原始问题>\n(.*?)\n</原始问题>", user, re.S)
        if m:
            q = m.group(1)
        rnd = random.Random(hash(answer) & 0xFFFF)
        out = []
        for c in codes:
            pats = self.HINTS.get(c, ())
            hit = any(re.search(p, answer) for p in pats) if pats else rnd.random() < 0.06
            rec = {"code": c, "hit": bool(hit),
                   "quote": (re.search("|".join(pats), answer).group(0)
                             if hit and pats else (answer[:12] if hit else "")),
                   "why": "mock：关键词命中" if hit else "mock：未命中"}
            if c in self.APPLIES:
                ap = self.APPLIES[c]
                rec["applies"] = bool(any(x in q for x in ap)) if ap else rnd.random() < 0.3
            for f, v in self.TYPES.items():
                if f in system and hit:
                    rec[f] = v
            out.append(rec)
        body = json.dumps({"verdicts": out}, ensure_ascii=False)
        return body, Usage(len(system) + len(user), len(body))


def get_client(kind: str = "auto") -> LLMClient:
    """auto：谁配好了用谁，都没配就报清楚缺什么。"""
    if kind != "auto":
        return {"mock": MockClient, "dify": DifyClient,
                "openai": OpenAICompatClient}[kind]()
    for cls in (OpenAICompatClient, DifyClient):
        c = cls()
        if getattr(c, "ready", False):
            return c
    raise NotImplementedError(
        "judge 的模型还没配。在 config.env 里填一组：\n"
        "  JUDGE_BASE_URL= / JUDGE_API_KEY= / JUDGE_MODEL=   （OpenAI 兼容端点）\n"
        "  或 JUDGE_DIFY_API_KEY=                            （Dify 应用）\n"
        "只想跑通链路：--llm mock")


def status() -> str:
    L = []
    for cls in (OpenAICompatClient, DifyClient):
        c = cls()
        L.append(f"  {c.name:8} {'可用' if c.ready else '未配置'}"
                 + (f"　{c.why}" if not c.ready else
                    f"　{getattr(c, 'model', '') or getattr(c, 'url', '')}"))
    L.append("  mock     可用　（判定无意义，只跑链路）")
    return "\n".join(L)
