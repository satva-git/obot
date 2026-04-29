#!/usr/bin/env python3
"""
test_mcp.py — Satva MCP smoke-tester. Two transports:

  direct     hits the MCP's public URL straight (e.g. https://instantlymcp.satva.xyz/mcp/instantly/)
  via-obot   routes through O-Bot's gateway at /mcp-connect/{mcp_id} — proves O-Bot
             can see the catalog entry and route to it. Uses OBOT_API_KEY (group
             api-key, scoped to /mcp-connect/ + /api/me only).

The script is catalog-aware: --catalog satva-instantly resolves both the direct
URL and the auth header from D:/oBot/.workspaces/mcp-catalog/satva-instantly.yaml.

Modes
-----
1. Direct, catalog-aware:
     python test_mcp.py --catalog satva-instantly --key $INSTANTLY_KEY

2. Direct, manual:
     python test_mcp.py --url https://x.satva.xyz/mcp/x/ --header x-x-key --key $K

3. Through O-Bot (proves catalog entry is wired up in obot.satva.xyz):
     python test_mcp.py --catalog satva-instantly --via-obot --mcp-id me41454
     # or with --key still passed: --key forwards as the upstream auth header
     # via x-instantly-api-key. Without --key, O-Bot uses whatever credentials
     # the catalog entry has stored.

4. Run a tool call:
     python test_mcp.py --catalog satva-instantly --key $KEY \\
       --call campaigns_list --args '{"limit": 3}'

5. Liveness scan across every Satva catalog YAML (no per-MCP keys needed —
   reports HTTP-level liveness only, not full MCP handshake):
     python test_mcp.py --scan-direct

6. Through-O-Bot scan against a name→mcp_id mapping file:
     python test_mcp.py --scan-via-obot
     # mapping at D:/oBot/.workspaces/obot-mcp-ids.json (or --map-file <path>)
     # shape: { "satva-instantly": "<mcp_id>", "satva-pipedrive": "<mcp_id>", ... }
     # populated by hand from O-Bot Admin UI as you map catalog entries to registries.

7. Show O-Bot catalog-sync status (latest catalog commit + a probe of an mcp_id
   to confirm O-Bot has ingested it):
     python test_mcp.py --catalog-status [--mcp-id <id>]

Catalog sync caveat
-------------------
O-Bot syncs from the GitOps catalog repo on its own polling schedule (~1 min
observed). There is no public API to force a re-sync — `/api/mcp-catalogs/*`
is admin-only, and the OBOT_API_KEY is in the `api-key` group which has access
to `/api/me` and `/mcp-connect/` only. So the best this script can do for
"sync" is: (a) tell you what the local catalog repo contains; (b) probe a
specific {mcp_id} to confirm O-Bot has it; (c) re-probe after waiting if you
just pushed a change.

Exit codes
----------
  0  pass
  2  handshake (initialize) failed
  3  tools/list failed
  4  tools/call failed
  5  config / argument error
  6  one or more entries failed in --scan-* mode
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import time
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
DEFAULT_OBOT_BASE = os.environ.get("OBOT_BASE_URL", "https://obot.satva.xyz")
DEFAULT_MAP_FILE = os.environ.get(
    "OBOT_MCP_ID_MAP",
    r"D:\oBot\.workspaces\obot-mcp-ids.json",
)
DEFAULT_ENV_FILE = os.environ.get("ENV_FILE", r"D:\oBot\.env")

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
# .env reader (used to get OBOT_API_KEY without echoing)
# ---------------------------------------------------------------------------

def load_env(path: str = DEFAULT_ENV_FILE) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Catalog YAML loader (PyYAML if available, regex fallback otherwise)
# ---------------------------------------------------------------------------

@dataclass
class CatalogEntry:
    name: str
    url: str
    header_key: str | None
    file_path: str


def load_catalog(name: str, catalog_dir: str) -> CatalogEntry:
    fname = name if name.endswith(".yaml") else name + ".yaml"
    path = os.path.join(catalog_dir, fname)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Catalog file not found: {path}\n"
            f"Hint: set --catalog-dir or MCP_CATALOG_DIR env var."
        )
    return _load_catalog_path(path)


def _load_catalog_path(path: str) -> CatalogEntry:
    with open(path, encoding="utf-8") as f:
        text = f.read()

    try:
        import yaml  # type: ignore
        data = yaml.safe_load(text) or {}
        name_val = str(data.get("name") or os.path.basename(path))
        remote = data.get("remoteConfig") or {}
        url = remote.get("fixedURL")
        headers = remote.get("headers") or []
        header_key = headers[0].get("key") if headers else None
    except ImportError:
        url = _extract_scalar(text, "fixedURL")
        header_key = _extract_first_header_key(text)
        name_val = _extract_scalar(text, "name") or os.path.basename(path)

    if not url:
        raise ValueError(f"{path} has no remoteConfig.fixedURL")

    return CatalogEntry(name=name_val, url=url, header_key=header_key, file_path=path)


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


def list_satva_catalog_entries(catalog_dir: str) -> list[CatalogEntry]:
    entries: list[CatalogEntry] = []
    for path in sorted(glob.glob(os.path.join(catalog_dir, "satva-*.yaml"))):
        try:
            entries.append(_load_catalog_path(path))
        except Exception as e:
            print(f"  skip {os.path.basename(path)}: {e}")
    return entries


# ---------------------------------------------------------------------------
# MCP client
# ---------------------------------------------------------------------------

class McpHttpClient:
    def __init__(
        self,
        url: str,
        header_name: str | None = None,
        api_key: str | None = None,
        bearer: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self.url = url
        self.header_name = header_name
        self.api_key = api_key
        self.bearer = bearer
        self.extra_headers = extra_headers or {}
        self.session_id: str | None = None

    def _headers(self) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self.bearer:
            h["authorization"] = f"Bearer {self.bearer}"
        if self.api_key and self.header_name:
            h[self.header_name] = self.api_key
        if self.session_id:
            h["mcp-session-id"] = self.session_id
        h.update(self.extra_headers)
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
        except urllib.error.URLError as e:
            return 0, {}, {"_network_error": str(e)}

        sid = resp_headers.get("mcp-session-id")
        if sid and not self.session_id:
            self.session_id = sid

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
    """Best-effort health check at /health on the same origin (direct mode only)."""
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
    if status == 0:
        fail(f"network error: {payload.get('_network_error')}")
        sys.exit(2)
    if status != 200 or "result" not in payload:
        fail(f"initialize -> HTTP {status} {json.dumps(payload)[:300]}")
        sys.exit(2)
    info = payload["result"].get("serverInfo", {})
    proto = payload["result"].get("protocolVersion")
    ok(f"server={info.get('name')}@{info.get('version')} proto={proto} session={client.session_id}")
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
    status, _, payload = client.call("tools/call", {"name": name, "arguments": args})
    if status != 200 or "result" not in payload:
        fail(f"HTTP {status} {json.dumps(payload)[:600]}")
        sys.exit(4)
    result = payload["result"]
    if result.get("isError"):
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
# Scan modes
# ---------------------------------------------------------------------------

def scan_direct(catalog_dir: str) -> int:
    """Walk every satva-*.yaml in the catalog repo and probe HTTP-level liveness.
    No per-MCP key needed — we just check the public URL responds at all
    (200/401/4xx are all "alive"; only 5xx, network errors, timeouts mean dead).
    """
    section("scan-direct")
    entries = list_satva_catalog_entries(catalog_dir)
    if not entries:
        fail(f"no satva-*.yaml in {catalog_dir}")
        return 5
    bad = 0
    for e in entries:
        liveness = _probe_liveness(e.url)
        label = os.path.basename(e.file_path)
        if liveness["alive"]:
            ok(f"{label:32}  {e.url}  -> {liveness['summary']}")
        else:
            fail(f"{label:32}  {e.url}  -> {liveness['summary']}")
            bad += 1
    print()
    print(_color("1", f"scan-direct: {len(entries) - bad}/{len(entries)} alive"))
    return 6 if bad else 0


def _probe_liveness(url: str) -> dict:
    parsed = urllib.parse.urlparse(url)
    health_url = f"{parsed.scheme}://{parsed.netloc}/health"
    # Try /health first (cheap and unambiguous on Satva MCPs).
    try:
        with urllib.request.urlopen(health_url, timeout=10) as r:
            body = r.read().decode()
            return {"alive": True, "summary": f"/health 200 {body[:80]}"}
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            # Origin is up but /health may not exist; fall through to MCP probe.
            pass
        else:
            return {"alive": False, "summary": f"/health {e.code}"}
    except Exception:
        pass

    # Fall back to a bare initialize POST without auth — any HTTP response
    # (even 401) means the origin is reachable.
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                   "clientInfo": {"name": "scan", "version": "1"}}
    }).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"content-type": "application/json",
                 "accept": "application/json, text/event-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return {"alive": True, "summary": f"POST {r.status}"}
    except urllib.error.HTTPError as e:
        return {"alive": e.code < 500, "summary": f"POST {e.code}"}
    except urllib.error.URLError as e:
        return {"alive": False, "summary": f"network: {e}"}
    except Exception as e:
        return {"alive": False, "summary": f"error: {e}"}


def scan_via_obot(map_file: str, obot_base: str, obot_key: str) -> int:
    section("scan-via-obot")
    if not os.path.exists(map_file):
        fail(f"mapping file not found: {map_file}\n"
             f"  Create it with shape: {{\"satva-instantly\": \"<mcp_id_from_obot_ui>\", ...}}")
        return 5
    with open(map_file) as f:
        mapping = json.load(f)
    if not mapping:
        fail(f"{map_file} is empty")
        return 5

    bad = 0
    for cat_name, mcp_id in mapping.items():
        url = f"{obot_base}/mcp-connect/{mcp_id}"
        client = McpHttpClient(url, bearer=obot_key)
        status, _, payload = client.call(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "satva-scan", "version": "1.0"},
            },
        )
        if status == 200 and "result" in payload:
            info = payload["result"].get("serverInfo", {})
            ok(f"{cat_name:32}  mcp_id={mcp_id}  -> {info.get('name')}@{info.get('version')}")
        else:
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            fail(f"{cat_name:32}  mcp_id={mcp_id}  -> HTTP {status} {str(err)[:200]}")
            bad += 1
    print()
    print(_color("1", f"scan-via-obot: {len(mapping) - bad}/{len(mapping)} alive"))
    return 6 if bad else 0


def catalog_status(catalog_dir: str, obot_base: str, obot_key: str | None,
                   mcp_id: str | None) -> int:
    """Report local catalog state and (if given an mcp_id) probe O-Bot's view."""
    section("catalog-status")
    # Local repo state
    try:
        import subprocess
        head = subprocess.check_output(
            ["git", "-C", catalog_dir, "log", "-1", "--format=%h %ci %s"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        ok(f"local catalog HEAD: {head}")
    except Exception as e:
        warn(f"could not read git state in {catalog_dir}: {e}")

    yamls = sorted(os.path.basename(p)
                   for p in glob.glob(os.path.join(catalog_dir, "satva-*.yaml")))
    print(f"  satva-* YAMLs: {len(yamls)}")
    for n in yamls:
        print(f"    - {n}")

    # NOTE: O-Bot does not expose a force-sync API for our scope. Polling is
    # automatic. To verify a specific entry has been ingested, supply --mcp-id.
    if mcp_id:
        if not obot_key:
            warn("--mcp-id given but OBOT_API_KEY not in .env; skipping O-Bot probe")
            return 0
        url = f"{obot_base}/mcp-connect/{mcp_id}"
        client = McpHttpClient(url, bearer=obot_key)
        status, _, payload = client.call(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
             "clientInfo": {"name": "catalog-status", "version": "1.0"}},
        )
        if status == 200 and "result" in payload:
            info = payload["result"].get("serverInfo", {})
            ok(f"O-Bot has mcp_id={mcp_id} -> {info.get('name')}@{info.get('version')}")
        else:
            err = payload.get("error", payload) if isinstance(payload, dict) else payload
            fail(f"O-Bot probe mcp_id={mcp_id} -> HTTP {status} {str(err)[:200]}")
            return 6
    else:
        warn("no --mcp-id; can't verify O-Bot ingestion (catalog refresh has no public API)")
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Direct/through-O-Bot smoke-tester for Satva MCPs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mode selection (mutually exclusive)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--scan-direct", action="store_true",
                      help="HTTP-liveness scan of every satva-*.yaml in the catalog")
    mode.add_argument("--scan-via-obot", action="store_true",
                      help="Through-O-Bot handshake scan over a name->mcp_id mapping")
    mode.add_argument("--catalog-status", action="store_true",
                      help="Show local catalog state; optionally probe an mcp_id in O-Bot")

    # Source for single-MCP modes
    src = p.add_mutually_exclusive_group()
    src.add_argument("--catalog", help="Catalog YAML basename, e.g. 'satva-instantly'")
    src.add_argument("--url", help="Full MCP fixedURL (overrides --catalog)")

    p.add_argument("--catalog-dir", default=DEFAULT_CATALOG_DIR,
                   help=f"Local catalog repo path (default: {DEFAULT_CATALOG_DIR})")
    p.add_argument("--header", help="Auth header name (overrides catalog)")
    p.add_argument("--key", help="Upstream MCP API key. Falls back to env $MCP_API_KEY.")
    p.add_argument("--no-auth", action="store_true",
                   help="Skip upstream auth headers (for unauthenticated MCPs)")
    p.add_argument("--quick", action="store_true",
                   help="Only do handshake; skip tools/list")
    p.add_argument("--list-limit", type=int, default=8,
                   help="How many tool names to print (default 8)")
    p.add_argument("--call", help="Run tools/call for this tool name")
    p.add_argument("--args", default="{}",
                   help="JSON object passed as tool arguments (default {})")

    # O-Bot routing
    p.add_argument("--via-obot", action="store_true",
                   help="Route through O-Bot's /mcp-connect/{mcp_id} instead of the direct URL")
    p.add_argument("--mcp-id",
                   help="O-Bot catalog entry id (required with --via-obot or --catalog-status probe)")
    p.add_argument("--obot-base", default=DEFAULT_OBOT_BASE,
                   help=f"O-Bot base URL (default: {DEFAULT_OBOT_BASE})")
    p.add_argument("--map-file", default=DEFAULT_MAP_FILE,
                   help=f"name->mcp_id JSON for --scan-via-obot (default: {DEFAULT_MAP_FILE})")
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                   help=f".env path for OBOT_API_KEY (default: {DEFAULT_ENV_FILE})")

    args = p.parse_args()

    env = load_env(args.env_file)
    obot_key = env.get("OBOT_API_KEY") or os.environ.get("OBOT_API_KEY")

    # Multi-MCP modes short-circuit before single-MCP plumbing.
    if args.scan_direct:
        return scan_direct(args.catalog_dir)
    if args.scan_via_obot:
        if not obot_key:
            fail("OBOT_API_KEY not in .env or environment")
            return 5
        return scan_via_obot(args.map_file, args.obot_base, obot_key)
    if args.catalog_status:
        return catalog_status(args.catalog_dir, args.obot_base, obot_key, args.mcp_id)

    # ----- single-MCP mode -----
    if not args.catalog and not args.url and not args.via_obot:
        fail("provide one of: --catalog, --url, --scan-direct, --scan-via-obot, --catalog-status")
        return 5

    # Resolve direct URL + header from catalog or flags.
    direct_url = args.url
    header_name = args.header
    label = "(manual)"
    if args.catalog:
        try:
            entry = load_catalog(args.catalog, args.catalog_dir)
        except (FileNotFoundError, ValueError) as e:
            fail(str(e))
            return 5
        direct_url = direct_url or entry.url
        if not header_name:
            header_name = entry.header_key
        label = entry.name

    api_key = None if args.no_auth else (args.key or os.environ.get("MCP_API_KEY"))

    # Pick the actual transport URL.
    if args.via_obot:
        if not args.mcp_id:
            fail("--via-obot requires --mcp-id <O-Bot catalog entry id>")
            return 5
        if not obot_key:
            fail("OBOT_API_KEY not found in .env")
            return 5
        url = f"{args.obot_base}/mcp-connect/{args.mcp_id}"
        bearer = obot_key
        # When routing via O-Bot, the upstream MCP key is forwarded as a
        # passthrough header (O-Bot will pass arbitrary headers through to
        # the configured MCP). Catalog entry's stored credentials may also
        # work without --key.
        extra: dict[str, str] = {}
        if api_key and header_name:
            extra[header_name] = api_key
        client_kwargs = {"bearer": bearer, "extra_headers": extra}
        print(_color("1", f"Target: {label} via O-Bot"))
        print(f"  URL:      {url}")
        print(f"  Bearer:   (OBOT_API_KEY)")
        print(f"  Forward:  {header_name+'='+ '(provided)' if api_key and header_name else '(none)'}")
        client = McpHttpClient(url, **client_kwargs)
        # No /health on /mcp-connect; skip stage_health.
    else:
        if not direct_url:
            fail("no URL resolved")
            return 5
        if not args.no_auth and header_name and not api_key:
            fail(f"MCP requires header '{header_name}' but no --key / $MCP_API_KEY provided")
            return 5
        print(_color("1", f"Target: {label} (direct)"))
        print(f"  URL:    {direct_url}")
        print(f"  Header: {header_name or '(none)'}")
        print(f"  Key:    {'(provided)' if api_key else '(none)'}")
        stage_health(direct_url)
        client = McpHttpClient(direct_url, header_name=header_name, api_key=api_key)

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
