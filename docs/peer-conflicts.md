# Peer conflicts

Two teams often plan the *same* rack without either baselining on the other —
neither is an ancestor or descendant of the other, so [Design chains](design-chains.md)'s
inheritance never puts one in front of the other. Without a chain relating
them, both were blind to each other while planning: both could claim the same
unit, the same name, or the same real device, with nothing reported until
someone actually tried to `apply` one of them. A design's projection now
reports these overlaps as they happen, so a planner sees the clash while
there is still time to talk to the other side.

## Who counts as a peer

A **peer** is another design that scopes the rack being projected, is not
`implemented`, and is neither:

- **in this design's lineage** — an ancestor or a descendant. An ancestor's
  claim on a unit is inheritance, already drawn as an [inherited
  tile](design-chains.md#Inherited-tiles); it is not a peer conflict.
- **another version of the same plan** — versions of one plan claim the same
  units by construction, and at most one version may ever be [approved](design-chains.md#What-the-freeze-means-and-how-to-get-out-of-it),
  so comparing them would only ever report the plan against itself.

An `implemented` design is excluded too, for a different reason: its
placements are no longer a *plan* competing with yours, they are real DCIM
data, and the ordinary occupancy rule that governs any real device already
owns that case.

Everyone else who scopes the rack is a peer, whether their design was created
before or after yours, and whether or not either of you knows the other
exists.

## What is detected

Three kinds of overlap, each reported with one of these exact `kind` values:

- **`peer_slot_claim`** — a peer plans a device on a unit this design claims.
- **`peer_name_claim`** — a peer plans the same name.
- **`peer_device_claim`** — a peer plans to move the same real device
  somewhere. Both plans are legal on their own; whichever gets applied first
  makes the other one false.

## What "a unit this design claims" means

For `peer_slot_claim`, "claims" means this design's own adds and move-ins,
plus any unit it inherits from an approved ancestor — the same units that
render as `add`, `move in`, or `inherited` tiles in the editor.

It deliberately does **not** mean untouched reality. A peer dropping a device
onto a real, already-occupied unit that this design never touches is the
peer's own collision — its own occupancy rule already catches it — and
reporting it here as well would put a conflict in every design that merely
shares a busy rack with another planner, whether or not the two plans
actually overlap.

**Removals are not a case.** If a peer plans to *remove* the device at a
unit, this design still sees the real device standing there (a removal
hasn't happened yet), and the ordinary occupancy rule governs exactly as if
no peer existed.

## Severity, and why nothing blocks Save

An **approved** peer's claim is reported as an **error**; a **draft** peer's
claim is a **warning** — an approved plan is more likely to actually happen.

Neither severity blocks a save. A peer conflict is not this design's fault,
and there is nothing to fix by editing the tile — the tile is correct as
drawn from this design's own point of view. The two ways out are talking to
the other planner, or changing this design's own plan (a different unit, a
different name).

**What IS refused:** once a peer has actually **applied**, it owns a real
`dcim.Device`, and saving this design onto that unit is refused outright by
the editor's existing collision check — that behaviour is pre-existing, not
new here. Peer detection only covers the window before either side has
applied.

## Not covered

**Device bays.** Peer detection reads rack U slots only. A peer claiming a
device bay — a blade inside a chassis this design also plans into — is not
detected, and nothing else catches it either: the uniqueness rule on a
planned bay is scoped to one design, so two designs can both plan a blade
into the same bay of the same chassis and both saves succeed. Until this is
covered, two teams planning blades into a shared chassis need to coordinate
outside the plugin.

## Where it shows

A peer conflict is a third row in the editor's **Design conflicts** panel,
next to the stale-placement and upstream (chain) rows, with its own wording:
[re-basing](design-chains.md#What-a-refused-chain-looks-like-and-how-to-fix-it)
does **nothing** about a peer, because a peer is not upstream of this design
— there is no ancestor to point at instead.

A contested tile is flagged in the rack itself, on both the interactive
editor and the read-only elevation view, with a **Peer conflict** legend chip
to filter contested tiles in or out alongside the existing state and
Inherited/Conflict filters.

Several `peer_name_claim` rows against the *same* peer design collapse into a
single row with a **Show** toggle listing each colliding name — a peer whose
naming convention continues the same family can otherwise claim a whole
batch of names at once, and a batch is more useful read as one row than as a
wall of identical-looking lines. A single collision against a peer stays a
plain row.

## Re-running naming

A **Re-run naming** button on a name-claim row opens a read-only `old ->
new` diff for the colliding placement(s) in that row. **Confirm** writes
every line shown; **Cancel** writes nothing.

Only the **colliding** names are recomputed. A clean sibling placement in the
same design is left alone, even if that leaves the rack's numbering reading
`10, 8, 9` top-down instead of a tidy `8, 9, 10` — a clean name may already
be referenced elsewhere (cabling, documentation, another design's own plan),
and renumbering it for cosmetic contiguity is not worth invalidating a name
nobody asked to change. A name the planner simply dislikes is fixed
individually afterwards through the existing per-placement rename, unrelated
to this dialog.

### Why the family counter never reserves against a peer's draft

[Family counters](design-chains.md#Naming-across-a-chain) count reality and
this design's own chain — never a peer's plan, and deliberately not even a
peer's *draft*. A draft may stay a draft forever, and a number the counter
skipped to avoid an abandoned draft would be burned permanently for no
reason. So it is entirely legitimate for two drafts to independently land on
the same planned name; that is exactly the situation this feature reports.
It resolves itself the moment one side is applied — at which point the
*other* design collides with a now-real device, which the existing
occupancy check catches the ordinary way.

One consequence of this worth knowing before you click Re-run naming:
re-running can hand back a name that **still** collides, because both sides'
counters legitimately land on the same next number again. The dialog marks
such a line `(still collides)` rather than presenting it as a fix it did not
make, and marks a line whose name did not change at all `(unchanged)` — both
are still written on Confirm, since a line the dialog showed is a line it
promised to write.

## REST API

- `GET /api/plugins/rack-design/designs/<pk>/conflicts/` — requires
  `view_design`, writes nothing. Returns `{"conflicts": [...]}`, where each
  entry has `kind`, `severity`, `detail`, `rack_id`, `source_design_id`,
  `source_design_name`, and `slot_key`. This also carries **chain**
  conflicts, not only peer ones — the point is that an automation pipeline
  can gate on one call before ever reaching `apply`, which is otherwise the
  first thing that would tell it about a problem.
- `POST .../designs/<pk>/rerun-naming-preview/` — requires `view_design`,
  writes nothing, returns the same diff the dialog shows.
- `POST .../designs/<pk>/rerun-naming/` — requires `change_design`, writes
  the confirmed set. A frozen (approved) design refuses with 409, like every
  other write action on a design.
- Both rename endpoints take `{"placement_ids": [...]}` and recompute the
  diff server-side from scratch rather than trusting the request body — so a
  dialog left open while the world moved on cannot rename a placement that no
  longer collides; the whole request is refused if any listed placement is
  not *currently* claimed by a peer name.

## Configuration

`peer_conflicts_enabled` in `PLUGINS_CONFIG` (default `True`) controls
whether peer detection runs at all. See the [configuration
table](index.md#Configuration) for where it sits alongside the plugin's
other keys.

**Why a flag exists at all.** A peer design's **title** is shown in a
conflict's `source_design_name` even when NetBox's own object permissions
would otherwise hide that design from the reader — "another design claims
U36" without a name is not actionable; you cannot go talk to anyone about
it. **No other field of the hidden design is exposed** — the title is a
plain string, never a nested representation of the peer design. For a
deployment where even that disclosure is unacceptable, setting
`peer_conflicts_enabled` to `False` turns off peer detection entirely — no
peer query runs, and no peer design's title is ever returned.
