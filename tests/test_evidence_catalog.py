import json
import sqlite3

import pytest

import evidence_catalog
from evidence_catalog import EvidenceCatalog, EvidenceCatalogError
from index_integrity import reconcile_index
from model_types import validate_evidence_ref


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

    expected = {
        "evidence_id": "raw-1",
        "source": "codex",
        "raw_id": "raw-1",
        "content_hash": ref["content_hash"],
        "status": "available",
        "observed_at": "2026-07-19T00:00:00+00:00",
        "privacy": "restricted",
    }
    assert {
        key: value for key, value in ref.items() if key != "source_detail"
    } == expected
    assert ref.get("source_detail") in {None, "codex-conversation"}
    assert ref["content_hash"].startswith("sha256:")
    assert len(ref["content_hash"]) == 71
    assert "rowid" not in ref
    assert "content" not in ref
    assert "事实正文" not in json.dumps(catalog.list(), ensure_ascii=False)
    validate_evidence_ref(ref)


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
    catalog = EvidenceCatalog(rowid_only)
    assert_error("missing_evidence_id", catalog.list)


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
    assert first.get("source_detail") in {None, "feishu-im"}
    assert first["status"] == "source_broken"
    assert first["status"] != "available"
    assert "statement" not in first


def test_missing_source_with_unresolved_legacy_id_stays_source_broken(tmp_path):
    catalog = EvidenceCatalog(tmp_path / "missing-index.jsonl")

    ref = catalog.from_legacy(
        source="feishu-im",
        raw_id="missing",
        timestamp="2026-07-01T00:00:00Z",
        statement="历史候选",
    )

    assert ref["raw_id"] == "missing"
    assert ref["evidence_id"].startswith("ev_legacy_")
    assert ref["status"] == "source_broken"
    assert ref["status"] != "available"


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
    validate_evidence_ref(ref)


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
        ("obsidian-note", "local"),
        ("desktop-output", "local"),
        ("hermes-conversation", "custom"),
    ],
)
def test_real_source_names_are_normalized(tmp_path, raw_source, normalized):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实", source=raw_source)])

    ref = EvidenceCatalog(index).resolve("raw-1")

    assert ref["source"] == normalized
    if normalized == "custom":
        assert ref["source_detail"] == raw_source
    else:
        assert ref.get("source_detail") in {None, raw_source}
    validate_evidence_ref(ref)


def test_unknown_source_uses_custom_with_safe_source_detail(tmp_path):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "事实", source="private-provider")])

    ref = EvidenceCatalog(index).resolve("raw-1")

    assert ref["source"] == "custom"
    assert ref["source_detail"] == "private-provider"
    validate_evidence_ref(ref)


def test_long_raw_body_is_hashed_but_never_retained_as_metadata(tmp_path):
    index = tmp_path / "index.jsonl"
    body = "长正文" * 1_000
    write_records(index, [record("raw-1", body)])

    catalog = EvidenceCatalog(index)
    ref = catalog.resolve("raw-1")

    assert ref["content_hash"].startswith("sha256:")
    assert "content" not in ref
    assert body not in json.dumps(catalog.list(), ensure_ascii=False)


def test_constructor_is_lazy_and_verified_sqlite_handles_source_above_fallback_limits(
    tmp_path,
):
    index = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    rows = [
        record(f"raw-{number}", "x" * 200)
        for number in range(30)
    ]
    write_records(index, rows)
    reconcile_index(index, database)

    catalog = EvidenceCatalog(
        index,
        database_path=database,
        max_fallback_bytes=64,
        max_fallback_records=1,
        max_scan_bytes=index.stat().st_size + 1,
        max_records=100,
    )
    preflight = catalog.preflight()
    ref = catalog.resolve("raw-29")

    assert preflight["mode"] == "verified_sqlite"
    assert preflight["source_size"] > 64
    assert preflight["indexed_id_count"] == 30
    assert not hasattr(catalog, "_entries")
    assert ref["evidence_id"] == "raw-29"
    assert "rowid" not in ref
    validate_evidence_ref(ref)
    assert_error("catalog_limit_exceeded", catalog.list)


def test_verified_database_miss_avoids_jsonl_scan(tmp_path, monkeypatch):
    index = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(index, [record("raw-1", "事实")])
    reconcile_index(index, database)
    catalog = EvidenceCatalog(index, database_path=database)

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("verified database miss must not scan JSONL")

    monkeypatch.setattr(catalog, "_scan_for_id", fail_scan)

    assert_error("evidence_not_found", lambda: catalog.resolve("missing"))


def test_verified_database_hit_uses_bound_snapshot_without_jsonl_rescan(
    tmp_path,
    monkeypatch,
):
    index = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(index, [record("raw-1", "事实")])
    reconcile_index(index, database)
    catalog = EvidenceCatalog(index, database_path=database)

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("single resolve must use the verified snapshot")

    monkeypatch.setattr(catalog, "_scan_for_id", fail_scan)

    ref = catalog.resolve("raw-1")

    assert ref["evidence_id"] == "raw-1"
    assert ref["content_hash"].startswith("sha256:")
    validate_evidence_ref(ref)


