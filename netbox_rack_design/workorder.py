"""
Work order of a design's execution plan (PLAN-execution-steps.md Sec. 10.1, E10/E13).

:func:`build` collects everything a person on site (or an LLM writing their ticket)
needs, per step and per action, and :func:`render_markdown` turns that same dict into
a plain Markdown checklist. Both are read-only. The REST action ``work-order`` and the
UI Export are thin wrappers around these two functions.

Shape returned by :func:`build`::

    {"design": {id, title, status, sites, url},
     "removal_status": "decommissioning", "planned_status": "planned",
     "total_steps": 7,
     "steps": [{index, total_steps, title, racks, power, problems, actions}],
     "unscheduled": [action, ...],
     "peaks": [{rack, draw_w, capacity_w, step, summary}]}

    action: {order, placement_id, kind, kind_label, device{...}, from, to, power,
             power_from{cabling} (= power.cabling), power_to[{pdu, bank, feed_leg, outlet, text}],
             status_after, url, device_url, bay, chassis, planning_data, children[]}

A blade planned into a chassis of this design is listed under that chassis
(``children``), never as an action of its own. The safety data (``power``,
``problems``) comes from :func:`steps.simulate` over the SAVED order.
"""

import csv
import io

from netbox.plugins import get_plugin_config

from . import steps as steps_mod
from .choices import DesignPlacementKindChoices as Kind
from .models import DesignPlacement, PlannedRack

__all__ = ("CSV_COLUMNS", "StepNotFound", "build", "render_csv", "render_markdown")

_PLUGIN = "netbox_rack_design"


class StepNotFound(ValueError):
    """``step`` names a step the design does not have."""


# --- small helpers ----------------------------------------------------------

def _fmt_u(value):
    if value is None:
        return None
    number = float(value)
    return str(int(number)) if number == int(number) else str(number)


def _kw(watts):
    return f"{(watts or 0) / 1000:.1f}"


def _location(site, rack_name, position=None, face=None, location=None, **extra):
    out = {
        "site": site.name if site else None,
        "location": location.name if location else None,
        "rack": rack_name,
        "position": _fmt_u(position),
        "face": face or None,
    }
    out.update(extra)
    return out


def _rack_place(rack, position, face):
    return _location(rack.site, rack.name, position, face, rack.location)


def _planned_place(planned, position, face):
    location = planned.location
    return _location(location.site if location else None, planned.name, position, face, location)


def _device_url(device):
    return device.get_absolute_url() if device is not None and device.pk else None


# --- action pieces ----------------------------------------------------------

def _origin(placement):
    """The placement whose device type / name stand for an ancestor-planned device."""
    return placement.base_placement if placement.base_placement_id else None


def _device_info(placement):
    device = placement.device
    base = _origin(placement)
    dtype = (
        device.device_type if device is not None
        else placement.device_type or (base.device_type if base else None)
    )
    if placement.kind == Kind.KIND_ADD:
        name = placement.proposed_name
    elif device is not None:
        name = placement.proposed_name or device.name
    elif base is not None:
        name = placement.proposed_name or base.proposed_name
    else:
        name = placement.proposed_name or placement.stale_device_name
    name = name or (dtype.model if dtype else "")
    role = placement.resolved_role()
    return {
        "name": name,
        "current_name": device.name if device is not None else None,
        "id": device.pk if device is not None else None,
        "manufacturer": dtype.manufacturer.name if dtype else "",
        "model": dtype.model if dtype else "",
        "u_height": float(dtype.u_height) if dtype and dtype.u_height % 1 else (
            int(dtype.u_height) if dtype else None),
        "full_depth": bool(dtype.is_full_depth) if dtype else None,
        "role": role.name if role else "",
        # A real device carries its own serial/asset tag; the plugin plans neither.
        "serial": (device.serial if device is not None else "") or "",
        "asset_tag": (device.asset_tag if device is not None else "") or "",
    }


def _from(placement):
    if placement.kind == Kind.KIND_ADD:
        return None
    device = placement.device
    if device is None:
        return None
    if device.rack_id is None:
        return _location(device.site, None, location=device.location)
    return _rack_place(device.rack, device.position, device.face)


