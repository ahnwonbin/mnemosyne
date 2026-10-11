"""Run on the existing VM after staging the three .next files. No DB mutations."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    home = Path.home()
    root = home / ".claude-mem"
    target = root / "scripts/secretary_projects_mcp.py"
    soul = home / ".hermes/profiles/secretary/SOUL.md"
    names = root / "hermes-project-names.json"
    staged = {p: p.with_name(p.name + ".next") for p in (target, soul, names)}
    digest = hashlib.sha256(staged[target].read_bytes()).hexdigest()
    if digest != args.expected_sha256:
        raise ValueError("Staged tool hash does not match tested artifact")
    module = runpy.run_path(str(staged[target]), run_name="preflight")
    module["Projects"](names_file=staged[names]).list_projects({"limit": 1})
    instructions = staged[soul].read_text(encoding="utf-8-sig")
    if "get_project_context" not in instructions or "private DMs" not in instructions:
        raise ValueError("Missing continuation or privacy instructions")
    incoming = json.loads(staged[names].read_text(encoding="utf-8-sig"))
    if not isinstance(incoming, dict) or any(not isinstance(v, dict) or not isinstance(v.get("name"), str) for v in incoming.values()):
        raise ValueError("Invalid identity metadata")
    print(json.dumps({"preflight": "passed", "sha256": digest, "apply": args.apply}), flush=True)
    if not args.apply:
        return
    backup = root / "deploy-backups" / ("project-recall-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    backup.mkdir(parents=True, mode=0o700)
    manifest = []
    for path in staged:
        relative = path.relative_to(home)
        destination = backup / relative
        if path.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        manifest.append({"target": str(path), "backup": str(destination) if path.exists() else None})
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2))
    try:
        existing = json.loads(names.read_text()) if names.exists() else {}
        existing.update(incoming)
        staged[names].write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        for path, stage in staged.items():
            stage.chmod(0o600)
            stage.replace(path)
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway.service"], check=True, timeout=45)
        subprocess.run(["systemctl", "--user", "is-active", "--quiet", "hermes-gateway.service"], check=True, timeout=15)
        subprocess.run(["systemctl", "--user", "is-active", "--quiet", "claude-mem-worker.service"], check=True, timeout=15)
    except Exception:
        for entry in manifest:
            path = Path(entry["target"])
            if entry["backup"]:
                shutil.copy2(entry["backup"], path)
            elif path.exists():
                path.unlink()
        subprocess.run(["systemctl", "--user", "restart", "hermes-gateway.service"], timeout=45)
        raise
    print(json.dumps({"deployed": True, "backup": str(backup), "gateway": "active", "memory_worker": "active"}), flush=True)


if __name__ == "__main__":
    main()
