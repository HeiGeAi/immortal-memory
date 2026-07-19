from __future__ import annotations

import ast
import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import index_integrity


CORE_DIR = Path(__file__).resolve().parent.parent / "core"
PUBLIC_WRITERS = {
    "collect.py": "write_records",
    "feishu_collect.py": "write_records",
    "web_capture.py": "append_records",
    "immortal.py": "command_train",
}


def _record(rec_id: str) -> dict[str, str]:
    return {
        "id": rec_id,
        "timestamp": "2026-07-19T12:00:00+08:00",
        "source": "writer-contract",
        "role": "user",
        "project": "immortal",
        "content": f"record {rec_id}",
    }


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _is_authoritative_index_path(node: ast.AST) -> bool:
    return any(
        (
            isinstance(child, ast.Name)
            and child.id == "INDEX_FILE"
        )
        or (
            isinstance(child, ast.Constant)
            and isinstance(child.value, str)
            and (
                child.value == "index"
                or child.value.endswith("index.jsonl")
            )
        )
        for child in ast.walk(node)
    )


def _direct_append_path(call: ast.Call) -> ast.AST | None:
    if isinstance(call.func, ast.Name) and call.func.id == "open":
        path = call.args[0] if call.args else None
        mode = call.args[1] if len(call.args) > 1 else None
    elif isinstance(call.func, ast.Attribute) and call.func.attr == "open":
        path = call.func.value
        mode = call.args[0] if call.args else None
    else:
        return None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    if (
        path is not None
        and isinstance(mode, ast.Constant)
        and isinstance(mode.value, str)
        and mode.value.startswith("a")
    ):
        return path
    return None


def _append_in_process(source: str, prefix: str, count: int, start) -> None:
    sys.path.insert(0, str(CORE_DIR))
    from index_writer import append_jsonl_records

    start.wait(timeout=10)
    for number in range(count):
        append_jsonl_records(
            Path(source),
            [_record(f"{prefix}-{number}")],
        )


def _reconcile_in_process(source: str, database: str, rounds: int, start, errors) -> None:
    sys.path.insert(0, str(CORE_DIR))
    import index_integrity as process_integrity

    start.wait(timeout=10)
    try:
        for _ in range(rounds):
            process_integrity.reconcile_index(
                Path(source),
                Path(database),
                force_rebuild=True,
            )
    except Exception as exc:
        errors.put(repr(exc))


def test_public_index_writers_delegate_to_one_durable_helper() -> None:
    for filename, function_name in PUBLIC_WRITERS.items():
        tree = ast.parse((CORE_DIR / filename).read_text(encoding="utf-8"))
        imports_helper = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "index_writer"
            and any(alias.name == "append_jsonl_records" for alias in node.names)
            for node in tree.body
        )
        calls_helper = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "append_jsonl_records"
            for node in ast.walk(_function(tree, function_name))
        )
        assert imports_helper, filename
        assert calls_helper, f"{filename}:{function_name}"


def test_public_index_writers_have_no_legacy_direct_append_pattern() -> None:
    result = subprocess.run(
        [
            "rg",
            "-U",
            "-n",
            (
                r"source_lock\(\s*INDEX_FILE|open\(INDEX_FILE,\s*[\"']a[\"']|"
                r"paths\[\"index\"\]\.open\([\"']a[\"']|"
                r"\(\s*vault\s*/\s*[\"']index\.jsonl[\"']\s*\)\.open\([\"']a[\"']"
            ),
            "--glob",
            "*.py",
            str(CORE_DIR),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stdout


def test_core_has_no_ast_visible_direct_authoritative_index_append() -> None:
    violations = []
    for path in sorted(CORE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in (
            node for node in ast.walk(tree) if isinstance(node, ast.Call)
        ):
            target = _direct_append_path(call)
            if target is not None and _is_authoritative_index_path(target):
                violations.append(f"{path.name}:{call.lineno}")
    assert violations == []


def test_durable_append_retries_short_writes_and_fsyncs(tmp_path, monkeypatch) -> None:
    from index_writer import append_jsonl_records

    source = tmp_path / "index.jsonl"
    real_write = os.write
    writes: list[int] = []
    fsyncs: list[int] = []

    def short_write(fd: int, data) -> int:
        chunk = bytes(data[: max(1, len(data) // 2)])
        written = real_write(fd, chunk)
        writes.append(written)
        return written

    monkeypatch.setattr(os, "write", short_write)
    monkeypatch.setattr(os, "fsync", lambda fd: fsyncs.append(fd))

    assert append_jsonl_records(source, [_record("one"), _record("two")]) == 2

    rows = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
    ]
    assert [row["id"] for row in rows] == ["one", "two"]
    assert len(writes) > 1
    assert len(fsyncs) >= 1


def test_writer_and_reconcile_processes_preserve_exact_parity(tmp_path) -> None:
    from index_writer import append_jsonl_records

    source = tmp_path / "index.jsonl"
    database = tmp_path / "search_index.db"
    append_jsonl_records(source, [_record("seed")])
    index_integrity.reconcile_index(source, database)

    context = multiprocessing.get_context("spawn")
    start = context.Event()
    errors = context.Queue()
    processes = [
        context.Process(
            target=_append_in_process,
            args=(str(source), "a", 30, start),
        ),
        context.Process(
            target=_append_in_process,
            args=(str(source), "b", 30, start),
        ),
        context.Process(
            target=_reconcile_in_process,
            args=(str(source), str(database), 4, start, errors),
        ),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert errors.empty()

    source_rows = [
        json.loads(line)
        for line in source.read_text(encoding="utf-8").splitlines()
    ]
    source_ids = {row["id"] for row in source_rows}
    assert len(source_rows) == 61
    assert len(source_ids) == 61

    result = index_integrity.reconcile_index(source, database)
    assert result["missing_in_sqlite"] == []
    assert result["missing_in_jsonl"] == []
    with sqlite3.connect(database) as connection:
        database_ids = {
            str(row[0])
            for row in connection.execute("SELECT rec_id FROM docs")
        }
    assert database_ids == source_ids
