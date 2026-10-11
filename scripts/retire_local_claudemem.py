"""Reversibly retire the customized recorder; never touch memory databases."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil


def clean_instructions(text):
    for pattern in (r"<!-- claudemem-agent-driven -->.*?<!-- /claudemem-agent-driven -->",
                    r"<claude-mem-context>.*?</claude-mem-context>"):
        text = re.sub(pattern, "", text, flags=re.DOTALL)
    return text.strip() + "\n" if text.strip() else ""


def clean_hooks(data):
    """Remove only named upstream local-worker hooks; preserve unrelated groups."""
    count = 0
    hooks = data.get("hooks", data)
    for event, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        remaining = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                remaining.append(group)
                continue
            kept = []
            for hook in group["hooks"]:
                command = str(hook.get("command", "")).replace("\\", "/") if isinstance(hook, dict) else ""
                if (isinstance(hook, dict) and hook.get("name") == "claude-mem"
                        and "/worker-service.cjs hook antigravity-cli " in command):
                    count += 1
                else:
                    kept.append(hook)
            if kept or not group["hooks"]:
                remaining.append({**group, "hooks": kept})
        hooks[event] = remaining
    return data, count


def retire(home, apply=False):
    home = Path(home).resolve()
    changes = []
    for relative in ("AGENTS.md", ".codex/AGENTS.md", ".gemini/GEMINI.md", ".claude/CLAUDE.md"):
        path = home / relative
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8-sig")
        cleaned = clean_instructions(original)
        if cleaned.strip() != original.strip():
            changes.append((path, cleaned))
    hooks_path = home / ".gemini/config/hooks.json"
    hook_count = 0
    if hooks_path.is_file():
        data = json.loads(hooks_path.read_text(encoding="utf-8-sig"))
        data, hook_count = clean_hooks(data)
        if hook_count:
            changes.append((hooks_path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"))
    skills = [home / platform / "skills" / name
              for platform in (".codex", ".gemini", ".gemini/config", ".claude", ".agents")
              for name in ("claudemem", "claude-mem", "mem-search")]
    skills = [p for p in skills if p.exists()]
    # Verify every resolved source is inside the specifically selected user home.
    for path in [p for p, _ in changes] + skills:
        if not path.resolve().is_relative_to(home):
            raise ValueError(f"Refusing external linked path: {path}")
    result = {"apply": apply, "instruction_characters_removed": sum(
        len(p.read_text(encoding="utf-8-sig")) - len(text) for p, text in changes if p.suffix == ".md"),
        "legacy_hooks_removed": hook_count, "files": [str(p) for p, _ in changes],
        "skills_to_archive": [str(p) for p in skills]}
    if not apply or not (changes or skills):
        return result
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / ".hermes/legacy-recorder-backups" / stamp
    backup.mkdir(parents=True, exist_ok=False)
    # Back up every configuration before changing any of it.
    for path, _ in changes:
        dest = backup / path.relative_to(home)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    for path in skills:
        dest = backup / path.relative_to(home)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
    for path, text in changes:
        staged = path.with_name(path.name + ".hermes-retire.tmp")
        staged.write_text(text, encoding="utf-8")
        staged.replace(path)
    result["backup"] = str(backup)
    (backup / "manifest.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply after backing up; default is preview")
    args = parser.parse_args()
    print(json.dumps(retire(Path.home(), args.apply), indent=2))
