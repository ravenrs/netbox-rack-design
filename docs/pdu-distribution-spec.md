# PDU power distribution — specification

Status: **active** — universalization in progress (started 2026-07-17; reworked to
the feed-model design 2026-07-22). Fleshes out **Tier 2/3** of
`docs/power-projection-spec.md` with the *concrete* distribution algorithm: how a
planned device's draw is split across a rack's PDUs, feeds, and banks, and how the
editor previews the resulting per-bank load.

Definition of done = the conformance checklist (§10) covered by backend tests
(mirrors `naming.py`'s test-first rule) and the heatmap items verified live.

## 0. Principle — universal base, native-first

The distribution engine is the **power analogue of the naming-through-script
engine** (`naming.py`), but with one deliberate difference from the naming
engine: **there is a working built-in**. Two site-agnostic naming conventions
(§1) make it safe to compute a real distribution with **zero config and zero
script**, so the plugin can *announce* power distribution as a base feature — not
only as a bring-your-own-script hook.

### 0.1 The three tiers

| Tier | Who it's for | Mechanism |
|---|---|---|
| **Base** (announced) | anyone who follows the two naming conventions | `distribution_mode = "builtin"`: bank from the outlet port name, breaker from the **bound feed**, feed-leg from the binding. No config, no script. |
| **Config bridge** | sites that keep power limits/topology in **custom fields** | `planning_fields` config maps *custom fields only* into the planning dialogs and the script's view of the rack. Never touches native fields. |
| **Script** | sites whose distribution *behaviour* differs | `distribution_mode = "script"`: a dotted path to `fn(rack, devices) -> Distribution`. Only for logic (direction, ceilings, PSU schemes) — **never** for feed *data*. |

### 0.2 The universal split — native vs config

- **Everything native is base logic, never config.** Real `PowerFeed`
  voltage/amperage/phase/supply, port `allocated_draw`, the outlet port name, the
  device name — the plugin reads these directly. They exist on every NetBox
  instance, so no mapping is needed.
- **Config bridges custom fields only.** Site quirks that live in custom fields —
  `power_limitation` (rack ceiling), `pdu_location` (unit→bank direction), and any
  future field — are declared in `planning_fields`, so the plugin never hardcodes
  a site's cf names. A DC that doesn't use them simply doesn't declare them; the
  dialog shows nothing extra and the base feature still works.

### 0.3 The two universal conventions

The base feature works out of the box **iff** the data follows these two rules —
they are the documented contract for using the plugin, replacing the old
"bank identity is site-specific, so no built-in" stance:

1. **Bank = first segment of the outlet port name.** A PDU outlet is named
   `"<bank>/<port>"`; the segment before `/` is the bank (`1/1` → bank 1). The
   count of distinct bank ids across a PDU's outlets is its `power_bank_count`.
2. **Feed-leg = the feed a PDU is bound to** (§6). A PDU is on leg A because it is
   bound to *Feed A*, not because of its name. Redundancy (a device's two PSUs on
   two feeds) falls out of the bindings. There is **no device-name parsing** in
   the base.

Everything here is a **read-only overlay**: no `dcim` writes, no design dirty
flag, nothing saved back to real records. A bad script degrades to the crude rack
total — it must never break the editor.

## 1. Vocabulary (from the NetBox power model)

- **PDU** — a device with role slug in `{pdu, unmanageable-pdu}`, status
  `active`/`planned`. It *distributes* power; it is **not** a consumer
  (excluded from the draw sum via `power_exclude_roles`).
- **Feed** — the power source a PDU draws from. Two kinds, one shape:
  - **Real** — a native `dcim.PowerFeed` the PDU's power port is cabled to
    (provisioned racks). Carries `voltage`, `amperage`, `phase`, `supply`.
  - **Planned** — a plugin-side `DesignPowerFeed` (§6) for greenfield planning,
    where the rack has no real feeds yet. Same electrical fields.
- **Feed-leg** (`a` / `b`) — the redundancy leg = **which feed the PDU is bound
  to**. Two independent feeds; a rack is sized so **either leg can carry the whole
  load alone** on failover.
- **Bank** — a breaker/phase group inside one PDU. **Identity lives in the outlet
  PORT name** (`"<bank>/<port>"`, §0.3), not on the PDU. `power_bank_count` =
  distinct `<bank>` values across a PDU's outlets.
- **Breaker (per bank)** — `bank_max = pdu_input_draw / power_bank_count`. A bank
  whose charged load exceeds this is an **overload** (alarm).
- **PDU input capacity** (`allocated_draw`) — from the **bound feed**:
  `voltage × amperage × phase_rate`, `phase_rate = 1.732` for three-phase else
  `1`. For a real PDU with no binding and a cabled `PowerFeed`, the native cable
  path supplies the same figure; with neither, the port's own `allocated_draw`.
- **Phase** — from the bound feed's `phase` (3-phase → `phase_rate = 1.732`).

## 2. Distribution algorithm (built-in + reference script share it)

The base builder (`distribution.build_native`) and the shipped reference script
(`distribution_example.py`, cf. `naming_example.py`) run the **same** algorithm
over the **same** helpers — the script only exists so a site can override the
*behaviour* pieces (direction, ceilings, PSU scheme). Two independent questions,
resolved per rack.

### 2.1 Which bank owns each rack unit — `unit → (pdu, bank)` map

`_get_pdu_dicts()`. For **each feed-leg** (`a`, `b`), a map from every rack unit to
the PDU+bank feeding it — so an **uncabled** device is still attributed by its U
position.

```
units          = [1 .. rack.u_height]          # reversed if pdu_location == "top"
bank_count/rack = Σ(power_bank_count over PDUs) / 2      # /2 because a & b mirror
units_per_bank  = round(len(units) / bank_count/rack)
```

Walking PDUs in feed order, each bank claims the next contiguous `units_per_bank`
slice of its leg's unit list. **Remainder** units attach to the **previous**
bank, so every unit is owned. `pdu_location` (`top`/`bottom`, a rack custom field
read via the config bridge) flips the direction so bank 1 sits where the PDU
physically starts. **`pdu_location` is optional**: absent, direction defaults to
`bottom`.

**PDU scheme** — the multiset of per-PDU bank counts, sorted and `_`-joined, is
looked up in `BANK_LIST_TO_PDU_SCHEMAS` to label the topology (validation aid;
unknown signature raises in the reference — the built-in tolerates it).

### 2.2 How much draw each device puts on which bank

Per planned device (`check_power_consumption()`):

1. **Skip non-consumers** — roles `{cable-management, patch-panel, pdu,
   unmanageable-pdu, rack-mount-boxes, rack-mount-kit}`; a `blade-server` with no
   bank connection.
2. **Per power port**, read `allocated_draw`. Then:
   - **Cabled** (`PowerPort → PowerOutlet` on a PDU): bank from the outlet name
     `"<bank>/<port>"`, PDU/leg from the binding → charge to that PDU+bank.
   - **Uncabled** (planned): look up the device's U position in the §2.1 map for
     each leg → charge there.
3. **Active vs planned split** — status `planned` charges `planned_power`; else
   `allocated_power`. Both accumulate per bank (committed vs projected).
4. **Redundancy is "full", never split** — a device's draw is charged **in full
   to each feed-leg it participates in** (on an A/B failure the surviving leg
   carries the whole load). A single-PSU device sits entirely on one leg.

### 2.3 Per-PSU → leg wiring

For a planned/uncabled device, which PSU lands on which leg is a small
scheme table (`pdu-1` = leg `a`, `pdu-2` = leg `b`): `p2` →
ps1→a, ps2→b; `p4` → ps1,2→a, ps3,4→b; `p6` → ps1-3→a, ps4-6→b; single PSU → the
leg with more free ports. Redundancy sanity checks warn, don't block. **This is
a behaviour piece — script-only; the built-in uses a simple A/B split.**

## 3. Data contract — the `Distribution` object

Structured, template-agnostic, attached to the projection bundle as
`power["distribution"]`. Single source for the editor heatmap and any read-only
view.

```
Distribution
  scheme            "2x1PH2Banks" | ...            # §2.1 topology label
  pdu_location      "top" | "bottom"
  pdus:  { pdu_name: {
            feed_name:        "a1",
            feed_letter:      "a" | "b",           # = the bound feed's leg
            feed_source:      "real" | "planned",  # which kind of feed backed it
            phase:            1 | 3,
            allocated_draw:   int  W                # PDU input breaker (bound feed)
            power_bank_count: int,
            banks: { bank_id: {
                       max_power:       int W       # per-bank breaker
                       allocated_power: int W       # committed (active devices)
                       planned_power:   int W       # projected (planned devices)
                       util_pct:        float
                       state:           ok|warn|critical|overload
                       units:           [int, ...]
                       devices:         [ {name, ru, draw_w, status, ports}, ... ]
            } }
  } }
  rack:  { power_limitation_w, power_consumption_w, alarm: bool, warnings: [str] }
```

`state` uses the existing thresholds (`power_warn_pct`, `power_critical_pct`);
`allocated_power > max_power` is `overload` → sets `rack.alarm` + a warning.

## 4. Heatmap behavior — what `distribution_mode` changes

The heatmap toggle is unchanged; `distribution_mode` decides **what the colors
mean** when it's on.

- **`"none"`** — per-device rack-share gradient (today's Tier-1 behavior). PDUs
  are excluded infrastructure.
- **`"builtin"` / `"script"`** — the plugin colors from the returned
  `Distribution`; the **PDU/bank** becomes the heat subject:
  - each bank is a **filled health bar** green→red by
    `(allocated_power + planned_power) / max_power`; `overload` is a distinct
    hard-red.
  - PDU column headers are **feed-leg colored** (leg a / leg b) — that *is* the
    A/B key (no separate legend). Banks of one PDU stack vertically.
  - consumer tiles get an A/B feed edge by the leg(s) they land on; unknown-draw
    keeps the neutral hatched shade (absence of data ≠ zero).
  - instant per-bank tooltip (used W / breaker W); overload/redundancy warnings
    from `Distribution.rack`.

Toggle off → styling restores exactly (pure view state, never persisted).

## 5. Config

```python
# --- Power distribution engine (see pdu-distribution-spec.md) --------------
# How per-PDU/bank load is distributed for the power heatmap.
#   "none"    -> Tier 1: per-rack total only, per-device gradient (default)
#   "builtin" -> native distribution from the two conventions (§0.3), no script
#   "script"  -> a dotted path to fn(rack, devices) -> Distribution
"distribution_mode": "none",
# Dotted path to a callable used when distribution_mode == "script".
"distribution_script": "",
# Custom-field bridge for the planning dialogs (Tier 2). Maps site custom
# fields into the rack/PDU planning inputs -- NATIVE fields are never listed
# here. Empty by default (base feature needs none). Both "rack" and "pdu" keys
# are optional; each is a list of {key,label,type,source,choices?}. Example:
#   "planning_fields": {
#     "rack": [
#       {"key": "power_limitation", "label": "Power limitation (W)",
#        "type": "number", "source": "cf.power_limitation"},
#       {"key": "pdu_location", "label": "PDU location", "type": "choice",
#        "choices": ["top", "bottom"], "source": "cf.pdu_location"},
#     ],
#     "pdu": [
#       {"key": "cooling_mode", "label": "Cooling mode",
#        "type": "choice", "choices": ["active", "passive"],
#        "source": "cf.cooling_mode"},
#     ],
#   }
"planning_fields": {},
```

`source` is a dotted path (`cf.<name>` for custom fields, `rack.role.name` for a
native attribute *to read from a copy source*) — the same token grammar as the
naming templates, so it's self-documenting. It seeds the dialog / copy-from-rack;
it is **never written back** to a native field (the planned object has no real
record). `type` ∈ `{number, text, choice}`; `choices` for `choice`.

There is no `naming_template`-style middle mode: distribution can't be a format
string, so it's off (`none`), built-in (`builtin`), or fully delegated
(`script`).

## 6. The feed model — how a PDU gets its breaker

A real PDU reads its breaker from a **cabled `PowerFeed`** (native). A planned PDU
has none, so instead of inventing a parallel shape (inline V/A/phase JSON) the
plugin **mirrors the native model**: model the feed, and **bind** the PDU to it.
The read path is then uniform — real or planned, "get the bound feed, read its
electricals" — so the script/built-in never branches on real-vs-planned.

### 6.1 `DesignPowerFeed` (planned feed)

One row per planned feed, scoped to `(design, rack)`:

```
DesignPowerFeed
  design      FK Design      (CASCADE, related_name="planned_feeds")
  rack        FK dcim.Rack   (CASCADE)
  name        str            # e.g. "Feed A" — its leg/identity
  voltage     int
  amperage    int
  phase       1 | 3
  supply      "ac" | "dc"
  unique_together (design, rack, name)
```

Plain `models.Model` (like `HiddenDesignRack`/`DesignRackPower`) — planning
scratch data, not a change-logged object. Read-only w.r.t. `dcim`.

### 6.2 Binding — the PDU → feed link

Two nullable FKs on `DesignPlacement` (avoids a GenericForeignKey; both queryable):

```
DesignPlacement
  real_power_feed     FK dcim.PowerFeed        (null, on_delete=SET_NULL)
  planned_power_feed  FK DesignPowerFeed       (null, on_delete=SET_NULL)
```

- `clean()` enforces **at most one** is set.
- property `bound_feed` returns whichever is set, exposing a duck-typed
  `{voltage, amperage, phase, supply, name}` regardless of source.
- One PDU binds to **one** feed (matches "one power port → one feed").
- A PDU's custom fields come from **one** of two sources, mutually exclusive:
  - **Live from a real device** — new FK `power_source_device` (see §6.5);
  - **Manual entry** — via `power_config` (see §6.5).

### 6.3 Dialog flows

**Common case — rack has real feeds** (ordered against a contract →
`PowerFeed`s + `power_limitation` already exist). Adding a PDU opens the
**bind-to-feed** dialog:

- a picker of the rack's feeds — **real feeds first**, then any planned feeds;
- a secondary **"＋ define planned feed"** option, *always available* as a
  fallback (so a mixed real/planned rack is never a dead end);
- confirm → the binding (`real_power_feed_id` or `planned_power_feed_id`) is
  stashed on the widget and rides the design Save.

**Edge case — rack has no feeds** (greenfield). The per-rack **Power** button
(gated: shown when the rack has no real feeds) opens the planned-power flow:

- define planned feeds (`DesignPowerFeed`) — manual (name + V/A/phase/supply) or
  copy-from-rack (materialize another rack's real feeds);
- set the planned `power_limitation` (and any `planning_fields` cf) via
  `DesignRackPower`;
- then adding PDUs binds to those planned feeds.

**Feeds are never defined by a script** — a script can't invent breaker
amperage; it needs source data, which the model provides. The script/CF layer is
for distribution *behaviour* only.

### 6.4 `DesignRackPower` (rack custom-field override)

Unchanged in shape/purpose: one row per `(design, rack)`, holding the planned
`custom_fields` (e.g. `power_limitation`, `pdu_location`) merged **in-memory**
over `rack.cf` before the distribution runs (never written to `dcim.Rack`). Now
populated via the `planning_fields`-driven rack dialog.

### 6.5 Planned-PDU custom fields — device reference vs manual entry

A planned PDU's custom fields are **resolved from one source only** (mutually
exclusive):

#### 6.5.1 Live device reference — `power_source_device` FK

```
DesignPlacement
  power_source_device  FK dcim.Device  (null, on_delete=SET_NULL)
```

When set, the PDU's cf are **read live from the source device** — `device.cf`
(the full custom-field value dict) — on every `generate_distribution()` call,
never snapshotted. Editing the source device's cf updates the plan immediately.

- `clean()` enforces `power_source_device` and manual `power_config` are never
  both supplied.
- The source device can be any `dcim.Device` (not restricted to PDU role); site
  convention decides (e.g., a PDU template device, a real PDU, etc.).
- Unresolvable source (device deleted) degrades cleanly: logged, fallback to
  manual `power_config`.

#### 6.5.2 Manual entry — `power_config` custom fields only

`power_config` is now a JSON field holding:

```json
{"custom_fields": {...}}
```

The `custom_fields` map is populated **at planning time** via the
`planning_fields["pdu"]` config schema (same cf-bridge grammar as
`planning_fields["rack"]`, §5), drives the PDU dialog's cf inputs, and persists
to `power_config` on Save. **The old `feed` key and `copied_from`/`source`
provenance are gone** — feed data now lives on the feed model / binding
(§6.1–6.2).

#### 6.5.3 Distribution engine resolution

`generate_distribution()` resolves a PDU's custom fields as follows:

1. If `power_source_device` is set, read `device.cf` live (and log source);
2. else if `power_config.custom_fields` exists, use it;
3. else use `{}` (no custom fields).

This happens per-rack, per-PDU; the distribution object (`Distribution.pdus[pdu_name]`)
carries no provenance marker — to the heatmap and read-only views, the cf
are resolved and uniform.

## 7. What the engine receives & returns

### 7.1 Inputs (the planned world, read-only)

`generate_distribution(elevation, *, mode=None)` builds, per rack:

- **`rack`** — the planned `dcim.Rack`; the built-in/script reads `rack.u_height`,
  `rack.cf` (the cf **value dict** — not `.custom_fields`, a manager), and
  `rack.devices.all()` for real PDUs. Effective cf = real `rack.cf` merged with
  `DesignRackPower` (§6.4).
- **`devices`** (`devices_from_elevation`) — planned consumers **plus planned PDU
  adds**. Each PDU entry carries:
  - `role = pdu`, `device = None` (planned) or the real device;
  - `device_type` (for `PowerOutletTemplate`s → `power_bank_count`);
  - a resolved **`feed`** dict from the binding (`bound_feed` → real or planned,
    §6.2) — uniform `{voltage, amperage, phase, supply, name, leg, source}`;
  - `custom_fields` — resolved per §6.5.3 (from `power_source_device.cf` live,
    or fallback to manual `power_config.custom_fields`, or `{}`).
  Consumers carry identity, `u_position`/`face`, `draw_w`/`draw_known`, and
  `power_ports` (each `allocated_draw` + outlet peer where cabled).

The engine never queries `dcim` for writes and never mutates its inputs.

### 7.2 Resolution & fallback (`distribution.py`)

Import-safe, read-only, parallel to `naming.py`:

```
DEFAULT_DISTRIBUTION_MODE = "none"
generate_distribution(elevation, *, mode=None) -> Distribution | None
    "none"    -> None                        # caller uses Tier 1
    "builtin" -> build_native(rack, devices) # the two conventions (§0.3)
    "script"  -> _run_script(...), guarded → None on any failure
_run_script -> import_string(distribution_script)(rack, devices)
```

An empty/unimportable/non-callable path, or any exception the script raises, is
caught → logs a warning → returns `None` → page falls back to the `none`
heatmap. A buggy script degrades the overlay; it never errors the editor. Same
`SCRIPTS_ROOT` story as naming (the script may live in `scripts/`, editable from
the NetBox UI).

### 7.3 What the plugin does with the result

`projection.project_rack` calls `generate_distribution(elevation)` while building
the `power` bundle and attaches the `Distribution` (or omits on `None`) as
`power["distribution"]`, computed **server-side** so editor and read-only
elevation read identical figures. Recompute cadence = **on load and after Save**;
no mid-drag live recompute.

## 8. API endpoints

All authenticated, read-only w.r.t. `dcim`, with debug logging on entry + result.

- **`GET .../designs/{id}/feeds/?rack_id=`** → the rack's real `PowerFeed`s
  (uniform electricals) + its `DesignPowerFeed`s, for the bind picker (real
  first).
- **`POST/GET .../designs/{id}/planned-feed/`** → upsert / list `DesignPowerFeed`
  (create a planned feed; copy-from-rack materializes real feeds).
- **`POST/GET .../designs/{id}/rack-power/`** `{rack_id, power_config}` → upsert /
  read `DesignRackPower` (planned `power_limitation` etc.), immediate.
- **`GET .../power-source/?kind=rack&rack_id=`** → copy-from-rack prefill: the
  rack's custom fields, read via the `planning_fields["rack"]` `source` paths
  (for rack dialog prefill and copy operations).
- **PDU listing** — the PDU dialog lists a rack's PDUs via the **core dcim API**
  (`GET /api/dcim/devices/?rack_id=<id>&role=pdu&role=unmanageable-pdu`), not a
  plugin endpoint. Enables referencing existing PDU devices for the
  `power_source_device` FK.
- Save-layout item carries:
  - `real_power_feed_id` / `planned_power_feed_id` (the feed binding)
  - `power_source_device_id` (the cf source device, if set)
  - `power_config` (manual cf as `{"custom_fields": {...}}`, if set)
  All written by `_reconcile_item` on PDU adds; the binding and cf source ride the
  existing Save.

## 9. Frontend instrumentation (debuggability)

Every interaction that touches this feature emits a **dev-only tracer** event
(`window.__rdDragTrace`, gated on DEBUG/DjDT — inert in prod), carrying its
**actual payload**, not just a name:

- **device move/drag** → source/target unit, device, which PDU banks it now
  charges, draw delta;
- **every button press** → which button + rack/PDU context + state;
- **copy-from-rack** → source rack, feeds/cf values pulled, what got prefilled;
- **bind-to-feed** → chosen feed (real/planned, id, V/A/phase, leg) + the PDU;
- **planned-feed create** → the full feed record;
- **dialogs** → open / confirm / cancel with field values;
- **save** → the binding + planning payload per item;
- **heatmap render** → `heat.feed` / `heat.bank` per bank (load/breaker/leg).

Backend mirrors this with a `logger.debug` sweep across feed resolution (real vs
planned), bank/leg, breaker, override applied, and every graceful fallback.

## 10. Conformance checklist (definition of done)

**Base (builtin) — the announced feature:**
- [ ] `distribution_mode = "builtin"` with convention-named outlets + PDUs bound
      to feeds produces a per-bank heatmap with **no config and no script**.
- [ ] Bank = first segment of the outlet port name (`1/1` → bank 1);
      `power_bank_count` = distinct banks.
- [ ] Feed-leg comes from the **binding** (bound feed), not device-name parsing;
      redundancy falls out of two bindings.
- [ ] Per-bank `max_power = pdu_input_draw / bank_count`; `allocated > max` →
      `overload` + rack alarm + warning; active vs planned tracked separately.
- [ ] `distribution_mode = "none"` (default) reproduces today's per-device
      rack-share heatmap.

**Feed model & binding:**
- [ ] A real PDU cabled to a `PowerFeed` sizes its breaker from that feed (native).
- [ ] A planned PDU **bound to a real `PowerFeed`** sizes from it.
- [ ] A planned PDU **bound to a `DesignPowerFeed`** sizes from it.
- [ ] `DesignPlacement.clean()` rejects both FKs set; `bound_feed` resolves the
      set one; unbound PDU degrades cleanly (logged, omitted, page fine).
- [ ] `DesignPowerFeed` round-trips; `unique_together(design, rack, name)`;
      cascade on design delete.

**Config bridge (Tier 2) — custom fields only:**
- [ ] Rack/PDU planning dialogs render their cf inputs from `planning_fields`; no
      cf name is hardcoded in JS/HTML.
- [ ] `planning_fields` includes both `"rack"` and `"pdu"` keys (both optional);
      `"pdu"` drives the PDU dialog's manual cf inputs.
- [ ] `power_limitation` / `pdu_location` reach the script via
      `DesignRackPower` merged over `rack.cf`, without writing `dcim.Rack`.
- [ ] `planning_fields = {}` (default) → dialogs show only native inputs; base
      feature unaffected.

**Planned-PDU custom fields (§6.5):**
- [ ] A planned PDU can reference a real device via `power_source_device` FK; its
      cf are read **live** (never snapshotted) on each distribution run.
- [ ] A planned PDU can have manual cf via `power_config = {"custom_fields": {...}}`,
      driven by `planning_fields["pdu"]` dialog inputs (the old `feed` key and
      `copied_from`/`source` provenance are gone).
- [ ] `DesignPlacement.clean()` rejects both `power_source_device` and manual
      `power_config` supplied together (mutually exclusive).
- [ ] Distribution engine (§6.5.3) resolves cf in order: live from
      `power_source_device.cf`, else manual `power_config.custom_fields`, else `{}`.
- [ ] Unresolvable `power_source_device` (device deleted) degrades cleanly; no
      errors, logged, fallback to manual cf.

**Script (Tier 3):**
- [ ] `distribution_example.py` runs the §2 algorithm over the shared helpers and
      works under the builtin conventions with no site code.
- [ ] Empty/unimportable/non-callable/raising `distribution_script` →
      `generate_distribution` returns `None`, page falls back to `none`.
- [ ] Nothing site-specific ships in the public wheel.

**Frontend & instrumentation:**
- [ ] Heatmap (builtin/script): banks are filled health bars, PDU headers
      feed-leg colored, tiles get A/B edges, instant tooltip — verified live.
- [ ] Heatmap off: original rendering restored (verified live + e2e).
- [ ] Every §9 interaction emits its tracer event with the actual payload
      (DEBUG on); inert in prod.
- [ ] Read-only throughout: no dcim writes, no design dirty flag.

## 11. Out of scope (v1)

- Live mid-drag distribution recompute.
- Breaker-trip / inrush / power-factor modeling beyond NetBox's fields.
- Auto-cabling planned devices in `dcim` (this feature is preview-only and never
  writes cable connections).
- Writing planning inputs back to native `dcim` fields (the plugin only ever
  stores its own planning copy).
