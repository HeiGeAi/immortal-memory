from __future__ import annotations

import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

import profile_review
from product_data import ProductData, ProductDataError
from profile_review import FactoryStore, ReviewHandler, ReviewStore, ThreadingHTTPServer


class FakeProductData:
    def __init__(self):
        self.calls = []
        self.failure = None

    def _result(self, name, *args):
        self.calls.append((name, args))
        if self.failure is not None:
            raise self.failure
        return {"route": name, "args": list(args)}

    def home(self):
        return self._result("home")

    def memories(self, query):
        return self._result("memories", query)

    def memory_detail(self, memory_id):
        return self._result("memory_detail", memory_id)

    def self_model(self):
        return self._result("self_model")

    def self_item(self, item_id):
        return self._result("self_item", item_id)

    def self_versions(self, query):
        return self._result("self_versions", query)

    def self_diff(self, from_version_id, to_version_id):
        return self._result("self_diff", from_version_id, to_version_id)

    def judgments(self, query):
        return self._result("judgments", query)

    def judgment_detail(self, card_id):
        return self._result("judgment_detail", card_id)

    def contexts(self, query):
        return self._result("contexts", query)

    def context_detail(self, context_id):
        return self._result("context_detail", context_id)

    def trust(self):
        return self._result("trust")

    def system(self):
        return self._result("system")


def start_v2_server(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ReviewHandler)
    server.store = ReviewStore(
        tmp_path / "proposal.md",
        tmp_path / "memories.jsonl",
        tmp_path / "reviewed.jsonl",
        tmp_path / "review_state.json",
    )
    server.factory = FactoryStore(
        history_path=tmp_path / "runtime" / "control_jobs.json",
        immortal_dir=tmp_path,
        skill_dir=tmp_path,
    )
    from control_center import ControlCenter

    server.control_center = ControlCenter(
        tmp_path,
        skill_dir=tmp_path,
        scheduler_probe=lambda: {"status": "unknown", "detail": "test"},
        service_reachable=True,
    )
    server.product_data = FakeProductData()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "http://127.0.0.1:%d" % server.server_port


def stop(server):
    server.shutdown()
    server.server_close()


