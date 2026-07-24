from __future__ import annotations

import sys
import time
from pathlib import Path

from .app import SingleRunApp
from .env import Env
from .multirun import MultiRunApp
from .resolver import explicit_resolver, glob_resolver, manifest_resolver, ref_from_path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    group_by: str | None = None
    if "--group-by" in args:
        i = args.index("--group-by")
        try:
            group_by = args[i + 1]
        except IndexError:
            print("usage: --group-by <attr>", file=sys.stderr)
            return 2
        del args[i : i + 2]
    if len(args) < 1:
        print(
            "usage: runstate-tui <run.db> | <dir> | <manifest.json> | <run.db> [<run.db> ...] "
            "[--group-by <attr>]",
            file=sys.stderr,
        )
        return 2
    if len(args) == 1 and Path(args[0]).is_dir():
        root = args[0]
        MultiRunApp(
            glob_resolver(root),
            Env(clock=time.time),
            group_by=group_by,
            empty_hint=f"watching {root}/**/*.db — no runs yet",
        ).run()  # real wall-clock; blocks until quit
        return 0
    if len(args) == 1 and Path(args[0]).suffix.lower() == ".json":
        path = args[0]
        if not Path(path).is_file():
            # a .json arg ALWAYS means "manifest"; a missing one is a usage error (spec §5),
            # never a fall-through to a phantom SingleRunApp over a nonexistent run.
            print(f"error: manifest not found: {path}", file=sys.stderr)
            return 2
        MultiRunApp(
            manifest_resolver(path),
            Env(clock=time.time),
            group_by=group_by,
            empty_hint=f"watching {path} — no runs yet",
        ).run()  # real wall-clock; blocks until quit
        return 0
    if len(args) >= 2:
        resolver = explicit_resolver([ref_from_path(p) for p in args])
        MultiRunApp(resolver, Env(clock=time.time), group_by=group_by).run()  # blocks until quit
        return 0
    ref = ref_from_path(args[0])
    SingleRunApp(ref, Env(clock=time.time)).run()  # real wall-clock; blocks until quit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
