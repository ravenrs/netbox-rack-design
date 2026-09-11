/*
 * Cursor-governed placement (spec §4.1) -- extracted verbatim from editor.js,
 * where this block sat as one contiguous section. Tracks the pointer at the
 * document level for the duration of a drag gesture and enforces Petr's
 * cursor-wins ruling at drop time, independent of GridStack's own placeholder
 * math.
 */

// Legality is the read-model's call, never GridStack's collision cascade:
// rdUpdateCursorGesture asks rdCanPlaceAt whether the cursor's rows are legal
// before choosing the green allow or the red deny indicator.
import { rdCanPlaceAt } from "rd/model.js";

// The editor root. Only rdFaceHostAt() below dereferences it, and that runs
// solely inside an armed gesture, which cannot exist on a page without the
// editor -- so no guard is needed here, unlike power.js's API_BASE.
const root = document.getElementById("rd-editor");

// ---- Cursor-governed placement (spec §4.1 hard rule, ruling 2026-07-08) ----
// GridStack's own drag math parks its placeholder on the LAST VALID slot
// while the pointer hovers an illegal one, and a release then commits at
// that fallback slot -- confirmed live (design 6, dra4-sl-isp29 F11 ->
// F08 rear: released over a full-depth device's rear shadows at ~U10,
// committed at U7). Petr's ruling: there is no "suggested placement" --
// the commit position is always the CURSOR's rows; releasing over
// illegal rows is a full snap-back home. Rather than patching the vendor
// placeholder math, WE track the pointer at the document level for the
// duration of a gesture (armed by onDragStart, disarmed by thawAllTiles)
// and enforce the ruling at drop time (enforceCursorPlacement, called
// from maybePromptMove/maybeRevertAddMove before validation): when the
// pointer's rows at release disagree with where the engine landed the
// tile, the pointer wins -- reposition when its rows are legal, snap
// back home when they are not. Mid-drag the same tracking renders a red
// deny indicator at the cursor rows (and hides the vendor placeholder)
// whenever the hovered rows are illegal. A gesture with no pointer data
// (deterministic test shims fire the grid handlers directly, without a
// mouse) leaves the tracker inert, preserving engine-landed behaviour.
var rdLastPointer = null;
var rdCursorGesture = null;  // {el, gsH, isFullDepth, grabRows, lastHost, lastRow}
var rdDenyEl = null;

// The face grid host (front/rear only, never the tray) under (x, y).
function rdFaceHostAt(x, y) {
    var hosts = root.querySelectorAll(
        '.nbx-rd-rack-block .grid-stack[data-face="front"], '
        + '.nbx-rd-rack-block .grid-stack[data-face="rear"]');
    for (var i = 0; i < hosts.length; i++) {
        var r = hosts[i].getBoundingClientRect();
        if (x >= r.left && x < r.right && y >= r.top && y < r.bottom) {
            return hosts[i];
        }
    }
    return null;
}

// True when this element represents a CHILD device type -- a blade. Read from
// the palette row's stamp, or from a placed tile's own marker.
function rdIsChildEl(el) {
    if (!el) { return false; }
    return el.getAttribute("data-subdevice-role") === "child"
        || el.classList.contains("nbx-rd-palette-child")
        || el.classList.contains("nbx-rd-child-device");
}

function rdMaxRow(host) {
    var fromAttr = parseInt(host.getAttribute("gs-max-row"), 10);
    if (!isNaN(fromAttr) && fromAttr > 0) { return fromAttr; }
    var g = host.gridstack;
    return (g && g.opts && g.opts.maxRow) || 0;
}

// The 0.5U row under viewport-Y `y` within `host`.
function rdRowAt(host, y) {
    var maxRow = rdMaxRow(host);
    if (!maxRow) { return null; }
    var r = host.getBoundingClientRect();
    if (r.height <= 0) { return null; }
    var row = Math.floor((y - r.top) / (r.height / maxRow));
    return Math.max(0, Math.min(maxRow - 1, row));
}

function rdHideDeny() {
    if (rdDenyEl && rdDenyEl.parentNode) {
        rdDenyEl.parentNode.classList.remove("nbx-rd-deny-active");
        rdDenyEl.parentNode.removeChild(rdDenyEl);
    }
}

