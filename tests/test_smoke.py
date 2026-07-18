def test_runstate_and_package_import():
    import runstate

    assert hasattr(runstate, "open_channel")
