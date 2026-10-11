"""Unit tests for payload sanitizer and secret scrubber."""

from bridge.sanitizer import (
    scrub_secrets_from_string,
    is_sensitive_path,
    sanitize_payload,
)


def test_scrub_api_keys():
    text = "Here is an openai key: sk-proj-1234567890123456789012345678901234567890123456 and anthropic sk-ant-api03-abcdefghijklmnopqrstuvwxyz01234567890123456789"  # gitleaks:allow -- synthetic keys used to test redaction
    scrubbed = scrub_secrets_from_string(text)
    assert "sk-proj-" not in scrubbed
    assert "sk-ant-" not in scrubbed
    assert "[REDACTED_OPENAI_KEY]" in scrubbed
    assert "[REDACTED_ANTHROPIC_KEY]" in scrubbed


def test_is_sensitive_path():
    assert is_sensitive_path("C:\\Users\\user\\.env") is True
    assert is_sensitive_path("/path/to/.env.production") is True
    assert is_sensitive_path("C:\\Users\\user\\.ssh\\id_rsa") is True
    assert is_sensitive_path("C:\\Users\\user\\config\\service-account.json") is True
    assert is_sensitive_path("C:\\Users\\user\\main.py") is False


def test_sanitize_payload_sensitive_file():
    payload = {
        "file_path": "C:\\repo\\.env",
        "content": "SECRET_DB_PASSWORD=supersecretpass\nAPI_KEY=12345",
        "tool": "read_file",
    }
    sanitized, omissions = sanitize_payload(payload)
    assert sanitized["content"] == "[OMITTED: Content of sensitive environment/credential file]"
    assert "_omissions" in sanitized
    assert any("sensitive file" in o for o in omissions)


def test_sanitize_payload_key_name_scrubbing():
    payload = {
        "client_id": "test",
        "api_key": "my-secret-key-value",
        "headers": {"Authorization": "Bearer some-secret-token-string-here"},
    }
    sanitized, omissions = sanitize_payload(payload)
    assert sanitized["api_key"] == "[REDACTED_BY_KEY_NAME]"
    assert "_omissions" in sanitized


def test_sanitize_payload_large_string_truncation():
    large_str = "x" * (200 * 1024)  # 200 KB
    payload = {"output": large_str}
    sanitized, omissions = sanitize_payload(payload)
    assert "[TRUNCATED:" in sanitized["output"]
    assert any("Truncated field" in o for o in omissions)
