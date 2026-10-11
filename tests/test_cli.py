"""Unit tests for Hermes bridge CLI."""

import argparse
from pathlib import Path
from bridge.cli import cmd_bind, cmd_discover
from bridge.identity import resolve_project


def test_cli_bind_and_resolve(tmp_path: Path):
    target = tmp_path / "custom_project"
    target.mkdir()

    args = argparse.Namespace(
        path=str(target),
        project_id="test-bind-id-1234",
        name="BoundProjectName",
    )
    ret = cmd_bind(args)
    assert ret == 0

    resolved = resolve_project(target)
    assert resolved.project_id == "test-bind-id-1234"
    assert resolved.name == "BoundProjectName"
    assert resolved.resolution_source == "explicit_override"


def test_cli_discover(tmp_path: Path, capsys):
    p1 = tmp_path / "app1"
    p1.mkdir()
    (p1 / "package.json").write_text("{}")

    args = argparse.Namespace(roots=[str(tmp_path)], max_depth=2)
    ret = cmd_discover(args)
    assert ret == 0
    captured = capsys.readouterr()
    assert "Discovered 1 candidate projects" in captured.out
    assert "app1" in captured.out
