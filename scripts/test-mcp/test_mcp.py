#!/usr/bin/env python3
"""
test_mcp.py — direct end-to-end tester for any Satva-hosted (or any) MCP
server. Speaks the Streamable-HTTP MCP protocol against the public URL —
does NOT go through O-Bot. Use this to confirm a newly deployed MCP
actually works before mapping it into an O-Bot registry.

Usage modes
-----------
1. Manual:
     python test_mcp.py --url https://instantlymcp.satva.xyz/mcp/instantly/ \\
                        --header x-instantly-api-key --key $INSTANTLY_KEY

2. Catalog-aware (reads fixedURL + auth header from a satva-* catalog YAML):
     python test_mcp.py --catalog satva-instantly --key $INSTANTLY_KEY
     # default catalog dir: D:/oBot/.workspaces/mcp-catalog
     # or override:
     python test_mcp.py --catalog satva-instantly --catalog-dir /path/to/mcp-catalog

3. Run a specific tool:
     python test_mcp.py --catalog satva-instantly --key $KEY \\
       --call campaigns_list --args '{"limit": 3}'

Exit codes
----------
  0  all stages passed
  2  handshake (initialize) failed
  3  tools/list failed
  4  tools/call failed
  5  config / argument error
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

DEFAULT_CATALOG_DIR = os.environ.get(
    "MCP_CATALOG_DIR",
    r"D:\oBot\.workspaces\mcp-catalog",
)
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 30


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _color(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def ok(msg: str) -> None:
    print(_color("32", "  PASS"), msg)


def warn(msg: str) -> None:
    print(_color("33", "  WARN"), msg)


def fail(msg: str) -> None:
    print(_color("31", "  FAIL"), msg)


def section(msg: str) -> None:
    print()
    print(_color("36;1", f"== {msg} =="))


# ---------------------------------------------------------------------------
# Catalog YAML loader (PyYAML if available, regex fallback otherwise)
# ---------------------------------------------------------------------------

@dataclass
class CatalogEntry:
    name: str
    url: str
    header_key: str | None  # None means no header required


def load_catalog(name: str, catalog_dir: str) -> CatalogEntry:
    fname = name if name.endswith(".yaml") else name + ".yaml"
    path = os.path.join(catalog_dir, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Catalog file not found: {path}\n"
            f"Hint: set --catalog-dir or MCP_CATALOG_DIR env var."
        )
    with open(path, encoding="utf-8") as f:
        text = f.read()

    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        name_val = str(data.get("name") or fname)
        remote = data.get("remoteConfig") or {}
        url = remote.get("fixedURL")
        headers = remote.get("headers") or []
        header_key = headers[0].get("key") if headers else None
    except ImportError:
        # PyYAML missing — extract the two fields we need with a tiny parser.
        url = _extract_scalar(text, "fixedURL")
        header_key = _extract_first_header_key(text)
        name_val = _extract_scalar(text, "name") or fname

    if not url:
        raise ValueError(f"{path} has no remoteConfig.fixedURL")

    return CatalogEntry(name=name_val, url=url, header_key=header_key)


def _extract_scalar(text: str, key: str) -> str | None:
    import re
    m = re.search(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$", text, re.MULTILINE)
    if not m:
        return None
    val = m.group(1).strip()
    if val.startswith(("'", '"')):
        val = val[1:-1]
    return val


def _extract_first_header_key(text: str) -> str | None:
    import re
    m = re.search(r"headers\s*:\s*\n\s*-\s*name:[^\n]*\n\s*key:\s*(\S+)", text)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------

class McpHttpClient:
    def __init__(self, url: str, header_name: str | None, api_key: str | None):
        self.url = url
        self.header_name = header_name
        self.api_key = api_key
        self.session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self.api_key and self.header_name:
            h[self.header_name] = self.api_key
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        return h

    def call(self, method: str, params: dict | None = None) -> tuple[int, dict, dict]:
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
        if params is not None:
            body["params"] = params
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            headers=self._headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                raw = r.read().decode()
                resp_headers = {k.lower(): v for k, v in r.headers.items()}
                status = r.status
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            resp_headers = {k.lower(): v for k, v in e.headers.items()}
            status = e.code

        # Capture session id from initialize response.
        sid = resp_headers.get("mcp-session-id")
        if sid and not self.session_id:
            self.session_id = sid

        # Streamable-HTTP can return either JSON or SSE. We requested both and
        # the server may pick either.
        ct = resp_headers.get("content-type", "")
        if "text/event-stream" in ct:
            payload = _parse_sse(raw)
        elif raw.strip():
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"_raw": raw}
        else:
            payload = {}

        return status, resp_headers, payload


def _parse_sse(text: str) -> dict:
    # MCP SSE responses are a single 'message' event with a JSON data line.
    data_lines = [
        line[5:].strip()
        for line in text.splitlines()
        if line.startswith("data:")
    ]
    if not data_lines:
        return {"_raw": text}
    try:
        return json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return {"_raw": text}


# ---------------------------------------------------------------------------
# Stage runners
# ---------------------------------------------------------------------------

def stage_health(url: str) -> None:
    """Best-effort health check at /health on the same origin."""
    section("health")
    parsed = urllib.parse.urlparse(url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health"
    try:
        with urllib.request.urlopen(health_url, timeout=10) as r:
            body = r.read().decode()
            ok(f"GET /health -> {r.status} {body[:120]}")
    except Exception as e:
        warn(f"GET /health failed (not all MCPs implement it): {e}")


def stage_initialize(client: McpHttpClient) -> None:
    section("initialize")
    status, _, payload = client.call(
        "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "satva-mcp-tester", "version": "1.0"},
        },
    )
    if status != 200 or "result" not in payload:
        fail(f"initialize -> HTTP {status} {json.dumps(payload)[:300]}")
        sys.exit(2)
    info = payload["result"].get("serverInfo", {})
    proto = payload["result"].get("protocolVersion")
    ok(f"server={info.get('name')}@{info.get('version')} proto={proto} session={client.session_id}")

    # Per spec, send the initialized notification before issuing further calls.
    # Some servers are lenient but the spec requires it.
    client.call("notifications/initialized")


def stage_tools_list(client: McpHttpClient, show: int = 8) -> list[dict]:
    section("tools/list")
    status, _, payload = client.call("tools/list")
    if status != 200 or "result" not in payload:
        fail(f"tools/list -> HTTP {status} {json.dumps(payload)[:400]}")
        sys.exit(3)
    tools = payload["result"].get("tools", [])
    ok(f"received {len(tools)} tools")
    for t in tools[:show]:
        desc = (t.get("description") or "").splitlines()[0][:90]
        print(f"    - {t['name']}: {desc}")
    if len(tools) > show:
        print(f"    ... +{len(tools) - show} more")
    return tools


def stage_tool_call(client: McpHttpClient, name: str, args: dict) -> None:
    section(f"tools/call {name}")
    status, _, payload = client.call(
        "tools/call", {"name": name, "arguments": args}
    )
    if status != 200 or "result" not in payload:
        fail(f"HTTP {status} {json.dumps(payload)[:600]}")
        sys.exit(4)
    result = payload["result"]
    if result.get("isError"):
        # Many MCPs return tool errors inside a successful HTTP response —
        # surface that distinctly so smoke-tests can decide if it's a config
        # issue (bad API key) vs a real bug.
        fail("tool returned isError=true")
        for c in result.get("content", []):
            if c.get("type") == "text":
                print(f"    {c.get('text', '')[:400]}")
        sys.exit(4)
    ok("tool call succeeded")
    for c in result.get("content", [])[:3]:
        if c.get("type") == "text":
            txt = c.get("text", "")
            print(f"    {txt[:400]}{'...' if len(txt) > 400 else ''}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Direct MCP smoke-tester for Satva-hosted MCPs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--catalog", help="Catalog YAML basename, e.g. 'satva-instantly'")
    src.add_argument("--url", help="Full MCP fixedURL (overrides --catalog)")

    p.add_argument("--catalog-dir", default=DEFAULT_CATALOG_DIR,
                   help=f"Local catalog repo path (default: {DEFAULT_CATALOG_DIR})")
    p.add_argument("--header", help="Auth header name (overrides catalog)")
    p.add_argument("--key", help="API key value. Falls back to env $MCP_API_KEY.")
    p.add_argument("--no-auth", action="store_true",
                   help="Skip auth headers entirely (for unauthenticated MCPs)")
    p.add_argument("--quick", action="store_true",
                   help="Only do handshake; skip tools/list")
    p.add_argument("--list-limit", type=int, default=8,
                   help="How many tool names to print (default 8)")
    p.add_argument("--call", help="Run tools/call for this tool name")
    p.add_argument("--args", default="{}",
                   help="JSON object passed as tool arguments (default {})")

    args = p.parse_args()

    # Resolve URL + header from catalog or flags.
    url = args.url
    header_name = args.header
    label = "(manual)"
    if args.catalog:
        try:
            entry = load_catalog(args.catalog, args.catalog_dir)
        except (FileNotFoundError, ValueError) as e:
            fail(str(e))
            return 5
        url = url or entry.url
        if not header_name:
            header_name = entry.header_key
        label = entry.name

    if not url:
        fail("no URL resolved")
        return 5

    api_key = None if args.no_auth else (args.key or os.environ.get("MCP_API_KEY"))
    if not args.no_auth and header_name and not api_key:
        fail(f"this MCP requires header '{header_name}' but no --key / $MCP_API_KEY was provided")
        return 5

    print(_color("1", f"Target: {label}"))
    print(f"  URL:    {url}")
    print(f"  Header: {header_name or '(none)'}")
    print(f"  Key:    {'(provided)' if api_key else '(none)'}")

    stage_health(url)

    client = McpHttpClient(url, header_name, api_key)
    stage_initialize(client)

    if args.quick:
        return 0

    tools = stage_tools_list(client, show=args.list_limit)

    if args.call:
        try:
            tool_args = json.loads(args.args)
        except json.JSONDecodeError as e:
            fail(f"--args is not valid JSON: {e}")
            return 5
        if args.call not in {t["name"] for t in tools}:
            warn(f"tool '{args.call}' not in tools/list — will attempt anyway")
        stage_tool_call(client, args.call, tool_args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
