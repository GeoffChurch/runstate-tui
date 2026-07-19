from runstate_tui.__main__ import main


def test_no_argument_prints_usage_and_returns_2(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_two_paths_construct_multirun(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    m.main([str(tmp_path / "a.db"), str(tmp_path / "b.db")])
    assert "multi" in made


def test_one_path_still_constructs_single(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    m.main([str(tmp_path / "a.db")])
    assert "single" in made


def test_no_args_is_usage_error():
    import runstate_tui.__main__ as m

    assert m.main([]) == 2