function rdShowDeny(host, topRow, gsH) {
    if (!rdDenyEl) {
        rdDenyEl = document.createElement("div");
        rdDenyEl.className = "nbx-rd-cursor-deny";
    }
    if (rdDenyEl.parentNode !== host) {
        rdHideDeny();
        host.appendChild(rdDenyEl);
    }
    var maxRow = rdMaxRow(host);
    if (!maxRow) { return; }
    rdHideAllow();  // deny and allow are mutually exclusive
    var rowPx = host.getBoundingClientRect().height / maxRow;
    rdDenyEl.style.top = (topRow * rowPx) + "px";
    rdDenyEl.style.height = (gsH * rowPx) + "px";
    host.classList.add("nbx-rd-deny-active");
}

// The positive counterpart of the deny box: a translucent landing preview
// marking the EXACT rows the dragged/added tile will occupy on release,
// so the drop target is visible under the (translucent) drag helper
// (user request 2026-07-10). Same geometry as rdShowDeny, opposite intent.
var rdAllowEl = null;
function rdHideAllow() {
    if (rdAllowEl && rdAllowEl.parentNode) {
        rdAllowEl.parentNode.classList.remove("nbx-rd-allow-active");
        rdAllowEl.parentNode.removeChild(rdAllowEl);
    }
}
function rdShowAllow(host, topRow, gsH) {
    if (!rdAllowEl) {
        rdAllowEl = document.createElement("div");
        rdAllowEl.className = "nbx-rd-cursor-allow";
    }
    if (rdAllowEl.parentNode !== host) {
        rdHideAllow();
        host.appendChild(rdAllowEl);
    }
    var maxRow = rdMaxRow(host);
    if (!maxRow) { return; }
    rdHideDeny();  // deny and allow are mutually exclusive
    var rowPx = host.getBoundingClientRect().height / maxRow;
    rdAllowEl.style.top = (topRow * rowPx) + "px";
    rdAllowEl.style.height = (gsH * rowPx) + "px";
    host.classList.add("nbx-rd-allow-active");
}
// Hide BOTH cursor indicators (teardown / off-grid).
function rdClearCursorInds() {
    rdHideDeny();
    rdHideAllow();
}

// The cursor's candidate placement for the active gesture: the host the
// pointer is over and the top row the dragged tile would take there
// (pointer row minus the in-tile grab offset, clamped to the rack).
function rdCursorCandidate() {
    var g = rdCursorGesture;
    if (!g || !g.lastHost || g.lastRow == null) { return null; }
    var maxRow = rdMaxRow(g.lastHost);
    if (!maxRow) { return null; }
    var top = g.lastRow - g.grabRows;
    // A whole-U PALETTE add snaps to the U-grid. A unit spans two 0.5U
    // rows, so a pointer at a unit's visual centre floors to its LOWER
    // row; with the palette gesture's grabRows==0 that raw row became the
    // tile top and the add fell a unit low (live bug 2026-07-10, Petr:
    // "dropped on 23, landed on 22"). An integer-U device (even gsH) has
    // only even valid tops, and floor-to-even is exactly the unit that
    // contains the cursor row -- so pointing anywhere inside a unit lands
    // the device ON it.
    //
    // A MOVE needs the same snap, for a different reason. grabRows was read
    // to be the source tile's own U-alignment, but that only holds while the
    // pointer keeps the same offset WITHIN a unit that it had at grab time:
    // floor() is applied to the grab offset and to the pointer row
    // independently, so drifting half a unit vertically flips the parity and
    // the tile lands on a half-unit boundary. Snapping is only correct for a
    // device that STARTED on a whole unit, though -- a rack with half-unit
    // mounting legitimately holds devices at x.5 (target_position 33.5), and
    // those must keep their odd rows.
    if (g.gsH % 2 === 0) {
        if (g.palette || (g.srcTop != null && g.srcTop % 2 === 0)) {
            top -= top % 2;
        }
    }
    top = Math.max(0, Math.min(top, maxRow - g.gsH));
    var block = g.lastHost.closest(".nbx-rd-rack-block");
    return {
        host: g.lastHost,
        top: top,
        face: g.lastHost.getAttribute("data-face"),
        rackId: block ? parseInt(block.getAttribute("data-rack-id"), 10) : null,
    };
}