def get_json(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.headers, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, json.loads(exc.read())


def raw_get(server, target, *, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    connection.request(
        "GET",
        target,
        headers={"Host": host or "127.0.0.1:%d" % server.server_port},
    )
    response = connection.getresponse()
    body = response.read()
    result = response.status, response.headers, json.loads(body)
    connection.close()
    return result


def test_all_thirteen_v2_get_routes_delegate_to_product_data(tmp_path):
    server, base = start_v2_server(tmp_path)
    routes = {
        "/api/v2/home": "home",
        "/api/v2/memories?limit=10": "memories",
        "/api/v2/memories/mem_1": "memory_detail",
        "/api/v2/self": "self_model",
        "/api/v2/self/items/self_1": "self_item",
        "/api/v2/self/versions?limit=5": "self_versions",
        "/api/v2/self/versions/ver_2/diff?from=ver_1": "self_diff",
        "/api/v2/judgments?status=active": "judgments",
        "/api/v2/judgments/card_1": "judgment_detail",
        "/api/v2/contexts?mode=task": "contexts",
        "/api/v2/contexts/ctx_1": "context_detail",
        "/api/v2/trust": "trust",
        "/api/v2/system": "system",
    }
    try:
        for path, expected in routes.items():
            status, headers, payload = get_json(base + path)
            assert status == 200, (path, payload)
            assert headers.get_content_type() == "application/json"
            assert payload["route"] == expected
    finally:
        stop(server)


def test_list_query_preserves_values_and_rejects_duplicates_in_domain(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        status, _, payload = get_json(base + "/api/v2/memories?q=abc&limit=2")
        assert status == 200
        assert payload["args"] == [{"q": ["abc"], "limit": ["2"]}]

        server.product_data.failure = ProductDataError("invalid_query", "查询参数不能重复")
        status, _, payload = get_json(base + "/api/v2/memories?q=abc&q=def")
    finally:
        stop(server)
    assert status == 400
    assert payload == {
        "error": {
            "code": "invalid_query",
            "message": "查询参数不能重复",
            "detail": "",
            "retryable": False,
        }
    }


def test_real_product_data_rejects_repeated_query_before_index_access(tmp_path):
    server, base = start_v2_server(tmp_path)
    server.product_data = ProductData(
        tmp_path,
        control_data=server.control_data
        if hasattr(server, "control_data")
        else None,
        control_center=server.control_center,
    )
    try:
        status, _, payload = get_json(base + "/api/v2/memories?q=abc&q=def")
    finally:
        stop(server)
    assert status == 400
    assert payload["error"]["code"] == "invalid_query"


def test_diff_contract_uses_path_as_to_and_requires_one_from(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        ok_status, _, ok = get_json(
            base + "/api/v2/self/versions/version%5F2/diff?from=version%5F1"
        )
        missing_status, _, missing = get_json(
            base + "/api/v2/self/versions/version_2/diff"
        )
        duplicate_status, _, duplicate = get_json(
            base + "/api/v2/self/versions/version_2/diff?from=a&from=b"
        )
        unknown_status, _, unknown = get_json(
            base + "/api/v2/self/versions/version_2/diff?from=a&to=b"
        )
        same_status, _, same = get_json(
            base + "/api/v2/self/versions/version_2/diff?from=version_2"
        )
    finally:
        stop(server)

    assert ok_status == 200
    assert ok["args"] == ["version_1", "version_2"]
    assert same_status == 200
    assert same["args"] == ["version_2", "version_2"]
    for status, payload in (
        (missing_status, missing),
        (duplicate_status, duplicate),
        (unknown_status, unknown),
    ):
        assert status == 400
        assert payload["error"]["code"] == "invalid_query"


@pytest.mark.parametrize(
    "target",
    (
        "/api/v2/memories/%2Fetc",
        "/api/v2/memories/%252Fetc",
        "/api/v2/memories/%FF",
        "/api/v2/memories/%ZZ",
        "/api/v2/memories/",
        "/api/v2/memories/" + "a" * 181,
        "/api/v2/self/items/a%2Fb",
        "/api/v2/contexts/a%00b",
    ),
)
def test_encoded_identifier_attacks_are_rejected_before_product_data(tmp_path, target):
    server, _ = start_v2_server(tmp_path)
    try:
        status, _, payload = raw_get(server, target)
    finally:
        calls = list(server.product_data.calls)
        stop(server)
    assert status == 400
    assert payload["error"]["code"] == "invalid_path"
    assert calls == []


def test_unknown_routes_are_stable_404_including_v2_root(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        for path in ("/api/v2/unknown", "/api/v2"):
            status, _, payload = get_json(base + path)
            assert status == 404
            assert payload == {
                "error": {
                    "code": "not_found",
                    "message": "未找到接口",
                    "detail": "",
                    "retryable": False,
                }
            }
    finally:
        stop(server)


@pytest.mark.parametrize(
    ("code", "status", "retryable"),
    (
        ("invalid_cursor", 400, False),
        ("query_too_short", 400, False),
        ("memory_not_found", 404, False),
        ("index_unavailable", 503, True),
        ("cursor_key_unavailable", 503, True),
        ("self_model_unavailable", 503, True),
    ),
)
def test_product_data_errors_map_to_stable_status(tmp_path, code, status, retryable):
    server, base = start_v2_server(tmp_path)
    server.product_data.failure = ProductDataError(code, "安全消息")
    try:
        actual, _, payload = get_json(base + "/api/v2/home")
    finally:
        stop(server)
    assert actual == status
    assert payload == {
        "error": {
            "code": code,
            "message": "安全消息",
            "detail": "",
            "retryable": retryable,
        }
    }


def test_unknown_exception_is_generic_500_without_leak(tmp_path):
    server, base = start_v2_server(tmp_path)
    server.product_data.failure = RuntimeError(
        "secret /Users/private-user/.immortal bearer sk-live-super-secret"
    )
    try:
        status, _, payload = get_json(base + "/api/v2/home")
    finally:
        stop(server)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert status == 500
    assert payload["error"] == {
        "code": "internal_error",
        "message": "服务暂时无法完成请求",
        "detail": "",
        "retryable": True,
    }
    assert "private-user" not in encoded
    assert "super-secret" not in encoded


def test_unknown_domain_error_code_and_message_are_not_exposed(tmp_path):
    server, base = start_v2_server(tmp_path)
    server.product_data.failure = ProductDataError(
        "future_internal_failure",
        "secret /Users/private-user/.immortal sk-live-domain-secret",
    )
    try:
        status, _, payload = get_json(base + "/api/v2/home")
    finally:
        stop(server)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert status == 503
    assert payload["error"] == {
        "code": "service_unavailable",
        "message": "产品数据暂时不可用",
        "detail": "",
        "retryable": True,
    }
    assert "private-user" not in encoded
    assert "domain-secret" not in encoded


def test_product_data_construction_failure_is_stable_500(tmp_path, monkeypatch):
    server, base = start_v2_server(tmp_path)
    del server.product_data

    def fail_construction(*args, **kwargs):
        raise RuntimeError(
            "/Users/private-user/.immortal sk-live-constructor-secret"
        )

    monkeypatch.setattr(profile_review, "ProductData", fail_construction)
    try:
        status, _, payload = get_json(base + "/api/v2/home")
    finally:
        stop(server)
    encoded = json.dumps(payload, ensure_ascii=False)
    assert status == 500
    assert payload["error"] == {
        "code": "internal_error",
        "message": "服务暂时无法完成请求",
        "detail": "",
        "retryable": True,
    }
    assert "private-user" not in encoded
    assert "constructor-secret" not in encoded


def test_v2_keeps_host_guard_security_headers_and_legacy_routes(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        status, headers, payload = get_json(base + "/api/v2/home")
        legacy_status, _, legacy = get_json(base + "/healthz")
        denied_status, denied_headers, denied = raw_get(
            server, "/api/v2/home", host="evil.example"
        )
    finally:
        stop(server)
    assert status == 200
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert legacy_status == 200
    assert legacy == {"status": "ok"}
    assert denied_status == 403
    assert denied["error"]["code"] == "host_not_allowed"
    assert denied_headers["X-Frame-Options"] == "DENY"


def test_query_encoding_errors_and_query_on_scalar_route_are_400(tmp_path):
    server, _ = start_v2_server(tmp_path)
    try:
        bad_percent = raw_get(server, "/api/v2/memories?q=%ZZ")
        bad_utf8 = raw_get(server, "/api/v2/memories?q=%FF")
        scalar_query = raw_get(server, "/api/v2/home?unused=1")
    finally:
        stop(server)
    for status, _, payload in (bad_percent, bad_utf8, scalar_query):
        assert status == 400
        assert payload["error"]["code"] in {"invalid_query", "invalid_path"}


@pytest.mark.parametrize(
    "target",
    (
        "/api/v2/self/versions/version_2/diff?from=x&",
        "/api/v2/self/versions/version_2/diff?from=x&&",
        "/api/v2/home?&&",
        "/api/v2/memories?bare",
        "/api/v2/memories?q=abc&&limit=2",
    ),
)
def test_malformed_query_fields_are_rejected_strictly(tmp_path, target):
    server, _ = start_v2_server(tmp_path)
    try:
        status, _, payload = raw_get(server, target)
    finally:
        stop(server)
    assert status == 400
    assert payload == {
        "error": {
            "code": "invalid_query",
            "message": "查询参数无效",
            "detail": "",
            "retryable": False,
        }
    }


def test_valid_percent_encoding_and_blank_known_value_reach_domain(tmp_path):
    server, base = start_v2_server(tmp_path)
    try:
        encoded_status, _, encoded = get_json(base + "/api/v2/memories?q=a%26b")
        blank_status, _, blank = get_json(base + "/api/v2/memories?q=")
    finally:
        stop(server)
    assert encoded_status == 200
    assert encoded["args"] == [{"q": ["a&b"]}]
    assert blank_status == 200
    assert blank["args"] == [{"q": [""]}]
