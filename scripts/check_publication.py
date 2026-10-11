"""Check staged source for common publication hazards; never print matched values."""
import argparse
import re
import subprocess
import sys
import string
from pathlib import Path

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Read the exact Git index contents")
    args = parser.parse_args()
    command = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"] if args.staged else ["git", "ls-files", "-z"]
    names = subprocess.check_output(command).decode("utf-8").split("\0")
    findings = []
    token_pattern = re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{35}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)")
    email_pattern = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    for name in filter(None, names):
        path = Path(name)
        if path.suffix in {".db", ".sqlite3", ".pem", ".key"} or any(part in {"scratch", "context", "graphify-out", ".venv"} for part in path.parts) or path.name in {".env", "hermes.env", "credentials.local.json", "hermes-config.yaml", "cliproxy-config.yaml"} or ".local." in path.name:
            findings.append((name, 0, "runtime or private file"))
            continue
        raw = subprocess.check_output(["git", "show", ":" + name]) if args.staged else path.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            findings.append((name, 0, "binary file requires manual review"))
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for match in token_pattern.finditer(line):
                # Exact synthetic fixtures exercise redaction without uploading a real credential.
                fixture = name in {"tests/test_adapters.py", "tests/test_sanitizer.py"} and match.group() in {"sk-proj-" + "1234567890" * 4 + "123456", "sk-ant-api03-" + string.ascii_lowercase + "0123456789" * 2}
                if not fixture:
                    findings.append((name, number, "credential-shaped value"))
            for match in email_pattern.finditer(line):
                domain = match.group(1).lower()
                if domain not in {"example.com", "example.org", "example.net", "example.invalid", "memory.example"} and not domain.endswith(".example"):
                    findings.append((name, number, "email-like identifier"))
            for match in re.finditer(r"(?i)(?:[A-Z]:[\\/]+Users[\\/]+([^\\/\s\"']+)|[/]home/([^/\s\"']+))", line):
                username = next(group for group in match.groups() if group)
                if username.lower() not in {"developer", "operator", "owner", "example", "user"}:
                    findings.append((name, number, "user-specific absolute path"))
    for name, number, category in findings:
        print(f"{name}:{number}: {category}")
    if findings:
        print(f"Publication check failed: {len(findings)} finding(s). Review without printing credentials.")
        return 1
    print("Publication check passed for selected Git contents. Pattern checks are not a complete security audit.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
