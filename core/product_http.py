#!/usr/bin/env python3
"""Strict, JSON-only GET routing for the local Immortal product API.

The self-version diff contract has one authority: the path version is the
``to_version_id`` and the required, single ``from`` query value is the
``from_version_id``.  A ``to`` query value is deliberately not accepted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple
from urllib.parse import parse_qs, unquote_to_bytes

from product_data import ProductDataError


MAX_TARGET_CHARS = 8192
MAX_PATH_CHARS = 2048
MAX_QUERY_FIELDS = 32
IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9._:@+-]{1,180}\Z")
BAD_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
SAFE_BAD_REQUEST_CODES = {
    "invalid_context_id",
    "invalid_cursor",
    "invalid_judgment_id",
    "invalid_memory_id",
    "invalid_query",
    "invalid_self_item_id",
    "invalid_version_id",
    "query_too_short",
}
SAFE_NOT_FOUND_CODES = {
    "context_not_found",
    "judgment_not_found",
    "memory_not_found",
    "not_found",
    "self_item_not_found",
    "self_version_not_found",
}
SAFE_UNAVAILABLE_CODES = {
    "clock_unavailable",
    "context_unavailable",
    "cursor_key_unavailable",
    "index_unavailable",
    "judgment_unavailable",
    "model_unavailable",
    "outcome_unavailable",
    "self_model_unavailable",
    "system_unavailable",
    "trust_unavailable",
}


@dataclass(frozen=True)
class ProductHttpError(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False


def error_body(code: str, message: str, *, retryable: bool = False) -> Dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
    }


def is_v2_get_target(target: str) -> bool:
    path = target.partition("?")[0]
    return path == "/api/v2" or path.startswith("/api/v2/")


def _strict_unquote(value: str, *, code: str, message: str) -> str:
    if BAD_PERCENT_RE.search(value):
        raise ProductHttpError(400, code, message)
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError):
        raise ProductHttpError(400, code, message)
    if any(character in decoded for character in ("/", "\\", "\x00", "%", "?", "#")):
        raise ProductHttpError(400, code, message)
    return decoded


def _parse_target(target: str) -> Tuple[List[str], Dict[str, List[str]]]:
    if not isinstance(target, str) or len(target) > MAX_TARGET_CHARS:
        raise ProductHttpError(400, "invalid_path", "请求路径无效")
    raw_path, separator, raw_query = target.partition("?")
    if not raw_path.startswith("/") or len(raw_path) > MAX_PATH_CHARS or "#" in target:
        raise ProductHttpError(400, "invalid_path", "请求路径无效")
    raw_segments = raw_path.split("/")
    if not raw_segments or raw_segments[0] != "":
        raise ProductHttpError(400, "invalid_path", "请求路径无效")
    segments = [
        _strict_unquote(value, code="invalid_path", message="请求路径无效")
        for value in raw_segments[1:]
    ]
    if any(value == "" for value in segments):
        raise ProductHttpError(400, "invalid_path", "请求路径无效")
    if not separator:
        return segments, {}
    if len(raw_query) > MAX_TARGET_CHARS - len(raw_path):
        raise ProductHttpError(400, "invalid_query", "查询参数无效")
    if BAD_PERCENT_RE.search(raw_query):
        raise ProductHttpError(400, "invalid_query", "查询参数无效")
    try:
        query = parse_qs(
            raw_query,
            keep_blank_values=True,
            strict_parsing=False,
            encoding="utf-8",
            errors="strict",
            max_num_fields=MAX_QUERY_FIELDS,
        )
    except (UnicodeDecodeError, ValueError):
        raise ProductHttpError(400, "invalid_query", "查询参数无效")
    if any(not isinstance(key, str) or not key for key in query):
        raise ProductHttpError(400, "invalid_query", "查询参数无效")
    return segments, query


def _identifier(value: str) -> str:
    if IDENTIFIER_RE.fullmatch(value) is None:
        raise ProductHttpError(400, "invalid_path", "请求路径无效")
    return value


def _without_query(query: Mapping[str, Sequence[str]]) -> None:
    if query:
        raise ProductHttpError(400, "invalid_query", "该接口不接受查询参数")


def _domain_error(exc: ProductDataError) -> ProductHttpError:
    code = str(exc.code or "service_unavailable")
    if code in SAFE_BAD_REQUEST_CODES:
        return ProductHttpError(400, code, str(exc), False)
    if code in SAFE_NOT_FOUND_CODES:
        return ProductHttpError(404, code, str(exc), False)
    if code in SAFE_UNAVAILABLE_CODES:
        return ProductHttpError(503, code, str(exc), True)
    return ProductHttpError(
        503, "service_unavailable", "产品数据暂时不可用", True
    )


def _exception_response(exc: Exception) -> Tuple[int, Dict[str, Any]]:
    if isinstance(exc, ProductDataError):
        mapped = _domain_error(exc)
        return mapped.status, error_body(
            mapped.code, mapped.message, retryable=mapped.retryable
        )
    if isinstance(exc, ProductHttpError):
        return exc.status, error_body(
            exc.code, exc.message, retryable=exc.retryable
        )
    return 500, error_body(
        "internal_error",
        "服务暂时无法完成请求",
        retryable=True,
    )


def route_product_get(
    target: str, data_factory: Callable[[], Any]
) -> Tuple[int, Dict[str, Any]]:
    """Construct the read model and route it inside one stable error boundary."""
    try:
        data = data_factory()
    except Exception as exc:
        return _exception_response(exc)
    return ProductRouter(data).get(target)


class ProductRouter:
    """Dispatch bounded v2 reads without exposing filesystem or exception data."""

    def __init__(self, data: Any) -> None:
        self.data = data

    def get(self, target: str) -> Tuple[int, Dict[str, Any]]:
        try:
            segments, query = _parse_target(target)
            return 200, self._dispatch(segments, query)
        except Exception as exc:
            return _exception_response(exc)

    def _dispatch(
        self, segments: Sequence[str], query: Mapping[str, Sequence[str]]
    ) -> Dict[str, Any]:
        if list(segments) == ["api", "v2", "home"]:
            _without_query(query)
            return self.data.home()
        if list(segments) == ["api", "v2", "memories"]:
            return self.data.memories(query)
        if len(segments) == 4 and list(segments[:3]) == ["api", "v2", "memories"]:
            _without_query(query)
            return self.data.memory_detail(_identifier(segments[3]))
        if list(segments) == ["api", "v2", "self"]:
            _without_query(query)
            return self.data.self_model()
        if (
            len(segments) == 5
            and list(segments[:4]) == ["api", "v2", "self", "items"]
        ):
            _without_query(query)
            return self.data.self_item(_identifier(segments[4]))
        if list(segments) == ["api", "v2", "self", "versions"]:
            return self.data.self_versions(query)
        if (
            len(segments) == 6
            and list(segments[:4]) == ["api", "v2", "self", "versions"]
            and segments[5] == "diff"
        ):
            if set(query) != {"from"} or len(query["from"]) != 1 or not query["from"][0]:
                raise ProductHttpError(
                    400, "invalid_query", "版本差异必须提供一个 from 参数"
                )
            return self.data.self_diff(
                _identifier(str(query["from"][0])),
                _identifier(segments[4]),
            )
        if list(segments) == ["api", "v2", "judgments"]:
            return self.data.judgments(query)
        if len(segments) == 4 and list(segments[:3]) == ["api", "v2", "judgments"]:
            _without_query(query)
            return self.data.judgment_detail(_identifier(segments[3]))
        if list(segments) == ["api", "v2", "contexts"]:
            return self.data.contexts(query)
        if len(segments) == 4 and list(segments[:3]) == ["api", "v2", "contexts"]:
            _without_query(query)
            return self.data.context_detail(_identifier(segments[3]))
        if list(segments) == ["api", "v2", "trust"]:
            _without_query(query)
            return self.data.trust()
        if list(segments) == ["api", "v2", "system"]:
            _without_query(query)
            return self.data.system()
        raise ProductHttpError(404, "not_found", "未找到接口")
