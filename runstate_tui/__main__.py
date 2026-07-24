from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path

from .app import SingleRunApp
from .env import Env
from .multirun import MultiRunApp
from .resolver import Resolver, explicit_resolver, glob_resolver, manifest_resolver, ref_from_path


@dataclass(frozen=True)
class CliArgs:
    """The parsed CLI, splatted out of argparse's untyped `Namespace` into a frozen, mypy-checked
    shape so downstream access is field-based and type-checked. `__post_init__` owns the
    FS-independent invariants; shape-derived checks (e.g. `--group-by` requires a multi-run target)
    cannot live here -- they need the filesystem -- so they live in `_dispatch` where the target
    shape is known."""

    paths: tuple[str, ...]
    group_by: str | None

    def __post_init__(self) -> None:
        if (
            not self.paths
        ):  # argparse nargs="+" guarantees this; the dataclass owns its own contract
            raise ValueError("at least one target is required")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="runstate-tui",
        description="Observe runstate runs: a single run.db, a directory (recursive glob), a "
        "manifest.json, or several run.db paths.",
    )
    p.add_argument(
        "paths", nargs="+", metavar="TARGET", help="run.db | dir | manifest.json | run.db ..."
    )
    p.add_argument(
        "--group-by",
        metavar="ATTR",
        default=None,
        help="section the multi-run table by this manifest attribute",
    )
    return p


def _multirun(resolver: Resolver, group_by: str | None, empty_hint: str | None = None) -> None:
    MultiRunApp(
        resolver, Env(clock=time.time), group_by=group_by, empty_hint=empty_hint
    ).run()  # real wall-clock; blocks until quit


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        ns = parser.parse_args(argv)  # argv=None -> argparse reads sys.argv[1:]
        try:
            cfg = CliArgs(paths=tuple(ns.paths), group_by=ns.group_by)
        except ValueError as e:  # a dataclass invariant -> a clean usage error, not a traceback
            parser.error(str(e))
        return _dispatch(cfg, parser)
    except SystemExit as e:  # argparse / parser.error exit(2); normalize to an int return
        return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 2)


def _dispatch(cfg: CliArgs, parser: argparse.ArgumentParser) -> int:
    """Shape-dispatch on the target(s): dir -> glob, manifest.json -> manifest, N paths -> explicit,
    one run.db -> single. `--group-by` is checked here (not in CliArgs) because 'single run' is a
    filesystem-derived shape."""
    paths = cfg.paths
    if len(paths) == 1 and Path(paths[0]).is_dir():
        root = paths[0]
        _multirun(glob_resolver(root), cfg.group_by, f"watching {root}/**/*.db — no runs yet")
        return 0
    if len(paths) == 1 and Path(paths[0]).suffix.lower() == ".json":
        path = paths[0]
        if not Path(path).is_file():
            # a .json arg ALWAYS means "manifest"; a missing one is a usage error (spec §5),
            # never a fall-through to a phantom SingleRunApp over a nonexistent run.
            parser.error(f"manifest not found: {path}")
        _multirun(manifest_resolver(path), cfg.group_by, f"watching {path} — no runs yet")
        return 0
    if len(paths) >= 2:
        _multirun(explicit_resolver([ref_from_path(p) for p in paths]), cfg.group_by)
        return 0
    # a single run.db -- the only non-multi-run target, so grouping has no meaning here.
    if cfg.group_by is not None:
        parser.error(
            "--group-by applies to a directory / manifest / multiple runs, not a single run"
        )
    SingleRunApp(ref_from_path(paths[0]), Env(clock=time.time)).run()  # blocks until quit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
