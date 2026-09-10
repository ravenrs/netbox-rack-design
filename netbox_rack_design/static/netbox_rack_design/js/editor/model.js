/*
 * The editor's read-model (docs/editor-behavior-spec.md §2) and the placement
 * rules built on it -- extracted verbatim from editor.js, which had grown to
 * 8455 lines.
 *
 * This module is self-contained BY CONSTRUCTION: it closes over nothing from
 * the editor, reads only the rendered DOM plus each rack's embedded
 * `rd-editor-data-<rackId>` JSON payload, and mutates neither. That is what
 * made it the first piece to move -- a pure lift, with imports standing in for
 * what used to be closure lookups.
 *
 * Exports, and who needs them:
 *   rdCanPlaceAt      -- the SINGLE authority for "is this target legal"; live
 *                        on every drag/drop (7 call sites in editor.js)
 *   rdBuildModel      -- the DOM -> model scan
 *   rdCheckInvariants -- spec §6 I1/I2 and §9.2 I4
 *   rdLabelFor        -- also used by the displacement scan
 *
 * `window.__rdModel` stays published here: it is the console/Playwright poking
 * surface, and it is read-only.
 */

// ==========================================================================
// ---- Phase 1 read-model (spec §2) ----
// ------------------------------------------------------------------------
// Introduces the domain classes (`RDDevice`/`RDShadow`/`RDGhost`/`RDFace`/
// `RDRack`) from docs/editor-behavior-spec.md §2, and a builder
// (`rdBuildModel`) that populates them by scanning the CURRENT rendered DOM
// and each grid item's live GridStack node. Per the migration plan (spec
// §7), this is Phase 1 ONLY: a read-only snapshot/query layer rebuilt on
// demand. It never drives behaviour, never mutates the DOM/grids, and is
// never called automatically except by the guarded debug hook below.
//
// Device identity (deviceId/placementId) is resolved, best-effort, from the
// per-rack `rd-editor-data-<rackId>` JSON payload via each tile's
// `data-widget-index` -- the same payload initRack() hydrates from. A tile
// added or cross-rack-adopted THIS session has no matching payload entry
// (the payload is only the server-rendered original layout), so its
// deviceId/placementId legitimately read back as null; the model still
// recovers its label/state/position purely from the DOM, which is enough
// for occupancy accounting (I1) and full-depth shadow pairing (I2).
// ==========================================================================

// device_id -> true for every full-depth real device seen in any rack's payload
// (a full-depth device emits an opposite_face slot). It lives HERE, with the
// model that reads it, but editor.js populates it as each rack hydrates and
// reads it in three more places -- an ES module binding is live and this is an
// object, so both sides share the one map exactly as the closure did.
const fullDepthDeviceIds = {};

// ---- Domain classes (spec §2) -------------------------------------------

// One planned placement of a device on one face of one rack (spec §2.1).
// `rows` is the GridStack row span (0.5U resolution, matches `gs-h`);
// `uHeight` is the same span expressed in whole/half U for readability.
function RDDevice(opts) {
    this.deviceId = (opts.deviceId != null) ? opts.deviceId : null;
    this.placementId = (opts.placementId != null) ? opts.placementId : null;
    this.label = opts.label || "";
    this.rows = (opts.rows != null) ? opts.rows : null;
    this.uHeight = (opts.uHeight != null) ? opts.uHeight
        : ((this.rows != null) ? this.rows / 2 : null);
    this.isFullDepth = !!opts.isFullDepth;
    this.rackId = opts.rackId;
    this.face = opts.face || "";
    this.y = (opts.y != null) ? opts.y : null;
    this.state = opts.state || "unknown";
    this.el = opts.el || null;
    // This rack's `data-widget-index` for the tile (Phase 3, spec §7 goal 5):
    // the SAME identity `placeOrMoveShadow` stamps onto an owned shadow/ghost
    // mirror hatch (`data-rd-owner-widx`/`data-rd-owner-rack`), so the model
    // can associate a hatch to its owner by reference instead of by
    // label+position heuristics.
    this.widgetIndex = (opts.widgetIndex != null) ? opts.widgetIndex : null;
    // Owned view parts (spec §2.1: "Owns .shadow / .ghost"). Populated by
    // rdBuildModel's association pass; null until then / if none exists.
    this.shadow = null;
    this.ghost = null;
    // Device bays (spec §10.2). A Bay is to a Device what a Unit is to a
    // Face: the slot a placement competes for. Empty for a non-parent type.
    this.bays = [];
    // The Bay this device occupies, when it is a blade. A blade claims no
    // Units and casts no Shadow -- it sits inside its parent's envelope,
    // which already claims those rows -- so y/rows/face stay null/"".
    this.bay = null;
}

