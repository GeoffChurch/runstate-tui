from runstate_tui.resolver import const_resolver, ref_from_path


def test_const_resolver_yields_the_single_ref_regardless_of_now():
    ref = ("run-1", "/tmp/runs", "sqlite")
    resolve = const_resolver(ref)
    assert resolve(0.0) == [ref]
    assert resolve(9999.0) == [ref]


def test_ref_from_path_splits_a_sqlite_db_path():
    assert ref_from_path("/tmp/runs/lattice-b6.1.db") == ("lattice-b6.1", "/tmp/runs", "sqlite")
    assert ref_from_path("run.db") == ("run", ".", "sqlite")
