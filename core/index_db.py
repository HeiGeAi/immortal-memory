#!/usr/bin/env python3
"""永生记忆库 — 持久化检索索引 (SQLite + FTS5 trigram) v0.1

把 recall 从"每次全量读 1GB + 重新分词(约 54s)"降到亚秒级。

- docs 表存正文与元数据；docs_fts(fts5 trigram) 给 >=3 字查询做 bm25 召回，
  <3 字中文查询(trigram 无法 MATCH)用 LIKE 兜底。
- 按字节 offset 增量同步，append-only 友好；源文件缩小则自动全量重建。
- channels() 返回多通道排序结果，交给 search.py 做 RRF 融合。
- 设计为"可失败"：任何异常由调用方(search.py)回退到内存引擎，绝不影响 recall 可用性。

CLI:
  python3 index_db.py reindex   # 全量重建
  python3 index_db.py sync      # 增量同步
  python3 index_db.py stats     # 索引状态
  python3 index_db.py search <关键词>
"""

import os
import sys
import json
import math
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

IMMORTAL_DIR = Path.home() / ".immortal"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
DB_FILE = IMMORTAL_DIR / "search_index.db"

# 时间衰减收敛到 ranking_common（单一真源），与 search.py 共用，避免两通道打分口径漂移。
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from ranking_common import RECENCY_TAU_DAYS, RECENCY_BOOST, local_date, recency_multiplier  # noqa: E402,F401


def _connect() -> sqlite3.Connection:
    # SQLite does not always honor busy_timeout while two first-use
    # connections both try to switch journal_mode. Retry that narrow startup
    # race; normal writer serialization is handled by BEGIN IMMEDIATE.
    for attempt in range(5):
        con = sqlite3.connect(str(DB_FILE), timeout=10)
        con.execute("PRAGMA busy_timeout=10000")
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            con.create_function("immortal_local_date", 1, local_date, deterministic=True)
            return con
        except sqlite3.OperationalError as exc:
            con.close()
            if "locked" not in str(exc).lower() or attempt == 4:
                raise
            time.sleep(0.05 * (attempt + 1))
    raise RuntimeError("unreachable")


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS docs("
        "rowid INTEGER PRIMARY KEY, rec_id TEXT, ts TEXT, source TEXT, "
        "role TEXT, project TEXT, content TEXT)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_source ON docs(source)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_ts ON docs(ts)")
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5("
        "content, tokenize='trigram')"
    )
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")


def _meta_get(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _meta_set(con, key, value):
    con.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def _reset(con) -> None:
    con.execute("DROP TABLE IF EXISTS docs")
    con.execute("DROP TABLE IF EXISTS docs_fts")
    con.execute("DELETE FROM meta")
    _ensure_schema(con)


def sync(verbose: bool = False) -> int:
    """增量同步：只处理 index.jsonl 中上次 offset 之后的新行。

    源文件比上次记录的体积还小（被 dedup/rotate 重写过）则全量重建。
    返回本次新增条数。
    """
    if not INDEX_FILE.exists():
        return 0

    con = _connect()
    added = 0
    try:
        # Serialize metadata reads with writes. Reading the old offset before
        # acquiring the writer lock lets two recall processes index the same
        # byte range or makes one fail with "database is locked".
        con.execute("BEGIN IMMEDIATE")
        _ensure_schema(con)
        last_offset = int(_meta_get(con, "last_offset", 0) or 0)
        last_size = int(_meta_get(con, "last_size", 0) or 0)
        fsize = INDEX_FILE.stat().st_size

        if fsize < last_size:
            if verbose:
                print("源文件变小，触发全量重建索引")
            _reset(con)
            last_offset = 0

        if fsize == last_size and last_offset >= fsize:
            con.commit()
            return 0

        with open(INDEX_FILE, "rb") as f:
            f.seek(last_offset)
            while True:
                line_start = f.tell()
                raw = f.readline()
                if not raw:
                    break
                # A collector may still be appending the final JSONL record.
                # Keep the offset before that fragment so the next sync can
                # parse the completed line instead of skipping it forever.
                if not raw.endswith(b"\n"):
                    f.seek(line_start)
                    break
                try:
                    r = json.loads(raw.decode("utf-8", "ignore"))
                except Exception:
                    continue
                content = r.get("content", "") or ""
                cur = con.execute(
                    "INSERT INTO docs(rec_id,ts,source,role,project,content) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        r.get("id", ""),
                        r.get("timestamp", ""),
                        r.get("source", ""),
                        r.get("role", ""),
                        r.get("project", ""),
                        content,
                    ),
                )
                con.execute(
                    "INSERT INTO docs_fts(rowid,content) VALUES(?,?)",
                    (cur.lastrowid, content),
                )
                added += 1
                if verbose and added % 50000 == 0:
                    print(f"  已索引 {added} 条...")
            new_offset = f.tell()
        _meta_set(con, "last_offset", new_offset)
        _meta_set(con, "last_size", new_offset)
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return added


