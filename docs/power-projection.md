# Power projection

▶ Video: [Part 7 — Power projection & rebalancing](https://youtu.be/jge_MjSdNxQ)

Every rack in the editor shows how much power it **will** draw once the design
is applied — before anything is cabled. The numbers are read-only and follow
every edit live: drop, move or remove a device and the bar changes on release,
no Save needed. Nothing is written to DCIM.

This page covers the rack-level view: the power bar and the heatmap. Where the
load lands per PDU and per breaker bank is [Power distribution](power-distribution.md).

## The power bar

Directly under each rack's name:

```
5600 / 11776 W · 48% ⚠ 4
```

- **Draw** — the sum of every device the rack will hold: its current devices,
  minus the ones this design removes or moves out, plus the ones it adds or
  moves in. A moved device's draw leaves one rack and arrives in the other.
- **Capacity** — the rack's power feeds, each counted the way NetBox counts a
  feed's available power (volts × amps × phases, derated by
  `POWERFEED_DEFAULT_MAX_UTILIZATION`). [Planned feeds](planned-racks.md#power-on-a-planned-rack)
  count exactly like real ones. A rack with **no feeds at all** uses
  `power_capacity_default_w` (1000 W) as a stand-in, so a greenfield rack still
  has a bar.
- **Utilization** — draw / capacity. The bar turns amber at `power_warn_pct`
  (80 %) and red at `power_critical_pct` (100 %).
- **⚠ N** — devices with a power port that isn't cabled to anything. Hover the
  ⚠ for their names.

### Where a device's draw comes from

In order: the device's own power ports (`allocated_draw`, or `maximum_draw`
if you set `power_draw_basis = "maximum"` — each falls back to the other when
unset), then its device type's power port templates. Two special cases:

- **Unknown draw** — the device has power ports but none of them carries a
  draw value. It counts as 0 W but is **flagged** (hatched in the heatmap,
  named in the hover), because missing data isn't the same as zero.
- **Passive** — no power ports at all (patch panels, cable managers, blanking
  panels). It draws nothing and is never flagged.

Devices whose role is in `power_exclude_roles` (by default `pdu` and
`unmanageable-pdu`) are power infrastructure, not load: they never count
toward draw. **Give a PDU the PDU role** — one planned under another role counts its
own rating as consumption and the bar goes red.

## The heatmap

Tick **Power heatmap** on the toolbar. Normal tile colours are replaced by a
green → red scale: each tile's colour is its draw as a **share of the biggest
consumer in its own rack**. The heaviest device in a rack is red; one drawing
half as much is amber. The scale is per rack, so the same switch can read
amber in one rack and green in another that holds heavier servers. Unknown-draw
devices are hatched, not green.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `power_capacity_default_w` | `1000` | Capacity of a rack with no feeds |
| `power_draw_basis` | `"allocated"` | `"allocated"` or `"maximum"` draw from power ports |
| `power_warn_pct` | `80` | Utilization that turns the bar amber |
| `power_critical_pct` | `100` | Utilization that turns the bar red |
| `power_exclude_roles` | `("pdu", "unmanageable-pdu")` | Role slugs that never count as load |

All are optional keys under `PLUGINS_CONFIG["netbox_rack_design"]`.

## Related

- [Power distribution](power-distribution.md) — per-PDU / per-bank chips, bank
  zones, feed binding, the three distribution modes.
- [Planned racks](planned-racks.md) — supply for a rack that doesn't exist yet.
- [Power projection spec](power-projection-spec.md) — the design notes behind it.
