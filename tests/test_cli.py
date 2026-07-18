from runstate_tui.__main__ import main


def test_no_argument_prints_usage_and_returns_2(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_too_many_arguments_returns_2():
    assert main(["a.db", "b.db"]) == 2
