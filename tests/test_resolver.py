from runstate_tui.resolver import const_resolver


def test_const_resolver_yields_the_single_ref_regardless_of_now():
    ref = ("run-1", "/tmp/runs", "sqlite")
    resolve = const_resolver(ref)
    assert resolve(0.0) == [ref]
    assert resolve(9999.0) == [ref]