def _escape_match(q: str) -> str:
    # 包成 FTS5 短语，trigram 下等价于子串匹配；转义内部双引号
    return '"' + q.replace('"', '""') + '"'


def _build_filter(source, source_prefix, since, until):
    conds = []
    params = []
    if source:
        conds.append("d.source = ?")
        params.append(source)
    if source_prefix:
        conds.append("d.source LIKE ?")
        params.append(source_prefix + "%")
    if since:
        conds.append("immortal_local_date(d.ts) >= ?")
        params.append(since)
    if until:
        conds.append("immortal_local_date(d.ts) <= ?")
        params.append(until)
    clause = (" AND " + " AND ".join(conds)) if conds else ""
    return clause, params


def _row_to_rec(row) -> dict:
    return {
        "id": row[0],
        "timestamp": row[1],
        "source": row[2],
        "role": row[3],
        "project": row[4],
        "content": row[5],
    }


def channels(query: str, limit: int = 20, source: Optional[str] = None,
             source_prefix: Optional[str] = None, since: Optional[str] = None,
             until: Optional[str] = None, pool: Optional[int] = None):
    """返回 (labels, rankings) 供 RRF 融合。

    labels: ["bm25"/"like", "phrase"] 中非空的那些
    rankings: 与 labels 对应的 [(score, record), ...]
    不可用或全空时返回 ([], [])。
    """
    if not DB_FILE.exists():
        return ([], [])
    try:
        con = _connect()
        total = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    except Exception:
        return ([], [])
    if total == 0:
        con.close()
        return ([], [])

    if pool is None:
        pool = max(limit * 8, 80)
    ql = query.strip()
    if not ql:
        con.close()
        return ([], [])
    clause, fparams = _build_filter(source, source_prefix, since, until)

    rows = []
    kw_label = None
    try:
        if len(ql) >= 3:
            # FTS5 trigram + bm25（>=3 字才能 MATCH）
            sql = (
                "SELECT d.rec_id,d.ts,d.source,d.role,d.project,d.content, bm25(docs_fts) AS b "
                "FROM docs_fts JOIN docs d ON d.rowid = docs_fts.rowid "
                "WHERE docs_fts MATCH ?" + clause + " ORDER BY b LIMIT ?"
            )
            rows = con.execute(sql, [_escape_match(ql)] + fparams + [pool]).fetchall()
            kw_label = "bm25"
        if not rows:
            # 兜底：<3 字查询，或 FTS 未命中 -> LIKE 子串扫描
            # 高频词命中可能上千条（实测：报价~1053、妙记~971、客户~6745）。
            # 池子开到 5000 让常见 2 字词基本全覆盖，不漏相关老记录；
            # 超高频词保留最近 5000 条（老记录本就被时间衰减压低）。
            # 按时间倒序取，再交给 recency/phrase 通道精排。
            like_pool = max(limit * 15, 5000)
            sql = (
                "SELECT d.rec_id,d.ts,d.source,d.role,d.project,d.content "
                "FROM docs d WHERE d.content LIKE ?" + clause +
                " ORDER BY d.ts DESC LIMIT ?"
            )
            rows = con.execute(sql, ["%" + ql + "%"] + fparams + [like_pool]).fetchall()
            # LIKE 行没有 bm25 列，补一个占位让下游统一处理
            rows = [tuple(r) + (None,) for r in rows]
            kw_label = "like"
    except Exception:
        con.close()
        return ([], [])
    finally:
        try:
            con.close()
        except Exception:
            pass

    if not rows:
        return ([], [])

    # 关键词通道
    kw_ranked = []
    for row in rows:
        rec = _row_to_rec(row)
        b = row[6]
        rec_mult = recency_multiplier(rec.get("timestamp", ""))
        if b is None:
            # LIKE：无相关性分，按 recency 排
            score = rec_mult
        else:
            # bm25 越小越好 -> 取正后乘 recency
            score = (-(b)) * rec_mult
        kw_ranked.append((score, rec))
    kw_ranked.sort(key=lambda x: -x[0])

    # 短语通道：在已召回的候选里数整串出现次数（候选少，纯内存）
    qlow = ql.lower()
    phrase_ranked = []
    for _s, rec in kw_ranked:
        cnt = rec.get("content", "").lower().count(qlow)
        if cnt > 0:
            phrase_ranked.append((cnt * recency_multiplier(rec.get("timestamp", "")), rec))
    phrase_ranked.sort(key=lambda x: -x[0])

    labels = []
    rankings = []
    if kw_ranked:
        labels.append(kw_label)
        rankings.append(kw_ranked)
    if phrase_ranked:
        labels.append("phrase")
        rankings.append(phrase_ranked)
    return (labels, rankings)


