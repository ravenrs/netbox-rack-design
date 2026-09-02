# Design chains

Two teams often plan the same rack in sequence: one moves and removes network
gear, the next plans servers on the world that move leaves behind. A **design
chain** lets the second design see the first design's result *before* anyone
has physically touched a cable — as if the first design had already happened.

Baseline on an **approved** design, and every placement it made — adds,
moves, removals — becomes part of the world your design starts from.

## Deriving a design

A design becomes eligible to be a parent the moment it is **approved**.
Approval is what freezes it (see below), and freezing is what makes it safe
for another design to build on.

From an approved design's page, **Design chain → Derive design** creates a new
draft design with `Based on` pointed at it, in the same site. You can also set
`Based on` directly on the create/edit form (**Design chain → Derive design**
is a convenience for the common path). Either way:

- the parent must be **approved**;
- the parent and the child must be the **same site** — a chain never crosses
  sites, because a parent's placements are scoped to its own site's racks;
- a design may have **only one parent**, but a parent may have many children.
  The lineage is a tree, not a graph: each design's own baseline is a strict,
  linear stack of its ancestors.

Open the child's editor and the parent's result is already there — the moved
device is at its new position, the removed one is gone, the added one is
sitting in its planned slot — rendered as ordinary, existing hardware. You
plan on top of it exactly as you would on top of real DCIM data.

The design page's **Design chain** card shows the parent, the full ancestor
stack (oldest first), and every child based on this design, with **Derive
design** and **Re-base** actions.

## What the freeze means, and how to get out of it

Approving a design commits it. From that point its placements, planned power
feeds, rack power overrides and rack scope stop moving — because the moment a
design can be a parent, every child that baselines on it needs to trust that
the ground will not shift underneath it.

You cannot simply un-approve a design once something depends on it — the
design page and the model both refuse, with a message naming the children
that would lose their baseline. Two ways forward:

- if the design genuinely has no children yet, set its status back to
  **draft** to edit it directly;
- if it does have children, create a **new version** of the design (the
  existing versioning feature), make your changes there, approve the new
  version, and use **Re-base** on each child to point it at the new version
  instead of the old one.

