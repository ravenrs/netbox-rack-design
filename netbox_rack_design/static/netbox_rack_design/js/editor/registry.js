/*
 * The cross-rack registry -- extracted verbatim from editor.js. Each rack
 * controller registers itself here so the cross-rack flow can reach the OTHER
 * rack's grids, and the freeze/thaw bracket that shields a whole gesture lives
 * here with it.
 *
 * tileInFlight is a HOLDER ({current}) rather than a bare binding: the rack
 * controller writes it on dragstart, and an ES module importer cannot assign
 * to an imported binding -- but it can mutate an imported object's property.
 */

import { rdBeginPushSuppression, rdEndPushSuppression } from "rd/push.js";
import { withDirtySuppressed } from "rd/dirty.js";
import { rdEndCursorGesture } from "rd/cursor.js";

var controllersByRackId = {};
const tileInFlight = { current: null };

// Freeze/thaw every rack's tiles around a drag so a moved or newly-added
// device can never displace an existing planned tile (see freezeOthers/thaw).
function freezeAllTiles(exceptEl) {
    // Begin push suppression for the WHOLE gesture this freeze opens (see
    // guardPushDuringGesture above) -- matched 1:1 by thawAllTiles' end
    // below, at every call site that already pairs these two today.
    rdBeginPushSuppression();
    withDirtySuppressed(function () {
        Object.keys(controllersByRackId).forEach(function (rid) {
            controllersByRackId[rid].freezeOthers(exceptEl);
        });
    });
}
function thawAllTiles() {
    try {
        withDirtySuppressed(function () {
            Object.keys(controllersByRackId).forEach(function (rid) {
                controllersByRackId[rid].thaw();
            });
        });
    } finally {
        // End push suppression AFTER the thaw itself (a thawed tile's own
        // grid.update() must still be shielded from _fixCollisions).
        rdEndPushSuppression();
    }
    // Gesture-end settle for EVERY rack (live bug, 2026-07-08): a gesture
    // can transiently disturb tiles on ANY rack the pointer passed over
    // (vendor drag-over paths, see guardPushDuringGesture's third layer),
    // and the per-rack event flow does not guarantee a final refresh on
    // racks the gesture merely crossed. One deferred refreshGhosts per
    // rack after every gesture guarantees classes + owned shadows are
    // re-synced from the settled DOM no matter which path the gesture
    // took. scheduleRefresh is a debounced setTimeout(0) into an
    // idempotent reconciliation, so this is cheap.
    Object.keys(controllersByRackId).forEach(function (rid) {
        if (controllersByRackId[rid].scheduleRefresh) {
            controllersByRackId[rid].scheduleRefresh();
        }
    });
    // The gesture is over: disarm the cursor tracker + deny indicator
    // (spec §4.1 cursor-governed placement). Drop-time enforcement has
    // already run by now (maybePromptMove precedes the thaw on every
    // drop path).
    rdEndCursorGesture();
}

// Search EVERY rendered rack block for the move-out ghost of a placement.
// Used by ×/cancel on a RELOADED cross-rack move_in tile (its ghost lives in
// a different block than the tile). Returns {controller, ghostEl} or null.
function findGhostAcrossBlocks(placementId) {
    if (placementId == null) { return null; }
    var hit = null;
    Object.keys(controllersByRackId).forEach(function (rid) {
        if (hit) { return; }
        var c = controllersByRackId[rid];
        var g = c.findGhost(placementId);
        if (g) { hit = { controller: c, ghostEl: g }; }
    });
    return hit;
}

export {
    controllersByRackId,
    tileInFlight,
    freezeAllTiles,
    thawAllTiles,
    findGhostAcrossBlocks,
};