// One device bay (spec §10.2): the bay-side twin of Unit (§2.3). Named
// rather than numbered, and single-occupancy, so it has no row range, never
// partially overlaps, and never displaces -- a drop onto an occupied bay is
// rejected outright (§10.3).
function RDBay(parent, name, el) {
    this.parent = parent || null;
    this.name = name || "";
    this.el = el || null;
    this.occupant = null;
    this.state = null;
}

// True when this bay can accept a blade right now: empty, or emptied by the
// plan (a `remove`-flagged occupant has vacated -- spec §10.3, the
// vacating-slot rule of §4.3 minus the displacement stripe).
RDBay.prototype.isFree = function () {
    if (this.occupant === null) { return true; }
    return this.state === "remove";
};

// The claim standing in this bay, mirroring Unit.claims() (§2.3) but for a
// single-occupancy slot: [] or one {device, kind:"bay"}.
RDBay.prototype.claims = function () {
    return this.occupant ? [{ device: this.occupant, kind: "bay" }] : [];
};

// The opposite-face projection of a full-depth device (spec §2.2). Has no
// lifecycle of its own in Phase 1 either -- it is just the derived-opposite
// DOM node this scan matched to `device` by label + mirrored face + same
// y/rows. `label`/`rackId`/`isGhostMirror` are Phase 1 diagnostic extras
// (not in the spec's field list) kept so an orphan hatch can still be
// reported by name and so I2 can tell a genuine device-shadow orphan apart
// from a ghost's opposite-face mirror hatch (see rdBuildModel's temp-ghost
// skip comment: a mid-move temp ghost is never modelled as an RDGhost in
// Phase 1, so its mirror hatch is EXPECTED to come up unmatched -- that is
// normal, in-progress editing, not drift).
function RDShadow(device, el, face, y, rows) {
    this.device = device || null;
    this.el = el || null;
    this.face = face || "";
    this.y = (y != null) ? y : null;
    this.rows = (rows != null) ? rows : null;
    this.label = "";
    this.rackId = null;
    this.isGhostMirror = false;
}

// The origin-slot marker of a device that moved away but is still shown as
// vacating (spec §2.4). `device` is the live device it belongs to when one
// could be matched by placement/device identity; may be null (e.g. the
// device moved off-screen or identity could not be resolved from the DOM).
function RDGhost(device, el, face, y, rows) {
    this.device = device || null;
    this.el = el || null;
    this.face = face || "";
    this.y = (y != null) ? y : null;
    this.rows = (rows != null) ? rows : null;
    this.label = "";
    this.deviceId = null;
    this.placementId = null;
    this.rackId = null;
    // This rack's `data-widget-index` for the ghost tile (see RDDevice's
    // widgetIndex comment above) -- a ghost's mirror hatch is keyed by this
    // SAME index in editor.js's `ghostShadows`, so it is the identity a
    // conflict-free match uses first.
    this.widgetIndex = null;
}

// One face (front/rear) of one rack (spec §2.5): a passive collection of the
// devices/shadows/ghosts currently rendered on it, plus the occupancy query.
function RDFace(rack, face) {
    this.rack = rack;
    this.face = face;
    this.devices = [];
    this.shadows = [];
    this.ghosts = [];
    this.units = new RDUnitMap(this);
}

// claims(y, rows) -> [{device, kind}] for every body/shadow/ghost claim
// overlapping the given row range on this face (spec §2.3 Unit.claims()). A
// computed index rather than one object per 0.5U row -- the INTERFACE is
// what a caller (a future canPlaceAt) needs, not the storage shape.
RDFace.prototype.claims = function (y, rows) {
    var out = [];
    if (y == null || rows == null) { return out; }
    var yEnd = y + rows;
    this.devices.forEach(function (d) {
        if (d.y == null || d.rows == null) { return; }
        if (y < d.y + d.rows && d.y < yEnd) { out.push({ device: d, kind: "body" }); }
    });
    this.shadows.forEach(function (s) {
        if (s.y == null || s.rows == null) { return; }
        if (y < s.y + s.rows && s.y < yEnd) { out.push({ device: s.device, kind: "shadow" }); }
    });
    this.ghosts.forEach(function (g) {
        if (g.y == null || g.rows == null) { return; }
        if (y < g.y + g.rows && g.y < yEnd) { out.push({ device: g.device, kind: "ghost" }); }
    });
    return out;
};