def stats() -> None:
    if not DB_FILE.exists():
        print("索引未构建。运行: python3 index_db.py reindex")
        return
    con = _connect()
    total = con.execute("SELECT count(*) FROM docs").fetchone()[0]
    last_offset = _meta_get(con, "last_offset", 0)
    src_size = INDEX_FILE.stat().st_size if INDEX_FILE.exists() else 0
    db_size = DB_FILE.stat().st_size
    con.close()
    print(f"索引文档数: {total}")
    print(f"已处理 offset: {last_offset} / 源文件 {src_size} 字节"
          f"（{'已最新' if int(last_offset or 0) >= src_size else '待增量同步'}）")
    print(f"索引库大小: {db_size/1e9:.2f} GB")


def main():
    if len(sys.argv) < 2:
        print("用法: index_db.py [reindex|sync|stats|search <kw>]")
        return
    cmd = sys.argv[1]
    if cmd == "reindex":
        if DB_FILE.exists():
            DB_FILE.unlink()
        for ext in ("-wal", "-shm"):
            p = Path(str(DB_FILE) + ext)
            if p.exists():
                p.unlink()
        print("开始全量构建索引（首次约 1-3 分钟）...")
        n = sync(verbose=True)
        print(f"完成，索引 {n} 条")
        stats()
    elif cmd == "sync":
        n = sync(verbose=True)
        print(f"增量同步新增 {n} 条")
    elif cmd == "stats":
        stats()
    elif cmd == "search" and len(sys.argv) > 2:
        labels, rankings = channels(sys.argv[2], limit=10)
        print("通道:", labels)
        for lab, rk in zip(labels, rankings):
            print(f"--- {lab} top3 ---")
            for s, rec in rk[:3]:
                print(f"  {rec['timestamp'][:19]} [{rec['source']}] score={s:.3f} {rec['content'][:80]}")
    else:
        print("未知命令")


if __name__ == "__main__":
    main()
