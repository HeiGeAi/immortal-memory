"""Product-facing bounded read models never fall back to raw vault scans."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from zoneinfo import ZoneInfo

from index_integrity import INDEX_SCHEMA_VERSION, ids_sha256
from product_data import ProductData, ProductDataError, ProductIndexIntegrity


def test_product_data_module_exposes_stable_error_contract(tmp_path):
    error = ProductDataError("index_unavailable", "索引不可用")
    assert error.code == "index_unavailable"
    assert str(error) == "索引不可用"


class FakeControlData:
    def __init__(self):
        self.calls = []

    def capabilities(self):
        self.calls.append("capabilities")
        return {"schema_version": 1, "modules": [{"id": "memories"}]}

    def sources(self):
        self.calls.append("sources")
        return {"items": [{"id": "local", "status": "success"}]}

    def backups(self):
        self.calls.append("backups")
        return {"items": [{"id": "backup-1", "verified": True}]}

    def diagnostics(self):
        self.calls.append("diagnostics")
        return {"version": "1.1.0", "ready": True}


class FakeControlCenter:
    def __init__(self):
        self.calls = 0

    def build_snapshot(self):
        self.calls += 1
        return {
            "status": "healthy",
            "status_label": "运行健康",
            "version": "1.1.0",
            "proofs": [{"id": "latest_run", "status": "healthy"}],
            "attention": [],
            "current_run": {"status": "success"},
            "metrics": {"new_records": 3},
        }


class FakeClaims:
    def list(self):
        return [
            {
                "claim_id": "clm_candidate",
                "statement": "Bear" + "er candidate-private-token",
                "status": "candidate",
                "confidence": 0.42,
                "privacy": "internal",
                "updated_at": "2026-07-22T08:00:00+00:00",
                "evidence_ids": [],
                "counter_evidence_ids": [],
                "based_on_event_seq": 4,
                "revision": 1,
            },
            {
                "claim_id": "clm_confirmed",
                "statement": "已确认的稳定事实",
                "status": "confirmed",
                "confidence": 0.91,
                "privacy": "internal",
                "updated_at": "2026-07-21T08:00:00+00:00",
                "evidence_ids": ["ev_1"],
                "counter_evidence_ids": ["ev_2"],
                "based_on_event_seq": 3,
                "revision": 7,
            },
        ]

    def get(self, claim_id):
        for row in self.list():
            if row["claim_id"] == claim_id:
                return row
        raise KeyError(claim_id)


class FakeLivingSelf:
    def __init__(self):
        item = {
            "item_id": "self-1",
            "title": "表达方式",
            "summary": "偏好直接表达 " + "Bear" + "er self-private-token",
            "confidence": 0.88,
            "scope": ["creator"],
            "status": "confirmed",
            "evidence_ids": ["ev-private"],
            "counter_evidence_ids": ["ev-counter"],
            "claim_ids": ["clm_confirmed"],
        }
        self._current = {
            "version_id": "lsv_" + "1" * 32,
            "parent_version_id": None,
            "status": "confirmed",
            "generation_reason": "claim_change",
            "content_hash": "sha256:" + "2" * 64,
            "based_on_claim_seq": 4,
            "generated_at": "2026-07-21T01:00:00+00:00",
            "confirmed_at": "2026-07-21T02:00:00+00:00",
            "sections": {
                "identity_commitments": [],
                "values": [],
                "expression_dna": [item],
                "mental_models": [],
                "decision_heuristics": [],
                "anti_patterns": [],
                "tensions": [],
                "honest_boundaries": [],
            },
        }

    def current(self):
        return json.loads(json.dumps(self._current))

    def versions(self):
        rows = []
        for index in range(4):
            row = json.loads(json.dumps(self._current))
            row["version_id"] = "lsv_" + str(index + 1) * 32
            row["parent_version_id"] = (
                None if index == 0 else "lsv_" + str(index) * 32
            )
            row["confirmed_at"] = "2026-07-%02dT02:00:00+00:00" % (18 + index)
            rows.append(row)
        return rows

    def load_version(self, version_id):
        for row in self.versions():
            if row["version_id"] == version_id:
                return row
        raise FileNotFoundError(version_id)

    def diff(self, from_version_id, to_version_id):
        self.load_version(from_version_id)
        self.load_version(to_version_id)
        return {
            "added": [{"item_id": "self-1", "item": self._current["sections"]["expression_dna"][0]}],
            "changed": [],
            "removed": [],
        }


class FakeJudgments:
    def list(self):
        return [
            {
                "card_id": "jud_%032x" % index,
                "title": "判断 %02d" % index,
                "situation": "客户 " + "Bear" + "er judgment-private-token",
                "goal": "交付",
                "constraints": ["成本"],
                "signals": ["明确需求"],
                "decision": "先验证",
                "alternatives": ["直接上线"],
                "outcome": {"status": "unknown", "summary": "", "observed_at": None},
                "lesson": "先做证据闭环",
                "next_trigger": "再次出现",
                "status": "candidate" if index == 0 else "confirmed",
                "evidence_ids": ["ev-private"],
                "claim_ids": ["clm_confirmed"],
                "privacy": "internal",
                "created_at": "2026-07-21T%02d:00:00+00:00" % index,
                "updated_at": "2026-07-21T%02d:00:00+00:00" % index,
                "revision": 1,
                "stream_version": 1,
                "based_on_event_seq": index + 1,
            }
            for index in range(12)
        ]

    def get(self, card_id):
        for row in self.list():
            if row["card_id"] == card_id:
                return row
        raise KeyError(card_id)


class FakeContexts:
    def list(self):
        return [
            {
                "preview_id": "prv_%032x" % index,
                "context_id": "ctx_%032x" % index,
                "revision": 2,
                "lifecycle_status": "consumed" if index else "outcome_recorded",
                "availability_status": "active",
                "task": "任务 " + "Bear" + "er context-private-token %02d" % index,
                "mode": "answer",
                "source_revision": {
                    "claims_event_seq": 4,
                    "living_self_version": "lsv_" + "1" * 32,
                    "judgments_event_seq": 12,
                    "compiler_version": "1.1",
                    "policy_version": 1,
                },
                "selection": {
                    "section_item_ids": {},
                    "selected_item_ids": ["clm_confirmed"],
                    "excluded_item_ids": ["clm_candidate"],
                },
                "privacy_policy": {"excluded_count": 1, "reasons": ["private"]},
                "preview_hash": "sha256:" + "1" * 64,
                "preview_body_hash": "sha256:" + "2" * 64,
                "generated_at": "2026-07-21T%02d:00:00+00:00" % index,
                "expires_at": "2026-08-21T00:00:00+00:00",
                "updated_at": "2026-07-21T%02d:30:00+00:00" % index,
                "compiled_at": "2026-07-21T%02d:10:00+00:00" % index,
                "consumed_at": "2026-07-21T%02d:20:00+00:00" % index,
                "outcome_recorded_at": "2026-07-21T00:40:00+00:00" if index == 0 else None,
                "outcome_id": "out_" + "0" * 32 if index == 0 else None,
                "outcome_hash": "sha256:" + "3" * 64 if index == 0 else None,
                "pack_snapshot_hash": "sha256:" + "4" * 64,
                "based_on_event_seq": index + 1,
                "stream_version": 2,
            }
            for index in range(9)
        ]

    def get(self, context_id):
        for row in self.list():
            if row["context_id"] == context_id or row["preview_id"] == context_id:
                return row
        raise KeyError(context_id)

    def load_preview_body(self, preview_id, preview_hash):
        record = self.get(preview_id)
        assert preview_hash == record["preview_hash"]
        return {
            "preview_id": record["preview_id"],
            "preview_hash": record["preview_hash"],
            "preview_body_hash": record["preview_body_hash"],
            "source_revision": record["source_revision"],
            "sections": {
                "verified_facts": [
                    {
                        "kind": "claim",
                        "id": "clm_confirmed",
                        "revision": 7,
                        "status": "confirmed",
                        "source_kind": "explicit",
                        "summary": "已确认事实",
                        "privacy": "internal",
                        "evidence_ids": ["ev_1"],
                        "claim_ids": ["clm_confirmed"],
                    }
                ],
                "confirmed_self_models": [],
                "judgment_cards": [],
                "counter_evidence": [],
                "inferences": [],
                "unknowns": [],
            },
            "compile_policy": {"max_chars": 12000, "max_bytes": 96000},
            "generated_at": record["generated_at"],
            "expires_at": record["expires_at"],
        }


class FakeContextCompiler:
    def __init__(self, contexts):
        self.contexts = contexts
        self.calls = []

    @staticmethod
    def _snapshot(record):
        sections = {
            "verified_facts": [
                {
                    "kind": "claim",
                    "id": "clm_confirmed",
                    "revision": 7,
                    "status": "confirmed",
                    "source_kind": "explicit",
                    "summary": "不可变快照正文",
                    "privacy": "internal",
                    "evidence_ids": ["ev_1"],
                    "claim_ids": ["clm_confirmed"],
                }
            ],
            "confirmed_self_models": [],
            "judgment_cards": [],
            "counter_evidence": [],
            "inferences": [],
            "unknowns": [],
        }
        return {
            "context_id": record["context_id"],
            "task": record["task"],
            "mode": record["mode"],
            "lifecycle_status": "compiled",
            "availability_status": record["availability_status"],
            "budget": {
                "max_chars": 12000,
                "used_chars": 321,
                "max_bytes": 96000,
                "used_bytes": 333,
            },
            "sections": sections,
            "provenance": {
                "evidence_ids": ["ev_1"],
                "claim_ids": ["clm_confirmed"],
                "self_model_item_ids": [],
                "judgment_card_ids": [],
            },
            "privacy_policy": {"excluded_count": 1, "reasons": ["private"]},
            "source_revision": record["source_revision"],
            "preview_hash": record["preview_hash"],
            "content_hash": "sha256:" + "5" * 64,
            "generated_at": record["generated_at"],
            "expires_at": record["expires_at"],
            "context_markdown": "# Immutable\n\n不可变快照正文\n",
            "context_markdown_hash": "sha256:" + "6" * 64,
            "context_json": "/private/must-not-leak/context.json",
            "context_md": "/private/must-not-leak/TASK_CONTEXT.md",
        }

    def load_compiled(self, context_id):
        self.calls.append(("compiled", context_id))
        return self._snapshot(self.contexts.get(context_id))

    def load_outcome_snapshot(self, context_id):
        self.calls.append(("outcome", context_id))
        return self._snapshot(self.contexts.get(context_id))


class FakeOutcomes:
    def list(self):
        return [
            {
                "outcome_id": "out_" + "0" * 32,
                "context_id": "ctx_" + "0" * 32,
                "adopted": "yes",
                "result": "success",
                "summary": "结果 " + "Bear" + "er outcome-private-token",
                "confirmed_refs": [{"kind": "claim", "id": "clm_confirmed", "revision": 3}],
                "challenged_refs": [],
                "created_at": "2026-07-21T00:40:00+00:00",
            }
        ]

    def get(self, context_id):
        for row in self.list():
            if row["context_id"] == context_id:
                return row
        raise KeyError(context_id)


def _seed_trusted_index(vault, memory_count=120):
    vault.mkdir(parents=True, exist_ok=True)
    source = vault / "index.jsonl"
    source.write_bytes(b"not-jsonl-and-must-never-be-scanned\n")
    db = vault / "search_index.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE docs(rowid INTEGER PRIMARY KEY, rec_id TEXT, ts TEXT, ts_utc TEXT NOT NULL, source TEXT, "
        "role TEXT, project TEXT, content TEXT, source_offset INTEGER NOT NULL, "
        "source_length INTEGER NOT NULL, line_number INTEGER NOT NULL, content_sha256 TEXT NOT NULL)"
    )
    con.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("CREATE INDEX idx_docs_rec_id ON docs(rec_id)")
    con.execute("CREATE INDEX idx_docs_source ON docs(source)")
    con.execute("CREATE INDEX idx_docs_ts ON docs(ts)")
    con.execute(
        "CREATE INDEX idx_docs_ts_utc_rowid ON docs(ts_utc DESC,rowid DESC)"
    )
    con.execute("CREATE UNIQUE INDEX idx_docs_source_offset ON docs(source_offset)")
    con.execute("CREATE UNIQUE INDEX idx_docs_line_number ON docs(line_number)")
    con.execute("CREATE VIRTUAL TABLE docs_fts USING fts5(content, tokenize='trigram')")
    ids = []
    for index in range(memory_count):
        memory_id = "memory-%04d" % index
        ids.append(memory_id)
        content = "安全摘要 %04d person-A topic-A" % index
        if index == 0:
            content += " " + "Bear" + "er memory-private-token"
        con.execute(
            "INSERT INTO docs(rec_id,ts,ts_utc,source,role,project,content,source_offset,source_length,line_number,content_sha256) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                memory_id,
                "2026-07-%02dT%02d:%02d:00+00:00" % (1 + index // (24 * 60), (index // 60) % 24, index % 60),
                "2026-07-%02dT%02d:%02d:00.000000Z" % (1 + index // (24 * 60), (index // 60) % 24, index % 60),
                "codex" if index % 2 else "claude",
                "user",
                "project-A" if index % 3 else "project-B",
                content,
                index * 100,
                len(content.encode("utf-8")),
                index + 1,
                "%064x" % index,
            ),
        )
        con.execute(
            "INSERT INTO docs_fts(rowid,content) VALUES(?,?)",
            (index + 1, content),
        )
    stat = source.stat()
    meta = {
        "parity_status": "trusted",
        "last_size": stat.st_size,
        "source_dev": stat.st_dev,
        "source_ino": stat.st_ino,
        "source_mtime_ns": stat.st_mtime_ns,
        "source_ctime_ns": stat.st_ctime_ns,
        "indexed_id_count": len(ids),
        "indexed_ids_sha256": ids_sha256(ids),
        "index_schema_version": INDEX_SCHEMA_VERSION,
        "coverage_person": "partial",
        "coverage_project": "complete",
        "coverage_topic": "partial",
    }
    con.executemany(
        "INSERT INTO meta(key,value) VALUES(?,?)",
        [(key, str(value)) for key, value in meta.items()],
    )
    con.commit()
    con.close()


def seeded_product_data(tmp_path, memory_count=120):
    vault = tmp_path / "vault"
    _seed_trusted_index(vault, memory_count=memory_count)
    control_data = FakeControlData()
    control_center = FakeControlCenter()
    contexts = FakeContexts()
    data = ProductData(
        vault,
        control_data=control_data,
        control_center=control_center,
        claim_store=FakeClaims(),
        living_self=FakeLivingSelf(),
        judgment_store=FakeJudgments(),
        context_store=contexts,
        context_compiler=FakeContextCompiler(contexts),
        outcome_store=FakeOutcomes(),
        clock=lambda: datetime(2026, 7, 22, 12, tzinfo=timezone.utc),
    )
    return data, control_data, control_center


def test_home_leads_with_memory_value_not_machine_metrics(tmp_path):
    data, _control, center = seeded_product_data(tmp_path)
    home = data.home()
    assert list(home)[:5] == [
        "remembered_today",
        "understanding_changes",
        "needs_confirmation",
        "latest_context_use",
        "latest_outcome",
    ]
    assert home["system_health"]["status"] == "healthy"
    assert home["confirmation_summary"] == {
        "total": 2,
        "claims": 1,
        "judgments": 1,
        "visible": 2,
    }
    assert center.calls == 1
    assert "private-token" not in json.dumps(home)


def test_memory_list_is_cursor_bounded_body_free_and_capped(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path, memory_count=80)
    page = data.memories({"limit": ["500"]})
    assert len(page["items"]) == 50
    assert page["next_cursor"]
    assert all("content" not in item for item in page["items"])
    assert all("summary" in item for item in page["items"])
    assert "offset" not in page


def test_memory_api_refuses_untrusted_index_instead_of_scanning_jsonl(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    data.index_integrity.mark_untrusted("id parity failed")
    with pytest.raises(ProductDataError) as raised:
        data.memories({"limit": ["20"]})
    assert raised.value.code == "index_unavailable"


def test_index_source_stat_drift_fails_closed(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    with (data.vault_dir / "index.jsonl").open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ProductDataError) as raised:
        data.memory_detail("memory-0001")
    assert raised.value.code == "index_unavailable"


def test_keyset_cursor_has_no_duplicates_or_gaps(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path, memory_count=120)
    seen = []
    cursor = ""
    while True:
        page = data.memories({"limit": ["17"], "cursor": [cursor]})
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 120
    assert len(set(seen)) == 120


@pytest.mark.parametrize("mutation", ["tamper", "filter", "endpoint"])
def test_cursor_is_signed_and_bound_to_filters_and_endpoint(tmp_path, mutation):
    data, _control, _center = seeded_product_data(tmp_path)
    page = data.memories({"limit": ["5"], "source": ["codex"]})
    cursor = page["next_cursor"]
    if mutation == "tamper":
        cursor = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        action = lambda: data.memories({"limit": ["5"], "source": ["codex"], "cursor": [cursor]})
    elif mutation == "filter":
        action = lambda: data.memories({"limit": ["5"], "source": ["claude"], "cursor": [cursor]})
    else:
        action = lambda: data.judgments({"limit": ["5"], "cursor": [cursor]})
    with pytest.raises(ProductDataError) as raised:
        action()
    assert raised.value.code == "invalid_cursor"


def test_memory_queries_reject_offset_and_unknown_parameters(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    for query in ({"offset": ["10"]}, {"unknown": ["x"]}):
        with pytest.raises(ProductDataError) as raised:
            data.memories(query)
        assert raised.value.code == "invalid_query"


def test_memory_filters_return_explicit_coverage(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    page = data.memories(
        {
            "person": ["person-A"],
            "project": ["project-A"],
            "topic": ["topic-A"],
            "limit": ["10"],
        }
    )
    assert page["coverage"] == {
        "person": {"status": "partial", "complete": False},
        "project": {"status": "complete", "complete": True},
        "topic": {"status": "partial", "complete": False},
    }
    assert page["coverage_complete"] is False


def test_memory_detail_is_single_record_and_redacted(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    detail = data.memory_detail("memory-0000")
    assert detail["id"] == "memory-0000"
    assert "private-token" not in json.dumps(detail)
    with pytest.raises(ProductDataError) as raised:
        data.memory_detail("missing")
    assert raised.value.code == "memory_not_found"


def test_self_model_and_item_expose_ids_not_private_evidence(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    model = data.self_model()
    detail = data.self_item("self-1")
    assert list(model["sections"]) == [
        "identity_commitments",
        "values",
        "expression_dna",
        "mental_models",
        "decision_heuristics",
        "anti_patterns",
        "tensions",
        "honest_boundaries",
    ]
    assert detail["evidence_ids"] == ["ev-private"]
    assert detail["claim_refs"] == [
        {"claim_id": "clm_confirmed", "revision": 7}
    ]
    assert model["total"] == 1
    assert model["truncated"] is False
    assert "private-token" not in json.dumps(model)
    assert "private-token" not in json.dumps(detail)
    assert "path" not in json.dumps(detail).lower()


def test_self_model_reports_honest_fifty_item_boundary(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    current = data.living_self._current
    template = current["sections"]["expression_dna"][0]
    current["sections"]["expression_dna"] = [
        {**template, "item_id": "self-%02d" % index}
        for index in range(53)
    ]
    model = data.self_model()
    returned = sum(len(rows) for rows in model["sections"].values())
    assert returned == 50
    assert model["total"] == 53
    assert model["truncated"] is True
    assert data.self_item("self-52")["item_id"] == "self-52"


def test_self_versions_are_keyset_paginated_and_diff_redacted(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    first = data.self_versions({"limit": ["2"]})
    second = data.self_versions({"limit": ["2"], "cursor": [first["next_cursor"]]})
    assert len(first["items"]) == 2
    assert len(second["items"]) == 2
    assert not second["next_cursor"]
    diff = data.self_diff(first["items"][-1]["version_id"], first["items"][0]["version_id"])
    assert "private-token" not in json.dumps(diff)


def test_self_unknown_and_corrupt_authority_fail_closed(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    with pytest.raises(ProductDataError) as missing:
        data.self_item("missing")
    assert missing.value.code == "self_item_not_found"
    data.living_self.current = lambda: (_ for _ in ()).throw(ValueError("corrupt path /private/x"))
    with pytest.raises(ProductDataError) as corrupt:
        data.self_model()
    assert corrupt.value.code == "self_model_unavailable"
    assert "/private/x" not in str(corrupt.value)


def test_judgments_are_keyset_paginated_body_safe_and_detail_redacted(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    page = data.judgments({"status": ["confirmed"], "limit": ["5"]})
    assert len(page["items"]) == 5
    assert page["next_cursor"]
    assert all("situation" not in item for item in page["items"])
    detail = data.judgment_detail(page["items"][0]["card_id"])
    assert detail["evidence_ids"] == ["ev-private"]
    assert "private-token" not in json.dumps(detail)


def test_contexts_are_keyset_paginated_and_never_expose_body_or_paths(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    page = data.contexts({"limit": ["4"]})
    assert len(page["items"]) == 4
    assert page["next_cursor"]
    serialized = json.dumps(page)
    assert "private-token" not in serialized
    assert "context_markdown" not in serialized
    assert "path" not in serialized.lower()
    detail = data.context_detail(page["items"][0]["context_id"])
    assert detail["selected_item_ids"] == ["clm_confirmed"]
    assert "private-token" not in json.dumps(detail)


def test_preview_context_detail_returns_verified_refresh_contract(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    record = data.context_store.list()[1]
    record["lifecycle_status"] = "preview"
    record["context_id"] = ""
    data.context_store.list = lambda: [record]
    data.context_store.get = lambda identifier: record
    detail = data.context_detail(record["preview_id"])
    assert detail["preview_hash"] == record["preview_hash"]
    assert detail["expires_at"] == record["expires_at"]
    assert detail["revision"] == 2
    assert detail["sections"]["verified_facts"][0]["id"] == "clm_confirmed"
    assert detail["budget"] == {
        "max_chars": 12000,
        "used_chars": 309,
        "max_bytes": 96000,
        "used_bytes": 319,
    }
    assert detail["provenance"] == {
        "evidence_ids": ["ev_1"],
        "claim_ids": ["clm_confirmed"],
        "self_model_item_ids": [],
        "judgment_card_ids": [],
    }
    assert detail["privacy"] == {"excluded_count": 1, "reasons": ["private"]}


@pytest.mark.parametrize(
    ("status", "loader"),
    [("compiled", "compiled"), ("consumed", "outcome"), ("outcome_recorded", "outcome")],
)
def test_persisted_context_detail_uses_verified_immutable_snapshot(
    tmp_path, status, loader
):
    data, _control, _center = seeded_product_data(tmp_path)
    record = data.context_store.list()[1]
    record["lifecycle_status"] = status
    data.context_store.list = lambda: [record]
    data.context_store.get = lambda identifier: record
    detail = data.context_detail(record["context_id"])
    assert data.context_compiler.calls == [(loader, record["context_id"])]
    assert detail["context_markdown"] == "# Immutable\n\n不可变快照正文\n"
    assert detail["context_markdown_hash"] == "sha256:" + "6" * 64
    assert detail["content_hash"] == "sha256:" + "5" * 64
    assert detail["sections"]["verified_facts"][0]["summary"] == "不可变快照正文"
    assert detail["provenance"]["claim_ids"] == ["clm_confirmed"]
    assert detail["privacy"] == {"excluded_count": 1, "reasons": ["private"]}
    assert "must-not-leak" not in json.dumps(detail)


def test_latest_outcome_is_attached_to_context_detail_without_raw_body(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    detail = data.context_detail("ctx_" + "0" * 32)
    assert detail["outcome"]["result"] == "success"
    assert "private-token" not in json.dumps(detail)


def test_missing_and_corrupt_judgment_context_authorities_use_stable_errors(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    with pytest.raises(ProductDataError) as judgment:
        data.judgment_detail("jud_missing")
    assert judgment.value.code == "judgment_not_found"
    data.context_store.list = lambda: (_ for _ in ()).throw(ValueError("stderr Bearer x"))
    with pytest.raises(ProductDataError) as context:
        data.contexts({})
    assert context.value.code == "context_unavailable"
    assert "Bearer" not in str(context.value)


def test_trust_surfaces_bounded_evidence_gaps_privacy_and_candidates(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    trust = data.trust()
    assert trust["summary"]["needs_confirmation"] >= 2
    assert trust["summary"]["candidate_claims"] == 1
    assert trust["summary"]["candidate_judgments"] == 1
    kinds = {item["kind"] for item in trust["items"]}
    assert {"missing_evidence", "low_confidence", "privacy_exclusion"} <= kinds
    assert len(trust["items"]) <= 50
    assert "private-token" not in json.dumps(trust)


def test_trust_does_not_mislabel_user_exclusion_as_privacy_policy(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    contexts = data.context_store.list()
    for context in contexts:
        context["privacy_policy"] = {
            "excluded_count": 1,
            "reasons": ["user_excluded"],
        }
    data.context_store.list = lambda: contexts

    trust = data.trust()

    assert trust["categories"]["privacy_exclusion"]["count"] == 0


def test_system_delegates_to_existing_authoritative_probes(tmp_path):
    data, control, center = seeded_product_data(tmp_path)
    result = data.system()
    assert control.calls == ["capabilities", "sources", "backups", "diagnostics"]
    assert center.calls == 1
    assert result["health"]["status"] == "healthy"
    assert result["diagnostics"]["version"] == "1.1.0"


def test_product_data_source_has_no_offset_or_jsonl_scan_fallback():
    source = Path(__file__).parents[1].joinpath("core", "product_data.py").read_text(encoding="utf-8")
    assert " OFFSET " not in source.upper()
    assert "_iter_memories" not in source
    assert ".read_text(" not in source


@pytest.mark.parametrize(
    "mutation",
    ["parity", "schema", "digest", "missing_meta", "locator_index"],
)
def test_invalid_sqlite_trust_evidence_fails_closed(tmp_path, mutation):
    data, _control, _center = seeded_product_data(tmp_path)
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    if mutation == "parity":
        con.execute("UPDATE meta SET value='untrusted' WHERE key='parity_status'")
    elif mutation == "schema":
        con.execute("UPDATE meta SET value='1' WHERE key='index_schema_version'")
    elif mutation == "digest":
        con.execute("UPDATE meta SET value='not-a-digest' WHERE key='indexed_ids_sha256'")
    elif mutation == "missing_meta":
        con.execute("DELETE FROM meta WHERE key='source_ctime_ns'")
    else:
        con.execute("DROP INDEX idx_docs_line_number")
    con.commit()
    con.close()
    with pytest.raises(ProductDataError) as raised:
        data.memories({})
    assert raised.value.code == "index_unavailable"
    assert "search_index" not in str(raised.value)


@pytest.mark.parametrize(
    "mutation",
    ["delete_doc", "replace_id", "delete_fts", "replace_fts_content"],
)
def test_sqlite_actual_rows_must_match_trusted_count_and_digest(tmp_path, mutation):
    data, _control, _center = seeded_product_data(tmp_path)
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    if mutation == "delete_doc":
        con.execute("DELETE FROM docs WHERE rec_id='memory-0000'")
    elif mutation == "replace_id":
        con.execute(
            "UPDATE docs SET rec_id='memory-replaced' WHERE rec_id='memory-0000'"
        )
    elif mutation == "delete_fts":
        con.execute("DELETE FROM docs_fts WHERE rowid=1")
    else:
        con.execute(
            "UPDATE docs_fts SET content='篡改命中标记' WHERE rowid=1"
        )
    con.commit()
    con.close()
    with pytest.raises(ProductDataError) as raised:
        data.memories({"limit": ["2"]})
    assert raised.value.code == "index_unavailable"


def test_missing_index_verification_receipt_is_rebuilt_after_full_validation(
    tmp_path, monkeypatch
):
    data, _control, _center = seeded_product_data(tmp_path)
    assert data.memories({"limit": ["2"]})["items"]
    receipt = data.vault_dir / "product" / "index-verification.json"
    assert receipt.is_file()
    receipt.unlink()
    integrity = ProductIndexIntegrity(data.vault_dir)
    calls = []
    original = integrity._deep_validate_index

    def counted(connection, metadata):
        calls.append(True)
        return original(connection, metadata)

    monkeypatch.setattr(integrity, "_deep_validate_index", counted)
    with integrity.trusted_connection():
        pass
    assert calls == [True]
    assert receipt.is_file()
    assert receipt.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["corrupt", "permissions", "symlink"])
def test_unsafe_index_verification_receipt_fails_closed(tmp_path, mutation):
    data, _control, _center = seeded_product_data(tmp_path)
    assert data.memories({"limit": ["2"]})["items"]
    receipt = data.vault_dir / "product" / "index-verification.json"
    if mutation == "corrupt":
        receipt.write_text("not-json\n", encoding="utf-8")
    elif mutation == "permissions":
        receipt.chmod(0o644)
    else:
        receipt.unlink()
        receipt.symlink_to(data.vault_dir / "index.jsonl")
    integrity = ProductIndexIntegrity(data.vault_dir)
    with pytest.raises(ProductDataError) as raised:
        with integrity.trusted_connection():
            pass
    assert raised.value.code == "index_unavailable"


@pytest.mark.parametrize("mutation", ["delete", "corrupt", "permissions", "symlink"])
def test_same_integrity_instance_rechecks_receipt_safety_on_every_access(
    tmp_path, mutation
):
    data, _control, _center = seeded_product_data(tmp_path)
    assert data.memories({"limit": ["2"]})["items"]
    receipt = data.vault_dir / "product" / "index-verification.json"
    if mutation == "delete":
        receipt.unlink()
    elif mutation == "corrupt":
        receipt.write_text("not-json\n", encoding="utf-8")
    elif mutation == "permissions":
        receipt.chmod(0o644)
    else:
        receipt.unlink()
        receipt.symlink_to(data.vault_dir / "index.jsonl")
    with pytest.raises(ProductDataError) as raised:
        data.memories({"limit": ["2"]})
    assert raised.value.code == "index_unavailable"


def test_stale_index_verification_receipt_forces_full_revalidation(
    tmp_path, monkeypatch
):
    data, _control, _center = seeded_product_data(tmp_path)
    assert data.memories({"limit": ["2"]})["items"]
    receipt = data.vault_dir / "product" / "index-verification.json"
    body = json.loads(receipt.read_text(encoding="utf-8"))
    body["validation_version"] = 0
    receipt.write_text(json.dumps(body), encoding="utf-8")
    receipt.chmod(0o600)
    integrity = ProductIndexIntegrity(data.vault_dir)
    calls = []
    original = integrity._deep_validate_index

    def counted(connection, metadata):
        calls.append(True)
        return original(connection, metadata)

    monkeypatch.setattr(integrity, "_deep_validate_index", counted)
    with integrity.trusted_connection():
        pass
    assert calls == [True]
    assert json.loads(receipt.read_text(encoding="utf-8"))[
        "validation_version"
    ] == 1


def test_matching_receipt_skips_deep_scan_across_integrity_instances(
    tmp_path, monkeypatch
):
    data, _control, _center = seeded_product_data(tmp_path)
    assert data.memories({"limit": ["2"]})["items"]
    integrity = ProductIndexIntegrity(data.vault_dir)
    monkeypatch.setattr(
        integrity,
        "_deep_validate_index",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("matching receipt must skip deep scan")
        ),
    )
    with integrity.trusted_connection():
        pass


def test_receipt_does_not_hide_later_fts_content_change(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    assert data.memories({"limit": ["2"]})["items"]
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    con.execute("UPDATE docs_fts SET content='后续篡改命中' WHERE rowid=1")
    con.commit()
    con.close()
    integrity = ProductIndexIntegrity(data.vault_dir)
    with pytest.raises(ProductDataError) as raised:
        with integrity.trusted_connection():
            pass
    assert raised.value.code == "index_unavailable"


@pytest.mark.parametrize(
    ("endpoint", "id_field", "time_field"),
    [
        ("self_versions", "version_id", "confirmed_at"),
        ("judgments", "card_id", "updated_at"),
        ("contexts", "context_id", "updated_at"),
    ],
)
def test_model_keyset_uses_utc_order_for_mixed_offsets_without_gaps(
    tmp_path, endpoint, id_field, time_field
):
    data, _control, _center = seeded_product_data(tmp_path)
    if endpoint == "self_versions":
        rows = data.living_self.versions()
        data.living_self.versions = lambda: rows
    elif endpoint == "judgments":
        rows = data.judgment_store.list()
        data.judgment_store.list = lambda: rows
    else:
        rows = data.context_store.list()
        data.context_store.list = lambda: rows
    for index, row in enumerate(rows):
        utc_value = datetime(2026, 7, 20, 0, index, tzinfo=timezone.utc)
        if index % 2 == 0:
            row[time_field] = (
                utc_value.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
            )
        else:
            row[time_field] = utc_value.isoformat().replace("+00:00", "Z")
    expected = [row[id_field] for row in reversed(rows)]
    raw_times = {row[id_field]: row[time_field] for row in rows}

    seen = []
    cursor = ""
    while True:
        page = getattr(data, endpoint)({"limit": ["2"], "cursor": [cursor]})
        for item in page["items"]:
            seen.append(item[id_field])
            assert item[time_field] == raw_times[item[id_field]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == expected
    assert len(seen) == len(set(seen)) == len(rows)


@pytest.mark.parametrize(
    ("endpoint", "id_field", "expected"),
    [
        ("self_versions", "version_id", 4),
        ("judgments", "card_id", 12),
        ("contexts", "context_id", 9),
    ],
)
def test_model_keyset_pages_have_no_duplicates_or_gaps(
    tmp_path, endpoint, id_field, expected
):
    data, _control, _center = seeded_product_data(tmp_path)
    method = getattr(data, endpoint)
    seen = []
    cursor = ""
    while True:
        page = method({"limit": ["3"], "cursor": [cursor]})
        seen.extend(row[id_field] for row in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == expected
    assert len(set(seen)) == expected


@pytest.mark.parametrize("endpoint", ["self_versions", "judgments", "contexts"])
def test_model_cursors_reject_tampering_and_cross_endpoint_reuse(tmp_path, endpoint):
    data, _control, _center = seeded_product_data(tmp_path)
    page = getattr(data, endpoint)({"limit": ["2"]})
    cursor = page["next_cursor"]
    tampered = cursor[:-2] + ("AA" if cursor[-2:] != "AA" else "BB")
    with pytest.raises(ProductDataError) as changed:
        getattr(data, endpoint)({"limit": ["2"], "cursor": [tampered]})
    assert changed.value.code == "invalid_cursor"
    other = "contexts" if endpoint != "contexts" else "judgments"
    with pytest.raises(ProductDataError) as crossed:
        getattr(data, other)({"limit": ["2"], "cursor": [cursor]})
    assert crossed.value.code == "invalid_cursor"


def test_model_cursor_is_bound_to_filter(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    page = data.judgments({"status": ["confirmed"], "limit": ["2"]})
    with pytest.raises(ProductDataError) as raised:
        data.judgments(
            {"status": ["candidate"], "limit": ["2"], "cursor": [page["next_cursor"]]}
        )
    assert raised.value.code == "invalid_cursor"


@pytest.mark.parametrize("endpoint", ["self_versions", "judgments", "contexts"])
def test_model_cursor_is_invalidated_when_authority_generation_changes(
    tmp_path, endpoint
):
    data, _control, _center = seeded_product_data(tmp_path)
    method = getattr(data, endpoint)
    first = method({"limit": ["2"]})
    cursor = first["next_cursor"]
    assert cursor
    if endpoint == "self_versions":
        rows = data.living_self.versions()
        extra = dict(rows[-1])
        extra["version_id"] = "lsv_" + "f" * 32
        extra["confirmed_at"] = "2026-07-22T00:00:00+00:00"
        data.living_self.versions = lambda: rows + [extra]
    elif endpoint == "judgments":
        rows = data.judgment_store.list()
        extra = dict(rows[-1])
        extra["card_id"] = "jud_" + "f" * 32
        extra["updated_at"] = "2026-07-22T00:00:00+00:00"
        extra["revision"] = 2
        data.judgment_store.list = lambda: rows + [extra]
    else:
        rows = data.context_store.list()
        extra = dict(rows[-1])
        extra["context_id"] = "ctx_" + "e" * 32
        extra["preview_id"] = "prv_" + "e" * 32
        extra["updated_at"] = "2026-07-22T00:00:00+00:00"
        extra["revision"] = 3
        data.context_store.list = lambda: rows + [extra]
    with pytest.raises(ProductDataError) as raised:
        method({"limit": ["2"], "cursor": [cursor]})
    assert raised.value.code == "invalid_cursor"


@pytest.mark.parametrize(
    ("authority", "code"),
    [
        ("claims", "trust_unavailable"),
        ("living", "self_model_unavailable"),
        ("judgments", "judgment_unavailable"),
        ("contexts", "context_unavailable"),
        ("outcomes", "outcome_unavailable"),
        ("system", "system_unavailable"),
    ],
)
def test_home_converts_each_authority_failure_to_stable_safe_error(
    tmp_path, authority, code
):
    data, _control, center = seeded_product_data(tmp_path)
    failure = lambda: (_ for _ in ()).throw(
        RuntimeError(
            "stderr "
            + ("/" + "Users/private-owner")
            + " "
            + "Bear"
            + "er secret-value"
        )
    )
    if authority == "claims":
        data.claim_store.list = failure
    elif authority == "living":
        data.living_self.versions = failure
    elif authority == "judgments":
        data.judgment_store.list = failure
    elif authority == "contexts":
        data.context_store.list = failure
    elif authority == "outcomes":
        data.outcome_store.list = failure
    else:
        center.build_snapshot = failure
    with pytest.raises(ProductDataError) as raised:
        data.home()
    assert raised.value.code == code
    assert "stderr" not in str(raised.value)
    assert "private-owner" not in str(raised.value)


def test_product_payload_redacts_all_supported_private_shapes(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path, memory_count=2)
    home_path = "/" + "Users/private-owner/secret.txt"
    open_id = "o" + "u_" + "a" * 32
    aws_key = "A" + "KIA" + "A" * 16
    pem_body = "c2VjcmV0LWJvZHktbXVzdC1ub3QtbGVhaw=="
    private_key = (
        "-" * 5
        + "BEGIN PRIVATE KEY"
        + "-" * 5
        + "\n"
        + pem_body
        + "\n"
        + "-" * 5
        + "END PRIVATE KEY"
        + "-" * 5
    )
    secret = " | ".join(
        [
            "Cookie" + ": session=abcdefghijklmnop",
            "https://" + "person:" + "password@" + "example.com/a",
            home_path,
            open_id,
            aws_key,
            private_key,
        ]
    )
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    con.execute("UPDATE docs SET content=? WHERE rec_id='memory-0000'", (secret,))
    con.execute("UPDATE docs_fts SET content=? WHERE rowid=1", (secret,))
    con.commit()
    con.close()
    payload = json.dumps(data.memory_detail("memory-0000"))
    for marker in (
        "abcdefghijklmnop",
        "person:password",
        "private-owner",
        open_id,
        aws_key,
        "BEGIN PRIVATE KEY",
        pem_body,
    ):
        assert marker not in payload


@pytest.mark.parametrize(
    ("method_name", "query"),
    [
        ("memories", {"limit": ["0"]}),
        ("memories", {"limit": ["bad"]}),
        ("memories", {"from": ["not-a-time"]}),
        ("memories", {"from": ["2026-08-01T00:00:00+00:00"], "to": ["2026-07-01T00:00:00+00:00"]}),
        ("self_versions", {"unknown": ["x"]}),
        ("judgments", {"limit": ["0"]}),
        ("contexts", {"cursor": ["not-a-cursor"]}),
    ],
)
def test_all_lists_reject_invalid_query_contracts(tmp_path, method_name, query):
    data, _control, _center = seeded_product_data(tmp_path)
    with pytest.raises(ProductDataError) as raised:
        getattr(data, method_name)(query)
    assert raised.value.code in {"invalid_query", "invalid_cursor"}


def test_coverage_is_unknown_without_explicit_derived_coverage_metadata(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    con.execute("DELETE FROM meta WHERE key LIKE 'coverage_%'")
    con.commit()
    con.close()
    page = data.memories({"project": ["project-A"]})
    assert page["coverage"]["project"] == {
        "status": "unknown",
        "complete": False,
    }
    assert page["coverage_complete"] is False


def test_legacy_control_data_scan_is_never_called(tmp_path, monkeypatch):
    data, _control, _center = seeded_product_data(tmp_path)
    from control_data import ControlData

    monkeypatch.setattr(
        ControlData,
        "_iter_memories",
        lambda _self: (_ for _ in ()).throw(AssertionError("legacy scan called")),
    )
    assert data.memories({"limit": ["2"]})["items"]
    assert data.memory_detail("memory-0001")["id"] == "memory-0001"


def test_memory_query_plan_uses_index_and_never_offset(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path, memory_count=5000)
    with data.index_integrity.trusted_connection() as (connection, _meta):
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT rowid,rec_id,ts FROM docs "
            "WHERE (ts_utc,rowid) < (?,?) "
            "ORDER BY ts_utc DESC,rowid DESC LIMIT ?",
            ("2026-08-01T00:00:00.000000Z", 4000, 51),
        ).fetchall()
    assert any(
        "SEARCH" in str(row).upper()
        and "idx_docs_ts_utc_rowid" in str(row)
        for row in plan
    )
    assert not any("SCAN" in str(row).upper() for row in plan)
    first = data.memories({"limit": ["50"]})
    assert len(first["items"]) == 50
    assert first["next_cursor"]


def test_system_recursively_redacts_and_removes_internal_execution_fields(tmp_path):
    data, control, center = seeded_product_data(tmp_path)
    private_path = "/" + "Users/private-owner/runtime.log"
    open_id = "o" + "u_" + "b" * 32
    secret_shapes = (
        "Cookie" + ": sid=abcdefghijklmnop | "
        "https://" + "user:" + "password@" + "example.com | "
        + private_path
        + " | "
        + open_id
        + " | "
        + ("A" + "KIA" + "B" * 16)
        + " | "
        + ("-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5)
    )
    center.build_snapshot = lambda: {
        "status": "healthy",
        "nested": {
            "path": private_path,
            "command": "dangerous",
            "stderr": secret_shapes,
            "safe": secret_shapes,
        },
    }
    control.diagnostics = lambda: {
        "ready": True,
        "args": ["private"],
        "note": secret_shapes,
    }
    payload = data.system()
    serialized = json.dumps(payload)
    for forbidden in ("path", "command", "stderr"):
        assert forbidden not in payload["health"]["nested"]
    assert "args" not in payload["diagnostics"]
    for marker in (
        "abcdefghijklmnop",
        "user:password",
        "private-owner",
        open_id,
        "BEGIN PRIVATE KEY",
    ):
        assert marker not in serialized


def test_home_latest_context_is_latest_real_use_not_preview_or_compiled(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    existing = data.context_store.list()
    existing.append(
        {
            **existing[0],
            "context_id": "ctx_" + "f" * 32,
            "preview_id": "prv_" + "f" * 32,
            "lifecycle_status": "preview",
            "updated_at": "2026-07-22T11:59:00+00:00",
            "consumed_at": None,
            "outcome_recorded_at": None,
            "outcome_id": None,
        }
    )
    data.context_store.list = lambda: existing
    latest = data.home()["latest_context_use"]
    assert latest["lifecycle_status"] in {"consumed", "outcome_recorded"}
    assert latest["context_id"] != "ctx_" + "f" * 32


def test_home_today_uses_clock_local_timezone_boundary(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path, memory_count=3)
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    con.execute(
        "UPDATE docs SET ts='2026-07-21T16:30:00+00:00',ts_utc='2026-07-21T16:30:00.000000Z' WHERE rec_id='memory-0000'"
    )
    con.execute(
        "UPDATE docs SET ts='2026-07-21T15:30:00+00:00',ts_utc='2026-07-21T15:30:00.000000Z' WHERE rec_id='memory-0001'"
    )
    con.execute(
        "UPDATE docs SET ts='2026-07-21T17:30:00+00:00',ts_utc='2026-07-21T17:30:00.000000Z' WHERE rec_id='memory-0002'"
    )
    con.commit()
    con.close()
    data._clock = lambda: datetime(
        2026, 7, 22, 0, 45, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    remembered = data.home()["remembered_today"]
    assert [row["id"] for row in remembered] == ["memory-0000"]


def test_memory_text_filters_use_fts_and_short_terms_fail_explicitly(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    statements = []
    original = data.index_integrity.trusted_connection

    @contextmanager
    def traced_connection():
        with original() as pair:
            pair[0].set_trace_callback(statements.append)
            yield pair
            pair[0].set_trace_callback(None)

    data.index_integrity.trusted_connection = traced_connection
    try:
        page = data.memories({"q": ["安全摘要"], "limit": ["3"]})
    finally:
        data.index_integrity.trusted_connection = original
    assert page["items"]
    assert any("docs_fts" in statement and "MATCH" in statement for statement in statements)
    assert not any("content LIKE" in statement for statement in statements)
    for field in ("q", "person", "topic"):
        with pytest.raises(ProductDataError) as raised:
            data.memories({field: ["AI"]})
        assert raised.value.code == "query_too_short"


@pytest.mark.parametrize(
    "query",
    [
        {"q": ["x" * 257]},
        {"project": ["x" * 257]},
        {"cursor": ["x" * 4097]},
        {"from": ["2026-07-22"]},
    ],
)
def test_memory_filters_and_cursor_have_hard_input_bounds(tmp_path, query):
    data, _control, _center = seeded_product_data(tmp_path)
    with pytest.raises(ProductDataError) as raised:
        data.memories(query)
    assert raised.value.code in {"invalid_query", "invalid_cursor"}


def test_memory_response_does_not_expose_internal_index_generation(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    page = data.memories({"limit": ["2"]})
    assert "index_generation" not in page


def test_home_understanding_changes_are_real_diff_not_version_metadata(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    changes = data.home()["understanding_changes"]
    assert changes["kind"] == "diff"
    assert changes["from_version_id"] == "lsv_" + "3" * 32
    assert changes["to_version_id"] == "lsv_" + "4" * 32
    assert changes["counts"] == {"added": 1, "changed": 0, "removed": 0}
    assert changes["added"][0]["item_id"] == "self-1"
    assert "private-token" not in json.dumps(changes)


def test_home_understanding_changes_marks_first_version_without_inventing_diff(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    only = data.living_self.versions()[0]
    data.living_self.versions = lambda: [only]
    changes = data.home()["understanding_changes"]
    assert changes == {
        "kind": "initial",
        "from_version_id": None,
        "to_version_id": only["version_id"],
        "counts": {"added": 0, "changed": 0, "removed": 0},
        "added": [],
        "changed": [],
        "removed": [],
    }


def test_trust_has_all_spec_categories_with_counts_and_coverage(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    claims = data.claim_store.list()
    claims[0].update(
        {
            "speaker": {"kind": "unknown", "id": "unknown"},
            "subject": {"kind": "owner", "id": "owner"},
            "valid_to": "2026-07-20T00:00:00+00:00",
            "counter_evidence_ids": ["ev_counter"],
        }
    )
    data.claim_store.list = lambda: claims
    trust = data.trust()
    expected = {
        "unknown_speaker",
        "other_view_candidate",
        "missing_evidence",
        "low_confidence",
        "expired_model",
        "conflict",
        "source_broken",
        "privacy_exclusion",
        "recent_correction",
        "model_evaluation",
    }
    assert set(trust["categories"]) == expected
    for category in trust["categories"].values():
        assert set(category) == {"count", "coverage", "items", "truncated"}
        assert category["coverage"] in {"complete", "partial", "unknown"}
        assert category["count"] >= len(category["items"])
        assert category["truncated"] is (
            category["count"] > len(category["items"])
        )
        assert len(category["items"]) <= 50
    assert trust["categories"]["unknown_speaker"]["count"] == 1
    assert trust["categories"]["expired_model"]["count"] == 1
    assert trust["categories"]["conflict"]["count"] >= 1
    assert trust["categories"]["source_broken"] == {
        "count": 0,
        "coverage": "unknown",
        "items": [],
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("target", "code"),
    [
        ("self_diff", "self_model_unavailable"),
        ("judgment_detail", "judgment_unavailable"),
        ("context_detail", "context_unavailable"),
        ("outcome_detail", "outcome_unavailable"),
    ],
)
def test_detail_authority_unexpected_failures_are_stable_and_safe(
    tmp_path, target, code
):
    data, _control, _center = seeded_product_data(tmp_path)
    failure = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("stderr " + ("/" + "Users/private-owner") + " secret")
    )
    if target == "self_diff":
        data.living_self.diff = failure
        call = lambda: data.self_diff("lsv_" + "1" * 32, "lsv_" + "2" * 32)
    elif target == "judgment_detail":
        data.judgment_store.get = failure
        call = lambda: data.judgment_detail("jud_" + "1" * 32)
    elif target == "context_detail":
        data.context_store.get = failure
        call = lambda: data.context_detail("ctx_" + "1" * 32)
    else:
        data.outcome_store.get = failure
        call = lambda: data.context_detail("ctx_" + "0" * 32)
    with pytest.raises(ProductDataError) as raised:
        call()
    assert raised.value.code == code
    assert "stderr" not in str(raised.value)
    assert "private-owner" not in str(raised.value)


def test_trust_and_nested_product_lists_share_a_global_fifty_item_budget(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    base = data.claim_store.list()[0]
    data.claim_store.list = lambda: [
        {
            **base,
            "claim_id": "clm_%04d" % index,
            "statement": "候选 %04d" % index,
            "status": "candidate",
            "confidence": 0.1,
            "evidence_ids": [],
        }
        for index in range(120)
    ]
    trust = data.trust()
    visible = sum(
        len(category["items"]) for category in trust["categories"].values()
    )
    assert visible <= 50
    assert len(trust["items"]) <= 50
    missing = trust["categories"]["missing_evidence"]
    low = trust["categories"]["low_confidence"]
    assert missing["count"] == 120
    assert low["count"] == 120
    assert missing["truncated"] is True
    assert low["truncated"] is True
    assert len({item["id"] for item in missing["items"]}) == len(
        missing["items"]
    )
    assert missing["items"]
    assert low["items"]


def test_time_filters_normalize_offsets_before_sql_comparison(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path, memory_count=1)
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    con.execute(
        "UPDATE docs SET ts='2026-07-21T16:30:00+00:00',ts_utc='2026-07-21T16:30:00.000000Z' WHERE rec_id='memory-0000'"
    )
    con.commit()
    con.close()
    page = data.memories(
        {
            "from": ["2026-07-22T00:00:00+08:00"],
            "to": ["2026-07-22T01:00:00+08:00"],
        }
    )
    assert [row["id"] for row in page["items"]] == ["memory-0000"]


def test_memory_order_uses_normalized_utc_not_raw_iso_text(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path, memory_count=2)
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    con.execute(
        "UPDATE docs SET ts=?,ts_utc=? WHERE rec_id='memory-0000'",
        ("2026-07-22T00:00:00+08:00", "2026-07-21T16:00:00.000000Z"),
    )
    con.execute(
        "UPDATE docs SET ts=?,ts_utc=? WHERE rec_id='memory-0001'",
        ("2026-07-21T17:00:00+00:00", "2026-07-21T17:00:00.000000Z"),
    )
    con.commit()
    con.close()
    page = data.memories({"limit": ["2"]})
    assert [row["id"] for row in page["items"]] == [
        "memory-0001",
        "memory-0000",
    ]
    assert [row["timestamp"] for row in page["items"]] == [
        "2026-07-21T17:00:00+00:00",
        "2026-07-22T00:00:00+08:00",
    ]


def test_product_read_model_rejects_old_v2_index_until_rebuilt(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    con = sqlite3.connect(str(data.vault_dir / "search_index.db"))
    con.execute("UPDATE meta SET value='2' WHERE key='index_schema_version'")
    con.execute("DROP INDEX idx_docs_ts_utc_rowid")
    con.commit()
    con.close()
    with pytest.raises(ProductDataError) as raised:
        data.memories({"limit": ["2"]})
    assert raised.value.code == "index_unavailable"


def _reopen_product_data(data):
    return ProductData(
        data.vault_dir,
        control_data=data.control_data,
        control_center=data.control_center,
        claim_store=data.claim_store,
        living_self=data.living_self,
        judgment_store=data.judgment_store,
        context_store=data.context_store,
        outcome_store=data.outcome_store,
        clock=data._clock,
    )


def test_cursor_signing_key_is_random_persistent_private_and_vault_bound(tmp_path):
    first, _control, _center = seeded_product_data(tmp_path / "first")
    page = first.memories({"limit": ["2"]})
    cursor = page["next_cursor"]
    key_path = first.vault_dir / "product" / "cursor-signing.key"
    assert key_path.is_file()
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert len(key_path.read_text(encoding="ascii").strip()) == 64

    reopened = _reopen_product_data(first)
    assert reopened.memories(
        {"limit": ["2"], "cursor": [cursor]}
    )["items"]

    second, _control, _center = seeded_product_data(tmp_path / "second")
    with pytest.raises(ProductDataError) as crossed:
        second.memories({"limit": ["2"], "cursor": [cursor]})
    assert crossed.value.code == "invalid_cursor"
    second_key = (
        second.vault_dir / "product" / "cursor-signing.key"
    ).read_text(encoding="ascii")
    assert second_key != key_path.read_text(encoding="ascii")


@pytest.mark.parametrize("mutation", ["corrupt", "permissions", "symlink"])
def test_cursor_signing_key_unsafe_state_fails_closed(tmp_path, mutation):
    data, _control, _center = seeded_product_data(tmp_path)
    assert data.memories({"limit": ["2"]})["next_cursor"]
    key_path = data.vault_dir / "product" / "cursor-signing.key"
    if mutation == "corrupt":
        key_path.write_text("not-a-key\n", encoding="ascii")
    elif mutation == "permissions":
        key_path.chmod(0o644)
    else:
        key_path.unlink()
        key_path.symlink_to(data.vault_dir / "index.jsonl")
    reopened = _reopen_product_data(data)
    assert reopened.home()["system_health"]["status"] == "healthy"
    with pytest.raises(ProductDataError) as raised:
        reopened.memories({"limit": ["2"]})
    assert raised.value.code == "cursor_key_unavailable"


def test_self_model_has_one_global_fifty_item_response_budget(tmp_path):
    data, _control, _center = seeded_product_data(tmp_path)
    current = data.living_self.current()
    template = current["sections"]["expression_dna"][0]
    for section in current["sections"]:
        current["sections"][section] = [
            {**template, "item_id": "self-%s-%03d" % (section, index)}
            for index in range(20)
        ]
    data.living_self.current = lambda: current
    model = data.self_model()
    assert sum(len(rows) for rows in model["sections"].values()) <= 50
