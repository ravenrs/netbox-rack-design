/*
 * Ownership of the editor's "suppress the dirty flag" state.
 *
 * This was a closure variable in editor.js written from BOTH sides of the
 * initRack boundary -- the module level (freezeAllTiles / thawAllTiles) and
 * inside the rack controller (the load-time hatch derivation and
 * syncOwnedShadows). An ES module importer cannot assign to an imported
 * binding, so that shape is exactly what blocked moving initRack out. The flag
 * lives here now and every write goes through a function, which leaves one
 * owner and lets both sides be plain importers.
 */

// Set while a controller is re-deriving its purely-visual full-depth opposite
// hatches: those grid mutations are not user edits, so they must not flip the
// dirty state or arm the Save button.
let suppressDirty = false;

function isDirtySuppressed() {
    return suppressDirty;
}

// The save/restore bracket every call site uses. There is deliberately no flat
// set/clear pair: the one site that had one -- the load-time lock/detach pass
// over the three grids -- was not exception-safe, so a throw anywhere inside it
// left the flag stuck true for the life of the page and Save silently stopped
// arming, with nothing in the console to say why.
// Restores the PREVIOUS value rather than false, so an inner suppression
// cannot end an outer one early.
function withDirtySuppressed(fn) {
    const prev = suppressDirty;
    suppressDirty = true;
    try {
        return fn();
    } finally {
        suppressDirty = prev;
    }
}

export { isDirtySuppressed, withDirtySuppressed };
