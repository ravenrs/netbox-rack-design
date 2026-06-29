/*
 * Left-rail PANELS for the NetBox Rack Design multi-rack editor (slice 2d Phase
 * C). Two cards wired to existing REST endpoints; design EDITS still live in
 * editor.js — these panels only manage the design's rack SCOPE and the user's
 * personal per-rack visibility.
 *
 *   1. "Add rack"     — POST designs/<pk>/add-rack/ {rack_id}; reload on success.
 *   2. "Design racks" — per-rack visibility toggle (hidden-design-racks/toggle/,
 *                       reload-free: just toggles the .hidden class on the rack
 *                       block), an "All" button (show-all/), and a destructive
 *                       remove-from-design control (remove-rack/, with the 409
 *                       requires_confirmation two-step).
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

    function rackBlock(rackId) {
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
            postJSON(API + "designs/" + designId + "/add-rack/", { rack_id: rackId })
                .then(function (response) {
                    if (response.status === 200) {
                        // The server re-renders the new block on reload.
                        window.location.reload();
                        return;
                    }
                    btn.removeAttribute("disabled");
                    readError(response, "The rack could not be added.").then(function (msg) {
                        toast("danger", "Could not add rack", msg);
                    });
                })
                .catch(function (err) {
                    btn.removeAttribute("disabled");
                    toast("danger", "Error", String(err));
                });
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
        function doRemove(rackId, confirmFlag) {
            return postJSON(API + "designs/" + designId + "/remove-rack/", {
                rack_id: parseInt(rackId, 10),
                confirm: !!confirmFlag,
            });
        }

        function onRemove(rackId, rackName) {
            doRemove(rackId, false).then(function (response) {
                if (response.status === 200) {
                    window.location.reload();
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
                                window.location.reload();
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
        var VALID = { device: true, favorites: true, racks: true };

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
                var loc = document.getElementById("id_add_location");
                if (loc && loc.focus) {
                    try { loc.focus(); } catch (e) { /* ignore */ }
                }
            });
        }
    })();
})();
