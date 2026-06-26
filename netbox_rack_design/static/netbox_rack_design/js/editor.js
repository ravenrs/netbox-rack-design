/*
 * Interactive single-rack layout editor for NetBox Rack Design (Stage 2, slice
 * 2a: MOVE + REMOVE on a single rack). Adapted from netbox-reorder-rack's
 * rack.js, but driven by the projection contract:
 *
 *   - The page embeds a JSON payload in <script id="rd-editor-data"> — one
 *     widget per projected slot, in the order (*front, *rear, *non_racked).
 *     Each grid tile carries data-widget-index pointing back into that array.
 *   - We init three NON-static GridStacks (front, rear, non-racked tray) so a
 *     device can be dragged vertically, between faces, or off the rack.
 *   - On Save we walk the live DOM, derive each item's USER-intent kind
 *     (existing / move / remove), compute u_position via the inverse of the
 *     server-side slot_gs_y filter, and POST a diff to data-save-url.
 *
 * No real Devices are mutated server-side; the endpoint only writes
 * DesignPlacement rows.
 */
(function () {
    "use strict";

    if (typeof GridStack === "undefined") {
        return;
    }

    var root = document.getElementById("rd-editor");
    if (!root) {
        return;
    }

    // ---- Context from the template -----------------------------------------
    var saveUrl = root.getAttribute("data-save-url");
    var rackId = parseInt(root.getAttribute("data-rack-id"), 10);
    var rackUHeight = parseInt(root.getAttribute("data-u-height"), 10);
    var descUnits = root.getAttribute("data-desc-units") === "true";

    // CSRF token. NetBox sets CSRF_COOKIE_HTTPONLY=True, so the cookie is NOT
    // readable from JS — we cannot rely on document.cookie. We resolve it from
    // (in order): the token the template rendered onto #rd-editor via
    // {{ csrf_token }}, NetBox's `netbox_csrf_token` global if present, then the
    // hidden form input. (This mirrors netbox-reorder-rack's proven approach.)
    function getCsrfToken() {
        var fromAttr = root.getAttribute("data-csrf-token");
        if (fromAttr) { return fromAttr; }
        if (typeof window.netbox_csrf_token !== "undefined" && window.netbox_csrf_token) {
            return window.netbox_csrf_token;
        }
        var input = document.querySelector("[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }

    // ---- Hydrate the widget payload (index -> widget) ----------------------
    var widgets = [];
    var dataEl = document.getElementById("rd-editor-data");
    try {
        widgets = JSON.parse(dataEl.textContent || "[]");
    } catch (e) {
        widgets = [];
    }

    // Per-widget runtime state, keyed by global index. We snapshot the ORIGINAL
    // (u_position, face) so at save time we can tell "moved" from "unchanged".
    var state = widgets.map(function (w) {
        return {
            widget: w,
            origUPosition: w.u_position,         // number | null
            origFace: w.face || "",              // "front" | "rear" | ""
            // A tile projected from a pre-existing REMOVE placement arrives
            // already flagged for removal — seed `removed` from the projected
            // state so an untouched round-trip re-asserts the remove (rather than
            // mis-serializing it as a move). The × button toggles this for any
            // tile; for a `remove` tile the user can un-remove by clicking ×.
            removed: w.kind === "remove",
        };
    });

    var changesMade = false;
    var saveButton = document.getElementById("rd-editor-save");
    var toastContainer = document.getElementById("rd-editor-toasts");

    function markDirty() {
        changesMade = true;
        if (saveButton) {
            saveButton.removeAttribute("disabled");
        }
    }

    // ---- Toasts (Bootstrap 5; bundled with NetBox) -------------------------
    function createToast(level, title, message) {
        var icon = "mdi-alert";
        if (level === "success") { icon = "mdi-check-circle"; }
        else if (level === "info") { icon = "mdi-information"; }

        var el = document.createElement("div");
        el.className = "toast";
        el.setAttribute("role", "alert");
        el.setAttribute("aria-live", "assertive");
        el.setAttribute("aria-atomic", "true");

        var header = document.createElement("div");
        header.className = "toast-header bg-" + level;
        var i = document.createElement("i");
        i.className = "mdi " + icon + " me-1";
        var strong = document.createElement("strong");
        strong.className = "me-auto";
        strong.textContent = title;
        var close = document.createElement("button");
        close.type = "button";
        close.className = "btn-close";
        close.setAttribute("data-bs-dismiss", "toast");
        close.setAttribute("aria-label", "Close");
        header.appendChild(i);
        header.appendChild(strong);
        header.appendChild(close);

        var body = document.createElement("div");
        body.className = "toast-body";
        body.textContent = (message || "").trim();

        el.appendChild(header);
        el.appendChild(body);
        (toastContainer || document.body).appendChild(el);

        var ctor = (window.bootstrap && window.bootstrap.Toast) || window.Toast;
        if (ctor) {
            var t = new ctor(el, { delay: 6000 });
            el.addEventListener("hidden.bs.toast", function () { el.remove(); });
            t.show();
        } else {
            // Fallback if Bootstrap's JS isn't present: leave it on screen.
            el.classList.add("show");
        }
    }

    // ---- gs-y -> u_position (inverse of templatetags/rack_design.slot_gs_y) -
    // slot_gs_y forward (H = rack_u_height*2, h = item u_height*2, u = pos*2):
    //   desc_units : gs_y = u - 2
    //   h > 1      : gs_y = H - u - h + 2
    //   else       : gs_y = H - u
    // Working in U units: y = gs_y/2, uHeight = gs_h/2, rackH = rack u_height.
    //   desc_units : pos = y + 1
    //   uHeight>1  : pos = rackH - y - uHeight + 1
    //   else       : pos = rackH - y
    function gsYToUPosition(gsY, gsH) {
        var y = gsY / 2;
        var uHeight = gsH / 2;
        if (descUnits) {
            return y + 1;
        }
        if (uHeight > 1) {
            return rackUHeight - y - uHeight + 1;
        }
        return rackUHeight - y;
    }

    // ---- u_position -> gs-y (forward; inverse of gsYToUPosition) -------------
    // Mirrors templatetags/rack_design.slot_gs_y (working in GridStack rows,
    // i.e. half-U units). gsH is the tile's gs-h (= u_height * 2). Used to place
    // a dynamically-created move_out_ghost back at a device's ORIGINAL slot.
    function uPositionToGsY(uPosition, gsH) {
        if (descUnits) {
            return uPosition * 2 - 2;
        }
        if (gsH > 2) {
            return rackUHeight * 2 - uPosition * 2 - gsH + 2;
        }
        return rackUHeight * 2 - uPosition * 2;
    }

    // ---- Initialise the three grids ----------------------------------------
    function commonOptions(extra) {
        var opts = {
            cellHeight: 11,
            margin: 0,
            marginBottom: 1,
            column: 1,
            float: true,
            animate: true,
            disableResize: true,   // slice 2a: move only, no resize
            acceptWidgets: true,   // allow drops between grids
            removable: false,
            // Don't start a drag when the pointer goes down on a tile's remove
            // (×) button — otherwise GridStack captures the pointer and the
            // click that toggles removal never fires.
            draggable: { cancel: ".nbx-rd-remove-btn" },
        };
        if (extra) {
            Object.keys(extra).forEach(function (k) { opts[k] = extra[k]; });
        }
        return opts;
    }

    var frontEl = document.getElementById("nbx-rd-grid-front");
    var rearEl = document.getElementById("nbx-rd-grid-rear");
    var trayEl = document.getElementById("nbx-rd-grid-tray");

    var grids = [];
    var frontGrid = frontEl ? GridStack.init(commonOptions(), frontEl) : null;
    var rearGrid = rearEl ? GridStack.init(commonOptions(), rearEl) : null;
    // The tray is unbounded vertically; let dropped items float to the top.
    var trayGrid = trayEl ? GridStack.init(commonOptions({ float: false }), trayEl) : null;

    [frontGrid, rearGrid, trayGrid].forEach(function (g) {
        if (g) { grids.push(g); }
    });

    grids.forEach(function (grid) {
        grid.on("change", markDirty);
        grid.on("added", markDirty);
        grid.on("removed", markDirty);
        grid.on("dropped", markDirty);
    });

    // Lock move_out_ghost tiles: they only visualise the U a moved device is
    // vacating, so they must not be draggable. Also lock tiles that arrive
    // already flagged for removal (a projected `remove` placement) — they follow
    // the same "removed ⇒ not draggable" rule the × toggle enforces.
    [[frontGrid, frontEl], [rearGrid, rearEl], [trayGrid, trayEl]].forEach(function (pair) {
        var g = pair[0], host = pair[1];
        if (!g || !host) { return; }
        host.querySelectorAll(".nbx-rd-state-move_out_ghost, .nbx-rd-state-remove").forEach(function (el) {
            g.update(el, { noMove: true, locked: true });
        });
    });

    // Map a face ("front"/"rear") to its grid + host so we can drop a ghost back
    // onto the device's original face.
    var faceGrids = {
        front: { grid: frontGrid, host: frontEl },
        rear: { grid: rearGrid, host: rearEl },
    };

    // ---- Live move visualisation -------------------------------------------
    // When the user drags a real (`existing`) device off its original slot we
    // leave a locked grey "move_out_ghost" tile behind at that original slot and
    // restyle the dragged tile as a cyan `move_in`. Dragging it back onto its
    // original slot removes the ghost and restores the `existing` styling.
    //
    // Pre-existing `move_in` tiles already ship with a STATIC server-rendered
    // ghost, so we never synthesise one for them (we'd duplicate it). Temp
    // ghosts are keyed by global widget index so there is at most one per device.
    var tempGhosts = {};      // widget-index -> ghost element
    var refreshing = false;   // re-entrancy guard (our grid.update calls fire `change`)

    function makeGhostElement(label) {
        var item = document.createElement("div");
        item.className = "grid-stack-item nbx-rd-state-move_out_ghost";
        item.setAttribute("data-rd-temp-ghost", "1");
        // No data-widget-index: buildRackPayload skips tiles without a state
        // entry, and the explicit marker makes the intent obvious.
        var content = document.createElement("div");
        content.className = "grid-stack-item-content";
        content.setAttribute("title", (label || "") + " (move out)");
        var span = document.createElement("span");
        span.className = "nbx-rd-label";
        span.textContent = label || "";
        content.appendChild(span);
        item.appendChild(content);
        return item;
    }

    function removeTempGhost(idx) {
        var ghost = tempGhosts[idx];
        if (!ghost) { return; }
        var g = (ghost.gridstackNode && ghost.gridstackNode.grid) || null;
        if (g) {
            g.removeWidget(ghost, true);
        } else if (ghost.parentNode) {
            ghost.parentNode.removeChild(ghost);
        }
        delete tempGhosts[idx];
    }

    function ensureTempGhost(idx, st) {
        if (tempGhosts[idx]) { return; }
        var face = st.origFace;
        var target = faceGrids[face];
        if (!target || !target.grid) { return; }
        var w = st.widget;
        var gsH = Math.round((w.u_height || 1) * 2);
        var gsY = uPositionToGsY(st.origUPosition, gsH);
        var ghost = makeGhostElement(w.label);
        // addWidget places + registers the node; then lock it so it can't drag.
        var added = target.grid.addWidget(ghost, {
            x: 0, y: gsY, w: 1, h: gsH, noMove: true, noResize: true, locked: true,
        });
        var el = added || ghost;
        target.grid.update(el, { noMove: true, locked: true });
        tempGhosts[idx] = el;
    }

    function faceOfItem(itemEl) {
        var host = itemEl.closest(".grid-stack");
        if (!host) { return ""; }
        if (host === frontEl) { return "front"; }
        if (host === rearEl) { return "rear"; }
        return "";   // tray / off-rack
    }

    // Paint a tile to look like a normally-installed (existing) device, using the
    // device's role color stamped on the content element by the template
    // (data-role-bg / data-role-fg). Devices with no role color get no inline
    // style — matching how a role-less existing device renders. Used by every
    // restore-to-existing path so a cancelled move/remove shows the real color
    // instead of the grey the move_in/ghost/remove CSS leaves behind once its
    // class is dropped.
    function applyExistingColor(itemEl) {
        var content = itemEl.querySelector(".grid-stack-item-content");
        if (!content) { return; }
        var bg = content.getAttribute("data-role-bg");
        var fg = content.getAttribute("data-role-fg");
        if (bg) {
            content.style.backgroundColor = "#" + bg;
            content.style.color = fg ? "#" + fg : "";
        } else {
            // Role-less device: clear any leftover inline color so it falls back
            // to the plain existing styling.
            content.style.backgroundColor = "";
            content.style.color = "";
        }
    }

    // Re-evaluate every eligible real-device tile and converge the ghosts +
    // move_in styling to match where each tile currently sits. Idempotent: safe
    // to call after any drag, and never spawns a second ghost for a device.
    function refreshGhosts() {
        if (refreshing) { return; }
        refreshing = true;
        try {
            root.querySelectorAll(".grid-stack-item").forEach(function (itemEl) {
                if (itemEl.getAttribute("data-rd-temp-ghost")) { return; }
                var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
                var st = state[idx];
                if (!st) { return; }
                var w = st.widget;
                // Only ORIGINALLY-existing real devices get a synthesised ghost.
                // Pre-existing move_in tiles keep their static server ghost; adds
                // and ghosts are excluded.
                if (w.device_id == null) { return; }
                if (w.kind !== "existing") { return; }
                // A tile flagged for removal is locked and handled by toggleRemove.
                if (st.removed) { return; }

                var node = itemEl.gridstackNode;
                var curFace = faceOfItem(itemEl);
                var curGsY = node && node.y != null ? node.y : null;
                var gsH = Math.round((w.u_height || 1) * 2);
                var origGsY = uPositionToGsY(st.origUPosition, gsH);

                var atOrigin = (curFace === st.origFace) && (curGsY === origGsY);

                if (atOrigin) {
                    removeTempGhost(idx);
                    itemEl.classList.remove("nbx-rd-state-move_in");
                    itemEl.classList.add("nbx-rd-state-existing");
                    // Restore the device's real role color (move_in CSS left the
                    // tile cyan / colorless) and drop the per-tile "dirty" outline.
                    applyExistingColor(itemEl);
                    itemEl.classList.remove("nbx-rd-dirty");
                } else {
                    ensureTempGhost(idx, st);
                    itemEl.classList.remove("nbx-rd-state-existing");
                    itemEl.classList.add("nbx-rd-state-move_in");
                    itemEl.classList.add("nbx-rd-dirty");
                }
            });
        } finally {
            refreshing = false;
        }
    }

    // Recompute ghosts whenever a drag settles. `dragstop` fires per grid after
    // the user releases; `dropped` covers cross-grid moves. We defer to the next
    // tick so GridStack has finalised each node's y/h first.
    function scheduleRefresh() {
        if (refreshing) { return; }
        window.setTimeout(refreshGhosts, 0);
    }
    // When the user STARTS dragging a tile that already has a temp ghost, drop
    // that ghost first so its original slot is free — otherwise the locked ghost
    // would block the device from settling back onto its own origin (and a drop
    // there would be pushed one slot away). refreshGhosts re-creates the ghost on
    // drop only if the tile ended somewhere other than its origin.
    function onDragStart(event, el) {
        if (!el) { return; }
        var idx = parseInt(el.getAttribute("data-widget-index"), 10);
        if (tempGhosts[idx]) {
            removeTempGhost(idx);
        }
    }
    grids.forEach(function (grid) {
        grid.on("dragstart", onDragStart);
        grid.on("dragstop", scheduleRefresh);
        grid.on("dropped", scheduleRefresh);
        // `change` is the catch-all: it fires for drags (after the node's y/h are
        // finalised) and for cross-grid moves, so the ghosts converge even if a
        // particular GridStack build doesn't surface `dragstop`. refreshGhosts is
        // idempotent and guarded against the events its own add/remove emit.
        grid.on("change", scheduleRefresh);
    });

    // ---- Remove affordance --------------------------------------------------
    // Resolve the GridStack instance that owns a given tile so we can lock /
    // unlock it. GridStack stamps the live grid onto each item's node; fall back
    // to matching the tile's host .grid-stack element against our grids.
    function gridForItem(itemEl) {
        if (itemEl.gridstackNode && itemEl.gridstackNode.grid) {
            return itemEl.gridstackNode.grid;
        }
        var host = itemEl.closest(".grid-stack");
        var found = null;
        grids.forEach(function (g) {
            if (g.el === host) { found = g; }
        });
        return found;
    }

    // Is this tile currently representing a MOVE (as opposed to an untouched
    // existing device)? Two cases: (a) a pre-existing move loaded from the design
    // (widget.kind === "move_in"), or (b) an existing device the user dragged off
    // its origin this session (we tagged it nbx-rd-state-move_in + a temp ghost).
    function isMoveTile(itemEl, idx, st) {
        if (st.removed) { return false; }
        if (st.widget.kind === "move_in") { return true; }
        if (tempGhosts[idx]) { return true; }
        return itemEl.classList.contains("nbx-rd-state-move_in");
    }

    // The STATIC server-rendered move_out_ghost tile for a pre-existing move,
    // matched by shared placement_id. It carries the device's REAL position.
    function staticGhostFor(placementId) {
        if (placementId == null) { return null; }
        var found = null;
        root.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (el) {
            if (found) { return; }
            if (el.getAttribute("data-rd-temp-ghost")) { return; }
            var gidx = parseInt(el.getAttribute("data-widget-index"), 10);
            var gst = state[gidx];
            if (gst && gst.widget.placement_id === placementId) { found = el; }
        });
        return found;
    }

    // Cancel a planned move: return the device to its REAL original slot, restyle
    // it as a normal existing device, and drop the ghost that marked the vacated
    // slot. For a pre-existing move this makes buildRackPayload emit the device as
    // `existing` at its real position, so the server's at_real branch deletes the
    // move placement; for a session move it simply restores the pre-drag state.
    function cancelMove(itemEl, idx, st) {
        var w = st.widget;
        var gsH = Math.round((w.u_height || 1) * 2);

        // Resolve the device's REAL original slot (face + gs-y).
        var origFace, origGsY;
        if (w.kind === "move_in") {
            // Real position lives on the companion static ghost (same placement).
            var ghost = staticGhostFor(w.placement_id);
            if (ghost) {
                var gidx = parseInt(ghost.getAttribute("data-widget-index"), 10);
                var gst = state[gidx];
                origFace = gst.widget.face || "";
                origGsY = uPositionToGsY(gst.widget.u_position, Math.round((gst.widget.u_height || 1) * 2));
                // The ghost slot becomes the live existing tile — remove the ghost.
                var gg = (ghost.gridstackNode && ghost.gridstackNode.grid) || null;
                if (gg) { gg.removeWidget(ghost, true); } else if (ghost.parentNode) { ghost.parentNode.removeChild(ghost); }
            } else {
                // No ghost (shouldn't happen): fall back to the tile's own face.
                origFace = w.face || "";
                origGsY = uPositionToGsY(w.u_position, gsH);
            }
        } else {
            // Session move of an originally-existing device.
            origFace = st.origFace;
            origGsY = uPositionToGsY(st.origUPosition, gsH);
            removeTempGhost(idx);
        }

        // Move the tile onto its original slot, in its original face's grid.
        var target = faceGrids[origFace];
        refreshing = true;   // suppress our own change-driven ghost recompute
        try {
            if (target && target.grid) {
                var curGrid = gridForItem(itemEl);
                if (curGrid && curGrid !== target.grid) {
                    // Cross-face: move the DOM node into the destination grid.
                    curGrid.removeWidget(itemEl, false);
                    target.grid.makeWidget(itemEl);
                }
                target.grid.update(itemEl, { x: 0, y: origGsY, w: 1, h: gsH, noMove: false, locked: false });
            }
        } finally {
            refreshing = false;
        }

        // Restyle as a plain existing device. We DON'T mutate w.kind for a session
        // move (it's already "existing"); for a pre-existing move_in we leave
        // w.kind as-is but the tile now sits at the real slot, so buildRackPayload
        // (which sends move_in/existing tiles as `existing` at their live spot)
        // emits the real position → server clears the move.
        itemEl.classList.remove("nbx-rd-state-move_in", "nbx-rd-state-move_out_ghost");
        itemEl.classList.add("nbx-rd-state-existing");
        // Paint the device's real role color (a pre-existing move_in tile never
        // carried an inline color, so without this it would render grey/blank).
        applyExistingColor(itemEl);
        // The tile now reads as a plain existing device — clear the per-tile
        // "dirty" outline. The change itself is still unsaved, so markDirty()
        // below keeps Save enabled (global dirty state is independent).
        itemEl.classList.remove("nbx-rd-dirty");
        markDirty();
    }

    // Toggle removal flag on an existing device. A flagged tile is LOCKED from
    // dragging (a device being removed shouldn't also be moved); click × again to
    // un-flag and unlock it.
    function flagRemove(itemEl, idx, st) {
        st.removed = !st.removed;
        itemEl.classList.toggle("nbx-rd-state-remove", st.removed);
        itemEl.classList.toggle("nbx-rd-dirty", st.removed);
        // On un-flag the tile is a plain existing device again — restore its role
        // color. (While flagged the remove CSS overrides any inline color via
        // !important, so we can leave the inline style untouched when flagging.)
        if (!st.removed) {
            applyExistingColor(itemEl);
        }
        var grid = gridForItem(itemEl);
        if (grid) {
            grid.update(itemEl, { noMove: st.removed, locked: st.removed });
        }
        markDirty();
    }

    // Toggle the cancel flag on a planned ADD tile. Flagged → same struck/red look
    // as a removal (+ dirty) and LOCKED from dragging; un-flagged → restore the
    // normal green `add` styling and unlock. We reuse st.removed as the flag; the
    // tile's widget.kind stays "add", so buildRackPayload emits kind:"add" with
    // cancel:true while flagged.
    function flagCancelAdd(itemEl, idx, st) {
        st.removed = !st.removed;
        itemEl.classList.toggle("nbx-rd-state-remove", st.removed);
        itemEl.classList.toggle("nbx-rd-state-add", !st.removed);
        itemEl.classList.toggle("nbx-rd-dirty", st.removed);
        var grid = gridForItem(itemEl);
        if (grid) {
            grid.update(itemEl, { noMove: st.removed, locked: st.removed });
        }
        markDirty();
    }

    // Cancel an UNSAVED add (a palette-dropped device type that was never saved,
    // placement_id == null): there's no server placement to flag, so just delete
    // the tile and orphan its state entry locally. We null the state slot rather
    // than splice the array so every other tile's data-widget-index stays valid.
    function removeUnsavedAdd(itemEl, idx, st) {
        var grid = gridForItem(itemEl);
        if (grid) {
            grid.removeWidget(itemEl, true);
        } else if (itemEl.parentNode) {
            itemEl.parentNode.removeChild(itemEl);
        }
        state[idx] = null;
        markDirty();
    }

    // Context-sensitive × ("undo the planned change on this tile"):
    //   • a planned ADD                                → flag / un-flag cancel
    //   • a MOVE tile (pre-existing or this-session)  → cancel the move
    //   • an existing device                          → flag / un-flag removal
    function handleRemoveClick(itemEl) {
        var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
        var st = state[idx];
        if (!st) { return; }
        if (st.widget.kind === "add") {
            // A planned add has no real device; × cancels the planned addition.
            // An UNSAVED add (no placement_id, e.g. just dragged from the palette)
            // has no server row → remove it locally. A pre-existing add (loaded
            // from the design, has placement_id) is flagged cancel:true for save.
            if (st.widget.placement_id == null) {
                removeUnsavedAdd(itemEl, idx, st);
            } else {
                flagCancelAdd(itemEl, idx, st);
            }
            return;
        }
        // Beyond adds, only real devices respond to ×.
        if (st.widget.device_id == null) { return; }
        if (isMoveTile(itemEl, idx, st)) {
            cancelMove(itemEl, idx, st);
        } else {
            flagRemove(itemEl, idx, st);
        }
    }

    root.addEventListener("click", function (event) {
        var btn = event.target.closest(".nbx-rd-remove-btn");
        if (!btn) { return; }
        event.preventDefault();
        event.stopPropagation();
        var itemEl = btn.closest(".grid-stack-item");
        if (itemEl) {
            handleRemoveClick(itemEl);
        }
    });

    // Belt-and-suspenders alongside draggable.cancel: stop the pointer-down on
    // the × button from reaching GridStack's drag machinery so the subsequent
    // click reliably fires.
    ["pointerdown", "mousedown", "touchstart"].forEach(function (evtName) {
        root.addEventListener(evtName, function (event) {
            if (event.target.closest(".nbx-rd-remove-btn")) {
                event.stopPropagation();
            }
        }, true);
    });

    // ---- Build the save request from the live DOM --------------------------
    function buildRackPayload() {
        var buckets = { front: [], rear: [], other: [] };
        // Dedup by placement_id: an already-saved move can appear as both a
        // move_in tile and a move_out_ghost tile; keep one (prefer non-ghost).
        var seenPlacement = {};

        function pushItem(itemEl, faceKey) {
            // Dynamically-created move-out ghosts are purely visual: they carry a
            // marker and no widget index, so they have no state entry to send.
            if (itemEl.getAttribute("data-rd-temp-ghost")) { return; }
            var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
            var st = state[idx];
            if (!st) { return; }
            var w = st.widget;

            // Skip ghost tiles entirely — they merely visualise the vacated U of
            // a move that the move_in tile already represents.
            if (w.kind === "move_out_ghost") {
                return;
            }

            var placementId = (w.placement_id !== undefined) ? w.placement_id : null;
            if (placementId != null) {
                if (seenPlacement[placementId]) { return; }
                seenPlacement[placementId] = true;
            }

            var isAdd = w.kind === "add";
            var item = {
                kind: null,
                device_id: (w.device_id != null) ? w.device_id : null,
                device_type_id: (w.device_type_id != null) ? w.device_type_id : null,
                placement_id: placementId,
                u_position: null,
                face: "",
            };
            // Carry the intended role/tenant for a brand-new add (set at drop
            // time). Only meaningful for adds; the server ignores them otherwise.
            if (isAdd) {
                if (w.device_role_id != null) { item.device_role_id = w.device_role_id; }
                if (w.tenant_id != null) { item.tenant_id = w.tenant_id; }
            }

            // A planned add the user flagged via × → cancel the addition. The
            // server deletes the add placement when cancel is true; no position is
            // needed. Bucket placement is irrelevant for a delete, so use faceKey.
            if (st.removed && isAdd) {
                item.kind = "add";
                item.cancel = true;
                buckets[faceKey].push(item);
                return;
            }

            // A real device the user flagged via × → removal.
            if (st.removed && item.device_id != null) {
                item.kind = "remove";
                buckets[faceKey].push(item);
                return;
            }

            if (faceKey === "other") {
                // Off-rack bucket: positionless. Adds stay adds.
                item.kind = isAdd ? "add" : "move";
                buckets.other.push(item);
                return;
            }

            // Racked: read the LIVE position from GridStack's node. The gs-y /
            // gs-h DOM attributes are NOT updated after a drag, so reading them
            // would always yield the ORIGINAL position — that is the "no changes
            // detected" bug, and the reason moved/removed tiles were being
            // mis-serialized (and then deleted) on save.
            var node = itemEl.gridstackNode;
            var gsY = (node && node.y != null) ? node.y : parseInt(itemEl.getAttribute("gs-y"), 10);
            var gsH = (node && node.h != null) ? node.h : parseInt(itemEl.getAttribute("gs-h"), 10);
            item.u_position = gsYToUPosition(gsY, gsH);
            item.face = faceKey;

            // Adds stay adds (their position may have changed). Real devices are
            // submitted as 'existing' carrying their CURRENT position; the server
            // promotes them to a move when they no longer sit at their real spot,
            // and clears a stale move when dragged back — so the editor doesn't
            // have to track "moved vs not" itself.
            item.kind = isAdd ? "add" : "existing";
            buckets[faceKey].push(item);
        }

        function walkGrid(grid, faceKey) {
            if (!grid) { return; }
            grid.getGridItems().forEach(function (itemEl) {
                pushItem(itemEl, faceKey);
            });
        }

        walkGrid(frontGrid, "front");
        walkGrid(rearGrid, "rear");
        walkGrid(trayGrid, "other");

        return {
            rack_id: rackId,
            front: buckets.front,
            rear: buckets.rear,
            other: buckets.other,
        };
    }

    function numEq(a, b) {
        if (a == null && b == null) { return true; }
        if (a == null || b == null) { return false; }
        return Math.abs(Number(a) - Number(b)) < 1e-6;
    }

    // ---- Highlight / clear server-reported errors --------------------------
    function clearErrors() {
        root.querySelectorAll(".grid-stack-item.nbx-rd-error").forEach(function (el) {
            el.classList.remove("nbx-rd-error");
        });
    }

    function highlightError(err) {
        // Match the offending widget by device_id + u_position when possible.
        var tiles = root.querySelectorAll(".grid-stack-item");
        tiles.forEach(function (el) {
            var idx = parseInt(el.getAttribute("data-widget-index"), 10);
            var st = state[idx];
            if (!st) { return; }
            if (err.device_id != null && st.widget.device_id === err.device_id) {
                el.classList.add("nbx-rd-error");
            }
        });
    }

    // ---- Save ---------------------------------------------------------------
    function doSave() {
        clearErrors();
        // design_id is the Design pk; the save URL already encodes it, but the
        // SaveLayoutSerializer also accepts it in the body. Derive it from the URL.
        var m = saveUrl.match(/designs\/(\d+)\//);
        var payload = {
            design_id: m ? parseInt(m[1], 10) : null,
            racks: [buildRackPayload()],
        };

        if (saveButton) { saveButton.setAttribute("disabled", "disabled"); }

        fetch(saveUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify(payload),
        }).then(function (response) {
            if (response.status === 200) {
                response.json().then(function () {
                    changesMade = false;
                    createToast("success", "Saved", "Layout saved.");
                    // Refresh to re-project the design with the persisted state.
                    window.location.reload();
                });
            } else if (response.status === 304) {
                changesMade = false;
                createToast("info", "No changes", "No changes were detected.");
            } else if (response.status === 403) {
                if (saveButton) { saveButton.removeAttribute("disabled"); }
                // Surface the server's actual reason rather than assuming a perms
                // problem — a 403 here can also be a CSRF failure.
                response.text().then(function (text) {
                    var detail = "";
                    try {
                        var data = JSON.parse(text);
                        detail = data.detail || data.error || data.message || "";
                    } catch (e) {
                        detail = (text || "").trim();
                    }
                    createToast("danger", "Forbidden", detail || "The request was rejected (403).");
                }).catch(function () {
                    createToast("danger", "Forbidden", "The request was rejected (403).");
                });
            } else if (response.status === 400) {
                if (saveButton) { saveButton.removeAttribute("disabled"); }
                response.json().then(function (data) {
                    var errs = (data && data.errors) || [];
                    if (!errs.length) {
                        createToast("danger", "Error", "The layout could not be saved.");
                        return;
                    }
                    errs.forEach(function (err) {
                        highlightError(err);
                        var where = (err.u_position != null) ? (" (U" + err.u_position + ")") : "";
                        createToast("danger", "Conflict", (err.detail || "Validation error") + where);
                    });
                }).catch(function () {
                    createToast("danger", "Error", "The layout could not be saved.");
                });
            } else {
                if (saveButton) { saveButton.removeAttribute("disabled"); }
                createToast("danger", "Error", "Unexpected response (" + response.status + ").");
            }
        }).catch(function (error) {
            if (saveButton) { saveButton.removeAttribute("disabled"); }
            createToast("danger", "Error", String(error));
        });
    }

    if (saveButton) {
        saveButton.addEventListener("click", doSave);
    }

    // ---- Device-type catalog palette + drag-in -----------------------------
    // A search box lists draggable device types fetched from NetBox's core API;
    // dragging one onto the front/rear grid plans a brand-new KIND_ADD placement.
    // No dcim.Device is ever created — only a synthetic `add` widget + state entry
    // that buildRackPayload serializes as {kind:"add", device_type_id, placement_id:null}.
    (function setupPalette() {
        var paletteEl = document.getElementById("nbx-rd-palette");
        var searchEl = document.getElementById("nbx-rd-palette-search");
        // These three are NetBox API-backed searchable selects rendered from
        // DesignEditorPaletteForm (DynamicModelChoiceField). NetBox's select-init
        // enhances them into remote-loading TomSelects and writes the chosen value
        // back to the underlying <select>, so reading .value by its Django id
        // (id_<name>) gives the selected pk.
        var manufEl = document.getElementById("id_manufacturer");
        var roleEl = document.getElementById("id_device_role");
        var tenantEl = document.getElementById("id_tenant");
        var listEl = document.getElementById("nbx-rd-palette-list");
        var statusEl = document.getElementById("nbx-rd-palette-status");
        var layoutEl = document.querySelector(".nbx-rd-editor-layout");
        if (!paletteEl || !listEl || !searchEl) { return; }
        if (typeof GridStack === "undefined" || !GridStack.setupDragIn) { return; }

        // Match the catalog card's max height to the (currently visible) rack
        // elevation so the card is rack-tall and only the list scrolls. Recompute
        // on resize / face toggle. Falls back to the CSS default when unmeasurable.
        function syncRackHeight() {
            if (!layoutEl) { return; }
            var face = document.getElementById("nbx-rd-face-front");
            if (!face || face.offsetParent === null) {
                face = document.getElementById("nbx-rd-face-rear");
            }
            var grid = face && face.querySelector(".nbx-rd-rack");
            var h = grid ? grid.offsetHeight : 0;
            if (h > 80) {
                layoutEl.style.setProperty("--nbx-rd-rack-height", h + "px");
            }
        }
        syncRackHeight();
        window.addEventListener("resize", syncRackHeight);
        paletteEl.nbxSyncRackHeight = syncRackHeight;   // let the face toggle call it

        function setStatus(msg) {
            if (statusEl) { statusEl.textContent = msg || ""; }
        }

        function renderResults(results) {
            listEl.innerHTML = "";
            if (!results.length) {
                setStatus("No device types found.");
                return;
            }
            setStatus("");
            results.forEach(function (dt) {
                var uHeight = (dt.u_height != null) ? dt.u_height : 1;
                var gsH = Math.max(1, Math.round(uHeight * 2));
                // A palette item must be a real GridStack DRAG SOURCE so the
                // bundled GridStack accepts the drop:
                //   • class "grid-stack-item" — the receiving grid's acceptWidgets
                //     check is `el.matches('.grid-stack-item')`;
                //   • gs-w/gs-h — GridStack sizes the dropped node from these
                //     (via _readAttr);
                //   • an inner ".grid-stack-item-content" — the default drag
                //     HANDLE (draggable.handle === '.grid-stack-item-content').
                // Without these the real HTML5 drag-in never fires.
                var li = document.createElement("div");
                li.className = "list-group-item list-group-item-action grid-stack-item nbx-rd-palette-item";
                li.setAttribute("gs-w", "1");
                li.setAttribute("gs-h", String(gsH));
                li.setAttribute("data-device-type-id", dt.id);
                li.setAttribute("data-u-height", uHeight);
                li.setAttribute("data-is-full-depth", dt.is_full_depth ? "true" : "false");
                var manuf = (dt.manufacturer && (dt.manufacturer.name || dt.manufacturer.display)) || "";
                var label = (manuf ? manuf + " " : "") + (dt.model || dt.display || ("type " + dt.id));
                li.setAttribute("data-label", label);

                var content = document.createElement("div");
                content.className = "grid-stack-item-content";
                var model = document.createElement("div");
                model.className = "nbx-rd-palette-model";
                model.textContent = dt.model || dt.display || ("Device type " + dt.id);
                var meta = document.createElement("div");
                meta.className = "nbx-rd-palette-meta";
                meta.textContent = (manuf ? manuf + " · " : "") + uHeight + "U" + (dt.is_full_depth ? " · full-depth" : "");
                content.appendChild(model);
                content.appendChild(meta);
                li.appendChild(content);
                listEl.appendChild(li);
            });

            // (Re)register the freshly-rendered items as an external drag source.
            // Re-applied every render because the nodes are replaced. appendTo:body
            // + helper:clone so the dragged proxy floats above the layout and the
            // palette list stays intact.
            GridStack.setupDragIn(".nbx-rd-palette-item", { appendTo: "body", helper: "clone" });
        }

        var lastKey = null;
        function fetchTypes() {
            var q = searchEl.value.trim();
            var manufId = manufEl ? manufEl.value : "";
            var url = "/api/dcim/device-types/?brief=true&limit=50";
            if (q) { url += "&q=" + encodeURIComponent(q); }
            if (manufId) { url += "&manufacturer_id=" + encodeURIComponent(manufId); }
            setStatus("Searching…");
            fetch(url, {
                credentials: "same-origin",
                headers: { "Accept": "application/json" },
            }).then(function (resp) {
                if (!resp.ok) { throw new Error("HTTP " + resp.status); }
                return resp.json();
            }).then(function (data) {
                renderResults((data && data.results) || []);
            }).catch(function (err) {
                setStatus("Could not load device types (" + err.message + ").");
            });
        }

        var debounceTimer = null;
        function scheduleFetch() {
            var key = searchEl.value.trim() + "|" + (manufEl ? manufEl.value : "");
            if (key === lastKey) { return; }
            lastKey = key;
            if (debounceTimer) { window.clearTimeout(debounceTimer); }
            debounceTimer = window.setTimeout(fetchTypes, 300);
        }
        searchEl.addEventListener("input", scheduleFetch);
        if (manufEl) {
            // The manufacturer filter changes results immediately (no debounce).
            manufEl.addEventListener("change", function () {
                lastKey = searchEl.value.trim() + "|" + manufEl.value;
                fetchTypes();
            });
        }

        // The manufacturer / role / tenant selects are populated + searched by
        // NetBox's own API-backed select widget (DynamicModelChoiceField); no
        // manual option-loading needed. We only read their current value.

        // Initial device-type population.
        fetchTypes();

        // ---- Convert a dropped palette clone into a planned add tile --------
        // The receiving (front/rear) grid fires `dropped` with the new node. We
        // re-shape it to the device type's U-height, derive u_position + face, and
        // register a synthetic `add` widget in state[] keyed by a fresh index.
        function onPaletteDrop(face, grid, event, previousNode, newNode) {
            // Only handle drops that ORIGINATED in the palette (carry our marker).
            var el = newNode && newNode.el;
            if (!el) { return; }
            var dtId = el.getAttribute("data-device-type-id");
            if (dtId == null) { return; }   // not a palette item (a normal tile move)

            var uHeight = parseFloat(el.getAttribute("data-u-height")) || 1;
            var label = el.getAttribute("data-label") || ("Device type " + dtId);
            var gsH = Math.max(1, Math.round(uHeight * 2));

            // Resize/normalise the dropped node to a single-column rack slot.
            grid.update(el, { x: 0, w: 1, h: gsH });
            var node = el.gridstackNode || newNode;
            var gsY = (node && node.y != null) ? node.y : 0;
            var uPosition = gsYToUPosition(gsY, gsH);

            // Capture the CURRENT role/tenant selections — they apply to this new
            // add only (future drops; not retroactive). Empty select => null.
            var roleId = (roleEl && roleEl.value) ? parseInt(roleEl.value, 10) : null;
            var tenantId = (tenantEl && tenantEl.value) ? parseInt(tenantEl.value, 10) : null;
            var roleName = (roleEl && roleEl.value && roleEl.selectedOptions.length)
                ? roleEl.selectedOptions[0].textContent : "";

            // Register a synthetic add widget + state entry; stamp the tile index.
            var newIdx = state.length;
            var widget = {
                kind: "add",
                device_type_id: parseInt(dtId, 10),
                device_id: null,
                placement_id: null,
                device_role_id: roleId,
                tenant_id: tenantId,
                u_height: uHeight,
                u_position: uPosition,
                label: label,
                face: face,
            };
            state.push({ widget: widget, origUPosition: uPosition, origFace: face, removed: false });

            // Re-shape the dropped element into a standard add tile: clear the
            // palette markup/classes, add state class, inject label + × button.
            el.setAttribute("data-widget-index", newIdx);
            el.removeAttribute("data-device-type-id");
            el.removeAttribute("data-u-height");
            el.removeAttribute("data-is-full-depth");
            el.removeAttribute("data-label");
            el.classList.remove("nbx-rd-palette-item");
            el.classList.add("nbx-rd-state-add");

            var content = el.querySelector(".grid-stack-item-content");
            if (!content) {
                content = document.createElement("div");
                content.className = "grid-stack-item-content";
                el.appendChild(content);
            }
            content.innerHTML = "";
            content.setAttribute(
                "title",
                label + " (U" + Math.round(uPosition) + ", add"
                    + (roleName ? ", role: " + roleName : "") + ")"
            );
            var btn = document.createElement("button");
            btn.type = "button";
            btn.className = "nbx-rd-remove-btn";
            btn.setAttribute("title", "Cancel this planned add");
            btn.setAttribute("aria-label", "Cancel this planned add");
            btn.innerHTML = "&times;";
            var span = document.createElement("span");
            span.className = "nbx-rd-label";
            span.textContent = label;
            content.appendChild(btn);
            content.appendChild(span);

            markDirty();
        }

        // Bind dropped on the racked grids only — a brand-new add must land on a
        // real U. A palette drop onto the TRAY is rejected (the model requires
        // target_position); we delete the stray clone so no position-less add forms.
        [[frontGrid, "front"], [rearGrid, "rear"]].forEach(function (pair) {
            var g = pair[0], face = pair[1];
            if (!g) { return; }
            g.on("dropped", function (event, previousNode, newNode) {
                onPaletteDrop(face, g, event, previousNode, newNode);
            });
        });
        if (trayGrid) {
            trayGrid.on("dropped", function (event, previousNode, newNode) {
                var el = newNode && newNode.el;
                if (el && el.getAttribute("data-device-type-id") != null) {
                    // Reject off-rack palette drops: remove the clone, keep state clean.
                    trayGrid.removeWidget(el, true);
                }
            });
        }
    })();

    // ---- Face toggle (mirrors design_elevation.html) -----------------------
    var faceFront = document.getElementById("nbx-rd-face-front");
    var faceRear = document.getElementById("nbx-rd-face-rear");
    var btnFront = document.getElementById("nbx-rd-show-front");
    var btnRear = document.getElementById("nbx-rd-show-rear");
    function showFace(isFront) {
        if (faceFront) { faceFront.style.display = isFront ? "" : "none"; }
        if (faceRear) { faceRear.style.display = isFront ? "none" : ""; }
        if (btnFront) { btnFront.classList.toggle("active", isFront); }
        if (btnRear) { btnRear.classList.toggle("active", !isFront); }
        // Re-measure the rack height for the catalog card (the newly-shown face's
        // grid is what we size against).
        var pal = document.getElementById("nbx-rd-palette");
        if (pal && typeof pal.nbxSyncRackHeight === "function") { pal.nbxSyncRackHeight(); }
    }
    if (btnFront) { btnFront.addEventListener("click", function () { showFace(true); }); }
    if (btnRear) { btnRear.addEventListener("click", function () { showFace(false); }); }

    // ---- Unsaved-changes guard ---------------------------------------------
    window.addEventListener("beforeunload", function (event) {
        if (changesMade) {
            event.preventDefault();
            event.returnValue = "";
            return "";
        }
    });
})();
