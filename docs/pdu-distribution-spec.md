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
| **`"none"`** | racks/sites with no per-bank accounting | No bank attribution at all — where a device sits is irrelevant because there is no way to know which bank feeds it. The heatmap falls back to a per-device share of the rack total. |
| **`"builtin"`** (announced) | anyone who follows the two naming conventions | Purely native: bank from the outlet port name, breaker from the **bound feed**, feed-leg from the binding, rack units split symmetrically across banks in a fixed direction. Reads **no custom fields at all** — no rack ceiling, no PDU orientation. |
| **`"script"`** | sites whose distribution *logic* differs | A dotted path to `fn(rack, devices) -> Distribution`. This is where distribution **logic** lives — the conventions of a company or a data centre — and the only tier where custom fields reach the engine, via the `planning_fields` config bridge (merged over `rack.cf` for this tier only). On any failure the engine logs a warning and returns `None`; the heatmap degrades to the per-device view and never breaks the editor. |

### 0.2 The universal split — native vs config

- **Everything native is base logic, never config.** Real `PowerFeed`
  voltage/amperage/phase/supply, port `allocated_draw`, the outlet port name, the
  device name — the plugin reads these directly. They exist on every NetBox
  instance, so no mapping is needed.
- **The builtin tier is purely native — it reads no custom fields at all.** Bank
  identity, breaker sizing, and the unit→bank split direction all come from
  outlet names and feed bindings; there is no rack-ceiling lookup and no
  PDU-orientation lookup in `"builtin"`. Custom fields matter only once a site
  needs *behaviour* the two conventions can't express.
