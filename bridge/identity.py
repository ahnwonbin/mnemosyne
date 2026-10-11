"""Project identity resolution for Hermes second-brain bridge.
Implements the 5-tier resolution priority defined in section 5 of the architecture plan.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Dict, List, Optional
import uuid

# Namespace for deterministic UUID generation from canonical paths/remotes
HERMES_PROJECT_NAMESPACE = uuid.UUID("3c8e71b2-10f4-4b47-9dc3-e0d04c000101")


class ResolvedProject:
    """Represents a resolved project identity."""

    def __init__(
        self,
        project_id: str,
        name: str,
        canonical_path: str,
        resolution_source: str,  # explicit_override, path_alias, git_common_dir, nearest_parent, new_candidate
        is_git: bool = False,
        git_common_dir: Optional[str] = None,
        branch: Optional[str] = None,
        worktree_root: Optional[str] = None,
        remotes: Optional[List[str]] = None,
    ):
        self.project_id = project_id
        self.name = name
        self.canonical_path = canonical_path
        self.resolution_source = resolution_source
        self.is_git = is_git
        self.git_common_dir = git_common_dir
        self.branch = branch
        self.worktree_root = worktree_root
        self.remotes = remotes or []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "canonical_path": self.canonical_path,
            "resolution_source": self.resolution_source,
            "is_git": self.is_git,
            "git_common_dir": self.git_common_dir,
            "branch": self.branch,
            "worktree_root": self.worktree_root,
            "remotes": self.remotes,
        }

    def __repr__(self) -> str:
        return f"<ResolvedProject id={self.project_id} name={self.name} src={self.resolution_source}>"


def normalize_path(raw_path: str | Path) -> str:
    """Normalize a path to a canonical, cross-platform string.
    On Windows:
    - Resolves absolute path
    - Normalizes slashes to forward slashes '/'
    - Drives are lowercased (e.g., 'c:/users/developer/...')
    - Trailing slashes stripped
    """
    if not raw_path:
        return ""
    p = Path(raw_path).resolve()
    normalized = str(p).replace("\\", "/")
    # Lowercase full path on Windows for case-insensitive NTFS matching
    if os.name == "nt":
        normalized = normalized.lower()
    elif len(normalized) >= 2 and normalized[1] == ":":
        normalized = normalized[0].lower() + normalized[1:]
    return normalized.rstrip("/")


def inspect_git_repo(directory: str | Path) -> Optional[Dict[str, Any]]:
    """Inspect Git properties for a directory without external shell dependence if possible."""
    norm_dir = Path(directory).resolve()

    # Check git commands
    try:
        common_cmd = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(norm_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if common_cmd.returncode != 0:
            return None

        raw_common = common_cmd.stdout.strip()
        common_dir = (
            Path(raw_common).resolve()
            if os.path.isabs(raw_common)
            else (norm_dir / raw_common).resolve()
        )

        worktree_cmd = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(norm_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        worktree_root = (
            Path(worktree_cmd.stdout.strip()).resolve()
            if worktree_cmd.returncode == 0
            else norm_dir
        )

        branch_cmd = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=str(norm_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        branch = branch_cmd.stdout.strip() if branch_cmd.returncode == 0 else ""
        if not branch:
            # Maybe detached HEAD
            head_cmd = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(norm_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
            branch = head_cmd.stdout.strip() if head_cmd.returncode == 0 else "HEAD"

        remotes_cmd = subprocess.run(
            ["git", "remote", "-v"],
            cwd=str(norm_dir),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        remotes = []
        if remotes_cmd.returncode == 0:
            for line in remotes_cmd.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] not in remotes:
                    remotes.append(parts[1])

        return {
            "git_common_dir": normalize_path(common_dir),
            "worktree_root": normalize_path(worktree_root),
            "branch": branch,
            "remotes": remotes,
        }
    except Exception:
        return None


def find_explicit_override(start_dir: Path) -> Optional[Dict[str, str]]:
    """Look for .hermes_project or .hermes/project.json from start_dir up to root."""
    curr = start_dir.resolve()
    while True:
        # Check .hermes_project file
        hp_file = curr / ".hermes_project"
        if hp_file.is_file():
            try:
                content = hp_file.read_text(encoding="utf-8").strip()
                if content.startswith("{"):
                    data = json.loads(content)
                    return {
                        "project_id": data.get("project_id", ""),
                        "name": data.get("name", curr.name),
                        "path": normalize_path(curr),
                    }
                else:
                    # Plain string: can be "project_id" or "project_id:name"
                    parts = content.split(":", 1)
                    pid = parts[0].strip()
                    name = parts[1].strip() if len(parts) > 1 else curr.name
                    return {"project_id": pid, "name": name, "path": normalize_path(curr)}
            except Exception:
                pass

        # Check .hermes/project.json
        hp_json = curr / ".hermes" / "project.json"
        if hp_json.is_file():
            try:
                data = json.loads(hp_json.read_text(encoding="utf-8"))
                return {
                    "project_id": data.get("project_id", ""),
                    "name": data.get("name", curr.name),
                    "path": normalize_path(curr),
                }
            except Exception:
                pass

        if curr.parent == curr:
            break
        curr = curr.parent
    return None


def resolve_project(
    workspace_path: str | Path,
    known_aliases: Optional[Dict[str, Dict[str, Any]]] = None,
    registered_roots: Optional[List[Dict[str, Any]]] = None,
) -> ResolvedProject:
    """Resolve project identity using the 5-priority scheme:
    1. Explicit project binding/override (.hermes_project).
    2. Existing machine/path alias from known_aliases dict.
    3. Registered Git common-directory identity.
    4. Nearest registered parent for a non-Git workspace.
    5. New candidate generated with deterministic UUID5.
    """
    path_obj = Path(workspace_path).resolve()
    norm_path = normalize_path(path_obj)
    known_aliases = known_aliases or {}
    registered_roots = registered_roots or []

    # Priority 1: Explicit project binding/override
    override = find_explicit_override(path_obj)
    if override and override.get("project_id"):
        return ResolvedProject(
            project_id=override["project_id"],
            name=override.get("name") or path_obj.name,
            canonical_path=override["path"],
            resolution_source="explicit_override",
        )

    # Priority 2: Existing machine/path alias
    if norm_path in known_aliases:
        alias_info = known_aliases[norm_path]
        return ResolvedProject(
            project_id=alias_info["project_id"],
            name=alias_info.get("name", path_obj.name),
            canonical_path=norm_path,
            resolution_source="path_alias",
        )

    # Priority 3: Registered Git common-directory identity
    git_info = inspect_git_repo(path_obj)
    if git_info:
        git_common = git_info["git_common_dir"]
        # Check if git_common_dir is mapped
        if git_common in known_aliases:
            alias_info = known_aliases[git_common]
            return ResolvedProject(
                project_id=alias_info["project_id"],
                name=alias_info.get("name", path_obj.name),
                canonical_path=git_info["worktree_root"],
                resolution_source="git_common_dir",
                is_git=True,
                git_common_dir=git_common,
                branch=git_info["branch"],
                worktree_root=git_info["worktree_root"],
                remotes=git_info["remotes"],
            )

        # Generate deterministic UUID from canonical git common dir
        det_id = str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"git:{git_common}"))
        worktree_root = git_info["worktree_root"]
        proj_name = Path(worktree_root).name
        return ResolvedProject(
            project_id=det_id,
            name=proj_name,
            canonical_path=worktree_root,
            resolution_source="git_common_dir",
            is_git=True,
            git_common_dir=git_common,
            branch=git_info["branch"],
            worktree_root=worktree_root,
            remotes=git_info["remotes"],
        )

    # Priority 4: Nearest registered parent for a non-Git workspace
    for root in sorted(registered_roots, key=lambda r: len(r.get("path", "")), reverse=True):
        root_path = normalize_path(root.get("path", ""))
        if root_path and (norm_path == root_path or norm_path.startswith(root_path + "/")):
            return ResolvedProject(
                project_id=root["project_id"],
                name=root.get("name", Path(root_path).name),
                canonical_path=root_path,
                resolution_source="nearest_parent",
                is_git=False,
            )

    # Priority 5: New candidate requiring classification
    det_id = str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"path:{norm_path}"))
    return ResolvedProject(
        project_id=det_id,
        name=path_obj.name or "unnamed-project",
        canonical_path=norm_path,
        resolution_source="new_candidate",
        is_git=False,
    )