def _to(placement):
    if placement.kind == Kind.KIND_REMOVE:
        return None
    if placement.target_bay_id:
        chassis = placement.target_bay.device
        out = (_rack_place(chassis.rack, None, None) if chassis.rack_id
               else _location(chassis.site))
        out.update(chassis=chassis.name, bay=placement.target_bay_name or placement.target_bay.name)
        return out
    if placement.target_rack_id:
        return _rack_place(placement.target_rack, placement.target_position, placement.target_face)
    if placement.target_planned_rack_id:
        return _planned_place(
            placement.target_planned_rack, placement.target_position, placement.target_face)
    return None


def _real_cabling(device):
    out = []
    if device is None:
        return out
    for port in device.powerports.all():
        if not port.cable_id:
            continue
        for peer in port.link_peers:
            entry = {"port": port.name, "pdu": None, "outlet": None, "bank": "", "feed": None}
            if hasattr(peer, "device"):
                entry.update(pdu=peer.device.name, outlet=peer.name,
                             bank=getattr(peer, "feed_leg", "") or "")
            else:
                entry["feed"] = peer.name
            out.append(entry)
    return out


def _power(placement):
    feed = placement.bound_feed
    feed_info = None
    if feed is not None:
        feed_info = {
            "name": feed.name,
            "voltage": feed.voltage,
            "amperage": feed.amperage,
            "phase": feed.phase,
            "planned": bool(placement.planned_power_feed_id),
        }
    source = placement.power_source_device
    return {
        "cabling": _real_cabling(placement.device),
        "preferred_feed_legs": list(placement.preferred_feed_legs or []),
        "feed": feed_info,
        "source_device": source.name if source is not None else None,
        "power_config": placement.power_config or None,
    }


def _rack_key_of(placement):
    if placement.target_rack_id:
        return f"r:{placement.target_rack_id}"
    if placement.target_planned_rack_id:
        return f"p:{placement.target_planned_rack_id}"
    return None


def _power_to(placement, device, sim_entry):
    """
    Where the device is plugged in on the TARGET rack, as the plan's own projection
    (:func:`steps.simulate`, same subset) attributes it: one entry per PDU bank.
    No cable is planned, so the outlet is never invented (``outlet`` is None).
    """
    if placement.kind == Kind.KIND_REMOVE or sim_entry is None:
        return []
    key = _rack_key_of(placement)
    rack = sim_entry["racks"].get(key) if key else None
    pdus = ((rack or {}).get("distribution") or {}).get("pdus") or {}
    names = {n for n in (device["name"], device["current_name"]) if n}
    out = []
    for pdu_name, pdu in pdus.items():
        for bank_id, bank in (pdu.get("banks") or {}).items():
            if any(d.get("name") in names for d in bank.get("devices") or ()):
                out.append({
                    "pdu": pdu_name,
                    "bank": str(bank_id),
                    "feed_leg": pdu.get("feed_letter") or "",
                    "outlet": None,
                    "text": f"PDU {pdu_name} bank {bank_id} - any free outlet",
                })
    return out


def _status_after(placement, removal_status, planned_status):
    """Removals get ``removal_status``, adds ``planned_status``; a move keeps the status."""
    if placement.kind == Kind.KIND_REMOVE:
        return removal_status
    if placement.kind == Kind.KIND_MOVE and placement.device is not None:
        return placement.device.status
    return planned_status


def _action(placement, order, children, removal_status, planned_status, sim_entry=None):
    chassis_bay = placement.target_bay if placement.target_bay_id else None
    device = _device_info(placement)
    power = _power(placement)
    return {
        "order": order,
        "placement_id": placement.pk,
        "kind": placement.kind,
        "kind_label": placement.get_kind_display(),
        "stale": placement.stale,
        "device": device,
        "from": _from(placement),
        "to": _to(placement),
        # ``power`` is kept for old consumers; ``power_from`` is the same source cabling.
        "power": power,
        "power_from": {"cabling": power["cabling"]},
        "power_to": _power_to(placement, device, sim_entry),
        "status_after": _status_after(placement, removal_status, planned_status),
        "url": placement.get_absolute_url(),
        "device_url": _device_url(placement.device),
        "bay": placement.target_bay_name or (chassis_bay.name if chassis_bay else None),
        "chassis": chassis_bay.device.name if chassis_bay else None,
        "planning_data": placement.planning_data or None,
        "children": [
            {
                "placement_id": child.pk,
                "kind": child.kind,
                "device": _device_info(child),
                "bay": child.target_bay_name,
                "power": _power(child),
                "url": child.get_absolute_url(),
            }
            for child in children
        ],
    }


