#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge 侧的知识库复核检索 —— 零依赖 BM25。

被测 agent 那一轮召回了什么，和知识库里到底写着什么，是两件事。
judge 只拿前者当口径，就只能判出「无据」，判不出「说错了」：
`neg_1_1` 的 11 条漏判里，事实在知识库里逐字都有，只是没进那一轮的召回。

这里用问题+回答再检一次全量语料，作为 <知识库复核> 材料交给 T1。
中文按字符二元组切，英文数字按词切；索引建一次缓存到磁盘。
"""
from __future__ import annotations

import json
import math
import pickle
import re
from array import array
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "data/kb_corpus.jsonl"
CACHE = ROOT / "data/kb_corpus.index"

CJK = re.compile(r"[一-鿿]")
WORD = re.compile(r"[a-zA-Z][a-zA-Z0-9]+|\d+(?:\.\d+)?%?")
K1, B = 1.5, 0.75
# 这些字对切出来到处都是，不承载区分度
STOP = {"的的", "了的", "您可", "可以", "进行", "如果", "我们", "您的", "请您", "以及"}


def tokens(text: str) -> list[str]:
    out = [w.lower() for w in WORD.findall(text)]
    cjk = CJK.findall(text)
    # 只对连续中文取二元组：先按非中文切段，段内滑窗
    for seg in re.split(r"[^一-鿿]+", text):
        for i in range(len(seg) - 1):
            t = seg[i:i + 2]
            if t not in STOP:
                out.append(t)
    if not cjk and not out:
        return []
    return out


class Index:
    def __init__(self, docs: list[dict]):
        self.docs = docs
        self.post: dict[str, array] = {}
        self.tf: dict[str, array] = {}
        self.dl = array("i", [0]) * 0
        post: dict[str, list[int]] = defaultdict(list)
        tf: dict[str, list[int]] = defaultdict(list)
        dl = []
        for i, d in enumerate(docs):
            counts: dict[str, int] = defaultdict(int)
            for t in tokens(d["text"]):
                counts[t] += 1
            dl.append(sum(counts.values()) or 1)
            for t, c in counts.items():
                post[t].append(i)
                tf[t].append(min(c, 255))
        self.post = {t: array("i", v) for t, v in post.items()}
        self.tf = {t: array("H", v) for t, v in tf.items()}
        self.dl = array("i", dl)
        self.avgdl = sum(dl) / max(1, len(dl))
        self.N = len(docs)

    def search(self, text: str, k: int = 8) -> list[tuple[float, dict]]:
        q = [t for t in dict.fromkeys(tokens(text)) if t in self.post]
        if not q:
            return []
        scores: dict[int, float] = defaultdict(float)
        for t in q:
            ids, tfs = self.post[t], self.tf[t]
            idf = math.log(1 + (self.N - len(ids) + 0.5) / (len(ids) + 0.5))
            if len(ids) > self.N * 0.25:          # 烂大街的词不算分
                continue
            for j, i in enumerate(ids):
                f = tfs[j]
                scores[i] += idf * f * (K1 + 1) / (
                    f + K1 * (1 - B + B * self.dl[i] / self.avgdl))
        top = sorted(scores.items(), key=lambda kv: -kv[1])[:k * 3]
        out, seen = [], set()
        for i, s in top:
            d = self.docs[i]
            key = re.sub(r"\s+", "", d["text"])[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append((s, d))
            if len(out) >= k:
                break
        return out


_IDX: Index | None = None


def index(corpus: Path = CORPUS, cache: Path = CACHE) -> Index | None:
    """建一次，之后从缓存读。语料不在就返回 None —— 调用方降级成不给这份材料。

    缓存存的是纯数据（dict + array），不是对象 —— pickle 一个类实例会把
    `__main__.Index` 写进去，换个入口再读就找不到这个类。
    """
    global _IDX
    if _IDX is not None:
        return _IDX
    if not corpus.exists():
        return None
    if cache.exists() and cache.stat().st_mtime >= corpus.stat().st_mtime:
        with cache.open("rb") as f:
            d = pickle.load(f)
        _IDX = Index.__new__(Index)
        _IDX.__dict__.update(d)
        return _IDX
    docs = [json.loads(l) for l in corpus.read_text(encoding="utf-8").splitlines() if l.strip()]
    _IDX = Index(docs)
    with cache.open("wb") as f:
        pickle.dump(dict(_IDX.__dict__), f, protocol=4)
    return _IDX


def lookup(query: str, answer: str, k: int = 8, max_chars: int = 2600) -> str:
    """返回喂给 prompt 的 <知识库复核> 正文。语料缺失时返回空串。"""
    idx = index()
    if idx is None:
        return ""
    hits = idx.search(f"{query}\n{answer}", k=k)
    if not hits:
        return ""
    out, used = [], 0
    for n, (s, d) in enumerate(hits, 1):
        piece = f"[K{n}] {d['dataset']} · {d['document']}\n{d['text']}"
        if used + len(piece) > max_chars:
            break
        out.append(piece)
        used += len(piece)
    return "\n\n".join(out)


if __name__ == "__main__":
    import sys
    idx = index()
    print(f"语料 {idx.N} 条，词表 {len(idx.post)}，平均长度 {idx.avgdl:.0f}")
    q = sys.argv[1] if len(sys.argv) > 1 else "怎么能有这仓位合并这四个字"
    a = sys.argv[2] if len(sys.argv) > 2 else "目前平台不支持仓位合并操作"
    for s, d in idx.search(f"{q}\n{a}", k=5):
        print(f"\n[{s:.1f}] {d['dataset']} · {d['document']}\n{d['text'][:300]}")
