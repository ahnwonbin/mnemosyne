"""Unit tests for bounded project discovery."""

from pathlib import Path
from bridge.discovery import DiscoveryEngine


def test_discovery_engine(tmp_path: Path):
    root = tmp_path / "workspace_collection"
    root.mkdir()

    # Project A (Python)
    proj_a = root / "proj_a"
    proj_a.mkdir()
    (proj_a / "pyproject.toml").write_text("[project]\nname='proj_a'")

    # Project B (Node with node_modules)
    proj_b = root / "proj_b"
    proj_b.mkdir()
    (proj_b / "package.json").write_text('{"name": "proj_b"}')
    nm = proj_b / "node_modules" / "some_pkg"
    nm.mkdir(parents=True)
    (nm / "package.json").write_text('{"name": "ignore_me"}')

    # Non-project dir
    empty_dir = root / "empty_folder"
    empty_dir.mkdir()

    engine = DiscoveryEngine([root])
    results = engine.scan(max_depth=2)

    assert results["total_found"] == 2
    paths = [c["name"] for c in results["candidates"]]
    assert "proj_a" in paths
    assert "proj_b" in paths
    assert "some_pkg" not in paths
