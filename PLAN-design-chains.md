# Plan — design chains (a design built on top of an approved design)

Status: **implemented**. All six phases in §5 are built; full suite green at
1008 tests. See the Status paragraph after §5's table and the "Still open"
section at the end for what remains.

Goal (user, 2026-08-31): once a design is approved, another design can be built
**on its result**, and a third on that one, forming a chain — one person moves
and removes network gear, the next plans servers on the world that leaves
behind. Every design in the chain sees its predecessors' changes as if they had
already happened.

---

## 1. Where the codebase stood before this work

**Historical baseline — superseded by §5's Status paragraph.** The table below
describes the pre-chains codebase and is kept as the starting point the rest
of this document reasons from. Every row's gap is now closed; see §4 and §5.

The vocabulary for chains existed; the machinery did not, at the time this
plan was written.

| Thing | Where | State |
|---|---|---|
| `Design.based_on` (FK to self) | `models.py:128` | stored, displayed, filterable — **no engine reads it** |
| `Design.depends_on` (M2M to self) | `models.py:143` | same |
| `Design.root` / `version`, "one approved version per plan" | `models.py:117`, `clean()` `models.py:222` | enforced |
| Projection | `projection.project_rack()` `projection.py:1105` | **exactly one layer**: real `dcim` devices (`_existing_slots`) + this design's placements |
| Apply / realize to DCIM | — | **does not exist**; `STATUS_IMPLEMENTED` is a colour in `choices.py` and nothing else |

Confirmed by grep: `based_on` / `depends_on` appear only in `forms.py`,
`filtersets.py`, `api/serializers.py`, `graphql/types.py` and
`templates/.../design.html`. They are labels on a page.

---

## 2. Decisions that shape everything

### 2.1 A chain is a **stack**, not a merge

Design B's baseline is `reality + A`; design C's is `reality + A + B`. The
transitive `based_on` chain **is** the layer stack.

`depends_on` stays what it is today: an informational "must be executed after"
edge that does **not** affect projection. Making an M2M drive projection means
writing a merge-conflict resolver for peer designs; that is a different, much
larger feature and is not needed for the described workflow.

**Settled (user, 2026-09-01): one `based_on` parent per design — but a parent
may have many children.** So the lineage is a *tree*, not a single chain: each
design's own baseline is still a strictly linear stack of its ancestors, and
multi-parent baselining (which would need merge semantics between peer designs)
stays out of scope.

**Consequence — siblings are blind to each other.** B and C both baseline on A
and neither sees the other's placements. Two siblings can therefore claim the
same U in the same rack, and their family counters can hand out the same
`IDS-…-N`. Note `name_exists_in_site` (`naming.py:334`) matches by site
regardless of design, so it already catches a sibling *name* — the counter in
`_next_number` does not, and nothing at all catches a sibling *U collision*.
**Settled (user, 2026-09-01): first approved wins; the other sibling re-bases
onto it.** No cross-sibling conflict detection is built — once B is approved, C
is re-based from A onto B and its conflicts surface through the ordinary
staleness report (G4), which then sees B in its own baseline.

### 2.2 A design with dependents is frozen

Approving A is what makes it derivable. From that point A's placements are
read-only. If A must change it becomes A v2 (the versioning machinery already
exists), and B must be **explicitly re-based** onto v2. Without this rule every
downstream design rots silently.

Because the parent is frozen, live inheritance and a snapshot are equivalent —
so inherit **live** (resolve through FKs at projection time) and copy nothing.

---

## 3. Naming across a chain

This is a hard requirement, not a nicety (user, 2026-08-31).

### 3.1 The problem

`DesignPlacement.proposed_name` currently carries two different things at once:

- the **planning name** inside the design that owns the change —
  `IDS-1234_old_name`, where `IDS-1234` marks "this device is touched by that
  ticket";
- the **settled name** the device ends up with once that design is done —
  `old_name`.

Inside one design they are the same string, so nothing has ever forced them
apart. The moment B baselines on A they diverge: from B's point of view A's move
is already done, so the device must appear as `old_name`. A's ticket prefix is
A's bookkeeping, not part of the device's identity.

### 3.2 The rules

> **R1.** An inherited placement renders under its **settled name**. The
> planning prefix belongs only to the design that owns the change.
>
> **R2.** The input to name generation in a child design is always the settled
> name, never the parent's planning name. Prefixes never stack — B re-moving
> that device produces `IDS-5678_old_name`, never
> `IDS-5678_IDS-1234_old_name`.
>
> **R3.** De-prefixing applies **once per layer**. A three-deep chain strips
> A's prefix once, not three times.

### 3.3 Implementation

**Settled (user, 2026-09-01): the planning prefix is a *project name*, and it is
not reliably derivable from the design title — different designs use different
project names.** In this deployment it lives in a custom field (`cf.Task` on the
design), but that name is proprietary, so the plugin must never hardcode it. It
is therefore resolved through a **config-declared source path**, exactly as
`planning_fields` already does for the power dialogs (see
the planning-fields design record):

