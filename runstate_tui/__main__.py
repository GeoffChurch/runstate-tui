from __future__ import annotations

import sys
import time
from pathlib import Path

from .app import SingleRunApp
from .env import Env
from .multirun import MultiRunApp
from .resolver import explicit_resolver, glob_resolver, ref_from_path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 1:
        print("usage: runstate-tui <run.db> | <dir> | <run.db> [<run.db> ...]", file=sys.stderr)
        return 2
    if len(args) == 1 and Path(args[0]).is_dir():
        root = args[0]
        MultiRunApp(
            glob_resolver(root),
            Env(clock=time.time),
            empty_hint=f"watching {root}/**/*.db — no runs yet",
        ).run()  # real wall-clock; blocks until quit
        return 0
    if len(args) >= 2:
        resolver = explicit_resolver([ref_from_path(p) for p in args])
        MultiRunApp(resolver, Env(clock=time.time)).run()  # real wall-clock; blocks until quit
        return 0
    ref = ref_from_path(args[0])
    SingleRunApp(ref, Env(clock=time.time)).run()  # real wall-clock; blocks until quit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
