/*
 * Frame construction shared by every rack controller -- extracted verbatim
 * from editor.js: the rack-height sync for the fixed left rail, the common
 * GridStack options every grid is built with, and makeFrame(), which is what
 * isolates the rack/chassis enclosure differences behind one shape.
 */

import { rdIsChildEl } from "rd/cursor.js";

// The editor root, re-derived here: editor.js holds it in its closure and
// returns early when it is absent, and only makeFrame() below dereferences it.
const root = document.getElementById("rd-editor");

// ---- Shared rack-height sync -------------------------------------------
// The fixed left rail (catalog) + quick-access columns track the height of a
// VISIBLE rack elevation so they read as rack-tall. With several racks we
// simply measure the first visible elevation found. Recomputed on resize and
// when a face is toggled.
var layoutEl = document.querySelector(".nbx-rd-editor-layout");
function syncRackHeight() {
    if (!layoutEl) { return; }
    var grids = root.querySelectorAll(".nbx-rd-rack");
    var h = 0;
    for (var i = 0; i < grids.length; i++) {
        if (grids[i].offsetParent !== null && grids[i].offsetHeight > h) {
            h = grids[i].offsetHeight;
        }
    }
    if (h > 80) {
        layoutEl.style.setProperty("--nbx-rd-rack-height", h + "px");
    }
}

// ---- Shared GridStack options ------------------------------------------
function commonOptions(extra) {
    var opts = {
        cellHeight: 11,
        margin: 0,
        marginBottom: 1,
        column: 1,
        float: true,
        animate: true,
        disableResize: true,   // slice 2a: move only, no resize
        acceptWidgets: true,   // overridden per rack to scope cross-grid drops
        removable: false,
        // Don't start a drag when the pointer goes down on a tile's remove
        // (×) button, a palette row's favorite (star) button, or an add
        // tile's editable name input — otherwise GridStack captures the
        // pointer and the click/focus never fires.
        draggable: { cancel: ".nbx-rd-remove-btn, .nbx-rd-fav-btn, .nbx-rd-name-input, .nbx-rd-name-edit-btn" },
    };
    if (extra) {
        Object.keys(extra).forEach(function (k) { opts[k] = extra[k]; });
    }
    return opts;
}

// Cursor-governed placement (spec §4.1) lives in editor/cursor.js.

// ========================================================================
// Cross-rack move plumbing (module level, shared by every rack controller).
// ------------------------------------------------------------------------
// controllersByRackId: each initRack registers itself here so the cross-rack
//   flow can reach the OTHER rack's grids (to drop an origin ghost on the
//   source, or to snap a tile back into the source on ×/cancel).
// tileInFlight: the origin descriptor captured on dragstart of a real-device
//   tile. It survives the synchronous removed(source)+dropped(destination)
//   events of a GridStack cross-grid drag; the next dragstart overwrites it.
// ========================================================================

