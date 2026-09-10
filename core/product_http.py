#!/usr/bin/env python3
"""Strict, JSON-only GET routing for the local Immortal product API.

The self-version diff contract has one authority: the path version is the
``to_version_id`` and the required, single ``from`` query value is the
``from_version_id``.  A ``to`` query value is deliberately not accepted.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Sequence, Tuple
from urllib.parse import parse_qs, unquote_to_bytes, urlsplit

from product_data import ProductDataError
from product_mutations import MutationError, sanitize_public_mutation_result


MAX_TARGET_CHARS = 8192
MAX_PATH_CHARS = 2048
MAX_QUERY_FIELDS = 32
MAX_REQUEST_BYTES = 256 * 1024
IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9._:@+-]{1,180}\Z")
HEADER_ID_RE = re.compile(r"\A[A-Za-z0-9._:@+-]{1,128}\Z")
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
SAFE_MUTATION_RESPONSE_CODES = {
    "context_budget_exceeded", "context_not_found", "evidence_not_found",
    "idempotency_conflict", "invalid_context_budget", "invalid_context_mode",
    "invalid_correction", "invalid_request", "invalid_transition",
    "judgment_not_found", "mutation_authority_unavailable",
    "private_content_blocked", "scope_mismatch", "self_item_not_found",
    "stale_preview", "statement_required", "task_required",
    "unresolved_context_mode", "version_conflict",
}


@dataclass(frozen=True)
class ProductHttpError(Exception):
    status: int
    code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class RequestMetadata:
    request_id: str
    idempotency_key: str


def error_body(
    code: str,
    message: str,
    *,
    detail: str = "",
    retryable: bool = False,
) -> Dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "detail": detail,
            "retryable": retryable,
        }
    }


def is_v2_get_target(target: str) -> bool:
    path = target.partition("?")[0]
    return path == "/api/v2" or path.startswith("/api/v2/")


def is_v2_post_target(target: str) -> bool:
    return is_v2_get_target(target)


def _header_values(headers: Any, name: str) -> List[str]:
    getter = getattr(headers, "get_all", None)
    if callable(getter):
        values = getter(name) or []
    else:
        value = headers.get(name) if hasattr(headers, "get") else None
        values = [] if value is None else [value]
    return [str(value) for value in values]


def _one_header(headers: Any, name: str, *, required: bool = True) -> str:
    values = _header_values(headers, name)
    if len(values) != 1 or not values[0].strip():
        if not values and not required:
            return ""
        raise ProductHttpError(400, "invalid_headers", "请求头无效")
    return values[0].strip()


def require_write_metadata(headers: Any, expected_port: int) -> RequestMetadata:
    origins = _header_values(headers, "Origin")
    request_ids = _header_values(headers, "X-Immortal-Request-Id")
    idempotency_keys = _header_values(headers, "Idempotency-Key")
    if not origins or not request_ids or not idempotency_keys:
        raise ProductHttpError(400, "request_metadata_required", "写操作需要本地 Origin、request ID 和幂等键")
    if len(origins) != 1 or len(request_ids) != 1 or len(idempotency_keys) != 1:
        raise ProductHttpError(400, "request_metadata_required", "写操作请求元数据必须是单值")
    origin = origins[0].strip()
    hosts = _header_values(headers, "Host")
    if len(hosts) != 1:
        raise ProductHttpError(403, "origin_not_allowed", "写操作只接受当前服务同源请求")
    try:
        parsed = urlsplit(origin)
        host_value = hosts[0].strip()
        host_parsed = urlsplit("http://" + host_value)
        origin_port = parsed.port
        host_port = host_parsed.port
    except ValueError as exc:
        raise ProductHttpError(403, "origin_not_allowed", "写操作只接受当前服务同源请求") from exc
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if (
        not origin.startswith("http://")
        or parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != ""
        or parsed.query
        or parsed.fragment
        or parsed.hostname not in allowed_hosts
        or host_parsed.hostname not in allowed_hosts
        or parsed.hostname != host_parsed.hostname
        or parsed.hostname is None
        or parsed.hostname.endswith(".")
        or host_parsed.hostname is None
        or host_parsed.hostname.endswith(".")
        or origin_port != expected_port
        or host_port != expected_port
        or not _canonical_authority(parsed, expected_port)
        or not _canonical_authority(host_parsed, expected_port)
    ):
        raise ProductHttpError(403, "origin_not_allowed", "写操作只接受当前服务同源请求")
    request_id = request_ids[0].strip()
    idempotency_key = idempotency_keys[0].strip()
    if HEADER_ID_RE.fullmatch(request_id) is None or HEADER_ID_RE.fullmatch(idempotency_key) is None:
        raise ProductHttpError(400, "request_metadata_required", "写操作需要有效 request ID 和幂等键")
    return RequestMetadata(request_id, idempotency_key)


def _canonical_authority(parsed: Any, port: int) -> bool:
    hostname = parsed.hostname
    if hostname is None:
        return False
    expected = (
        "[::1]:" + str(port)
        if hostname == "::1"
        else hostname.lower() + ":" + str(port)
    )
    return parsed.netloc.lower() == expected


def read_product_json(headers: Any, reader: Callable[[int], bytes]) -> Dict[str, Any]:
    if _header_values(headers, "Transfer-Encoding"):
        raise ProductHttpError(400, "invalid_body_framing", "不接受分块请求体")
    content_types = _header_values(headers, "Content-Type")
    if len(content_types) != 1:
        raise ProductHttpError(415, "unsupported_media_type", "写操作只接受 application/json")
    parts = [part.strip() for part in content_types[0].split(";")]
    if not parts or parts[0].lower() != "application/json":
        raise ProductHttpError(415, "unsupported_media_type", "写操作只接受 application/json")
    params = parts[1:]
    if len(params) > 1 or (
        params
        and (
            "=" not in params[0]
            or params[0].split("=", 1)[0].strip().lower() != "charset"
            or params[0].split("=", 1)[1].strip().strip('"').lower() not in {"utf-8", "utf8"}
        )
    ):
        raise ProductHttpError(415, "unsupported_media_type", "JSON charset 必须是 UTF-8")
    lengths = _header_values(headers, "Content-Length")
    if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0].strip()):
        raise ProductHttpError(400, "invalid_content_length", "Content-Length 无效")
    length = int(lengths[0])
    if length <= 0:
        raise ProductHttpError(400, "invalid_json", "请求体必须是 JSON 对象")
    if length > MAX_REQUEST_BYTES:
        raise ProductHttpError(413, "request_too_large", "请求体超过大小上限")
    raw = reader(length)
    if not isinstance(raw, bytes) or len(raw) != length:
        raise ProductHttpError(400, "invalid_body_framing", "请求体长度不完整")
    try:
        def unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
            result: Dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = item
            return result

        def reject_constant(_value: str) -> Any:
            raise ValueError("non-standard JSON constant")

        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ProductHttpError(400, "invalid_json", "请求体不是有效 JSON") from exc
    if not isinstance(value, dict):
        raise ProductHttpError(400, "invalid_json", "请求体必须是 JSON 对象")
    return value


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
            strict_parsing=True,
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
    if isinstance(exc, MutationError):
        if exc.code not in SAFE_MUTATION_RESPONSE_CODES:
            return 503, error_body(
                "mutation_failed",
                "写操作暂时无法完成",
                retryable=True,
            )
        status = 400
        if exc.code in {"version_conflict", "idempotency_conflict"}:
            status = 409
        elif exc.code in {"not_found", "self_item_not_found", "judgment_not_found", "context_not_found"}:
            status = 404
        elif exc.code in {"mutation_authority_unavailable", "mutation_failed"}:
            status = 503
        return status, error_body(
            exc.code,
            str(exc) if exc.code in {
                "version_conflict", "idempotency_conflict", "invalid_request",
                "invalid_transition", "scope_mismatch", "stale_preview",
            } else "写操作暂时无法完成",
            retryable=bool(exc.retryable or status == 503),
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


def route_product_post(
    target: str,
    body: Mapping[str, Any],
    metadata: RequestMetadata,
    coordinator_factory: Callable[[], Any],
) -> Tuple[int, Dict[str, Any]]:
    try:
        segments, query = _parse_target(target)
        _without_query(query)
        route = "/" + "/".join(segments)
        coordinator = coordinator_factory()
        result = coordinator.mutate(
            route,
            body,
            request_id=metadata.request_id,
            idempotency_key=metadata.idempotency_key,
        )
        public = sanitize_public_mutation_result(result)
        return (202 if public.get("derived_update_pending") is True else 200), public
    except Exception as exc:
        return _exception_response(exc)


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