// Thin per-face occupancy handle (spec §2.3 `Unit`). Phase 1 keeps a single
// computed index (RDFace.claims) instead of one object per 0.5U row; this
// wrapper exists so the spec's `Unit`-shaped call (`face.units.claims(...)`)
// is available even though there is no per-row object behind it yet.
function RDUnitMap(face) {
    this.face = face;
}
RDUnitMap.prototype.claims = function (y, rows) {
    return this.face.claims(y, rows);
};

// One rack: its two faces plus the tray (spec §2.5). `trayDevices` holds
// non-racked tiles for completeness; they carry no face and never
// participate in claims()/invariant checks (a tray slot has no opposite
// face to shadow and no row range to overlap).
function RDRack(rackId, uHeight, descUnits, blockEl) {
    this.rackId = rackId;
    this.uHeight = uHeight;
    this.descUnits = descUnits;
    this.blockEl = blockEl;
    this.faces = {
        front: new RDFace(this, "front"),
        rear: new RDFace(this, "rear"),
    };
    this.trayDevices = [];
    // Scratch lists consumed by rdBuildModel's association pass; not part of
    // the public shape once build() returns.
    this.pendingShadows = [];
    this.pendingGhosts = [];
}

// ---- Builder --------------------------------------------------------------

// Extract the "existing"/"add"/"move_in"/"move_out_ghost"/"remove" token off
// a tile's `nbx-rd-state-*` class (the same vocabulary the CSS/legend use).
function rdStateFromClassList(el) {
    // A tile can legitimately carry MORE THAN ONE nbx-rd-state-* class: the
    // base `existing` plus an additive overlay. flagRemove() only toggles
    // `nbx-rd-state-remove` ON, leaving `nbx-rd-state-existing` in place, so
    // a removed existing device is `[existing, remove]`. A naive
    // first-match regex returned `existing` -> the read-model saw the device
    // as a LIVE body, so canPlaceAt kept BLOCKING its own being-vacated slot
    // and any tile dropped there snapped back (user bug 2026-07-15: could not
    // move a device onto the space freed by a removal -- worst for full-depth
    // gear, whose opposite face is validated too). Resolve by PRECEDENCE: an
    // overlay state wins over the base `existing`. Order: remove >
    // move_out_ghost > move_in > add > existing.
    var cl = el.classList;
    if (cl.contains("nbx-rd-state-remove")) { return "remove"; }
    if (cl.contains("nbx-rd-state-move_out_ghost")) { return "move_out_ghost"; }
    if (cl.contains("nbx-rd-state-move_in")) { return "move_in"; }
    if (cl.contains("nbx-rd-state-add")) { return "add"; }
    if (cl.contains("nbx-rd-state-existing")) { return "existing"; }
    var m = /(?:^|\s)nbx-rd-state-([a-z_]+)(?:\s|$)/.exec(el.className || "");
    return m ? m[1] : "unknown";
}

// Best-effort label for a tile: the `.nbx-rd-label` span's text, falling
// back to the content's `data-name`. The span is the STABLE identity
// string -- it is written once (server render, or onPaletteDrop for an
// add) and never touched again. `data-name` is NOT stable: an add's
// preview-name auto-fill and the §4a move-rename dialog both rewrite it
// to the user-facing PROPOSED name, while a derived opposite-face hatch's
// own label (addOppositeHatch's `label` argument) is always the device's
// original static `widget.label` -- i.e. the same string the span holds.
// Preferring `data-name` here would make a full-depth add's own shadow
// unmatchable the moment its preview name lands (confirmed live: the
// model briefly reports an orphan shadow + a non-full-depth device until
// the async response caught the label; span-based matching does not).
function rdLabelFor(el) {
    var span = el.querySelector(".nbx-rd-label");
    if (span && span.textContent) { return span.textContent; }
    var content = el.querySelector(".grid-stack-item-content");
    return (content && content.getAttribute("data-name")) || "";
}

