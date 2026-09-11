/*
 * Phase 2 push neutralization -- extracted verbatim from editor.js, where this
 * block sat as one contiguous section above the editor-root check. It wraps the
 * shared GridStack engine prototype so the vendor's collision-driven pushing is
 * disabled for the whole duration of a gesture; legality is the read-model's
 * call (rd/model.js's rdCanPlaceAt), never the engine's collision cascade.
 *
 * Patches GridStack.Engine.prototype at module evaluation, exactly as before:
 * gridstack-all.js is a classic script and therefore runs before any module.
 */

// Phase 0 (spec §5, §7) used to wrap the shared engine prototype's
// _fixCollisions with a per-engine recursion-depth cap here, as a vendor-
// level backstop against a densely-packed float:true rack sending
// GridStack's engine into infinite mutual recursion between
// _fixCollisions()/moveNode(). Phase 2's push neutralization below (which
// disables GridStack's collision-driven pushing for the ENTIRE duration
// of any gesture, not just capping a runaway cascade) already made that
// recursion structurally unreachable; Phase 4 removed the now-redundant
// guard once the full gate (incl. the dense-pack E8 + hatch-overlap
// regression tests) was confirmed green without it.
//
// ---- Phase 2 push neutralization (spec §5, §4.1) -----------------------
// GridStack's Engine._fixCollisions() is what "resolves" a collision by
// pushing either the moving node itself further down (past a locked
// neighbour) or the OTHER node aside (moveNode(other, ...)) -- both are
// exactly the "the engine decides placement" behaviour the spec forbids:
// legality is OUR call (rdCanPlaceAt, see the Phase 1/2 read-model
// section below), made once on drop, never GridStack's mid-drag collision
// cascade. Every gesture (a real drag, a shim-driven move, a palette
// drag-in) is already bracketed by freezeAllTiles/thawAllTiles below;
// rdPushSuppressDepth mirrors that SAME bracket as a counter (not a bool)
// so a nested freeze/thaw pair -- e.g. a cross-rack adoption's deferred
// thaw racing a fresh drag's freeze -- never leaves suppression stuck on,
// nor turns it off while an outer gesture is still in flight.
//
// TWO layers, because a live-mouse drag was confirmed (probe, see the
// Phase 2 handoff notes) to relocate OTHER tiles via a path that never
// goes through _fixCollisions at all (GridStack's own drag-collision
// math can call Engine.moveNode(otherNode, ...) directly) -- suppressing
// _fixCollisions alone was NOT sufficient:
//   1. _fixCollisions is a no-op while suppressed (belt): stops the
//      classic push-cascade (and is what the recursion-depth guard above
//      was originally added to cap).
//   2. moveNode itself refuses to reposition any node whose element
//      freezeOthers has marked `_rdFrozen` for this gesture (suspenders,
//      and the one that actually matters): this blocks a relocation
//      REGARDLESS of which internal GridStack code path asked for it.
//      The gesture's own tile is deliberately excluded from freezing
//      (freezeOthers' `exceptEl`), so it alone is still free to move
//      wherever the pixel/cell math (or a test shim's fastSetY) puts it,
//      colliding or not -- tileOverlapsOther/rdCanPlaceAt independently
//      re-scans for a genuine collision on drop and reverts (cancelMove)
//      if the target is illegal. THAT is what decides accept/reject, not
//      GridStack.
// Outside a gesture (suppression off / nothing frozen), both wrapped
// methods behave exactly as before -- this is purely additive.
var rdPushSuppressDepth = 0;
function rdBeginPushSuppression() { rdPushSuppressDepth++; }
function rdEndPushSuppression() {
    if (rdPushSuppressDepth > 0) { rdPushSuppressDepth--; }
}
(function guardPushDuringGesture() {
    if (!GridStack.Engine || !GridStack.Engine.prototype) { return; }
    var proto = GridStack.Engine.prototype;
    if (proto.__rdPushGuarded) { return; }
    var origFix = proto._fixCollisions;
    var origMove = proto.moveNode;
    var origPack = proto._packNodes;
    var origMoveCheck = proto.moveNodeCheck;
    if (typeof origFix !== "function" || typeof origMove !== "function") { return; }
    proto._fixCollisions = function () {
        if (rdPushSuppressDepth > 0) { return false; }
        return origFix.apply(this, arguments);
    };
    proto.moveNode = function (node) {
        if (node && node.el && node.el._rdFrozen) { return false; }
        return origMove.apply(this, arguments);
    };
    // THIRD layer (found root-causing the 2026-07-08 live stale-shadow
    // bug): the two suppressed/guarded methods above are NOT the only
    // vendor paths that reposition OTHER nodes during a drag --
    //   * Engine._packNodes()'s float branch does DIRECT `n.y = ...`
    //     writes (no moveNode, no _fixCollisions) to float any node whose
    //     y drifted from its `_orig` snapshot, and it runs from the tail
    //     of every Engine.moveNode call (`t.pack` defaults on);
    //   * Engine.moveNodeCheck() -- the entry point GridStack's live
    //     drag-over uses on a maxRow grid (every rack face grid sets
    //     gs-max-row) -- simulates the move on a CLONED engine and then
    //     copies every dirty clone's position back onto the REAL nodes
    //     via direct copyPos writes, bypassing moveNode entirely.
    // Both are neutralized the same way as _fixCollisions: while a
    // gesture's suppression bracket is open, _packNodes is a no-op and
    // moveNodeCheck degrades to a plain (guarded) moveNode of the checked
    // node itself -- so during any gesture the ONLY node that can change
    // position through ANY engine path is the gesture's own tile, which
    // is exactly spec §4.1's "no other tile may change position as a side
    // effect".
    if (typeof origPack === "function") {
        proto._packNodes = function () {
            if (rdPushSuppressDepth > 0) { return this; }
            return origPack.apply(this, arguments);
        };
    }
    if (typeof origMoveCheck === "function") {
        proto.moveNodeCheck = function (node, o) {
            if (rdPushSuppressDepth > 0) {
                return proto.moveNode.call(this, node, o);
            }
            return origMoveCheck.apply(this, arguments);
        };
    }
    proto.__rdPushGuarded = true;
})();

export { rdBeginPushSuppression, rdEndPushSuppression };
