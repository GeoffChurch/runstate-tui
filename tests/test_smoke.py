def test_runstate_and_package_import():
    import runstate

    # the locator split (docs/specs/channel-locators.md): the creating `open_channel`
    # and the worker factory `attach` are GONE; the new surface is attach_channel
    # (existing-only, non-mutating) / create_channel (birth) / current_channel (worker
    # env factory) + the RunNotFound absence signal.
    for name in ("attach_channel", "create_channel", "current_channel", "RunNotFound"):
        assert hasattr(runstate, name), f"runstate should export {name!r}"
    assert not hasattr(runstate, "open_channel")  # removed, not aliased
    assert not hasattr(runstate, "attach")  # -> current_channel