// gs-y / gs-h for a tile, preferring the live GridStack node (authoritative
// once the engine has attached) and falling back to the rendered attributes
// (covers a detached persistent ghost/hatch, whose node was intentionally
// removed from the engine -- see the "detach from the engine" comments in
// initRack -- but whose gs-y/gs-h attributes are never rewritten).
function rdRowsFor(el) {
    var node = el.gridstackNode;
    var y = (node && node.y != null) ? node.y : parseInt(el.getAttribute("gs-y"), 10);
    var h = (node && node.h != null) ? node.h : parseInt(el.getAttribute("gs-h"), 10);
    return {
        y: isNaN(y) ? null : y,
        rows: isNaN(h) ? null : h,
    };
}

// Scan every currently-rendered rack block and build the read-model (spec
// §2). Skips `data-rd-temp-ghost` nodes: a temp ghost is the live,
// engine-detached marker ensureTempGhost() draws for an UNCOMMITTED move and
// carries no `data-widget-index`, so its device identity cannot be resolved
// from the DOM alone -- it will be superseded by an owned Ghost in a later
// phase. Returns { racks: {rackId: RDRack}, devices: [RDDevice],
// orphanShadows: [RDShadow], orphanGhosts: [RDGhost] }.
function rdBuildModel() {
    var model = { racks: {}, devices: [], orphanShadows: [], orphanGhosts: [] };
    var widgetsByRack = {};

    document.querySelectorAll(".nbx-rd-rack-block").forEach(function (block) {
        var rackId = parseInt(block.getAttribute("data-rack-id"), 10);
        var uHeight = parseInt(block.getAttribute("data-u-height"), 10);
        var descUnits = block.getAttribute("data-desc-units") === "true";
        var dataEl = document.getElementById("rd-editor-data-" + rackId);
        var widgets = [];
        try {
            widgets = JSON.parse((dataEl && dataEl.textContent) || "[]");
        } catch (e) { widgets = []; }
        widgetsByRack[rackId] = widgets;
        model.racks[rackId] = new RDRack(rackId, uHeight, descUnits, block);
    });

    // Pass 1: classify every non-temp-ghost tile into a device body, a
    // derived opposite-face hatch (shadow, possibly a ghost's mirror), or a
    // persistent move-out ghost.
    document.querySelectorAll(".nbx-rd-rack-block").forEach(function (block) {
        var rackId = parseInt(block.getAttribute("data-rack-id"), 10);
        var rack = model.racks[rackId];
        var widgets = widgetsByRack[rackId] || [];

        block.querySelectorAll(".grid-stack-item").forEach(function (el) {
            if (el.getAttribute("data-rd-temp-ghost")) { return; }

            var gridHost = el.closest(".grid-stack");
            var face = gridHost ? (gridHost.getAttribute("data-face") || "") : "";
            var rc = rdRowsFor(el);
            var label = rdLabelFor(el);
            var idx = parseInt(el.getAttribute("data-widget-index"), 10);
            var w = isNaN(idx) ? null : widgets[idx];

            var isDerivedShadow = !!el.getAttribute("data-rd-derived-opp");
            var isGhostClass = el.classList.contains("nbx-rd-state-move_out_ghost");

            if (isDerivedShadow) {
                // A derived hatch is ALWAYS a shadow (spec §2.2), even when it
                // also carries the move_out_ghost class -- that combination is
                // a ghost's opposite-face mirror (see syncGhostShadow in
                // editor.js), still a Shadow, just following its owner's
                // move_out_ghost render style per spec §3. Phase 3 (spec §7
                // goal 5): every owned hatch also carries the OWNER's exact
                // identity (its rack + this-rack widget-index) stamped by
                // placeOrMoveShadow, so pass 3 below can match by reference
                // first and only fall back to the y/rows/label heuristic for
                // any hatch that (for whatever reason) doesn't carry it.
                var ownerWidx = parseInt(el.getAttribute("data-rd-owner-widx"), 10);
                var ownerRack = parseInt(el.getAttribute("data-rd-owner-rack"), 10);
                rack.pendingShadows.push({
                    el: el, face: face, y: rc.y, rows: rc.rows, label: label,
                    isGhostMirror: isGhostClass,
                    ownerWidx: isNaN(ownerWidx) ? null : ownerWidx,
                    ownerRack: isNaN(ownerRack) ? null : ownerRack,
                });
                return;
            }
            if (isGhostClass) {
                // A persistent (server-reloaded) move-out ghost at its origin.
                rack.pendingGhosts.push({
                    el: el, face: face, y: rc.y, rows: rc.rows, label: label,
                    deviceId: w ? w.device_id : null,
                    placementId: w ? w.placement_id : null,
                    widgetIndex: isNaN(idx) ? null : idx,
                });
                return;
            }

            var device = new RDDevice({
                deviceId: w ? w.device_id : null,
                placementId: w ? w.placement_id : null,
                label: label,
                rows: rc.rows,
                uHeight: (w && w.u_height != null) ? w.u_height : null,
                isFullDepth: !!(w && w.device_id != null && fullDepthDeviceIds[w.device_id]),
                rackId: rackId,
                face: face,
                y: rc.y,
                state: rdStateFromClassList(el),
                el: el,
                widgetIndex: isNaN(idx) ? null : idx,
            });
            // Bays (spec §10.2): a parent tile carries a strip of bay cells.
            // Each becomes an RDBay owned by this device; an occupied one
            // also becomes a blade RDDevice whose container is that bay, so
            // the read-model holds blades as first-class devices rather than
            // as markup inside someone else's tile.
            el.querySelectorAll(".nbx-rd-bay").forEach(function (bayEl) {
                var bay = new RDBay(device, bayEl.getAttribute("data-bay-name") || "", bayEl);
                bay.state = bayEl.getAttribute("data-bay-state") || null;
                var occupantLabel = bayEl.getAttribute("data-bay-device");
                if (occupantLabel) {
                    var blade = new RDDevice({
                        label: occupantLabel,
                        rackId: rackId,
                        face: "",
                        state: bay.state || "existing",
                        el: bayEl,
                    });
                    blade.bay = bay;
                    bay.occupant = blade;
                    model.devices.push(blade);
                }
                device.bays.push(bay);
            });

            model.devices.push(device);
            if (face === "front" || face === "rear") {
                rack.faces[face].devices.push(device);
            } else {
                rack.trayDevices.push(device);
            }
        });
    });

    // Pass 2: associate every pending ghost to a live device elsewhere (by
    // placement identity, falling back to device identity), then attach it
    // to its own face's ghost list.
    Object.keys(model.racks).forEach(function (rid) {
        var rack = model.racks[rid];
        rack.pendingGhosts.forEach(function (pg) {
            var owner = null;
            model.devices.forEach(function (d) {
                if (owner) { return; }
                if (pg.placementId != null && d.placementId === pg.placementId) { owner = d; return; }
                if (pg.placementId == null && pg.deviceId != null && d.deviceId === pg.deviceId) { owner = d; }
            });
            var ghost = new RDGhost(owner, pg.el, pg.face, pg.y, pg.rows);
            ghost.label = pg.label;
            ghost.deviceId = pg.deviceId;
            ghost.placementId = pg.placementId;
            ghost.rackId = parseInt(rid, 10);
            ghost.widgetIndex = pg.widgetIndex;
            if (owner) { owner.ghost = ghost; } else { model.orphanGhosts.push(ghost); }
            if (ghost.face === "front" || ghost.face === "rear") {
                rack.faces[ghost.face].ghosts.push(ghost);
            }
        });
    });

    // Pass 3: associate every pending shadow. A regular full-depth shadow
    // matches a LIVE device on the mirrored face at the same y/rows with the
    // same label; a ghost's mirror (isGhostMirror) instead matches a Ghost
    // built in pass 2 the same way -- but pass 1 deliberately never builds
    // an RDGhost for a TEMP ghost (no `data-widget-index` to resolve its
    // identity from), so a full-depth device's mid-move ghost mirror is
    // EXPECTED to come up unmatched on every ordinary in-progress edit, not
    // just on drift. rdCheckInvariants' I2 therefore only treats a regular
    // (non-ghost-mirror) unmatched shadow as a violation; ghost-mirror
    // orphans still land in model.orphanShadows (flagged isGhostMirror) as
    // a diagnostic only.
    Object.keys(model.racks).forEach(function (rid) {
        var rack = model.racks[rid];
        rack.pendingShadows.forEach(function (ps) {
            var mirrorFace = (ps.face === "front") ? "rear" : ((ps.face === "rear") ? "front" : "");
            var shadow = new RDShadow(null, ps.el, ps.face, ps.y, ps.rows);
            shadow.label = ps.label;
            shadow.rackId = parseInt(rid, 10);

            var pool = ps.isGhostMirror ? rack.faces[mirrorFace].ghosts : rack.faces[mirrorFace].devices;
            var owner = null;
            // Identity-first (spec §7 goal 5): an owned hatch stamps exactly
            // which rack + widget-index it belongs to, so match by reference
            // before ever falling back to a position/label heuristic -- this
            // is what makes a "wrong-name shadow" (bug #11/4b) structurally
            // impossible for any hatch created by placeOrMoveShadow.
            if (ps.ownerWidx != null && ps.ownerRack != null) {
                var identityPool = ps.isGhostMirror
                    ? (model.racks[ps.ownerRack] ? model.racks[ps.ownerRack].faces[mirrorFace].ghosts : [])
                    : (model.racks[ps.ownerRack] ? model.racks[ps.ownerRack].faces[mirrorFace].devices : []);
                (identityPool || []).forEach(function (candidate) {
                    if (owner) { return; }
                    if (candidate.widgetIndex === ps.ownerWidx) { owner = candidate; }
                });
            }
            if (!owner) {
                (pool || []).forEach(function (candidate) {
                    if (owner) { return; }
                    if (candidate.y !== ps.y || candidate.rows !== ps.rows) { return; }
                    if ((candidate.label || "") !== (ps.label || "")) { return; }
                    owner = candidate;
                });
            }

            if (ps.isGhostMirror) {
                // Matched to a Ghost, not a Device: record the pairing on the
                // shadow for diagnostics, but this is NOT the spec §2.1
                // device-owns-shadow relationship, so it is deliberately left
                // out of model.devices' shadow ownership. Flag it so
                // rdCheckInvariants can tell it apart from a genuine orphan.
                shadow.isGhostMirror = true;
                shadow.device = owner ? owner.device : null;
                if (!owner) { model.orphanShadows.push(shadow); }
                return;
            }
            if (owner) {
                owner.isFullDepth = true;
                owner.shadow = shadow;
                shadow.device = owner;
                // The shadow lives on ITS OWN face (ps.face) -- the opposite
                // face from its owning device (mirrorFace) -- so it must be
                // filed there, never into the owner's own face's shadow list
                // (that bug made a device's own shadow "overlap" its body).
                if (ps.face === "front" || ps.face === "rear") {
                    rack.faces[ps.face].shadows.push(shadow);
                }
            } else {
                model.orphanShadows.push(shadow);
            }
        });
    });

    return model;
}

