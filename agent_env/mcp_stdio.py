"""Minimal MCP-style stdio bridge for ChinaTravel tools.

The implementation is dependency-free and supports the basic JSON-RPC methods
used by MCP clients: initialize, tools/list, tools/call, and ping.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from agent_env.adapter import ChinaTravelEnvAdapter, dumps_result


ADAPTER = ChinaTravelEnvAdapter()


def _response(message_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if message_id is None:
        return None

    if method == "initialize":
        return _response(
            message_id,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "serverInfo": {"name": "chinatravel-agent-env", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            },
        )
    if method == "ping":
        return _response(message_id, {})
    if method == "tools/list":
        return _response(message_id, {"tools": ADAPTER.list_tools()})
    if method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        result = ADAPTER.call_tool(name, arguments)
        return _response(
            message_id,
            {
                "content": [{"type": "text", "text": dumps_result(result)}],
                "isError": not bool(result.get("success")),
            },
        )
    return _error(message_id, -32601, f"Unsupported method: {method}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = _handle(message)
        except Exception as exc:
            response = _error(None, -32603, f"{exc.__class__.__name__}: {exc}")
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()

