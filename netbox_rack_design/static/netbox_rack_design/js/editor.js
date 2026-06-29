/*
 * Interactive MULTI-RACK layout editor for NetBox Rack Design (Stage 2, slice
 * 2d Phase B). Renders EVERY currently-visible scoped rack of a design side by
 * side and drives them from a SINGLE design-level Save. Adapted from the
 * original single-rack editor; the per-rack behaviour is unchanged, it is just
 * factored into an initRack(block) function that is called for every rack block.
 *
 *   - The page embeds one JSON payload per rack in <script id="rd-editor-data-
 *     <rackId>"> — one widget per projected slot, in the order (*front, *rear,
 *     *non_racked). Each grid tile carries data-widget-index pointing back into
 *     that rack's array.
 *   - Each rack block inits three NON-static GridStacks (front, rear, tray) so a
 *     device can be dragged vertically, between that rack's faces, or off it.
 *   - The catalog + quick-access columns are SHARED; dragging a device type into
 *     a face plans a new add on whichever rack owns the drop-target grid.
 *   - On Save we walk every rack block's live DOM, build a per-rack payload, and
 *     POST a single dict keyed by rack_id to data-save-url.
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

    // ---- Shared context from the template ----------------------------------
    var saveUrl = root.getAttribute("data-save-url");

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

    // ---- Shared dirty state + Save button + toasts -------------------------
    // changesMade is design-level: ANY edit in ANY rack enables the single Save
    // button and arms the beforeunload guard.
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
            // (×) button or a palette row's favorite (star) button — otherwise
            // GridStack captures the pointer and the click never fires.
            draggable: { cancel: ".nbx-rd-remove-btn, .nbx-rd-fav-btn" },
        };
        if (extra) {
            Object.keys(extra).forEach(function (k) { opts[k] = extra[k]; });
        }
        return opts;
    }

    // ========================================================================
    // Per-rack controller. Initialises one rack block's three grids and wires
    // every per-rack behaviour (move ghosts, context-sensitive ×, full-depth
    // hatch, face toggles, palette drops). Returns a small controller the shared
    // Save uses to build that rack's slice of the multi-rack payload.
    // ========================================================================
    function initRack(block) {
        var rackId = parseInt(block.getAttribute("data-rack-id"), 10);
        var rackUHeight = parseInt(block.getAttribute("data-u-height"), 10);
        var descUnits = block.getAttribute("data-desc-units") === "true";

        // ---- Hydrate this rack's widget payload (index -> widget) ----------
        var widgets = [];
        var dataEl = document.getElementById("rd-editor-data-" + rackId);
        try {
            widgets = JSON.parse((dataEl && dataEl.textContent) || "[]");
        } catch (e) {
            widgets = [];
        }

        // Per-widget runtime state, keyed by this rack's local index. We snapshot
        // the ORIGINAL (u_position, face) so at save time we can tell "moved"
        // from "unchanged".
        var state = widgets.map(function (w) {
            return {
                widget: w,
                origUPosition: w.u_position,
                origFace: w.face || "",
                removed: w.kind === "remove",
            };
        });

        // ---- gs-y <-> u_position (inverse of templatetags/rack_design.slot_gs_y)
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
        function uPositionToGsY(uPosition, gsH) {
            if (descUnits) {
                return uPosition * 2 - 2;
            }
            if (gsH > 2) {
                return rackUHeight * 2 - uPosition * 2 - gsH + 2;
            }
            return rackUHeight * 2 - uPosition * 2;
        }

        // ---- Resolve this rack's three grid hosts --------------------------
        var frontEl = document.getElementById("nbx-rd-grid-front-" + rackId);
        var rearEl = document.getElementById("nbx-rd-grid-rear-" + rackId);
        var trayEl = document.getElementById("nbx-rd-grid-tray-" + rackId);

        // Only accept drops that belong to THIS rack: a palette/quick-access drag
        // source (planning a new add), or a tile already inside this rack block
        // (front <-> rear <-> tray). Tiles from OTHER racks are rejected so a
        // real device can't be silently dragged across racks (the save endpoint
        // reconciles each rack independently and has no cross-rack move concept).
        function acceptForBlock(el) {
            if (!el) { return false; }
            if (el.getAttribute && el.getAttribute("data-device-type-id") != null) {
                return true;
            }
            if (el.classList && el.classList.contains("nbx-rd-palette-item")) {
                return true;
            }
            return el.closest && el.closest(".nbx-rd-rack-block") === block;
        }

        var grids = [];
        var frontGrid = frontEl
            ? GridStack.init(commonOptions({ acceptWidgets: acceptForBlock }), frontEl) : null;
        var rearGrid = rearEl
            ? GridStack.init(commonOptions({ acceptWidgets: acceptForBlock }), rearEl) : null;
        // The tray is unbounded vertically; let dropped items float to the top.
        var trayGrid = trayEl
            ? GridStack.init(commonOptions({ float: false, acceptWidgets: acceptForBlock }), trayEl) : null;

        [frontGrid, rearGrid, trayGrid].forEach(function (g) {
            if (g) { grids.push(g); }
        });

        grids.forEach(function (grid) {
            grid.on("change", markDirty);
            grid.on("added", markDirty);
            grid.on("removed", markDirty);
            grid.on("dropped", markDirty);
        });

        // Lock move_out_ghost / pre-existing remove / full-depth opposite tiles:
        // they are passive and must never be draggable.
        [[frontGrid, frontEl], [rearGrid, rearEl], [trayGrid, trayEl]].forEach(function (pair) {
            var g = pair[0], host = pair[1];
            if (!g || !host) { return; }
            host.querySelectorAll(
                ".nbx-rd-state-move_out_ghost, .nbx-rd-state-remove, .nbx-rd-opposite"
            ).forEach(function (el) {
                g.update(el, { noMove: true, noResize: true, locked: true });
            });
        });

        var faceGrids = {
            front: { grid: frontGrid, host: frontEl },
            rear: { grid: rearGrid, host: rearEl },
        };

        // ---- Live move visualisation ---------------------------------------
        var tempGhosts = {};      // widget-index -> ghost element
        var refreshing = false;   // re-entrancy guard

        function makeGhostElement(label) {
            var item = document.createElement("div");
            item.className = "grid-stack-item nbx-rd-state-move_out_ghost";
            item.setAttribute("data-rd-temp-ghost", "1");
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

        function applyExistingColor(itemEl) {
            var content = itemEl.querySelector(".grid-stack-item-content");
            if (!content) { return; }
            var bg = content.getAttribute("data-role-bg");
            var fg = content.getAttribute("data-role-fg");
            if (bg) {
                content.style.backgroundColor = "#" + bg;
                content.style.color = fg ? "#" + fg : "";
            } else {
                content.style.backgroundColor = "";
                content.style.color = "";
            }
        }

        // Converge ghosts + move_in styling to where each tile currently sits.
        // Scoped to THIS rack block so racks never affect each other.
        function refreshGhosts() {
            if (refreshing) { return; }
            refreshing = true;
            try {
                block.querySelectorAll(".grid-stack-item").forEach(function (itemEl) {
                    if (itemEl.getAttribute("data-rd-temp-ghost")) { return; }
                    var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
                    var st = state[idx];
                    if (!st) { return; }
                    var w = st.widget;
                    if (w.opposite_face) { return; }
                    if (w.device_id == null) { return; }
                    if (w.kind !== "existing") { return; }
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

        function scheduleRefresh() {
            if (refreshing) { return; }
            window.setTimeout(refreshGhosts, 0);
        }
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
            grid.on("change", scheduleRefresh);
        });

        // ---- Remove affordance ---------------------------------------------
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

        function isMoveTile(itemEl, idx, st) {
            if (st.removed) { return false; }
            if (st.widget.kind === "move_in") { return true; }
            if (tempGhosts[idx]) { return true; }
            return itemEl.classList.contains("nbx-rd-state-move_in");
        }

        function staticGhostFor(placementId) {
            if (placementId == null) { return null; }
            var found = null;
            block.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (el) {
                if (found) { return; }
                if (el.getAttribute("data-rd-temp-ghost")) { return; }
                var gidx = parseInt(el.getAttribute("data-widget-index"), 10);
                var gst = state[gidx];
                if (gst && gst.widget.placement_id === placementId) { found = el; }
            });
            return found;
        }

        function cancelMove(itemEl, idx, st) {
            var w = st.widget;
            var gsH = Math.round((w.u_height || 1) * 2);

            var origFace, origGsY;
            if (w.kind === "move_in") {
                var ghost = staticGhostFor(w.placement_id);
                if (ghost) {
                    var gidx = parseInt(ghost.getAttribute("data-widget-index"), 10);
                    var gst = state[gidx];
                    origFace = gst.widget.face || "";
                    origGsY = uPositionToGsY(gst.widget.u_position, Math.round((gst.widget.u_height || 1) * 2));
                    var gg = (ghost.gridstackNode && ghost.gridstackNode.grid) || null;
                    if (gg) { gg.removeWidget(ghost, true); } else if (ghost.parentNode) { ghost.parentNode.removeChild(ghost); }
                } else {
                    origFace = w.face || "";
                    origGsY = uPositionToGsY(w.u_position, gsH);
                }
            } else {
                origFace = st.origFace;
                origGsY = uPositionToGsY(st.origUPosition, gsH);
                removeTempGhost(idx);
            }

            var target = faceGrids[origFace];
            refreshing = true;
            try {
                if (target && target.grid) {
                    var curGrid = gridForItem(itemEl);
                    if (curGrid && curGrid !== target.grid) {
                        curGrid.removeWidget(itemEl, false);
                        target.grid.makeWidget(itemEl);
                    }
                    target.grid.update(itemEl, { x: 0, y: origGsY, w: 1, h: gsH, noMove: false, locked: false });
                }
            } finally {
                refreshing = false;
            }

            itemEl.classList.remove("nbx-rd-state-move_in", "nbx-rd-state-move_out_ghost");
            itemEl.classList.add("nbx-rd-state-existing");
            applyExistingColor(itemEl);
            itemEl.classList.remove("nbx-rd-dirty");
            markDirty();
        }

        function flagRemove(itemEl, idx, st) {
            st.removed = !st.removed;
            itemEl.classList.toggle("nbx-rd-state-remove", st.removed);
            itemEl.classList.toggle("nbx-rd-dirty", st.removed);
            if (!st.removed) {
                applyExistingColor(itemEl);
            }
            var grid = gridForItem(itemEl);
            if (grid) {
                grid.update(itemEl, { noMove: st.removed, locked: st.removed });
            }
            markDirty();
        }

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

        function handleRemoveClick(itemEl) {
            var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
            var st = state[idx];
            if (!st) { return; }
            if (st.widget.kind === "add") {
                if (st.widget.placement_id == null) {
                    removeUnsavedAdd(itemEl, idx, st);
                } else {
                    flagCancelAdd(itemEl, idx, st);
                }
                return;
            }
            if (st.widget.device_id == null) { return; }
            if (isMoveTile(itemEl, idx, st)) {
                cancelMove(itemEl, idx, st);
            } else {
                flagRemove(itemEl, idx, st);
            }
        }

        block.addEventListener("click", function (event) {
            var btn = event.target.closest(".nbx-rd-remove-btn");
            if (!btn) { return; }
            if (!block.contains(btn)) { return; }
            event.preventDefault();
            event.stopPropagation();
            var itemEl = btn.closest(".grid-stack-item");
            if (itemEl) {
                handleRemoveClick(itemEl);
            }
        });

        // ---- Build this rack's save payload --------------------------------
        function buildRackPayload() {
            var buckets = { front: [], rear: [], other: [] };
            var seenPlacement = {};

            function pushItem(itemEl, faceKey) {
                if (itemEl.getAttribute("data-rd-temp-ghost")) { return; }
                var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
                var st = state[idx];
                if (!st) { return; }
                var w = st.widget;

                if (w.kind === "move_out_ghost") { return; }
                if (w.opposite_face) { return; }

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
                if (isAdd) {
                    if (w.device_role_id != null) { item.device_role_id = w.device_role_id; }
                    if (w.tenant_id != null) { item.tenant_id = w.tenant_id; }
                }

                if (st.removed && isAdd) {
                    item.kind = "add";
                    item.cancel = true;
                    buckets[faceKey].push(item);
                    return;
                }

                if (st.removed && item.device_id != null) {
                    item.kind = "remove";
                    buckets[faceKey].push(item);
                    return;
                }

                if (faceKey === "other") {
                    item.kind = isAdd ? "add" : "move";
                    buckets.other.push(item);
                    return;
                }

                var node = itemEl.gridstackNode;
                var gsY = (node && node.y != null) ? node.y : parseInt(itemEl.getAttribute("gs-y"), 10);
                var gsH = (node && node.h != null) ? node.h : parseInt(itemEl.getAttribute("gs-h"), 10);
                item.u_position = gsYToUPosition(gsY, gsH);
                item.face = faceKey;

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

        // Highlight a server-reported error tile within THIS rack (matched by
        // device_id). Safe to call for every error from every controller.
        function highlightError(err) {
            block.querySelectorAll(".grid-stack-item").forEach(function (el) {
                var idx = parseInt(el.getAttribute("data-widget-index"), 10);
                var st = state[idx];
                if (!st) { return; }
                if (err.device_id != null && st.widget.device_id === err.device_id) {
                    el.classList.add("nbx-rd-error");
                }
            });
        }

        // ---- Per-rack independent face toggles -----------------------------
        var faceFront = document.getElementById("nbx-rd-face-front-" + rackId);
        var faceRear = document.getElementById("nbx-rd-face-rear-" + rackId);
        var btnFront = block.querySelector("[data-rd-show-front]");
        var btnRear = block.querySelector("[data-rd-show-rear]");

        function faceVisible(faceEl) {
            return !!faceEl && faceEl.style.display !== "none";
        }
        function setFace(faceEl, btnEl, on) {
            if (faceEl) { faceEl.style.display = on ? "" : "none"; }
            if (btnEl) {
                btnEl.classList.toggle("active", on);
                btnEl.setAttribute("aria-pressed", on ? "true" : "false");
            }
        }
        function toggleFace(faceEl, btnEl, otherFaceEl) {
            if (!faceEl) { return; }
            var on = faceVisible(faceEl);
            if (on && !faceVisible(otherFaceEl)) { return; }   // keep one face on
            setFace(faceEl, btnEl, !on);
            syncRackHeight();
        }
        if (btnFront) {
            btnFront.addEventListener("click", function () {
                toggleFace(faceFront, btnFront, faceRear);
            });
        }
        if (btnRear) {
            btnRear.addEventListener("click", function () {
                toggleFace(faceRear, btnRear, faceFront);
            });
        }

        // ---- Palette drops onto THIS rack's faces --------------------------
        // The receiving (front/rear) grid fires `dropped` with the new node. We
        // re-shape it to the device type's U-height, derive u_position + face,
        // and register a synthetic `add` widget in this rack's state[].
        function onPaletteDrop(face, grid, event, previousNode, newNode) {
            var el = newNode && newNode.el;
            if (!el) { return; }
            var dtId = el.getAttribute("data-device-type-id");
            if (dtId == null) { return; }   // a normal tile move, not a palette drop

            var uHeight = parseFloat(el.getAttribute("data-u-height")) || 1;
            var label = el.getAttribute("data-label") || ("Device type " + dtId);
            var gsH = Math.max(1, Math.round(uHeight * 2));

            grid.update(el, { x: 0, w: 1, h: gsH });
            var node = el.gridstackNode || newNode;
            var gsY = (node && node.y != null) ? node.y : 0;
            var uPosition = gsYToUPosition(gsY, gsH);

            // Read the CURRENT shared role/tenant selections (left rail).
            var roleEl = document.getElementById("id_device_role");
            var tenantEl = document.getElementById("id_tenant");
            var roleId = (roleEl && roleEl.value) ? parseInt(roleEl.value, 10) : null;
            var tenantId = (tenantEl && tenantEl.value) ? parseInt(tenantEl.value, 10) : null;
            var roleName = (roleEl && roleEl.value && roleEl.selectedOptions.length)
                ? roleEl.selectedOptions[0].textContent.trim() : "";
            var tenantName = (tenantEl && tenantEl.value && tenantEl.selectedOptions.length)
                ? tenantEl.selectedOptions[0].textContent.trim() : "";

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

            el.setAttribute("data-widget-index", newIdx);
            el.removeAttribute("data-device-type-id");
            el.removeAttribute("data-u-height");
            el.removeAttribute("data-is-full-depth");
            el.removeAttribute("data-label");
            el.classList.remove("nbx-rd-palette-item");
            el.classList.add("nbx-rd-state-add");
            el.querySelectorAll(".nbx-rd-fav-btn").forEach(function (s) { s.remove(); });

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
            content.setAttribute("data-name", label);
            if (roleName) { content.setAttribute("data-role-name", roleName); }
            if (tenantName) { content.setAttribute("data-tenant-name", tenantName); }
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
                    // Reject off-rack palette drops: a brand-new add needs a U.
                    trayGrid.removeWidget(el, true);
                }
            });
        }

        return {
            rackId: rackId,
            buildRackPayload: buildRackPayload,
            highlightError: highlightError,
        };
    }

    // ---- Initialise every visible rack block -------------------------------
    var rackControllers = Array.prototype.map.call(
        root.querySelectorAll(".nbx-rd-rack-block"),
        initRack
    );

    syncRackHeight();
    window.addEventListener("resize", syncRackHeight);

    // Belt-and-suspenders alongside draggable.cancel: stop pointer-down on a ×
    // button or a palette star from reaching GridStack so the click fires. One
    // shared listener covers every rack block AND the palette / quick-access.
    ["pointerdown", "mousedown", "touchstart"].forEach(function (evtName) {
        root.addEventListener(evtName, function (event) {
            if (event.target.closest(".nbx-rd-remove-btn, .nbx-rd-fav-btn")) {
                event.stopPropagation();
            }
        }, true);
    });

    // ---- Save (single design-level POST across all racks) ------------------
    function clearAllErrors() {
        root.querySelectorAll(".grid-stack-item.nbx-rd-error").forEach(function (el) {
            el.classList.remove("nbx-rd-error");
        });
    }

    function doSave() {
        clearAllErrors();
        // The save URL encodes the Design pk; SaveLayoutSerializer also accepts
        // it in the body. Build a payload keyed by rack — one slice per rack.
        var m = saveUrl.match(/designs\/(\d+)\//);
        var payload = {
            design_id: m ? parseInt(m[1], 10) : null,
            racks: rackControllers.map(function (c) { return c.buildRackPayload(); }),
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
                    window.location.reload();
                });
            } else if (response.status === 304) {
                changesMade = false;
                createToast("info", "No changes", "No changes were detected.");
            } else if (response.status === 403) {
                if (saveButton) { saveButton.removeAttribute("disabled"); }
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
                        rackControllers.forEach(function (c) { c.highlightError(err); });
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

    // ---- Shared device-type catalog palette + drag-in ----------------------
    // A search box lists draggable device types fetched from NetBox's core API;
    // dragging one onto ANY rack's front/rear grid plans a brand-new KIND_ADD
    // placement on that rack (the drop handler is wired per-rack in initRack).
    // No dcim.Device is ever created.
    (function setupPalette() {
        var paletteEl = document.getElementById("nbx-rd-palette");
        var searchEl = document.getElementById("nbx-rd-palette-search");
        var manufEl = document.getElementById("id_manufacturer");
        var listEl = document.getElementById("nbx-rd-palette-list");
        var quickListEl = document.getElementById("nbx-rd-quick-list");
        var statusEl = document.getElementById("nbx-rd-palette-status");
        if (!paletteEl || !listEl || !searchEl) { return; }
        if (typeof GridStack === "undefined" || !GridStack.setupDragIn) { return; }

        var favoritesUrl = root.getAttribute("data-favorites-url")
            || "/api/plugins/rack-design/favorite-device-types/";
        var favoriteIds = {};   // id (number) -> true  (used as a Set)

        function setStatus(msg) {
            if (statusEl) { statusEl.textContent = msg || ""; }
        }

        function buildPaletteRow(dt) {
            var uHeight = (dt.u_height != null) ? dt.u_height : 1;
            var gsH = Math.max(1, Math.round(uHeight * 2));
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

            var fav = !!favoriteIds[dt.id];
            var star = document.createElement("button");
            star.type = "button";
            star.className = "nbx-rd-fav-btn" + (fav ? " is-fav" : "");
            star.setAttribute("data-device-type-id", String(dt.id));
            star.setAttribute("title", fav ? "Unstar (remove favorite)" : "Star (add favorite)");
            star.setAttribute("aria-label", star.getAttribute("title"));
            star.setAttribute("aria-pressed", fav ? "true" : "false");
            var icon = document.createElement("i");
            icon.className = "mdi " + (fav ? "mdi-star" : "mdi-star-outline");
            star.appendChild(icon);
            li.appendChild(star);
            return li;
        }

        function refreshDragIn() {
            GridStack.setupDragIn(".nbx-rd-palette-item", { appendTo: "body", helper: "clone" });
        }

        function renderResults(results) {
            listEl.innerHTML = "";
            var shown = results || [];
            if (!shown.length) {
                setStatus("No device types found.");
                return;
            }
            setStatus("");
            shown.forEach(function (dt) { listEl.appendChild(buildPaletteRow(dt)); });
            refreshDragIn();
        }

        function renderQuickAccess() {
            if (!quickListEl) { return; }
            var ids = Object.keys(favoriteIds);
            if (!ids.length) {
                quickListEl.innerHTML =
                    '<div class="list-group-item text-muted small nbx-rd-quick-empty">'
                    + "Star a device type to pin it here.</div>";
                return;
            }
            var url = "/api/dcim/device-types/?brief=true&limit=200";
            ids.forEach(function (id) { url += "&id=" + encodeURIComponent(id); });
            fetch(url, {
                credentials: "same-origin",
                headers: { "Accept": "application/json" },
            }).then(function (resp) {
                if (!resp.ok) { throw new Error("HTTP " + resp.status); }
                return resp.json();
            }).then(function (data) {
                var results = (data && data.results) || [];
                quickListEl.innerHTML = "";
                if (!results.length) {
                    quickListEl.innerHTML =
                        '<div class="list-group-item text-muted small nbx-rd-quick-empty">'
                        + "Star a device type to pin it here.</div>";
                    return;
                }
                results.forEach(function (dt) { quickListEl.appendChild(buildPaletteRow(dt)); });
                refreshDragIn();
            }).catch(function () {
                quickListEl.innerHTML =
                    '<div class="list-group-item text-muted small nbx-rd-quick-empty">'
                    + "Could not load favorites.</div>";
            });
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

        function currentKey() {
            return searchEl.value.trim() + "|" + (manufEl ? manufEl.value : "");
        }

        var debounceTimer = null;
        function scheduleFetch() {
            var key = currentKey();
            if (key === lastKey) { return; }
            lastKey = key;
            if (debounceTimer) { window.clearTimeout(debounceTimer); }
            debounceTimer = window.setTimeout(fetchTypes, 300);
        }
        searchEl.addEventListener("input", scheduleFetch);
        if (manufEl) {
            manufEl.addEventListener("change", function () {
                lastKey = currentKey();
                fetchTypes();
            });
        }

        // ---- Favorites (catalog stars) -------------------------------------
        function applyFavState(starBtn, fav) {
            starBtn.classList.toggle("is-fav", fav);
            starBtn.setAttribute("aria-pressed", fav ? "true" : "false");
            starBtn.setAttribute("title", fav ? "Unstar (remove favorite)" : "Star (add favorite)");
            starBtn.setAttribute("aria-label", starBtn.getAttribute("title"));
            var icon = starBtn.querySelector("i");
            if (icon) { icon.className = "mdi " + (fav ? "mdi-star" : "mdi-star-outline"); }
        }

        function syncStarsFor(id, fav) {
            root.querySelectorAll(
                '.nbx-rd-fav-btn[data-device-type-id="' + id + '"]'
            ).forEach(function (btn) { applyFavState(btn, fav); });
        }

        function toggleFavorite(starBtn) {
            var id = parseInt(starBtn.getAttribute("data-device-type-id"), 10);
            if (isNaN(id)) { return; }
            starBtn.disabled = true;
            fetch(favoritesUrl + "toggle/", {
                method: "POST",
                credentials: "same-origin",
                headers: {
                    "Content-Type": "application/json",
                    "X-CSRFToken": getCsrfToken(),
                },
                body: JSON.stringify({ device_type_id: id }),
            }).then(function (resp) {
                if (!resp.ok) { throw new Error("HTTP " + resp.status); }
                return resp.json();
            }).then(function (data) {
                var fav = !!(data && data.favorite);
                if (fav) { favoriteIds[id] = true; } else { delete favoriteIds[id]; }
                syncStarsFor(id, fav);
                renderQuickAccess();
            }).catch(function () {
                /* leave the icon as-is on failure */
            }).then(function () {
                starBtn.disabled = false;
            });
        }

        function onStarClick(event) {
            var starBtn = event.target.closest(".nbx-rd-fav-btn");
            if (!starBtn) { return; }
            event.preventDefault();
            event.stopPropagation();
            toggleFavorite(starBtn);
        }
        listEl.addEventListener("click", onStarClick);
        if (quickListEl) { quickListEl.addEventListener("click", onStarClick); }

        function loadFavoritesThenFetch() {
            fetch(favoritesUrl, {
                credentials: "same-origin",
                headers: { "Accept": "application/json" },
            }).then(function (resp) {
                if (!resp.ok) { throw new Error("HTTP " + resp.status); }
                return resp.json();
            }).then(function (data) {
                favoriteIds = {};
                ((data && data.device_type_ids) || []).forEach(function (id) {
                    favoriteIds[id] = true;
                });
            }).catch(function () {
                favoriteIds = {};
            }).then(function () {
                renderQuickAccess();
                fetchTypes();
            });
        }
        loadFavoritesThenFetch();
    })();

    // ---- Shared device hover card (name / role / tenant) -------------------
    // One body-appended floating card, shown on hover over any device tile in
    // any rack block (and the trays). It reads ONLY data-* attributes stamped on
    // each tile — no network calls.
    (function () {
        var hcard = document.createElement("div");
        hcard.className = "nbx-rd-hovercard";
        hcard.setAttribute("role", "tooltip");
        hcard.style.display = "none";
        document.body.appendChild(hcard);
        var currentContent = null;

        function hideCard() {
            hcard.style.display = "none";
            currentContent = null;
        }

        function fillCard(content) {
            var name = content.getAttribute("data-name");
            var role = content.getAttribute("data-role-name");
            var tenant = content.getAttribute("data-tenant-name");
            if (!name && !role && !tenant) { return false; }
            hcard.textContent = "";
            if (name) {
                var n = document.createElement("div");
                n.className = "nbx-rd-hovercard-name";
                n.textContent = name;
                hcard.appendChild(n);
            }
            [["Role", role], ["Tenant", tenant]].forEach(function (pair) {
                if (!pair[1]) { return; }
                var row = document.createElement("div");
                row.className = "nbx-rd-hovercard-row";
                var key = document.createElement("span");
                key.className = "nbx-rd-hovercard-key";
                key.textContent = pair[0];
                var val = document.createElement("span");
                val.textContent = pair[1];
                row.appendChild(key);
                row.appendChild(val);
                hcard.appendChild(row);
            });
            return true;
        }

        function positionCard(target) {
            var r = target.getBoundingClientRect();
            hcard.style.display = "block";
            var cw = hcard.offsetWidth;
            var ch = hcard.offsetHeight;
            var left = r.right + 8;
            if (left + cw > window.innerWidth - 8) {
                left = r.left - cw - 8;   // flip to the tile's left edge
            }
            if (left < 8) { left = 8; }
            var top = r.top;
            if (top + ch > window.innerHeight - 8) {
                top = window.innerHeight - ch - 8;
            }
            if (top < 8) { top = 8; }
            hcard.style.left = left + "px";
            hcard.style.top = top + "px";
        }

        root.addEventListener("pointerover", function (e) {
            var content = e.target.closest && e.target.closest(".grid-stack-item-content");
            if (!content || content === currentContent) { return; }
            if (!fillCard(content)) { hideCard(); return; }
            currentContent = content;
            positionCard(content);
        });
        root.addEventListener("pointerout", function (e) {
            var content = e.target.closest && e.target.closest(".grid-stack-item-content");
            if (!content) { return; }
            if (e.relatedTarget && content.contains(e.relatedTarget)) { return; }
            hideCard();
        });
        root.addEventListener("pointerdown", hideCard, true);
        window.addEventListener("scroll", hideCard, true);
    })();

    // ---- Shared helpers for sibling modules (editor_panels.js) -------------
    // Expose the proven CSRF + toast helpers so the left-rail panels module can
    // reuse them instead of duplicating the resolution logic.
    window.NbxRdEditor = {
        getCsrfToken: getCsrfToken,
        createToast: createToast,
    };

    // ---- Unsaved-changes guard (design-level) ------------------------------
    window.addEventListener("beforeunload", function (event) {
        if (changesMade) {
            event.preventDefault();
            event.returnValue = "";
            return "";
        }
    });
})();
