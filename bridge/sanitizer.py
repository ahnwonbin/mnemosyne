"""Conservative payload filtering before local persistence or network upload."""
import json
import re

SENSITIVE_KEY_PATTERNS = re.compile(r"(?i)^(?:.*(?:api[_-]?key|password|secret|credentials|private[_-]?key)|authorization|bearer|access_token|refresh_token|session_token|token)$")
SENSITIVE_FILE_PATTERNS = re.compile(r"(?i)(?:\.env(?:\..*)?|.*\.env|id_rsa|id_ed25519|.*credentials.*\.json|.*service[-_]account.*\.json|\.npmrc|\.pypirc|\.netrc|.*\.pem)$")
DEFAULT_MAX_FIELD_LENGTH = 128 * 1024
DEFAULT_MAX_EVENT_PAYLOAD_BYTES = 512 * 1024
SECRET_PATTERNS = [
    (re.compile(r"<private\b[^>]*>[\s\S]*?(?:</private\s*>|$)", re.I), "[OMITTED: private content]"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)"), "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"sk-(?:proj-)?[a-zA-Z0-9_-]{20,}"), "[REDACTED_OPENAI_KEY]"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"gh[pousr]_[0-9A-Za-z]{20,}|github_pat_[0-9A-Za-z_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"(?i)bearer\s+[a-zA-Z0-9_\-\.=]+"), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"://([^:/\s]+):([^@\s]+)@"), r"://\1:[REDACTED_PASSWORD]@"),
    (re.compile(r"""(?i)\b([\w-]*(?:api[_-]?key|password|secret|access_token|refresh_token))\s*[:=]\s*["']?[^\s"',;]+"""), r"\1=[REDACTED_ASSIGNMENT]"),
]


def scrub_secrets_from_string(text):
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def is_sensitive_path(path):
    return bool(path and SENSITIVE_FILE_PATTERNS.fullmatch(path.replace("\\", "/").split("/")[-1]))


def _sensitive_reference(value):
    if isinstance(value, dict):
        return any((k in {"path", "file_path", "filename", "filePath"} and isinstance(v, str) and is_sensitive_path(v))
                   or (k in {"command", "cmd"} and isinstance(v, str) and
                       re.search(r"(?i)(?:^|[\s/\\])(?:\.env\b|id_rsa\b|id_ed25519\b|[^\s]*credentials[^\s]*\.json)", v))
                   or _sensitive_reference(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_sensitive_reference(v) for v in value)
    return False


def sanitize_value(value, parent_key="", max_field_len=DEFAULT_MAX_FIELD_LENGTH, depth=0):
    if depth > 24:
        return "[OMITTED: nesting limit]", ["Nesting limit"]
    if parent_key and SENSITIVE_KEY_PATTERNS.search(parent_key):
        return "[REDACTED_BY_KEY_NAME]", ["Scrubbed sensitive field"]
    omissions = []
    if isinstance(value, str):
        # JSON can contain lone surrogates; keep the event and mark damaged text.
        if re.search(r"[\ud800-\udfff]", value):
            value = value.encode("utf-16", errors="surrogatepass").decode("utf-16", errors="replace")
            omissions.append("Normalized surrogate characters")
        clean = scrub_secrets_from_string(value)
        if clean != value:
            omissions.append("Secret/private content redacted")
        encoded = clean.encode("utf-8")
        if len(encoded) > max_field_len:
            clean = encoded[:max_field_len - 120].decode("utf-8", errors="ignore") + "\n[TRUNCATED: field byte limit]"
            omissions.append("Truncated field exceeding byte limit")
        return clean, omissions
    if isinstance(value, dict):
        sensitive = _sensitive_reference(value)
        result = {}
        for key, item in value.items():
            if sensitive and key in {"content", "file_content", "text", "output", "result", "tool_response", "toolResponse", "response", "stdout", "stderr"}:
                result[key] = "[OMITTED: Content of sensitive environment/credential file]"
                omissions.append("Omitted content for sensitive file")
            else:
                result[key], notes = sanitize_value(item, key, max_field_len, depth + 1)
                omissions.extend(notes)
        return result, omissions
    if isinstance(value, list):
        result = []
        for item in value[:1000]:
            clean, notes = sanitize_value(item, "", max_field_len, depth + 1)
            result.append(clean)
            omissions.extend(notes)
        if len(value) > 1000:
            omissions.append("Truncated array at 1000 entries")
        return result, omissions
    return value, omissions


def sanitize_payload(payload, max_payload_bytes=DEFAULT_MAX_EVENT_PAYLOAD_BYTES):
    clean, notes = sanitize_value(payload)
    if notes:
        clean["_omissions"] = sorted(set(notes))
    if len(json.dumps(clean, ensure_ascii=False).encode()) > max_payload_bytes:
        # Hard cap even for deeply nested arrays of short strings.
        clean = {"_omissions": ["Payload omitted: total byte limit exceeded"]}
        notes.append("Payload total byte limit exceeded")
    return clean, notes
