"""FastMCP stdio server. Read-only: nothing here changes a design in NetBox."""

from __future__ import annotations

import json
import logging
import sys

from mcp.server.fastmcp import FastMCP

from .client import NetBoxClient

# stdout carries the MCP protocol; every log line goes to stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # its request lines would show URLs
log = logging.getLogger("netbox_rack_design_mcp")

DEFAULT_TICKET_FORMAT = """\
DEFAULT SMART-HANDS TICKET FORMAT (use it only when the user gave no format of their own):
1. Header: design, step (number and title), site, rack(s), maintenance window.
2. Safety notes: power headroom per rack from the simulation ("R12 peaks at 6.7/8 kW"),
   devices that stay live, the PDU and bank touched, any problems the plan reports.
3. Numbered actions in the order given, each with device, from/to rack and U, serial and
   asset tag, power: for a move or add, "Unplug from" power_from (the current PDU outlets) and
   "Plug into" power_to (the target rack's PDU and bank; the outlet is "any free outlet"
   when no cable is planned).
4. A photo or check per action (for example "photo of R12 front after U20 is installed").
5. A rollback line: how to undo the step if something goes wrong.
6. What to report back: serials, photos, any deviation from the plan."""

mcp = FastMCP("rack-design")

_client: NetBoxClient | None = None


def get_client() -> NetBoxClient:
    global _client
    if _client is None:
        _client = NetBoxClient.from_env()
    return _client


def set_client(client: NetBoxClient | None) -> None:
    """Install a ready client (tests)."""
    global _client
    _client = client


@mcp.tool()
def list_designs(site: str | None = None, status: str | None = None) -> dict:
    """List rack designs. Read-only.

    site: site slug to filter by (for example "ams1"). status: design status
    (for example "draft", "approved", "implemented")."""
    data = get_client().get("designs/", site=site, status=status, limit=100)
    return {
        "count": data["count"],
        "designs": [
            {k: d.get(k) for k in ("id", "title", "status", "sites", "version", "url", "description")}
            for d in data["results"]
        ],
    }


@mcp.tool()
def get_design(design_id: int) -> dict:
    """Get one design with all its fields. Read-only."""
    return get_client().get(f"designs/{design_id}/")


def _saved_order(placements: list[dict]) -> dict[int, list[tuple[int, int]]]:
    """Placement ids per step, in saved order. Blades follow their chassis and are not listed."""
    by_step: dict[int, list[tuple[int, int]]] = {}
    for p in placements:
        step = p.get("step")
        if not step or p.get("parent_placement") or p.get("base_parent_placement"):
            continue
        by_step.setdefault(step["index"], []).append((p.get("step_order") or 0, p["id"]))
    return by_step


