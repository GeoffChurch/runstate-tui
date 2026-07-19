import asyncio
import importlib.util

import pytest


def test_showcase_writes_the_hero_png(tmp_path):  # sync wrapper — NO @pytest.mark.asyncio
    if importlib.util.find_spec("cairosvg") is None:
        pytest.skip("cairosvg not installed")
    from scripts.showcase import scene_table  # scripts importable via pythonpath="."

    out = asyncio.run(scene_table(tmp_path))
    assert out.exists() and out.stat().st_size > 0
    assert out.with_suffix(".svg").exists()