# --- safety data -------------------------------------------------------------

def _rack_names(keys):
    names = {}
    real = [int(k[2:]) for k in keys if k.startswith("r:")]
    planned = [int(k[2:]) for k in keys if k.startswith("p:")]
    from dcim.models import Rack
    for rack in Rack.objects.filter(pk__in=real):
        names[f"r:{rack.pk}"] = rack.name
    for rack in PlannedRack.objects.filter(pk__in=planned):
        names[f"p:{rack.pk}"] = rack.name
    return names


def _safety(sim_entry):
    """``(racks, power, problems)`` of one simulated step."""
    keys = list(sim_entry["racks"])
    names = _rack_names(keys)
    rows = sorted((names.get(k, k), k) for k in keys)
    power, problems = [], []
    for name, key in rows:
        data = sim_entry["racks"][key]
        info = data.get("power") or {}
        draw, cap = info.get("draw_w") or 0.0, info.get("capacity_w") or 0.0
        power.append({
            "rack": name,
            "key": key,
            "draw_w": float(draw),
            "capacity_w": float(cap),
            "util_pct": float(info.get("util_pct") or 0.0),
            "state": info.get("state") or "",
            "summary": f"{name} at {_kw(draw)}/{_kw(cap)} kW",
        })
        problems += [dict(p, rack=name) for p in data.get("problems") or []]
    return [n for n, _ in rows], power, problems


# --- build ------------------------------------------------------------------

def _fetch(design):
    return list(
        DesignPlacement.objects.filter(design=design)
        .select_related(
            "device", "device__rack", "device__rack__site", "device__rack__location",
            "device__site", "device__location", "device__device_type",
            "device__device_type__manufacturer", "device__role",
            "device_type", "device_type__manufacturer", "device_role",
            "target_rack", "target_rack__site", "target_rack__location",
            "target_planned_rack", "target_planned_rack__location",
            "target_planned_rack__location__site",
            "target_bay", "target_bay__device", "target_bay__device__rack",
            "target_bay__device__site",
            "base_placement", "base_placement__device_type",
            "base_placement__device_type__manufacturer",
            "power_source_device", "real_power_feed", "planned_power_feed",
        )
        .order_by("step__index", "step_order", "pk")
    )


def build(design, step=None):
    """
    The work order of ``design``: all steps, or only step number ``step``.
    Raises :class:`StepNotFound` for a step the design lacks.
    """
    removal_status = get_plugin_config(_PLUGIN, "removal_status")
    planned_status = get_plugin_config(_PLUGIN, "planned_status")

    plan_steps = list(design.steps.order_by("index"))
    if step is not None and step not in {s.index for s in plan_steps}:
        raise StepNotFound(f"{design} has no step {step}.")

    placements = _fetch(design)
    children = {}
    top = []
    for p in placements:
        if p.parent_placement_id:
            children.setdefault(p.parent_placement_id, []).append(p)
        elif not p.base_parent_placement_id:
            top.append(p)

    by_step = {}
    unscheduled = []
    for p in top:
        (by_step.setdefault(p.step_id, []) if p.step_id else unscheduled).append(p)

    # The simulation takes the SAVED order: one id list per step, in step order.
    order = [[p.pk for p in by_step.get(s.pk, ())] for s in plan_steps]
    sim = {}
    if plan_steps:
        low = step or 1
        for entry in steps_mod.simulate(design, order, from_step=low, to_step=step):
            sim[entry["index"]] = entry

    def actions(items, sim_entry=None):
        return [
            _action(p, n, children.get(p.pk, ()), removal_status, planned_status, sim_entry)
            for n, p in enumerate(items, start=1)
        ]

    out_steps, peaks = [], {}
    for pos, s in enumerate(plan_steps, start=1):
        if step is not None and s.index != step:
            continue
        racks, power, problems = _safety(sim[pos]) if pos in sim else ([], [], [])
        for row in power:
            best = peaks.get(row["rack"])
            if best is None or row["draw_w"] > best["draw_w"]:
                peaks[row["rack"]] = {
                    "rack": row["rack"], "draw_w": row["draw_w"],
                    "capacity_w": row["capacity_w"], "step": s.index,
                    "summary": f"{row['rack']} peaks at {_kw(row['draw_w'])}/"
                               f"{_kw(row['capacity_w'])} kW (step {s.index})",
                }
        out_steps.append({
            "index": s.index,
            "total_steps": len(plan_steps),
            "title": s.title,
            "racks": racks,
            "power": power,
            "problems": problems,
            "actions": actions(by_step.get(s.pk, ()), sim.get(pos)),
        })

    return {
        "design": {
            "id": design.pk,
            "title": design.title,
            "status": design.status,
            "sites": [site.name for site in design.sites.all()],
            "url": design.get_absolute_url(),
        },
        "removal_status": removal_status,
        "planned_status": planned_status,
        "total_steps": len(plan_steps),
        "steps": out_steps,
        "unscheduled": actions(unscheduled) if step is None else [],
        "peaks": sorted(peaks.values(), key=lambda r: r["rack"]),
    }


