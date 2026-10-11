"""Create ignored local credentials for the reference API without printing secrets."""
import hashlib
import json
from pathlib import Path
import secrets

credential_path = Path("credentials.local.json")
token_path = Path("admin-token.local.txt")
if credential_path.exists() or token_path.exists():
    raise SystemExit("Refusing to overwrite existing credentials.")
token = secrets.token_urlsafe(32)
with token_path.open("x", encoding="utf-8") as handle:
    handle.write(token + "\n")
try:
    with credential_path.open("x", encoding="utf-8") as handle:
        json.dump([{"token_hash": hashlib.sha256(token.encode()).hexdigest(), "role": "admin"}], handle, indent=2)
except Exception:
    token_path.unlink()
    raise
for path in (token_path, credential_path):
    path.chmod(0o600)
print(f"Created {credential_path} and {token_path}; both are ignored by Git.")
