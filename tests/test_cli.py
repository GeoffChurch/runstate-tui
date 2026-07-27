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
    assert made["refs"] == [(ref_from_path(a), {}), (ref_from_path(b), {})]


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
    assert {r for r, _attrs in made["refs"]} == {
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


def test_json_manifest_constructs_multirun_with_group_by(monkeypatch, tmp_path):
    import json

    import runstate_tui.__main__ as m

    made = {}

    def fake_run(self):
        made["multi"] = self
        made["items"] = self._resolver(0.0)  # prove main() wired the manifest resolver

    monkeypatch.setattr(m.MultiRunApp, "run", fake_run)
    manifest = tmp_path / "exp.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "run_id": "r1",
                    "root": "/x",
                    "backend": "sqlite",
                    "attrs": {"scenario": "s", "variant": "v"},
                }
            ]
        )
    )
    m.main([str(manifest), "--group-by", "scenario"])
    assert made["multi"]._group_by == "scenario"
    assert made["items"] == [(("r1", "/x", "sqlite"), {"scenario": "s", "variant": "v"})]
    assert made["multi"]._empty_hint is not None  # manifest mode wires a placeholder hint


def test_json_manifest_without_group_by_is_flat(monkeypatch, tmp_path):
    import json

    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    manifest = tmp_path / "exp.json"
    manifest.write_text(json.dumps([]))
    m.main([str(manifest)])
    assert made["multi"]._group_by is None


def test_missing_json_manifest_is_a_usage_error(monkeypatch, tmp_path):
    # spec §5: a missing manifest path is a usage error (refuse to start), NOT a phantom
    # SingleRunApp over a nonexistent run. A .json suffix routes to the manifest branch
    # regardless of existence; a nonexistent one returns 2 and constructs no app.
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    rc = m.main([str(tmp_path / "nope.json")])
    assert rc == 2
    assert made == {}  # neither app constructed nor run


def test_group_by_equals_form_is_recognized(monkeypatch, tmp_path):
    import json

    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    manifest = tmp_path / "exp.json"
    manifest.write_text(json.dumps([]))
    m.main([str(manifest), "--group-by=scenario"])  # equals form, not two tokens
    assert made["multi"]._group_by == "scenario"


def test_unknown_flag_is_a_usage_error(monkeypatch, tmp_path):
    import json

    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    manifest = tmp_path / "exp.json"
    manifest.write_text(json.dumps([]))
    rc = m.main([str(manifest), "--bogus"])  # unknown flag must NOT leak as a phantom run path
    assert rc == 2
    assert made == {}


def test_group_by_on_single_db_is_a_usage_error(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    f = tmp_path / "a.db"
    f.write_text("")
    rc = m.main([str(f), "--group-by", "scenario"])  # grouping is meaningless for a single run
    assert rc == 2
    assert made == {}  # not silently dropped -> a usage error, no app


def test_duplicate_group_by_takes_last(monkeypatch, tmp_path):
    import json

    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    manifest = tmp_path / "exp.json"
    manifest.write_text(json.dumps([]))
    m.main([str(manifest), "--group-by", "a", "--group-by", "b"])  # no leak; last wins
    assert made["multi"]._group_by == "b"


def _empty_manifest(tmp_path):
    import json

    manifest = tmp_path / "exp.json"
    manifest.write_text(json.dumps([]))
    return str(manifest)


def test_objective_defaults_to_the_env_default(monkeypatch, tmp_path):
    # Asserted against Env's own default, not a literal, so the CLI can never drift from it.
    import runstate_tui.__main__ as m
    from runstate_tui.env import Env

    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    f = tmp_path / "a.db"
    f.write_text("")
    m.main([str(f)])
    assert made["single"]._env.objective == Env.objective
    assert made["single"]._env.stuck_threshold == Env.stuck_threshold  # not a CLI knob


def test_objective_threads_into_a_multirun_env(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    m.main([_empty_manifest(tmp_path), "--objective", "mean_p1"])
    assert made["multi"]._env.objective == "mean_p1"


def test_objective_threads_into_a_single_run_env(monkeypatch, tmp_path):
    # Unlike --group-by, this applies to EVERY target shape: it parameterizes the fold, not the
    # aggregation, and the single-run view is the same fold at the singleton resolver.
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    f = tmp_path / "a.db"
    f.write_text("")
    m.main([str(f), "--objective", "loss"])
    assert made["single"]._env.objective == "loss"


def test_objective_accepts_any_name(monkeypatch, tmp_path):
    # Whether the name exists is a RUNTIME fact (an absent value folds to a blank cell), so the
    # CLI must not pre-validate it against anything.
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    m.main([_empty_manifest(tmp_path), "--objective=no_such_metric"])  # equals form too
    assert made["multi"]._env.objective == "no_such_metric"
