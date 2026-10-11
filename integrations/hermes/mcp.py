"""Hermes Agent MCP integration wrapper.
Points Hermes to the memory MCP server implementation.
"""
from server.mcp_server import MemoryMCPServer, TOOL_DEFINITIONS, main

__all__ = ["MemoryMCPServer", "TOOL_DEFINITIONS", "main"]

if __name__ == "__main__":
    main()
