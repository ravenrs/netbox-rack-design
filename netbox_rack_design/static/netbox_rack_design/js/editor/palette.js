/*
 * Shared device-type catalog palette + drag-in -- extracted verbatim from
 * editor.js. It is a leaf: it declares nothing at the 4-space level, so
 * nothing inside it is referenced from anywhere else in the file. `root` and
 * `isChassisLayer` are the only two things it needed from the enclosing
 * closure, so they come in as arguments.
 */

// ---- Shared device-type catalog palette + drag-in ----------------------
// A search box lists draggable device types fetched from NetBox's core API;
// dragging one onto ANY rack's front/rear grid plans a brand-new KIND_ADD
// placement on that rack (the drop handler is wired per-rack in initRack).
// No dcim.Device is ever created.
import { getCsrfToken, createToast } from "rd/core.js";

export function setupPalette(root, isChassisLayer) {
    // The CHASSIS LAYER (spec §10.3) filters the whole catalog to CHILD device
    // types. Not a validation -- a filter: a rack-mountable type is never
    // offered as a blade in the first place, so the mistake cannot be made.
    var subdeviceFilter = isChassisLayer ? "&subdevice_role=child" : "";

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
    // Named favorite SETS: one starred list per way of working (user request
    // 2026-08-28 -- "for server" pulls different types than "for network",
    // and one flat list meant re-starring on every switch). The stars read
    // and write whichever set is selected here.
    var favoriteSetsUrl = root.getAttribute("data-favorite-sets-url")
        || "/api/plugins/rack-design/favorite-sets/";
    var favoriteIds = {};   // id (number) -> true  (used as a Set)
    var favoriteSets = [];  // [{id, name, is_default, device_type_ids}]
    var activeSetId = null;
    var setSelectEl = document.getElementById("nbx-rd-favset-select");
    // The chosen set is per-user UI state that must survive a reload, but it
    // is not design data -- it belongs in the browser, not in a table.
    var SET_STORAGE_KEY = "nbx-rd-favorite-set";

    function rememberedSetId() {
        try {
            var raw = window.localStorage.getItem(SET_STORAGE_KEY);
            return raw ? parseInt(raw, 10) : null;
        } catch (e) { return null; }
    }

    function rememberSetId(id) {
        try {
            if (id == null) { window.localStorage.removeItem(SET_STORAGE_KEY); }
            else { window.localStorage.setItem(SET_STORAGE_KEY, String(id)); }
        } catch (e) { /* private mode: the choice just does not persist */ }
    }

    function activeSetName() {
        for (var i = 0; i < favoriteSets.length; i += 1) {
            if (favoriteSets[i].id === activeSetId) { return favoriteSets[i].name; }
        }
        return "";
    }

    function starTitle(fav) {
        var name = activeSetName();
        if (fav) { return name ? "Unstar (remove from " + name + ")" : "Unstar (remove favorite)"; }
        return name ? "Star (add to " + name + ")" : "Star (add favorite)";
    }

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
        // Subdevice role (spec §10.3): a CHILD type is a blade -- it may not be
        // racked at all, so its only legal target is a free device bay. Stamped
        // here so the drag layer can pick the target set without re-querying.
        var subrole = "";
        if (dt.subdevice_role) {
            subrole = dt.subdevice_role.value || dt.subdevice_role || "";
        }
        li.setAttribute("data-subdevice-role", subrole);
        if (subrole === "child") { li.classList.add("nbx-rd-palette-child"); }
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
        meta.textContent = (manuf ? manuf + " · " : "")
            + (subrole === "child" ? "child · fits a device bay"
               : (uHeight + "U" + (dt.is_full_depth ? " · full-depth" : "")
                  + (subrole === "parent" ? " · chassis" : "")));
        content.appendChild(model);
        content.appendChild(meta);
        li.appendChild(content);

        var fav = !!favoriteIds[dt.id];
        var star = document.createElement("button");
        star.type = "button";
        star.className = "nbx-rd-fav-btn" + (fav ? " is-fav" : "");
        star.setAttribute("data-device-type-id", String(dt.id));
        star.setAttribute("title", starTitle(fav));
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

    // Stamp each just-rendered palette row with its device type's projected
    // power draw (docs/power-projection-spec.md) so a freshly dropped catalog
    // add shows the SAME draw LIVE as it will after Save + reload -- the core
    // /api/dcim/device-types/ feed carries no computed draw, so a small
    // companion endpoint resolves it (same logic as the projection). The
    // attributes ride the palette <li>, so the drag-in CLONE inherits them;
    // onPaletteDrop copies them onto the tile content (which the heatmap
    // reads). Best-effort: a drop before this resolves just falls back to 0.
    // The selected palette Role rides along: the excluded-role rule
    // (power_exclude_roles) lives in the projection, so only the server can
    // say that a PDU-role add is a known 0 W rather than the unknown its
    // draw-less inlet template implies. Changing the Role select therefore
    // re-stamps the rendered rows (see the change listener below).
    var powerUrl = "/api/plugins/rack-design/device-type-power/";
    function paletteRoleId() {
        var el = document.getElementById("id_device_role");
        return (el && el.value) ? el.value : "";
    }
    function stampDraw(container) {
        if (!container) { return; }
        var rows = Array.prototype.slice.call(
            container.querySelectorAll(".nbx-rd-palette-item[data-device-type-id]"));
        if (!rows.length) { return; }
        var roleId = paletteRoleId();
        var url = powerUrl + "?" + rows.map(function (r) {
            return "id=" + encodeURIComponent(r.getAttribute("data-device-type-id"));
        }).join("&") + (roleId ? "&role_id=" + encodeURIComponent(roleId) : "");
        fetch(url, {
            credentials: "same-origin",
            headers: { "Accept": "application/json" },
        }).then(function (resp) {
            return resp.ok ? resp.json() : null;
        }).then(function (data) {
            if (!data || !data.results) { return; }
            rows.forEach(function (r) {
                var info = data.results[r.getAttribute("data-device-type-id")];
                if (!info) { return; }
                r.setAttribute("data-draw-w", String(Math.round(info.draw_w || 0)));
                r.setAttribute("data-draw-known", info.draw_known ? "1" : "0");
                // "name:draw:conn|..." — a type template has no cabling, so the
                // conn field is left blank (matches the server template's add row).
                var pp = (info.power_ports || []).map(function (p) {
                    return p.name + ":" + Math.round(p.draw || 0) + ":";
                }).join("|");
                if (pp) { r.setAttribute("data-power", pp); }
            });
        }).catch(function () { /* best-effort; tile falls back to 0 W */ });
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
        stampDraw(listEl);
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
        var url = "/api/dcim/device-types/?limit=200" + subdeviceFilter;
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
            stampDraw(quickListEl);
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
        var url = "/api/dcim/device-types/?limit=50" + subdeviceFilter;
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

    // The projected draw of an already-rendered row depends on the Role that
    // would be assigned (power_exclude_roles: a PDU-role add is a known 0 W,
    // not a consumer), so a Role change re-stamps both palettes rather than
    // leaving rows carrying the previous role's figure.
    var roleSelectEl = document.getElementById("id_device_role");
    if (roleSelectEl) {
        roleSelectEl.addEventListener("change", function () {
            stampDraw(listEl);
            stampDraw(quickListEl);
        });
    }

    // ---- Favorites (catalog stars) -------------------------------------
    function applyFavState(starBtn, fav) {
        starBtn.classList.toggle("is-fav", fav);
        starBtn.setAttribute("aria-pressed", fav ? "true" : "false");
        starBtn.setAttribute("title", starTitle(fav));
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
            body: JSON.stringify({ device_type_id: id, set_id: activeSetId }),
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
        var url = favoritesUrl
            + (activeSetId != null ? "?set_id=" + encodeURIComponent(activeSetId) : "");
        fetch(url, {
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
            // The server has the last word on which set answered: a stale
            // remembered id (its set deleted in another tab) resolves to the
            // default rather than 404-ing, and the UI must follow it there.
            if (data && data.set_id != null) { activeSetId = data.set_id; }
        }).catch(function () {
            favoriteIds = {};
        }).then(function () {
            renderSetSelector();
            renderQuickAccess();
            fetchTypes();
        });
    }

    // ---- Favorite sets (named star lists) -------------------------------
    function renderSetSelector() {
        if (!setSelectEl) { return; }
        setSelectEl.innerHTML = "";
        favoriteSets.forEach(function (fs) {
            var opt = document.createElement("option");
            opt.value = String(fs.id);
            opt.textContent = fs.name
                + " (" + ((fs.device_type_ids || []).length) + ")";
            if (fs.id === activeSetId) { opt.selected = true; }
            setSelectEl.appendChild(opt);
        });
        // A user always has at least one set (the server provisions the
        // default on first read), so an empty select means the load failed.
        setSelectEl.disabled = !favoriteSets.length;
    }

    function setsFetch(method, url, body) {
        var opts = {
            method: method,
            credentials: "same-origin",
            headers: { "Accept": "application/json" },
        };
        if (body !== undefined) {
            opts.headers["Content-Type"] = "application/json";
            opts.headers["X-CSRFToken"] = getCsrfToken();
            opts.body = JSON.stringify(body);
        } else if (method !== "GET") {
            opts.headers["X-CSRFToken"] = getCsrfToken();
        }
        return fetch(url, opts).then(function (resp) {
            return resp.text().then(function (text) {
                var data = null;
                try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
                if (!resp.ok) {
                    var msg = (data && data.name && data.name[0])
                        || ("HTTP " + resp.status);
                    throw new Error(msg);
                }
                return data;
            });
        });
    }

    function loadSets() {
        return setsFetch("GET", favoriteSetsUrl).then(function (data) {
            favoriteSets = (data && data.results) || [];
            var remembered = rememberedSetId();
            var found = favoriteSets.some(function (fs) { return fs.id === remembered; });
            activeSetId = found
                ? remembered
                : (favoriteSets.length ? favoriteSets[0].id : null);
        }).catch(function () {
            favoriteSets = [];
            activeSetId = null;
        });
    }

    function switchToSet(id) {
        activeSetId = id;
        rememberSetId(id);
        loadFavoritesThenFetch();
    }

    function refreshSetsThen(selectId) {
        return loadSets().then(function () {
            if (selectId != null
                && favoriteSets.some(function (fs) { return fs.id === selectId; })) {
                activeSetId = selectId;
            }
            rememberSetId(activeSetId);
            loadFavoritesThenFetch();
        });
    }

    if (setSelectEl) {
        setSelectEl.addEventListener("change", function () {
            var id = parseInt(setSelectEl.value, 10);
            if (!isNaN(id)) { switchToSet(id); }
        });
    }

    var newSetBtn = document.getElementById("nbx-rd-favset-new");
    if (newSetBtn) {
        newSetBtn.addEventListener("click", function () {
            showFavSetPrompt({
                title: "New favorite set",
                label: "Name",
                value: "",
                confirmText: "Create",
            }, function (name) {
                setsFetch("POST", favoriteSetsUrl, { name: name })
                    .then(function (data) { refreshSetsThen(data && data.id); })
                    .catch(function (err) { showFavSetError(err.message); });
            });
        });
    }

    var renameSetBtn = document.getElementById("nbx-rd-favset-rename");
    if (renameSetBtn) {
        renameSetBtn.addEventListener("click", function () {
            if (activeSetId == null) { return; }
            showFavSetPrompt({
                title: "Rename favorite set",
                label: "Name",
                value: activeSetName(),
                confirmText: "Rename",
            }, function (name) {
                setsFetch("PATCH", favoriteSetsUrl + activeSetId + "/", { name: name })
                    .then(function () { refreshSetsThen(activeSetId); })
                    .catch(function (err) { showFavSetError(err.message); });
            });
        });
    }

    var deleteSetBtn = document.getElementById("nbx-rd-favset-delete");
    if (deleteSetBtn) {
        deleteSetBtn.addEventListener("click", function () {
            if (activeSetId == null) { return; }
            var name = activeSetName();
            var count = Object.keys(favoriteIds).length;
            showFavSetConfirm(
                "Delete favorite set",
                "Delete \u201c" + name + "\u201d and its "
                    + count + " starred device type"
                    + (count === 1 ? "" : "s") + "? The device types themselves "
                    + "are not touched.",
                "Delete",
                function () {
                    setsFetch("DELETE", favoriteSetsUrl + activeSetId + "/")
                        .then(function () { refreshSetsThen(null); })
                        .catch(function (err) { showFavSetError(err.message); });
                }
            );
        });
    }

    // Bootstrap modals, never window.prompt/confirm -- every dialog in this
    // editor is this shape (see showMoveNameDialog), including for the test
    // shim that drives them through the DOM.
    function buildFavSetModal(title, bodyHtml, confirmText, danger) {
        var overlay = document.createElement("div");
        overlay.className = "modal fade nbx-rd-favset-modal";
        overlay.setAttribute("tabindex", "-1");
        overlay.innerHTML =
            '<div class="modal-dialog modal-dialog-centered modal-sm">'
            + '<div class="modal-content">'
            + '<div class="modal-header">'
            + '<h5 class="modal-title"></h5>'
            + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
            + "</div>"
            + '<div class="modal-body">' + bodyHtml + "</div>"
            + '<div class="modal-footer">'
            + '<button type="button" class="btn btn-sm btn-link" data-rd-favset-cancel>Cancel</button>'
            + '<button type="button" class="btn btn-sm '
            + (danger ? "btn-danger" : "btn-primary")
            + '" data-rd-favset-confirm></button>'
            + "</div>"
            + "</div></div>";
        overlay.querySelector(".modal-title").textContent = title;
        overlay.querySelector("[data-rd-favset-confirm]").textContent = confirmText;
        document.body.appendChild(overlay);

        var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
        var modal = ctor ? new ctor(overlay) : null;
        var shownDone = false, hidePending = false, decided = false;
        overlay.addEventListener("shown.bs.modal", function () {
            shownDone = true;
            if (hidePending && modal) { modal.hide(); }
            var input = overlay.querySelector("[data-rd-favset-input]");
            if (input) { input.focus(); input.select(); }
        });
        overlay.addEventListener("hidden.bs.modal", function () { overlay.remove(); });
        return {
            overlay: overlay,
            requestHide: function () {
                if (!modal) { overlay.remove(); return; }
                if (shownDone) { modal.hide(); } else { hidePending = true; }
            },
            show: function () { if (modal) { modal.show(); } },
            decide: function (fn) {
                if (decided) { return; }
                decided = true;
                if (typeof fn === "function") { fn(); }
            },
        };
    }

    function showFavSetPrompt(opts, onConfirm) {
        var dlg = buildFavSetModal(
            opts.title,
            '<label class="form-label" data-rd-favset-label></label>'
            + '<input type="text" class="form-control form-control-sm" '
            + 'maxlength="100" data-rd-favset-input>'
            + '<div class="text-danger small mt-1" data-rd-favset-error></div>',
            opts.confirmText, false);
        dlg.overlay.querySelector("[data-rd-favset-label]").textContent = opts.label;
        var input = dlg.overlay.querySelector("[data-rd-favset-input]");
        input.value = opts.value || "";

        function submit() {
            var name = (input.value || "").trim();
            if (!name) {
                dlg.overlay.querySelector("[data-rd-favset-error]").textContent =
                    "A set needs a name.";
                return;
            }
            dlg.decide(function () { onConfirm(name); });
            dlg.requestHide();
        }
        dlg.overlay.querySelector("[data-rd-favset-confirm]")
            .addEventListener("click", submit);
        input.addEventListener("keydown", function (event) {
            if (event.key === "Enter") { event.preventDefault(); submit(); }
        });
        dlg.overlay.querySelector("[data-rd-favset-cancel]")
            .addEventListener("click", function () { dlg.requestHide(); });
        dlg.overlay.querySelector(".btn-close")
            .addEventListener("click", function () { dlg.requestHide(); });
        dlg.show();
    }

    function showFavSetConfirm(title, message, confirmText, onConfirm) {
        var dlg = buildFavSetModal(
            title, '<p data-rd-favset-message></p>', confirmText, true);
        dlg.overlay.querySelector("[data-rd-favset-message]").textContent = message;
        dlg.overlay.querySelector("[data-rd-favset-confirm]")
            .addEventListener("click", function () {
                dlg.decide(onConfirm);
                dlg.requestHide();
            });
        dlg.overlay.querySelector("[data-rd-favset-cancel]")
            .addEventListener("click", function () { dlg.requestHide(); });
        dlg.overlay.querySelector(".btn-close")
            .addEventListener("click", function () { dlg.requestHide(); });
        dlg.show();
    }

    // A rejected name (duplicate, empty) must say so where the user is
    // looking, not vanish into the console.
    function showFavSetError(message) {
        createToast("danger", "Favorites",
            message || "Could not update the favorite set.");
    }

    loadSets().then(loadFavoritesThenFetch);
}
