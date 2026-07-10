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

    // Set while a controller is re-deriving its purely-visual full-depth opposite
    // hatches: those grid mutations are not user edits, so they must not flip the
    // dirty state or arm the Save button.
    var suppressDirty = false;

    function markDirty() {
        if (suppressDirty) { return; }
        changesMade = true;
        if (saveButton) {
            saveButton.removeAttribute("disabled");
        }
    }

    // device_id -> true for every full-depth real device seen in any rack's
    // payload (a full-depth device emits an opposite_face slot). Shared across
    // controllers so a cross-rack move into another rack still knows the device
    // blocks its opposite face and can render the hatch there. Populated as each
    // rack hydrates.
    var fullDepthDeviceIds = {};

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

    // ---- Phase 3: naming-convention wiring ---------------------------------
    // The editor surfaces the Phase 1-2 naming engine in the UI:
    //   * ADD tiles auto-fill a proposed name from the read-only preview-name
    //     endpoint and let the user override it (their value then wins);
    //   * MOVE tiles open the §4a keep-old / rename dialog;
    //   * both flow their proposed_name through the design-level Save.
    var designTitle = root.getAttribute("data-design-title") || "";
    var previewNameUrl = root.getAttribute("data-preview-name-url") || "";

    // Best-effort 1-based ordinal for a brand-new add's preview name. We seed it
    // from the count of already-saved placements (distinct placement_id across
    // every rack's embedded payload) so sequential drops preview as -N, -N+1, …
    // instead of all colliding on the same ordinal before the first save.
    function countSavedPlacements() {
        var seen = {};
        root.querySelectorAll("[id^='rd-editor-data-']").forEach(function (scriptEl) {
            try {
                JSON.parse(scriptEl.textContent || "[]").forEach(function (w) {
                    if (w && w.placement_id != null) { seen[w.placement_id] = true; }
                });
            } catch (e) { /* ignore a malformed payload */ }
        });
        return Object.keys(seen).length;
    }
    var addOrdinalCounter = countSavedPlacements();
    function nextAddIndex() {
        addOrdinalCounter += 1;
        return addOrdinalCounter;
    }

    // Names already assigned in THIS editor session across every rack --
    // unsaved placements are invisible to the DB, so without this every
    // same-family preview returned the SAME next number (user bug
    // 2026-07-10: two palette adds both named dra4-dcs7010t-46). Collected
    // fresh per request from every rack controller's live state.
    function collectPendingNames() {
        var names = [];
        Object.keys(controllersByRackId).forEach(function (rid) {
            var ctrl = controllersByRackId[rid];
            if (ctrl && typeof ctrl.pendingNames === "function") {
                ctrl.pendingNames().forEach(function (name) {
                    if (name && names.indexOf(name) === -1) { names.push(name); }
                });
            }
        });
        return names;
    }

    // Read-only POST to the preview-name endpoint. Resolves to the response
    // {name, exists_in_site} or null on any failure (the auto-fill is a
    // convenience, never blocking — a failure just leaves the name blank).
    // Every request carries the session's current pending names so the
    // naming engine can count unsaved siblings (see collectPendingNames).
    function previewName(body) {
        if (!previewNameUrl) { return Promise.resolve(null); }
        body = body || {};
        if (!body.pending_names) { body.pending_names = collectPendingNames(); }
        return fetch(previewNameUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify(body),
        }).then(function (resp) {
            if (!resp.ok) { return null; }
            return resp.json();
        }).catch(function () { return null; });
    }

    // §4a move-drop dialog. A lightweight Bootstrap 5.3 modal offering two
    // choices for a device that became a MOVE: keep the old name (the default,
    // which yields "<design title>-<old device name>" — a name-preserving move)
    // or set a new name. Clicking Apply calls onConfirm(name) with the chosen
    // value. Dismissing the dialog any other way (the Cancel button, the × close,
    // Esc, or a backdrop click) ABORTS the move and calls onCancel() — the drag
    // is undone, not silently confirmed. Built fresh each open and removed on hide
    // so no state leaks between drags.
    // Tile label = ASSIGNED name (user ruling 2026-07-10). The visible name
    // is a SEPARATE `.nbx-rd-name-display` span layered over the stable
    // `.nbx-rd-label` identity span (which is deliberately never rewritten --
    // it anchors ghost pairing, the read-model, and the test harnesses).
    // Passing a blank/equal name removes the display span and unhides the
    // identity span again.
    function setTileDisplayName(content, name) {
        if (!content) { return; }
        var identity = content.querySelector(".nbx-rd-label");
        var display = content.querySelector(".nbx-rd-name-display");
        var identityText = identity ? identity.textContent : "";
        if (name && name !== identityText) {
            if (!display) {
                display = document.createElement("span");
                display.className = "nbx-rd-name-display";
                if (identity && identity.nextSibling) {
                    content.insertBefore(display, identity.nextSibling);
                } else {
                    content.appendChild(display);
                }
            }
            display.textContent = name;
            if (identity) { identity.classList.add("nbx-rd-label-hidden"); }
        } else {
            if (display) { display.remove(); }
            if (identity) { identity.classList.remove("nbx-rd-label-hidden"); }
        }
    }

    function showMoveNameDialog(oldName, currentName, onConfirm, onCancel) {
        var keepName = (designTitle ? designTitle + "-" : "") + (oldName || "");

        var overlay = document.createElement("div");
        overlay.className = "modal fade nbx-rd-move-modal";
        overlay.setAttribute("tabindex", "-1");
        overlay.innerHTML =
            '<div class="modal-dialog modal-dialog-centered modal-sm">'
            + '<div class="modal-content">'
            + '<div class="modal-header">'
            + '<h5 class="modal-title">' + "Name this move" + "</h5>"
            + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
            + "</div>"
            + '<div class="modal-body">'
            + '<div class="form-check">'
            + '<input class="form-check-input" type="radio" name="nbx-rd-move-name" id="nbx-rd-move-keep" value="keep" checked>'
            + '<label class="form-check-label" for="nbx-rd-move-keep">Keep the old name</label>'
            + '<div class="form-text"><code></code></div>'
            + "</div>"
            + '<div class="form-check mt-2">'
            + '<input class="form-check-input" type="radio" name="nbx-rd-move-name" id="nbx-rd-move-new" value="new">'
            + '<label class="form-check-label" for="nbx-rd-move-new">Set a new name</label>'
            + "</div>"
            + '<input type="text" class="form-control form-control-sm mt-1 nbx-rd-move-new-input" '
            + 'placeholder="New name" disabled>'
            + '<div class="form-text">Template tokens are dotted model paths, e.g. '
            + "<code>{design.name}</code>, <code>{device.site.name}</code>.</div>"
            + "</div>"
            + '<div class="modal-footer">'
            + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
            + '<button type="button" class="btn btn-sm btn-primary" data-rd-move-apply>Apply</button>'
            + "</div>"
            + "</div></div>";
        document.body.appendChild(overlay);

        // Fill the keep-name preview + the new-name input's starting value.
        overlay.querySelector(".form-text code").textContent = keepName;
        var keepRadio = overlay.querySelector("#nbx-rd-move-keep");
        var newRadio = overlay.querySelector("#nbx-rd-move-new");
        var newInput = overlay.querySelector(".nbx-rd-move-new-input");
        newInput.value = (currentName && currentName !== keepName) ? currentName : "";

        function syncEnabled() {
            newInput.disabled = !newRadio.checked;
            if (newRadio.checked) { newInput.focus(); }
        }
        keepRadio.addEventListener("change", syncEnabled);
        newRadio.addEventListener("change", syncEnabled);
        newInput.addEventListener("focus", function () {
            newRadio.checked = true;
            syncEnabled();
        });

        var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
        var modal = ctor ? new ctor(overlay) : null;
        var decided = false;

        // Bootstrap's hide() SILENTLY bails while the show-fade transition
        // is still running (`_isTransitioning`) -- confirmed live
        // (2026-07-08): an Apply click within ~150ms of the dialog opening
        // ran onConfirm but left the modal on screen FOREVER. Queue the
        // hide until 'shown.bs.modal' has fired, so a fast click can never
        // strand the dialog.
        var shownDone = false, hidePending = false;
        overlay.addEventListener("shown.bs.modal", function () {
            shownDone = true;
            if (hidePending && modal) { modal.hide(); }
        });
        function requestHide() {
            if (!modal) { overlay.remove(); return; }
            if (shownDone) { modal.hide(); } else { hidePending = true; }
        }

        function finishConfirm(name) {
            if (decided) { return; }
            decided = true;
            if (typeof onConfirm === "function") { onConfirm(name); }
        }
        function finishCancel() {
            if (decided) { return; }
            decided = true;
            if (typeof onCancel === "function") { onCancel(); }
        }

        overlay.querySelector("[data-rd-move-apply]").addEventListener("click", function () {
            var chosen = newRadio.checked ? newInput.value.trim() : keepName;
            finishConfirm(chosen || keepName);
            requestHide();
        });
        // Bootstrap sets aria-hidden on the modal as it hides, which triggers a
        // console warning if DOM focus is still inside (e.g. the Apply/Cancel
        // button that was just clicked). Move focus out before that happens,
        // regardless of which dismissal path (Apply, Cancel, ×, Esc, backdrop)
        // triggered the hide.
        overlay.addEventListener("hide.bs.modal", function () {
            if (overlay.contains(document.activeElement)) {
                document.activeElement.blur();
            }
        });
        // Explicit Cancel/× handling (same reasoning as the displace dialog
        // below): call our own finishCancel + transition-safe hide rather
        // than relying on Bootstrap's document-level dismiss delegation,
        // which routes through the same transition-guarded hide() and can
        // strand the dialog on a fast click.
        overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
            btn.addEventListener("click", function () {
                finishCancel();
                requestHide();
            });
        });
        // Any dismissal that wasn't Apply (Cancel button, ×, Esc, backdrop) aborts
        // the move. `decided` is already set if Apply ran, so this is a no-op then.
        overlay.addEventListener("hidden.bs.modal", function () {
            finishCancel();
            overlay.remove();
        });

        if (modal) {
            modal.show();
        } else {
            // No Bootstrap JS: degrade to confirming the default without blocking.
            finishConfirm(keepName);
            overlay.remove();
        }
    }

    // Phase 4 (spec §4.3.4, §8.1): confirmation dialog shown on EVERY
    // displacement -- always strictly AFTER canPlaceAt/tileOverlapsOther has
    // already passed for the drop (never dialog-then-discover-invalid).
    // `displaced` is a list of {label} describing whichever ghost(s)/remove-
    // flagged device(s) occupy the target units. Mirrors showMoveNameDialog's
    // proven shape (fresh overlay per open, removed on hide, a `decided` flag
    // so Apply-then-hidden never double-fires, blur-before-hide to dodge the
    // aria-hidden-while-focused console warning) so both dialogs read/behave
    // the same to a user AND to a test shim driving them via the DOM (no
    // window.confirm -- this project's dialogs are always this Bootstrap
    // modal shape, see showMoveNameDialog above).
    function showDisplaceConfirmDialog(displaced, newLabel, onConfirm, onCancel) {
        var overlay = document.createElement("div");
        overlay.className = "modal fade nbx-rd-displace-modal";
        overlay.setAttribute("tabindex", "-1");
        overlay.innerHTML =
            '<div class="modal-dialog modal-dialog-centered modal-sm">'
            + '<div class="modal-content">'
            + '<div class="modal-header">'
            + '<h5 class="modal-title">Confirm placement</h5>'
            + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
            + "</div>"
            + '<div class="modal-body">'
            + '<p>This slot is occupied by <strong class="nbx-rd-displace-old"></strong> '
            + "(being removed or moved away). Place "
            + '<strong class="nbx-rd-displace-new"></strong> here?</p>'
            + "</div>"
            + '<div class="modal-footer">'
            + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
            + '<button type="button" class="btn btn-sm btn-primary" data-rd-displace-confirm>Place here</button>'
            + "</div>"
            + "</div></div>";
        document.body.appendChild(overlay);

        overlay.querySelector(".nbx-rd-displace-old").textContent =
            displaced.map(function (d) { return d.label; }).join(", ");
        overlay.querySelector(".nbx-rd-displace-new").textContent = newLabel || "";

        var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
        var modal = ctor ? new ctor(overlay) : null;
        var decided = false;

        // Transition-safe hide, same as showMoveNameDialog above: a hide()
        // issued while the show-fade is still running is silently dropped
        // by Bootstrap, stranding the dialog on screen forever.
        var shownDone = false, hidePending = false;
        overlay.addEventListener("shown.bs.modal", function () {
            shownDone = true;
            if (hidePending && modal) { modal.hide(); }
        });
        function requestHide() {
            if (!modal) { overlay.remove(); return; }
            if (shownDone) { modal.hide(); } else { hidePending = true; }
        }

        function finishConfirm() {
            if (decided) { return; }
            decided = true;
            if (typeof onConfirm === "function") { onConfirm(); }
        }
        function finishCancel() {
            if (decided) { return; }
            decided = true;
            if (typeof onCancel === "function") { onCancel(); }
        }

        overlay.querySelector("[data-rd-displace-confirm]").addEventListener("click", function () {
            finishConfirm();
            requestHide();
        });
        // Every `data-bs-dismiss="modal"` control (Cancel, the × close
        // button) explicitly calls finishCancel()+hide() itself, rather than
        // relying on Bootstrap's own document-level dismiss delegation to
        // reach OUR modal instance -- confirmed live that relying on it
        // alone left a dialog open forever with neither onConfirm nor
        // onCancel ever firing. Esc/backdrop dismissal (which never runs a
        // click handler at all) is still covered by the `hidden.bs.modal`
        // fallback below.
        overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
            btn.addEventListener("click", function () {
                finishCancel();
                requestHide();
            });
        });
        overlay.addEventListener("hide.bs.modal", function () {
            if (overlay.contains(document.activeElement)) {
                document.activeElement.blur();
            }
        });
        // Any dismissal that wasn't caught above (Esc, backdrop click) still
        // aborts the displacement. `decided` already guards against a
        // double-fire alongside the explicit handlers above.
        overlay.addEventListener("hidden.bs.modal", function () {
            finishCancel();
            overlay.remove();
        });

        if (modal) {
            modal.show();
        } else {
            // No Bootstrap JS: degrade to confirming without blocking, same
            // fallback showMoveNameDialog uses.
            finishConfirm();
            overlay.remove();
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
            // (×) button, a palette row's favorite (star) button, or an add
            // tile's editable name input — otherwise GridStack captures the
            // pointer and the click/focus never fires.
            draggable: { cancel: ".nbx-rd-remove-btn, .nbx-rd-fav-btn, .nbx-rd-name-input" },
        };
        if (extra) {
            Object.keys(extra).forEach(function (k) { opts[k] = extra[k]; });
        }
        return opts;
    }

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
        // the device ON it. Moves are untouched: their grabRows already
        // preserves the source tile's own U-alignment.
        if (g.palette && g.gsH % 2 === 0) { top -= top % 2; }
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
        rdCursorGesture = {
            el: el, gsH: gsH, isFullDepth: !!isFullDepth,
            grabRows: grabRows, lastHost: null, lastRow: null,
        };
        rdUpdateCursorGesture();
    }

    function rdEndCursorGesture() {
        rdCursorGesture = null;
        rdClearCursorInds();
    }

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
    var controllersByRackId = {};
    var tileInFlight = null;

    // Freeze/thaw every rack's tiles around a drag so a moved or newly-added
    // device can never displace an existing planned tile (see freezeOthers/thaw).
    function freezeAllTiles(exceptEl) {
        // Begin push suppression for the WHOLE gesture this freeze opens (see
        // guardPushDuringGesture above) -- matched 1:1 by thawAllTiles' end
        // below, at every call site that already pairs these two today.
        rdBeginPushSuppression();
        var prev = suppressDirty;
        suppressDirty = true;
        try {
            Object.keys(controllersByRackId).forEach(function (rid) {
                controllersByRackId[rid].freezeOthers(exceptEl);
            });
        } finally {
            suppressDirty = prev;
        }
    }
    function thawAllTiles() {
        var prev = suppressDirty;
        suppressDirty = true;
        try {
            Object.keys(controllersByRackId).forEach(function (rid) {
                controllersByRackId[rid].thaw();
            });
        } finally {
            suppressDirty = prev;
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
                // Phase 3 (spec §2.1/§2.2): the device's OWN opposite-face
                // shadow, owned by this state entry -- null if not full-depth
                // or not currently rendered on any face.
                shadowEl: null,
            };
        });

        // Record which real devices are full-depth (they project an opposite_face
        // slot) so the live opposite-face hatch can follow them anywhere, including
        // a cross-rack move into a rack that never hosted the device.
        widgets.forEach(function (w) {
            if (w.opposite_face && w.device_id != null) {
                fullDepthDeviceIds[w.device_id] = true;
            }
        });

        // Stamp each server-rendered tile with its device identity (user
        // ruling 2026-07-10, ghost<->body hover link): `data-rd-device-id`
        // travels WITH the element through moves/adoptions/re-taggings, so a
        // move_in body and its origin ghost can find each other by pure DOM
        // identity from ANY rack block, with no per-rack closure lookups.
        widgets.forEach(function (w, idx) {
            if (!w || w.device_id == null || w.opposite_face) { return; }
            var el = block.querySelector(
                '.grid-stack-item[data-widget-index="' + idx + '"]');
            if (el && !el.getAttribute("data-rd-derived-opp")) {
                el.setAttribute("data-rd-device-id", w.device_id);
            }
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

        // A foreign REAL-DEVICE tile (existing / move_in) dragged from ANOTHER
        // rack block: the basis of a cross-rack move. Passive tiles are never
        // adopted — move-out ghosts, the full-depth opposite-face hatch, tiles
        // flagged for removal, and temp ghosts all stay put on their own rack.
        function isForeignRealTile(el) {
            if (!el || !el.classList) { return false; }
            if (!el.closest || !el.closest(".nbx-rd-rack-block")) { return false; }
            if (el.closest(".nbx-rd-rack-block") === block) { return false; }
            if (el.getAttribute("data-rd-temp-ghost")) { return false; }
            if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return false; }
            if (el.classList.contains("nbx-rd-opposite")) { return false; }
            if (el.classList.contains("nbx-rd-state-remove")) { return false; }
            return el.classList.contains("nbx-rd-state-existing")
                || el.classList.contains("nbx-rd-state-move_in");
        }

        // Accept drops onto THIS rack's grids: a palette/quick-access drag source
        // (a new add), a tile already inside this rack block (front <-> rear <->
        // tray within-rack move), OR a real-device tile from ANOTHER rack block
        // (a cross-rack move — the backend already reconciles a move whose
        // target_rack differs from the device's rack). The tray ALSO accepts a
        // foreign real tile (spec §9.3 "tray -> tray (cross-rack): reassociate
        // with another rack") -- it flows through the SAME cross-rack adoption
        // hooks (`added`/`removed` below) as a face target, with `face=""`
        // (spec §9.2) resolved generically by `faceOfItem`/`adoptForeignTile`.
        function makeAccept(isTray) {
            return function (el) {
                if (!el) { return false; }
                if (el.getAttribute && el.getAttribute("data-device-type-id") != null) {
                    return true;
                }
                if (el.classList && el.classList.contains("nbx-rd-palette-item")) {
                    return true;
                }
                if (el.closest && el.closest(".nbx-rd-rack-block") === block) {
                    return true;
                }
                return isForeignRealTile(el);
            };
        }

        // Strip the server-rendered full-depth opposite-face hatches from the
        // raw DOM BEFORE GridStack.init() ever parses this host. They reflect
        // only the ORIGINAL layout -- editor.js's own owned-shadow sync
        // (syncOwnedShadows/placeOrMoveShadow) re-derives them live, tracking
        // each full-depth device's CURRENT slot. This must happen pre-init,
        // not just post-init (as the removeWidget cleanup a few lines below
        // still also does, for belt-and-suspenders): GridStack.init()'s OWN
        // initial float/collision pass runs the moment it parses the DOM, so
        // when a pre-existing double-booked layout (spec §7 Phase 3 bug 4c --
        // a real body already sitting on a full-depth device's mirrored
        // rows) puts a real tile and a server hatch on overlapping rows in
        // the INITIAL markup, GridStack silently relocates the REAL tile
        // during init, before ANY of our code (including the post-init
        // removeWidget cleanup) gets a chance to intervene -- confirmed live:
        // the real occupant renders as a phantom "moved from origin" tile
        // with a ghost, purely as an artifact of load order, on every such
        // rack, even before this Phase 3 change existed. A plain DOM removal
        // (no GridStack API involved -- nothing is registered yet) sidesteps
        // it entirely.
        [frontEl, rearEl].forEach(function (host) {
            if (!host) { return; }
            host.querySelectorAll(".grid-stack-item.nbx-rd-opposite").forEach(function (el) {
                el.parentNode.removeChild(el);
            });
        });

        var grids = [];
        var frontGrid = frontEl
            ? GridStack.init(commonOptions({ acceptWidgets: makeAccept(false) }), frontEl) : null;
        var rearGrid = rearEl
            ? GridStack.init(commonOptions({ acceptWidgets: makeAccept(false) }), rearEl) : null;
        // The tray is unbounded vertically; let dropped items float to the top.
        var trayGrid = trayEl
            ? GridStack.init(commonOptions({ float: false, acceptWidgets: makeAccept(true) }), trayEl) : null;

        [frontGrid, rearGrid, trayGrid].forEach(function (g) {
            if (g) { grids.push(g); }
        });

        grids.forEach(function (grid) {
            grid.on("change", markDirty);
            // A palette CLONE's registration (it still carries its
            // data-device-type-id) is not an edit yet -- the add only
            // becomes real in finishAdd (which calls markDirty itself);
            // a discarded drag-in (cursor over illegal rows, occupied
            // target, cancelled displacement dialog) must leave the dirty
            // state -- and the Save button -- untouched (spec §4.1 palette
            // context, ruling 2026-07-08).
            function isUnregisteredClone(n) {
                var cel = n && n.el;
                return !!(cel && cel.getAttribute
                    && cel.getAttribute("data-device-type-id") != null);
            }
            grid.on("added", function (event, items) {
                var all = items || [];
                for (var i = 0; i < all.length; i++) {
                    if (isUnregisteredClone(all[i])) { continue; }
                    markDirty();
                    return;
                }
            });
            grid.on("removed", function (event, items) {
                var all = items || [];
                for (var i = 0; i < all.length; i++) {
                    if (isUnregisteredClone(all[i])) { continue; }
                    markDirty();
                    return;
                }
            });
            grid.on("dropped", function (event, previousNode, newNode) {
                if (isUnregisteredClone(newNode)) { return; }
                markDirty();
            });
        });

        // Lock move_out_ghost / pre-existing remove tiles: they are passive and
        // must never be draggable. Removing the server opposites fires `removed`;
        // suppress dirty so loading the editor never arms Save.
        suppressDirty = true;
        [[frontGrid, frontEl], [rearGrid, rearEl], [trayGrid, trayEl]].forEach(function (pair) {
            var g = pair[0], host = pair[1];
            if (!g || !host) { return; }
            host.querySelectorAll(".nbx-rd-state-remove").forEach(function (el) {
                g.update(el, { noMove: true, noResize: true, locked: true });
            });
            // A move-out ghost is a passive marker of a VACATED slot, not a real
            // occupying tile. Lock it, then detach it from the grid ENGINE
            // (removeWidget with removeDOM=false: the element and its CSS
            // position stay put, only its collision bookkeeping goes away) so it
            // can never make GridStack push a real widget -- or a derived
            // full-depth hatch -- off the exact slot it visually still marks.
            // removeWidget deletes el.gridstackNode as a side effect; restore the
            // (now engine-detached) node object right after so `.locked`/`.y`
            // stay readable exactly as before.
            host.querySelectorAll(".nbx-rd-state-move_out_ghost").forEach(function (el) {
                g.update(el, { noMove: true, noResize: true, locked: true });
                var node = el.gridstackNode;
                g.removeWidget(el, false, false);
                el.gridstackNode = node;
            });
            // Drop the server-rendered full-depth opposite hatches: they reflect
            // only the ORIGINAL layout. The live derive pass (recomputeOpposites)
            // owns them now, tracking each full-depth device's current slot.
            host.querySelectorAll(".grid-stack-item.nbx-rd-opposite").forEach(function (el) {
                g.removeWidget(el, true);
            });
        });
        suppressDirty = false;

        var faceGrids = {
            front: { grid: frontGrid, host: frontEl },
            rear: { grid: rearGrid, host: rearEl },
        };
        // The tray target (spec §9.2: face "" -- no U). A snap-back/cancel
        // whose ORIGIN was the tray must re-home into the tray host, never
        // `faceGrids[""]` (undefined) -- that left the tile physically
        // stranded on whatever face grid the drag/tray-target attempt had
        // moved it to while the classList was force-set back to "existing",
        // only for the next refreshGhosts pass to see the mismatch (curFace
        // "front" != origFace "") and re-flag it "move_in" -- a confirmed
        // live bug (design 6, F08 tray PDU dragged onto an occupied U then
        // Cancel: stuck at move_in/dirty on the face grid, never restored).
        function targetFor(face) {
            return (face === "") ? { grid: trayGrid, host: trayEl } : faceGrids[face];
        }

        // Re-home a detached tile element into `target` ({grid, host}) at a slot.
        // GridStack.makeWidget only adopts an element that already lives inside the
        // grid's DOM container, so when a tile comes from ANOTHER grid (a cross-
        // rack or cross-face snap-back) we must move its DOM node into the target
        // host FIRST — otherwise makeWidget no-ops and the tile is orphaned
        // (invisible). Returns true if it was homed.
        // GridStack's float:true engine tracks each node's `_orig` (x/y) --
        // the position it silently "packs" a node back toward the next time
        // ANY repack pass runs elsewhere on the same grid (see the test
        // shim's `fastSetY` for the fully-worked-out incident this same
        // hazard caused there). The public `update()` API does NOT refresh
        // `_orig` as a side effect of an explicit reposition, so a revert
        // (cancelMove's snap-back, homeInto's re-home) that lands a tile
        // somewhere other than wherever `_orig` still points is only stable
        // until the next unrelated repack (e.g. a sibling's owned-hatch
        // sync) -- confirmed live: a cancelled displaced move settled at the
        // right row, then silently snapped back to the abandoned target
        // moments later. Sync it explicitly after every deliberate reposition
        // outside GridStack's own native drag lifecycle (which keeps `_orig`
        // current on its own).
        function syncNodeOrig(el) {
            var n = el && el.gridstackNode;
            if (n) { n._orig = { x: n.x, y: n.y }; }
        }

        function homeInto(target, itemEl, gsY, gsH) {
            if (!target || !target.grid) { return false; }
            if (target.host && itemEl.parentNode !== target.host) {
                target.host.appendChild(itemEl);
            }
            // A snap-back re-home lands on rows the model has ALREADY vetted
            // (the tile's own origin); the engine's registration-time collision
            // pass (makeWidget -> addNode -> _fixCollisions, plus update's
            // moveNode) must not second-guess it -- an × click or a cancelled
            // dialog runs this OUTSIDE the freeze/thaw gesture bracket, where
            // a cascade would relocate real tiles. Suppress pushes.
            rdBeginPushSuppression();
            try {
                target.grid.makeWidget(itemEl);
                target.grid.update(itemEl, {
                    x: 0, y: gsY, w: 1, h: gsH, noMove: false, locked: false,
                });
                syncNodeOrig(itemEl);
            } finally {
                rdEndPushSuppression();
            }
            return true;
        }

        // ---- Live move visualisation ---------------------------------------
        var tempGhosts = {};         // widget-index -> ghost element
        var refreshing = false;      // re-entrancy guard
        // Phase 3 (spec §2.1/§2.2): shadows/ghost-mirror hatches are OWNED by
        // their device/ghost, not derived by a global scan. `ghostShadows` is
        // keyed by the ORIGIN widget-index of the move-out ghost it mirrors
        // (temp or persistent); a device's own full-depth shadow is tracked on
        // its own state[] entry (`st.shadowEl`) right next to `st.widget`.
        var ghostShadows = {};
        var recomputing = false;     // set while an owned hatch add/removeWidget runs
        // Live mid-drag shadow tracking (spec §2.2): the widget-index/element of
        // whichever tile is currently between dragstart and drop/dragstop on
        // ANY of this rack's three grids, or null when nothing is mid-gesture.
        var curDragIdx = null;
        var curDragEl = null;

        function makeGhostElement(label, deviceId) {
            var item = document.createElement("div");
            item.className = "grid-stack-item nbx-rd-state-move_out_ghost";
            item.setAttribute("data-rd-temp-ghost", "1");
            // Device identity for the ghost<->body hover link (user ruling
            // 2026-07-10) -- same attribute the hydration pass stamps on
            // server-rendered tiles.
            if (deviceId != null) {
                item.setAttribute("data-rd-device-id", deviceId);
            }
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
            // Removing a widget triggers the engine's repack; run it under
            // push suppression so it can never relocate a real tile (Phase 2:
            // this helper also runs OUTSIDE the freeze/thaw gesture bracket,
            // e.g. from the post-drop refresh or an × click).
            rdBeginPushSuppression();
            try {
                var g = (ghost.gridstackNode && ghost.gridstackNode.grid) || null;
                if (g) {
                    g.removeWidget(ghost, true);
                } else if (ghost.parentNode) {
                    ghost.parentNode.removeChild(ghost);
                }
            } finally {
                rdEndPushSuppression();
            }
            delete tempGhosts[idx];
            // Phase 3: the ghost OWNS its opposite-face mirror hatch (if the
            // vacating device is full-depth) -- it goes away with the ghost,
            // in the same call, never left for a later global scan to notice.
            destroyGhostShadow(idx);
        }

        function ensureTempGhost(idx, st) {
            if (tempGhosts[idx]) { return; }
            var face = st.origFace;
            var target = targetFor(face);
            if (!target || !target.grid) { return; }
            var w = st.widget;
            // A tray origin ghost is a list-style entry, no rows (spec §9.3):
            // fixed height, appended after whatever else is already there --
            // never the U-derived gsH/gsY, which are meaningless off-rack.
            var gsH = (face === "") ? 2 : Math.round((w.u_height || 1) * 2);
            var gsY = (face === "") ? trayAppendRow(null) : uPositionToGsY(st.origUPosition, gsH);
            var ghost = makeGhostElement(w.label, w.device_id);
            // addWidget -> Engine.addNode -> _fixCollisions can cascade into
            // real tiles when the origin rows are (legitimately) re-occupied;
            // this helper also runs OUTSIDE the gesture bracket (deferred
            // onTileDeparted, post-drop refresh), so bring its own bracket.
            rdBeginPushSuppression();
            try {
                var added = target.grid.addWidget(ghost, {
                    x: 0, y: gsY, w: 1, h: gsH, noMove: true, noResize: true, locked: true,
                });
                var el = added || ghost;
                target.grid.update(el, { noMove: true, locked: true });
                // Detach from the engine (see the persistent-ghost comment above):
                // a live move-out ghost must mark the vacated origin without ever
                // blocking a real widget or a derived hatch from landing there too.
                // Restore the (now engine-detached) node object so `.locked`/`.y`
                // stay readable exactly as before removeWidget's side effect.
                var ghostNode = el.gridstackNode;
                target.grid.removeWidget(el, false, false);
                el.gridstackNode = ghostNode;
                tempGhosts[idx] = el;
            } finally {
                rdEndPushSuppression();
            }
            // Phase 3: the ghost owns its opposite-face mirror hatch (created
            // in the SAME call that creates the ghost, not by a later scan).
            syncGhostShadow(idx);
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
            // Phase 2: the WHOLE grid-mutation phase of the refresh cycle
            // (temp-ghost add/remove + the recomputeOpposites hatch teardown/
            // re-add below) runs OUTSIDE the freeze/thaw gesture bracket --
            // it is a post-drop setTimeout. Confirmed live: a hatch insertion
            // colliding in-engine here cascaded _fixCollisions -> moveNode
            // relocations across REAL rear tiles (200+ collateral moves on a
            // dense rack). Run the whole phase under push suppression, same
            // counter-safe bracket discipline as freezeAllTiles/thawAllTiles.
            rdBeginPushSuppression();
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

                    // A tray origin (spec §9.2: face "", no U) has no row to
                    // compare -- the tray is an unordered list, so "still at
                    // origin" means only "still in a tray", never a gsY match
                    // (which is otherwise NaN and would always mismatch).
                    var atOrigin = (st.origFace === "")
                        ? (curFace === "")
                        : (curFace === st.origFace) && (curGsY === uPositionToGsY(st.origUPosition, gsH));

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
                syncOwnedShadows();
                // Tray list compaction (spec §9.4 ruling 2026-07-09): close any
                // row holes a departure/ghost-destruction left, still under
                // this settle pass's push-suppression bracket.
                compactTray();
                // Saved displacements (spec §3/§4.3 parity): applied once, on
                // the FIRST settle -- after syncOwnedShadows has created the
                // ghost-mirror hatches the full-depth collapse needs.
                if (!savedDisplacementsApplied) {
                    savedDisplacementsApplied = true;
                    applySavedDisplacements();
                }
            } finally {
                rdEndPushSuppression();
                refreshing = false;
                // Phase 1 read-model (spec §2): an OFF-by-default diagnostic. When a
                // developer sets window.__rdDebugInvariants, re-derive the read-model
                // from the freshly-settled DOM and log any invariant violation. This
                // never drives behaviour and must never throw or break a real refresh.
                if (window.__rdDebugInvariants) {
                    try {
                        rdCheckInvariants(rdBuildModel()).forEach(function (violation) {
                            console.warn("[rd-model] " + violation);
                        });
                    } catch (e) { /* debug hook must never break the editor */ }
                }
            }
        }

        // ---- Owned full-depth opposite-face shadow (Phase 3, spec §2.1/§2.2) ---
        // A full-depth device occupies BOTH faces at its U. The server renders an
        // "opposite_face" blocked hatch on the non-mounted face, but only for the
        // ORIGINAL layout, so a moved (or cross-rack adopted) full-depth device
        // needs a live-updated hatch on its opposite face too -- otherwise a user
        // could double-book that U and hit a save error.
        //
        // Phase 3 inversion: the hatch is no longer produced by a "tear every
        // hatch down, rescan the whole DOM by label, rebuild everything" cycle
        // (the old recomputeOpposites). Each device's shadow is a single OWNED
        // element tracked on its own state[] entry (`st.shadowEl`); each ghost's
        // mirror hatch is OWNED by the ghost (`ghostShadows[originIdx]`). Both are
        // moved in place (placeOrMoveShadow) when their owner moves, and destroyed
        // with their owner -- never destroyed-then-recreated just because some
        // OTHER device on the rack happened to move. `syncOwnedShadows` (the
        // reconciliation entry point refreshGhosts calls after every settled
        // gesture) still visits every live tile once, but only to re-sync each
        // one's OWN shadow/ghost-mirror to its OWN current position -- it never
        // clears the board first.
        var SHADOW_STATE_CLASSES = {
            existing: ["nbx-rd-state-existing"],
            add: ["nbx-rd-state-add", "nbx-rd-opposite-add"],
            move_in: ["nbx-rd-state-move_in", "nbx-rd-opposite-move_in"],
            remove: ["nbx-rd-state-remove", "nbx-rd-opposite-remove", "nbx-rd-opposite-crossed"]
        };
        var SHADOW_ALL_CLASSES = [
            "nbx-rd-state-existing", "nbx-rd-state-add", "nbx-rd-state-move_in",
            "nbx-rd-state-move_out_ghost", "nbx-rd-state-remove",
            "nbx-rd-opposite-add", "nbx-rd-opposite-move_in", "nbx-rd-opposite-remove",
            "nbx-rd-opposite-ghost", "nbx-rd-opposite-crossed", "nbx-rd-opposite-conflict"
        ];

        // Bare skeleton for a fresh owned hatch. State classes/label/owner
        // identity are always applied right after by placeOrMoveShadow, on both
        // a fresh element and a reused (moved) one, so they stay in one place.
        function makeOppositeElement(label) {
            var item = document.createElement("div");
            item.className = "grid-stack-item nbx-rd-opposite";
            item.setAttribute("data-rd-derived-opp", "1");
            var content = document.createElement("div");
            content.className = "grid-stack-item-content";
            var span = document.createElement("span");
            span.className = "nbx-rd-label";
            content.appendChild(span);
            item.appendChild(content);
            return item;
        }

        // Is the [gsY, gsY+gsH) row range on `grid` occupied by a real (live)
        // tile? A hatch (this device's own previous position included -- hatches
        // always carry data-rd-derived-opp) and a vacating ghost never count; only
        // a genuine body counts. Used to flag the "shadow slot occupied by a real
        // device" conflict (spec §7 Phase 3 bug 4c) instead of silently skipping.
        function rangeOccupied(grid, gsY, gsH) {
            var hit = false;
            grid.getGridItems().forEach(function (el) {
                if (hit) { return; }
                if (el.getAttribute("data-rd-derived-opp")) { return; }
                if (el.getAttribute("data-rd-temp-ghost")) { return; }
                // A move-out ghost (temp or persistent) marks a VACATED slot, not
                // an occupying tile -- it must never block a shadow from landing
                // on it (mirrors tileOverlapsOther's same exclusion).
                if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                var n = el.gridstackNode;
                if (!n || n.y == null) { return; }
                var h = n.h || 1;
                if (gsY < n.y + h && n.y < gsY + gsH) { hit = true; }
            });
            return hit;
        }

        // Is this widget full-depth? An EXISTING/moved device is known full-depth
        // via the server-seeded fullDepthDeviceIds map (keyed by device_id). A
        // palette ADD has no device_id yet, so it carries its own is_full_depth flag
        // (read from the palette item's data-is-full-depth in onPaletteDrop).
        function isFullDepthWidget(w) {
            if (!w) { return false; }
            if (w.device_id != null && fullDepthDeviceIds[w.device_id]) { return true; }
            return !!w.is_full_depth;
        }

        // Move `prevEl` (this device's/ghost's OWN previously-owned hatch, or
        // null the first time) to [gsY, gsY+gsH) on `target`, applying `classes`
        // and stamping the owner's identity so the read-model (and any future
        // caller) associates it by reference, never by label+position guessing.
        // Returns the (possibly new, if the face flipped) owned element.
        // Freeze every REAL (non-hatch) tile on `targetGrid` that isn't
        // ALREADY frozen by an outer gesture (freezeAllTiles/freezeOthers),
        // marking it `_rdFrozen` -- the same marker moveNode's guard (see
        // guardPushDuringGesture) refuses to relocate. Returns exactly the
        // elements THIS call froze, so the matching thaw only releases those
        // -- an outer gesture's own freeze (still in progress, e.g. a live
        // mid-drag hatch sync) is left untouched.
        function freezeGridForHatchInsert(targetGrid) {
            var frozenHere = [];
            if (!targetGrid || !targetGrid.el) { return frozenHere; }
            targetGrid.el.querySelectorAll(".grid-stack-item").forEach(function (el) {
                if (el.getAttribute("data-rd-derived-opp")) { return; }
                if (el._rdFrozen) { return; }
                var node = el.gridstackNode;
                if (!node) { return; }
                el._rdFrozen = { locked: !!node.locked, noMove: !!node.noMove };
                targetGrid.update(el, { locked: true, noMove: true });
                frozenHere.push(el);
            });
            return frozenHere;
        }
        function thawFrozenForHatchInsert(frozenHere) {
            frozenHere.forEach(function (el) {
                if (!el._rdFrozen) { return; }
                var prev = el._rdFrozen;
                delete el._rdFrozen;
                var g = (el.gridstackNode && el.gridstackNode.grid) || null;
                if (g) { g.update(el, { locked: prev.locked, noMove: prev.noMove }); }
            });
        }

        function placeOrMoveShadow(prevEl, target, gsY, gsH, label, classes, ownerWidx, ownerRackId) {
            recomputing = true;
            rdBeginPushSuppression();
            // Belt over the _fixCollisions/rdPushSuppressDepth guard: that guard
            // only no-ops _fixCollisions itself, but Engine.addNode's own
            // registration-time collision handling can call moveNode() on the
            // COLLIDING node directly (bypassing _fixCollisions entirely) --
            // confirmed live when a conflict shadow's hatch is deliberately
            // inserted OVER an already-occupied real slot (spec §7 Phase 3 bug
            // 4c): the real occupant got pushed past the hatch's far edge.
            // moveNode's OWN guard only refuses a node marked `_rdFrozen`;
            // this reconciliation pass is never inside a drag gesture, so
            // nothing is frozen unless we do it here -- scoped to exactly the
            // elements THIS call freezes, so a live mid-drag call nested
            // inside an OUTER freeze (onDragStart's freezeAllTiles) never
            // thaws that outer gesture's own freeze early.
            var frozenHere = freezeGridForHatchInsert(target.grid);
            try {
                var el = prevEl;
                var curGrid = el ? ((el.gridstackNode && el.gridstackNode.grid) || null) : null;
                if (el && curGrid === target.grid) {
                    // Same face as before: move it in place -- this IS the
                    // "atomic commit" the spec requires, not a destroy/recreate.
                    target.grid.update(el, { x: 0, y: gsY, w: 1, h: gsH, noMove: true, locked: true });
                    syncNodeOrig(el);
                } else {
                    // No owned element yet, or the owner flipped faces: drop the
                    // stale one (if any) and create fresh on the NEW face.
                    if (el) {
                        if (curGrid) { curGrid.removeWidget(el, true); }
                        else if (el.parentNode) { el.parentNode.removeChild(el); }
                    }
                    el = makeOppositeElement(label);
                    var added = target.grid.addWidget(el, {
                        x: 0, y: gsY, w: 1, h: gsH, noMove: true, noResize: true, locked: true,
                    });
                    el = added || el;
                }
                SHADOW_ALL_CLASSES.forEach(function (c) { el.classList.remove(c); });
                el.classList.add("grid-stack-item", "nbx-rd-opposite");
                classes.forEach(function (c) { el.classList.add(c); });
                el.setAttribute("data-rd-owner-widx", String(ownerWidx));
                el.setAttribute("data-rd-owner-rack", String(ownerRackId));
                var content = el.querySelector(".grid-stack-item-content");
                if (content) {
                    var span = content.querySelector(".nbx-rd-label");
                    if (span) { span.textContent = label || ""; }
                    content.setAttribute("title", (label || "") + " (full-depth: opposite face)");
                }
                return el;
            } finally {
                thawFrozenForHatchInsert(frozenHere);
                rdEndPushSuppression();
                recomputing = false;
            }
        }

        function destroyShadowEl(idx) {
            var st = state[idx];
            if (!st || !st.shadowEl) { return; }
            var el = st.shadowEl;
            recomputing = true;
            rdBeginPushSuppression();
            try {
                var g = (el.gridstackNode && el.gridstackNode.grid) || null;
                if (g) { g.removeWidget(el, true); }
                else if (el.parentNode) { el.parentNode.removeChild(el); }
            } finally {
                rdEndPushSuppression();
                recomputing = false;
            }
            st.shadowEl = null;
        }

        // Classify a body tile's current render state for shadow styling (spec
        // §3): matches the legend vocabulary exactly so the shadow's class
        // always follows its OWNER's state, not a fixed generic style.
        function stateKeyForItem(itemEl, st) {
            if (st.removed) { return "remove"; }
            if (itemEl.classList.contains("nbx-rd-state-add")) { return "add"; }
            if (itemEl.classList.contains("nbx-rd-state-move_in")) { return "move_in"; }
            return "existing";
        }

        // Re-sync ONE device's own shadow to its OWN current body position/state.
        // Called from the settle-pass (syncOwnedShadows) after any gesture AND,
        // live, from the mid-drag "change" listener below (spec §2.2) -- both
        // paths funnel through the SAME function so a mid-drag preview and a
        // post-drop settle render identically.
        function syncDeviceShadow(idx, itemEl) {
            var st = state[idx];
            if (!st) { return; }
            var w = st.widget;
            if (!isFullDepthWidget(w)) { destroyShadowEl(idx); return; }
            var curFace = faceOfItem(itemEl);
            if (curFace !== "front" && curFace !== "rear") { destroyShadowEl(idx); return; }
            var target = faceGrids[curFace === "front" ? "rear" : "front"];
            if (!target || !target.grid) { destroyShadowEl(idx); return; }
            var node = itemEl.gridstackNode;
            var gsY = (node && node.y != null) ? node.y : null;
            if (gsY == null) { destroyShadowEl(idx); return; }
            var gsH = Math.round((w.u_height || 1) * 2);
            var stateKey = stateKeyForItem(itemEl, st);
            var classes = (SHADOW_STATE_CLASSES[stateKey] || []).slice();
            // Spec §7 Phase 3 bug (c): a pre-existing double-booked opposite slot
            // (a real body already sitting on the mirrored rows -- Phase 2's
            // rdCanPlaceAt blocks any NEW placement like this, but a server-
            // loaded layout can already be in this state) must still be VISIBLE,
            // overlapping, red-tinted -- not silently skipped. rdCheckInvariants'
            // I1 overlap check then reports it as the conflict it is.
            if (rangeOccupied(target.grid, gsY, gsH)) {
                classes.push("nbx-rd-opposite-conflict");
            }
            st.shadowEl = placeOrMoveShadow(st.shadowEl, target, gsY, gsH, w.label, classes, idx, rackId);
            // The rear shadow always shows the device's STABLE IDENTITY -- the
            // device-type model for an add, the real name for an existing
            // device -- never the mutable planned-name overlay (user ruling
            // 2026-07-10, revised: the front body reads "what will it be called",
            // the rear hatch reads "what hardware is it"). Previously the shadow
            // was given `w.proposed_name`, which leaked the name onto it
            // inconsistently -- an add whose async preview-name hadn't returned
            // yet showed the type while its already-named siblings showed the
            // name. Force-clear any overlay a prior sync applied so every rear
            // hatch reads uniformly.
            if (st.shadowEl) {
                setTileDisplayName(
                    st.shadowEl.querySelector(".grid-stack-item-content"), "");
            }
        }

        // Re-sync ONE ghost's own mirror hatch (its full-depth device's vacated
        // footprint on the opposite face). Owned by the ghost, keyed by the
        // ghost's ORIGIN widget-index -- never re-matched by label, so a group of
        // simultaneous move-out ghosts can never swap mirror labels (bug 4b).
        function syncGhostShadow(idx) {
            var st = state[idx];
            if (!st) { destroyGhostShadow(idx); return; }
            var w = st.widget;
            if (w.device_id == null || !fullDepthDeviceIds[w.device_id]) { destroyGhostShadow(idx); return; }
            var ghostFace = st.origFace;
            if (ghostFace !== "front" && ghostFace !== "rear") { destroyGhostShadow(idx); return; }
            var target = faceGrids[ghostFace === "front" ? "rear" : "front"];
            if (!target || !target.grid) { destroyGhostShadow(idx); return; }
            var gsH = Math.round((w.u_height || 1) * 2);
            var gsY = uPositionToGsY(st.origUPosition, gsH);
            var classes = ["nbx-rd-state-move_out_ghost", "nbx-rd-opposite-ghost", "nbx-rd-opposite-crossed"];
            ghostShadows[idx] = placeOrMoveShadow(ghostShadows[idx], target, gsY, gsH, w.label, classes, idx, rackId);
        }

        function destroyGhostShadow(idx) {
            var el = ghostShadows[idx];
            if (!el) { return; }
            recomputing = true;
            rdBeginPushSuppression();
            try {
                var g = (el.gridstackNode && el.gridstackNode.grid) || null;
                if (g) { g.removeWidget(el, true); }
                else if (el.parentNode) { el.parentNode.removeChild(el); }
            } finally {
                rdEndPushSuppression();
                recomputing = false;
            }
            delete ghostShadows[idx];
        }

        // The settle-pass reconciliation entry point: visits every currently
        // live body tile and every currently visible move-out ghost ONCE, each
        // re-syncing its OWN owned shadow/mirror to its OWN current position --
        // no teardown of anything that has not actually moved.
        function syncOwnedShadows() {
            var prevSuppress = suppressDirty;
            suppressDirty = true;
            try {
                block.querySelectorAll(".grid-stack-item").forEach(function (itemEl) {
                    if (itemEl.getAttribute("data-rd-temp-ghost")) { return; }
                    if (itemEl.getAttribute("data-rd-derived-opp")) { return; }
                    if (itemEl.classList.contains("nbx-rd-opposite")) { return; }
                    if (itemEl.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                    var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
                    if (isNaN(idx) || !state[idx]) { return; }
                    syncDeviceShadow(idx, itemEl);
                });
                var seenGhostIdx = {};
                Object.keys(tempGhosts).forEach(function (k) {
                    seenGhostIdx[k] = true;
                    syncGhostShadow(parseInt(k, 10));
                });
                block.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (gel) {
                    if (gel.getAttribute("data-rd-temp-ghost")) { return; }
                    var gidx = parseInt(gel.getAttribute("data-widget-index"), 10);
                    if (isNaN(gidx) || seenGhostIdx[gidx]) { return; }
                    seenGhostIdx[gidx] = true;
                    syncGhostShadow(gidx);
                });
            } finally {
                suppressDirty = prevSuppress;
            }
        }

        function scheduleRefresh() {
            if (refreshing) { return; }
            window.setTimeout(refreshGhosts, 0);
        }

        // ---- Phase 4: displacement (spec §2.4, §3, §4.3) --------------------
        // Placing device NEW onto units whose current occupant OLD is
        // vacating (a ghost's origin, or a remove-flagged body) is already
        // ALLOWED by rdCanPlaceAt/tileOverlapsOther (ghosts and remove-state
        // claims never block, spec §4.2). What was missing: telling the user
        // (a confirm dialog, always AFTER validation passed) and rendering
        // OLD as a side reservation stripe instead of silently leaving two
        // tiles stacked with no visual distinction.
        //
        // OLD's identity for the read-model/invariant checks is left exactly
        // as it already was (still a `move_out_ghost`/`remove` tile, still
        // excluded from I1 since neither state is in RD_LIVE_STATES) -- the
        // stripe is a presentation-only flag (`nbx-rd-displaced` class + a
        // `.nbx-rd-stripe` child), never a new lifecycle state, so the read-
        // model needs no change to keep §7 goal 3's invariant checks green.
        //
        // Scans the LIVE DOM directly (not rdBuildModel) because a mid-move
        // TEMP ghost (ensureTempGhost) is deliberately never represented in
        // the Phase 1 read-model (see rdBuildModel's temp-ghost skip comment)
        // -- an in-session, not-yet-saved move must trigger this flow just as
        // much as a server-reloaded persistent ghost does.
        function findDisplacedInFace(faceName, gsY, gsH) {
            var target = faceGrids[faceName];
            var out = [];
            if (!target || !target.host) { return out; }
            var yEnd = gsY + gsH;
            target.host.querySelectorAll(".grid-stack-item").forEach(function (el) {
                // Never an owned hatch/shadow -- only a genuine ghost or
                // remove-flagged BODY collapses to a stripe; its own mirror
                // hatch (ghostShadows[idx] / st.shadowEl) is collapsed
                // alongside it by displaceOne, by identity, not by re-scan.
                if (el.getAttribute("data-rd-derived-opp")) { return; }
                var isGhost = el.classList.contains("nbx-rd-state-move_out_ghost");
                var isRemove = el.classList.contains("nbx-rd-state-remove");
                if (!isGhost && !isRemove) { return; }
                var node = el.gridstackNode;
                var y = (node && node.y != null) ? node.y : parseInt(el.getAttribute("gs-y"), 10);
                var h = (node && node.h != null) ? node.h : parseInt(el.getAttribute("gs-h"), 10);
                if (isNaN(y) || isNaN(h)) { return; }
                if (!(gsY < y + h && y < yEnd)) { return; }
                var idx = parseInt(el.getAttribute("data-widget-index"), 10);
                // A TEMP ghost (an in-session, not-yet-saved move-out marker,
                // see ensureTempGhost) carries no `data-widget-index` of its
                // own -- only a persistent (server-reloaded) ghost does. Its
                // identity only lives in the `tempGhosts` map, keyed by its
                // origin's widget-index; reverse-lookup it by reference so a
                // temp ghost's full-depth mirror hatch (ghostShadows[idx],
                // keyed the SAME way) can still be found and collapsed too.
                if (isGhost && isNaN(idx)) {
                    Object.keys(tempGhosts).forEach(function (k) {
                        if (tempGhosts[k] === el) { idx = parseInt(k, 10); }
                    });
                }
                out.push({
                    el: el,
                    kind: isGhost ? "ghost" : "remove",
                    label: rdLabelFor(el),
                    widgetIndex: isNaN(idx) ? null : idx,
                });
            });
            return out;
        }

        // Everything device D (targeting `face`/[gsY,gsY+gsH)) would displace:
        // D's own face, plus the mirrored face too when D is full-depth --
        // exactly the same two-face scan rdCanPlaceAt runs (spec §4.2).
        function findDisplaced(face, gsY, gsH, isFullDepth) {
            var out = findDisplacedInFace(face, gsY, gsH);
            if (isFullDepth) {
                out = out.concat(findDisplacedInFace(face === "front" ? "rear" : "front", gsY, gsH));
            }
            return out;
        }

        // Create the OUTSIDE-the-frame stripe bar for one collapsed element
        // (spec §3, user ruling 2026-07-09: NetBox core's reservation-bar
        // look, recoloured red, OUTSIDE the rack frame -- never a sliver
        // inside the occupying tile next to its x button). The bar is
        // absolutely positioned against the face grid's .nbx-rd-grid-wrap
        // anchor; top/height are PERCENTAGES of the grid's row span, so it
        // keeps tracking the displaced rows through any container resize.
        // Carries the owner's nbx-rd-state-* class so the legend filters
        // toggle it exactly like the collapsed tile it stands for, plus
        // data attributes so tests/diagnostics can associate it by identity.
        // The richest metadata source for a displaced device (feeds the
        // hover card): the collapsed element's own content when it carries
        // the stamped device attributes (a remove-flagged real tile, or a
        // server-rendered persistent ghost), else the device's LIVE body
        // tile found by its stable label span (a temp ghost created by
        // makeGhostElement carries only the label -- the real tile, now
        // sitting elsewhere, still has its full data-* set).
        function stripeSourceContent(el, label) {
            var content = el && el.querySelector(".grid-stack-item-content");
            if (content && (content.getAttribute("data-device-type-name")
                    || content.getAttribute("data-role-name")
                    || content.getAttribute("data-tenant-name"))) {
                return content;
            }
            var hit = null;
            document.querySelectorAll(".grid-stack-item").forEach(function (cand) {
                if (hit) { return; }
                if (cand.getAttribute("data-rd-derived-opp")) { return; }
                if (cand.hasAttribute("data-rd-temp-ghost")) { return; }
                if (cand.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                var span = cand.querySelector(".nbx-rd-label");
                if (span && span.textContent === label) {
                    hit = cand.querySelector(".grid-stack-item-content");
                }
            });
            return hit || content;
        }

        function makeStripeBar(el, label, widgetIndex) {
            if (!el) { return null; }
            var host = el.closest(".grid-stack");
            if (!host) { return null; }
            var wrap = host.closest(".nbx-rd-grid-wrap") || host.parentNode;
            if (!wrap) { return null; }
            var node = el.gridstackNode;
            var y = (node && node.y != null) ? node.y : parseInt(el.getAttribute("gs-y"), 10);
            var h = (node && node.h != null) ? node.h : parseInt(el.getAttribute("gs-h"), 10);
            var maxRow = parseInt(host.getAttribute("gs-max-row"), 10);
            if (isNaN(y) || isNaN(h) || isNaN(maxRow) || !maxRow) { return null; }
            var stateClass = el.classList.contains("nbx-rd-state-remove")
                ? "nbx-rd-state-remove" : "nbx-rd-state-move_out_ghost";
            var bar = document.createElement("div");
            bar.className = "nbx-rd-stripe " + stateClass;
            bar.setAttribute("title", "was: " + (label || ""));
            bar.setAttribute("data-rd-stripe-for", label || "");
            bar.setAttribute("data-rd-stripe-face", host.getAttribute("data-face") || "");
            if (widgetIndex != null) {
                bar.setAttribute("data-rd-stripe-owner-widx", widgetIndex);
            }
            // Feed the shared device hover-card (user adjustment 2026-07-09:
            // hovering the bar must answer "what was here" with the same card
            // device tiles show): stamp the displaced device's data-* set.
            bar.setAttribute("data-name", "was: " + (label || ""));
            var src = stripeSourceContent(el, label);
            if (src) {
                ["data-device-type-name", "data-role-name", "data-tenant-name"].forEach(function (attr) {
                    var v = src.getAttribute(attr);
                    if (v) { bar.setAttribute(attr, v); }
                });
            }
            bar.style.top = (y / maxRow * 100) + "%";
            bar.style.height = (h / maxRow * 100) + "%";
            wrap.appendChild(bar);
            return bar;
        }

        // Collapse ONE displaced marker (and its owned opposite-face mirror,
        // if any -- a full-depth ghost's mirror hatch, or a full-depth
        // remove-flagged device's own shadow) to the side stripe bar.
        // `collapseMirror` is NEW's own full-depth-ness (spec §4.3.3): OLD's
        // mirror hatch only collapses too when NEW's placement ALSO reaches
        // the opposite face -- a half-depth NEW landing on OLD's origin
        // never touches OLD's (still fully vacating) rear footprint, so it
        // must stay exactly as it already renders. The bars are OWNED by
        // this displacement record (`d.stripeEls`) -- created here,
        // destroyed only by undisplaceOne, never re-derived by a scan.
        function displaceOne(d, collapseMirror) {
            d.stripeEls = d.stripeEls || [];
            function collapse(el) {
                if (!el) { return; }
                el.classList.add("nbx-rd-displaced");
                var bar = makeStripeBar(el, d.label, d.widgetIndex);
                if (bar) { d.stripeEls.push(bar); }
            }
            collapse(d.el);
            var mirrorEl = null;
            if (collapseMirror && d.widgetIndex != null) {
                mirrorEl = (d.kind === "ghost")
                    ? (ghostShadows[d.widgetIndex] || null)
                    : ((state[d.widgetIndex] && state[d.widgetIndex].shadowEl) || null);
                collapse(mirrorEl);
            }
            d.mirrorEl = mirrorEl;
        }

        function undisplaceOne(d) {
            function restore(el) {
                if (!el) { return; }
                el.classList.remove("nbx-rd-displaced");
            }
            restore(d.el);
            restore(d.mirrorEl);
            (d.stripeEls || []).forEach(function (bar) {
                if (bar && bar.parentNode) { bar.parentNode.removeChild(bar); }
            });
            d.stripeEls = [];
        }

        // Release whatever device `idx` is CURRENTLY displacing (its stripe(s)
        // revert to their normal ghost/remove rendering). Called whenever
        // `idx`'s own placement is about to change or disappear (spec §4.3.5:
        // moving NEW away again, or cancelling it, restores OLD).
        function restoreDisplaced(idx) {
            var st = state[idx];
            if (!st || !st.displaces || !st.displaces.length) { return; }
            st.displaces.forEach(undisplaceOne);
            st.displaces = [];
        }

        // Apply SAVED displacements on load (spec §3/§4.3, parity ruling
        // 2026-07-09): the projection marks a vacating slot displaced (+
        // displaced_by) server-side, and the widget payload carries it here.
        // Without this, a saved displacement rendered OVERLAPPED on every
        // editor load (two full tiles composited) -- the session only ever
        // looked right because the interactive gesture had run displaceOne.
        // Runs ONCE, at the end of the FIRST refreshGhosts settle (so the
        // ghost-mirror hatches -- ghostShadows[idx] -- already exist for the
        // full-depth mirror collapse). Ownership discipline unchanged: this
        // routes through the SAME displaceOne/state[].displaces records the
        // live flow uses, so a later move-NEW-away restores OLD identically.
        var savedDisplacementsApplied = false;
        function applySavedDisplacements() {
            widgets.forEach(function (w, idx) {
                if (!w || !w.displaced || w.opposite_face) { return; }
                if (w.kind !== "move_out_ghost" && w.kind !== "remove") { return; }
                var el = block.querySelector(
                    '.grid-stack-item[data-widget-index="' + idx + '"]');
                if (!el || el.getAttribute("data-rd-derived-opp")) { return; }
                if (el.classList.contains("nbx-rd-displaced")) { return; }
                // NEW: the live occupant the projection named in displaced_by.
                var newIdx = null;
                widgets.forEach(function (cand, ci) {
                    if (newIdx != null || !cand || cand.opposite_face) { return; }
                    if (cand.kind !== "add" && cand.kind !== "move_in") { return; }
                    if (cand.label === w.displaced_by
                            || cand.proposed_name === w.displaced_by) { newIdx = ci; }
                });
                var newSt = (newIdx != null) ? state[newIdx] : null;
                var d = {
                    el: el,
                    kind: (w.kind === "remove") ? "remove" : "ghost",
                    label: w.label,
                    widgetIndex: idx,
                };
                displaceOne(d, isFullDepthWidget(newSt && newSt.widget));
                if (newSt) {
                    newSt.displaces = newSt.displaces || [];
                    newSt.displaces.push(d);
                }
            });
        }

        // §4a: when an EXISTING device tile ends a drag away from its origin slot
        // (a different U, the other face, or the tray) it has become a MOVE — open
        // the keep-old / rename dialog once and store the chosen proposed_name on
        // the widget. Dragging it back to its origin re-arms the prompt. Adds have
        // their own inline name field and never open this dialog. Cross-rack moves
        // are impossible here (drops are scoped to one rack block), so every move
        // is a within-rack unit/face/tray move, all of which prompt.
        function maybePromptMove(itemEl) {
            if (!itemEl || itemEl.getAttribute("data-rd-temp-ghost")) { return; }
            // After a cross-rack drag the source grid's dragstop fires with the
            // element now living in the DESTINATION block — ignore it here; the
            // destination controller prompts for it.
            if (itemEl.closest(".nbx-rd-rack-block") !== block) { return; }
            var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
            var st = state[idx];
            if (!st) { return; }
            var w = st.widget;
            if (w.opposite_face || st.removed) { return; }
            // A palette ADD being RE-dragged after its initial drop runs the
            // same validate-or-revert pipeline as a real device (spec §4.8
            // "same pipeline") -- previously it was exempted here entirely
            // (device_id == null bailed out), so a moved add could silently
            // land on top of a live body/shadow (caught by the cross-rack
            // sweep: I1 "add(body) overlaps subject(shadow)").
            if (w.device_id == null) {
                if (w.kind === "add") { maybeRevertAddMove(itemEl, idx, st); }
                return;
            }
            if (w.kind !== "existing" && w.kind !== "move_in") { return; }

            // Spec §4.1 cursor-governed placement (ruling 2026-07-08): the
            // pointer's rows at release override wherever the engine parked
            // the tile -- BEFORE validation, so validation always judges the
            // cursor's target. A rejection here is the full snap-back.
            if (!enforceCursorPlacement(
                    itemEl, Math.round((w.u_height || 1) * 2),
                    isFullDepthWidget(w), function () {
                        st.moveDialogShown = false;
                        cancelMove(itemEl, idx, st);
                    })) {
                return;
            }

            // Invalid drop onto an occupied slot: never overwrite/hide the device
            // that was there. Snap this tile back to its origin (cross-rack: back
            // to the source rack) and don't prompt a move. The occupant is locked
            // during the drag, so it never moved.
            if (tileOverlapsOther(itemEl)) {
                st.moveDialogShown = false;
                cancelMove(itemEl, idx, st);
                return;
            }

            // The rest of the pipeline (origin/self-return detection, the
            // displacement dialog, the §4a rename prompt) runs for BOTH an
            // EXISTING device becoming a move AND a move_in tile (spec §4.1
            // ruling 2026-07-08: EVERY committed cross-rack move runs the
            // full dialog pipeline -- an adopted move_in used to bail out
            // right here, silently skipping displacement + rename).

            var gsH = Math.round((w.u_height || 1) * 2);
            var node = itemEl.gridstackNode;
            var curFace = faceOfItem(itemEl);
            var curGsY = (node && node.y != null) ? node.y : null;
            // A cross-rack adoption is ALWAYS a move (its origin U/face live in a
            // different rack's coordinates), so never short-circuit on atOrigin.
            // A tray origin (spec §9.2) has no row to compare -- "still at
            // origin" means only "still in a tray" (never a gsY match, which is
            // otherwise NaN and would always mismatch).
            var atOrigin = !st.crossRack && (st.origFace === ""
                ? curFace === ""
                : (curFace === st.origFace) && (curGsY === uPositionToGsY(st.origUPosition, gsH)));

            if (atOrigin) {
                st.moveDialogShown = false;
                // Silent self-return onto this device's OWN origin ghost (spec
                // §4.4/§8.3): no dialog. Nothing about THIS device could still
                // be displacing anything at its own origin either.
                restoreDisplaced(idx);
                return;
            }

            // Phase 4 (spec §4.3.5): whatever this tile was displacing at its
            // PREVIOUS position is stale the instant it lands somewhere else --
            // release it before asking about the NEW target.
            restoreDisplaced(idx);

            function promptRename() {
                // An EXISTING device becoming a move always prompts for a
                // name. A move_in tile prompts iff it still NEEDS naming
                // (st.needsRename -- set by adoptForeignTile on every
                // cross-rack adoption, ruling 2026-07-08: every committed
                // cross-rack move runs the dialog pipeline); an already-
                // decided/reloaded move_in re-dragged within a rack does
                // not re-prompt.
                if (w.kind !== "existing" && !st.needsRename) { return; }
                if (st.moveDialogShown) { return; }
                st.moveDialogShown = true;
                var oldName = w.label || "";
                showMoveNameDialog(oldName, w.proposed_name || "", function (name) {
                    w.proposed_name = name;
                    st.needsRename = false;
                    var content = itemEl.querySelector(".grid-stack-item-content");
                    if (content) {
                        content.setAttribute("data-name", name || oldName);
                        content.setAttribute("title", oldName + " → " + name);
                        // The tile SHOWS the plan's new identity (user
                        // ruling 2026-07-10); the hover card tells the
                        // identity story (new name + the device's real one).
                        setTileDisplayName(content, name);
                        content.setAttribute("data-old-name", oldName);
                    }
                    markDirty();
                }, function () {
                    // Cancel/dismiss => abort: snap the tile back to its origin
                    // slot (cross-rack moves return to their source rack via
                    // cancelMove).
                    st.moveDialogShown = false;
                    cancelMove(itemEl, idx, st);
                });
            }

            // Phase 4 (spec §4.3.4): a confirmation dialog on EVERY displacement,
            // strictly AFTER validation (tileOverlapsOther above) already passed.
            var displaced = (curFace === "front" || curFace === "rear")
                ? findDisplaced(curFace, curGsY, gsH, isFullDepthWidget(w))
                : [];
            if (displaced.length) {
                if (st.moveDialogShown) { return; }
                st.moveDialogShown = true;
                var newLabel = w.proposed_name || w.label || "";
                showDisplaceConfirmDialog(displaced, newLabel, function () {
                    st.moveDialogShown = false;
                    displaced.forEach(function (d) { displaceOne(d, isFullDepthWidget(w)); });
                    st.displaces = displaced;
                    markDirty();
                    promptRename();
                }, function () {
                    st.moveDialogShown = false;
                    cancelMove(itemEl, idx, st);
                });
                return;
            }
            promptRename();
        }

        // Drop-time validation for a RE-dragged palette add (spec §4.8: adds
        // follow the same pipeline as real devices). An add has no server
        // origin -- its "committed" position is simply wherever it last
        // legally sat (st.origUPosition/origFace, updated on every accepted
        // move) -- so a rejected drop snaps back THERE, keeping add styling.
        function maybeRevertAddMove(itemEl, idx, st) {
            var w = st.widget;
            var gsH = Math.round((w.u_height || 1) * 2);
            var node = itemEl.gridstackNode;
            var curFace = faceOfItem(itemEl);
            var curGsY = (node && node.y != null) ? node.y : null;
            if (curFace !== "front" && curFace !== "rear" || curGsY == null) { return; }

            function snapBack() {
                var target = targetFor(st.origFace);
                var origGsY = uPositionToGsY(st.origUPosition, gsH);
                refreshing = true;
                rdBeginPushSuppression();
                try {
                    if (target && target.grid) {
                        var curGrid = gridForItem(itemEl);
                        if (curGrid && curGrid !== target.grid) {
                            curGrid.removeWidget(itemEl, false);
                            homeInto(target, itemEl, origGsY, gsH);
                        } else if (curGrid) {
                            curGrid.update(itemEl, {
                                x: 0, y: origGsY, w: 1, h: gsH, noMove: false, locked: false,
                            });
                            syncNodeOrig(itemEl);
                        }
                    }
                } finally {
                    rdEndPushSuppression();
                    refreshing = false;
                }
                scheduleRefresh();
            }

            function commitHere() {
                st.origFace = curFace;
                st.origUPosition = gsYToUPosition(curGsY, gsH);
                w.face = curFace;
                w.u_position = st.origUPosition;
                markDirty();
            }

            // Spec §4.1 cursor-governed placement: same enforcement as a
            // real device's drop (maybePromptMove) -- the pointer's rows at
            // release win; illegal pointer rows snap the add back.
            if (!enforceCursorPlacement(itemEl, gsH, isFullDepthWidget(w), snapBack)) {
                restoreDisplaced(idx);
                return;
            }
            // Re-read the (possibly cursor-corrected) position.
            node = itemEl.gridstackNode;
            curFace = faceOfItem(itemEl);
            curGsY = (node && node.y != null) ? node.y : null;

            // Tray origin (spec §9.2): no row to compare, only "still in a tray".
            var atOrigin = (st.origFace === "")
                ? (curFace === "")
                : (curFace === st.origFace) && (curGsY === uPositionToGsY(st.origUPosition, gsH));
            if (atOrigin) {
                restoreDisplaced(idx);
                return;
            }
            restoreDisplaced(idx);

            // Reject onto-occupied (same authority as every other drop).
            if (tileOverlapsOther(itemEl)) {
                snapBack();
                return;
            }

            // Displacement dialog when the add lands on a vacating slot
            // (spec §4.3/§4.8) -- after validation passed, like everywhere.
            var displaced = findDisplaced(curFace, curGsY, gsH, isFullDepthWidget(w));
            if (displaced.length) {
                showDisplaceConfirmDialog(displaced, w.proposed_name || w.label || "", function () {
                    displaced.forEach(function (d) { displaceOne(d, isFullDepthWidget(w)); });
                    st.displaces = displaced;
                    commitHere();
                }, function () {
                    snapBack();
                });
                return;
            }
            commitHere();
        }

        function onDragStart(event, el) {
            if (!el) { return; }
            var idx = parseInt(el.getAttribute("data-widget-index"), 10);
            // Live mid-drag shadow tracking (spec §2.2, Phase 3): remember which
            // device is being dragged so every `change` tick during the gesture
            // (see the listener below) can move its OWN shadow in real time,
            // through the exact same syncDeviceShadow used to settle after drop.
            curDragIdx = (!isNaN(idx) && state[idx]) ? idx : null;
            curDragEl = curDragIdx != null ? el : null;
            // Re-arm the §4a prompt: a fresh drag of this tile may produce a move.
            if (state[idx]) { state[idx].moveDialogShown = false; }
            // Phase 4 (spec §4.3.5): this tile is about to move away from
            // wherever it currently sits -- release anything it was displacing
            // there now. If it lands back on the same displaced slot, drop-time
            // re-evaluation (maybePromptMove) recomputes it fresh.
            restoreDisplaced(idx);
            if (tempGhosts[idx]) {
                removeTempGhost(idx);
            }
            // Capture a cross-rack origin descriptor for a real-device tile, so a
            // drop into ANOTHER rack block can adopt it (and this rack can drop an
            // origin ghost). A non-eligible drag clears it. The descriptor records
            // the device's identity and its ORIGINAL (this-rack) U/face.
            var st = state[idx];
            if (st && st.widget && st.widget.device_id != null
                && !st.widget.opposite_face && !st.removed
                && (st.widget.kind === "existing" || st.widget.kind === "move_in")) {
                var w = st.widget;
                // The TRUE origin (home rack + real slot) the device should snap
                // back to on cancel. If this tile is itself a live cross-rack move
                // (crossRack), chain to ITS origin so a multi-hop A->B->C move still
                // remembers A; otherwise THIS rack/slot is the origin.
                var originRackId = st.crossRack ? st.originRackId : rackId;
                var originWidgetIndex = st.crossRack ? st.originWidgetIndex : idx;
                tileInFlight = {
                    sourceRackId: rackId,
                    widgetIndex: idx,
                    device_id: w.device_id,
                    placement_id: (w.placement_id != null) ? w.placement_id : null,
                    device_type_id: (w.device_type_id != null) ? w.device_type_id : null,
                    kind: w.kind,
                    u_height: w.u_height,
                    label: w.label,
                    proposed_name: w.proposed_name || "",
                    origUPosition: st.origUPosition,
                    origFace: st.origFace,
                    originRackId: originRackId,
                    originWidgetIndex: originWidgetIndex,
                };
                // Hint the rack face grids as drop targets while a tile is dragged.
                root.classList.add("nbx-rd-dragging-tile");
            } else {
                tileInFlight = null;
            }
            // Spec §4.1 cursor-governed placement: arm the pointer tracker
            // for this gesture (inert when no real pointer is on the tile,
            // i.e. shim-driven moves).
            if (st && st.widget && !st.widget.opposite_face && !st.removed) {
                rdBeginCursorGesture(
                    el, Math.round((st.widget.u_height || 1) * 2),
                    isFullDepthWidget(st.widget));
            } else {
                rdEndCursorGesture();
            }
            // Detach every other tile from the collision engine (this rack and all
            // others) so this move can't shove an existing planned device AND so
            // GridStack's float+push collision resolution can't recurse to a stack
            // overflow against dozens of neighbours on a dense rack. Re-attached on
            // dragstop (thaw).
            freezeAllTiles(el);
        }
        grids.forEach(function (grid) {
            grid.on("dragstart", onDragStart);
            grid.on("dragstop", function (event, el) {
                // Resolve the drop (snap back if it overlaps an occupied slot)
                // while the other tiles are STILL frozen, then thaw — so GridStack
                // can't push a neighbour in the window before we revert.
                curDragIdx = null;
                curDragEl = null;
                maybePromptMove(el);
                thawAllTiles();
                scheduleRefresh();
            });
            grid.on("dropped", function (event, previousNode, newNode) {
                curDragIdx = null;
                curDragEl = null;
                scheduleRefresh();
                if (newNode && newNode.el) { maybePromptMove(newNode.el); }
            });
            // Cross-rack adoption hooks. GridStack fires `added` (multi-listener)
            // on the DESTINATION grid and `removed` on the SOURCE for a grid-to-
            // grid drag — unlike `dropped` (single-listener, claimed by the
            // palette / tray handlers), so adoption can't ride `dropped`.
            grid.on("added", function (event, items) {
                // Ignore our own derived opposite-face hatch additions.
                if (recomputing) { return; }
                // A foreign real-device tile just landed here = a cross-rack move.
                if (!tileInFlight || tileInFlight.sourceRackId === rackId
                    || tileInFlight.device_id == null) { return; }
                (items || []).forEach(function (node) {
                    var el = node && node.el;
                    if (!el) { return; }
                    if (el.getAttribute("data-device-type-id") != null) { return; }
                    if (el.getAttribute("data-rd-temp-ghost")) { return; }
                    if (el.getAttribute("data-rd-derived-opp")) { return; }
                    if (el.classList.contains("nbx-rd-opposite")) { return; }
                    // Homecoming (spec §4.6) takes priority over an ordinary
                    // adoption -- see homecomingAdopt's header comment.
                    if (homecomingAdopt(el, tileInFlight)) {
                        scheduleRefresh();
                        maybePromptMove(el);
                        return;
                    }
                    // Adopt into THIS rack's state, then fire the §4a move prompt.
                    adoptForeignTile(el);
                    scheduleRefresh();
                    maybePromptMove(el);
                });
            });
            grid.on("removed", function (event, items) {
                // Ignore our own derived opposite-face hatch teardowns.
                if (recomputing) { return; }
                // A real-device tile left THIS rack for another: leave a persistent
                // move-out ghost at its origin and drop its abandoned full-depth
                // opposite-face copy. The guard in onTileDeparted no-ops a plain
                // within-rack (front<->rear<->tray) move where the tile stays.
                if (tileInFlight && tileInFlight.sourceRackId === rackId) {
                    onTileDeparted(tileInFlight);
                }
            });
            grid.on("change", scheduleRefresh);
            // Live mid-drag shadow tracking (spec §2.2): GridStack fires `change`
            // continuously while the pointer moves (a real drag) AND the test
            // shim fires it once between dragstart and dragstop with a candidate
            // position already written to the node (fastSetY / grid.update). Both
            // paths land here; if the tile currently being dragged is a tracked
            // full-depth device, move its OWN shadow to the candidate position
            // RIGHT NOW -- not on the next debounced settle pass -- through the
            // exact same syncDeviceShadow the drop/settle path uses, so the
            // preview and the final render are always the same code, never two
            // slightly different renderings racing each other.
            grid.on("change", function () {
                if (curDragIdx != null && curDragEl && state[curDragIdx]) {
                    syncDeviceShadow(curDragIdx, curDragEl);
                }
            });
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

        // Does itemEl's current slot overlap ANOTHER occupying tile? Used to
        // reject a drop that would land on top of an existing device.
        //
        // Phase 2 (spec §4.1/§4.2): this is now a thin wrapper over
        // rdCanPlaceAt -- the SINGLE authority for "is this target legal",
        // shared by every drop path (same-grid move, cross-face, cross-rack
        // adoption, palette add) via maybePromptMove/onPaletteDrop, which
        // already call this function. rdCanPlaceAt rebuilds the read-model
        // fresh from the (already-moved, per the existing dragstop-time
        // check-then-revert flow) DOM, so it sees exactly what this function
        // used to hand-scan: a live body/shadow blocks, a ghost or a
        // remove-flagged device's body/shadow does NOT (spec §4.2 -- the
        // displacement flow they trigger is Phase 4, not yet built; for now
        // they simply allow), and the device's OWN body/shadow/ghost never
        // blocks itself.
        function tileOverlapsOther(itemEl) {
            var node = itemEl.gridstackNode;
            if (!node || node.y == null) { return false; }
            var curFace = faceOfItem(itemEl);
            if (curFace !== "front" && curFace !== "rear") { return false; }
            var idx = parseInt(itemEl.getAttribute("data-widget-index"), 10);
            var st = state[idx];
            var w = st && st.widget;
            var verdict = rdCanPlaceAt(
                itemEl, rackId, curFace, node.y, node.h || 1, isFullDepthWidget(w));
            return !verdict.ok;
        }

        // Spec §4.1 "Cursor-governed placement" (ruling 2026-07-08), the
        // drop-time enforcement: when the tracked pointer's rows at release
        // disagree with where the engine landed the tile, the POINTER wins.
        //   * pointer inside the landed span on the landed grid -> the
        //     engine followed the cursor; nothing to enforce;
        //   * pointer rows ILLEGAL (the confirmed live fallback bug), or
        //     pointer over a different grid than the engine landed in ->
        //     `onReject()` (the caller's snap-back path) and return false;
        //   * pointer rows legal on the landed grid but the engine parked
        //     the tile elsewhere -> commit at the CURSOR's rows, return true.
        // A gesture without pointer data (test shims) enforces nothing.
        function enforceCursorPlacement(itemEl, gsH, isFullDepth, onReject) {
            var g = rdCursorGesture;
            if (!g || g.el !== itemEl) { return true; }
            var cand = rdCursorCandidate();
            if (!cand) { return true; }   // pointer not over any face grid
            var node = itemEl.gridstackNode;
            if (!node || node.y == null) { return true; }
            var curHost = itemEl.closest(".grid-stack");
            if (g.lastHost === curHost
                    && g.lastRow >= node.y && g.lastRow < node.y + (node.h || gsH)) {
                return true;
            }
            var verdict = rdCanPlaceAt(
                itemEl, cand.rackId, cand.face, cand.top, gsH, isFullDepth);
            if (!verdict.ok || g.lastHost !== curHost) {
                onReject();
                return false;
            }
            // Same grid, legal cursor rows, engine parked the tile elsewhere:
            // reposition to the cursor's rows before validation continues.
            rdBeginPushSuppression();
            try {
                var grid = node.grid || gridForItem(itemEl);
                if (grid) {
                    grid.update(itemEl, { x: 0, y: cand.top, w: 1, h: gsH });
                    syncNodeOrig(itemEl);
                }
            } finally {
                rdEndPushSuppression();
            }
            return true;
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

        // ---- Cross-rack move helpers ---------------------------------------
        // Adopt a real-device tile dragged in from ANOTHER rack block as a
        // cross-rack move_in: register it in THIS rack's state[] carrying the real
        // device identity, with its ORIGIN (source-rack) U/face recorded for an ×
        // snap-back. Re-stamp the tile's widget index and flag it dirty. The §4a
        // dialog (fired by the destination dropped handler) then names the move.
        function adoptForeignTile(el) {
            var d = tileInFlight;
            if (!d) { return; }
            var face = faceOfItem(el);   // 'front' | 'rear' ('' tray never adopts)
            var newIdx = state.length;
            var widget = {
                kind: "move_in",
                device_id: d.device_id,
                device_type_id: d.device_type_id,
                placement_id: d.placement_id,
                u_height: d.u_height,
                label: d.label,
                proposed_name: d.proposed_name || "",
                face: face,
            };
            state.push({
                widget: widget,
                origUPosition: d.origUPosition,
                origFace: d.origFace,
                removed: false,
                shadowEl: null,
                crossRack: true,
                // Spec §4.1 ruling (2026-07-08): EVERY committed cross-rack
                // move runs the full dialog pipeline -- this adoption must
                // open the §4a rename dialog (promptRename honours this
                // flag for a move_in tile; it is cleared once a name is
                // chosen).
                needsRename: true,
                // The device's TRUE origin (chained across hops), not the immediate
                // source rack — so cancelling a multi-hop move returns it home.
                originRackId: d.originRackId,
                originWidgetIndex: d.originWidgetIndex,
            });
            el.setAttribute("data-widget-index", newIdx);
            el.classList.remove("nbx-rd-state-existing", "nbx-rd-state-move_out_ghost");
            el.classList.add("nbx-rd-state-move_in", "nbx-rd-dirty");
            markDirty();
        }

        // Find the widget-index of THIS rack's own move-out ghost for
        // `deviceId` -- temp (in-session) first, then persistent (server-
        // reloaded). A ghost's mere PRESENCE for a device is proof this rack
        // is that device's true origin (a ghost is only ever created here in
        // onTileDeparted for a departing `existing` entry -- i.e. THIS
        // rack's own real home -- or rendered by the server from that same
        // fact), so this is a pure DEVICE-IDENTITY lookup, independent of
        // any in-session hop bookkeeping (tileInFlight/crossRack/
        // originRackId). That independence matters: tileInFlight's chain is
        // lost across a page reload, but a SAVED move still leaves a
        // persistent ghost, and this lookup still finds it by device_id.
        function findOwnGhostEntryIndex(deviceId) {
            if (deviceId == null) { return null; }
            var found = null;
            Object.keys(tempGhosts).forEach(function (k) {
                if (found != null) { return; }
                var gidx = parseInt(k, 10);
                var gst = state[gidx];
                if (gst && gst.widget && gst.widget.device_id === deviceId) { found = gidx; }
            });
            if (found != null) { return found; }
            var hit = null;
            block.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (gel) {
                if (hit != null) { return; }
                if (gel.getAttribute("data-rd-temp-ghost")) { return; }
                var gidx = parseInt(gel.getAttribute("data-widget-index"), 10);
                var gst = state[gidx];
                if (gst && gst.widget && gst.widget.device_id === deviceId) { hit = gidx; }
            });
            return hit;
        }

        // Cross-rack HOMECOMING (spec §4.6, DEVICE-IDENTITY based -- 2026-07-08
        // fix for the confirmed live 2-hop bug). A tile carrying device D is
        // being dropped into THIS rack; if THIS rack already holds D's own
        // move-out ghost (findOwnGhostEntryIndex, above -- proof this rack is
        // D's true origin, works for any number of hops AND survives a page
        // reload, unlike tileInFlight's in-session originRackId chain), this
        // is a homecoming, not an ordinary adoption. Rather than adopt a
        // brand-new state entry (adoptForeignTile) -- which would leave D's
        // real origin entry AND a fresh copy both claiming the device, the
        // root cause of the 2026-07-08 five-entity incident (orphan shadow +
        // stale ghost + duplicate body + two rear mirrors) -- REVIVE the
        // original entry at the drop position: destroy whatever ghost/temp-
        // ghost was marking it vacated, re-tag the dropped element with the
        // ORIGINAL widget-index, and let refreshGhosts/maybePromptMove's
        // EXISTING atOrigin comparison (against ost.origFace/ost.origUPosition,
        // left unchanged here) decide whether this is a full silent restore
        // (dropped exactly back on the ghost, spec §4.4/§4.6) or an ordinary
        // move away from origin (dropped elsewhere in this rack -- the
        // near-miss case: the ghost stays, the tile becomes a normal move of
        // the revived original entry, never a second entity) -- the exact
        // same code path a same-rack move already uses, so there is nothing
        // new to get wrong for either branch, and by construction a rack can
        // never hold two body entities for one device_id. Returns true if
        // this WAS a homecoming (adoptForeignTile must then be skipped).
        function homecomingAdopt(el, d) {
            if (!d || d.device_id == null) { return false; }
            var originIdx = findOwnGhostEntryIndex(d.device_id);
            if (originIdx == null) { return false; }
            var ost = state[originIdx];
            if (!ost || !ost.widget || ost.widget.device_id !== d.device_id) { return false; }
            var face = faceOfItem(el);
            // A tray target (face "", spec §9.2) is a valid homecoming
            // destination too -- e.g. a device dragged tray -> tray back onto
            // its own rack, or units -> tray landing where it originally left
            // from. Only the row/column check below is face-grid-specific.
            if (face !== "front" && face !== "rear" && face !== "") { return false; }
            var node = el.gridstackNode;
            if (face !== "" && (!node || node.y == null)) { return false; }

            // Clear whatever was marking origIdx's slot vacated -- a temp
            // ghost from this session, and/or a persistent (server-reloaded)
            // ghost -- plus its owned mirror hatch either way.
            removeTempGhost(originIdx);
            var staticG = staticGhostFor(ost.widget.placement_id);
            if (staticG) {
                var gg = (staticG.gridstackNode && staticG.gridstackNode.grid) || null;
                if (gg) { gg.removeWidget(staticG, true); }
                else if (staticG.parentNode) { staticG.parentNode.removeChild(staticG); }
            }
            destroyGhostShadow(originIdx);

            el.setAttribute("data-widget-index", originIdx);
            ost.removed = false;
            ost.crossRack = false;
            ost.moveDialogShown = false;
            ost.needsRename = false;
            // Destroy (not null out) any shadow this origin entry still
            // owns -- same orphan-avoidance reasoning as restoreTile: a
            // mid-gesture refresh can have already grown one, and nulling
            // the reference would leave it stranded for a second shadow to
            // pile on top of.
            destroyShadowEl(originIdx);
            el.classList.remove("nbx-rd-state-move_in", "nbx-rd-state-move_out_ghost", "nbx-rd-dirty");
            el.classList.add("nbx-rd-state-existing");
            applyExistingColor(el);
            markDirty();
            return true;
        }

        // A real-device tile left THIS rack for another block (cross-rack move):
        // drop a persistent move-out ghost at its origin slot. GridStack's
        // acceptWidgets flow fires this rack's `removed` BEFORE it relocates the
        // tile's DOM node out of the block, so a synchronous "is it still here?"
        // check misfires (the node is momentarily still present) and no ghost is
        // left. Defer one tick and detect departure by DEVICE IDENTITY: if no live
        // tile in this block carries the device any more, it genuinely left for
        // another rack. A within-rack front<->rear move keeps the device here, so
        // this no-ops and refreshGhosts draws that move's ghost instead.
        function onTileDeparted(d) {
            window.setTimeout(function () {
                var st = state[d.widgetIndex];
                if (!st) { return; }
                var stillHere = false;
                block.querySelectorAll(".grid-stack-item").forEach(function (el) {
                    if (stillHere) { return; }
                    if (el.getAttribute("data-rd-temp-ghost")) { return; }
                    if (el.getAttribute("data-rd-derived-opp")) { return; }
                    if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                    var s = state[parseInt(el.getAttribute("data-widget-index"), 10)];
                    if (s && s.widget && !s.widget.opposite_face
                        && s.widget.device_id === d.device_id) {
                        stillHere = true;
                    }
                });
                if (stillHere) { return; }   // within-rack move: refreshGhosts owns it
                // The device's body genuinely left this rack (for another
                // rack, OR homecomingAdopt just re-tagged the DOM element
                // under a DIFFERENT (origin) widget-index, retiring this
                // entry): destroy THIS rack's now-stale shadow reference
                // regardless of kind -- an un-destroyed shadowEl becomes an
                // orphan the instant its owning body tile is gone from this
                // rack's DOM (2026-07-08 five-entity homecoming incident,
                // entity #1: "orphan shadow ... has no owning device"). Also
                // release anything this entry was still displacing here --
                // that claim is stale once the body leaves (spec §4.3.5).
                destroyShadowEl(d.widgetIndex);
                restoreDisplaced(d.widgetIndex);
                // Only an EXISTING device (this rack is its real home) leaves a
                // move-out ghost. A departing move_in was only transiently here —
                // its true origin ghost already lives in its home rack (or, for a
                // homecoming, this entry is retired outright) — so it must leave
                // nothing behind (otherwise stale ghosts/entries pile up per hop).
                if (st.widget.kind !== "existing") {
                    state[d.widgetIndex] = null;
                    return;
                }
                ensureTempGhost(d.widgetIndex, st);
                markDirty();
            }, 0);
        }

        // Find this rack block's move-out ghost for a placement (reloaded ghost
        // only; temp ghosts are excluded). Exposed for cross-block ×/cancel.
        function findGhost(placementId) {
            return staticGhostFor(placementId);
        }

        // Remove a temp origin ghost this rack created for a departed tile.
        function removeOriginGhost(originIdx) {
            removeTempGhost(originIdx);
        }

        // Snap a LIVE cross-rack move tile back into THIS (its origin) rack: re-home
        // the element at its original U/face, restamp it to its original index, and
        // reset that state entry to a plain existing tile.
        function restoreTile(itemEl, face, uPosition, originIdx, srcWidget) {
            var target = targetFor(face) || faceGrids.front || faceGrids.rear;
            if (!target || !target.grid) { return; }
            // A tray origin (spec §9.2) is a fixed-height list row, appended
            // after whatever else is already there -- never the U-derived
            // gsH/gsY, which are meaningless off-rack.
            var gsH = (face === "") ? 2 : Math.round(((srcWidget && srcWidget.u_height) || 1) * 2);
            var gsY = (face === "") ? trayAppendRow(itemEl) : uPositionToGsY(uPosition, gsH);
            removeTempGhost(originIdx);
            refreshing = true;
            try {
                homeInto(target, itemEl, gsY, gsH);
            } finally {
                refreshing = false;
            }
            itemEl.setAttribute("data-widget-index", originIdx);
            var st = state[originIdx];
            if (st) {
                st.removed = false;
                st.crossRack = false;
                st.moveDialogShown = false;
                st.needsRename = false;
                // DESTROY (not just null out) any shadow element this origin
                // entry still owns: a mid-gesture refresh can have re-created
                // one before this revert runs, and nulling the reference
                // orphans that element -- the next settle then grows a SECOND
                // shadow on top of it (confirmed by the cross-rack sweep:
                // "subject(shadow) overlaps subject(shadow)" I1 violations
                // piling up one per rejected hop).
                destroyShadowEl(originIdx);
            }
            itemEl.classList.remove("nbx-rd-state-move_in", "nbx-rd-state-move_out_ghost", "nbx-rd-dirty");
            itemEl.classList.add("nbx-rd-state-existing");
            applyExistingColor(itemEl);
            markDirty();
            // Settle this (origin) rack: re-grow the restored device's shadow
            // and re-sync every class -- cancelMove's cross-rack caller used
            // to rely on a refresh that never actually ran here (live bug,
            // 2026-07-08: the restored full-depth device came back with NO
            // shadow, or with a stale-tinted one, until the next gesture).
            scheduleRefresh();
        }

        // Snap a RELOADED cross-rack move_in tile back to THIS rack, where its
        // move-out ghost sits: remove the ghost, drop the device tile at the
        // ghost's REAL slot, and repurpose the ghost's state slot as an existing
        // tile so Save deletes the move placement.
        function restoreFromGhost(itemEl, ghostEl) {
            var gidx = parseInt(ghostEl.getAttribute("data-widget-index"), 10);
            var gst = state[gidx];
            if (!gst) { return; }
            var gw = gst.widget;
            var face = gw.face || "";
            var gsH = (face === "") ? 2 : Math.round((gw.u_height || 1) * 2);
            var gsY = (face === "") ? trayAppendRow(itemEl) : uPositionToGsY(gw.u_position, gsH);
            var gg = (ghostEl.gridstackNode && ghostEl.gridstackNode.grid) || null;
            refreshing = true;
            try {
                if (gg) { gg.removeWidget(ghostEl, true); }
                else if (ghostEl.parentNode) { ghostEl.parentNode.removeChild(ghostEl); }
                homeInto(targetFor(face) || faceGrids.front || faceGrids.rear, itemEl, gsY, gsH);
            } finally {
                refreshing = false;
            }
            // The ghost's owned mirror hatch (if any) goes away with it -- this
            // widget-index is being repurposed as a live existing device, not a
            // ghost, so nothing should still call it one.
            destroyGhostShadow(gidx);
            itemEl.setAttribute("data-widget-index", gidx);
            gst.widget = {
                kind: "existing",
                device_id: gw.device_id,
                device_type_id: (gw.device_type_id != null) ? gw.device_type_id : null,
                placement_id: null,
                u_height: gw.u_height,
                u_position: gw.u_position,
                face: face,
                label: gw.label,
            };
            gst.origUPosition = gw.u_position;
            gst.origFace = face;
            gst.removed = false;
            gst.crossRack = false;
            gst.moveDialogShown = false;
            // Same leak guard as restoreTile: destroy any shadow element this
            // entry still owns instead of orphaning it by nulling.
            destroyShadowEl(gidx);
            itemEl.classList.remove("nbx-rd-state-move_in", "nbx-rd-state-move_out_ghost", "nbx-rd-dirty");
            itemEl.classList.add("nbx-rd-state-existing");
            applyExistingColor(itemEl);
            markDirty();
            // Settle this (origin) rack -- same reasoning as restoreTile.
            scheduleRefresh();
        }

        function cancelMove(itemEl, idx, st) {
            // Phase 4 (spec §4.3.5): a full revert releases whatever this tile
            // was displacing -- OLD's ghost/remove rendering comes back.
            restoreDisplaced(idx);
            var w = st.widget;
            var gsH = Math.round((w.u_height || 1) * 2);

            // (a) A LIVE (unsaved) cross-rack move adopted this session: snap the
            // tile back into its ORIGIN rack via that rack's controller.
            if (st.crossRack) {
                var srcCtrl = controllersByRackId[st.originRackId];
                if (!srcCtrl) {
                    // Source rack not on screen (shouldn't happen for a same-design
                    // editor): keep the tile where it is rather than lose it.
                    return;
                }
                var curGridX = gridForItem(itemEl);
                if (curGridX) { curGridX.removeWidget(itemEl, false); }
                // This rack's copy of the device's own shadow goes away with it
                // -- the device (and its shadow) are moving back to their ORIGIN
                // rack, which will grow its own fresh shadow via restoreTile's
                // subsequent scheduleRefresh.
                destroyShadowEl(idx);
                state[idx] = null;
                srcCtrl.removeOriginGhost(st.originWidgetIndex);
                srcCtrl.restoreTile(itemEl, st.origFace, st.origUPosition, st.originWidgetIndex, w);
                markDirty();
                // Settle THIS (destination) rack too (live bug, 2026-07-08):
                // this early return used to skip the trailing scheduleRefresh,
                // so a rejected foreign drop never re-synced the destination's
                // bodies/shadows -- any transient move_in tint picked up
                // mid-gesture stayed on a shadow forever. The source rack is
                // settled by restoreTile's own scheduleRefresh.
                scheduleRefresh();
                return;
            }

            // (b) A RELOADED cross-rack move_in: its move-out ghost lives in a
            // DIFFERENT rack block. Relocate the device tile back onto that rack.
            if (w.kind === "move_in") {
                var hit = findGhostAcrossBlocks(w.placement_id);
                if (hit && hit.controller.rackId !== rackId) {
                    var curGridG = gridForItem(itemEl);
                    if (curGridG) { curGridG.removeWidget(itemEl, false); }
                    destroyShadowEl(idx);
                    state[idx] = null;
                    hit.controller.restoreFromGhost(itemEl, hit.ghostEl);
                    markDirty();
                    // Settle this (departure) rack -- same early-return gap as
                    // the live cross-rack branch above.
                    scheduleRefresh();
                    return;
                }
            }

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

            var target = targetFor(origFace);
            // A tray origin has no meaningful row/height (spec §9.2 -- every
            // tray tile is a fixed gs-h=2 list row, regardless of the
            // device's real U height): append after whatever else is
            // already in the tray instead of the U-derived origGsY/gsH
            // above, which are meaningless off-rack.
            var homeGsH = gsH;
            if (origFace === "") { origGsY = trayAppendRow(itemEl); homeGsH = 2; }
            refreshing = true;
            // The snap-back target is the tile's own origin -- already legal
            // by construction. Suppress engine pushes for the whole revert
            // (an × click runs this outside the freeze/thaw gesture bracket).
            rdBeginPushSuppression();
            try {
                if (target && target.grid) {
                    var curGrid = gridForItem(itemEl);
                    if (curGrid && curGrid !== target.grid) {
                        // Different grid (face<->face or face<->tray): move the
                        // DOM node across via homeInto.
                        curGrid.removeWidget(itemEl, false);
                        homeInto(target, itemEl, origGsY, homeGsH);
                    } else {
                        target.grid.update(itemEl, { x: 0, y: origGsY, w: 1, h: homeGsH, noMove: false, locked: false });
                        syncNodeOrig(itemEl);
                    }
                }
            } finally {
                rdEndPushSuppression();
                refreshing = false;
            }

            itemEl.classList.remove("nbx-rd-state-move_in", "nbx-rd-state-move_out_ghost");
            itemEl.classList.add("nbx-rd-state-existing");
            applyExistingColor(itemEl);
            itemEl.classList.remove("nbx-rd-dirty");
            markDirty();
            // Re-derive ghosts + full-depth opposite hatches now that the tile is
            // back at its origin. cancelMove's own grid.update() fires a `change`
            // while `refreshing` is still true (set above), so that event's
            // scheduleRefresh is dropped by the re-entrancy guard -- leaving a
            // full-depth device's opposite-face shadow stranded at the abandoned
            // drop slot (manifests on the drag-reject path, where the caller's
            // trailing scheduleRefresh is also swallowed). `refreshing` is false
            // again here, so this one runs.
            scheduleRefresh();
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
            // Phase 4 (spec §4.3.5): cancelling this add releases whatever it
            // was displacing.
            restoreDisplaced(idx);
            var grid = gridForItem(itemEl);
            if (grid) {
                grid.removeWidget(itemEl, true);
            } else if (itemEl.parentNode) {
                itemEl.parentNode.removeChild(itemEl);
            }
            // The cancelled add's own shadow (if it was full-depth) is destroyed
            // in the SAME call, not left for a later scan to notice it is gone.
            destroyShadowEl(idx);
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
                // A cancel/remove changes what occupies each face, so re-derive the
                // full-depth opposite-face hatches: otherwise a cancelled full-depth
                // move leaves its hatch orphaned at the abandoned slot, and a removed
                // full-depth device keeps a stale hatch on the opposite face.
                scheduleRefresh();
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
                    // An add always carries its (auto-filled or user-edited) name,
                    // even when blank, so save_layout persists the editor's choice.
                    item.proposed_name = (w.proposed_name != null) ? w.proposed_name : "";
                } else if (w.proposed_name) {
                    // A move that went through the §4a dialog carries its chosen
                    // name. Omitted (no name) => the view leaves the placement's
                    // existing proposed_name untouched, keeping the save idempotent.
                    item.proposed_name = w.proposed_name;
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
                    // Mirrors the front/rear branch below (spec §9.5): send
                    // "existing" for a non-add tray item and let the backend's
                    // own at_real comparison (device.position is None, face
                    // ignored for a tray target) decide whether it is
                    // genuinely untouched or actually a move -- never hardcode
                    // "move" here, or a real, never-touched tray device would
                    // register a spurious move placement on every save.
                    item.kind = isAdd ? "add" : "existing";
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
            if (dtId == null) {
                // Not a palette add: a real device tile was dropped here after
                // crossing GridStack instances -- a within-rack front<->rear face
                // change, or a cross-rack move. GridStack does NOT fire `dragstop`
                // for a cross-grid drag (only `dropped` on the destination), so the
                // convergence dragstop normally performs never runs on this path.
                // That is why such a drag froze every other tile (thawAllTiles was
                // skipped, leaving the whole rack un-draggable until reload) and
                // stranded a full-depth device's opposite-face hatch at its old
                // slot (recomputeOpposites never re-ran). Run that convergence here.
                //
                // A WITHIN-rack move is resolved here: maybePromptMove snaps it back
                // on an occupied slot, else prompts for a rename, while the other
                // tiles are still frozen. A CROSS-rack move is resolved by the
                // `added` handler instead, so for it we only thaw + refresh. Defer
                // the thaw one tick so it lands after any sibling `added` handler
                // has finished resolving a cross-rack adoption.
                // tileInFlight is only set for a REAL device drag; a null
                // here means a within-rack move of a device_id-less tile (a
                // palette add changing face -- adds can never cross racks,
                // the acceptWidgets policy rejects them) -- which must be
                // validated too (maybePromptMove routes adds through
                // maybeRevertAddMove). A FOREIGN real tile (tileInFlight set,
                // different source rack) was already adopted + resolved by
                // the `added` handler -- but that ran BEFORE GridStack wrote
                // the drop's FINAL position (confirmed live, 2026-07-08: an
                // enforceCursorPlacement reposition from the added-pass got
                // silently clobbered back to the engine's placeholder slot).
                // Re-run the pipeline here, where the position IS final; the
                // in-flight guards (moveDialogShown, the block-membership
                // check at maybePromptMove's top for a tile the added-pass
                // already snapped home) make this second pass idempotent on
                // the dialog side.
                maybePromptMove(el);
                window.setTimeout(thawAllTiles, 0);
                scheduleRefresh();
                markDirty();
                return;
            }

            var uHeight = parseFloat(el.getAttribute("data-u-height")) || 1;
            var isFullDepth = el.getAttribute("data-is-full-depth") === "true";
            var label = el.getAttribute("data-label") || ("Device type " + dtId);
            var model = el.getAttribute("data-model") || label;
            var gsH = Math.max(1, Math.round(uHeight * 2));

            // Palette -> tray (spec §9.3): a new off-rack device has no U/face
            // to validate, no shadow, and never displaces anything (a tray is
            // an unordered list, not a grid) -- register it directly with
            // uPosition=null, skipping every row/collision-based check below,
            // which assumes face grid geometry that does not apply here.
            if (face === "") {
                grid.update(el, { x: 0, w: 1, h: 2 });
                uPosition = null;
                finishAdd([]);
                return;
            }

            grid.update(el, { x: 0, w: 1, h: gsH });
            var node = el.gridstackNode || newNode;
            var gsY = (node && node.y != null) ? node.y : 0;
            var uPosition = gsYToUPosition(gsY, gsH);

            // Spec §4.1 cursor-governed placement, PALETTE context (ruling
            // 2026-07-08): the palette gesture armed at pointer-down on the
            // palette item governs where -- and WHETHER -- this add lands.
            // Cursor inside the engine-landed span on the drop grid: normal.
            // Otherwise the CURSOR's rows decide: illegal (or the cursor is
            // over a different grid than the engine dropped into) -> the
            // drag-in is DISCARDED outright, no add, no dialog, no dirty
            // residue; legal -> the add is committed at the cursor's rows.
            var pg = rdCursorGesture;
            if (pg && pg.palette && pg.lastHost && pg.lastRow != null) {
                var dropHost = el.closest(".grid-stack");
                // Cursor governs a palette add UNCONDITIONALLY. The engine's
                // landing (`gsY`) is derived from the drag HELPER, whose offset
                // from the pointer varies with where on the (tall) palette row
                // the user grabbed -- so trusting it made the SAME gesture land
                // on a different (often HALF-)unit drop-to-drop (live bug
                // 2026-07-10: "drop on 23, lands 22/23/23.5"). Place on the unit
                // UNDER THE CURSOR instead (rdCursorCandidate snaps a whole-U add
                // to the U-grid). An earlier `inSpan` shortcut kept the engine's
                // slot whenever the cursor fell inside its span -- exactly the
                // jittery half-unit case -- so it is gone: the cursor decides
                // every time. Illegal rows, or a cursor over a different grid
                // than the drop, DISCARD the drag-in cleanly (no add, no dirty
                // residue); legal -> commit at the cursor's snapped rows.
                var cand = rdCursorCandidate();
                var verdict = cand
                    ? rdCanPlaceAt(el, cand.rackId, cand.face, cand.top, gsH, isFullDepth)
                    : { ok: false };
                if (!verdict.ok || !cand || pg.lastHost !== dropHost) {
                    rdEndCursorGesture();
                    rdBeginPushSuppression();
                    try {
                        grid.removeWidget(el, true);
                    } finally {
                        rdEndPushSuppression();
                    }
                    return;
                }
                rdBeginPushSuppression();
                try {
                    grid.update(el, { x: 0, y: cand.top, w: 1, h: gsH });
                    syncNodeOrig(el);
                } finally {
                    rdEndPushSuppression();
                }
                node = el.gridstackNode || node;
                gsY = cand.top;
                uPosition = gsYToUPosition(gsY, gsH);
                rdEndCursorGesture();
            }

            // Reject an add that lands on an occupied slot: never hide the device
            // that was there. Drop the clone and bail (no placement registered).
            if (tileOverlapsOther(el)) {
                grid.removeWidget(el, true);
                return;
            }

            // Phase 4 (spec §4.3.4, §4.8): a confirmation dialog on EVERY
            // displacement, strictly AFTER validation (tileOverlapsOther) has
            // already passed. finishAdd(...) below (the whole registration
            // this function used to do unconditionally) only runs once the
            // user confirms; a cancel drops the clone exactly like a rejected
            // drop above.
            var displaced = findDisplaced(face, gsY, gsH, isFullDepth);
            if (displaced.length) {
                showDisplaceConfirmDialog(displaced, label, function () {
                    displaced.forEach(function (d) { displaceOne(d, isFullDepth); });
                    finishAdd(displaced);
                }, function () {
                    grid.removeWidget(el, true);
                });
                return;
            }
            finishAdd([]);

            function finishAdd(displacedList) {
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
                    is_full_depth: isFullDepth,
                };
                state.push({
                    widget: widget, origUPosition: uPosition, origFace: face, removed: false, shadowEl: null,
                    displaces: displacedList || [],
                });

                el.setAttribute("data-widget-index", newIdx);
                el.removeAttribute("data-device-type-id");
                el.removeAttribute("data-u-height");
                el.removeAttribute("data-is-full-depth");
                el.removeAttribute("data-label");
                el.removeAttribute("data-model");
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
                if (model) { content.setAttribute("data-device-type-name", model); }
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

                // ---- Editable proposed-name field + collision warning (Phase 3 A) --
                // The name auto-fills from the read-only preview-name endpoint. If the
                // user types into it, their value WINS and a later auto-fill response
                // is ignored. A non-blocking warning badge appears when the name
                // already exists in the design's site.
                widget.proposed_name = "";
                var nameInput = document.createElement("input");
                nameInput.type = "text";
                nameInput.className = "form-control form-control-sm nbx-rd-name-input";
                nameInput.setAttribute("placeholder", "name…");
                nameInput.setAttribute("aria-label", "Proposed name");
                nameInput.setAttribute(
                    "title",
                    "Proposed name. In template mode the tokens are dotted NetBox-model "
                        + "paths, e.g. {design.name}, {device.site.name}."
                );
                var warn = document.createElement("span");
                warn.className = "nbx-rd-name-warning";
                warn.setAttribute("title", "A device with this name already exists in the site.");
                warn.innerHTML = '<i class="mdi mdi-alert" aria-hidden="true"></i>';
                warn.style.display = "none";
                content.appendChild(nameInput);
                content.appendChild(warn);

                function applyWarn(exists) {
                    warn.style.display = exists ? "" : "none";
                    content.classList.toggle("nbx-rd-name-collision", !!exists);
                }
                nameInput.addEventListener("input", function () {
                    widget.nameUserSet = true;
                    widget.proposed_name = nameInput.value;
                    content.setAttribute("data-name", nameInput.value || label);
                    // The tile SHOWS the assigned name, falling back to the
                    // type model while blank (user ruling 2026-07-10).
                    setTileDisplayName(content, nameInput.value);
                    markDirty();
                });

                // Auto-fill the prospective name (best-effort; never blocks the add).
                previewName({
                    kind: "add",
                    device_type: widget.device_type_id,
                    device_role: roleId,
                    tenant: tenantId,
                    target_rack: rackId,
                    target_position: uPosition,
                    target_face: face,
                    index: nextAddIndex(),
                }).then(function (data) {
                    if (!data) { return; }
                    if (!widget.nameUserSet) {
                        nameInput.value = data.name || "";
                        widget.proposed_name = data.name || "";
                        content.setAttribute("data-name", data.name || label);
                        // The tile SHOWS the naming engine's assigned name
                        // the moment it lands (user ruling 2026-07-10).
                        setTileDisplayName(content, data.name || "");
                    }
                    applyWarn(!!data.exists_in_site);
                });

                markDirty();
                // Derive the full-depth opposite-face hatch for this add now. A
                // full-depth ADD occupies both faces just like an existing full-depth
                // device, but nothing else triggers a recompute after the drop (the
                // GridStack `change`/`added` events fire BEFORE this handler creates the
                // state entry, so their scheduled refresh runs against a not-yet-present
                // widget). Schedule one now that the add's widget (with is_full_depth)
                // is in state[].
                scheduleRefresh();
            }   // end finishAdd
        }

        [[frontGrid, "front"], [rearGrid, "rear"]].forEach(function (pair) {
            var g = pair[0], face = pair[1];
            if (!g) { return; }
            g.on("dropped", function (event, previousNode, newNode) {
                onPaletteDrop(face, g, event, previousNode, newNode);
            });
        });
        // The tray is a LIST (spec §9.2/§9.4), not a grid with meaningful
        // rows: whatever row GridStack's own drag math (or a cursor's pixel
        // position) computed for a dropped item is meaningless here and must
        // be OVERWRITTEN to the next free row -- i.e. APPENDED after every
        // item already there -- so drops never overlap an existing tile.
        // Every tray tile carries a fixed gs-h="2" (see the template).
        function trayAppendRow(el) {
            // The next free row is BELOW the bottom-most occupied row -- never
            // a tile count. Counting broke the moment a tile LEFT the tray
            // (bystanders keep their rows per spec §4.1, so the remaining rows
            // are not contiguous): after tile A (rows 0-1) departed, tile B
            // still sat at rows 2-3, and count*2 put A's origin ghost at row 2
            // -- ON TOP of B. The ghost's translucent grey then composited
            // over B's solid role color into what read as "a solid
            // role-colored ghost" (confirmed live, design 6 acceptance).
            var next = 0;
            trayEl.querySelectorAll(".grid-stack-item").forEach(function (o) {
                if (o === el) { return; }
                var n = o.gridstackNode;
                var y = (n && n.y != null) ? n.y : parseInt(o.getAttribute("gs-y"), 10);
                var h = (n && n.h != null) ? n.h : parseInt(o.getAttribute("gs-h"), 10);
                if (isNaN(y)) { y = 0; }
                if (isNaN(h)) { h = 2; }
                if (y + h > next) { next = y + h; }
            });
            return next;
        }

        // Tray COMPACTION (spec §9.4, coordinator ruling 2026-07-09): the
        // tray is a COMPACT list -- after any removal (a tile departing for
        // another grid, a ghost destroyed on homecoming, a cancel-revert) the
        // remaining tiles renumber to contiguous rows 0,2,4,... preserving
        // their current relative order, and the container shrinks back to
        // content height. §4.1's no-bystander-movement rule constrains RACK
        // positions (U), not list reflow, so renumbering tray rows is
        // expressly allowed. Touches ONLY this rack's tray tiles' row
        // attributes -- never face tiles. Runs from refreshGhosts' settle
        // pass (every tray-membership change schedules one) under the
        // caller's push-suppression bracket.
        function compactTray() {
            if (!trayGrid || !trayEl) { return; }
            var items = [];
            trayEl.querySelectorAll(".grid-stack-item").forEach(function (el) {
                var n = el.gridstackNode;
                var y = (n && n.y != null) ? n.y : parseInt(el.getAttribute("gs-y"), 10);
                items.push({ el: el, y: isNaN(y) ? 0 : y });
            });
            items.sort(function (a, b) { return a.y - b.y; });
            var row = 0;
            items.forEach(function (it) {
                if (it.y !== row) {
                    var n = it.el.gridstackNode;
                    if (n && n.grid) {
                        trayGrid.update(it.el, { x: 0, y: row, h: 2 });
                        syncNodeOrig(it.el);
                    } else if (n) {
                        // An engine-DETACHED element (a temp ghost -- see
                        // ensureTempGhost's removeWidget note): update() needs
                        // an attached node, so reposition via the same private
                        // re-render GridStack itself paints nodes with.
                        n.x = 0;
                        n.y = row;
                        n._orig = { x: 0, y: row };
                        trayGrid._writePosAttr(it.el, n);
                    } else {
                        it.el.setAttribute("gs-y", String(row));
                    }
                }
                row += 2;
            });
        }

        if (trayGrid) {
            trayGrid.on("dropped", function (event, previousNode, newNode) {
                var el = newNode && newNode.el;
                if (el) {
                    rdBeginPushSuppression();
                    try {
                        trayGrid.update(el, { x: 0, y: trayAppendRow(el), w: 1, h: 2 });
                        syncNodeOrig(el);
                    } finally {
                        rdEndPushSuppression();
                    }
                }
                // Palette -> tray (spec §9.3, 0.9.0): a new off-rack device is
                // now a legal drop target -- route it through the SAME
                // onPaletteDrop pipeline as a face drop (face="" short-
                // circuits the row/collision-specific logic there). A real
                // device tile dropped here (within-rack or cross-rack move
                // into the tray) also runs through onPaletteDrop's "not a
                // palette add" branch, which resolves via maybePromptMove.
                onPaletteDrop("", trayGrid, event, previousNode, newNode);
            });
        }

        // ---- No-displacement guard -----------------------------------------
        // While ONE tile is being dragged (a move) or a palette tile is dragged in
        // (a new add), lock every OTHER tile so GridStack's float/collision can
        // never shove an existing planned device aside. A drop onto an occupied
        // slot is rejected by GridStack instead of pushing the occupant. The
        // previous lock/noMove flags are restored on thaw so tiles stay draggable.
        function freezeOthers(exceptEl) {
            block.querySelectorAll(".grid-stack-item").forEach(function (el) {
                if (el === exceptEl) { return; }
                if (el._rdFrozen) { return; }
                if (el.getAttribute("data-rd-derived-opp")) { return; }
                var g = (el.gridstackNode && el.gridstackNode.grid) || null;
                if (!g || !el.gridstackNode) { return; }
                el._rdFrozen = {
                    locked: !!el.gridstackNode.locked,
                    noMove: !!el.gridstackNode.noMove,
                };
                g.update(el, { locked: true, noMove: true });
            });
        }
        function thaw() {
            block.querySelectorAll(".grid-stack-item").forEach(function (el) {
                if (!el._rdFrozen) { return; }
                var prev = el._rdFrozen;
                delete el._rdFrozen;
                var g = (el.gridstackNode && el.gridstackNode.grid) || null;
                if (g) { g.update(el, { locked: prev.locked, noMove: prev.noMove }); }
            });
        }

        // Render the initial full-depth opposite-face shadows/ghost-mirrors from
        // the loaded layout (refreshGhosts -> syncOwnedShadows; both self-
        // suppress dirty). This first pass is also what CREATES the owned
        // shadowEl/ghostShadows[] references every later mutation then moves.
        refreshGhosts();

        // Every name assigned in THIS session within this rack (adds' typed/
        // auto-filled names, moves' dialog-chosen names) -- feeds the
        // preview API's pending_names so unsaved siblings never receive the
        // same generated name (user bug 2026-07-10).
        function pendingNames() {
            var names = [];
            state.forEach(function (st) {
                if (!st || !st.widget || st.removed) { return; }
                var name = st.widget.proposed_name;
                if (name) { names.push(name); }
            });
            return names;
        }

        var controller = {
            rackId: rackId,
            buildRackPayload: buildRackPayload,
            highlightError: highlightError,
            freezeOthers: freezeOthers,
            thaw: thaw,
            pendingNames: pendingNames,
            // Gesture-end settle hook (thawAllTiles calls it for every rack).
            scheduleRefresh: scheduleRefresh,
            // Cross-rack move surface used by OTHER rack controllers.
            findGhost: findGhost,
            removeOriginGhost: removeOriginGhost,
            restoreTile: restoreTile,
            restoreFromGhost: restoreFromGhost,
        };
        controllersByRackId[rackId] = controller;
        return controller;
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
            if (event.target.closest(".nbx-rd-remove-btn, .nbx-rd-fav-btn, .nbx-rd-name-input")) {
                event.stopPropagation();
            }
        }, true);
    });

    // No-displacement for NEW adds: freeze every tile while a palette device type
    // is dragged in, so the incoming add lands in free space instead of shoving an
    // existing planned device. Thaw once the pointer releases and GridStack's drop
    // has reconciled (next tick).
    function thawAfterPaletteDrag() {
        document.removeEventListener("pointerup", thawAfterPaletteDrag, true);
        document.removeEventListener("touchend", thawAfterPaletteDrag, true);
        document.removeEventListener("dragend", thawAfterPaletteDrag, true);
        window.setTimeout(thawAllTiles, 0);
    }
    ["pointerdown", "touchstart"].forEach(function (evtName) {
        root.addEventListener(evtName, function (event) {
            var pal = event.target.closest(".nbx-rd-palette-item");
            if (!pal || event.target.closest(".nbx-rd-fav-btn")) { return; }
            freezeAllTiles(null);
            document.addEventListener("pointerup", thawAfterPaletteDrag, true);
            document.addEventListener("touchend", thawAfterPaletteDrag, true);
            document.addEventListener("dragend", thawAfterPaletteDrag, true);
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
            var model = dt.model || dt.display || ("type " + dt.id);
            var label = (manuf ? manuf + " " : "") + model;
            li.setAttribute("data-label", label);
            // The bare device-type model name (no manufacturer) for the hover card.
            li.setAttribute("data-model", model);

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
            // NOTE: not brief — the brief DeviceType serializer omits u_height
            // and is_full_depth, which the tiles need to size correctly.
            var url = "/api/dcim/device-types/?limit=200";
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
            // NOTE: not brief — the brief DeviceType serializer omits u_height
            // and is_full_depth, so a brief row would size every tile at 1U and
            // a multi-U type (e.g. a 2U FX2) would jump to its real height only
            // after Save. Fetching the full serializer keeps drag == saved size.
            var url = "/api/dcim/device-types/?limit=50";
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
            var deviceType = content.getAttribute("data-device-type-name");
            var role = content.getAttribute("data-role-name");
            var tenant = content.getAttribute("data-tenant-name");
            // Identity story for planned changes (user ruling 2026-07-10):
            // the device's real dcim name/tenant + where it went/is going.
            var oldName = content.getAttribute("data-old-name");
            var oldTenant = content.getAttribute("data-old-tenant");
            var newName = content.getAttribute("data-new-name");
            var movedTo = content.getAttribute("data-moved-to");
            if (!name && !deviceType && !role && !tenant) { return false; }
            hcard.textContent = "";
            if (name) {
                var n = document.createElement("div");
                n.className = "nbx-rd-hovercard-name";
                n.textContent = name;
                hcard.appendChild(n);
            }
            [
                ["Was", (oldName && oldName !== name) ? oldName : null],
                ["New name", (newName && newName !== name) ? newName : null],
                ["Type", deviceType],
                ["Role", role],
                ["Tenant", tenant],
                ["Old tenant", (oldTenant && oldTenant !== tenant) ? oldTenant : null],
                ["To", movedTo],
            ].forEach(function (pair) {
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

        // Hover sources: a device tile's content, or a displacement stripe
        // bar (user adjustment 2026-07-09 -- the bar carries the DISPLACED
        // device's data-* set, so the card answers "what was here").
        var HOVER_SOURCE_SELECTOR = ".grid-stack-item-content, .nbx-rd-stripe";

        // ---- Ghost <-> body hover link (user ruling 2026-07-10) ----------
        // Hovering a move_in body highlights its origin ghost, and hovering a
        // ghost highlights the destination body -- same-rack, cross-rack and
        // tray alike (all blocks share this DOM). Identity: the
        // `data-rd-device-id` attribute stamped on every real-device tile at
        // hydration and on every temp ghost at creation; a ghost pairs with
        // the one non-ghost body carrying the same device id (derived
        // hatches are excluded -- they carry no data-rd-device-id).
        var linkedEls = [];
        function clearHoverLink() {
            linkedEls.forEach(function (el) {
                el.classList.remove("nbx-rd-hover-linked");
            });
            linkedEls = [];
        }
        function applyHoverLink(item) {
            clearHoverLink();
                if (!item) { return; }
            var did = item.getAttribute("data-rd-device-id");
            if (!did) { return; }
            var isGhost = item.classList.contains("nbx-rd-state-move_out_ghost");
            root.querySelectorAll(
                '.grid-stack-item[data-rd-device-id="' + did + '"]'
            ).forEach(function (cand) {
                if (cand === item) { return; }
                var candGhost = cand.classList.contains("nbx-rd-state-move_out_ghost");
                if (candGhost === isGhost) { return; }   // link ghost <-> body only
                cand.classList.add("nbx-rd-hover-linked");
                linkedEls.push(cand);
            });
        }

        root.addEventListener("pointerover", function (e) {
            var content = e.target.closest && e.target.closest(HOVER_SOURCE_SELECTOR);
            var item = e.target.closest && e.target.closest(".grid-stack-item");
            applyHoverLink(item);
            if (!content || content === currentContent) { return; }
            if (!fillCard(content)) { hideCard(); return; }
            currentContent = content;
            positionCard(content);
        });
        root.addEventListener("pointerout", function (e) {
            var item = e.target.closest && e.target.closest(".grid-stack-item");
            if (item && !(e.relatedTarget && item.contains(e.relatedTarget))) {
                clearHoverLink();
            }
            var content = e.target.closest && e.target.closest(HOVER_SOURCE_SELECTOR);
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
    }

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

    // ---- Unsaved-changes guard (design-level) ------------------------------
    window.addEventListener("beforeunload", function (event) {
        if (changesMade) {
            event.preventDefault();
            event.returnValue = "";
            return "";
        }
    });
})();
