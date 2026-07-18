from runstate_tui.env import Env, FreshnessSignal, Liveness, resolve_liveness


def test_freshness_signal_live_stale_and_no_opinion(build_log):
    ch = build_log([])
    env = Env(clock=lambda: 100.0, stuck_threshold=60.0)
    assert resolve_liveness(ch, env, now=100.0, last_activity=100.0) is Liveness.LIVE
    assert resolve_liveness(ch, env, now=161.0, last_activity=100.0) is Liveness.STALE
    assert resolve_liveness(ch, env, now=200.0, last_activity=None) is None  # no dated activity


def test_signals_are_consulted_in_order_first_opinion_wins(build_log):
    ch = build_log([])

    class AlwaysDead:
        def liveness(self, channel, env, now, last_activity):
            return Liveness.DEAD

    env = Env(clock=lambda: 0.0, liveness=(AlwaysDead(), FreshnessSignal()))
    assert resolve_liveness(ch, env, now=0.0, last_activity=None) is Liveness.DEAD
