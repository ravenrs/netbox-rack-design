# Claude and smart-hands work orders (MCP)

▶ Video: [What's new in 1.2.0](https://youtu.be/8cgy2hknDYY) — the MCP part starts at [8:45](https://youtu.be/8cgy2hknDYY?t=525).

`netbox-rack-design-mcp` lets Claude read a design's execution plan and write the
smart-hands ticket for a step. It is read-only: Claude can list designs, read the plan
and its power simulation, and fetch the work order. It cannot reorder steps or apply.

## Why our own server

NetBox Labs' `netbox-mcp-server` is read-only GET over a fixed registry of core object
types. Its plugin discovery adds list and get of plugin *models* only, so it cannot
reach the `work-order` action or the `simulate-steps` POST of this plugin. Generic
OpenAPI bridges expose thousands of NetBox operations and carry no ticket guidance. The
official server stays useful next to ours for browsing core DCIM data (devices, racks,
cables); install both if you want that.

## Where to get it

The server is a small Python package, `netbox-rack-design-mcp`:

- on PyPI: <https://pypi.org/project/netbox-rack-design-mcp/> (published with plug-in
  release 1.2.0, so it is available once 1.2.0 is out);
- its source is the `mcp/` folder of this repository.

## Prerequisites

- **Claude Code** or **Claude Desktop**.
- **uv** (it provides `uvx`) or **pipx**. `uvx` fetches and runs the package from PyPI
  on first use, so there is nothing to install by hand.
- A NetBox with this plug-in (1.2.0 or newer) and a user who can see the designs.

## Step 1: create a read-only API token

In NetBox open the user menu (top right), **API Tokens**, **Add a Token**:

1. Untick **Write enabled**. This is what makes the token read-only.
2. Add a description, for example "Claude, read-only", and click **Create**.
3. Copy the token. NetBox shows it **once**; keep it out of chat messages and tickets.

The user the token belongs to needs the `view` permission on the Rack Design objects
(designs, placements, steps). No tool here needs more, and none of them writes.

## Step 2: register the server

### Claude Code

```
claude mcp add --env NETBOX_URL=https://netbox.example.com --env NETBOX_TOKEN=$NETBOX_TOKEN --transport stdio rack-design -- uvx netbox-rack-design-mcp
```

Put your token in the `NETBOX_TOKEN` environment variable first (or paste it in place
of `$NETBOX_TOKEN`). With pipx instead of uvx: `pipx install netbox-rack-design-mcp`,
then register the command `netbox-rack-design-mcp`.

### Claude Desktop

In `claude_desktop_config.json` (Settings, Developer, Edit config), then restart the app:

```json
{"mcpServers":{"rack-design":{"command":"uvx","args":["netbox-rack-design-mcp"],"env":{"NETBOX_URL":"https://netbox.example.com","NETBOX_TOKEN":"<your token>"}}}}
```

### Settings

The server reads two environment variables: `NETBOX_URL` and `NETBOX_TOKEN`. Set
`NETBOX_VERIFY_SSL=false` only for a lab instance with a self-signed certificate. Logs
go to stderr, and the token is never logged.

## Step 3: check that it works

- Claude Code: run `claude mcp list`; `rack-design` should show as connected. Inside a
  session, `/mcp` shows the server and its tools.
- Ask Claude "List the rack-design tools you have". It should name five tools:
  `list_designs`, `get_design`, `get_execution_plan`, `get_work_order`, `simulate_order`.
- Then try "Which steps in the execution plan of design <title> are risky?"

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Connection refused, or the server shows as failed | `NETBOX_URL` is wrong or unreachable from the machine running Claude. Open it in a browser from that machine; include `https://` and no trailing path. |
| 403 Forbidden | The token's user lacks the `view` permission on the Rack Design objects (or the design's site is outside the user's constraints). Grant it and retry. |
| 401 / "Invalid token" | The token was copied wrongly, expired, or is disabled. Create a new one. A v2 token starts with `nbt_`; keep the whole `nbt_key.secret` string. |
| Certificate error | A self-signed NetBox: set `NETBOX_VERIFY_SSL=false` (lab only). |
| `uvx: command not found` | Install uv (<https://docs.astral.sh/uv/>), or use pipx. |
| A tool says the design has no saved plan | Open the design's **Execution plan** tab and click **Create plan** once. |

## Tools

| Tool | What it returns |
|---|---|
| `list_designs(site=None, status=None)` | Designs, filtered by site slug and status. |
| `get_design(design_id)` | One design. |
| `get_execution_plan(design_id, detail=false)` | A compact summary: per step the actions (kind, device, from/to rack and U), per touched rack draw/capacity/util/state, only the warn/critical/overload banks, and the problems. `detail=true` returns the full simulation payload (very large). It makes one POST to `simulate-steps` with the saved order; the POST changes nothing. |
| `get_work_order(design_id, step=None)` | The work order: per action the device, serial, asset tag, from and to (rack, U, face), power cabling, status afterwards and a NetBox link; per step the power headroom and problems. |
| `simulate_order(design_id, steps, detail=false)` | "What if" for a proposed order of placement ids; compact summary like `get_execution_plan`, `detail=true` for the raw result. A POST that changes nothing. |

Claude Code also gets the prompt `smart_hands_ticket(design_id, step, instructions="")`,
shown as `/mcp__rack-design__smart_hands_ticket`. Claude Desktop handles MCP prompts
poorly, so the default format below is also written into the `get_work_order` tool
description, which every client reads.

## The ticket format is yours

The plugin and the MCP server only supply data. Where the ticket goes and how it looks
is up to your instructions; they always win over the default. Put them in a Claude
project, a skill, or simply in the chat.

**Example only** (describes a made-up team's format, copy and adapt):

```
Write the ticket in Jira wiki markup for the DC-OPS project.
Title: "[<site>] <design title> - step <n>". One sub-task line per action.
Always give serial numbers in a table: device | serial | rack U.
Add the on-call number +31 20 000 0000 at the bottom.
Skip the rollback section for steps that only remove devices.
```

### Default format

When you give no instructions, Claude writes:

1. **Header**: design, step, site, rack(s), maintenance window.
2. **Safety notes**: power headroom per rack from the simulation ("R12 peaks at
   6.7/8 kW"), devices that stay live, the PDU and bank touched, any problems the plan
   reports.
3. **Numbered actions** in the safe order, each with device, from/to rack and U, serial
   and asset tag, power cabling.
4. **A photo or check per action**, for example "photo of R12 front after U20 is
   installed".
5. **A rollback line**: how to undo the step if something goes wrong.
6. **What to report back**: serials, photos, any deviation from the plan.
