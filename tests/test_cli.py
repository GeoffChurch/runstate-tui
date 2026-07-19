from runstate_tui.__main__ import main


def test_no_argument_prints_usage_and_returns_2(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_two_paths_construct_multirun(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m
    from runstate_tui.resolver import ref_from_path

    made = {}

    def fake_run(self):
        made["multi"] = self
        made["refs"] = self._resolver(0.0)  # prove main() built the resolver correctly

    monkeypatch.setattr(m.MultiRunApp, "run", fake_run)
    a = str(tmp_path / "a.db")
    b = str(tmp_path / "b.db")
    m.main([a, b])
    assert "multi" in made
    assert made["refs"] == [ref_from_path(a), ref_from_path(b)]


def test_one_path_still_constructs_single(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    m.main([str(tmp_path / "a.db")])
    assert "single" in made


def test_no_args_is_usage_error():
    import runstate_tui.__main__ as m

    assert m.main([]) == 2
