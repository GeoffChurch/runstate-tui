def test_runstate_and_package_import():
    import runstate

    # the places (docs/specs/channel-locators.md in GeoffChurch/runstate): a channel is
    # opened from a place (`SqliteLogs`, `PostgresLogs`), by `reader` (existing-only,
    # read-only), `attach` (existing-only, writable) or `create` (birth); the module-level
    # locators are GONE. `current_channel` is the worker's, and RunNotFound the absence signal.
    for name in ("Logs", "Reader", "SqliteLogs", "PostgresLogs", "current_channel", "RunNotFound"):
        assert hasattr(runstate, name), f"runstate should export {name!r}"
    for gone in ("open_channel", "attach_channel", "create_channel"):
        assert not hasattr(runstate, gone)  # removed, not aliased
    assert not hasattr(runstate, "attach")  # -> current_channel
