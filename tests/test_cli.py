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


def test_directory_argument_constructs_multirun_with_glob(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m
    from runstate_tui.resolver import ref_from_path

    made = {}

    def fake_run(self):
        made["multi"] = self
        made["refs"] = self._resolver(0.0)  # prove main() built the glob resolver

    monkeypatch.setattr(m.MultiRunApp, "run", fake_run)
    (tmp_path / "exp1").mkdir()
    (tmp_path / "a.db").write_text("")
    (tmp_path / "exp1" / "trial.db").write_text("")
    m.main([str(tmp_path)])
    assert "multi" in made
    assert set(made["refs"]) == {
        ref_from_path(str(tmp_path / "a.db")),
        ref_from_path(str(tmp_path / "exp1" / "trial.db")),
    }
    assert made["multi"]._empty_hint is not None  # glob mode wires a placeholder hint


def test_single_db_file_still_constructs_single(monkeypatch, tmp_path):
    # A single .db FILE (not a dir) still routes to SingleRunApp -- the is_dir() branch
    # must not swallow the single-file case.
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    f = tmp_path / "a.db"
    f.write_text("")
    m.main([str(f)])
    assert "single" in made
