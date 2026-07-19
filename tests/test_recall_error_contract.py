import argparse
import json
import sys

import immortal
import search


def recall_args(*, json_output: bool = True) -> argparse.Namespace:
    return argparse.Namespace(
        query="needle",
        source=None,
        since=None,
        json=json_output,
    )


def test_search_cli_reports_index_unavailable_as_operational_error(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        search,
        "unified_search",
        lambda *_args, **_kwargs: ("index_unavailable", []),
    )
    monkeypatch.setattr(sys, "argv", ["search.py", "needle"])

    code = search.main()

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "搜索索引不可用" in captured.err
    assert "index_db.py sync" in captured.err


def test_search_cli_keeps_normal_zero_hits_successful(monkeypatch, capsys):
    monkeypatch.setattr(
        search,
        "unified_search",
        lambda *_args, **_kwargs: ("none", []),
    )
    monkeypatch.setattr(sys, "argv", ["search.py", "needle"])

    code = search.main()

    captured = capsys.readouterr()
    assert code == 0
    assert "未找到与 needle 相关的记录" in captured.out
    assert captured.err == ""


def test_recall_json_reports_index_unavailable_as_structured_error(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        search,
        "unified_search",
        lambda *_args, **_kwargs: ("index_unavailable", []),
    )

    code = immortal._recall_json(recall_args())

    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["ok"] is False
    assert payload["engine"] == "unavailable"
    assert payload["hits"] == []
    assert payload["error"] == {
        "code": "index_unavailable",
        "message": search.INDEX_UNAVAILABLE_MESSAGE,
        "retryable": True,
    }


def test_recall_json_keeps_normal_zero_hits_successful(monkeypatch, capsys):
    monkeypatch.setattr(
        search,
        "unified_search",
        lambda *_args, **_kwargs: ("none", []),
    )

    code = immortal._recall_json(recall_args())

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["hits"] == []
    assert "error" not in payload