# --- markdown ---------------------------------------------------------------

def _place(loc):
    if not loc:
        return "-"
    if loc.get("chassis"):
        text = f"chassis {loc['chassis']} bay {loc.get('bay')}"
    else:
        text = loc["rack"] or "no rack"
        if loc.get("position"):
            text += f" U{loc['position']}"
        if loc.get("face"):
            text += f" {loc['face']}"
    where = " / ".join(x for x in (loc.get("site"), loc.get("location")) if x)
    return f"{text} ({where})" if where else text


def _device_line(device):
    bits = [" ".join(x for x in (device["manufacturer"], device["model"]) if x)]
    if device["u_height"] is not None:
        bits.append(f"{device['u_height']}U")
    if device["full_depth"] is not None:
        bits.append("full-depth" if device["full_depth"] else "half-depth")
    return ", ".join(b for b in bits if b)


def _power_lines(power, cabling_label="Power"):
    """``(label, text)`` pairs; the cabling lines carry ``cabling_label``."""
    lines = []
    for c in power["cabling"]:
        if c["pdu"]:
            bank = f", bank {c['bank']}" if c["bank"] else ""
            lines.append((cabling_label, f"{c['port']} on PDU {c['pdu']} outlet {c['outlet']}{bank}"))
        elif c["feed"]:
            lines.append((cabling_label, f"{c['port']} on feed {c['feed']}"))
    if power["feed"]:
        f = power["feed"]
        kind = "planned feed" if f["planned"] else "feed"
        lines.append(("Power", f"{kind} {f['name']} ({f['voltage']} V, {f['amperage']} A)"))
    if power["preferred_feed_legs"]:
        lines.append(("Power", "feed legs " + "+".join(power["preferred_feed_legs"])))
    if power["source_device"]:
        lines.append(("Power", f"PDU data from {power['source_device']}"))
    return lines


def _action_md(act, indent=""):
    dev = act["device"]
    head = f"{indent}{act['order']}. [ ] **{act['kind_label']}** {dev['name']}"
    if dev["current_name"] and dev["current_name"] != dev["name"]:
        head += f" (now {dev['current_name']})"
    details = _device_line(dev)
    if details:
        head += f" - {details}"
    lines = [head]
    pad = indent + "   "
    if act["from"]:
        lines.append(f"{pad}- From: {_place(act['from'])}")
    if act["to"]:
        lines.append(f"{pad}- To: {_place(act['to'])}")
    ids = [f"Serial {dev['serial']}" if dev["serial"] else "",
           f"asset tag {dev['asset_tag']}" if dev["asset_tag"] else ""]
    ids = [i for i in ids if i]
    if ids:
        lines.append(f"{pad}- " + ", ".join(ids))
    for label, text in _power_lines(act["power"], "Unplug from"):
        lines.append(f"{pad}- {label}: {text}")
    for entry in act.get("power_to") or ():
        lines.append(f"{pad}- Plug into: {entry['text']}")
    lines.append(f"{pad}- Status after: {act['status_after']}")
    if act["device_url"]:
        lines.append(f"{pad}- NetBox: {act['device_url']}")
    for child in act["children"]:
        cdev = child["device"]
        lines.append(
            f"{pad}- [ ] Blade {cdev['name']} ({_device_line(cdev)}) into bay {child['bay']}")
        for label, text in _power_lines(child["power"]):
            lines.append(f"{pad}  - {label}: {text}")
    return lines


