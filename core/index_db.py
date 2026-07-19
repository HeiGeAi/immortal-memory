#!/usr/bin/env python3
"""永生记忆库 — 持久化检索索引 (SQLite + FTS5 trigram) v0.1

把 recall 从"每次全量读 1GB + 重新分词(约 54s)"降到亚秒级。

- docs 表存正文与元数据；docs_fts(fts5 trigram) 给 >=3 字查询做 bm25 召回，
  <3 字中文查询(trigram 无法 MATCH)用 LIKE 兜底。
- 用前缀指纹验证 append-only，发现中段重写、缩小或 ID 漂移时安全重建。
- channels() 返回多通道排序结果，交给 search.py 做 RRF 融合。
- SQLite 是可重建读模型，index.jsonl 是事实源。

CLI:
  python3 index_db.py reindex   # 全量重建
  python3 index_db.py sync      # 增量同步
  python3 index_db.py stats     # 索引状态
  python3 index_db.py search <关键词>
"""

import sys
import math
import sqlite3
from pathlib import Path
from typing import Optional

from index_locks import database_lock, index_lock_pair

IMMORTAL_DIR = Path.home() / ".immortal"
INDEX_FILE = IMMORTAL_DIR / "index.jsonl"
DB_FILE = IMMORTAL_DIR / "search_index.db"

# 时间衰减收敛到 ranking_common（单一真源），与 search.py 共用，避免两通道打分口径漂移。
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from ranking_common import RECENCY_TAU_DAYS, RECENCY_BOOST, local_date, recency_multiplier  # noqa: E402,F401


def _connect() -> sqlite3.Connection:
    # Query paths are strictly read-only. In particular, they must not create
    # WAL sidecars that could race an atomic staging database replacement.
    uri = DB_FILE.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.execute("PRAGMA query_only=ON")
    con.create_function("immortal_local_date", 1, local_date, deterministic=True)
    return con


def _ensure_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS docs("
        "rowid INTEGER PRIMARY KEY, rec_id TEXT, ts TEXT, source TEXT, "
        "role TEXT, project TEXT, content TEXT)"
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_docs_rec_id ON docs(rec_id)")
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


def sync(verbose: bool = False, force_rebuild: bool = False) -> int:
    """Deep-reconcile the read model and return the inserted record count.

    This belongs to the scheduled collection pipeline, not the recall path.
    """
    if not INDEX_FILE.exists():
        return 0
    from index_integrity import reconcile_index

    result = reconcile_index(INDEX_FILE, DB_FILE, force_rebuild=force_rebuild)
    if verbose:
        print(
            f"索引同步模式: {result['mode']}，原因: {result['reason']}，"
            f"写入: {result['added']}"
        )
    return int(result["added"])


def is_ready() -> bool:
    if not INDEX_FILE.exists() or not DB_FILE.exists():
        return False
    try:
        with index_lock_pair(
            INDEX_FILE,
            DB_FILE,
            source_exclusive=False,
            database_exclusive=False,
        ):
            return _is_ready_unlocked()
    except OSError:
        return False


def _is_ready_unlocked() -> bool:
    """Return trusted index readiness using only source stat and fixed metadata."""
    if not INDEX_FILE.exists() or not DB_FILE.exists():
        return False
    con = None
    try:
        source_before = INDEX_FILE.stat()
        con = _connect()
        rows = dict(
            con.execute(
                "SELECT key,value FROM meta WHERE key IN "
                "('parity_status','last_size','source_dev','source_ino',"
                "'source_mtime_ns','source_ctime_ns','indexed_id_count',"
                "'indexed_ids_sha256')"
            ).fetchall()
        )
        source_after = INDEX_FILE.stat()
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return False
    finally:
        if con is not None:
            con.close()
    required = {
        "parity_status",
        "last_size",
        "source_dev",
        "source_ino",
        "source_mtime_ns",
        "source_ctime_ns",
        "indexed_id_count",
        "indexed_ids_sha256",
    }
    if set(rows) != required or rows["parity_status"] != "trusted":
        return False
    before_signature = (
        source_before.st_dev,
        source_before.st_ino,
        source_before.st_size,
        source_before.st_mtime_ns,
        source_before.st_ctime_ns,
    )
    after_signature = (
        source_after.st_dev,
        source_after.st_ino,
        source_after.st_size,
        source_after.st_mtime_ns,
        source_after.st_ctime_ns,
    )
    if before_signature != after_signature:
        return False
    try:
        return (
            int(rows["last_size"]) == source_after.st_size
            and int(rows["source_dev"]) == source_after.st_dev
            and int(rows["source_ino"]) == source_after.st_ino
            and int(rows["source_mtime_ns"]) == source_after.st_mtime_ns
            and int(rows["source_ctime_ns"]) == source_after.st_ctime_ns
            and int(rows["indexed_id_count"]) >= 0
            and bool(rows["indexed_ids_sha256"])
        )
    except (TypeError, ValueError):
        return False


def ready_channels(
    query: str,
    limit: int = 20,
    source: Optional[str] = None,
    source_prefix: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    pool: Optional[int] = None,
):
    """Check readiness and query one immutable source/DB generation snapshot."""
    if not INDEX_FILE.exists() or not DB_FILE.exists():
        return (False, [], [])
    try:
        with index_lock_pair(
            INDEX_FILE,
            DB_FILE,
            source_exclusive=False,
            database_exclusive=False,
        ):
            if not _is_ready_unlocked():
                return (False, [], [])
            labels, rankings = _channels_unlocked(
                query,
                limit=limit,
                source=source,
                source_prefix=source_prefix,
                since=since,
                until=until,
                pool=pool,
            )
            return (True, labels, rankings)
    except OSError:
        return (False, [], [])


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
    if not DB_FILE.exists():
        return ([], [])
    with database_lock(DB_FILE, exclusive=False):
        return _channels_unlocked(
            query,
            limit=limit,
            source=source,
            source_prefix=source_prefix,
            since=since,
            until=until,
            pool=pool,
        )


def _channels_unlocked(query: str, limit: int = 20, source: Optional[str] = None,
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
    with database_lock(DB_FILE, exclusive=False):
        _stats_unlocked()


def _stats_unlocked() -> None:
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
        print("开始全量构建索引（首次约 1-3 分钟）...")
        n = sync(verbose=True, force_rebuild=True)
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
