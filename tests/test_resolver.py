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
