"""MCP client-side instrumentation — record MCP traffic into agentcrash.schema.v1.

Mechanism A from ``docs/research/mcp.md`` §8.1: instrument the MCP *client*
inside the host/agent. This is where AgentCrash has the richest context — it
sees the agent turn that triggered each call, the request args, and the
result the model consumed.

Design (the ponytail win): every MCP interaction is funneled through
``ctx.call_external(kind="mcp", ...)``. That method already exists on both the
record path (:class:`agentcrash.sdk.RunContext`) and the replay path
(:class:`agentcrash.replay.ReplayContext`) with an identical signature, and
the replay engine already maps ``MCP_RESPONSE -> kind "mcp"`` and treats
``"mcp"`` as simulated-live-capable. So an agent that talks to a wrapped
session is **replayable as-is** and inherits deterministic replay,
counterfactuals, and the analyzer's disambiguation root-cause — for free,
with no core changes.

Two layers:

* :class:`MCPClientRecorder` — transport-agnostic and dependency-free. The
  caller supplies ``fn = lambda: <real MCP call>``; the recorder records the
  result frozen. Works with any MCP client in any language/SDK, since it only
  ever calls ``fn`` and records the returned JSON.
* :class:`RecordingClientSession` — optional convenience wrapper around a
  ``mcp.ClientSession`` (requires the ``mcp`` extra). Auto-records by
  delegating each method to the recorder.

MCP error channels are preserved per spec §7.2 / §9.5:

* ``isError: true`` is a *returned* value, not a raise. ``fn`` returns the
  result object; it is recorded as ``MCP_RESPONSE`` (completed) with the
  ``isError`` flag carried in the output, and replay returns the same object
  rather than raising — behavioral fidelity is preserved.
* JSON-RPC / transport errors surface as exceptions from ``fn``; they are
  recorded failed and replay re-raises the original error.

Records are redacted automatically (redaction runs inside ``call_external``),
so secrets in tool args / ``Mcp-Param-*`` headers are scrubbed before storage.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agentcrash.schema import Actor, ActorType, EventType

__all__ = ["MCPClientRecorder", "RecordingClientSession"]


class MCPClientRecorder:
    """Records MCP client traffic into a live AgentCrash run.

    ``ctx`` is a :class:`~agentcrash.sdk.RunContext` (record) or the matching
    replay context. ``server`` is the logical MCP server name (from
    ``serverInfo.name``) so fixture keys stay distinct across servers.
    """

    def __init__(self, ctx: Any, server: str):
        self.ctx = ctx
        self.server = server

    # ----- tools/call -----
    def call_tool(self, tool: str, arguments: dict[str, Any], fn: Callable[[], Any]) -> Any:
        """Record an MCP ``tools/call``. ``fn`` must return the MCP result
        object (a dict carrying ``content`` and optional ``isError``), or raise
        on a JSON-RPC / transport error. Returns the recorded/replayed result."""
        return self.ctx.call_external(
            kind="mcp",
            name=f"{self.server}.{tool}",
            signature={"server": self.server, "tool": tool, "arguments": arguments},
            fn=fn,
            request_type=EventType.MCP_REQUEST.value,
            response_type=EventType.MCP_RESPONSE.value,
            actor=Actor(type=ActorType.TOOL, name=tool),
            input_payload={"server": self.server, "tool": tool, "arguments": arguments},
        )

    # ----- resources/read -----
    def read_resource(self, uri: str, fn: Callable[[], Any]) -> Any:
        return self.ctx.call_external(
            kind="mcp",
            name=f"{self.server}.resource.{uri}",
            signature={"server": self.server, "uri": uri},
            fn=fn,
            request_type=EventType.MCP_REQUEST.value,
            response_type=EventType.MCP_RESPONSE.value,
            actor=Actor(type=ActorType.TOOL, name="read_resource"),
            input_payload={"server": self.server, "uri": uri},
        )

    # ----- prompts/get -----
    def get_prompt(self, name: str, arguments: dict[str, Any] | None, fn: Callable[[], Any]) -> Any:
        return self.ctx.call_external(
            kind="mcp",
            name=f"{self.server}.prompt.{name}",
            signature={"server": self.server, "prompt": name, "arguments": arguments or {}},
            fn=fn,
            request_type=EventType.MCP_REQUEST.value,
            response_type=EventType.MCP_RESPONSE.value,
            actor=Actor(type=ActorType.TOOL, name="get_prompt"),
            input_payload={"server": self.server, "prompt": name, "arguments": arguments or {}},
        )

    # ----- tools/list (discovery; frozen so exact replay returns the catalog) -----
    def list_tools(self, fn: Callable[[], Any]) -> Any:
        return self.ctx.call_external(
            kind="mcp",
            name=f"{self.server}.tools/list",
            signature={"server": self.server, "method": "tools/list"},
            fn=fn,
            request_type=EventType.MCP_REQUEST.value,
            response_type=EventType.MCP_RESPONSE.value,
            actor=Actor(type=ActorType.TOOL, name="list_tools"),
            input_payload={"server": self.server, "method": "tools/list"},
        )


def _to_dict(obj: Any) -> Any:
    """Normalize an MCP SDK result object to plain JSON-able dicts/lists.

    The ``mcp`` SDK returns pydantic models (``CallToolResult`` etc.) that
    redaction's walker and storage's ``json.dumps`` can't introspect. Coerce
    them to dicts so redaction + serialization behave identically to the
    dependency-free recorder path.
    """
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, list):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, tuple):
        return [_to_dict(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_dict(v) for k, v in obj.items()}
    return obj


class RecordingClientSession:
    """Optional wrapper around ``mcp.ClientSession`` that auto-records traffic.

    Requires the ``mcp`` extra (``pip install 'agentcrash[mcp]'``). Wraps a real
    ``ClientSession`` so an agent can use the identical API while every call is
    recorded into ``ctx`` via :class:`MCPClientRecorder`. During replay the
    underlying session is never called — frozen responses are returned — so the
    agent code is identical between record and replay.

    Only the high-signal methods are wrapped (``call_tool``, ``read_resource``,
    ``get_prompt``, ``list_tools``); everything else delegates to the
    underlying session.
    """

    def __init__(self, underlying: Any, ctx: Any, server: str):
        try:
            import mcp  # noqa: F401  (availability check)  # pragma: no cover
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "RecordingClientSession requires the `mcp` package. "
                "Install it with: pip install 'agentcrash[mcp]'"
            ) from e
        self._underlying = underlying
        self._recorder = MCPClientRecorder(ctx, server)

    def __getattr__(self, attr: str) -> Any:
        # Delegate anything we don't wrap (e.g. `initialize`, `send_request`)
        # straight to the underlying session.
        return getattr(self._underlying, attr)

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return self._recorder.call_tool(
            name, arguments or {},
            lambda: _to_dict(self._underlying.call_tool(name, arguments or {})),
        )

    def read_resource(self, uri: str) -> Any:
        return self._recorder.read_resource(uri, lambda: _to_dict(self._underlying.read_resource(uri)))

    def get_prompt(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        def _fn():
            return _to_dict(self._underlying.get_prompt(name, arguments or {}))
        return self._recorder.get_prompt(name, arguments, _fn)

    def list_tools(self) -> Any:
        return self._recorder.list_tools(lambda: _to_dict(self._underlying.list_tools()))