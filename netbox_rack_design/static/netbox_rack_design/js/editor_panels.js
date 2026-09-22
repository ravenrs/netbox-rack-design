/*
 * Left-rail PANELS for the NetBox Rack Design multi-rack editor (slice 2d Phase
 * C). Two cards wired to existing REST endpoints; design EDITS still live in
 * editor.js — these panels only manage the design's rack SCOPE and the user's
 * personal per-rack visibility.
 *
 *   1. "Add rack"     — POST designs/<pk>/add-rack/ {rack_id}; reload on success.
 *   1b."Create rack"  — a dialog (name/height/location) that POSTs
 *                       create-planned-rack/ to draft a rack that does not
 *                       exist in NetBox yet (T1.5, PLAN-templates.md §1);
 *                       reload on success, same contract as Add rack.
 *   2. "Design racks" — per-rack visibility toggle (hidden-design-racks/toggle/,
 *                       reload-free: just toggles the .hidden class on the rack
 *                       block), an "All" button (show-all/), and a destructive
 *                       remove-from-design control (remove-rack/, with the 409
 *                       requires_confirmation two-step). Planned racks are not
 *                       listed here (D25): they have no per-user hide toggle.
 *
 * Visibility is VIEW state, never a design edit: toggling here never marks the
 * layout dirty and never arms editor.js's beforeunload guard.
 */
