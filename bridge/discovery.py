"""Local project discovery engine for Hermes second-brain bridge.
Safely scans configured workspace roots and Obsidian vaults, recording
coverage, failures, dispositions, and submitting discovery runs to cloud.
"""
from __future__ import annotations
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Dict, List, Optional
import uuid

import httpx

from bridge.identity import normalize_path, resolve_project, inspect_git_repo, HERMES_PROJECT_NAMESPACE

# Folders to avoid descending into during discovery
IGNORE_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
}

# Indicators that a directory represents a project root
PROJECT_INDICATORS = {
    ".hermes_project",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "requirements.txt",
    "README.md",
    "implement plan.md",
}


class DiscoveredCandidate:
    """A discovered candidate project."""

    def __init__(
        self,
        project_id: str,
        name: str,
        path: str,
        is_git: bool,
        branch: Optional[str] = None,
        git_common_dir: Optional[str] = None,
        indicators: Optional[List[str]] = None,
        is_vault: bool = False,
        disposition: str = "unresolved",
    ):
        self.candidate_id = str(uuid.uuid5(HERMES_PROJECT_NAMESPACE, f"candidate:{path}"))
        self.project_id = project_id
        self.name = name
        self.path = path
        self.is_git = is_git
        self.branch = branch
        self.git_common_dir = git_common_dir
        self.indicators = indicators or []
        self.is_vault = is_vault
        self.disposition = disposition

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "project_id": self.project_id,
            "name": self.name,
            "path": self.path,
            "is_git": self.is_git,
            "branch": self.branch,
            "git_common_dir": self.git_common_dir,
            "indicators": self.indicators,
            "is_vault": self.is_vault,
            "disposition": self.disposition,
        }


class DiscoveryEngine:
    """Discovers projects under configured workspace roots and Obsidian vaults."""

    def __init__(
        self,
        workspace_roots: List[str | Path],
        vault_roots: Optional[List[str | Path]] = None,
    ):
        self.workspace_roots = [Path(r).resolve() for r in workspace_roots]
        self.vault_roots = [Path(v).resolve() for v in (vault_roots or [])]

    def scan(self, max_depth: int = 2) -> Dict[str, Any]:
        """Perform a bounded scan across all configured workspace and vault roots."""
        run_id = str(uuid.uuid4())
        scan_time = datetime.now(timezone.utc).isoformat()
        candidates: List[DiscoveredCandidate] = []
        failures: List[Dict[str, str]] = []
        visited_paths = set()

        # 1. Scan workspace roots
        for root in self.workspace_roots:
            if not root.is_dir():
                failures.append({"path": normalize_path(root), "error": "directory does not exist"})
                continue

            self._scan_directory(
                dir_path=root,
                current_depth=0,
                max_depth=max_depth,
                candidates=candidates,
                failures=failures,
                visited_paths=visited_paths,
            )

        # 2. Scan vault roots for planning-only and documented projects
        for vroot in self.vault_roots:
            if not vroot.is_dir():
                failures.append({"path": normalize_path(vroot), "error": "vault root does not exist"})
                continue
            self._scan_vault(
                vault_dir=vroot,
                candidates=candidates,
                failures=failures,
                visited_paths=visited_paths,
            )

        return {
            "run_id": run_id,
            "scan_time": scan_time,
            "roots": [normalize_path(r) for r in self.workspace_roots] + [normalize_path(v) for v in self.vault_roots],
            "total_found": len(candidates),
            "failures": failures,
            "candidates": [c.to_dict() for c in candidates],
        }

    def _scan_directory(
        self,
        dir_path: Path,
        current_depth: int,
        max_depth: int,
        candidates: List[DiscoveredCandidate],
        failures: List[Dict[str, str]],
        visited_paths: set,
    ) -> None:
        norm_dir = normalize_path(dir_path)
        if norm_dir in visited_paths:
            return
        visited_paths.add(norm_dir)

        is_project = False
        found_indicators = []

        # Check Git
        git_info = inspect_git_repo(dir_path)
        if git_info:
            is_project = True
            found_indicators.append(".git")

        # Check file indicators
        try:
            entries = list(os.scandir(dir_path))
        except (PermissionError, OSError) as exc:
            failures.append({"path": norm_dir, "error": str(exc)})
            return

        for entry in entries:
            if entry.name in PROJECT_INDICATORS:
                found_indicators.append(entry.name)
                is_project = True

        if is_project and current_depth > 0:
            resolved = resolve_project(dir_path)
            candidates.append(
                DiscoveredCandidate(
                    project_id=resolved.project_id,
                    name=resolved.name,
                    path=norm_dir,
                    is_git=bool(git_info),
                    branch=git_info["branch"] if git_info else None,
                    git_common_dir=git_info["git_common_dir"] if git_info else None,
                    indicators=found_indicators,
                    is_vault=False,
                )
            )
            if git_info and current_depth >= 1:
                return

        if current_depth < max_depth:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in IGNORE_DIRS and not entry.name.startswith("."):
                        self._scan_directory(
                            dir_path=Path(entry.path),
                            current_depth=current_depth + 1,
                            max_depth=max_depth,
                            candidates=candidates,
                            failures=failures,
                            visited_paths=visited_paths,
                        )

    def _scan_vault(
        self,
        vault_dir: Path,
        candidates: List[DiscoveredCandidate],
        failures: List[Dict[str, str]],
        visited_paths: set,
    ) -> None:
        """Scan Obsidian vault folders for planning-only project charters."""
        try:
            entries = list(os.scandir(vault_dir))
        except (PermissionError, OSError) as exc:
            failures.append({"path": normalize_path(vault_dir), "error": str(exc)})
            return

        for entry in entries:
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                p_dir = Path(entry.path)
                norm_p = normalize_path(p_dir)
                if norm_p in visited_paths:
                    continue
                visited_paths.add(norm_p)

                # Look for charter or project doc
                charter_files = ["charter.md", "project.md", "README.md", f"{entry.name}.md"]
                is_vault_proj = any((p_dir / cf).is_file() for cf in charter_files)

                if is_vault_proj:
                    resolved = resolve_project(p_dir)
                    candidates.append(
                        DiscoveredCandidate(
                            project_id=resolved.project_id,
                            name=entry.name,
                            path=norm_p,
                            is_git=False,
                            indicators=["vault_charter"],
                            is_vault=True,
                        )
                    )


def submit_discovery_to_cloud(
    endpoint_url: str,
    auth_token: str,
    device_id: str,
    scan_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Submit discovery run and candidates to cloud API endpoint."""
    body = {
        "run_id": scan_result["run_id"],
        "device_id": device_id,
        "roots": scan_result["roots"],
        "failures": scan_result.get("failures", []),
        "candidates": scan_result.get("candidates", []),
    }
    headers = {"Authorization": f"Bearer {auth_token}"}
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(f"{endpoint_url.rstrip('/')}/api/v1/discovery/runs", json=body, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(f"Cloud discovery submission failed: HTTP {resp.status_code}: {resp.text}")
        return resp.json()