def render_markdown(data):
    """A plain Markdown checklist of ``data`` (the dict from :func:`build`)."""
    design = data["design"]
    out = [f"# {design['title']}", ""]
    meta = [f"Sites: {', '.join(design['sites'])}" if design["sites"] else "",
            f"Status: {design['status']}", f"NetBox: {design['url']}"]
    out.append(" | ".join(m for m in meta if m))
    out.append("")
    for s in data["steps"]:
        total = s.get("total_steps") or data.get("total_steps")
        title = f"Step {s['index']}" + (f" of {total}" if total else "") + (f" – {s['title']}" if s["title"] else "")
        out.append(f"## {title}")
        if s["racks"]:
            out.append(f"Racks: {', '.join(s['racks'])}")
        out.append("")
        if s["power"]:
            out.append("Power after this step:")
            out += [f"- {p['summary']} ({p['util_pct']:.0f}%, {p['state'] or 'ok'})"
                    for p in s["power"]]
            out.append("")
        if s["problems"]:
            out.append("Problems:")
            out += [f"- [{p['severity']}] {p['code']}: {p['detail']}" for p in s["problems"]]
            out.append("")
        for act in s["actions"]:
            out += _action_md(act)
        if not s["actions"]:
            out.append("_No actions._")
        out.append("")
    if data["unscheduled"]:
        out += ["## Unscheduled", "", "Not part of any step yet.", ""]
        for act in data["unscheduled"]:
            out += _action_md(act)
        out.append("")
    return "\n".join(out).rstrip() + "\n"


# --- csv --------------------------------------------------------------------

CSV_COLUMNS = (
    "step", "step_title", "order", "kind", "device", "type",
    "from_rack", "from_u", "from_face", "to_rack", "to_u", "to_face",
    "pdu", "outlet", "bank", "to_pdu", "to_bank", "to_outlet", "status_after",
)


def _csv_row(step, title, order, act, child=None):
    src = child or act
    device = src["device"]
    frm = (None if child else act["from"]) or {}
    to = act["to"] or {}
    cabling = (src["power"] or {}).get("cabling") or []
    source = (src["power"] or {}).get("source_device")
    dest = [] if child else (act.get("power_to") or [])
    pdus = [c["pdu"] for c in cabling if c.get("pdu")] or ([source] if source else [])
    return {
        "step": step if step is not None else "",
        "step_title": title or "",
        "order": order,
        "kind": src["kind"],
        "device": device["name"],
        "type": device["model"],
        "from_rack": frm.get("rack") or "",
        "from_u": frm.get("position") or "",
        "from_face": frm.get("face") or "",
        "to_rack": to.get("rack") or "",
        "to_u": "" if child else (to.get("position") or ""),
        "to_face": "" if child else (to.get("face") or ""),
        "pdu": "; ".join(pdus),
        "outlet": "; ".join(c["outlet"] for c in cabling if c.get("outlet")),
        "bank": "; ".join(c["bank"] for c in cabling if c.get("bank")),
        "to_pdu": "; ".join(e["pdu"] for e in dest),
        "to_bank": "; ".join(e["bank"] for e in dest),
        "to_outlet": "; ".join(e["outlet"] or "any free outlet" for e in dest),
        "status_after": act["status_after"],
    }


def render_csv(data):
    """One CSV row per action (blades under their chassis), columns :data:`CSV_COLUMNS`."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    groups = [(s["index"], s["title"], s["actions"]) for s in data["steps"]]
    groups.append((None, "", data["unscheduled"]))
    for index, title, actions in groups:
        for act in actions:
            writer.writerow(_csv_row(index, title, act["order"], act))
            for child in act["children"]:
                writer.writerow(_csv_row(index, title, act["order"], act, child))
    return buf.getvalue()