(function () {
    "use strict";

    var root = document.getElementById("rd-editor");
    if (!root) { return; }

    var designId = parseInt(root.getAttribute("data-design-id"), 10);
    if (isNaN(designId)) { return; }

    var API = "/api/plugins/rack-design/";

    // ---- Reuse editor.js's helpers, with safe fallbacks --------------------
    var shared = window.NbxRdEditor || {};
    function getCsrfToken() {
        if (typeof shared.getCsrfToken === "function") { return shared.getCsrfToken(); }
        var fromAttr = root.getAttribute("data-csrf-token");
        if (fromAttr) { return fromAttr; }
        if (typeof window.netbox_csrf_token !== "undefined" && window.netbox_csrf_token) {
            return window.netbox_csrf_token;
        }
        var input = document.querySelector("[name=csrfmiddlewaretoken]");
        return input ? input.value : "";
    }
    function toast(level, title, message) {
        if (typeof shared.createToast === "function") {
            shared.createToast(level, title, message);
        } else {
            window.alert(title + ": " + message);
        }
    }

    function postJSON(url, body) {
        return fetch(url, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify(body || {}),
        });
    }

    function readError(response, fallback) {
        return response.text().then(function (text) {
            var msg = "";
            try {
                var data = JSON.parse(text);
                if (typeof data === "string") {
                    msg = data;
                } else if (data) {
                    // Surface the first field error or a detail/error string.
                    msg = data.detail || data.error || data.message || "";
                    if (!msg) {
                        var keys = Object.keys(data);
                        for (var i = 0; i < keys.length && !msg; i++) {
                            var v = data[keys[i]];
                            if (Array.isArray(v) && v.length) { msg = String(v[0]); }
                            else if (typeof v === "string") { msg = v; }
                        }
                    }
                }
            } catch (e) {
                msg = (text || "").trim();
            }
            return msg || fallback;
        }).catch(function () { return fallback; });
    }

    // ---- Reload without losing unsaved work --------------------------------
    // Add/Create/Remove rack all need the server to re-render the workspace,
    // so they reload the page. A reload with unsaved edits pending hits
    // editor.js's beforeunload guard, and the planner is handed the
    // browser's "Reload site? Changes you made may not be saved." -- whose
    // Reload button throws the work away. Ask the editor's own three-choice
    // question first instead, and do the request only once the answer is in
    // (so "Cancel" never leaves a rack added that the page cannot show).
    //
    // `request` returns a Promise that resolves truthy when the change was
    // made; it is called after the user has chosen.
    function withUnsavedGuard(opts, request) {
        var ed = window.NbxRdEditor || {};
        function run(then) {
            return request().then(function (ok) { if (ok) { then(); } });
        }
        if (!ed.hasUnsavedChanges || !ed.hasUnsavedChanges()) {
            return run(function () { window.location.reload(); });
        }
        ed.promptUnsavedChanges({
            body: opts.body,
            saveLabel: opts.saveLabel,
            discardLabel: opts.discardLabel,
            onSave: function () {
                // Request first, then save-and-reload in one step: the save
                // navigates to the current URL, which renders the new block.
                run(function () { ed.saveLayout(window.location.href); });
            },
            onDiscard: function () {
                ed.discardUnsavedChanges();
                run(function () { window.location.reload(); });
            },
        });
        return null;
    }

    // The removal has already happened server-side by the time we reload, so
    // this variant does not offer "Cancel" as a way out of the change -- only
    // a way to keep or drop the unsaved LAYOUT edits the reload would take
    // with it. Dismissing the dialog leaves the page as it is; the rack is
    // gone from the design either way and shows on the next reload.
    function reloadAfterRackChange() {
        var ed = window.NbxRdEditor || {};
        if (!ed.hasUnsavedChanges || !ed.hasUnsavedChanges()) {
            window.location.reload();
            return;
        }
        ed.promptUnsavedChanges({
            body: "The rack was removed from the design. Reloading shows the "
                + "result -- save your unsaved edits first?",
            saveLabel: "Save and reload",
            discardLabel: "Discard and reload",
            onSave: function () { ed.saveLayout(window.location.href); },
            onDiscard: function () { window.location.reload(); },
        });
    }

    function rackBlock(rackId) {
        // T1.5c (PLAN-templates.md D31): this panel is real-rack-only by
        // construction (scoped_rack_rows in views.py is built from
        // design.racks only, never design.planned_racks -- D25), so rackId
        // here is always a bare real dcim.Rack pk -- and that is exactly
        // what rack_block.html writes into data-rack-id for a real rack.
        // rack_dom_id (templatetags/rack_design.py) prefixes ONLY a planned
        // rack ("p-<pk>") and keeps a real rack's pk bare, so this lookup
        // must NOT prefix. It briefly did ("r-<pk>"), which matched nothing:
        // the toggle then updated the row and the database but never the
        // rack, so hiding appeared to do nothing and a rack that loaded
        // hidden could not be shown again without a reload.
        return root.querySelector('.nbx-rd-rack-block[data-rack-id="' + rackId + '"]');
    }
    function rackRow(rackId) {
        return root.querySelector('[data-rd-rack-row="' + rackId + '"]');
    }

    // Reflect a rack's shown/hidden state onto its block + its panel row.
    function applyVisibility(rackId, hidden) {
        var block = rackBlock(rackId);
        if (block) { block.classList.toggle("hidden", hidden); }
        var row = rackRow(rackId);
        if (!row) { return; }
        row.classList.toggle("is-hidden", hidden);
        var btn = row.querySelector("[data-rd-visi-toggle]");
        if (btn) {
            btn.setAttribute("aria-pressed", hidden ? "false" : "true");
            var icon = btn.querySelector("i");
            if (icon) {
                icon.className = "mdi " + (hidden ? "mdi-eye-off-outline" : "mdi-eye-outline");
            }
        }
    }

    // Sync every row from a returned hidden_rack_ids set.
    function syncFromHidden(hiddenIds) {
        var hiddenSet = {};
        (hiddenIds || []).forEach(function (id) { hiddenSet[String(id)] = true; });
        root.querySelectorAll("[data-rd-rack-row]").forEach(function (row) {
            var rid = row.getAttribute("data-rd-rack-row");
            applyVisibility(rid, !!hiddenSet[rid]);
        });
    }

    // ========================================================================
    // 0. Add-rack panel: Site -> Location -> Rack chain (PLAN-multi-site.md
    // M8). NetBox's own DynamicTomSelect already reloads/clears
    // add_location's and add_rack's OPTIONS whenever add_site changes (the
    // query_params={"site_id": "$add_site", ...} declared on forms.py's
    // DesignEditorAddRackForm wire that up for free -- see
    // project-static/src/select/classes/dynamicTomSelect.ts:handleEvent,
    // which clears the dependent field's selection and re-fetches on any
    // dependency's native `change` event). What NetBox does NOT do is toggle
    // the whole-field `disabled` state, so that is the only thing this panel
    // wires up by hand: Location/Rack stay disabled until a site is chosen,
    // and re-disable whenever the site changes (their selection is already
    // cleared for us by the above).
    // ========================================================================
    (function setupAddRackSiteChain() {
        var siteSel = document.getElementById("id_add_site");
        var locationSel = document.getElementById("id_add_location");
        var rackSel = document.getElementById("id_add_rack");
        if (!siteSel || !locationSel || !rackSel) { return; }

        function setEnabled(select, enabled) {
            if (select.tomselect) {
                if (enabled) { select.tomselect.enable(); } else { select.tomselect.disable(); }
            } else {
                select.disabled = !enabled;
            }
        }

        function syncDependents() {
            var hasSite = !!siteSel.value;
            setEnabled(locationSel, hasSite);
            setEnabled(rackSel, hasSite);
        }

        // Reflect the server-rendered state immediately (a one-site design
        // starts pre-filled + enabled; a multi-site one starts empty +
        // disabled -- forms.DesignEditorAddRackForm.__init__), then keep it in
        // sync as the user changes the site.
        syncDependents();
        siteSel.addEventListener("change", syncDependents);
    })();

    // ========================================================================
    // 1. Add rack
    // ========================================================================
    (function setupAddRack() {
        var btn = document.getElementById("nbx-rd-add-rack-btn");
        var rackSel = document.getElementById("id_add_rack");
        if (!btn || !rackSel) { return; }

        btn.addEventListener("click", function () {
            var rackId = rackSel.value ? parseInt(rackSel.value, 10) : null;
            if (!rackId) {
                toast("info", "Pick a rack", "Choose a rack to add to this design.");
                return;
            }
            btn.setAttribute("disabled", "disabled");
            // The server re-renders the workspace with the new block, so this
            // reloads -- ask about unsaved edits first (withUnsavedGuard).
            withUnsavedGuard({
                body: "Adding a rack reloads the editor. Save your changes first?",
                saveLabel: "Save and add",
                discardLabel: "Discard and add",
            }, function () {
                return postJSON(API + "designs/" + designId + "/add-rack/", { rack_id: rackId })
                    .then(function (response) {
                        if (response.status === 200) { return true; }
                        btn.removeAttribute("disabled");
                        readError(response, "The rack could not be added.").then(function (msg) {
                            toast("danger", "Could not add rack", msg);
                        });
                        return false;
                    })
                    .catch(function (err) {
                        btn.removeAttribute("disabled");
                        toast("danger", "Error", String(err));
                        return false;
                    });
            });
        });
    })();

    // ========================================================================
    // 1b. Create rack: a rack that does not exist in NetBox yet (T1.5,
    // PLAN-templates.md §1/D3/D4/D6 -- a "PlannedRack"). A Bootstrap modal
    // built at click time (name / U height / location), the same pattern
    // dialogs.js's own dialogs and power.js's plain <select> rows use -- no
    // TomSelect widget to initialize on markup that does not exist in the DOM
    // until the dialog opens. POSTs to create-planned-rack and reloads on
    // 201, mirroring Add rack above (the server re-renders the new block,
    // badged "Planned" by inc/rack_block.html).
    // ========================================================================
    (function setupCreatePlannedRack() {
        var openBtn = document.getElementById("nbx-rd-create-planned-rack-btn");
        if (!openBtn) { return; }

        var createUrl = root.getAttribute("data-create-planned-rack-url");
        if (!createUrl) { return; }

        // Read once: the design's own sites, each with its own locations
        // (views._design_editor_context's `site_locations`, PLAN-multi-site.md
        // M8), the same same-site-in-design scope add_rack_form enforces for a
        // real rack.
        var locationsEl = document.getElementById("rd-site-locations");
        var siteGroups = [];
        if (locationsEl) {
            try {
                siteGroups = JSON.parse(locationsEl.textContent) || [];
            } catch (e) {
                siteGroups = [];
            }
        }
        var singleSite = siteGroups.length === 1 ? siteGroups[0] : null;

        function locationsForSite(siteId) {
            var group = siteGroups.filter(function (g) {
                return String(g.site_id) === String(siteId);
            })[0];
            return group ? group.locations : [];
        }

        function siteOptionsHtml() {
            return siteGroups.map(function (g) {
                return '<option value="' + g.site_id + '">' + g.site + "</option>";
            }).join("");
        }

        function locationOptionsHtml(siteId) {
            var locations = siteId ? locationsForSite(siteId) : [];
            if (!locations.length) {
                return '<option value="">(no locations in this site)</option>';
            }
            return locations.map(function (loc) {
                return '<option value="' + loc.id + '">' + loc.name + "</option>";
            }).join("");
        }

        openBtn.addEventListener("click", function () {
            var overlay = document.createElement("div");
            overlay.className = "modal fade nbx-rd-create-rack-modal";
            overlay.setAttribute("tabindex", "-1");
            overlay.innerHTML =
                '<div class="modal-dialog modal-dialog-centered">'
                + '<div class="modal-content">'
                + '<div class="modal-header">'
                + '<h5 class="modal-title">Create rack</h5>'
                + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
                + "</div>"
                + '<div class="modal-body">'
                + '<div class="mb-2">'
                + '<label class="form-label small mb-1">Name</label>'
                + '<input type="text" class="form-control form-control-sm nbx-rd-create-rack-name" placeholder="e.g. R101">'
                + "</div>"
                + '<div class="mb-2">'
                + '<label class="form-label small mb-1">U height</label>'
                + '<input type="number" class="form-control form-control-sm nbx-rd-create-rack-height" value="42" min="1">'
                + "</div>"
                + '<div class="mb-2">'
                + '<label class="form-label small mb-1">Site</label>'
                + (singleSite
                    ? '<div class="form-control form-control-sm bg-body-tertiary nbx-rd-create-rack-site-chip" '
                      + 'data-rd-create-rack-site-value="' + singleSite.site_id + '">'
                      + '<i class="mdi mdi-map-marker-outline" aria-hidden="true"></i> ' + singleSite.site
                      + "</div>"
                    : '<select class="form-select form-select-sm nbx-rd-create-rack-site">'
                      + '<option value="">Choose a site…</option>'
                      + siteOptionsHtml()
                      + "</select>")
                + "</div>"
                + '<div class="mb-2">'
                + '<label class="form-label small mb-1">Location</label>'
                + '<select class="form-select form-select-sm nbx-rd-create-rack-location"'
                + (singleSite ? "" : " disabled")
                + ">"
                + '<option value="">Choose a location…</option>'
                + (singleSite ? locationOptionsHtml(singleSite.site_id) : "")
                + "</select>"
                + "</div>"
                + '<div class="text-danger small nbx-rd-create-rack-error" style="display:none"></div>'
                + "</div>"
                + '<div class="modal-footer">'
                + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
                + '<button type="button" class="btn btn-sm btn-primary" data-rd-create-rack-submit>Create</button>'
                + "</div>"
                + "</div></div>";
            document.body.appendChild(overlay);

            var nameInput = overlay.querySelector(".nbx-rd-create-rack-name");
            var heightInput = overlay.querySelector(".nbx-rd-create-rack-height");
            var siteSelect = overlay.querySelector(".nbx-rd-create-rack-site");
            var siteChip = overlay.querySelector(".nbx-rd-create-rack-site-chip");
            var locationSelect = overlay.querySelector(".nbx-rd-create-rack-location");
            var errorEl = overlay.querySelector(".nbx-rd-create-rack-error");
            var submitBtn = overlay.querySelector("[data-rd-create-rack-submit]");

            function showError(msg) {
                errorEl.textContent = msg;
                errorEl.style.display = "";
            }

            // The chosen (or pre-selected, single-site) site id -- read from
            // either the live <select> or the read-only chip's data attribute.
            function currentSiteId() {
                if (siteChip) { return siteChip.getAttribute("data-rd-create-rack-site-value"); }
                return siteSelect ? siteSelect.value : "";
            }

            // Multi-site design: Location is empty + disabled until a site is
            // chosen, then filtered to just that site's locations, and cleared
            // again on every site change -- the same Site -> Location gating
            // the Add-rack panel does with its own (TomSelect-backed) fields.
            if (siteSelect) {
                siteSelect.addEventListener("change", function () {
                    var siteId = siteSelect.value;
                    locationSelect.innerHTML = '<option value="">Choose a location…</option>'
                        + locationOptionsHtml(siteId);
                    locationSelect.value = "";
                    locationSelect.disabled = !siteId;
                });
            }

            var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
            var modal = ctor ? new ctor(overlay) : null;

            // Transition-safe hide (dialogs.js's own comment explains why: a
            // hide() issued while the show-fade is still running is silently
            // dropped by Bootstrap and strands the dialog on screen forever).
            var shownDone = false, hidePending = false;
            overlay.addEventListener("shown.bs.modal", function () {
                shownDone = true;
                if (hidePending && modal) { modal.hide(); }
                nameInput.focus();
            });
            function requestHide() {
                if (!modal) { overlay.remove(); return; }
                if (shownDone) { modal.hide(); } else { hidePending = true; }
            }
            overlay.addEventListener("hide.bs.modal", function () {
                if (overlay.contains(document.activeElement)) {
                    document.activeElement.blur();
                }
            });
            overlay.addEventListener("hidden.bs.modal", function () { overlay.remove(); });
            overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
                btn.addEventListener("click", function () { requestHide(); });
            });

            submitBtn.addEventListener("click", function () {
                var name = nameInput.value.trim();
                var uHeight = parseInt(heightInput.value, 10);
                var locationId = locationSelect.value ? parseInt(locationSelect.value, 10) : null;

                errorEl.style.display = "none";
                if (!name) {
                    showError("Name is required.");
                    return;
                }
                if (!uHeight || uHeight < 1) {
                    showError("U height must be a positive number.");
                    return;
                }
                // Site gates Location (M8): pick a site before a location can
                // even be chosen (the multi-site case; a one-site design has
                // its site fixed via the chip and needs no separate check).
                if (!currentSiteId()) {
                    showError("Site is required.");
                    return;
                }
                // Location is MANDATORY (D4): (location, name) is this
                // model's whole identity, matching dcim.Rack's own
                // uniqueness constraint -- there is no such thing as a
                // planned rack with no location.
                if (!locationId) {
                    showError("Location is required.");
                    return;
                }

                submitBtn.setAttribute("disabled", "disabled");
                // Same reload contract as Add rack -- and the same question
                // about unsaved edits before it.
                withUnsavedGuard({
                    body: "Creating a rack reloads the editor. Save your changes first?",
                    saveLabel: "Save and create",
                    discardLabel: "Discard and create",
                }, function () {
                    return postJSON(createUrl,
                                    { name: name, u_height: uHeight, location_id: locationId })
                        .then(function (response) {
                            if (response.status === 201) {
                                if (modal) { modal.hide(); }
                                return true;
                            }
                            submitBtn.removeAttribute("disabled");
                            readError(response, "The rack could not be created.").then(function (msg) {
                                showError(msg);
                            });
                            return false;
                        })
                        .catch(function (err) {
                            submitBtn.removeAttribute("disabled");
                            showError(String(err));
                            return false;
                        });
                });
            });

            if (modal) {
                modal.show();
            } else {
                // No Bootstrap JS: degrade the same way dialogs.js's dialogs do.
                toast("danger", "Error", "This dialog needs Bootstrap's modal JS.");
                overlay.remove();
            }
        });
    })();

    // ========================================================================
    // 2b. Chassis layer: chassis visibility (spec §10.3)
    // ------------------------------------------------------------------------
    // The chassis-layer twin of the Design-racks toggle below, and deliberately
    // the same contract: HIDDEN rows are stored server-side, the response is the
    // authority on the resulting set, and the canvas is re-synced from it with
    // no page reload. A PLANNED chassis has no device row, so it carries no
    // toggle at all -- it is always visible.
    // ========================================================================
    (function setupChassisPanel() {
        var list = document.querySelector("[data-rd-chassis-list]");
        if (!list) { return; }
        var url = list.getAttribute("data-hidden-chassis-url");
        if (!url) { return; }

        function syncFromHidden(hiddenIds) {
            var hidden = {};
            (hiddenIds || []).forEach(function (id) { hidden[id] = true; });
            list.querySelectorAll("[data-chassis-id]").forEach(function (row) {
                var id = parseInt(row.getAttribute("data-chassis-id"), 10);
                var isHidden = !!hidden[id];
                row.classList.toggle("nbx-rd-scope-hidden", isHidden);
                var btn = row.querySelector("[data-rd-chassis-toggle]");
                if (btn) {
                    btn.setAttribute("aria-pressed", isHidden ? "true" : "false");
                    btn.setAttribute("title", isHidden ? "Show this chassis" : "Hide this chassis");
                    var icon = btn.querySelector("i");
                    if (icon) {
                        icon.className = "mdi " + (isHidden ? "mdi-eye-off" : "mdi-eye");
                    }
                }
                var block = document.querySelector(
                    '.nbx-rd-chassis-block[data-chassis-id="' + id + '"]');
                if (block) { block.classList.toggle("hidden", isHidden); }
            });
        }

        list.addEventListener("click", function (event) {
            var btn = event.target.closest("[data-rd-chassis-toggle]");
            if (!btn) { return; }
            var row = btn.closest("[data-chassis-id]");
            if (!row) { return; }
            event.preventDefault();
            postJSON(url + "toggle/", {
                design_id: designId,
                chassis_id: parseInt(row.getAttribute("data-chassis-id"), 10),
            }).then(function (response) {
                if (!response.ok) {
                    return readError(response, "Could not change visibility.")
                        .then(function (msg) { toast("danger", "Error", msg); });
                }
                return response.json().then(function (data) {
                    syncFromHidden(data.hidden_chassis_ids);
                });
            }).catch(function (err) { toast("danger", "Error", String(err)); });
        });
    })();

    // ========================================================================
    // 2. Design racks panel: visibility toggle, "All", remove-from-design
    // ========================================================================
    (function setupDesignRacks() {
        var panel = document.getElementById("nbx-rd-design-racks-card");
        if (!panel) { return; }

        // ---- Per-row visibility toggle (reload-free view state) ------------
        function onToggle(rackId) {
            postJSON(API + "hidden-design-racks/toggle/", {
                design_id: designId,
                rack_id: parseInt(rackId, 10),
            }).then(function (response) {
                if (!response.ok) {
                    return readError(response, "Could not change visibility.").then(function (msg) {
                        toast("danger", "Error", msg);
                    });
                }
                return response.json().then(function (data) {
                    syncFromHidden(data.hidden_rack_ids);
                });
            }).catch(function (err) {
                toast("danger", "Error", String(err));
            });
        }

        // ---- "All": clear every hidden row for this user + design ----------
        function onShowAll() {
            postJSON(API + "hidden-design-racks/show-all/", { design_id: designId })
                .then(function (response) {
                    if (!response.ok) {
                        return readError(response, "Could not show all racks.").then(function (msg) {
                            toast("danger", "Error", msg);
                        });
                    }
                    return response.json().then(function (data) {
                        syncFromHidden(data.hidden_rack_ids || []);
                    });
                }).catch(function (err) {
                    toast("danger", "Error", String(err));
                });
        }

        // ---- Remove from design (destructive; 409 two-step confirm) --------
        // `rackKey` is the namespaced id the row carries ("r:<pk>" for a real
        // rack, "p:<pk>" for a planned one, D28) and goes to the server as-is
        // -- the two kinds keep separate pk sequences, so a bare integer
        // could name either rack.
        function doRemove(rackKey, confirmFlag) {
            return postJSON(API + "designs/" + designId + "/remove-rack/", {
                rack_id: String(rackKey),
                confirm: !!confirmFlag,
            });
        }

        function onRemove(rackId, rackName) {
            doRemove(rackId, false).then(function (response) {
                if (response.status === 200) {
                    reloadAfterRackChange();
                    return;
                }
                if (response.status === 409) {
                    response.json().then(function (data) {
                        var count = data.affected_count || 0;
                        var lines = (data.affected || []).slice(0, 8).map(function (a) {
                            var where = (a.u_position != null) ? (" @ U" + a.u_position) : "";
                            return "• " + (a.kind || "") + " " + (a.device_or_type || "") + where;
                        });
                        var msg = "Removing \"" + (rackName || "this rack") + "\" will discard "
                            + count + " planned placement" + (count === 1 ? "" : "s")
                            + " targeting it:\n\n" + lines.join("\n")
                            + (count > lines.length ? "\n…" : "")
                            + "\n\nProceed?";
                        if (!window.confirm(msg)) { return; }
                        doRemove(rackId, true).then(function (resp2) {
                            if (resp2.status === 200) {
                                reloadAfterRackChange();
                            } else {
                                readError(resp2, "The rack could not be removed.").then(function (m) {
                                    toast("danger", "Could not remove rack", m);
                                });
                            }
                        }).catch(function (err) {
                            toast("danger", "Error", String(err));
                        });
                    });
                    return;
                }
                readError(response, "The rack could not be removed.").then(function (msg) {
                    toast("danger", "Could not remove rack", msg);
                });
            }).catch(function (err) {
                toast("danger", "Error", String(err));
            });
        }

        panel.addEventListener("click", function (event) {
            var visiBtn = event.target.closest("[data-rd-visi-toggle]");
            if (visiBtn) {
                event.preventDefault();
                onToggle(visiBtn.getAttribute("data-rd-visi-toggle"));
                return;
            }
            var removeBtn = event.target.closest("[data-rd-remove-rack]");
            if (removeBtn) {
                event.preventDefault();
                onRemove(
                    removeBtn.getAttribute("data-rd-remove-rack"),
                    removeBtn.getAttribute("data-rack-name")
                );
                return;
            }
            if (event.target.closest("#nbx-rd-show-all-racks")) {
                event.preventDefault();
                onShowAll();
            }
        });
    })();

    // ========================================================================
    // 3. Sectioned tool drawer (push/collapse sidebar, three INDEPENDENT toggles)
    // ========================================================================
    // ONE push/collapse drawer hosting three INDEPENDENT sections — Device
    // (device-type catalog only; role + tenant live in the always-visible toolbar
    // above the shell), Favorites (quick access) and Racks (add-rack +
    // design-racks). Like the 2f Front/Rear face toggles, each card-header button
    // toggles ONLY its own section on/off:
    //   - clicking a section's button shows or hides JUST that section;
    //   - any combination can be visible at once (0, 1, 2 or all 3), laid out
    //     side by side as columns in the drawer (each column scrolls internally,
    //     so the drawer widens to the right as more sections open);
    //   - the drawer is OPEN whenever ANY section is active and CLOSED (racks go
    //     full width) only when NONE are.
    // PUSH/COLLAPSE, not an overlay: when open the rack workspace shifts right
    // (both visible, so a device type can still be dragged from the catalog onto
    // a rack face). Default CLOSED. The SET of open sections is pure view state,
    // remembered per browser in localStorage (never touches the design or the
    // dirty/Save state).
    (function setupDrawer() {
        var shell = document.getElementById("nbx-rd-editor-shell");
        if (!shell) { return; }

        var buttons = Array.prototype.slice.call(
            document.querySelectorAll("[data-rd-section-toggle]")
        );
        var sections = Array.prototype.slice.call(
            shell.querySelectorAll("[data-rd-section]")
        );
        if (!buttons.length) { return; }

        var STORE_KEY = "nbxRdDrawerSections";
        var VALID = { device: true, favorites: true, racks: true, templates: true };

        // The active set, as a plain object used as a string-set. Membership is
        // the single source of truth; the DOM/storage are derived from it.
        var active = {};

        // Parse a comma-joined preference ("device,racks") into the active set,
        // dropping blanks/unknowns. "" => empty set (explicitly all closed).
        function parse(value) {
            var set = {};
            (value || "").split(",").forEach(function (name) {
                name = name.trim();
                if (VALID[name]) { set[name] = true; }
            });
            return set;
        }

        // Stored preference: a comma-joined list of open sections if the user has
        // touched the drawer before, "" if they explicitly closed every section,
        // or null (no preference yet). We distinguish "unset" from "closed" so an
        // empty design can default OPEN on Racks without overriding a returning
        // user's explicit choice.
        function storedValue() {
            try {
                return window.localStorage.getItem(STORE_KEY);
            } catch (e) {
                return null;
            }
        }
        function writeStored() {
            try {
                window.localStorage.setItem(STORE_KEY, Object.keys(active).join(","));
            } catch (e) { /* storage unavailable: in-memory only */ }
        }

        // Reflect the active set onto the shell, each section, and each button.
        // The drawer is open iff ANY section is active; active sections sit side
        // by side as columns (CSS handles the row layout + per-column width).
        function render() {
            var open = Object.keys(active).length > 0;
            shell.classList.toggle("drawer-open", open);
            sections.forEach(function (s) {
                s.classList.toggle("is-active", !!active[s.getAttribute("data-rd-section")]);
            });
            buttons.forEach(function (b) {
                var on = !!active[b.getAttribute("data-rd-section-toggle")];
                b.classList.toggle("active", on);
                b.setAttribute("aria-expanded", on ? "true" : "false");
            });
        }

        // No stored preference => fall back to the template's default. The empty
        // editor sets data-drawer-section-initial="racks" so Add-rack shows
        // immediately; the normal default is "" (closed).
        var stored = storedValue();
        active = parse(
            stored === null
                ? (shell.getAttribute("data-drawer-section-initial") || "")
                : stored
        );
        render();

        // Toggle ONE section on/off, leaving the others untouched.
        function toggleSection(section) {
            if (!VALID[section]) { return; }
            if (active[section]) { delete active[section]; }
            else { active[section] = true; }
            render();
            writeStored();
            // The rack region resized; let the GridStack-backed faces relayout.
            window.dispatchEvent(new Event("resize"));
        }

        buttons.forEach(function (b) {
            b.addEventListener("click", function () {
                toggleSection(b.getAttribute("data-rd-section-toggle"));
            });
        });

        // Empty-state shortcut: "Add your first rack" ENSURES the Racks section
        // is on (without closing any other open section) and focuses the Add-rack
        // location field so a brand-new design can be populated straight away.
        var firstRackBtn = document.getElementById("nbx-rd-add-first-rack");
        if (firstRackBtn) {
            firstRackBtn.addEventListener("click", function () {
                if (!active.racks) {
                    active.racks = true;
                    render();
                    writeStored();
                    window.dispatchEvent(new Event("resize"));
                }
                // Focus the first field the planner still has to answer:
                // Site when it is empty (nothing downstream is usable yet),
                // otherwise Location -- a one-site design starts pre-filled,
                // so landing on Site would make them tab past a decision
                // that is already made. Site stays a live picker either way.
                var site = document.getElementById("id_add_site");
                var target = (site && !site.value)
                    ? site : document.getElementById("id_add_location");
                if (target && target.focus) {
                    try { target.focus(); } catch (e) { /* ignore */ }
                }
            });
        }
    })();
})();
