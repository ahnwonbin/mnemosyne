"""Tests for project identity resolution."""

from pathlib import Path
import uuid
from bridge.identity import (
    HERMES_PROJECT_NAMESPACE,
    normalize_path,
    resolve_project,
)


def test_normalize_path():
    path_win = "C:\\Users\\developer\\pythoncode\\hermes\\"
    norm = normalize_path(path_win)
    assert norm == "c:/users/developer/pythoncode/hermes"
    assert not norm.endswith("/")


def test_explicit_override_in_directory(tmp_path: Path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    override_file = sub / ".hermes_project"
    test_id = str(uuid.uuid4())
    override_file.write_text(f'{{"project_id": "{test_id}", "name": "my-explicit-project"}}')

    res = resolve_project(sub)
    assert res.project_id == test_id
    assert res.name == "my-explicit-project"
    assert res.resolution_source == "explicit_override"


def test_explicit_override_in_parent_directory(tmp_path: Path):
    parent = tmp_path / "repo_root"
    parent.mkdir()
    child = parent / "packages" / "frontend"
    child.mkdir(parents=True)

    override_file = parent / ".hermes_project"
    test_id = str(uuid.uuid4())
    override_file.write_text(f"{test_id}:monorepo-root")

    res = resolve_project(child)
    assert res.project_id == test_id
    assert res.name == "monorepo-root"
    assert res.resolution_source == "explicit_override"


def test_path_alias_resolution(tmp_path: Path):
    norm_path = normalize_path(tmp_path)
    aliases = {
        norm_path: {
            "project_id": "alias-proj-uuid",
            "name": "aliased-project",
        }
    }
    res = resolve_project(tmp_path, known_aliases=aliases)
    assert res.project_id == "alias-proj-uuid"
    assert res.name == "aliased-project"
    assert res.resolution_source == "path_alias"


def test_nearest_parent_resolution(tmp_path: Path):
    parent = tmp_path / "projects"
    child = parent / "unregistered_subfolder" / "deep"
    child.mkdir(parents=True)

    norm_parent = normalize_path(parent)
    registered_roots = [
        {"path": norm_parent, "project_id": "parent-proj-uuid", "name": "ProjectsParent"}
    ]

    res = resolve_project(child, registered_roots=registered_roots)
    assert res.project_id == "parent-proj-uuid"
    assert res.name == "ProjectsParent"
    assert res.resolution_source == "nearest_parent"


def test_new_candidate_deterministic_uuid(tmp_path: Path):
    res1 = resolve_project(tmp_path)
    res2 = resolve_project(tmp_path)

    assert res1.project_id == res2.project_id
    assert res1.resolution_source == "new_candidate"
    expected = str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"path:{normalize_path(tmp_path)}"))
    assert res1.project_id == expected
