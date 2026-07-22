from runstate_tui.resolver import const_resolver, ref_from_path


def test_const_resolver_yields_the_single_ref_regardless_of_now():
    ref = ("run-1", "/tmp/runs", "sqlite")
    resolve = const_resolver(ref)
    assert resolve(0.0) == [ref]
    assert resolve(9999.0) == [ref]


def test_ref_from_path_splits_a_sqlite_db_path():
    assert ref_from_path("/tmp/runs/lattice-b6.1.db") == ("lattice-b6.1", "/tmp/runs", "sqlite")
    assert ref_from_path("run.db") == ("run", ".", "sqlite")


def test_explicit_resolver_returns_the_fixed_list_regardless_of_now():
    from runstate_tui.resolver import explicit_resolver

    refs = [("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    resolve = explicit_resolver(refs)
    assert resolve(0.0) == refs and resolve(9999.0) == refs


def test_explicit_resolver_dedupes_exact_duplicates_preserving_order():
    from runstate_tui.resolver import explicit_resolver

    refs = [("a", "/root", "sqlite"), ("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    assert explicit_resolver(refs)(0.0) == [("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    # Order-discriminating case: the dedup'd order does NOT coincide with alphabetical, so
    # a sorted(set(...)) mis-implementation (which would yield a before b) fails here.
    reordered = [("b", "/r", "sqlite"), ("b", "/r", "sqlite"), ("a", "/r", "sqlite")]
    assert explicit_resolver(reordered)(0.0) == [("b", "/r", "sqlite"), ("a", "/r", "sqlite")]


def test_ref_key_distinguishes_same_basename_across_roots():
    from runstate_tui.resolver import ref_key

    a = ("run1", "/a", "sqlite")
    b = ("run1", "/b", "sqlite")  # same run_id (Path.stem), different root
    assert ref_key(a) != ref_key(b)
    assert ref_key(a) == ref_key(("run1", "/a", "sqlite"))  # stable


def test_glob_resolver_discovers_nested_db_files(tmp_path):
    from runstate_tui.resolver import glob_resolver, ref_from_path

    (tmp_path / "exp1").mkdir()
    (tmp_path / "a.db").write_text("")
    (tmp_path / "exp1" / "trial.db").write_text("")
    refs = glob_resolver(str(tmp_path))(0.0)
    assert set(refs) == {
        ref_from_path(str(tmp_path / "a.db")),
        ref_from_path(str(tmp_path / "exp1" / "trial.db")),
    }


def test_glob_resolver_is_live_reflecting_new_files(tmp_path):
    from runstate_tui.resolver import glob_resolver

    resolve = glob_resolver(str(tmp_path))
    assert resolve(0.0) == []
    (tmp_path / "new.db").write_text("")
    assert [r[0] for r in resolve(1.0)] == ["new"]


def test_glob_resolver_dedupes_matches(tmp_path):
    # rglob won't emit a path twice today, but the resolver contract is a deduped IndexSet;
    # pin it so a future change can't leak a duplicate RunRef -> a duplicate DataTable row.
    from runstate_tui.resolver import glob_resolver

    (tmp_path / "a.db").write_text("")
    refs = glob_resolver(str(tmp_path))(0.0)
    assert len(refs) == len(set(refs))


def test_glob_resolver_is_symlink_cycle_safe(tmp_path):
    import os

    from runstate_tui.resolver import glob_resolver

    (tmp_path / "sub").mkdir()
    (tmp_path / "a.db").write_text("")
    (tmp_path / "sub" / "b.db").write_text("")
    os.symlink(tmp_path, tmp_path / "sub" / "loop")  # a DIRECTORY cycle
    # Must RETURN (not hang) and NOT explode into sub/loop/sub/loop/... entries:
    # pathlib.rglob does not recurse into symlinked directories.
    refs = glob_resolver(str(tmp_path))(0.0)
    assert sorted(r[0] for r in refs) == ["a", "b"]


def test_disambiguate_is_a_noop_when_stems_are_unique():
    from runstate_tui.resolver import disambiguate, ref_key

    refs = [("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    labels = disambiguate(refs)
    assert labels[ref_key(refs[0])] == "a"
    assert labels[ref_key(refs[1])] == "b"


def test_disambiguate_grows_only_the_colliding_group():
    # 99 unique stems + one colliding pair: ONLY the pair grows a parent level; the rest
    # stay bare (ragged-minimal, not uniform-depth).
    from runstate_tui.resolver import disambiguate, ref_key

    refs = [(f"run{i:03d}", "/runs/g1", "sqlite") for i in range(1, 100)]
    refs += [("run000", "/runs/g1", "sqlite"), ("run000", "/runs/g2", "sqlite")]
    labels = disambiguate(refs)
    assert labels[ref_key(("run050", "/runs/g1", "sqlite"))] == "run050"  # untouched
    assert labels[ref_key(("run000", "/runs/g1", "sqlite"))] == "g1/run000"
    assert labels[ref_key(("run000", "/runs/g2", "sqlite"))] == "g2/run000"


def test_disambiguate_backtracks_deeper_when_the_parent_also_collides():
    from runstate_tui.resolver import disambiguate, ref_key

    a = ("trial", "/runs/a/g", "sqlite")
    b = ("trial", "/runs/b/g", "sqlite")  # same stem AND same parent dir name "g"
    labels = disambiguate([a, b])
    assert labels[ref_key(a)] == "a/g/trial"
    assert labels[ref_key(b)] == "b/g/trial"


def test_disambiguate_terminates_on_suffix_overlap():
    # One run's full path is a suffix of the other's -> the shorter maxes out while the
    # longer keeps growing; must terminate and disambiguate, not loop forever.
    from runstate_tui.resolver import disambiguate, ref_key

    short = ("trial", "/x", "sqlite")  # parts end (..., "x", "trial")
    long_ = ("trial", "/y/x", "sqlite")  # parts end (..., "y", "x", "trial")
    labels = disambiguate([short, long_])
    assert labels[ref_key(short)] != labels[ref_key(long_)]


def test_glob_resolver_matches_symlinked_file_but_not_symlinked_dir(tmp_path):
    # Pins the load-bearing pathlib guarantee (spec symlink table): rglob MATCHES a
    # symlinked *file* (the common `latest.db -> run.db` pattern) but does NOT recurse into
    # a symlinked *directory* (the documented fail-safe gap). A regression to
    # glob.glob(recursive=True) would flip the second assert (and explode on cycles).
    import os

    from runstate_tui.resolver import glob_resolver

    ext_file = tmp_path / "store" / "run_ext.db"
    ext_file.parent.mkdir()
    ext_file.write_text("")
    ext_dir = tmp_path / "ext"
    ext_dir.mkdir()
    (ext_dir / "inside.db").write_text("")

    root = tmp_path / "runs"
    root.mkdir()
    (root / "local.db").write_text("")
    os.symlink(ext_file, root / "latest.db")  # symlinked FILE -> matched
    os.symlink(ext_dir, root / "linked")  # symlinked DIR  -> NOT recursed

    run_ids = sorted(r[0] for r in glob_resolver(str(root))(0.0))
    assert "local" in run_ids
    assert "latest" in run_ids  # symlinked file IS found
    assert "inside" not in run_ids  # run inside a symlinked dir is NOT (fail-safe gap)
