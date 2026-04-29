# test-mcp

Direct end-to-end smoke tester for Satva-hosted MCP servers. Speaks the
Streamable-HTTP MCP protocol against the public URL — bypasses O-Bot
entirely so you can verify the MCP itself works before doing the
registry mapping inside O-Bot's UI.

This lives in `satva-git/obot` because it's an oBot-operations script;
nothing in the obot Coolify deployment consumes it.

## Why this exists

O-Bot's documented API is scoped to `/mcp-connect/` and `/api/me`. There
is no programmatic way to "log in as a user, sync the catalog, and click
'test'." The catalog itself is GitOps: O-Bot polls `satva-git/mcp-catalog`
on its own schedule. So validation is split:

1. **MCP responds correctly** — this script (talks to e.g. `https://instantlymcp.satva.xyz/mcp/instantly/`).
2. **O-Bot picked up the catalog change** — visible in O-Bot Admin UI; not automatable from here.
3. **Registry mapping** — manual one-time UI click per registry.

This script handles (1).

## Install

Python 3.10+. `pyyaml` optional but recommended:

```
pip install pyyaml
```

If `pyyaml` is missing the script falls back to a tiny regex-based reader
that handles the two fields it needs (`remoteConfig.fixedURL` and the
first `headers[].key`).

## Usage

### From a catalog YAML (preferred)

Reads `fixedURL` and the auth header name straight out of the catalog
YAML so you only have to pass the API key.

```
python test_mcp.py --catalog satva-instantly --key $INSTANTLY_KEY
```

`--catalog` can be the file basename with or without `.yaml`. The
default catalog directory is `D:\oBot\.workspaces\mcp-catalog` (override
with `--catalog-dir` or env `MCP_CATALOG_DIR`).

### Manual override

```
python test_mcp.py \
  --url https://instantlymcp.satva.xyz/mcp/instantly/ \
  --header x-instantly-api-key \
  --key $INSTANTLY_KEY
```

### Quick handshake only (no tools/list)

```
python test_mcp.py --catalog satva-instantly --key $KEY --quick
```

### Run an actual tool call

```
python test_mcp.py --catalog satva-instantly --key $KEY \
  --call campaigns_list --args '{"limit": 3}'
```

### Unauthenticated MCPs

```
python test_mcp.py --url https://example.com/mcp/foo/ --no-auth
```

## Stages

The script runs these in order — failure at any stage exits with a
distinct non-zero code so it's CI-friendly:

| Stage | What it checks | Exit |
|---|---|---|
| `health` | Best-effort `GET /health` on the same origin (warning if absent — many MCPs don't implement this) | — (warning only) |
| `initialize` | MCP `initialize` handshake; captures `Mcp-Session-Id` | 2 |
| `tools/list` | Server returns its tool catalog | 3 |
| `tools/call` | Optional. Runs the tool given by `--call` with `--args` JSON | 4 |

Argument or config errors exit with `5`.

## Examples

Smoke a freshly-deployed Instantly MCP:

```
$ python test_mcp.py --catalog satva-instantly --key xxx
Target: Satva Instantly
  URL:    https://instantlymcp.satva.xyz/mcp/instantly/
  Header: x-instantly-api-key
  Key:    (provided)

== health ==
  PASS GET /health -> 200 {"status":"ok","tools":170}

== initialize ==
  PASS server=instantly@0.1.0 proto=2025-06-18 session=8d6f...

== tools/list ==
  PASS received 170 tools
    - campaigns_list: List campaigns with filtering ...
    ...
```

Verify a real Instantly key works end-to-end:

```
python test_mcp.py --catalog satva-instantly --key $INSTANTLY_KEY \
  --call campaigns_list --args '{"limit": 1}'
```

A non-zero exit there means either the key is wrong, the user's plan
doesn't expose that endpoint, or the upstream API changed shape.

## When to use this vs. O-Bot UI

| Use this | Use O-Bot UI |
|---|---|
| Right after Coolify deploy finishes | After mapping the MCP into a registry |
| Catalog YAML changed and you want to verify the URL/header is correct | Verifying an end-user's agent can pick up the tool |
| Reproducing a bug report | Testing user permissions / group assignment |

## Adding a new MCP to the smoke-test loop

No code change needed — once the catalog YAML is published, the script
finds it by name. The convention `satva-<service>` keeps things tidy.
