def test_runstate_and_package_import():
    import runstate
    import runstate_tui
    assert hasattr(runstate, "open_channel")
