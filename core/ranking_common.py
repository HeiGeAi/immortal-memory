#!/usr/bin/env python3
"""检索排序的时间衰减 —— search.py 与 index_db.py 共用的单一真源。

历史上 recency_multiplier 和两个常量在 search.py / index_db.py 各存一份逐字拷贝，
靠人工同步（index_db.py 注释自承"与 search.py 保持一致"）。两者都进 RRF 融合，
改一处漏一处会让两通道打分口径静默漂移。收敛到此模块，两边 import。
"""

import math
from datetime import datetime, timezone

# 时间衰减：相关性仍是主轴，但越新的记录得分越高，避免旧内容长期压住新进展。
# 今天≈2.0x，120天≈1.37x，一年≈1.05x。
RECENCY_TAU_DAYS = 120.0
RECENCY_BOOST = 1.0


def local_date(ts: str) -> str:
    """Return the record's calendar date using the machine's local timezone."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return ""
    if dt.tzinfo is None:
        return dt.strftime("%Y-%m-%d")
    return dt.astimezone().strftime("%Y-%m-%d")


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
