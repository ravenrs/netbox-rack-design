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
// Submodules are imported by BARE specifier, resolved through the import map the
// editor template emits, so each URL carries the same ?v=<asset_version>
// cache-bust token every other editor asset gets. A relative "./editor/x.js"
// would resolve unversioned, and a stale submodule against a fresh editor.js is
// a broken editor.
import { initHoverCard } from "rd/hovercard.js";
import { getCsrfToken, createToast } from "rd/core.js";
import { showRerunNamingDialog } from "rd/dialogs.js";
import { setupPalette } from "rd/palette.js";
import {
    setPowerHooks,
    postSaveToastKey,
    looksLikePdu,
    showPduPowerDialog,
    showRackPowerDialog,
} from "rd/power.js";
import { isDirtySuppressed } from "rd/dirty.js";
import { syncRackHeight } from "rd/frame.js";
import { controllersByRackId, freezeAllTiles, thawAllTiles } from "rd/registry.js";
import { initRack, setRackHooks } from "rd/rack.js";

(function () {
    "use strict";

    if (typeof GridStack === "undefined") {
        return;
    }

    // The dev-only drag-lifecycle tracer lives in editor/trace.js. It reads
    // only window flags, so it moved with no dependencies at all.

    // GridStack push neutralization lives in editor/push.js.

    var root = document.getElementById("rd-editor");
    if (!root) {
        return;
    }

    // The CHASSIS LAYER renders chassis as degenerate racks (spec §10.3). The
    // editor is otherwise identical, so this single flag carries the difference:
    // which device types the grids accept, and which the palette offers.
    var isChassisLayer = !!root.querySelector('[data-rd-chassis-layer="true"]');

    // ---- Shared context from the template ----------------------------------
    var saveUrl = root.getAttribute("data-save-url");

    // CSRF token. NetBox sets CSRF_COOKIE_HTTPONLY=True, so the cookie is NOT
    // readable from JS — we cannot rely on document.cookie. We resolve it from
    // (in order): the token the template rendered onto #rd-editor via
    // {{ csrf_token }}, NetBox's `netbox_csrf_token` global if present, then the
    // hidden form input. (This mirrors netbox-reorder-rack's proven approach.)
    // getCsrfToken() moved to editor/core.js.

    // ---- Shared dirty state + Save button + toasts -------------------------
    // changesMade is design-level: ANY edit in ANY rack enables the single Save
    // button and arms the beforeunload guard.
    var changesMade = false;
    var saveButton = document.getElementById("rd-editor-save");

    // The suppress-dirty flag lives in editor/dirty.js: it is written from both
    // sides of the initRack boundary, which only works through functions.

    function markDirty() {
        if (isDirtySuppressed()) { return; }
        changesMade = true;
        if (saveButton) {
            saveButton.removeAttribute("disabled");
        }
    }

    // fullDepthDeviceIds (device_id -> true for every full-depth real device in
    // any rack's payload) is imported from editor/model.js. It is still
    // populated here as each rack hydrates -- the module binding is live, so the
    // model sees every write.

    // createToast() moved to editor/core.js.

    // ---- Phase 3: naming-convention wiring ---------------------------------
    // The editor surfaces the Phase 1-2 naming engine in the UI:
    //   * ADD tiles auto-fill a proposed name from the read-only preview-name
    //     endpoint and let the user override it (their value then wins);
    //   * MOVE tiles open the §4a keep-old / rename dialog;
    //   * both flow their proposed_name through the design-level Save.
    // designTitle moved with the dialogs (editor/dialogs.js derives it itself).
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

    // `previewCtx`, when given, is the body the naming engine needs to
    // suggest a name for this move: { device, device_role, tenant,
    // target_rack, target_position, target_face }. Passing null/undefined
    // (or omitting it) disables the auto-fill entirely -- the dialog still
    // works, the user just types a name by hand.
    // showMoveNameDialog(), showDisplaceConfirmDialog(), and
    // showRerunNamingDialog() moved to editor/dialogs.js.

    // Wiring for every "Re-run naming" button the panel rendered (P10):
    // both the grouped-row batch button and an ungrouped row's single-name
    // button carry the SAME `data-rd-rerun-naming` marker and a comma-
    // joined `data-rd-placement-ids`. Preview is a read-only POST; Confirm
    // POSTs the SAME id list to the commit endpoint (recomputed server-side,
    // never trusting this dialog's own copy, P9) and reloads on success so
    // the panel/tiles re-render from the design's new state.
    (function wireRerunNamingButtons() {
        var previewUrl = root.getAttribute("data-rerun-naming-preview-url") || "";
        var commitUrl = root.getAttribute("data-rerun-naming-url") || "";
        if (!previewUrl || !commitUrl) { return; }
        root.querySelectorAll("[data-rd-rerun-naming]").forEach(function (btn) {
            btn.addEventListener("click", function () {
                var idsAttr = btn.getAttribute("data-rd-placement-ids") || "";
                var placementIds = idsAttr.split(",").map(function (s) {
                    return parseInt(s, 10);
                }).filter(function (n) { return !isNaN(n); });
                if (!placementIds.length) { return; }
                btn.disabled = true;
                fetch(previewUrl, {
                    method: "POST",
                    credentials: "same-origin",
                    headers: {
                        "Content-Type": "application/json",
                        "X-CSRFToken": getCsrfToken(),
                    },
                    body: JSON.stringify({ placement_ids: placementIds }),
                }).then(function (resp) {
                    return resp.ok ? resp.json() : null;
                }).then(function (data) {
                    btn.disabled = false;
                    if (!data || !data.lines) {
                        createToast("danger", "Re-run naming", "Could not compute a preview.");
                        return;
                    }
                    showRerunNamingDialog(data.lines, function onConfirm() {
                        fetch(commitUrl, {
                            method: "POST",
                            credentials: "same-origin",
                            headers: {
                                "Content-Type": "application/json",
                                "X-CSRFToken": getCsrfToken(),
                            },
                            body: JSON.stringify({ placement_ids: placementIds }),
                        }).then(function (resp) {
                            if (!resp.ok) {
                                createToast("danger", "Re-run naming", "The rename could not be saved.");
                                return;
                            }
                            window.location.reload();
                        }).catch(function () {
                            createToast("danger", "Re-run naming", "The rename could not be saved.");
                        });
                    });
                }).catch(function () {
                    btn.disabled = false;
                    createToast("danger", "Re-run naming", "Could not compute a preview.");
                });
            });
        });
    })();

    // The planned-PDU / rack-power dialogs live in editor/power.js. They need
    // the dirty flag, which editor.js owns.
    setPowerHooks({ markDirty: markDirty });

    // Rack-height sync and the shared GridStack options live in editor/frame.js.
    // The cross-rack registry and the freeze/thaw bracket live in editor/registry.js.
    // makeFrame() lives in editor/frame.js, initRack() in editor/rack.js.

    // The per-rack controller closes over these five, which stay here: the dirty
    // flag and the naming helpers bound to this page's endpoints. Injected
    // through a setter rather than an initRack parameter -- initRack is handed
    // straight to Array.prototype.map below, which would pass the array INDEX
    // as a second argument.
    setRackHooks({
        markDirty: markDirty,
        setTileDisplayName: setTileDisplayName,
        previewName: previewName,
        nextAddIndex: nextAddIndex,
        root: root,
    });

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
            if (event.target.closest(".nbx-rd-remove-btn, .nbx-rd-fav-btn, .nbx-rd-name-input, .nbx-rd-name-edit-btn")) {
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

    // The save URL encodes the Design pk; SaveLayoutSerializer also accepts it in
    // the body. Build a payload keyed by rack — one slice per rack. Shared by the
    // save flow AND the live per-bank recompute below so both send an identical
    // payload (no second serializer to drift).
    // Translate one CHASSIS COLUMN's rack-shaped payload into the `bays` bucket
    // the save contract expects (spec §10.6).
    //
    // The column is a degenerate rack, so initRack produces ordinary front[]
    // items whose `u_position` is the 1-based BAY INDEX (see the view: one bay ==
    // one whole "U"). Everything else the server needs -- which real bay, or
    // which planned chassis -- is stamped on the block, so the translation is
    // pure lookup and initRack itself stays untouched.
    function buildLayoutPayload() {
        var m = saveUrl ? saveUrl.match(/designs\/(\d+)\//) : null;
        var racks = [];
        // No per-layer branch: every controller returns an already-addressed
        // payload, because its Frame produced the addresses (spec §2).
        rackControllers.forEach(function (c) {
            racks.push(c.buildRackPayload());
        });
        return { design_id: m ? parseInt(m[1], 10) : null, racks: racks };
    }

    // POST the CURRENT (unsaved) layout to the read-only recompute-distribution
    // endpoint, which re-runs the server distribution engine (builtin or a custom
    // distribution_script) over the live layout and returns the fresh per-rack
    // Distribution WITHOUT persisting anything. This is what lets the editor's
    // per-bank chips refresh live on every edit, exactly like the always-live
    // total power bar, using the very same engine as Save. Resolves to
    // {distributions: {"<rackId>": <distribution-or-null>, ...},
    //  power: {"<rackId>": <rack power summary>}} or null on any failure (the
    // caller then keeps the last-known numbers rather than blanking). The power
    // block carries the CAPACITY, which only the server can derive (feed
    // derating/phase maths over real AND planned feeds).
    var recomputeDistUrl = saveUrl
        ? saveUrl.replace(/save-layout\/?$/, "recompute-distribution/") : "";
    // The body of the last recompute we actually got an answer for. An identical
    // layout yields an identical answer, so re-sending one buys nothing but a
    // full server round trip (reconcile every item, run the distribution engine
    // once per rack). The live recompute is driven by DOM mutations and plenty
    // of those leave the LAYOUT untouched -- a settle pass, a class toggle, a
    // re-render -- so this guard is what keeps a single edit to a single
    // request no matter how many times the DOM churns behind it.
    var lastRecomputeBody = null;
    // The same payload split per rack: {rackId: JSON of that rack's bucket}, from
    // the last request that was ANSWERED. Diffing against it is what decides
    // which racks need re-projecting.
    var lastRackBodies = null;
    // ``opts.force``: send even when the layout is byte-identical. For the
    // callers whose ANSWER changes without the layout changing -- defining or
    // copying a planned feed moves the rack's capacity but mutates no tile.
    // Which racks to run the distribution engine over is derived HERE, by
    // diffing this payload against the last answered one rack by rack. The whole
    // layout still travels -- the server reconciles all of it, because a device
    // that left one rack is described by a placement filed under another -- but
    // projecting only the racks whose contents actually changed keeps a round
    // trip proportional to the edit rather than to the design.
    //
    // The payload is the right signal because it IS the question being asked.
    // Deriving the set from DOM mutations instead looked equivalent and was not:
    // GridStack toggles drag-lifecycle classes (ui-draggable-disabled and
    // friends) on tiles in every rack for the duration of a drag, so every drag
    // marked the whole design dirty however carefully the observers were scoped.
    function recomputeDistribution(opts) {
        if (!recomputeDistUrl) { return Promise.resolve(null); }
        var force = !!(opts && opts.force);
        var payload = buildLayoutPayload();
        var rackBodies = {};
        payload.racks.forEach(function (rack) {
            rackBodies[rack.rack_id] = JSON.stringify(rack);
        });
        var body = JSON.stringify(payload);
        if (!force && body === lastRecomputeBody) {
            return Promise.resolve({ unchanged: true });
        }
        // No baseline yet (first recompute of the session) or a forced refresh
        // whose trigger is invisible to the layout: project everything.
        var projectRacks = [];
        if (!force && lastRackBodies) {
            Object.keys(rackBodies).forEach(function (id) {
                if (rackBodies[id] !== lastRackBodies[id]) {
                    projectRacks.push(Number(id));
                }
            });
        }
        if (projectRacks.length) { payload.project_racks = projectRacks; }
        return fetch(recomputeDistUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify(payload),
        }).then(function (response) {
            return response.ok ? response.json() : null;
        }).then(function (data) {
            if (!data || !data.distributions) { return null; }
            // Remembered only once it has been ANSWERED, so a failed request is
            // retried on the next mutation rather than suppressed by the guard,
            // and the racks it would have covered stay in the next diff.
            lastRecomputeBody = body;
            lastRackBodies = rackBodies;
            return {
                distributions: data.distributions,
                distribution_status: data.distribution_status || {},
                power: data.power || {},
            };
        }).catch(function () { return null; });
    }

    // ``redirectTo`` (optional): where to go after a successful save instead of
    // reloading in place -- used by the layer switch so "Save and switch" is one
    // action rather than save, wait, then click again.
    function doSave(redirectTo) {
        clearAllErrors();
        var payload = buildLayoutPayload();

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
                    // A layer switch saves and GOES; everything else reloads in
                    // place, as it always did.
                    if (redirectTo) {
                        window.location.href = redirectTo;
                    } else {
                        // P5: arm the ONE-SHOT post-save toast for the reload
                        // this triggers -- see fireSaveTimeConflictToasts above.
                        // Not armed on the redirectTo branch: that leaves for a
                        // different page entirely (the layer switch), which has
                        // no peer-conflicts payload of its own to read.
                        try { sessionStorage.setItem(postSaveToastKey(), "1"); } catch (e) { /* ignore */ }
                        window.location.reload();
                    }
                });
            } else if (response.status === 304) {
                changesMade = false;
                createToast("info", "No changes", "No changes were detected.");
                if (redirectTo) { window.location.href = redirectTo; }
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
        // Wrapped, not passed directly: the click event would arrive as
        // doSave's `redirectTo` argument and be treated as a URL.
        saveButton.addEventListener("click", function () { doSave(); });
    }

    // The device-type catalog palette now lives in editor/palette.js -- it
    // reads only root and isChassisLayer from this closure.
    setupPalette(root, isChassisLayer);

    // The shared device hover card lives in editor/hovercard.js -- it reads only
    // data-* attributes off the tiles, so `root` was all it needed from here.
    initHoverCard(root);

    // ---- Shared helpers for sibling modules (editor_panels.js) -------------
    // Expose the proven CSRF + toast helpers so the left-rail panels module can
    // reuse them instead of duplicating the resolution logic.
    window.NbxRdEditor = {
        getCsrfToken: getCsrfToken,
        createToast: createToast,
        // Test/debug surface for the PDU + rack power dialogs (docs/pdu-
        // distribution-spec.md): lets an e2e test drive the dialog logic and
        // read back the per-rack save payload without simulating a full
        // GridStack drag.
        rackControllers: rackControllers,
        // Live per-bank distribution: power_heatmap.js calls this (debounced, on
        // the same mutation signal that drives the power bar) to re-run the server
        // distribution engine over the unsaved layout. Read-only, never persists.
        recomputeDistribution: recomputeDistribution,
        looksLikePdu: looksLikePdu,
        showPduPowerDialog: showPduPowerDialog,
        showRackPowerDialog: showRackPowerDialog,
    };

    // The Phase 1 read-model (spec §2), its invariant checks and rdCanPlaceAt
    // now live in editor/model.js -- imported at the top of this file. They
    // closed over nothing here, so the move was a pure lift.

    // ---- Layer switch with unsaved changes (spec §10.3) --------------------
    // The rack view and the chassis layer are separate pages, so switching is a
    // navigation and would drop unsaved edits. The browser's beforeunload guard
    // below catches that, but a bare "leave site?" is a poor answer when the
    // user's actual intent is "keep my work". Offer the three real choices.
    function showSaveBeforeSwitchDialog(targetUrl) {
        var overlay = document.createElement("div");
        overlay.className = "modal fade nbx-rd-switch-modal";
        overlay.setAttribute("tabindex", "-1");
        overlay.innerHTML =
            '<div class="modal-dialog modal-dialog-centered modal-sm">'
            + '<div class="modal-content">'
            + '<div class="modal-header">'
            + '<h5 class="modal-title">Unsaved changes</h5>'
            + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
            + "</div>"
            + '<div class="modal-body"><p>You have unsaved changes. '
            + "Save them before switching view?</p></div>"
            + '<div class="modal-footer">'
            + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
            + '<button type="button" class="btn btn-sm btn-outline-danger" data-rd-switch-discard>'
            + "Discard</button>"
            + '<button type="button" class="btn btn-sm btn-primary" data-rd-switch-save>'
            + "Save and switch</button>"
            + "</div></div></div>";
        document.body.appendChild(overlay);

        var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
        var modal = ctor ? new ctor(overlay) : null;
        overlay.addEventListener("hidden.bs.modal", function () { overlay.remove(); });

        overlay.querySelector("[data-rd-switch-discard]").addEventListener("click", function () {
            changesMade = false;                 // disarm beforeunload
            if (modal) { modal.hide(); }
            window.location.href = targetUrl;
        });
        overlay.querySelector("[data-rd-switch-save]").addEventListener("click", function () {
            if (modal) { modal.hide(); }
            doSave(targetUrl);
        });
        if (modal) { modal.show(); } else if (window.confirm("Save your changes before switching?")) {
            doSave(targetUrl);
        }
    }

    document.addEventListener("click", function (event) {
        var link = event.target.closest && event.target.closest("[data-rd-layer-switch]");
        if (!link || !changesMade) { return; }
        event.preventDefault();
        showSaveBeforeSwitchDialog(link.getAttribute("href"));
    }, true);

    // ---- Unsaved-changes guard (design-level) ------------------------------
    window.addEventListener("beforeunload", function (event) {
        if (changesMade) {
            event.preventDefault();
            event.returnValue = "";
            return "";
        }
    });
})();