// ========================================================================
// Frame / Container (spec §2, §10.3) -- the ONE place that knows how a
// physical enclosure differs from another.
// ------------------------------------------------------------------------
// A FRAME is one enclosure. It owns one or more CONTAINERS (addressable
// grids of slots) and, for a rack, a tray:
//
//   rack     containers [front, rear]   tray yes   pairing yes (full-depth)
//   chassis  containers [bays]          tray no    pairing no
//
// Everything above this object -- add, move, remove, cancel, ghosts,
// blocking, homecoming -- is written against SLOTS and ADDRESSES and never
// against units or bays. That is the whole point: a rack fix is a chassis
// fix, because there is only one implementation.
//
// Before this existed, a chassis was a rack whose payload got TRANSLATED
// afterwards (chassisColumnPayload), and the translation re-derived a bay
// from u_position -- so an item with no position, i.e. a cancel, was
// silently dropped and the user's edit never reached the server
// (user 2026-08-26). An address is now produced ONCE, by whoever owns the
// slot, and can never go missing on the way out.
// ========================================================================
function makeFrame(block) {
    var isChassis = !!block.getAttribute("data-chassis-key");
    var uHeight = parseInt(block.getAttribute("data-u-height"), 10);
    // Ascending slot numbering (bay 1 at the top). A rack numbers its units
    // the other way up, from the floor.
    var ascending = block.getAttribute("data-desc-units") === "true";
    // The pk the SERVER must see. A chassis column's own id is synthetic
    // (spec §10.3); the real rack is on data-real-rack-id.
    var realRackId = parseInt(block.getAttribute("data-real-rack-id"), 10);
    var chassisId = parseInt(block.getAttribute("data-chassis-id"), 10);
    var chassisPlacement = parseInt(block.getAttribute("data-chassis-placement"), 10);
    var bayNames = [];
    var bayIds = [];
    try { bayNames = JSON.parse(block.getAttribute("data-bay-names") || "[]"); }
    catch (e) { bayNames = []; }
    try { bayIds = JSON.parse(block.getAttribute("data-bay-ids") || "[]"); }
    catch (e) { bayIds = []; }

    return {
        isChassis: isChassis,
        // A frame with no pairing rule has no opposite face, so no full-depth
        // shadow and no hatch can exist in it -- absent, not suppressed.
        hasPairing: !isChassis,
        hasTray: !isChassis,
        serverRackId: !isNaN(realRackId) ? realRackId : parseInt(
            block.getAttribute("data-rack-id"), 10),

        // ---- geometry <-> slot (inverse of templatetags.slot_gs_y) ------
        slotFromGeometry: function (gsY, gsH) {
            var y = gsY / 2;
            var h = gsH / 2;
            if (ascending) { return y + 1; }
            if (h > 1) { return uHeight - y - h + 1; }
            return uHeight - y;
        },
        slotToGeometry: function (slot, gsH) {
            if (ascending) { return slot * 2 - 2; }
            if (gsH > 2) { return uHeight * 2 - slot * 2 - gsH + 2; }
            return uHeight * 2 - slot * 2;
        },

        // ---- the drop gate (spec §10.3) ---------------------------------
        // Container/type agreement, enforced BEFORE the gesture completes: a
        // child type may only land in a chassis, a rack-mountable only in a
        // rack. Core forbids a child device a position and a face, and
        // forbids a non-child a device bay, so each is illegal in the other.
        accepts: function (el) {
            // A tile that ALREADY LIVES in a chassis column is a bay occupant,
            // whatever markers it carries: containment is the fact, and
            // data-subdevice-role is only the hint the palette stamps on rows
            // that live nowhere yet. Judging a placed tile by that marker made
            // every real blade unmovable -- the server renders no such
            // attribute on a tile, so the destination column refused its own
            // kind and the drag silently did nothing (user 2026-08-26).
            var placed = el.closest && el.closest(".nbx-rd-chassis-block");
            var isChild = placed ? true : rdIsChildEl(el);
            return isChild === isChassis;
        },

        // ---- the save address -------------------------------------------
        // Which payload bucket this container's items belong to, and how one
        // slot in it is addressed. These two are the ONLY things the save
        // path needs to know about the difference between a rack and a
        // chassis.
        bucketFor: function (faceKey) {
            return isChassis ? "bays" : faceKey;
        },
        addressForSlot: function (slot, faceKey) {
            if (!isChassis) {
                // A rack slot is a unit on a face.
                return { u_position: slot, face: faceKey };
            }
            // A chassis slot is a bay. It carries NO face: a chassis has one
            // container, and the server stores "" for a bay placement anyway.
            var address = { target_bay_name: bayNames[slot - 1] || "" };
            if (!isNaN(chassisId)) {
                // Real chassis: address the bay by its dcim pk.
                var bayId = bayIds[slot - 1];
                if (bayId) { address.target_bay_id = bayId; }
            } else if (!isNaN(chassisPlacement)) {
                // Planned chassis: it is already a saved placement (the layer
                // only renders chassis the design has saved), so point at it.
                address.parent_placement_id = chassisPlacement;
            }
            return address;
        },
    };
}

export { syncRackHeight, commonOptions, makeFrame };
