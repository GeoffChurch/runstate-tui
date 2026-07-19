import asyncio
import importlib.util

import pytest


@pytest.mark.parametrize("name", ["table", "single", "integrity", "drilldown", "stop"])
def test_every_scene_renders(name, tmp_path):  # sync wrapper — NO @pytest.mark.asyncio
    if importlib.util.find_spec("cairosvg") is None:
        pytest.skip("cairosvg not installed")
    from scripts.showcase import SCENES  # scripts importable via pythonpath="."

    out = asyncio.run(SCENES[name](tmp_path))
    assert out.exists() and out.stat().st_size > 0
    assert out.with_suffix(".svg").exists()
