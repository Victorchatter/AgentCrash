"""Framework adapters mapping foreign agent events into agentcrash.schema.v1.

Each integration lives in its own module. The first one, ``mcp_client``,
records MCP client↔server traffic (Mechanism A in docs/research/mcp.md §8.1).
"""
from __future__ import annotations