// ---- Invariant checks (spec §6: I1, I2; spec §9.2: I4) ---------------------

// Live (non-vacating) lifecycle states: only these participate in the I1
// overlap check. Ghosts and remove-flagged devices never block (spec §4.2).
var RD_LIVE_STATES = { existing: true, add: true, move_in: true };

function rdRowRangeLabel(y, rows) {
    if (y == null) { return "?"; }
    var end = y + (rows || 0) - 1;
    return y + "-" + end;
}

// Returns an array of human-readable violation strings (empty = clean).
//   I1 -- no two live (body/shadow) claims overlap on the same face rows.
//   I2 -- every full-depth device has exactly one shadow, on the opposite
//         face, at its own y/rows; and no orphan (device) shadow exists.
function rdCheckInvariants(model) {
    var out = [];

    Object.keys(model.racks).forEach(function (rid) {
        var rack = model.racks[rid];
        ["front", "rear"].forEach(function (faceName) {
            var face = rack.faces[faceName];
            var claims = [];
            face.devices.forEach(function (d) {
                if (!RD_LIVE_STATES[d.state] || d.y == null || d.rows == null) { return; }
                claims.push({ label: d.label, kind: "body", y: d.y, rows: d.rows });
            });
            face.shadows.forEach(function (s) {
                var owner = s.device;
                if (!owner || !RD_LIVE_STATES[owner.state] || s.y == null || s.rows == null) { return; }
                claims.push({ label: owner.label, kind: "shadow", y: s.y, rows: s.rows });
            });
            for (var i = 0; i < claims.length; i++) {
                for (var j = i + 1; j < claims.length; j++) {
                    var a = claims[i], b = claims[j];
                    if (a.y < b.y + b.rows && b.y < a.y + a.rows) {
                        var lo = Math.min(a.y, b.y);
                        var hi = Math.max(a.y + a.rows, b.y + b.rows) - 1;
                        out.push(
                            "I1 rack " + rid + " " + faceName + " rows " + lo + "-" + hi + ": "
                            + a.label + "(" + a.kind + ") overlaps " + b.label + "(" + b.kind + ")"
                        );
                    }
                }
            }
        });
    });

    // I5 (spec §10.2): a Bay holds at most one occupant, and a Device occupies
    // at most one Bay. I1/I2 above cannot catch this -- a blade claims no rows
    // and casts no shadow -- so bay containment is checked on its own terms.
    var bayOccupancy = {};
    model.devices.forEach(function (d) {
        d.bays.forEach(function (bay) {
            var key = d.rackId + "/" + (d.widgetIndex != null ? d.widgetIndex : d.label) + "/" + bay.name;
            if (bayOccupancy[key]) {
                out.push(
                    "I5 rack " + d.rackId + ": " + d.label
                    + " has more than one bay named " + bay.name
                );
            }
            bayOccupancy[key] = true;
        });
    });
    var seatedIn = {};
    model.devices.forEach(function (d) {
        if (!d.bay) { return; }
        var who = d.rackId + "/" + d.label;
        if (seatedIn[who]) {
            out.push(
                "I5 rack " + d.rackId + ": " + d.label + " occupies more than one bay ("
                + seatedIn[who] + " and " + d.bay.name + ")"
            );
            return;
        }
        seatedIn[who] = d.bay.name;
    });

    model.devices.forEach(function (d) {
        if (!d.isFullDepth) { return; }
        var expectedFace = (d.face === "front") ? "rear" : ((d.face === "rear") ? "front" : null);
        if (!d.shadow) {
            out.push(
                "I2 rack " + d.rackId + " " + d.face + " rows " + rdRowRangeLabel(d.y, d.rows)
                + ": " + d.label + " is full-depth but has no shadow"
            );
            return;
        }
        if (d.shadow.face !== expectedFace || d.shadow.y !== d.y || d.shadow.rows !== d.rows) {
            out.push(
                "I2 rack " + d.rackId + ": shadow of " + d.label + " is at "
                + d.shadow.face + " rows " + rdRowRangeLabel(d.shadow.y, d.shadow.rows)
                + ", expected " + expectedFace + " rows " + rdRowRangeLabel(d.y, d.rows)
            );
        }
    });

    model.orphanShadows.forEach(function (s) {
        // A ghost's mirror hatch with no matching Ghost is EXPECTED here in
        // Phase 1 (temp ghosts are never modelled -- see rdBuildModel's
        // pass-1 skip), not drift: every full-depth device with an
        // in-progress, unsaved move produces one. Diagnostic only.
        if (s.isGhostMirror) { return; }
        out.push(
            "I2 rack " + (s.rackId != null ? s.rackId : "?") + " " + s.face + " rows "
            + rdRowRangeLabel(s.y, s.rows) + ": orphan shadow labelled '" + s.label
            + "' has no owning device"
        );
    });

    // I4 (spec §9.2): a device appears AT MOST ONCE, LIVE, across the whole
    // world -- its body in units (front/rear, any rack) XOR its body in a
    // tray (any rack) -- never both. A moved device's origin ghost is NOT a
    // live claim (RD_LIVE_STATES excludes move_out_ghost/remove), so the
    // normal body+origin-ghost pair for a move/dismount/mount/reassociation
    // is expected structure, not a violation; only a SECOND live body for
    // the same deviceId (e.g. stuck in both units and tray at once) trips
    // this. Devices with no resolvable identity (deviceId == null -- a
    // brand-new, not-yet-saved catalog add) are not checked here; I1
    // already covers their occupancy.
    var rdLiveByDeviceId = {};
    model.devices.forEach(function (d) {
        // Tray devices are ALSO present in model.devices (rdBuildModel's
        // pass 1 pushes every tile there regardless of face), so counting
        // them here too would double-count every tray device against its
        // own rack.trayDevices entry below -- only a real units body
        // (face front/rear) counts as a "units" location.
        if (d.face !== "front" && d.face !== "rear") { return; }
        if (d.deviceId == null || !RD_LIVE_STATES[d.state]) { return; }
        (rdLiveByDeviceId[d.deviceId] = rdLiveByDeviceId[d.deviceId] || []).push(
            { rackId: d.rackId, where: "units/" + d.face, label: d.label }
        );
    });
    Object.keys(model.racks).forEach(function (rid) {
        model.racks[rid].trayDevices.forEach(function (d) {
            if (d.deviceId == null || !RD_LIVE_STATES[d.state]) { return; }
            (rdLiveByDeviceId[d.deviceId] = rdLiveByDeviceId[d.deviceId] || []).push(
                { rackId: d.rackId, where: "tray", label: d.label }
            );
        });
    });
    Object.keys(rdLiveByDeviceId).forEach(function (deviceId) {
        var entries = rdLiveByDeviceId[deviceId];
        if (entries.length <= 1) { return; }
        var where = entries.map(function (e) {
            return "rack " + e.rackId + " " + e.where;
        }).join(", ");
        out.push(
            "I4 device " + deviceId + " (" + entries[0].label + ") has "
            + entries.length + " live entities: " + where
        );
    });

    return out;
}

