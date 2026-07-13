# Power projection — specification

Status: **draft / active cycle** (started 2026-07-13). Authoritative spec for the
power feature, in the same spirit as `docs/editor-behavior-spec.md`. Definition
of done = this spec's contracts covered by tests (backend) and the conformance
items below verified live.

## 0. Principle

Power projection is **read-only** and reflects the **planned world** — the
design applied hypothetically (existing − removes + adds, moves reassigned to
their target rack), the same world `projection.py` already builds. It never
writes to `dcim`. It reuses NetBox's native power primitives where they exist
(`PowerPort` / `PowerPortTemplate` draw, `PowerFeed.available_power`,
`Rack.get_power_utilization`) rather than reinventing the electrical model; the
plugin's value-add is projecting those numbers onto a not-yet-applied design and
distributing them per PDU/bank.

## 1. Per-device draw (watts)

Resolved per planned device, in order:

1. **Planned add** (no real device): sum the device-type's
   `PowerPortTemplate.allocated_draw` (fallback `maximum_draw`).
2. **Existing / moved** (real device): sum its `PowerPort.allocated_draw`
   (fallback to the type template, then `maximum_draw`).
3. **Unknown / unaccounted** (device HAS power ports, or its type defines port
   templates, but none carry a draw value): counted as **0 W but flagged** — the
   result carries `unknown_draw_count` **and** `unknown_devices` (their names) so
   the UI can say *which* powered devices lack draw data, not silently
   under-report.
4. **Passive** (no power ports at all — patch panels, cable managers, blanking
   panels): draws nothing by design, so it is **neither counted nor flagged**
   (it is not "missing" data). Treated as a known 0 for the heatmap.

## 2. Three calculation tiers

The tier is chosen by config; each is a strict superset of information over the
one before.

### Tier 1 — crude, zero-config (default: "тапорно")

No distribution. Per rack:

```
draw_w      = Σ per-device draw over the planned devices in the rack
capacity_w  = Σ PowerFeed.available_power for the rack's feeds,
              else config `power_capacity_default_w` (fallback)
util_pct    = draw_w / capacity_w * 100
state       = ok | warn | critical      (thresholds below)
```

Plus a design-level total (Σ racks). This is the always-on baseline — works on a
stock instance with nothing modeled beyond device types.

### Tier 2 — config-driven distribution across PDUs and feeds/banks

Report utilization **per feed/bank**, not just per rack. A device's draw lands on
the feed(s) its PSUs connect to; a "bank" is a PDU input/phase/breaker group.

**Distribution model (by PSU count) — the core rule:**

| PSUs (PowerPorts) | Feeds it touches | Per-feed charge |
|---|---|---|
| **1** | the one feed it's cabled/assigned to | **full** device draw on that one feed (single point of failure; never split) |
| **2 (A/B)** | feed A + feed B | **full** device draw charged to *each* feed (redundant sizing: either feed must carry it alone on failover) |
| **N** | its N feeds | full draw on each of the redundant feeds it participates in |

So a single PSU is **never smeared across two feeds** — it sits entirely on one.
Redundant PSUs charge each feed the full load (worst-case failover), not a split.

**Feed assignment source (in order):**
1. Real NetBox power cabling where modeled (`PowerPort` → `PowerOutlet` →
   `PowerFeed`) — authoritative.
2. Config rule for planned/uncabled devices (e.g. alternate A/B by PSU index).

**Config sketch:**

```python
"power_distribution": {
    "redundancy": "full",          # "full" (each feed sized for whole draw) | "shared"
    "assign": "alternate_ab",      # fallback A/B assignment when no cabling
    # optional per-feed/bank capacity overrides
},
```

**Output:** adds a `feeds` breakdown (per feed: draw_w, capacity_w, util_pct,
state) alongside the rack summary. Racks with no distribution config fall back to
Tier 1 (rack total only) transparently.

### Tier 3 — pluggable power script

Mirrors the naming engine exactly. A config dotted path
`"power_script": "scripts.power.distribute"` points at a read-only callable that
receives the planned world (rack + its devices + draws) and returns the
distribution (per-PDU/bank allocation, redundancy/phase logic the config table
can't express).

- Same **graceful fallback** contract as naming: an unresolvable/raising power
  script logs a warning and **falls back to Tier 1 crude**, never erroring the
  page (power is read-only overlay data; a bad script must not break the editor).
- Same SCRIPTS_ROOT story: the script can live in `scripts/` and be edited via
  the NetBox UI; a shipped generic example demonstrates the contract.

## 3. Heatmap toggle (the "галка")

A legend/toolbar checkbox **"Power heatmap"** in the editor (and optionally the
read-only elevation view). Default **off**.

When **ON**:

- **Normal device styling is neutralized** — the state colors (existing / add /
  move / remove / shadow tints) are suppressed so they don't compete with the
  heat colors.
- **Every device tile is colored on a green→red gradient** by its **share of the
  rack's total draw**: `share = device_draw / rack_total_draw`. Low consumers →
  green, high consumers → red. (Share is of the rack's total consumption, per the
  requirement — "от общего кол-ва стойки".)
- Devices with **unknown draw** get a distinct neutral shade (e.g. hatched grey),
  not green — absence of data ≠ zero consumption.
- A **gradient legend** replaces/augments the state legend while active.

When **OFF**: normal styling is restored exactly (the heatmap is a pure view
layer over the same tiles — no state change, no dirty flag, nothing saved).

## 4. Where the numbers come from / surfaces

- Computed **server-side in `projection.py`**, returned as a `power` block on the
  projection bundle so both the editor and the read-only elevation view read the
  same numbers.
- v1 recompute cadence: **on load and after Save** (no mid-drag live recompute).
  The heatmap uses the last computed bundle; a later phase may recompute live.

## 5. Out of scope (v1)

- Live mid-drag power recompute.
- Breaker-trip / inrush / power-factor modeling beyond NetBox's own fields.
- Automatic A/B redundancy inference without config or script (Tier 3 can do it).

## 6. Implementation phases (test-first, one at a time)

- **A. Backend Tier 1** — per-device draw resolution + per-rack/design crude
  projection in `projection.py`, `power` bundle block, `unknown_draw_count`.
  Backend tests: draw resolution (add-via-template, existing-via-port,
  unknown→0+flag), per-rack sum, thresholds, design total, removes/moves
  reflected, feed-vs-config capacity, read-only (no dcim writes).
- **B. Heatmap toggle** — editor checkbox, style neutralization, green→red
  gradient by rack share, unknown shade, gradient legend, off-restores-exactly.
  e2e tests.
- **C. Tier 2** — config-driven PDU/bank distribution + per-PDU/bank output +
  tests.
- **D. Tier 3** — `power_script` loader with graceful Tier-1 fallback (reuse the
  naming loader pattern) + shipped generic example + docs + tests.

## 7. Conformance checklist (definition of done)

- [ ] Tier 1 works with nothing configured; capacity uses feeds when present.
- [ ] Planned adds/removes/moves change the projected numbers correctly.
- [ ] Unknown-draw devices are flagged, not silently zero.
- [ ] Heatmap on: state styling suppressed, tiles gradient-colored by rack share,
      unknown shade distinct, legend shown.
- [ ] Heatmap off: original rendering byte-identical to before toggling.
- [ ] Power is read-only: no dcim writes, no design dirty flag from the overlay.
- [ ] Bad `power_script` falls back to Tier 1 (page never errors).
