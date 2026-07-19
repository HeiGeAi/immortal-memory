import orchestrator


def test_search_index_sync_failure_stays_an_explicit_boolean_stage_boundary(
    monkeypatch,
):
    monkeypatch.setattr(
        orchestrator,
        "run_script",
        lambda *_args, **_kwargs: (False, "forced index failure"),
    )
    messages = []
    monkeypatch.setattr(orchestrator, "log", messages.append)

    assert orchestrator.search_index_sync() is False
    assert any("控制中心检索索引同步失败" in message for message in messages)