```python
"naming": {
  # where the planning prefix token comes from; omitted => derive from title
  "prefix_source": "design.cf.Task",
}
```

The naming layer is already pluggable (`naming_mode` = `sequence` / `template` /
`script`, `naming.py`), so this lands as a fourth entry point rather than a
special case:

- **`settled_name(placement) -> str`** — a config-selected callable, defaulting
  to a builtin that resolves the prefix token via `prefix_source` (falling back
  to deriving `IDS-<digits>` from the design title the way
  the shipped naming script does, `naming_example.build_name`) and strips a leading
  `^<token>[-_]` from `proposed_name`.
- `projection` calls it for every **inherited** slot instead of reading
  `proposed_name` directly.
- **No `settled_name` column.** Because the prefix is resolved from a declared
  source rather than guessed, the parse is deterministic and a stored second
  column would only freeze the answer at save time and force every naming
  script to return two values. The column stays available as a fallback if a
  deployment ever needs a prefix that no source path can express.

Resolution failure is not silent: if `prefix_source` is configured but resolves
to nothing on a design whose placements are being inherited, that surfaces as an
error the same way a failing distribution/naming script must.

### 3.4 What this drags in

**CLOSED — family counters now span the chain.** `chain_placement_names()`
(`naming.py:671`) replaces the old self-only sibling query and is used by both
`_next_number` and `_next_pdu_slot` (`naming_example.py`). It costs one query for
an unchained design (identical to the query it replaces) and, for a chain, one
lineage hop per ancestor plus one placement query covering self and every
ancestor together — never one query per ancestor, never one per name. By
design it deliberately **excludes siblings**: two children of one parent stay
blind to each other (§2.1), and its docstring is explicit that a sibling
collision is expected to surface through `name_exists_in_site` instead.

Original problem, for the record: `_next_number` (`naming_example.py:107`) used
to count real devices plus `DesignPlacement.objects.filter(design=placement.design)`
— siblings in *this design only*. In a chain, B's counter could not see the
servers A already planned, so `IDS-5678-1` would collide with a name A had
reserved.

**CLOSED 2026-09-02 — collision checking now matches on the settled plane too.**
`name_exists_in_site` (`naming.py:749`) used to match every placement whose
design targets the site regardless of design — seeing ancestors for free, but
only under their *planning* names, which made the promise in
`chain_placement_names()`'s docstring unreachable: two siblings with
differing prefixes (`IDS-1234_x` vs `IDS-5678_x`) would never collide as far
as the literal `proposed_name` check was concerned, even though both settle to
`x`. The function now also checks the **settled** plane, in both directions —
an existing placement's settled name equals the candidate, or the candidate's
own settled form equals an existing placement's settled or planning name — and
gained an optional `design=` keyword so a caller without an `exclude_placement`
can still supply the design whose prefix token should be used to settle the
candidate name. Cost: 3 queries regardless of chain depth (a `dcim.Device`
existence check, a literal-`proposed_name` existence check, then one query
fetching the narrow `endswith` candidate set that gets settled in Python).

**Caveat, recorded here because it is real, not a footnote.** The `endswith`
prefilter that selects the settled-plane candidate set is exact for the
builtin strip-prefix engine, whose settled name is always a suffix of
`proposed_name`. A deployment's custom `naming["settled_name"]` callable can
return anything — unrelated to `proposed_name` entirely — and such a collision
can be **missed** by this prefilter. That is an accepted false negative on a
non-blocking warning channel, not silent completeness: the function still
performs no writes and never resolves the collision itself, it only warns.

---

## 4. Gaps

**G1 — CLOSED. Layered projection.** Built in `netbox_rack_design/projection.py`:
`resolve_baseline_chain()` (`projection.py:510`), `baseline_occupancy()`
(`projection.py:1131`), the `_Baseline` object (`projection.py:550`) that
replays ancestor placements and accumulates `conflicts`, and
`ProjectedElevation.conflicts` (`projection.py:225`). The ancestor ordering
itself lives on the model as `Design.baseline_chain()` (`models.py:236`,
ordered oldest-first, cycle-guarded — see G7). Below, the original problem
statement, still accurate as the reasoning:

The engine used to assume one design over DCIM. Needed: `design.baseline_chain()`
(ordered ancestors, cycle-guarded) and a `project_rack` that replays ancestor
placements into the baseline before applying its own — an ancestor `move`
vacates the source U *and* occupies the target, a `remove` frees the U, an
`add` occupies one. `_existing_slots()` (`projection.py:264`) grows a "then
replay these layers" pass. Must hold equally for the tray
(`_existing_tray_slots`), bays (`_attach_bays`, `_attach_planned_chassis_bays`,
`_overlay_planned_blades`) and full-depth face mirroring. **This was the bulk
of the work** — see §9 for how phase 3 narrowed its scope (an
all-or-nothing layer, no partial-realization reconciliation).