def _prune(value):
    """Drop null / empty values (False and 0 stay): they carry no information for a ticket."""
    if isinstance(value, dict):
        out = {k: _prune(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not None and v != [] and v != {} and v != ""}
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


_BAD_BANK_STATES = ("warn", "critical", "overload")


def _where(loc: dict | None) -> dict | None:
    """A from/to location reduced to rack, U, face (and chassis/bay for a blade)."""
    if not loc:
        return None
    out = {k: loc.get(k) for k in ("rack", "position", "face", "chassis", "bay") if loc.get(k)}
    if "position" in out:
        out["u"] = out.pop("position")
    return out or None


def _action_summary(act: dict) -> dict:
    out = {
        "placement_id": act.get("placement_id"),
        "kind": act.get("kind"),
        "device": (act.get("device") or {}).get("name"),
        "from": _where(act.get("from")),
        "to": _where(act.get("to")),
    }
    if act.get("children"):
        out["blades"] = [(c.get("device") or {}).get("name") for c in act["children"]]
    return out


def _lookup(client: NetBoxClient, design_id: int) -> tuple[dict, dict, list]:
    """(rack key -> name, placement id -> compact action, unscheduled compact actions),
    from the saved work order."""
    order = client.get(f"designs/{design_id}/work-order/", output="json")
    names: dict[str, str] = {}
    actions: dict[int, dict] = {}
    for s in order.get("steps", []):
        for row in s.get("power", []):
            names[row["key"]] = row["rack"]
        for act in s.get("actions", []):
            actions[act["placement_id"]] = _action_summary(act)
    unscheduled = [_action_summary(a) for a in order.get("unscheduled", [])]
    for a in unscheduled:
        actions[a["placement_id"]] = a
    return names, actions, unscheduled


def _compact_sim(entry: dict, names: dict[str, str]) -> dict:
    """One simulated step: per rack draw/capacity/util/state, only the hot banks, problems."""
    racks, problems = [], []
    for key, data in sorted((entry.get("racks") or {}).items(), key=lambda kv: names.get(kv[0], kv[0])):
        name = names.get(key, key)
        power = data.get("power") or {}
        banks = []
        for pdu_name, pdu in ((data.get("distribution") or {}).get("pdus") or {}).items():
            for bank_id, bank in (pdu.get("banks") or {}).items():
                if bank.get("state") in _BAD_BANK_STATES:
                    banks.append({
                        "pdu": pdu_name, "bank": bank_id, "state": bank["state"],
                        "load_w": round((bank.get("allocated_power") or 0) + (bank.get("planned_power") or 0)),
                        "limit_w": round(bank.get("max_power") or 0),
                        "pct": round(bank.get("util_pct") or 0),
                    })
        racks.append({
            "rack": name,
            "draw_w": round(power.get("draw_w") or 0),
            "capacity_w": round(power.get("capacity_w") or 0),
            "util_pct": round(power.get("util_pct") or 0),
            "state": power.get("state") or "",
            "hot_banks": banks,
        })
        # keep EVERY problem (bank_over / bank_near too); hot_banks is extra detail.
        problems += [
            {"rack": name, "code": p.get("code"), "severity": p.get("severity"), "detail": p.get("detail")}
            for p in data.get("problems") or []
        ]
    return {"racks": racks, "problems": problems}


@mcp.tool(
    description=(
        "Get a design's execution plan as a COMPACT SUMMARY: per step its index, title and "
        "actions (kind, device, from/to rack and U), plus the simulated power of each touched "
        "rack (draw_w, capacity_w, util_pct, state), only the PDU banks that are warn/critical/"
        "overload (hot_banks), and the problems found (code, severity, detail). Raw "
        "distribution and per-outlet data are left out. Call it with detail=true ONLY when you "
        "need the full simulation payload (every bank and outlet); that answer is very large "
        "(>100k characters). For a technician ticket use get_work_order instead. Read-only. "
        "This tool makes one POST (simulate-steps) with the SAVED order; that POST only "
        "computes the simulation and changes nothing in NetBox."
    )
)
def get_execution_plan(design_id: int, detail: bool = False) -> dict:
    client = get_client()
    steps = sorted(client.get("design-steps/", design_id=design_id, limit=0)["results"],
                   key=lambda s: s["index"])
    placements = client.get("placements/", design_id=design_id, limit=0)["results"]
    by_step = _saved_order(placements)
    ordered = [[pid for _, pid in sorted(by_step.get(s["index"], []))] for s in steps]
    simulation = (client.post(f"designs/{design_id}/simulate-steps/", {"steps": ordered}).get("steps", [])
                  if ordered else [])
    if detail:
        names = {p["id"]: p.get("display") for p in placements}
        return {
            "design_id": design_id,
            "steps": [
                {
                    "index": s["index"],
                    "title": s.get("title", ""),
                    "actions": [{"placement_id": pid, "display": names.get(pid)} for pid in ordered[i]],
                }
                for i, s in enumerate(steps)
            ],
            "unscheduled": [
                {"placement_id": p["id"], "display": p.get("display")}
                for p in placements
                if not p.get("step") and not p.get("parent_placement") and not p.get("base_parent_placement")
            ],
            "simulation": simulation,
        }
    rack_names, actions, unscheduled = _lookup(client, design_id)
    by_index = {e["index"]: e for e in simulation}
    out_steps = []
    for i, s in enumerate(steps, start=1):
        row = {
            "index": s["index"],
            "title": s.get("title", ""),
            "actions": [actions.get(pid, {"placement_id": pid}) for pid in ordered[i - 1]],
        }
        row.update(_compact_sim(by_index.get(i, {}), rack_names))
        out_steps.append(row)
    return {"design_id": design_id, "total_steps": len(steps), "steps": out_steps,
            "unscheduled": unscheduled}


@mcp.tool(
    description=(
        "Get the work order of a design: every step (or only `step`) with its actions in the "
        "safe order. Per action: kind (add/move/remove), device name, type, U height, "
        "full-depth flag, serial, asset tag, from and to (site, location, rack, U, face), "
        "power cabling (power_from: the PDU outlets the device leaves; power_to: the PDU and bank it "
        "plugs into on the target rack, outlet \"any free outlet\" unless cabled), the target status afterwards and a NetBox "
        "link; per step: its index, total_steps (say \"Step 1 of 7\", never \"1 of 1\", even "
        "when you ask for one step), the power headroom per rack and the problems the "
        "simulation found. Unscheduled actions come in a separate list. This is already the "
        "compact, technician-ready payload. Read-only.\n\n"
        "The user's own instructions about the ticket (format, language, destination, fields) "
        "ALWAYS win. Only when the user gave none, write a smart-hands ticket like this:\n"
        + DEFAULT_TICKET_FORMAT
    )
)
def get_work_order(design_id: int, step: int | None = None) -> dict:
    return _prune(get_client().get(f"designs/{design_id}/work-order/", step=step, output="json"))


@mcp.tool(
    description=(
        "What-if: simulate a PROPOSED order of placements without saving it. steps is a list "
        "of steps, each a list of placement ids (blades are not listed, they follow their "
        "chassis). Returns a COMPACT SUMMARY per step: actions, per touched rack draw_w/"
        "capacity_w/util_pct/state, only the warn/critical/overload banks (hot_banks) and "
        "problems (rack_over, bank_over, u_conflict, unpowered ...). Pass detail=true ONLY "
        "when you need the raw simulation (every bank and outlet; >100k characters). This is "
        "a POST, but it only computes a result: it changes nothing in NetBox."
    )
)
def simulate_order(design_id: int, steps: list[list[int]], detail: bool = False) -> dict:
    client = get_client()
    raw = client.post(f"designs/{design_id}/simulate-steps/", {"steps": steps})
    if detail:
        return raw
    rack_names, actions, _ = _lookup(client, design_id)
    out_steps = []
    for entry in raw.get("steps", []):
        i = entry["index"]
        row = {
            "index": i,
            "actions": [actions.get(pid, {"placement_id": pid}) for pid in (steps[i - 1] if i <= len(steps) else [])],
        }
        row.update(_compact_sim(entry, rack_names))
        out_steps.append(row)
    return {"design_id": design_id, "total_steps": len(steps), "steps": out_steps}


@mcp.prompt()
def smart_hands_ticket(design_id: int, step: int, instructions: str = "") -> str:
    """Write a smart-hands ticket for one step of a design's plan (Claude Code)."""
    order = _prune(get_client().get(f"designs/{design_id}/work-order/", step=step, output="json"))
    if instructions.strip():
        rules = ("Follow these instructions from the user for the ticket; they replace any "
                 "default format:\n" + instructions.strip())
    else:
        rules = "The user gave no format. " + DEFAULT_TICKET_FORMAT
    return (
        f"Write a smart-hands ticket for step {step} of design {design_id}.\n\n{rules}\n\n"
        f"Work order (JSON):\n{json.dumps(order, indent=2)}"
    )


def main() -> None:
    log.info("starting netbox-rack-design-mcp (stdio)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