// ---- Phase 2: canPlaceAt (spec §4.1, §4.2) ---------------------------------
// The SINGLE authority for "is this target legal", built on top of the
// Phase 1 read-model's claims() index. Pure query -- no mutation, no
// dialog, no revert; callers (tileOverlapsOther below) decide what to do
// with the verdict.
//
// A claim blocks D unless it is D's OWN claim (never self-blocking, spec
// §4.2 last row), a ghost (vacating-slot marker, spec §4.2 row 3 -- the
// displacement flow it would trigger is Phase 4, not built yet, so for
// now it simply allows), or the body/shadow of a device that is NOT in a
// live lifecycle state (spec §4.2 row 4: a `remove`-flagged device's
// claims do not block either, for the same Phase-4-deferred reason).
function rdIsBlockingClaim(claim, selfDevice) {
    if (!claim || !claim.device) { return false; }
    if (claim.device === selfDevice) { return false; }
    if (claim.kind === "ghost") { return false; }
    // kind is "body" or "shadow": blocks only while its owner is live.
    return !!RD_LIVE_STATES[claim.device.state];
}

// canPlaceAt(deviceEl, rackId, face, y, rows, isFullDepth) -> {ok, reason,
// blockers}. Checks the target [y, y+rows) rows on `face`, plus (when
// isFullDepth) the mirrored rows on the OPPOSITE face -- a full-depth
// device occupies both (spec §2.1/§4.2). `deviceEl` identifies the moving
// device so its own body/shadow/ghost never block itself, even though the
// read-model is rebuilt from the DOM AFTER GridStack has already written
// the candidate position there (today's dragstop-time check-then-revert
// sequencing; see tileOverlapsOther below).
function rdCanPlaceAt(deviceEl, rackId, face, y, rows, isFullDepth) {
    var model = rdBuildModel();
    var rack = model.racks[rackId];
    if (!rack || (face !== "front" && face !== "rear") || y == null || rows == null) {
        return { ok: true, reason: "", blockers: [] };
    }
    var selfDevice = null;
    model.devices.forEach(function (d) {
        if (d.el === deviceEl) { selfDevice = d; }
    });

    var blockers = [];
    function scan(faceName) {
        var f = rack.faces[faceName];
        if (!f) { return; }
        f.units.claims(y, rows).forEach(function (c) {
            if (rdIsBlockingClaim(c, selfDevice)) { blockers.push(c); }
        });
    }
    scan(face);
    if (isFullDepth) {
        scan(face === "front" ? "rear" : "front");
    }

    if (!blockers.length) {
        return { ok: true, reason: "", blockers: [] };
    }
    var names = blockers.map(function (b) { return b.device.label; }).join(", ");
    return { ok: false, reason: "occupied by " + names, blockers: blockers };
}

// ---- Debug hook (OFF by default) -------------------------------------------
// window.__rdModel.build()/.check() are always available for manual poking
// from the console/Playwright, but rdBuildModel() is never called
// automatically unless window.__rdDebugInvariants is set (see the guarded
// call in refreshGhosts above). canPlaceAt IS live behind the scenes
// already (tileOverlapsOther calls it directly on every drop); it is
// exposed here too, read-only, for the same manual-poking convenience.
window.__rdModel = {
    build: rdBuildModel,
    check: function () { return rdCheckInvariants(rdBuildModel()); },
    canPlaceAt: rdCanPlaceAt,
};

export {
    fullDepthDeviceIds,
    rdBuildModel,
    rdCheckInvariants,
    rdCanPlaceAt,
    rdLabelFor,
};