**G2 — CLOSED. Planned devices have no identity.** Built as option (a):
`DesignPlacement.base_placement` (`models.py:515`, FK to the upstream
placement, `on_delete=SET_NULL`, `related_name="downstream_placements"`).
`clean()` now requires exactly one of `device` / `base_placement` for a
move/remove (`models.py:889`), and `_validate_base_placement()`
(`models.py:944`) enforces that `base_placement` points at a TRUE ancestor's
`add`.

The three-way FK distinction actually shipped is wider than the original plan
anticipated — a third FK, `base_parent_placement`, was needed for a blade
planned into a chassis that an *ancestor* design (not this one) planned:

- **`base_placement`** — WHICH: the upstream ancestor's `add` that IS the
  device this move/remove acts on. Always crosses designs.
- **`parent_placement`** (`models.py:607`, pre-existing) — WHERE: the chassis
  this blade goes into, planned by *this same design*. Mutually exclusive
  with `base_parent_placement` — a placement has exactly one of the two.
- **`base_parent_placement`** (`models.py:639`) — WHERE: the chassis this
  blade goes into, planned by an *ancestor* design. `_validate_base_parent_placement()`
  (`models.py:1091`) mirrors `_validate_base_placement()`'s ancestor check.

`base_placement` and `base_parent_placement` answer different questions and a
placement can legitimately carry both at once (a blade whose ancestor-planned
chassis was itself planned by an ancestor). `on_delete=SET_NULL` was chosen
over `CASCADE` for the same reason G8/§7.6 B1 gives for the real `device` FK —
cancelling an upstream add must not silently delete downstream work; instead
the downstream placement goes `stale` (§7.6 B1's mechanism, generalised).

Below, the original problem statement, still accurate as the reasoning: an
ancestor `add` produces no `dcim.Device`; it used to exist only as a
`DesignPlacement` row, and `DesignPlacement.device` being a required FK to
`dcim.Device` meant a server design could not move or delete a device the
network design planned.

**G3 — CLOSED, resolved as FLAGS per §8.4, not new slot states.** Confirmed in
`projection.py`: each slot dict carries `inherited`, `source_design_id`,
`conflict` and `conflict_reason` flags (`projection.py:329`, `:988`) rather
than new `ProjectedSlotState` members such as `base_existing` / `base_add`.
`_reconcile_item` in `api/views.py` guards the ancestor case (an item whose
`placement_id` belongs to an ancestor is not updated in place); dragging an
inherited tile creates a move in this design that references the base
placement (G2). The stale-delete sweep stayed scoped to `design=design`
(`api/views.py`), so that side was already safe.

Original reasoning for flags-over-states, still the rationale: `displaced` was
already a flag rather than a state, and that call was right — new members
would have doubled the rendering matrix in `docs/editor-behavior-spec.md` §3
for every state, and broken the legend's one-checkbox-per-state filter model.

**G4 — CLOSED. Freeze + staleness.** `Design.is_frozen` (`models.py:205`) is
checked at every write path that matters: `DesignPlacement.clean()`
(`models.py:787`), `DesignPowerFeed.clean()` (`models.py:1625`), and in
`api/views.py` via a shared helper, `_reject_frozen_design()` (`api/views.py:211`),
called from the placement, power-feed and save-layout write paths. Un-approving
a design with dependents is blocked (existence of children is checked before
the frozen design can go back to draft). The chain-health report lands as
`DesignChainHealthView` (`views.py:1532`, template `chain_health.html`),
backed by `_chain_health_rows()` / `_chain_health_detail()` (`views.py:1465`,
`:1440`), which is the "your base changed / your base placement vanished /
your target U is now occupied upstream" report the plan called for. Original
requirement, unchanged: enforce read-only on a design that is approved or has
dependents (view, API action and `save-layout` each need the check), plus a
report on the child, and a re-base action (`DesignRebaseView`,
`views.py`/`api/views.py`, see G9).

**G5 — CLOSED. Power and feeds across the chain.** `DesignRackPower.effective_custom_fields`
(`models.py:1697`) resolves the ancestor-aware custom-field view; the REST
`feeds` action (`api/views.py:890`) widens to ancestors so a child design's
PDU can bind an ancestor's planned `DesignPowerFeed`; and `editor.js` builds a
three-tier picker (real feed / this design's planned feed / an ancestor's
planned feed, `editor.js` around line 1231–1273, "A design-chain child sees
its approved ancestors' planned feeds", grouped into a section per source
ancestor, oldest-first). Original requirement, unchanged: `DesignPowerFeed.design`
and `DesignRackPower` are per-design; a server design inheriting the network
design's planned PDUs must see their bank bindings and be able to bind its own
PDU to an ancestor's `DesignPowerFeed` — the same ancestor resolution as G1,
applied to feeds, `power_config` and `DesignRackPower`.

**G6 — CLOSED 2026-09-02. Rack scope.** `derive` seeds the child with a
**snapshot** of the parent's `racks`, taken inside `transaction.atomic()`, in
both the UI view (`views.py`, `DesignDeriveView.post`, `child.racks.set(design.racks.all())`
at `views.py:1306`) and the API action (`api/views.py`, `DesignViewSet.derive`).
Settled semantics: this is a snapshot, not a live link — a rack added to the
parent later does not appear on the child, which owns its own scope from that
point on. Safe because baseline replay is per-rack (G1), so the child's
baseline chain doesn't depend on the parent's scope staying in sync.

Original requirement, unchanged: `Design.racks` M2M with a site-consistency
check (`models.py:148`, `clean()` `models.py:370`). A derived design should
inherit the base's scope as its starting set and be able to extend it.
`HiddenDesignRack` / `HiddenDesignChassis` are per (user, design) and just
need to not leak across the chain.

**G7 — PARTIALLY CLOSED.** Cycle guards are real ancestor walks for both
relations now: `based_on`'s guard is `Design.clean()` (`models.py:293`)
reusing `baseline_chain()`'s own walk (`models.py:236`) rather than
duplicating it — a longer cycle (A → B → A or deeper) raises there, not just
the immediate self-reference case. `depends_on`'s guard is a DFS in `clean()`
(`models.py:320`–`336`) that raises `ValidationError({"depends_on": ...})` on
a cycle.

**The `sequence`-from-chain-depth item was NOT done.** `sequence`
(`models.py:139`) is still auto-assigned as "last + 10" per site on first save
(`models.py:283`–`290`: `self.sequence = (last or 0) + 10`), unrelated to
chain depth. Still open — see "Still open" at the end of this document.

**G8 — OUT OF SCOPE, deliberately, per §9.3. NOT done.** Confirmed: no
`realized_device` field exists anywhere in `models.py` or `projection.py`.
This was cut on cost grounds (§9) — phase 3 built the all-or-nothing layer
instead (§9.2: a parent contributes its layer whole or not at all, driven by
`Design.status`; `implemented` refuses to project the chain and asks for a
re-base). The reconciliation story below (per-placement realized/pending/
drifted classification) is real design work that was consciously not built;
do not read its absence as an oversight.

Original problem statement, still the reasoning for why this gap exists: when
A is actually built, its adds become real devices, and every downstream
`base_placement` should re-resolve to the real device. There is no apply
mechanism at all, so chains make this gap load-bearing. Minimum: a
`realized_device` FK on `DesignPlacement` that a future apply step stamps, so
the resolution rule becomes "realized device if present, else the planned
placement." See §7 and §9 for the full analysis and the scope cut.

**G9 — CLOSED. Surface area.** REST: `chain` (`api/views.py:2006`), `derive`
(`api/views.py:2068`) and `rebase` (`api/views.py:2134`) actions on the design
viewset. GraphQL: `ancestors`, `children` and `is_frozen` on `DesignType`
(`graphql/types.py`). Filter: `no_parent` (`filtersets.py`, deliberately not
named to avoid the `rack_id`-style filter/action-param collision this repo has
hit before). Lineage panel: `design_rebase.html`
(`templates/netbox_rack_design/design_rebase.html`) — the brief's "lineage
panel" landed as part of the rebase flow rather than a separate standalone
template. Docs: `docs/design-chains.md` (12.5K). Tests: chain-related coverage
spans `test_projection.py`, `test_naming_example.py`, `test_graphql.py`,
`test_views.py`, `test_filtersets.py`, `test_models.py`, `test_naming.py` and
`test_api.py` — every layer named in the original ask.

Original requirement, unchanged: REST (`chain` / `ancestors` / `descendants`,
a "derive" action), GraphQL, a filter for "designs derived from X", a lineage
panel on the design page, docs (`docs/editor-behavior-spec.md` needs a
layering section plus conformance rows), and tests at every layer.

---

## 5. Phases

| Phase | Content | What it buys | Status |
|---|---|---|---|
| 1 | `based_on` chain resolution, cycle guards (G7), freeze rules (G4), parent selection **on the design create form** plus a "Derive design" action from the parent, re-base action, lineage panel. No projection change. | Chains are declarable and enforced | **Implemented** |
| 2 | `base_placement` FK + `clean()` relaxation + migration (G2); `settled_name` hook (§3.3). | Downstream can reference upstream planned devices, under the right name | **Implemented** |
| 3 | Layered `project_rack` — racks → tray → bays → full-depth (G1). Read-only elevation first, editor second. | The server designer actually sees the network design's result | **Implemented** (scope narrowed by §9: all-or-nothing layer, no realized/pending/drifted classifier) |
| 4 | Inherited tiles in the editor, save-layout guards, drag-on-inherited semantics (G3). | Editable chains | **Implemented** |
| 5 | Power / feed inheritance across the chain (G5); chain-wide family counters (§3.4). | Power and names are correct in a chain | **Implemented** |
| 6 | Staleness report (G4), `realized_device` (G8), API + GraphQL + docs (G9). | Durable | **Implemented, except `realized_device` (G8) — cut in §9, out of scope by decision** |

**Status (2026-09-02):** all six phases implemented. Full suite green at 1008
tests (it was 669 when phase-1 work began — see §7.6 B1, which landed just
before chain work started). Nothing is committed yet; the work sits in the
working tree on `main`, with migrations `0013_designplacement_stale_and_more.py`,
`0014_designplacement_base_placement.py` and
`0015_designplacement_base_parent_placement_and_more.py`.

The **re-base action** lands in phase 1, not here: it is the only resolution
mechanism for sibling conflicts (§2.1), so it is needed the moment a parent can
have two children. The staleness *report* that tells a child it should re-base
can follow later.

**Picking the parent (user, 2026-09-01):** choosing what a design is based on is
part of *creating* it — the create form offers the parent (restricted to
approved designs), rather than the relation being attached after the fact.

Phase 3 carries most of the cost: it touches every function in `projection.py`
and its whole conformance matrix. See §9 for the settled scope of phase 3 and
why it is narrower than §7.

### 5.1 Execution: who runs what

| Phase | Owner model | Effort | Parallelises into | Verification gate |
|---|---|---|---|---|
| 1 | Sonnet | Medium | 3 agents: (a) `baseline_chain()` + cycle guards in `models.py`, (b) create-form parent field + the re-base action view, (c) the lineage panel template. | `dev/test.sh` green — run by the orchestrator, never trusted from an agent report. |
| 2 | Sonnet (FK/migration) + Opus (naming hook) | High | 2 parallel agents, one per owner model. | Naming hook is written test-first, red before green. |
| 3 | Opus, single owner for the core replay — **deliberately NOT parallelised.** Fan-out only after the core lands: one agent per surface (tray, bays, full-depth mirroring). | Max | 1 (core) → then 3 | Full-world sweep + the `docs/editor-behavior-spec.md` §3 conformance matrix, plus a projection benchmark taken BEFORE the phase starts. |
| 4 | Sonnet | High | 3 agents: (a) slot flags + widget payload server-side, (b) editor rendering + legend entries, (c) drag-on-inherited semantics + the `_reconcile_item` ancestor guard. | In-browser self-verification with the dev drag tracer — not e2e alone. |
| 5 | Sonnet, with Opus review of the counter change | High | 1, reviewed | Test-first; reviewed before merge. |
| 6 | Haiku or Sonnet | Low | 4 independent agents: REST, GraphQL, docs, the report view. | `dev/test.sh` green. |

**Phase 2 reason:** the naming hook must enforce §3.2 R1–R3 (settled name on
inherited slots, prefixes never stack, de-prefix once per layer) and a subtly
wrong strip silently corrupts every downstream name.

**Phase 3 reason:** this is where a cheap model produces plausible-but-wrong
output — the replay touches every function in `projection.py`, and a mistake
renders a believable rack that is simply false. The benchmark is taken before
the phase starts so the ~N× chain-depth cost is measured, not guessed.

**Phase 4 reason:** the e2e shim masks added-only paths, so a green e2e run is
not evidence a real drag works.

**Phase 5 reason:** widening `_next_number` / `_next_pdu_slot`
(`naming_example.py:107`, `:118`) to the whole chain is collision-critical — too
narrow and B reserves a name A already took; too wide and it skips numbers
forever.

**Standing rules**

- Test-first is blocking for every phase, per the repo's existing rule.
- The orchestrator runs `dev/test.sh` itself and never reports an agent's
  claim as verified.
- No phase is done until the full-world sweep and the editor e2e suite are
  green.
- Frontend phases self-verify in a real browser in the same session; handing
  Petr a document to test manually is not delivery.

---

## 6. Open questions

None outstanding — §2.1 (tree, one parent / many children), sibling resolution
(first approved wins, the other re-bases) and §3.3 (config-declared
`prefix_source`, hook not column) are all settled as of 2026-09-01, and all
shipped as designed (re-base action confirmed at `views.py`/`api/views.py`
`DesignRebaseView` / `rebase` action; `prefix_source` config path confirmed as
the resolution mechanism in `naming.py`).

Consequence folded into §5: **re-base landed in phase 1**, not phase 6. It is
the only resolution mechanism for sibling conflicts, so it was required as
soon as a parent could have two children — alongside the freeze rules.

---

## 7. What a child looks like after the parent is applied

Question (user, 2026-09-01): A is applied — by a future apply step, or by
external automation that turned everything A planned into real `dcim` objects.
Open B. What does it show?

### 7.1 The invariant

> **Applying an ancestor must be a no-op for the child's projection.** B renders
> the same devices, at the same U, under the same settled names, before and
> after A is applied. Only a tile's *provenance* changes — inherited-planned
> becomes real.

Anything else means the plan lied about the world it promised.

### 7.2 Why replay must reconcile, not just replay

B's baseline is `reality + replay(A)`. After apply, reality **already contains**
A's effects, so a blind replay double-counts:

| A's placement | Reality after apply | Blind replay does |
|---|---|---|
| `add` at U10 | real device at U10 | second, phantom tile at U10 → collision |
| `move` X → U20 | X already at U20 | vacates an empty source, occupies an occupied target |
| `remove` X | X gone | nothing — harmless, but hides drift |

So the replay classifies **per placement, not per design**: a `realized`
placement is *skipped*, because `_existing_slots()` (`projection.py:264`) already
covers it. Per-placement is required, not cosmetic — automation that applied
half of A must leave B with `reality (applied part) + replay(the remainder)`.

### 7.3 Three verdicts per ancestor placement

- **`pending`** — not in reality yet. Replay it, as in G1.
- **`realized`** — reality matches the plan. Skip the replay; bind
  `realized_device`.
- **`drifted`** — reality disagrees with the plan (different U, different name,
  different device type, or the device is somewhere nobody planned).
  **Reality wins for B's baseline** — B must plan against the world that
  actually exists — and A is flagged so a human sees that plan and reality
  diverged. B surfaces a banner; it never silently renders a plausible-looking
  world (see the no-silent-failure rule that governs the distribution engine).

### 7.4 Detecting realization

Two paths, and they differ entirely:

1. **Our own apply (G8)** stamps the result: `realized_device` FK for an `add`,
   a realized marker for `move` / `remove`. Deterministic and cheap.
2. **External automation** stamps nothing, so realization must be *detected* — a
   reconciliation pass over the ancestor's placements:
   - `add` → a real device at (`target_rack`, `target_position`, `target_face`)
     whose name equals `settled_name(placement)` (§3.3) and whose device type
     matches → realized; bind `realized_device`.
   - `move` → the referenced device now sits at the target.
   - `remove` → the device no longer exists.

   Name + position + type is the only signal available. A partial match is
   `drifted`, not `realized`.

This is also why `settled_name` must be right (§3.3): it is the join key between
a plan and the reality it produced.

### 7.5 Identity re-binding, and chains collapsing

The resolution rule from G8 — *realized device if present, else the planned
placement* — is what lets B keep working across the transition. Better still,
apply **rewrites B's `device` FK** to the now-real device, so B's `move` /
`remove` becomes an ordinary real-device operation and B stops depending on A.

That yields the clean end state: **once A is fully realized it drops out of the
stack**, `baseline_chain()` for B collapses to reality alone, and B is a normal
one-layer design again. Chains are temporary by construction — they exist only
while an ancestor is planned-but-not-built.

### 7.6 Two bugs this scenario exposes

**B1 — `device` was `CASCADE` — FIXED, in the working tree, not yet released.**
Applying A's `remove` used to delete the real `dcim.Device`; CASCADE then
silently deleted every downstream `DesignPlacement` pointing at it, destroying
B's planned work with no trace — a design erasing its own history, since the
`remove` row recording the deletion would itself vanish along with the device.
This was already reachable today by hand-deleting a device in DCIM — chains
only make it catastrophic. `DesignPlacement.device` (`models.py:471`) is now
`SET_NULL`, paired with two new fields — `stale`
(`default=False`) and `stale_device_name` (`CharField(64)`, captured at
deletion time, because "some device is gone" is not actionable) — added in
migration `0013_designplacement_stale_and_more.py`. A new `pre_delete`
receiver on `dcim.Device` (`signals.py`, wired via `PluginConfig.ready()` in
`__init__.py`) stamps both fields via `snapshot()` + `save()` so the
transition is changelogged; ordering is guaranteed by Django's
`Collector.delete`, which sends every `pre_delete` before the `field_updates`
pass that applies `SET_NULL` and touches only the `device` column, so the
stamp survives. `clean()` now requires a device for `move`/`remove` unless
`stale`, forbids `add` from ever being stale, clears `stale` the moment a
device is reassigned (so re-pointing revives the row with no separate
action), and returns early on a stale placement before target-slot
validation, since there is no device type left to measure a slot with. No
projection change was needed — `project_rack` already filtered
`.filter(device__isnull=False)` for move/remove (`projection.py:1177`), and
the save-layout stale-deletion sweep already required `p.device_id is not
None` (`api/views.py`) — so a null device degrades safely and the surviving
row cannot resurrect as a phantom tile. Surfaced via `Design.stale_placements`,
a card on `design.html`, rows on `designplacement.html`, an always-visible
`alert alert-warning` in `design_editor.html`, a default `stale` table column,
a `stale` filter, and `stale`/`stale_device_name` as `read_only_fields` in the
API serializer (staleness is an observation, never client input). Covered by
`StalePlacementTestCase` in `tests/test_models.py` plus new cases in
`tests/test_projection.py`, `tests/test_api.py` and `tests/test_filtersets.py`;
full suite green at 669 tests **at the point B1 landed** — this was before
chain-phase work started (see §5's Status paragraph: 669 is the same number
quoted there as the phase-1 starting point). The suite has since grown to
1008 tests with the six chain phases; 669 is not stale, it is a snapshot in
time and is left as-is.

**B2 — OUT OF SCOPE, belongs to G8, not done.** Feed bindings re-resolving on
apply (A's planned PDUs and `DesignPowerFeed` rows becoming real `dcim`
objects, requiring B's `planned_power_feed` to re-point at the corresponding
`real_power_feed`) is the feeds instance of the same realized-else-planned
rule G8 describes for devices. G8 was cut in §9 on cost grounds, so B2 is cut
with it — no `realized_device`-equivalent re-binding exists for feeds either.
The model still carries both FKs with `bound_feed` normalising them
(`models.py:766`, see `bound_feed` — confirmed as `return self.real_power_feed or self.planned_power_feed`),
so the mechanism this bug describes remains exactly where it was: unbuilt.

### 7.7 On screen

Same tiles, same positions, same settled names. Realized tiles lose their
inherited-planned styling because they are simply real devices now. The lineage
panel shows A as implemented with a `realized / pending / drifted` count, and any
non-zero drift raises a banner on B with a link to the reconciliation report.

### 7.8 Phase impact

Reconciliation is not phase-6 polish: without it, the first ancestor that gets
applied corrupts every child's projection. `realized_device` + the three-verdict
classifier move into **phase 3**, alongside layered projection — the replay and
the skip-if-realized rule are the same code path. **B1 (the CASCADE fix) was
independent of chains and has now landed — `SET_NULL` + staleness, in the
working tree, awaiting release** (current released version is 0.24.0).

§9 reverses this move: `realized_device` and the three-verdict classifier are
cut from phase 3 and out of scope entirely, on cost grounds.

## 8. How a conflict looks

### 8.1 The three channels that already exist

| Channel | Blocks save? | Mechanism |
|---|---|---|
| Hard collision | Yes | `_validate_target_slot()` (`models.py:677`) appends to `errors` → 400 in `save_layout` (`api/views.py:1082`); shown as a `createToast` plus a `.nbx-rd-error` red ring from `highlightError()` (`editor.js:5470`); drag snaps back (spec §4.7). |
| Displacement | No | vacating tile collapses (`.nbx-rd-displaced`) and a red diagonal-stripe bar (`.nbx-rd-stripe`, `editor.css:1046`) hangs OUTSIDE the rack's right edge with `title="was: X"`, set by `_mark_displaced()` (`projection.py:463`), spec §4.3. |
| Soft warning | No | badge/tint only — name collision (`.nbx-rd-name-collision` inset ring + `mdi-alert` badge, `editor.js:5784`), power capacity (`.nbx-rd-power-warn`/`-critical` bar). |

### 8.2 Which channel a chain conflict belongs to

> An upstream conflict must NOT block save. B did not cause it and cannot fix
> it by editing that tile, so the hard-collision path (rejection + snap-back)
> is wrong despite being the one that feels like "conflict".
>
> Nor is a toast right: a toast is transient, and an upstream conflict
> persists across sessions until someone re-bases.

### 8.3 The structural gap

`ProjectedElevation` (`projection.py:127`) carries only `design`, `rack`,
`front`, `rear`, `non_racked`, and a `power` dict. There is no
warnings/errors/conflicts collection anywhere in the projection contract, so
today there is nowhere to hang "this design has a problem that is not a
slot". First change: a `conflicts` list on `ProjectedElevation`, each entry
`{kind, severity, slot, placement, source_design, detail}`, rendered by a
PERSISTENT panel. The stale-placement alert already shipped in
`design_editor.html` (§7.6 B1) is the seed of that panel — build one
component and put both kinds in it, rather than two parallel alerts.

### 8.4 Flags, not new slot states

> Provenance and conflict are FLAGS on a slot dict, not new
> `ProjectedSlotState` members: `inherited` + `source_design_id`, and
> `conflict` + `conflict_reason`, alongside the existing `displaced`.

Reason: `displaced` is already a flag rather than a state, and that call was
right. New members (`base_existing`, `base_add`, …) would double the
rendering matrix in `docs/editor-behavior-spec.md` §3 for every state, and
break the legend's one-checkbox-per-state filter model
(`design_elevation.html:61`, `legend_filter.js`).

### 8.5 What the planner sees

1. **Inherited tile, no conflict** — draws as a normal device, dimmed/
   outlined for provenance, hover card names the source design; dragging it
   creates a move in THIS design (G2).
2. **Upstream vacated what you built on** (A cancelled its add) — the
   placement goes inert by exactly the mechanism shipped in §7.6 B1,
   generalised from the `device` FK to `base_placement`. Panel row plus
   re-point / drop actions.
3. **Upstream now occupies your target U** — the tile keeps its position and
   gets a conflict marker: reuse the stripe-bar geometry (it already hangs
   outside the frame, so it never fights the occupant) in a distinct amber,
   red staying reserved for displacement. Save still succeeds; the design is
   knowingly in conflict until re-based.
4. **Reality drifted after apply** (§7.3) — same panel, e.g. "planned U10,
   built at U12"; the baseline follows reality.

### 8.6 Legend debt

The legend lists only the five normal states; `displaced` and the
`.nbx-rd-error` ring appear in it NOWHERE, so a user must hover a stripe to
learn what it means. Any new inherited/conflict marker must add its legend
entry, and should fix the two existing omissions rather than replicate the
gap.

### 8.7 Docs

`docs/editor-behavior-spec.md` §3 (rendering table: state × part) needs rows
for inherited and conflict; §4.3 and §4.7 need to say explicitly that an
upstream conflict follows neither the displacement nor the rejection path.

---

## 9. Scope decision: what phase 3 actually builds

Settled (user, 2026-09-01), driven by cost.

### 9.1 The rejected option — sequential-only chains

The cheapest conceivable version was "a child may only be planned once its
parent is applied" — no replay at all, because reality would already contain
the parent. Rejected for two independent reasons.

1. **There is no apply step to depend on.** `STATUS_IMPLEMENTED`
   (`choices.py:18`) is a status string a human sets by hand; nothing verifies
   it. The rule reduces to "someone ticked a box", and a wrong box means the
   child plans against a world that does not exist, with nothing to catch it.
   Making it trustworthy needs either an apply feature (unbuilt, in no phase)
   or the reconciliation of §7.4 — the very cost being cut.
2. **It deletes the feature.** The goal (§ top) is planning on the world the
   parent LEAVES BEHIND — future tense; physical network moves take weeks. If
   the child can only start after the parent is physically done, reality
   already contains the parent and the child is an ordinary single-layer
   design, which the plugin supports today with zero new code. Phases 1–2
   would then buy a `based_on` label and a freeze rule, nothing more.

### 9.2 What phase 3 builds instead — an all-or-nothing layer

> A parent contributes its layer WHOLE or NOT AT ALL, driven by the existing
> `Design.status`: `approved` → replay every placement; `implemented` → refuse
> to project the chain.
>
> A child whose parent is marked `implemented` must be re-based before it will
> render. Re-basing drops the parent from the stack — which is where §7.5 says
> a fully realized ancestor belongs anyway.

### 9.3 What this drops from §7

Explicitly out of phase 3: `realized_device` (G8), the pending/realized/
drifted classifier (§7.3), per-placement realization detection (§7.4), partial
application, and drift reporting. §8's conflict UI reduces to a single panel
row ("your parent is implemented — re-base"), leaving the `conflicts` list on
`ProjectedElevation` (§8.3) as the only structural addition; the slot flags,
the amber stripe and the legend entries wait.

### 9.4 What is irreducible

The replay engine itself stays: `baseline_chain()` plus replaying ancestor
placements through racks, tray, bays and full-depth mirroring (G1,
`projection.py`). That is not a cost to optimise away — it IS the feature.

### 9.5 The risk accepted, and why it is honest

A partially-applied parent is not handled. But the failure mode is a BLOCK,
not a lie: instead of drawing a plausible rack that is quietly wrong, the
chain refuses to project and says re-base first. That keeps the no-silent-
failure rule that governs the distribution and naming engines — a rule, not
an engine, and therefore nearly free.

### 9.6 Cost

§5.1's estimate changes: phase 3 from roughly $15–40 to $8–20, phase 4 shrinks
with §8 reduced to one panel row, total from roughly $35–95 to $20–45. Basis:
Opus 5 at $5/$25 per Mtok, Sonnet 5 at $3/$15, Haiku 4.5 at $1/$5, with a
measured Sonnet code agent running 75–114k tokens. The swing is dominated by
how many red→green cycles the replay needs, which is why the phase stays
test-first (§5.1).

---

## 10. Still open

Everything else in this document is closed. What remains, collected in one
place:

1. **G8 / the realize story — out of scope by deliberate decision (§9), not a
   gap.** No `realized_device` field, no pending/realized/drifted classifier,
   no partial-application handling. B2 (§7.6) is the feeds instance of the
   same cut. If a future need for a real apply step arises, §7 is the design
   record to restart from.
2. **The `sequence`-from-chain-depth item (G7) was not done.** `sequence`
   (`models.py:139`) is still assigned "last + 10" per site
   (`models.py:283`–`290`), independent of chain depth. Cycle guards
   themselves (the rest of G7) are done.
3. **Nothing is committed.** All six phases and migrations `0013`–`0015` sit
   in the working tree on `main`, unreleased. Current released version is
   0.24.0 (§7.6 B1).

No other gaps turned up during verification of this document against the
code.
