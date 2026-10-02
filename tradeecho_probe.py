"""Step 1: probe the Trade Echo MCP endpoint (read-only).

Connects to the MCP server, lists every available tool, then calls the
Option Flow tool once and prints the raw result.

The bearer token is read from the TRADEECHO_TOKEN environment variable.
It is only ever placed in the Authorization request header -- it is never
printed, logged, or written to disk.

Usage:
    py tradeecho_probe.py
"""

import json
import os
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://api.tradeecho.com/api/mcp/intel"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT_SECONDS = 60

# Safety net: never call a tool whose name suggests it trades or touches the account.
BLOCKED_WORDS = ("order", "trade", "buy", "sell", "account", "position",
                 "transfer", "withdraw", "deposit", "cancel", "execute")


class HttpError(RuntimeError):
    def __init__(self, status, reason, body):
        super().__init__(f"HTTP {status} {reason}: {body[:2000]}")
        self.status = status
        self.body = body


class RpcError(RuntimeError):
    def __init__(self, method, error):
        super().__init__(f"{method} failed: {json.dumps(error)}")
        self.error = error


class McpClient:
    def __init__(self, url, token):
        self._url = url
        self._token = token
        self._session_id = None
        self._next_id = 1
        self.last_response_headers = {}

    def _post(self, payload):
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION

        request = urllib.request.Request(
            self._url, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            response = urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise HttpError(e.code, e.reason, body) from None

        with response:
            self.last_response_headers = dict(response.headers.items())
            session_id = response.headers.get("Mcp-Session-Id")
            if session_id:
                self._session_id = session_id
            content_type = response.headers.get("Content-Type", "")
            body = response.read().decode("utf-8", errors="replace")
        return content_type, body

    def request(self, method, params=None):
        request_id = self._next_id
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = params

        content_type, body = self._post(payload)
        if "text/event-stream" in content_type:
            message = _find_sse_response(body, request_id)
        else:
            message = json.loads(body)

        if "error" in message:
            raise RpcError(method, message["error"])
        return message["result"]

    def notify(self, method, params=None):
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload)


def _find_sse_response(body, request_id):
    """Return the JSON-RPC message matching request_id from an SSE stream."""
    data_lines = []
    for line in body.splitlines() + [""]:
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            message = json.loads("\n".join(data_lines))
            data_lines = []
            if message.get("id") == request_id:
                return message
    raise RuntimeError("No matching response found in event stream")


def list_all_tools(client):
    tools, cursor = [], None
    while True:
        result = client.request("tools/list", {"cursor": cursor} if cursor else {})
        tools.extend(result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            return tools


def find_option_flow_tool(tools):
    def normalized(tool):
        return tool["name"].lower().replace("_", "").replace("-", "").replace(" ", "")

    candidates = [t for t in tools if "optionflow" in normalized(t)]
    if not candidates:
        candidates = [t for t in tools
                      if "option" in normalized(t) and "flow" in normalized(t)]
    return candidates[0] if candidates else None


def is_blocked(tool):
    name = tool["name"].lower()
    if any(word in name for word in BLOCKED_WORDS):
        return True
    annotations = tool.get("annotations") or {}
    return annotations.get("readOnlyHint") is False


def main():
    token = os.environ.get("TRADEECHO_TOKEN")
    if not token:
        sys.exit("TRADEECHO_TOKEN is not set. Set it in your environment and retry.")

    client = McpClient(ENDPOINT, token)

    init = client.request("initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "options-flow-logger", "version": "0.1.0"},
    })
    client.notify("notifications/initialized")
    server = init.get("serverInfo", {})
    print(f"Connected to {server.get('name', '?')} {server.get('version', '')} "
          f"(protocol {init.get('protocolVersion')})\n")

    tools = list_all_tools(client)
    print(f"=== {len(tools)} available tools ===")
    for tool in tools:
        print(f"\n- {tool['name']}")
        if tool.get("description"):
            print(f"  {tool['description']}")
        print(f"  inputSchema: {json.dumps(tool.get('inputSchema', {}))}")
        if tool.get("annotations"):
            print(f"  annotations: {json.dumps(tool['annotations'])}")

    tool = find_option_flow_tool(tools)
    if tool is None:
        sys.exit("\nNo Option Flow tool found in the list above.")
    if is_blocked(tool):
        sys.exit(f"\nRefusing to call '{tool['name']}': it does not look read-only.")

    required = tool.get("inputSchema", {}).get("required", [])
    if required:
        sys.exit(f"\n'{tool['name']}' requires arguments {required}; "
                 "see its inputSchema above and tell me what to pass.")

    print(f"\n=== Calling {tool['name']} (no arguments) ===")
    result = client.request("tools/call", {"name": tool["name"], "arguments": {}})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
