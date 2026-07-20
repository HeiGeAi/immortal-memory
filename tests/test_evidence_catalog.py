import json

import pytest

from evidence_catalog import EvidenceCatalog, EvidenceCatalogError


def record(
    raw_id,
    content,
    *,
    source="codex-conversation",
    timestamp="2026-07-19T00:00:00+00:00",
):
    return {
        "id": raw_id,
        "source": source,
        "timestamp": timestamp,
        "content": content,
    }


def write_records(path, rows):
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def assert_error(code, operation):
    with pytest.raises(EvidenceCatalogError) as captured:
        operation()
    assert captured.value.code == code


def test_existing_fact_record_resolves_exact_stable_id_without_raw_body_or_rowid(
    tmp_path,
):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实正文")])

    catalog = EvidenceCatalog(index)
    ref = catalog.resolve("raw-1")

    assert ref == {
        "evidence_id": "raw-1",
        "source": "codex",
        "raw_id": "raw-1",
        "content_hash": ref["content_hash"],
        "status": "available",
        "observed_at": "2026-07-19T00:00:00+00:00",
        "privacy": "restricted",
    }
    assert ref["content_hash"].startswith("sha256:")
    assert len(ref["content_hash"]) == 71
    assert "rowid" not in ref
    assert "content" not in ref
    assert "事实正文" not in json.dumps(catalog.list(), ensure_ascii=False)


def test_resolve_requires_exact_requested_jsonl_id_and_never_uses_rowid(tmp_path):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实正文")])
    catalog = EvidenceCatalog(index)

    assert_error("evidence_not_found", lambda: catalog.resolve("1"))
    assert_error("evidence_not_found", lambda: catalog.resolve(" raw-1 "))
    assert_error("evidence_id_required", lambda: catalog.resolve(""))

    rowid_only = tmp_path / "rowid-only.jsonl"
    write_records(
        rowid_only,
        [
            {
                "rowid": 1,
                "source": "codex",
                "timestamp": "2026-07-19T00:00:00+00:00",
                "content": "不能作为稳定事实",
            }
        ],
    )
    assert_error("missing_evidence_id", lambda: EvidenceCatalog(rowid_only))


def test_missing_legacy_raw_id_is_deterministic_and_never_available(tmp_path):
    catalog = EvidenceCatalog(tmp_path / "missing-index.jsonl")
    arguments = {
        "source": "feishu-im",
        "raw_id": None,
        "timestamp": "2026-07-01T00:00:00Z",
        "statement": "历史候选",
    }

    first = catalog.from_legacy(**arguments)
    second = catalog.from_legacy(**arguments)

    assert first == second
    assert first["evidence_id"].startswith("ev_legacy_")
    assert first["raw_id"] is None
    assert first["source"] == "feishu"
    assert first["status"] == "source_broken"
    assert first["status"] != "available"
    assert "statement" not in first


def test_explicitly_deleted_legacy_source_stays_source_deleted(tmp_path):
    catalog = EvidenceCatalog(tmp_path / "missing-index.jsonl")

    ref = catalog.from_legacy(
        source="custom-archive",
        raw_id=None,
        timestamp="2026-07-01T00:00:00Z",
        statement="历史候选",
        source_deleted=True,
    )

    assert ref["status"] == "source_deleted"
    assert ref["source"] == "custom"
    assert ref["source_detail"] == "custom-archive"


def test_duplicate_logical_content_keeps_distinct_fact_ids(tmp_path):
    index = tmp_path / "index.jsonl"
    write_records(
        index,
        [
            record("raw-1", "同一句", source="codex"),
            record("raw-2", "同一句", source="claude-code-conversation"),
        ],
    )
    catalog = EvidenceCatalog(index)

    first = catalog.resolve("raw-1")
    second = catalog.resolve("raw-2")

    assert first["evidence_id"] != second["evidence_id"]
    assert first["content_hash"] == second["content_hash"]


@pytest.mark.parametrize(
    ("raw_source", "normalized"),
    [
        ("codex-conversation", "codex"),
        ("codex-memory", "codex"),
        ("claude-code-conversation", "claude"),
        ("feishu-im", "feishu"),
        ("web-page", "web"),
        ("local", "local"),
    ],
)
def test_real_source_names_are_normalized(tmp_path, raw_source, normalized):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实", source=raw_source)])

    ref = EvidenceCatalog(index).resolve("raw-1")

    assert ref["source"] == normalized
    assert "source_detail" not in ref


def test_unknown_source_uses_custom_with_safe_source_detail(tmp_path):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实", source="private-provider")])

    ref = EvidenceCatalog(index).resolve("raw-1")

    assert ref["source"] == "custom"
    assert ref["source_detail"] == "private-provider"


def test_long_raw_body_is_hashed_but_never_retained_as_metadata(tmp_path):
    index = tmp_path / "index.jsonl"
    body = "长正文" * 1_000
    write_records(index, [record("raw-1", body)])

    catalog = EvidenceCatalog(index)
    ref = catalog.resolve("raw-1")

    assert ref["content_hash"].startswith("sha256:")
    assert "content" not in ref
    assert body not in json.dumps(catalog.list(), ensure_ascii=False)


def test_malformed_middle_record_and_duplicate_id_fail_closed(tmp_path):
    malformed = tmp_path / "malformed.jsonl"
    malformed.write_text(
        json.dumps(record("raw-1", "第一条"), ensure_ascii=False)
        + "\n"
        + "{broken\n"
        + json.dumps(record("raw-2", "第二条"), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    assert_error("malformed_jsonl", lambda: EvidenceCatalog(malformed))

    duplicate = tmp_path / "duplicate.jsonl"
    write_records(
        duplicate,
        [record("raw-1", "第一条"), record("raw-1", "另一条")],
    )
    assert_error("duplicate_evidence_id", lambda: EvidenceCatalog(duplicate))


def test_bounded_scan_rejects_record_and_line_limit_overflow(tmp_path):
    records = tmp_path / "records.jsonl"
    write_records(
        records,
        [record("raw-1", "第一条"), record("raw-2", "第二条")],
    )
    assert_error(
        "catalog_limit_exceeded",
        lambda: EvidenceCatalog(records, max_records=1),
    )

    long_line = tmp_path / "long-line.jsonl"
    write_records(long_line, [record("raw-1", "x" * 200)])
    assert_error(
        "catalog_line_too_large",
        lambda: EvidenceCatalog(long_line, max_line_bytes=64),
    )


def test_source_change_after_catalog_build_fails_closed(tmp_path):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实")])
    catalog = EvidenceCatalog(index)
    write_records(
        index,
        [record("raw-1", "被改写"), record("raw-2", "新增")],
    )

    assert_error("source_changed", lambda: catalog.resolve("raw-1"))


def test_confirmed_record_becomes_explicit_source_deleted_if_file_disappears(
    tmp_path,
):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实")])
    catalog = EvidenceCatalog(index)
    index.unlink()

    ref = catalog.resolve("raw-1")

    assert ref["status"] == "source_deleted"
    assert ref["evidence_id"] == "raw-1"
    assert ref["content_hash"].startswith("sha256:")
