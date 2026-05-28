#!/usr/bin/env python3
"""
永生记忆库 — 语义搜索引擎 v0.2
支持两种模式（自动切换）：
  - Embedding 向量检索（需要 OPENAI_API_KEY，质量最高）
  - TF-IDF 关键词匹配（无依赖，零成本回退）
"""

import json
import sys
import math
import os
import re
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path
from collections import Counter


# 时间衰减：相关性仍是主轴，但越新的记录得分越高，避免旧内容长期压住新进展。
# 今天≈2.0x，120天≈1.37x，一年≈1.05x。
RECENCY_TAU_DAYS = 120.0
RECENCY_BOOST = 1.0


def recency_multiplier(ts: str) -> float:
    if not ts:
        return 1.0
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return 1.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    if age_days < 0:
        age_days = 0.0
    return 1.0 + RECENCY_BOOST * math.exp(-age_days / RECENCY_TAU_DAYS)


IMMORTAL_DIR = Path.home() / ".immortal"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
EMBEDDINGS_FILE = IMMORTAL_DIR / "embeddings.jsonl"
EMBEDDINGS_DIR = IMMORTAL_DIR / "embeddings"


def redact(text: str) -> str:
    patterns = [
        (r"\bcli_[A-Za-z0-9_\-]{8,}\b", "cli_[REDACTED]"),
        (r"(?i)(app secret\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(api\s*key\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(apikey\s*[:：]?\s*)[A-Za-z0-9_\-]{12,}", r"\1[REDACTED]"),
        (r"(?i)(password\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"(?i)(密码\s*[:：]?\s*)\S+", r"\1[REDACTED]"),
        (r"sk-[A-Za-z0-9_\-]{12,}", "sk-[REDACTED]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


# ============================================================
# Embedding 引擎（OpenAI text-embedding-3-small）
# ============================================================

def _get_embedding_client():
    """初始化 OpenAI embedding 客户端。"""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        import openai
        return openai.OpenAI(api_key=api_key)
    except ImportError:
        return None


def _get_embedding(client, text: str, dimensions: int = 512) -> list:
    """获取文本的 embedding 向量。"""
    resp = client.embeddings.create(
        model="text-embedding-3-small",
        input=text[:8000],
        dimensions=dimensions,
    )
    return resp.data[0].embedding


def build_embeddings(since: Optional[str] = None) -> int:
    """为所有记录生成 embedding 并存储。"""
    client = _get_embedding_client()
    if client is None:
        print("未检测到 OPENAI_API_KEY，跳过 embedding 生成。")
        return 0

    EMBEDDINGS_DIR.mkdir(parents=True, exist_ok=True)

    # 加载已有 embedding 的 ID 集合
    existing_ids = set()
    for emb_file in EMBEDDINGS_DIR.glob("*.jsonl"):
        with open(emb_file, "r") as f:
            for line in f:
                try:
                    obj = json.loads(line.strip())
                    existing_ids.add(obj.get("id", ""))
                except json.JSONDecodeError:
                    continue

    # 加载需要 embedding 的记录（只取对话类型）
    records = []
    if not INDEX_FILE.exists():
        return 0

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                record = json.loads(line.strip())
                if record.get("type") != "conversation":
                    continue
                if record.get("id", "") in existing_ids:
                    continue
                if not record.get("content", "").strip():
                    continue
                records.append(record)
            except json.JSONDecodeError:
                continue

    if not records:
        print("没有需要生成 embedding 的新记录。")
        return 0

    print(f"开始为 {len(records)} 条记录生成 embedding...")
    total = 0
    batch = []
    batch_ids = []

    # 按日期分文件存储
    for record in records:
        content = record.get("content", "")[:4000]
        ts = record.get("timestamp", "")
        date = ts[:10] if ts else "unknown"

        batch.append(content)
        batch_ids.append((record, date))

        if len(batch) >= 50:
            _flush_batch(client, batch, batch_ids, EMBEDDINGS_DIR)
            total += len(batch)
            print(f"  已处理 {total}/{len(records)}")
            batch = []
            batch_ids = []

    if batch:
        _flush_batch(client, batch, batch_ids, EMBEDDINGS_DIR)
        total += len(batch)

    return total


def _flush_batch(client, texts: list, batch_ids: list, output_dir: Path):
    """批量生成 embedding 并写入文件。"""
    try:
        resp = client.embeddings.create(
            model="text-embedding-3-small",
            input=[t[:8000] for t in texts],
            dimensions=512,
        )
        embeddings = [d.embedding for d in resp.data]
    except Exception as e:
        print(f"  Embedding 失败: {e}")
        return

    # 按日期分文件
    by_date = {}
    for (record, date), emb in zip(batch_ids, embeddings):
        if date not in by_date:
            by_date[date] = []
        by_date[date].append({
            "id": record["id"],
            "embedding": emb,
            "timestamp": record.get("timestamp", ""),
            "source": record.get("source", ""),
            "project": record.get("project", ""),
            "role": record.get("role", ""),
            "content_preview": record.get("content", "")[:200],
        })

    for date, entries in by_date.items():
        emb_file = output_dir / f"{date}.jsonl"
        with open(emb_file, "a", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================
# 向量搜索
# ============================================================

def _cosine_similarity(a: list, b: list) -> float:
    """计算余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embedding_search(query: str, limit: int = 20, source: Optional[str] = None,
                     since: Optional[str] = None, source_prefix: Optional[str] = None) -> list:
    """使用 embedding 向量搜索。"""
    client = _get_embedding_client()
    if client is None:
        return []

    # 获取查询的 embedding
    try:
        query_vec = _get_embedding(client, query)
    except Exception:
        return []

    results = []
    # 遍历所有 embedding 文件
    for emb_file in EMBEDDINGS_DIR.glob("*.jsonl"):
        with open(emb_file, "r") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                # 过滤
                if source and entry.get("source") != source:
                    continue
                if source_prefix and not entry.get("source", "").startswith(source_prefix):
                    continue
                ts = entry.get("timestamp", "")
                date = ts[:10] if ts else ""
                if since and date < since:
                    continue

                similarity = _cosine_similarity(query_vec, entry["embedding"])
                similarity *= recency_multiplier(entry.get("timestamp", ""))
                results.append((similarity, entry))

    results.sort(key=lambda x: -x[0])
    return results[:limit]


# ============================================================
# TF-IDF 搜索引擎（回退模式）
# ============================================================

def tokenize(text: str) -> list:
    """简易中英文分词。"""
    text = text.lower()
    tokens = re.findall(r'[a-z0-9]+', text)
    chinese = re.findall(r'[一-鿿]+', text)
    for chunk in chinese:
        if len(chunk) >= 2:
            for i in range(len(chunk) - 1):
                tokens.append(chunk[i:i+2])
        tokens.append(chunk)
    return tokens


class TFIDFEngine:
    def __init__(self):
        self.docs = []
        self.doc_freqs = Counter()
        self.total_docs = 0
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        if not INDEX_FILE.exists():
            return

        doc_freq_buffer = Counter()
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    record = json.loads(line.strip())
                    content = record.get("content", "")
                    tokens = set(tokenize(content))
                    record["_tokens"] = tokens
                    self.docs.append(record)
                    for t in tokens:
                        doc_freq_buffer[t] += 1
                except json.JSONDecodeError:
                    continue

        self.doc_freqs = doc_freq_buffer
        self.total_docs = len(self.docs)
        self._loaded = True

    def search(self, query: str, limit: int = 20, source: Optional[str] = None,
               source_prefix: Optional[str] = None, since: Optional[str] = None,
               until: Optional[str] = None) -> list:
        self.load()
        if self.total_docs == 0:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scored = []
        for doc in self.docs:
            if source and doc.get("source") != source:
                continue
            if source_prefix and not doc.get("source", "").startswith(source_prefix):
                continue
            ts = doc.get("timestamp", "")
            date = ts[:10] if ts else ""
            if since and date < since:
                continue
            if until and date > until:
                continue

            doc_tokens = doc.get("_tokens", set())
            if not doc_tokens:
                continue

            score = 0.0
            for qt in query_tokens:
                if qt in doc_tokens:
                    tf = 1.0
                    df = self.doc_freqs.get(qt, 0)
                    if df > 0:
                        idf = math.log(self.total_docs / (df + 1)) + 1
                    else:
                        idf = 1.0
                    score += tf * idf

            if score > 0:
                score *= recency_multiplier(doc.get("timestamp", ""))
                scored.append((score, doc))

        scored.sort(key=lambda x: -x[0])
        return [(s, {k: v for k, v in d.items() if k != "_tokens"}) for s, d in scored[:limit]]


# ============================================================
# 统一搜索入口
# ============================================================

def unified_search(query: str, limit: int = 20, source: Optional[str] = None,
                   source_prefix: Optional[str] = None, since: Optional[str] = None,
                   until: Optional[str] = None) -> tuple:
    """自动选择最佳搜索引擎。

    Returns: (mode, results)
        mode: "embedding" 或 "tfidf"
        results: list of (score, record)
    """
    # 优先尝试 embedding
    client = _get_embedding_client()
    if client and EMBEDDINGS_DIR.exists() and list(EMBEDDINGS_DIR.glob("*.jsonl")):
        results = embedding_search(query, limit=limit, source=source,
                                   source_prefix=source_prefix, since=since)
        if results:
            return ("embedding", results)

    # 回退到 TF-IDF
    engine = TFIDFEngine()
    results = engine.search(query, limit=limit, source=source,
                            source_prefix=source_prefix, since=since, until=until)
    return ("tfidf", results)


def format_results(results: list, query: str, mode: str) -> str:
    mode_label = "Embedding向量" if mode == "embedding" else "TF-IDF关键词"
    if not results:
        return f"未找到与 {query} 相关的记录。（模式：{mode_label}）"

    lines = [f"搜索 {query}，找到 {len(results)} 条结果（模式：{mode_label}）", ""]

    for i, (score, record) in enumerate(results, 1):
        ts = record.get("timestamp", "")[:19].replace("T", " ")
        source = record.get("source", "?")
        role = record.get("role", "?")
        content = record.get("content", "")

        query_lower = query.lower()
        pos = content.lower().find(query_lower)
        if pos >= 0:
            start = max(0, pos - 40)
            end = min(len(content), pos + len(query) + 60)
            preview = f"...{content[start:end].replace(chr(10), ' ')}..."
        else:
            preview = content[:250].replace(chr(10), " ")
            if len(content) > 250:
                preview += "..."
        preview = redact(preview)

        role_icon = {"user": "U", "assistant": "A", "system": "S"}.get(role, "?")
        lines.append(f"[{i}] {ts} [{source}] [{role_icon}] score={score:.2f}")
        lines.append(f"    {preview}")
        lines.append("")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  search.py <关键词>                    # 搜索")
        print("  search.py <关键词> --source codex     # 数据源过滤")
        print("  search.py <关键词> --since 2026-05-01 # 时间范围")
        print("  search.py --build-embeddings          # 构建 embedding 索引")
        print("  search.py --stats                     # 搜索引擎统计")
        return

    if sys.argv[1] == "--build-embeddings":
        since = None
        if len(sys.argv) > 2 and sys.argv[2] == "--since":
            since = sys.argv[3] if len(sys.argv) > 3 else None
        count = build_embeddings(since=since)
        print(f"生成 {count} 条 embedding")
        return

    if sys.argv[1] == "--stats":
        engine = TFIDFEngine()
        engine.load()
        print(f"TF-IDF 索引文档数: {engine.total_docs}")
        print(f"词表大小: {len(engine.doc_freqs)}")

        # embedding stats
        if EMBEDDINGS_DIR.exists():
            emb_count = 0
            for f in EMBEDDINGS_DIR.glob("*.jsonl"):
                emb_count += sum(1 for _ in open(f, "r"))
            print(f"Embedding 向量数: {emb_count}")
        else:
            print("Embedding 向量数: 0（未构建）")
        return

    query = sys.argv[1]
    source = None
    source_prefix = None
    since = None
    until = None

    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--source" and i + 1 < len(sys.argv):
            val = sys.argv[i + 1]
            if val == "claude":
                source_prefix = "claude-code"
            elif val == "codex":
                source_prefix = "codex"
            elif val == "hermes":
                source_prefix = "hermes"
            else:
                source = val
            i += 2
        elif sys.argv[i] == "--since" and i + 1 < len(sys.argv):
            since = sys.argv[i + 1]
            i += 2
        elif sys.argv[i] == "--until" and i + 1 < len(sys.argv):
            until = sys.argv[i + 1]
            i += 2
        else:
            i += 1

    mode, results = unified_search(query, limit=20, source=source,
                                   source_prefix=source_prefix, since=since, until=until)
    print(format_results(results, query, mode))


if __name__ == "__main__":
    main()