**Re-base** (on the design page, or the design's `Based on` field) is also
the escape hatch for two other situations: a parent that has since moved to
**implemented** (see below), and two designs that both baselined on the same
parent — whichever is approved first keeps that baseline; the other re-bases
onto it (see [Siblings are blind to each other](#Siblings-are-blind-to-each-other)).

## Inherited tiles

A tile that came from an approved ancestor's layer — not from reality, and
not from your own design — is marked **Inherited**. Hovering it names the
source design. It renders with the styling of ordinary existing hardware
(an ancestor's `add`, `move` or `remove` is not a proposal from your point of
view — it already happened), and the editor's legend has a dedicated
**Inherited** checkbox so you can filter it in or out alongside the five
state filters (Existing / Add / Move in / Move out / Remove).

Dragging an inherited tile does not edit the ancestor's placement — that
design is frozen. It creates a **move in your own design**, referencing the
upstream identity. If the ancestor later changes (a new version, or a
different result), your move still points at the same referenced identity;
if that reference vanishes (the ancestor's `add` was itself cancelled), your
placement goes **stale** rather than silently disappearing — the design page
reports every stale placement so nothing is lost without a trace.

## What a refused chain looks like, and how to fix it

A parent contributes its layer **whole, or not at all**. The rule is driven
by the parent's status:

- **approved** → every one of its placements is replayed into your baseline.
- **implemented** → the chain refuses. Reality may already contain part of
  what that design planned (someone applied it outside the plugin), so
  replaying it again would double-count. Nothing from that design — or from
  anything stacked on top of it — is inherited until you act.
- anything else (**draft**, or any other non-approved status somewhere in
  the chain) → the chain refuses for the same reason in reverse: an unapproved
  design is still free to change, so building on it would mean planning
  against a world that might not exist tomorrow.

A refusal is a **block, not a lie**. Your rack and your own design's layer
still render — only the inherited layer is missing. A banner on the design
page and a persistent panel in the editor name exactly which ancestor is the
problem and why, with a link to it. The fix is always the same: **re-base**
this design onto a different (approved) ancestor — typically a new version of
the one that moved to `implemented`, or the design that has since taken over
as the approved sibling.

This is deliberate: the alternative — silently drawing a plausible rack that
quietly disagrees with reality — is exactly the failure mode the naming and
distribution engines already refuse to produce.

### Upstream conflicts (do not block Save)

Once a chain is accepted, two more things can go wrong that are **not**
severe enough to refuse the whole chain, and — unlike a hard placement
collision — **never block Save**:

- **the tile you built on top of vanished** — an ancestor's `add` you moved a
  blade into, or moved/removed further, was itself cancelled. Your
  placement's reference goes stale; you can re-point it or drop it.
- **an ancestor now occupies your target unit** — a later-approved ancestor
  version, or a re-based lineage, ends up putting inherited hardware where
  you had already planned something. The tile keeps its position and gets an
  amber conflict marker (the same stripe-bar geometry used for displacement,
  but a different colour — red stays reserved for displacement). Save still
  succeeds; the design is knowingly in conflict until someone re-bases.

Both surface as rows in the editor's persistent **Design conflicts** panel —
never a toast, because an upstream conflict outlives the session it was
noticed in. The legend's **Conflict** checkbox filters these tiles the same
way **Inherited** does.

## Siblings are blind to each other

Two designs based on the **same** approved parent do not see each other's
placements. If both plan into the same unit, or both generate the same name,
nothing detects the clash while both are in progress — it surfaces the
ordinary way, as the ordinary name-collision warning, once someone actually
tries to save into an occupied unit.

**First approved wins.** The other design re-bases: once one sibling is
approved, use **Re-base** on the other to point it at the newly-approved
design instead of the old shared parent, and its own placements are then
checked against that result.

## Naming across a chain

A placement's `proposed_name` is a **planning** name — inside the design that
owns the change, it typically carries a project marker (`IDS-1234_old-name`
if your organisation tags work by ticket). That marker is bookkeeping for the
design that made the change, not part of the device's identity, so a design
built on top of it must never see it.

- **An inherited placement renders under its settled name** — the planning
  prefix stripped off, once. `IDS-1234_old-name` inherits as `old-name`.
- **Prefixes never stack.** If your design moves that same device again, it
  gets *your* design's prefix, not both: `IDS-5678_old-name`, never
  `IDS-5678_IDS-1234_old-name`.
- **Family counters span the whole chain.** If your naming convention
  continues a numbered family (`ams1-sw-4` → next is `-5`), the counter
  counts your own design's placements *and* every ancestor's placements
  (matched under their **settled** names) — so you never hand out a number an
  ancestor already reserved. Siblings are deliberately excluded (see above).

### Configuring the settled-name resolver

The planning prefix is a **project name** — it is not reliably derivable from
the design title, and it usually lives in a custom field that is specific to
your deployment. The plugin never hardcodes that field. Configure where the
prefix comes from under the `naming` key:

```python
PLUGINS_CONFIG = {
    "netbox_rack_design": {
        "naming": {
            # A dotted path, resolved relative to the design, to the field
            # that holds the planning-project token. Left empty, the token is
            # derived from the design title instead (an "IDS-<digits>" pattern).
            "prefix_source": "cf.<your project field>",

            # Optional: a dotted path to fn(placement) -> str replacing the
            # built-in prefix strip entirely. Left empty, the built-in strip
            # is used.
            "settled_name": "",
        },
    },
}
```

`prefix_source` (and `settled_name`, if you supply one) use the same dotted
attribute-path grammar as the naming templates and the planning-fields
bridge — see [Device naming](device-naming.md) and
[Planning fields](planning-fields.md). If a `prefix_source` is configured but
resolves to nothing for a design whose placements are being inherited, that
is reported as an error rather than falling back to a plausible-but-wrong
name — the same no-silent-failure rule that governs the naming and
distribution engines.

## Power across a chain

An approved ancestor's planned power feeds and rack-power overrides are
inherited the same way placements are:

- **Rack capacity.** An ancestor's planned `DesignPowerFeed` rows widen a
  greenfield rack's capacity figure, exactly as your own design's planned
  feeds do — so the capacity bar reflects the power the rack will actually
  have once the whole chain is realised, not just what you added yourself.
- **Rack power overrides merge oldest-first.** A distribution script's view
  of a rack's custom fields (`power_limitation`, `pdu_location`, or whatever
  your deployment declares) merges every ancestor's override, oldest first,
  with your own design's override winning last if you set one. This only
  matters for the `script` distribution tier — the `builtin` tier still
  reads no custom fields at all, chain or no chain.
- **Binding to an inherited feed.** A PDU you plan can bind to a feed an
  ancestor planned, not just to a real feed or one your own design defined.
- **Inherited PDUs contribute their banks.** An ancestor's planned PDU
  renders as ordinary (inherited) hardware, so it is picked up by the
  distribution engine exactly like a real PDU — its banks and bindings count
  toward the per-bank heatmap without any special handling.

See [Power distribution](power-distribution.md) for the distribution tiers
themselves; this section only covers what a chain adds to them.

## What happens when the parent is actually built

The plugin never applies a design to DCIM for you, and it does not detect on
its own that the physical work described by an approved parent has actually
happened. What it does react to is the parent's **status**.

While the parent stays **approved**, its layer keeps being replayed into
every child exactly as planned — nothing changes just because the hardware
was physically installed in the meantime.

The one status change that matters is marking the parent **implemented**.
The moment you do, every child stops inheriting from it (see
[What a refused chain looks like](#What-a-refused-chain-looks-like-and-how-to-fix-it))
until it is re-based — deliberately: once a design is implemented, its
result belongs to real DCIM data, and a child should baseline on *that*
directly rather than on a second copy of the same plan replayed on top of
it. This is an all-or-nothing switch: the plugin has no way to tell whether
only *part* of an implemented design was actually carried out, so it never
guesses — it blocks the whole chain and asks you to re-base, rather than
risk drawing a rack that looks right and silently is not. Re-basing a child
at that point commonly means pointing it at nothing (dropping the chain
entirely, now that reality already contains what the parent planned) or at
whatever design has since taken over as the approved parent.