def test_resolve_many_confirms_candidates_in_one_authoritative_jsonl_scan(
    tmp_path,
):
    index = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(
        index,
        [
            record("raw-1", "第一条"),
            record("raw-2", "第二条"),
            record("raw-3", "第三条"),
        ],
    )
    reconcile_index(index, database)
    catalog = EvidenceCatalog(index, database_path=database)

    refs = catalog.resolve_many(["raw-3", "raw-1"])

    assert [ref["evidence_id"] for ref in refs] == ["raw-3", "raw-1"]
    assert all("rowid" not in ref for ref in refs)
    assert all(ref["status"] == "available" for ref in refs)


def test_database_meta_must_match_current_jsonl_revision(tmp_path):
    index = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(index, [record("raw-1", "事实")])
    reconcile_index(index, database)
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE meta SET value='0' WHERE key='source_mtime_ns'"
        )
        connection.commit()

    assert_error(
        "database_stale",
        lambda: EvidenceCatalog(index, database_path=database),
    )


def test_source_change_after_database_build_fails_closed(tmp_path):
    index = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(index, [record("raw-1", "事实")])
    reconcile_index(index, database)
    write_records(
        index,
        [record("raw-1", "被改写"), record("raw-2", "新增")],
    )

    assert_error(
        "database_stale",
        lambda: EvidenceCatalog(index, database_path=database),
    )


def test_sqlite_candidate_is_verified_against_authoritative_jsonl(tmp_path):
    index = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    write_records(index, [record("raw-1", "权威正文")])
    reconcile_index(index, database)
    with sqlite3.connect(str(database)) as connection:
        connection.execute(
            "UPDATE docs SET content='伪造正文' WHERE rec_id='raw-1'"
        )
        connection.commit()

    assert_error(
        "database_source_mismatch",
        lambda: EvidenceCatalog(
            index,
            database_path=database,
        ).resolve_many(["raw-1"]),
    )


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
    assert_error("malformed_jsonl", EvidenceCatalog(malformed).list)

    duplicate = tmp_path / "duplicate.jsonl"
    write_records(
        duplicate,
        [record("raw-1", "第一条"), record("raw-1", "另一条")],
    )
    assert_error("duplicate_evidence_id", EvidenceCatalog(duplicate).list)


def test_no_database_fallback_is_lazy_but_bounded_on_resolve(tmp_path):
    records = tmp_path / "records.jsonl"
    write_records(
        records,
        [record("raw-1", "第一条"), record("raw-2", "第二条")],
    )
    catalog = EvidenceCatalog(
        records,
        max_fallback_records=1,
    )

    assert_error(
        "catalog_limit_exceeded",
        lambda: catalog.resolve("raw-2"),
    )

    long_line = tmp_path / "long-line.jsonl"
    write_records(long_line, [record("raw-1", "x" * 200)])
    catalog = EvidenceCatalog(long_line, max_line_bytes=64)
    assert_error(
        "catalog_line_too_large",
        lambda: catalog.resolve("raw-1"),
    )


def test_file_growth_crossing_runtime_byte_budget_stops_immediately(
    tmp_path,
    monkeypatch,
):
    index = tmp_path / "index.jsonl"
    write_records(index, [record("raw-1", "第一条")])
    initial_size = index.stat().st_size
    catalog = EvidenceCatalog(
        index,
        max_scan_bytes=initial_size + 40,
    )
    real_readline = evidence_catalog._readline
    appended = {"done": False}

    def append_after_first_read(handle, limit):
        raw = real_readline(handle, limit)
        if raw and not appended["done"]:
            appended["done"] = True
            with index.open("a", encoding="utf-8") as writer:
                writer.write(
                    json.dumps(record("raw-2", "x" * 200), ensure_ascii=False)
                    + "\n"
                )
        return raw

    monkeypatch.setattr(evidence_catalog, "_readline", append_after_first_read)

    assert_error(
        "catalog_limit_exceeded",
        lambda: catalog.resolve("missing"),
    )


def test_source_and_parent_symlinks_fail_closed(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    index = real_dir / "index.jsonl"
    write_records(index, [record("raw-1", "事实")])

    source_link = tmp_path / "source-link.jsonl"
    source_link.symlink_to(index)
    assert_error("unsafe_path", lambda: EvidenceCatalog(source_link))

    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_dir, target_is_directory=True)
    assert_error(
        "unsafe_path",
        lambda: EvidenceCatalog(parent_link / "index.jsonl"),
    )


def test_database_symlink_fails_closed(tmp_path):
    index = tmp_path / "index.jsonl"
    real_database = tmp_path / "real-search.db"
    database_link = tmp_path / "search_index.db"
    write_records(index, [record("raw-1", "事实")])
    reconcile_index(index, real_database)
    database_link.symlink_to(real_database)

    assert_error(
        "unsafe_path",
        lambda: EvidenceCatalog(index, database_path=database_link),
    )


def test_database_replacement_during_read_window_fails_closed(tmp_path):
    database = tmp_path / "search_index.db"
    moved = tmp_path / "moved.db"
    with sqlite3.connect(str(database)) as connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.commit()

    with pytest.raises(EvidenceCatalogError) as captured:
        with evidence_catalog._readonly_database(database):
            database.rename(moved)
            with sqlite3.connect(str(database)) as replacement:
                replacement.execute("CREATE TABLE replacement(value TEXT)")
                replacement.commit()

    assert captured.value.code == "unsafe_path"


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
    catalog.resolve("raw-1")
    index.unlink()

    ref = catalog.resolve("raw-1")

    assert ref["status"] == "source_deleted"
    assert ref["evidence_id"] == "raw-1"
    assert ref["content_hash"].startswith("sha256:")