- **Config bridges custom fields only, and only for the script tier.** Site
  quirks that live in custom fields — `power_limitation` (rack ceiling: the
  PDUs may total more breaker capacity than the hall can cool, so the cap is a
  separate fact that must alarm independently), `pdu_location` (which end of
  the rack a PDU's tail/inlet faces, so a script knows where each bank
  physically sits), and any future field — are declared in `planning_fields`
  and reached by a `distribution_script` through its `source` key, so the
  plugin never hardcodes a site's cf names *in the code it ships*. A DC that
  doesn't run a script simply doesn't declare them; the dialog shows nothing
  extra and the base feature still works.

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
units          = [1 .. rack.u_height]          # reversed if direction == "top"
bank_count/rack = Σ(power_bank_count over PDUs) / 2      # /2 because a & b mirror
units_per_bank  = round(len(units) / bank_count/rack)
```

Walking PDUs in feed order, each bank claims the next contiguous `units_per_bank`
slice of its leg's unit list. **Remainder** units attach to the **previous**
bank, so every unit is owned. **The builtin always splits in a fixed direction**
(bank 1 at the bottom) — it consults no custom field at all. A script variant
(e.g. `distribution_example.py`) may instead read the rack custom field
`pdu_location` (`top`/`bottom` — which end of the rack the PDU's tail/inlet
faces) through the `planning_fields` config bridge and flip the direction so
bank 1 sits where the PDU physically starts; there, `pdu_location` is
**optional**: absent, direction defaults to `bottom`.

**PDU scheme** — the multiset of per-PDU bank counts, sorted and `_`-joined, is
looked up in `BANK_LIST_TO_PDU_SCHEMAS` to label the topology (validation aid;
unknown signature raises in the reference — the built-in tolerates it).

### 2.2 How much draw each device puts on which bank

Per planned device (`check_power_consumption()`):

1. **Skip non-consumers** — roles `{cable-management, patch-panel, pdu,
   unmanageable-pdu, rack-mount-boxes, rack-mount-kit}`; a `blade-server` with no
   bank connection.
2. **Per power port**, read `allocated_draw`. Then:
   - **Cabled** (`PowerPort → PowerOutlet` on a PDU **in this rack**): bank from
     the outlet name `"<bank>/<port>"`, PDU/leg from the binding → charge to that
     PDU+bank.
   - **Uncabled** (planned) — and a device whose cabling leads OUT of this rack:
     look up the device's U position in the §2.1 map for each leg → charge there.
     A device MOVED here still carries its cabling to the source rack's PDU until
     the design is implemented, so that cabling names a PDU this topology does not
     contain; treating it as uncabled is what makes the draw follow the device
     across racks instead of disappearing from both.
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
# Custom-field bridge for the planning dialogs -- meaningful only when
# distribution_mode == "script" (the builtin tier reads no custom fields, so
# it ignores this entirely). Maps site custom fields into the rack/PDU
# planning inputs and into the script's view of the rack -- NATIVE fields are
# never listed here. Empty by default (the builtin feature needs none). Both
# "rack" and "pdu" keys are optional; each is a list of
# {key,label,type,source,choices?}. Example:
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

**`source` is meaningful to scripts only** — the builtin tier never resolves a
`planning_fields` entry, so with `distribution_mode` at `"none"` or `"builtin"`
the map has nothing to feed. Consequently the rack/PDU planning dialogs' manual
cf inputs are a **script-tier feature**: they only surface entries once a
`distribution_script` (and a matching `planning_fields` declaration) are
configured to consume them.

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

A `NetBoxModel` (unlike `HiddenDesignRack`/`DesignRackPower`, which stay plain):
a planned feed is design data a team reads, edits and deletes on its own.
Read-only w.r.t. `dcim` — no real `PowerFeed` is ever written.

**Its own views (Unreleased).** Feeds were created by the editor's dialogs and
then visible nowhere, with no way to remove one — while still sizing a
greenfield rack's capacity bar, so a stray feed silently inflated it. They now
have the same view set as `DesignPlacement`:

| Route | Does |
|---|---|
| `Rack Design → Planned Power Feeds` (`/plugins/rack-design/power-feeds/`) | filterable list, bulk edit, bulk delete, import/export |
| `/power-feeds/<pk>/` | detail: identity, electricals, **derated capacity**, and the planned PDUs bound to it |
| `/power-feeds/<pk>/edit/` · `/delete/` | correct or remove one feed |
| the design page's *Planned power feeds* panel | per-row edit + delete, returning to the design |
| `/api/plugins/rack-design/planned-power-feeds/` · GraphQL `planned_power_feed` | the REST/GraphQL twins |

`DesignPowerFeed.derated_watts` is the figure the capacity bar actually uses —
`breaker_watts()` derated by the instance's `POWERFEED_DEFAULT_MAX_UTILIZATION`
— so the list, the detail page and the rack's chips can never disagree about
what a feed is worth. Deleting a feed unbinds the planned PDUs that pointed at
it (`SET_NULL`), which the detail page names before you delete.

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

**Copy from rack REPLACES the target's planned feeds** (ruled 2026-08-28). The
button's promise is "this rack is fed like that one", so afterwards the target
holds exactly the source's set — no more, no less. It used to upsert by name and
only ever add, and since retargeting rewrites a *rack-name prefix* only, feeds
named by any other scheme (`Utility A`) never collided: copying from three racks
in turn left the union of all three, and the rack's capacity bar read as the sum
of every source ever clicked. Rules that fall out of it:

- a feed whose (retargeted) name survives keeps its **row**, so every PDU bound
  to it stays bound; the rest are deleted and their PDUs unbound
  (`planned_power_feed` is `SET_NULL`), with the count returned so the dialog can
  say so;
- a source with **no feeds is a no-op, not a wipe** — picking the wrong rack in
  the dropdown must not strip a supply that has no undo.

**Planned feeds are visible and removable.** They size a greenfield rack's
capacity bar, so they cannot be write-only:

- the rack power dialog lists this rack's planned feeds, each with a × that
  deletes it (`DELETE .../planned-feed/`), reporting how many PDUs it unbound;
- the design detail page carries a **Planned power feeds** panel — every feed in
  the design, its rack, its electricals, the **derated watts the bar actually
  uses**, and the PDUs bound to it.

**A failing engine must SAY SO** (ruled 2026-08-28). `generate_distribution()`
degrades to `None` on any failure, and an empty chip strip is indistinguishable
from a rack that legitimately has no PDUs — four causes, one blank result. So the
projection now carries a **status** beside the distribution, and the editor
renders it under the power bar:

| state | meaning | shown as |
|---|---|---|
| `ok` | a distribution was produced | nothing (the chips speak) |
| `off` | `distribution_mode` is `none` | muted "Per-bank distribution is off" |
| `failed` | the engine raised | **red** notice; hover gives the exception type, its message and the script's dotted path |
| `empty` | the engine ran, resolved no PDU | muted notice; hover names each omitted PDU and why (no feed / no parseable outlet banks) |

`generate_distribution_status(elevation)` returns `(distribution, status)`;
`generate_distribution(elevation)` still returns just the distribution for
scripts and older callers. The status rides `elevation.power["distribution_status"]`,
the `rd-diststatus-<rack_id>` json_script, and the `distribution_status` block of
`recompute-distribution/` — so a live edit that breaks the engine reports itself
immediately rather than at the next page load. Degrading (never erroring the
page) is unchanged; what changed is that the degrade is visible.

**Feeds are never defined by a script** — a script can't invent breaker
amperage; it needs source data, which the model provides. The script/CF layer is
for distribution *behaviour* only.

### 6.4 `DesignRackPower` (rack custom-field override)

Unchanged in shape/purpose: one row per `(design, rack)`, holding the planned
`custom_fields` (e.g. `power_limitation`, `pdu_location`) merged **in-memory**
over `rack.cf` before the distribution runs (never written to `dcim.Rack`). This
merge only matters for `distribution_mode = "script"` — a `distribution_script`
reads it through the `planning_fields` config bridge; the builtin tier ignores
`rack.cf` entirely. Now populated via the `planning_fields`-driven rack dialog.

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
elevation read identical figures.

Recompute cadence in the editor is **live**: the per-bank chip strip refreshes on
every add/remove/move, like the always-live power bar. Because bank assignment is
server logic (real cabling / a custom `distribution_script`) the browser must not
duplicate, the editor re-runs the SAME engine over the unsaved layout via the
read-only `recompute-distribution` endpoint (§8) — it applies the posted layout
through the save-layout reconciliation inside a **rolled-back transaction**, so the
engine sees the live edit but nothing is persisted. First paint (and the read-only
elevation, which has no editor) uses the static server-rendered figure.

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
- **`POST .../designs/{id}/recompute-distribution/`** (body = the save-layout
  payload) → the fresh per-rack `Distribution` for the UNSAVED editor layout,
  `{"distributions": {"<rack_id>": <Distribution-or-null>}}`. Applies the layout
  through the save-layout reconciliation inside a **rolled-back transaction** and
  projects each rack, so it re-runs the real distribution engine live yet persists
  nothing. Requires only `view` on the design. Drives the editor's live per-bank
  chip refresh (§7.3).
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

**Config bridge (script tier only) — custom fields only:**
- [ ] `distribution_mode = "builtin"` never resolves `planning_fields` and never
      reads `rack.cf`; only `distribution_mode = "script"` does.
- [ ] Rack/PDU planning dialogs render their cf inputs from `planning_fields`; no
      cf name is hardcoded in JS/HTML, and the dialogs' manual cf inputs only
      apply when a script is configured to consume them.
- [ ] `planning_fields` includes both `"rack"` and `"pdu"` keys (both optional);
      `"pdu"` drives the PDU dialog's manual cf inputs.
- [ ] `power_limitation` / `pdu_location` reach a `distribution_script` via
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

- Breaker-trip / inrush / power-factor modeling beyond NetBox's fields.
- Auto-cabling planned devices in `dcim` (this feature is preview-only and never
  writes cable connections).
- Writing planning inputs back to native `dcim` fields (the plugin only ever
  stores its own planning copy).
