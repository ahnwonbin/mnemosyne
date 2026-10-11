"""Capture adapters for CLI platforms."""

from bridge.adapters.base import BaseAdapter, read_hook_input
from bridge.adapters.claude_code import ClaudeCodeAdapter
from bridge.adapters.codex import CodexAdapter
from bridge.adapters.agy import AntigravityAdapter

__all__ = [
    "BaseAdapter",
    "read_hook_input",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "AntigravityAdapter",
]