// Mid-drag: refresh the tracked cursor rows + the allow/deny indicator.
function rdUpdateCursorGesture() {
    var g = rdCursorGesture;
    if (!g || !rdLastPointer) { return; }

    var host = rdFaceHostAt(rdLastPointer.x, rdLastPointer.y);
    if (!host) {
        g.lastHost = null;
        g.lastRow = null;
        rdClearCursorInds();
        return;
    }
    var row = rdRowAt(host, rdLastPointer.y);
    if (host === g.lastHost && row === g.lastRow) { return; }  // no change
    g.lastHost = host;
    g.lastRow = row;
    var cand = rdCursorCandidate();
    if (!cand) { rdClearCursorInds(); return; }
    var verdict = rdCanPlaceAt(
        g.el, cand.rackId, cand.face, cand.top, g.gsH, g.isFullDepth);
    if (verdict.ok) {
        // Show WHERE it lands: the snapped candidate rows (green preview).
        rdShowAllow(host, cand.top, g.gsH);
    } else {
        rdShowDeny(host, cand.top, g.gsH);
    }
}

function rdTrackPointer(ev) {
    if (ev.clientX == null || ev.clientY == null) { return; }
    rdLastPointer = { x: ev.clientX, y: ev.clientY };
    if (rdCursorGesture) { rdUpdateCursorGesture(); }
}
document.addEventListener("pointermove", rdTrackPointer, true);
document.addEventListener("mousemove", rdTrackPointer, true);

// The GRAB point: captured at pointer-DOWN on a tile, because by the
// time GridStack's drag threshold trips and fires `dragstart` a fast
// mouse flick can already be far outside the grabbed tile -- arming
// from the dragstart-time pointer position alone was confirmed (live
// probe, 2026-07-08) to intermittently leave the tracker inert for
// exactly the fast gestures the ruling is about.
var rdPendingGrab = null;
function rdTrackPointerDown(ev) {
    if (ev.clientX == null || ev.clientY == null) { return; }
    rdLastPointer = { x: ev.clientX, y: ev.clientY };
    // PALETTE drag-in (spec §4.1 palette context, ruling 2026-07-08):
    // palette items live OUTSIDE the grids, so no grid `dragstart` ever
    // fires for them -- arm the gesture straight from the pointer-down
    // on the item, with the device-type geometry from its data
    // attributes. grabRows is 0: a palette row's own height has no
    // relation to the grid's row scale, so the candidate top is simply
    // the row under the cursor. A plain click (no drag) is disarmed by
    // the pointer-up handler below.
    var pal = (ev.target && ev.target.closest)
        ? ev.target.closest(".nbx-rd-palette-item") : null;
    if (pal && pal.getAttribute("data-device-type-id") != null) {
        var uH = parseFloat(pal.getAttribute("data-u-height")) || 1;
        rdCursorGesture = {
            el: pal, palette: true,
            gsH: Math.max(1, Math.round(uH * 2)),
            isFullDepth: pal.getAttribute("data-is-full-depth") === "true",
            grabRows: 0, lastHost: null, lastRow: null,
        };
        rdClearCursorInds();
        rdPendingGrab = null;
        return;
    }
    var t = (ev.target && ev.target.closest)
        ? ev.target.closest(".grid-stack-item") : null;
    rdPendingGrab = t
        ? { el: t, y: ev.clientY, rect: t.getBoundingClientRect() }
        : null;
}
document.addEventListener("pointerdown", rdTrackPointerDown, true);
document.addEventListener("mousedown", rdTrackPointerDown, true);

// A PALETTE gesture ends at pointer-up (there is no grid dragstop/thaw
// bracket for an external drag that never reached a grid). Disarm one
// tick later so GridStack's own (synchronous) mouseup drop processing
// -- which runs onPaletteDrop's enforcement -- still sees the gesture.
function rdTrackPointerUp() {
    if (rdCursorGesture && rdCursorGesture.palette) {
        window.setTimeout(function () {
            if (rdCursorGesture && rdCursorGesture.palette) {
                rdEndCursorGesture();
            }
        }, 0);
    }
}
document.addEventListener("pointerup", rdTrackPointerUp, true);
document.addEventListener("mouseup", rdTrackPointerUp, true);

