# test-mcp

End-to-end smoke tester for Satva-hosted MCP servers. Two transports:

| Transport | What it proves |
|---|---|
| **direct** | The MCP server itself responds correctly to the MCP protocol on its public URL. Bypasses O-Bot. |
| **via-obot** | O-Bot can see the catalog entry and route through `/mcp-connect/{mcp_id}` — proves not just the MCP but that O-Bot's view of it is wired up. |

Lives in `satva-git/obot` because the upstream mirror has no active
deployment consuming it — the natural home for oBot ops scripts.

## What this does and doesn't replace

✅ Full MCP protocol handshake, `tools/list`, `tools/call` against any MCP — direct or via O-Bot.
✅ Liveness scan over every `satva-*.yaml` in the catalog repo.
✅ Through-O-Bot scan over a name→mcp_id mapping you maintain locally.

❌ Cannot trigger an O-Bot catalog re-sync. The `/api/mcp-catalogs/*` endpoints
   are admin-only; the OBOT_API_KEY is in the `api-key` group which is
   restricted (verified in `pkg/api/authz/authz.go`) to:
   - `GET /api/me`
   - `/mcp-connect/`
   O-Bot polls the catalog repo automatically (~1 min observed), so there's
   nothing to trigger — just wait, then re-probe.
❌ Cannot enumerate which MCPs are connected to your account programmatically
   (same authz reason). You discover catalog-entry IDs from the O-Bot UI URL
   bar and write them to a local mapping file.

## Setup

Python 3.10+. PyYAML optional but recommended:

```
pip install pyyaml
```

Secrets (read from `D:\oBot\.env`, gitignored):

```
OBOT_BASE_URL=https://obot.satva.xyz
OBOT_API_KEY=ok1-...    # for --via-obot and --scan-via-obot only
```

Override `.env` location with `--env-file <path>` or env `ENV_FILE`.

## Modes

### Direct, catalog-aware

```
python test_mcp.py --catalog satva-instantly --key $INSTANTLY_KEY
```

Reads `fixedURL` and the auth-header name straight from
`D:\oBot\.workspaces\mcp-catalog\satva-instantly.yaml`.

### Direct, manual override

```
python test_mcp.py --url https://x.satva.xyz/mcp/x/ --header x-x-key --key $K
python test_mcp.py --url https://example/ --no-auth     # for unauthenticated MCPs
python test_mcp.py --catalog satva-instantly --key $KEY --quick   # handshake only
```

### Tool call

```
python test_mcp.py --catalog satva-instantly --key $INSTANTLY_KEY \
  --call campaigns_list --args '{"limit": 3}'
```

### Through O-Bot

Need the catalog entry's `mcp_id` from O-Bot's UI (look at the URL when
viewing the MCP under Admin → MCP Servers/Registries):

```
python test_mcp.py --catalog satva-instantly --via-obot --mcp-id <mcp_id>
```

This routes the request through `https://obot.satva.xyz/mcp-connect/{mcp_id}/`
with `Authorization: Bearer $OBOT_API_KEY`. If you also pass `--key`, the
upstream MCP key is forwarded as the auth header (e.g. `x-instantly-api-key`)
on top of the bearer; otherwise O-Bot uses whatever credentials it has stored
for that catalog entry.

### Liveness scan over the entire catalog (no per-MCP keys needed)

```
python test_mcp.py --scan-direct
```

Probes `/health` first, falls back to a bare-POST initialize. Anything below
500 means the origin is reachable; only network errors / 5xx are flagged.

```
== scan-direct ==
  PASS satva-basecamp.yaml      https://basecampmcp.satva.xyz/mcp/basecamp/  -> /health 200 ...
  PASS satva-instantly.yaml     https://instantlymcp.satva.xyz/mcp/instantly/  -> /health 200 {"status":"ok","tools":170}
  ...
scan-direct: 11/11 alive
```

### Through-O-Bot scan

Maintain a local mapping at `D:\oBot\.workspaces\obot-mcp-ids.json`:

```json
{
  "satva-instantly": "<mcp_id_from_obot_ui>",
  "satva-pipedrive": "<mcp_id_from_obot_ui>",
  "_README": "Map keys are catalog YAML basenames; values are O-Bot catalog entry IDs."
}
```

Then:

```
python test_mcp.py --scan-via-obot
```

Each entry runs an `initialize` through O-Bot and the row passes if O-Bot
can route to it.

### Catalog status

```
python test_mcp.py --catalog-status
python test_mcp.py --catalog-status --mcp-id <mcp_id>   # also probe O-Bot for that id
```

Shows local catalog repo HEAD + the satva-*.yaml inventory. With `--mcp-id`,
also probes O-Bot to confirm a specific catalog entry has been ingested.

## Stages and exit codes

| Stage | When it runs | Exit on fail |
|---|---|---|
| `health` | direct mode only — best-effort `GET /health` | warning, doesn't fail |
| `initialize` | always — MCP `initialize` handshake | 2 |
| `tools/list` | unless `--quick` | 3 |
| `tools/call` | only with `--call` | 4 |
| scan summary | `--scan-*` modes | 6 if any entry failed |

Argument / config errors exit with `5`.

## Verification flow after deploying a new MCP

1. **Push the new fork** to GitHub and deploy to Coolify.
2. **Direct probe** to confirm the MCP itself works:

   ```
   python test_mcp.py --catalog satva-newthing --key $KEY
   ```
3. **Push the catalog YAML** to `satva-git/mcp-catalog`.
4. **Wait ~1 min** for O-Bot to poll the catalog.
5. **Get the catalog entry id** from the O-Bot UI (URL when viewing the MCP).
6. **Add it** to `obot-mcp-ids.json` and run:

   ```
   python test_mcp.py --catalog-status --mcp-id <id>
   python test_mcp.py --catalog satva-newthing --via-obot --mcp-id <id>
   ```
7. **Map to a registry** in O-Bot Admin UI (one-time, not automatable).

## Why no catalog-refresh API

`pkg/api/authz/authz.go` defines `GroupAPIKey` with exactly two routes:
`GET /api/me` and `/mcp-connect/`. Catalog management lives under
`/api/mcp-catalogs/*` which is admin-only. There is no scope of
programmatic API key that can refresh catalogs from outside O-Bot —
GitOps polling is the only path. This is a deliberate design choice
in O-Bot, not a missing feature in our automation.

If that ever changes (Obot adds an admin-API-key group) we can drop the
manual `obot-mcp-ids.json` and enumerate directly.