// Arm the tracker for a fresh gesture of `el`. The grab offset comes
// from the pointer-DOWN capture (preferred -- immune to drag-threshold
// timing); a gesture with no pointerdown on the tile (the test shims
// dispatch only pointermove) falls back to the current pointer position
// when it is inside the tile. No pointer data at all -> stays untracked.
function rdBeginCursorGesture(el, gsH, isFullDepth) {
    rdCursorGesture = null;
    rdClearCursorInds();
    if (!el || gsH <= 0) { return; }
    var grabRows = null;
    if (rdPendingGrab && rdPendingGrab.el === el
            && rdPendingGrab.rect.height > 0) {
        grabRows = Math.max(0, Math.min(gsH - 1, Math.floor(
            (rdPendingGrab.y - rdPendingGrab.rect.top)
            / (rdPendingGrab.rect.height / gsH))));
    } else if (rdLastPointer) {
        var r = el.getBoundingClientRect();
        if (r.height > 0
                && rdLastPointer.x >= r.left && rdLastPointer.x < r.right
                && rdLastPointer.y >= r.top && rdLastPointer.y < r.bottom) {
            grabRows = Math.max(0, Math.min(gsH - 1, Math.floor(
                (rdLastPointer.y - r.top) / (r.height / gsH))));
        }
    }
    if (grabRows == null) { return; }
    // The row the tile occupied BEFORE the gesture: rdCursorCandidate snaps a
    // whole-U device back to whole units only if it started on one, so a
    // legitimately half-unit-mounted device keeps its odd rows.
    var srcNode = el.gridstackNode;
    rdCursorGesture = {
        el: el, gsH: gsH, isFullDepth: !!isFullDepth,
        srcTop: srcNode && srcNode.y != null ? srcNode.y : null,
        grabRows: grabRows, lastHost: null, lastRow: null,
    };
    rdUpdateCursorGesture();
}

// ========================================================================
// Bay layer (spec §10): planned blades live here, not in a GridStack grid.
// ------------------------------------------------------------------------
// A blade is refused by every grid's acceptWidgets (see makeAccept), so
// nothing else in the editor can materialise one. This module owns the whole
// lifecycle: commit a palette drop into a bay cell, render the planned entry,
// remove it again, and hand the pending set to the save payload.
//
// Each pending add is keyed by a client-side ``ref``, which is also what the
// save contract (§10.6) uses to let a blade point at a chassis the SAME save
// is creating -- that chassis has no placement id yet.
// ========================================================================
var rdBayAdds = [];        // [{ref, rackId, cellEl, ...}] -- planned blades
var rdBayRemoves = [];     // [{rackId, deviceId, cellEl}] -- REAL blades flagged
var rdBayRefSeq = 0;

function rdNextBayRef() {
    rdBayRefSeq += 1;
    return "bay-" + rdBayRefSeq;
}

// Identify the chassis a bay cell belongs to, in the two forms the save
// contract needs: a REAL bay (its dcim pk, stamped by the projection) or a
// PLANNED chassis (the owning tile's widget index, resolved to a ``ref`` at
// payload time because the chassis add may not exist server-side yet).
function rdBayOwner(cellEl) {
    var tile = cellEl.closest(".grid-stack-item");
    var block = cellEl.closest(".nbx-rd-rack-block");
    var widx = tile ? parseInt(tile.getAttribute("data-widget-index"), 10) : NaN;
    return {
        rackId: block ? parseInt(block.getAttribute("data-rack-id"), 10) : null,
        tileEl: tile,
        widgetIndex: isNaN(widx) ? null : widx,
        bayId: parseInt(cellEl.getAttribute("data-bay-id"), 10) || null,
        bayName: cellEl.getAttribute("data-bay-name") || "",
    };
}

// The pending adds for one rack, in the shape SaveLayoutItemSerializer wants
// (spec §10.6). A blade whose chassis is itself planned resolves its parent
// through ``refFor`` -- the caller supplies it because only the rack
// controller knows which widget index maps to which item ref.
function rdBayItemsForRack(rackId, refFor) {
    var out = [];
    rdBayRemoves.forEach(function (r) {
        if (r.rackId !== rackId) { return; }
        // A blade removal needs no bay target: it is an ordinary `remove` of
        // the device, and the model takes no target for a removal at all.
        out.push({ kind: "remove", device_id: r.deviceId });
    });
    rdBayAdds.forEach(function (a) {
        if (a.rackId !== rackId) { return; }
        var item = {
            kind: "add",
            device_type_id: a.deviceTypeId,
            target_bay_name: a.bayName,
            proposed_name: "",
        };
        if (a.bayId) {
            item.target_bay_id = a.bayId;
        } else {
            var parentRef = refFor ? refFor(a.parentWidgetIndex) : null;
            if (!parentRef) { return; }   // chassis vanished -- drop the blade
            item.parent_ref = parentRef;
        }
        out.push(item);
    });
    return out;
}

function rdEndCursorGesture() {
    rdCursorGesture = null;
    rdClearCursorInds();
}

export {
    rdIsChildEl,
    rdCursorCandidate,
    rdBeginCursorGesture,
    rdEndCursorGesture,
    rdBayItemsForRack,
    rdCursorGesture,
};
