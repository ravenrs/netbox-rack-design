/*
 * Planned-PDU / rack power dialogs -- extracted verbatim from editor.js,
 * where this block sat as one contiguous section. Mirrors a real
 * dcim.PowerFeed/PowerPanel setup that has not been created yet, so the
 * rack- and PDU-power dialogs below work off a stored power_config
 * (custom-field snapshot + feed electricals) rather than live objects.
 */

import { rdTrace } from "rd/trace.js";
import { getCsrfToken, createToast, rackKeyToServer, rackDomIdToRealPk } from "rd/core.js";

// The editor's root element. editor.js reads it once and returns early when it
// is absent; this module is imported unconditionally, so API_BASE below guards
// the dereference (see the comment there).
const root = document.getElementById("rd-editor");

// markDirty lives in editor.js's IIFE (it owns the dirty flag and the Save
// button). editor.js hands it over once, at the point this block used to sit.
let markDirty = function () {};

function setPowerHooks(hooks) {
    if (hooks && hooks.markDirty) { markDirty = hooks.markDirty; }
}

// ---- Planned-PDU / rack power dialogs (docs/pdu-distribution-spec.md) --
// A planned PDU add has no real device/PowerFeed yet -- the distribution
// script needs a stored power_config (custom-fields snapshot + feed
// electricals) to size it, same as a rack needs a stored power_config for
// its power_limitation/pdu_location override (models.DesignPlacement /
// DesignRackPower, Phase A). Both dialogs mirror showMoveNameDialog's
// proven Bootstrap-modal shape above (fresh overlay per open, `decided`
// guard, transition-safe hide, blur-before-hide).

// Guarded, unlike the editor.js original: this runs at MODULE EVALUATION, and
// editor.js deliberately no-ops when #rd-editor is absent. An unguarded
// getAttribute here would turn that graceful no-op into a TypeError.
var API_BASE = "/api/plugins/rack-design/designs/"
    + ((root && root.getAttribute("data-design-id")) || "") + "/";

function apiRackPowerUrl() { return API_BASE + "rack-power/"; }
function apiFeedsUrl() { return API_BASE + "feeds/"; }
function apiPlannedFeedUrl() { return API_BASE + "planned-feed/"; }

// The custom-field bridge schema (docs/pdu-distribution-spec.md §5): read
// once from the editor-data json_script global. `{}` (no key for a role)
// means the rack-power dialog shows only the copy-from-rack row -- no
// hardcoded cf inputs anywhere in this file.
var PLANNING_FIELDS = (function () {
    var el = document.getElementById("rd-planning-fields");
    if (!el) { return {}; }
    try { return JSON.parse(el.textContent || "{}") || {}; } catch (e) { return {}; }
})();

// The effective power-distribution engine (docs/pdu-distribution-spec.md),
// read once from the editor-data json_script global. Custom-field planning
// inputs (rack ceiling, PDU orientation, ...) are consumed ONLY by a
// user's distribution script in "script" mode -- the native "none" and
// "builtin" engines never read a custom field, so the power dialogs must
// not offer manual cf inputs they cannot deliver on.
var DISTRIBUTION_MODE = (function () {
    var el = document.getElementById("rd-distribution-mode");
    if (!el) { return "none"; }
    try { return JSON.parse(el.textContent || "\"none\"") || "none"; } catch (e) { return "none"; }
})();

// The deployment's config-declared PLACEMENT fields (planning_fields.py):
// the descriptors for values a planner sets on a planned device, read once
// from the editor-data json_script global. `[]` (the shipped default) means
// no extra inputs render anywhere -- no cf name is ever hardcoded here.
var PLACEMENT_FIELDS = (function () {
    var el = document.getElementById("rd-placement-fields");
    if (!el) { return []; }
    try { return JSON.parse(el.textContent || "[]") || []; } catch (e) { return []; }
})();

// Peer conflicts (PLAN-peer-conflicts.md P5/P7), read once from the SAME
// json_script convention as the three globals above: the flat, ungrouped
// entries the panel's collapsed rows are also built from server-side
// (views._design_editor_context's `peer_conflicts` key). Used ONLY for
// the post-save toast below -- the panel itself is rendered directly by
// the template, never by this JS.
var PEER_CONFLICTS = (function () {
    var el = document.getElementById("rd-peer-conflicts");
    if (!el) { return []; }
    try { return JSON.parse(el.textContent || "[]") || []; } catch (e) { return []; }
})();

// One sessionStorage key per design (sessionStorage is per-tab already,
// but a design pk in the key keeps two designs opened in the same tab's
// history from ever reading each other's flag).
function postSaveToastKey() {
    return "nbx-rd-post-save-toast-" + (root.getAttribute("data-design-id") || "");
}

// P5: the toast is feedback for the action JUST TAKEN ("I just did
// that"), so it fires exactly once, on the very next load after a save
// that actually changed something -- doSave's 200 branch (below) stashes
// this key right before its `window.location.reload()`. An ordinary page
// view (opening the design tomorrow, or a peer's planner opening THEIR
// design and seeing the same conflict, P6) never sets the key, so it
// never re-fires here -- that case is what the panel row above is for
// instead, since it reads from the page's own render every time, not
// from this one-shot flag.
(function fireSaveTimeConflictToasts() {
    var key = postSaveToastKey();
    var flagged;
    try { flagged = sessionStorage.getItem(key); } catch (e) { flagged = null; }
    if (!flagged) { return; }
    try { sessionStorage.removeItem(key); } catch (e) { /* ignore */ }
    PEER_CONFLICTS.forEach(function (c) {
        var level = c.severity === "error" ? "danger" : "warning";
        createToast(level, "Peer conflict", c.detail);
    });
})();

// Which of them a given placement kind may carry. The default is add-only,
// matching the role/tenant rule the model enforces.
function placementFieldsFor(kind) {
    return PLACEMENT_FIELDS.filter(function (f) {
        var kinds = f.kinds && f.kinds.length ? f.kinds : ["add"];
        return kinds.indexOf(kind) !== -1;
    });
}

// Read a rendered set of planning inputs into a flat {key: value} blob,
// dropping the ones left blank -- an unset field is absent, never "".
function readPlacementFieldInputs(scope, selector) {
    var out = {};
    if (!scope) { return out; }
    // `select, input` rather than the bare class: NetBox's TomSelect
    // enhancement copies a select's classes onto the wrapper <div> it
    // inserts, which would otherwise match here and read as a blank field.
    scope.querySelectorAll("select" + selector + ", input" + selector + ", textarea" + selector)
        .forEach(function (input) {
            var key = input.getAttribute("data-field-key");
            if (!key) { return; }
            if (input.multiple) {
                var picked = Array.prototype.map.call(input.selectedOptions, function (o) {
                    return o.value;
                }).filter(function (v) { return v !== ""; });
                if (picked.length) { out[key] = picked; }
                return;
            }
            var value = (input.value || "").trim();
            if (value !== "") { out[key] = value; }
        });
    return out;
}

// Put a stored value back into a rendered planning input. Handles the shapes
// a bound custom field stores: a list (multi-select / multi-object), a
// boolean, a number, an ISO date-time (a datetime-local input takes
// "YYYY-MM-DDTHH:MM"), a JSON object.
function setPlacementFieldInput(input, value) {
    if (value == null) { return; }
    if (input.multiple) {
        var wanted = (Array.isArray(value) ? value : [value]).map(String);
        Array.prototype.forEach.call(input.options, function (o) {
            o.selected = wanted.indexOf(o.value) !== -1;
        });
        return;
    }
    if (typeof value === "boolean") { input.value = value ? "true" : "false"; return; }
    if (input.type === "datetime-local") { input.value = String(value).slice(0, 16); return; }
    if (typeof value === "object") { input.value = JSON.stringify(value); return; }
    input.value = String(value);
}

// Names of related objects picked in this session, by field key and id, so
// an in-session hover card shows "AMS1" rather than an id before any reload.
var objectPickLabels = {};

// An object / multi-object field is a <select> whose options are fetched from
// the field's REST list (`api_url`), filtered by the search box beside it.
// The stored value is the object id, exactly as NetBox stores it.
function wireObjectPickers(scope, current) {
    if (!scope) { return; }
    scope.querySelectorAll("select[data-api-url]").forEach(function (sel) {
        var key = sel.getAttribute("data-field-key");
        var url = sel.getAttribute("data-api-url");
        var search = sel.parentNode.querySelector("[data-rd-object-search]");
        var stored = current && current[key] != null ? current[key] : null;
        var storedIds = stored == null ? [] : (Array.isArray(stored) ? stored : [stored]).map(String);
        objectPickLabels[key] = objectPickLabels[key] || {};

        function load(q) {
            var keep = Array.prototype.filter.call(sel.options, function (o) {
                return o.selected && o.value !== "";
            }).map(function (o) { return o.value; });
            if (!keep.length) { keep = storedIds; }
            var params = "brief=true&limit=50" + (q ? "&q=" + encodeURIComponent(q) : "");
            var requests = [fetch(url + "?" + params, { credentials: "same-origin" })
                .then(function (r) { return r.ok ? r.json() : { results: [] }; })];
            if (keep.length) {
                requests.push(fetch(url + "?brief=true&limit=" + keep.length + "&"
                        + keep.map(function (id) { return "id=" + encodeURIComponent(id); }).join("&"),
                    { credentials: "same-origin" })
                    .then(function (r) { return r.ok ? r.json() : { results: [] }; }));
            }
            Promise.all(requests).then(function (pages) {
                var seen = {};
                var rows = [];
                pages.forEach(function (page) {
                    (page.results || []).forEach(function (obj) {
                        if (seen[obj.id]) { return; }
                        seen[obj.id] = true;
                        rows.push(obj);
                    });
                });
                var html = sel.multiple ? "" : '<option value="">—</option>';
                html += rows.map(function (obj) {
                    var text = String(obj.display || obj.name || obj.id);
                    objectPickLabels[key][String(obj.id)] = text;
                    var safe = text.replace(/&/g, "&amp;").replace(/</g, "&lt;");
                    var on = keep.indexOf(String(obj.id)) !== -1 ? " selected" : "";
                    return '<option value="' + obj.id + '"' + on + ">" + safe + "</option>";
                }).join("");
                sel.innerHTML = html;
            });
        }
        var timer = null;
        if (search) {
            search.addEventListener("input", function () {
                clearTimeout(timer);
                timer = setTimeout(function () { load(search.value.trim()); }, 250);
            });
        }
        load("");
    });
}

// What a hover card shows for one value, matching the server's
// planning_fields.display_value.
function planningDisplay(f, value) {
    var labels = f.choice_labels || {};
    if (f.type === "boolean") {
        return (value === true || value === "true") ? "Yes" : "No";
    }
    if (f.type === "select" || f.type === "choice") { return String(labels[value] || value); }
    if (f.type === "multiselect") {
        return (Array.isArray(value) ? value : [value]).map(function (v) {
            return String(labels[v] || v);
        }).join(", ");
    }
    if (f.type === "object" || f.type === "multiobject") {
        var names = objectPickLabels[f.key] || {};
        return (Array.isArray(value) ? value : [value]).map(function (v) {
            return names[String(v)] || "#" + v;
        }).join(", ");
    }
    if (typeof value === "object") { return JSON.stringify(value); }
    return String(value);
}

// ---- The palette rail's sticky defaults ---------------------------------
// Descriptors flagged `rail` behave like the Role/Tenant selects: pick once,
// every subsequent drag-in inherits the choice. Rendered from the schema
// into an empty mount in the toolbar, so the template names no field either.
var placementRailEl = document.querySelector("[data-rd-placement-rail]");

function renderPlacementRail() {
    if (!placementRailEl) { return; }
    var fields = PLACEMENT_FIELDS.filter(function (f) { return f.rail; });
    if (!fields.length) { return; }
    placementRailEl.innerHTML = fields.map(function (f) {
        var safeLabel = String(f.label || f.key).replace(/&/g, "&amp;").replace(/</g, "&lt;");
        return '<div class="nbx-rd-toolbar-field d-flex align-items-center gap-2">'
            + '<label class="form-label small mb-0 text-nowrap">' + safeLabel + "</label>"
            + planningFieldInputHtml(f, "nbx-rd-placement-rail-field") + "</div>";
    }).join("");
    wireObjectPickers(placementRailEl, {});
}

// The rail's current selections, as the planning_data blob a fresh add
// starts life with.
function railPlacementData() {
    return readPlacementFieldInputs(placementRailEl, ".nbx-rd-placement-rail-field");
}

// The rail's whole attribution set: Role, Tenant and the config-declared
// planning fields. One reader, because all three behave identically --
// "what the next device I touch should become" -- and a drop or a move
// applies them together.
function railAttribution() {
    var roleEl = document.getElementById("id_device_role");
    var tenantEl = document.getElementById("id_tenant");
    function label(el) {
        return (el && el.value && el.selectedOptions && el.selectedOptions.length)
            ? el.selectedOptions[0].textContent.trim() : "";
    }
    return {
        device_role_id: (roleEl && roleEl.value) ? parseInt(roleEl.value, 10) : null,
        device_role_name: label(roleEl),
        tenant_id: (tenantEl && tenantEl.value) ? parseInt(tenantEl.value, 10) : null,
        tenant_name: label(tenantEl),
        planning_data: railPlacementData(),
    };
}

// Show the planned attribution on the tile straight away.
//
// The hover card reads data-* attributes, and the SERVER writes them from
// the projection -- which resolves override-first, but only on the next
// load. An override chosen in this session has to reach the same attributes
// itself, or the card would keep answering with the device's current values
// until a save + reload. The value being displaced is stashed as the "old"
// one, which is exactly what the card contrasts against.
function overrideAttr(content, attr, oldAttr, value, stash) {
    if (!value) { return; }
    var current = content.getAttribute(attr);
    if (current && current !== value && !content.getAttribute(oldAttr)) {
        content.setAttribute(oldAttr, current);
        stash.push([attr, oldAttr]);
    }
    content.setAttribute(attr, value);
}

function stampAttributionAttrs(widget, content) {
    if (!content) { return; }
    var stash = widget._rdAttrStash || [];
    overrideAttr(content, "data-role-name", "data-old-role",
                 widget.device_role_name, stash);
    overrideAttr(content, "data-tenant-name", "data-old-tenant",
                 widget.tenant_name, stash);
    widget._rdAttrStash = stash;
    stampPlanningAttr(widget, content);
}

// Undo exactly what stampAttributionAttrs displaced -- never the server's
// own "old" attributes, which describe a move it already knows about.
function restoreAttributionAttrs(widget, content) {
    if (!content) { return; }
    (widget._rdAttrStash || []).forEach(function (pair) {
        var old = content.getAttribute(pair[1]);
        if (old !== null) {
            content.setAttribute(pair[0], old);
            content.removeAttribute(pair[1]);
        }
    });
    widget._rdAttrStash = null;
    stampPlanningAttr(widget, content);
}

// Stamp the rail onto a tile that has just BECOME a move. A move's role /
// tenant / planning fields are planned OVERRIDES -- "this device becomes
// that when it lands" -- so only fields the rail actually carries are
// written, and only into slots the tile has not already filled itself.
// Leaving the rail empty therefore keeps a plain reposition a plain
// reposition.
function applyRailToMove(widget) {
    var rail = railAttribution();
    if (rail.device_role_id != null && widget.device_role_id == null) {
        widget.device_role_id = rail.device_role_id;
        widget.device_role_name = rail.device_role_name;
    }
    if (rail.tenant_id != null && widget.tenant_id == null) {
        widget.tenant_id = rail.tenant_id;
        widget.tenant_name = rail.tenant_name;
    }
    if (Object.keys(rail.planning_data).length
            && !Object.keys(widget.planning_data || {}).length) {
        widget.planning_data = rail.planning_data;
    }
}

// ...and take it back off when the tile lands where it started. The device
// is not moving after all, so the design has nothing to say about it.
// Safe to clear outright: only a tile that began the session as `existing`
// reaches this path, and such a tile never carries a saved override.
function clearRailFromMove(widget) {
    widget.device_role_id = null;
    widget.device_role_name = "";
    widget.tenant_id = null;
    widget.tenant_name = "";
    widget.planning_data = null;
}

renderPlacementRail();

// The rack's real PowerFeeds + this design's planned DesignPowerFeeds, for
// the bind-to-feed picker (real first, then planned). Never writes.
function fetchFeeds(rackId) {
    rdTrace("feed.fetch", { rackId: rackId });
    return fetch(apiFeedsUrl() + "?rack_id=" + encodeURIComponent(rackKeyToServer(rackId)), {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
    }).then(function (resp) { return resp.ok ? resp.json() : null; })
        .then(function (data) { return data || { real: [], planned: [] }; })
        .catch(function () { return { real: [], planned: [] }; });
}

// Upsert a planned DesignPowerFeed (docs/pdu-distribution-spec.md §6.1) --
// the "define planned feed" fallback, always available in the bind dialog.
function postPlannedFeed(rackId, feed) {
    // T1.5c (D31): rackId is the DOM's colon-free rack_key() ("r-<pk>"/
    // "p-<pk>") -- parseInt would collide a PlannedRack and a same-pk'd
    // dcim.Rack (D28) instead of the server's parse_real_rack_id correctly
    // rejecting a "p:<pk>" as "no such rack." Convert to the server's colon
    // form, don't coerce to a number.
    var body = {
        rack_id: rackKeyToServer(rackId),
        name: feed.name, voltage: feed.voltage, amperage: feed.amperage,
        phase: feed.phase, supply: feed.supply,
    };
    return fetch(apiPlannedFeedUrl(), {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify(body),
    }).then(function (resp) {
        if (!resp.ok) { throw new Error("HTTP " + resp.status); }
        return resp.json();
    });
}

// A rack's real PDU devices, for the "reference a PDU" custom-fields source
// (docs/pdu-distribution-spec.md §6): a planned PDU can inherit its cf live
// from one of these via power_source_device. Read-only against core dcim.
function fetchRackPdus(rackId) {
    // T1.5c (D31): this hits core NetBox's OWN dcim API, which has no idea
    // what a PlannedRack is and wants a plain dcim.Rack int pk -- not this
    // plugin's rack_key() form. A planned rack can never have a real PDU
    // (no dcim.Rack row exists for it), so short-circuit rather than ask
    // core to filter by a nonsense value.
    var realPk = rackDomIdToRealPk(rackId);
    if (realPk == null) { return Promise.resolve([]); }
    var url = "/api/dcim/devices/?rack_id=" + encodeURIComponent(realPk)
        + "&role=pdu&role=unmanageable-pdu&brief=1&limit=200";
    rdTrace("pdu.cf.fetchpdus", { rackId: rackId });
    return fetch(url, {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
    }).then(function (resp) { return resp.ok ? resp.json() : null; })
        .then(function (data) { return (data && data.results) || []; })
        .catch(function () { return []; });
}

function fetchPowerSource(rackId, kind, feed) {
    var qs = "rack_id=" + encodeURIComponent(rackKeyToServer(rackId)) + "&kind=" + encodeURIComponent(kind);
    if (feed) { qs += "&feed=" + encodeURIComponent(feed); }
    rdTrace("dist.powersource.fetch", { rackId: rackId, kind: kind, feed: feed || null });
    return fetch(API_BASE + "power-source/?" + qs, {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
    }).then(function (resp) { return resp.ok ? resp.json() : null; })
        .catch(function () { return null; });
}

// A feed change moves the rack's CAPACITY but mutates no tile, so the
// heatmap's MutationObserver never sees it. Ask it to pull fresh numbers
// (capacity from the server, chips from the distribution engine) so the bar
// updates without a layout Save or a page reload.
function refreshLivePower() {
    var hm = window.NbxRdPowerHeatmap;
    if (hm && typeof hm.refresh === "function") { hm.refresh(); }
}

// Clone a source rack's feeds onto this rack as PLANNED feeds (the other
// half of "copy from rack": the supply itself, not just the custom fields).
// Writes only DesignPowerFeed rows; upserts by rack+name server-side.
function postCopyFeeds(rackId, sourceRackId) {
    rdTrace("dist.copyfeeds.save", { rackId: rackId, sourceRackId: sourceRackId });
    return fetch(API_BASE + "copy-feeds/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({
            // T1.5c (D31): colon form for the server, not a bare parsed int
            // -- see postPlannedFeed above.
            rack_id: rackKeyToServer(rackId),
            source_rack_id: rackKeyToServer(sourceRackId),
        }),
    }).then(function (resp) {
        if (!resp.ok) { throw new Error("HTTP " + resp.status); }
        return resp.json();
    });
}

// This rack's PLANNED feeds (DesignPowerFeed rows), for the rack power
// dialog's "current supply" list. Read-only.
function fetchPlannedFeeds(rackId) {
    return fetch(apiPlannedFeedUrl() + "?rack_id=" + encodeURIComponent(rackKeyToServer(rackId)), {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
    }).then(function (resp) { return resp.ok ? resp.json() : []; })
        .catch(function () { return []; });
}

// Delete ONE planned feed. Answers with how many planned PDUs it unbound --
// a feed is the thing a PDU's breaker is sized from, so removing one is not
// a silent act and the dialog says what it cost.
function deletePlannedFeed(feedId) {
    rdTrace("dist.plannedfeed.delete", { feedId: feedId });
    return fetch(apiPlannedFeedUrl(), {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        body: JSON.stringify({ feed_id: parseInt(feedId, 10) }),
    }).then(function (resp) {
        if (!resp.ok) { throw new Error("HTTP " + resp.status); }
        return resp.json();
    });
}

function postRackPower(rackId, powerConfig) {
    rdTrace("dist.rackpower.save", {
        rackId: rackId, source: powerConfig ? powerConfig.source : null,
    });
    return fetch(apiRackPowerUrl(), {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": getCsrfToken() },
        // T1.5c (D31): colon form for the server -- see postPlannedFeed above.
        body: JSON.stringify({ rack_id: rackKeyToServer(rackId), power_config: powerConfig }),
    }).then(function (resp) {
        if (!resp.ok) { throw new Error("HTTP " + resp.status); }
        return resp.json();
    });
}

function getRackPower(rackId) {
    return fetch(apiRackPowerUrl() + "?rack_id=" + encodeURIComponent(rackKeyToServer(rackId)), {
        credentials: "same-origin",
        headers: { "Accept": "application/json" },
    }).then(function (resp) { return resp.ok ? resp.json() : null; })
        .catch(function () { return null; });
}

// PDU detection. The backend's authoritative signal is the device role's
// SLUG (distribution_example.PDU_ROLE_SLUGS = "pdu"/"unmanageable-pdu"),
// mirrored here verbatim. A RELOADED add carries the real `role_slug` from
// the server (views.py _slot_to_widget -> projection._slot_role_slug), so
// that check is exact. A brand-new drag-in has no placement yet -- only
// the palette role SELECT's display name and id are available client-side
// (NetBox's DynamicModelChoiceField/TomSelect keeps no slug, only
// id/display -- confirmed against project-static/src/select/classes/
// dynamicTomSelect.ts), so the role's name is slugified the same way
// Django's slugify would (lowercase, non-alnum runs -> '-') and matched
// against the same set; the proposed name / device-type label containing
// "pdu" is a last-resort fallback (the backend already relies on the same
// "...-pdu-...-<letter><number>" naming convention to parse a PDU's feed).
var PDU_ROLE_SLUGS = ["pdu", "unmanageable-pdu"];
function slugifyLike(name) {
    return (name || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}
function looksLikePdu(roleSlug, roleName, proposedName, typeLabel) {
    if (roleSlug && PDU_ROLE_SLUGS.indexOf(roleSlug.toLowerCase()) !== -1) { return true; }
    if (roleName && PDU_ROLE_SLUGS.indexOf(slugifyLike(roleName)) !== -1) { return true; }
    if (proposedName && /pdu/i.test(proposedName)) { return true; }
    if (typeLabel && /pdu/i.test(typeLabel)) { return true; }
    return false;
}

// The feed leg (e.g. "a1") from a PDU's name suffix -- same convention
// distribution_example._feed_of parses server-side -- used only to SEED
// the copy-from-rack feed input; the user can always override it.
function guessFeedFromName(name) {
    var m = /([a-zA-Z])(\d+)\s*$/.exec((name || "").trim());
    return m ? (m[1].toLowerCase() + m[2]) : "";
}

// ---- Auto-bind a freshly added PDU to a feed --------------------------
//
// Binding every PDU by hand is the single most repetitive gesture in the
// editor: stamp a ToR template across ten racks and that is twenty trips
// through the bind dialog, each one picking the only sensible answer. So an
// added PDU binds ITSELF whenever the rack already has feeds, and the dialog
// only opens when the choice is genuinely ambiguous (no feeds, or none left).
// The ⚡ button stays, so any automatic choice is one click from being
// overridden.

// The leg letter a FEED name carries: "R1-A" -> "a", "ams1-r3 b2" -> "b".
// A separator before the letter is required, so a name that merely happens
// to end in a letter ("E2E Feed 4f2a1b") is not read as a leg.
function feedLegLetter(name) {
    var m = /[-_ ]([a-z])\d*\s*$/i.exec(String(name || "").trim());
    return m ? m[1].toLowerCase() : "";
}

// Which feeds the rack's other PDUs have already claimed. `peers` is the
// caller's live widget list (rack.js passes its `state` widgets); each entry
// contributes whichever of the two binding fields it carries.
function takenFeedIds(peers) {
    var taken = { real: {}, planned: {} };
    (peers || []).forEach(function (w) {
        if (!w) { return; }
        if (w.real_power_feed_id != null) { taken.real[w.real_power_feed_id] = true; }
        if (w.planned_power_feed_id != null) { taken.planned[w.planned_power_feed_id] = true; }
    });
    return taken;
}

// The feed an added PDU should take, or null to hand the choice back to the
// user. Real feeds rank above the design's own planned feeds, which rank
// above feeds inherited from an ancestor design -- but a leg-letter match
// beats all of that, because "-a1 goes on feed A" is what the planner means
// even when a redundant PDU already sits on that leg.
function pickFeedForPdu(pduName, data, peers) {
    var taken = takenFeedIds(peers);
    var candidates = [];
    ((data && data.real) || []).forEach(function (f) {
        candidates.push({ source: "real", feed: f, rank: 0 });
    });
    ((data && data.planned) || []).forEach(function (f) {
        candidates.push({ source: "planned", feed: f, rank: f.inherited ? 2 : 1 });
    });
    if (!candidates.length) { return null; }
    candidates.sort(function (a, b) { return a.rank - b.rank; });

    var wanted = (guessFeedFromName(pduName) || "").charAt(0);
    if (wanted) {
        var matched = candidates.filter(function (c) {
            return feedLegLetter(c.feed.name) === wanted;
        });
        if (matched.length) {
            var free = matched.filter(function (c) { return !taken[c.source][c.feed.id]; });
            return free.length ? free[0] : matched[0];
        }
    }
    var untaken = candidates.filter(function (c) { return !taken[c.source][c.feed.id]; });
    return untaken.length ? untaken[0] : null;
}

// Bind `widget` to the rack's obvious feed. Resolves with the chosen
// {source, feed} or null -- a falsy result is the caller's signal to open
// showPduPowerDialog instead. Never rejects: a failed fetch is just "no
// automatic answer", same as a rack without feeds.
function autoBindPduFeed(widget, content, ctx) {
    var rackId = (ctx || {}).rackId;
    var peers = (ctx || {}).peers || [];
    return fetchFeeds(rackId).then(function (data) {
        var picked = pickFeedForPdu(widget.proposed_name || widget.label, data, peers);
        if (!picked) {
            rdTrace("feed.autobind.none", { rackId: rackId, pduName: widget.proposed_name });
            return null;
        }
        if (picked.source === "real") {
            widget.real_power_feed_id = picked.feed.id;
            widget.planned_power_feed_id = null;
        } else {
            widget.planned_power_feed_id = picked.feed.id;
            widget.real_power_feed_id = null;
        }
        var btn = content && content.querySelector
            ? content.querySelector(".nbx-rd-power-btn") : null;
        if (btn) {
            btn.classList.add("has-config");
            btn.title = "PDU power — bound to " + picked.feed.name + " (click to change)";
        }
        rdTrace("feed.autobind.bind", {
            rackId: rackId, pduName: widget.proposed_name,
            feedId: picked.feed.id, feedName: picked.feed.name, feedSource: picked.source,
        });
        return picked;
    }).catch(function () { return null; });
}

// Every rack currently rendered in the editor, for the "copy from rack"
// selector: {id, name} pairs read straight off the live DOM (no fetch).
function racksInDom() {
    return Array.prototype.slice.call(document.querySelectorAll(".nbx-rd-rack-block"))
        .map(function (b) {
            var titleEl = b.querySelector(".nbx-rd-rack-block-title a");
            return {
                id: b.getAttribute("data-rack-id"),
                name: titleEl ? titleEl.textContent.trim() : ("rack " + b.getAttribute("data-rack-id")),
            };
        });
}

function rackOptionsHtml(racks, selectedId) {
    return racks.map(function (r) {
        var safe = String(r.name).replace(/&/g, "&amp;").replace(/</g, "&lt;");
        var sel = (selectedId != null && String(r.id) === String(selectedId)) ? " selected" : "";
        return '<option value="' + r.id + '"' + sel + ">" + safe + "</option>";
    }).join("");
}

// Shared transition-safe show/hide wiring (identical to showMoveNameDialog
// above): returns {modal, requestHide} once `overlay` is in the DOM. `decided`
// guards against a hidden.bs.modal firing after Confirm already ran.
function wireModal(overlay, decidedRef) {
    var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
    var modal = ctor ? new ctor(overlay) : null;
    var shownDone = false, hidePending = false;
    overlay.addEventListener("shown.bs.modal", function () {
        shownDone = true;
        if (hidePending && modal) { modal.hide(); }
    });
    overlay.addEventListener("hide.bs.modal", function () {
        if (overlay.contains(document.activeElement)) { document.activeElement.blur(); }
    });
    overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
        btn.addEventListener("click", function () {
            decidedRef.cancelled = true;
            if (modal) {
                if (shownDone) { modal.hide(); } else { hidePending = true; }
            } else { overlay.remove(); }
        });
    });
    overlay.addEventListener("hidden.bs.modal", function () { overlay.remove(); });
    return {
        show: function () {
            if (modal) { modal.show(); } else { overlay.remove(); }
        },
        requestHide: function () {
            if (!modal) { overlay.remove(); return; }
            if (shownDone) { modal.hide(); } else { hidePending = true; }
        },
    };
}

// Render one feed's picker label: "name — V/A/phase". `phase` is the
// native string ("single-phase"/"three-phase") the feeds/ endpoint returns.
function feedRowLabel(f) {
    var phaseLabel = f.phase === "three-phase" ? "3φ" : "1φ";
    var bits = [];
    if (f.voltage != null) { bits.push(f.voltage + "V"); }
    if (f.amperage != null) { bits.push(f.amperage + "A"); }
    bits.push(phaseLabel);
    return (f.name || "(unnamed)") + " — " + bits.join("/");
}

// The bind-to-feed dialog (docs/pdu-distribution-spec.md §6.2/§6.3): a
// newly-named (or reopened) PDU add binds to one of the rack's real
// PowerFeeds or this design's planned DesignPowerFeeds, replacing the old
// manual V/A/phase entry. `widget` is the live widget object stashed in
// editor state (setting widget.real_power_feed_id / .planned_power_feed_id
// here is what buildSavePayload later reads); `content` is the tile's
// .grid-stack-item-content (for the has-config button cue); `ctx` =
// {rackId}. widget.power_config is left untouched -- it is cf-bridge only
// now (§6.2); the feed binding rides its own two fields.
function showPduPowerDialog(widget, content, ctx) {
    var rackId = ctx.rackId;
    var currentReal = widget.real_power_feed_id != null ? widget.real_power_feed_id : null;
    var currentPlanned = widget.planned_power_feed_id != null ? widget.planned_power_feed_id : null;
    // Custom-field capture (docs/pdu-distribution-spec.md §6): a planned PDU's
    // cf come EITHER from referencing a real PDU (power_source_device, cf read
    // live) OR from manual entry (power_config.custom_fields). Both paths only
    // ever feed a user's distribution script -- the native "none"/"builtin"
    // engines never read a custom field -- so this whole section is
    // script-mode-only. The manual sub-form is further driven by
    // PLANNING_FIELDS.pdu -- empty => reference-only.
    var pduScriptMode = DISTRIBUTION_MODE === "script";
    var pduFields = pduScriptMode ? ((PLANNING_FIELDS && PLANNING_FIELDS.pdu) || []) : [];
    var currentSourceDev = widget.power_source_device_id != null ? widget.power_source_device_id : null;
    var currentManualCf = (widget.power_config && widget.power_config.custom_fields) || null;
    // Existing bindings stay visible/editable even outside "script" mode --
    // hiding a value someone already set would silently strand it.
    var showCfSection = pduFields.length > 0 || currentSourceDev != null;
    rdTrace("feed.bind.open", {
        rackId: rackId, label: widget.label,
        currentReal: currentReal, currentPlanned: currentPlanned,
        currentSourceDev: currentSourceDev,
    });

    var cfSectionHtml = "";
    if (!showCfSection && !pduScriptMode) {
        cfSectionHtml =
            '<hr class="my-2"><div class="form-text">'
            + 'Custom-field capture is hidden because distribution_mode is not "script" '
            + '— only a distribution script reads them.</div>';
    } else if (showCfSection) {
        var pduRacks = racksInDom();
        var manualFieldsHtml = pduFields.map(function (f) {
            var safeLabel = String(f.label || f.key).replace(/&/g, "&amp;").replace(/</g, "&lt;");
            return '<div class="col"><label class="form-label small mb-0">' + safeLabel + "</label>"
                + planningFieldInputHtml(f) + "</div>";
        }).join("");
        cfSectionHtml =
            '<hr class="my-2"><div class="text-muted small text-uppercase">Custom fields</div>'
            + '<div class="form-check">'
            + '<input class="form-check-input" type="radio" name="nbx-rd-pducf-mode" id="nbx-rd-pducf-ref" value="reference"'
            + (currentManualCf ? "" : " checked") + ">"
            + '<label class="form-check-label" for="nbx-rd-pducf-ref">Reference a PDU</label>'
            + "</div>"
            + '<div class="nbx-rd-pducf-ref-fields ms-4 mb-2">'
            + '<div class="row g-2 align-items-center">'
            + '<div class="col-auto"><select class="form-select form-select-sm nbx-rd-pducf-rack">'
            + rackOptionsHtml(pduRacks, rackId) + "</select></div>"
            + '<div class="col"><select class="form-select form-select-sm nbx-rd-pducf-pdu">'
            + '<option value="">— select a PDU —</option></select></div>'
            + "</div>"
            + '<div class="form-text nbx-rd-pducf-ref-status"></div>'
            + "</div>"
            + (pduFields.length
                ? ('<div class="form-check mt-1">'
                    + '<input class="form-check-input" type="radio" name="nbx-rd-pducf-mode" id="nbx-rd-pducf-manual" value="manual"'
                    + (currentManualCf ? " checked" : "") + ">"
                    + '<label class="form-check-label" for="nbx-rd-pducf-manual">Enter manually</label>'
                    + "</div>"
                    + '<div class="nbx-rd-pducf-manual-fields ms-4"><div class="row g-2 align-items-end">'
                    + manualFieldsHtml + "</div></div>")
                : "");
    }

    var overlay = document.createElement("div");
    overlay.className = "modal fade nbx-rd-power-modal";
    overlay.setAttribute("tabindex", "-1");
    overlay.innerHTML =
        '<div class="modal-dialog modal-dialog-centered">'
        + '<div class="modal-content">'
        + '<div class="modal-header">'
        + '<h5 class="modal-title">' + "Bind PDU to a power feed" + "</h5>"
        + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
        + "</div>"
        + '<div class="modal-body">'
        + '<div class="nbx-rd-feed-list"><div class="text-muted small">Loading feeds…</div></div>'
        + '<div class="mt-2">'
        + '<button type="button" class="btn btn-sm btn-outline-secondary nbx-rd-feed-new-toggle">'
        + '<i class="mdi mdi-plus" aria-hidden="true"></i> Define planned feed</button>'
        + "</div>"
        + '<div class="nbx-rd-feed-new-form mt-2" style="display:none;">'
        + '<div class="row g-2 align-items-center">'
        + '<div class="col"><input type="text" class="form-control form-control-sm nbx-rd-feed-new-name" placeholder="Name, e.g. Feed A"></div>'
        + '<div class="col"><input type="number" class="form-control form-control-sm nbx-rd-feed-new-voltage" placeholder="V" value="230"></div>'
        + '<div class="col"><input type="number" class="form-control form-control-sm nbx-rd-feed-new-amperage" placeholder="A" value="16"></div>'
        + '<div class="col"><select class="form-select form-select-sm nbx-rd-feed-new-phase">'
        + '<option value="single-phase">Single-phase</option><option value="three-phase">Three-phase</option>'
        + "</select></div>"
        + '<div class="col"><select class="form-select form-select-sm nbx-rd-feed-new-supply">'
        + '<option value="ac">AC</option><option value="dc">DC</option></select></div>'
        + '<div class="col-auto"><button type="button" class="btn btn-sm btn-outline-primary nbx-rd-feed-new-create">Create</button></div>'
        + "</div>"
        + '<div class="form-text nbx-rd-feed-new-status"></div>'
        + "</div>"
        + cfSectionHtml
        + "</div>"
        + '<div class="modal-footer">'
        + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
        + '<button type="button" class="btn btn-sm btn-primary" data-rd-pdu-confirm disabled>Bind</button>'
        + "</div>"
        + "</div></div>";
    document.body.appendChild(overlay);

    var listEl = overlay.querySelector(".nbx-rd-feed-list");
    var newToggle = overlay.querySelector(".nbx-rd-feed-new-toggle");
    var newForm = overlay.querySelector(".nbx-rd-feed-new-form");
    var newName = overlay.querySelector(".nbx-rd-feed-new-name");
    var newVoltage = overlay.querySelector(".nbx-rd-feed-new-voltage");
    var newAmperage = overlay.querySelector(".nbx-rd-feed-new-amperage");
    var newPhase = overlay.querySelector(".nbx-rd-feed-new-phase");
    var newSupply = overlay.querySelector(".nbx-rd-feed-new-supply");
    var newCreate = overlay.querySelector(".nbx-rd-feed-new-create");
    var newStatus = overlay.querySelector(".nbx-rd-feed-new-status");
    var confirmBtn = overlay.querySelector("[data-rd-pdu-confirm]");

    var selected = null;  // {source: "real"|"planned", id, name, voltage, amperage, phase, supply}

    // Confirm is enabled by ANY of: a feed picked, a PDU referenced, or a
    // manual cf field filled -- a user may set only cf, only a feed, or
    // both (docs/pdu-distribution-spec.md §6).
    function anyManualFilled() {
        var filled = false;
        cfManualInputs.forEach(function (inp) {
            if (String(inp.value || "").trim() !== "") { filled = true; }
        });
        return filled;
    }
    function refreshConfirmEnabled() {
        var cfOk = showCfSection && (
            (cfPduSel && cfPduSel.value) || anyManualFilled()
        );
        confirmBtn.disabled = !(selected || cfOk);
    }

    function selectFeed(source, feed) {
        selected = {
            source: source, id: feed.id, name: feed.name,
            voltage: feed.voltage, amperage: feed.amperage,
            phase: feed.phase, supply: feed.supply,
        };
        refreshConfirmEnabled();
        listEl.querySelectorAll("input[name=nbx-rd-feed-pick]").forEach(function (r) {
            r.checked = (r.getAttribute("data-source") === source
                && String(r.getAttribute("data-id")) === String(feed.id));
        });
    }

    function renderList(data) {
        var real = (data && data.real) || [];
        var planned = (data && data.planned) || [];
        if (!real.length && !planned.length) {
            listEl.innerHTML = '<div class="text-muted small">No feeds yet on this rack — define a planned feed below.</div>';
            return;
        }
        var html = "";
        function section(title, feeds, source) {
            if (!feeds.length) { return ""; }
            var rows = feeds.map(function (f) {
                var checked = (source === "real" && currentReal === f.id)
                    || (source === "planned" && currentPlanned === f.id);
                // A design-chain child sees its approved ancestors' planned
                // feeds too (docs/design-chains.md); those entries carry
                // `inherited`/`design_id`/`design_name` (absent for own
                // feeds and for an unchained design's whole response), so a
                // planner can tell "Feed A from the network design" apart
                // from an identically-named feed of their own.
                var inheritedTag = f.inherited
                    ? ' <span class="text-muted small nbx-rd-feed-inherited-tag">— from '
                        + String(f.design_name || "another design").replace(/&/g, "&amp;").replace(/</g, "&lt;")
                        + "</span>"
                    : "";
                return '<label class="form-check' + (f.inherited ? " nbx-rd-feed-inherited" : "") + '">'
                    + '<input class="form-check-input" type="radio" name="nbx-rd-feed-pick" '
                    + 'data-source="' + source + '" data-id="' + f.id + '"'
                    + (checked ? " checked" : "") + ">"
                    + '<span class="form-check-label">' + feedRowLabel(f) + inheritedTag + "</span>"
                    + "</label>";
            }).join("");
            return '<div class="nbx-rd-feed-section"><div class="text-muted small text-uppercase">'
                + title + "</div>" + rows + "</div>";
        }
        html += section("Real feeds", real, "real");
        // Own planned feeds render exactly as before, unconditionally
        // first (the API already puts them first). Inherited feeds --
        // absent entirely for an unchained design -- get their own
        // section per source ancestor, oldest-first, so the boundary
        // between "mine" and "inherited" is a section break rather than
        // something to spot within one flat list.
        var ownPlanned = planned.filter(function (f) { return !f.inherited; });
        var inheritedPlanned = planned.filter(function (f) { return !!f.inherited; });
        html += section("Planned feeds", ownPlanned, "planned");
        var inheritedGroups = [];
        var byDesign = {};
        inheritedPlanned.forEach(function (f) {
            var key = String(f.design_id);
            if (!byDesign[key]) {
                byDesign[key] = { name: f.design_name, feeds: [] };
                inheritedGroups.push(byDesign[key]);
            }
            byDesign[key].feeds.push(f);
        });
        inheritedGroups.forEach(function (g) {
            var safeName = String(g.name || "ancestor design").replace(/&/g, "&amp;").replace(/</g, "&lt;");
            html += section("Planned feeds — inherited from " + safeName, g.feeds, "planned");
        });
        listEl.innerHTML = html;
        listEl.querySelectorAll("input[name=nbx-rd-feed-pick]").forEach(function (r) {
            r.addEventListener("change", function () {
                var source = r.getAttribute("data-source");
                var id = parseInt(r.getAttribute("data-id"), 10);
                var pool = source === "real" ? real : planned;
                var feed = pool.filter(function (f) { return f.id === id; })[0];
                if (feed) { selectFeed(source, feed); }
            });
        });
        // Preselect the widget's current binding on reopen.
        var preselected = listEl.querySelector("input[name=nbx-rd-feed-pick]:checked");
        if (preselected) { preselected.dispatchEvent(new Event("change")); }
    }

    fetchFeeds(rackId).then(function (data) {
        rdTrace("feed.bind.list", {
            rackId: rackId,
            realCount: (data.real || []).length, plannedCount: (data.planned || []).length,
        });
        renderList(data);
    });

    newToggle.addEventListener("click", function () {
        var showing = newForm.style.display !== "none";
        newForm.style.display = showing ? "none" : "";
    });

    newCreate.addEventListener("click", function () {
        var name = (newName.value || "").trim();
        if (!name) { newStatus.textContent = "Name is required."; return; }
        var feed = {
            name: name,
            voltage: newVoltage.value !== "" ? parseInt(newVoltage.value, 10) : 230,
            amperage: newAmperage.value !== "" ? parseInt(newAmperage.value, 10) : 16,
            phase: newPhase.value, supply: newSupply.value,
        };
        newStatus.textContent = "Saving…";
        postPlannedFeed(rackId, feed).then(function (saved) {
            rdTrace("feed.planned.create", { rackId: rackId, feed: saved });
            newStatus.textContent = "Created " + saved.name + ".";
            newForm.style.display = "none";
            currentPlanned = saved.id;
            fetchFeeds(rackId).then(renderList);
            // A new feed changes the rack's capacity; pull it live so the bar
            // does not sit on the pre-feed denominator until the next Save.
            refreshLivePower();
        }).catch(function (err) {
            newStatus.textContent = "Could not create feed: " + String(err);
        });
    });

    // --- custom-fields section wiring (reference a PDU / manual entry) ----
    var cfRackSel = overlay.querySelector(".nbx-rd-pducf-rack");
    var cfPduSel = overlay.querySelector(".nbx-rd-pducf-pdu");
    var cfRefStatus = overlay.querySelector(".nbx-rd-pducf-ref-status");
    var cfManualInputs = overlay.querySelectorAll(".nbx-rd-pducf-manual-fields .nbx-rd-rackpower-field");

    function pduCfMode() {
        var checked = overlay.querySelector("input[name=nbx-rd-pducf-mode]:checked");
        return checked ? checked.value : null;
    }

    function loadPdusInto(sel, forRackId, preselectId) {
        if (!sel) { return; }
        sel.innerHTML = '<option value="">— loading… —</option>';
        fetchRackPdus(forRackId).then(function (pdus) {
            var opts = '<option value="">— select a PDU —</option>' + pdus.map(function (d) {
                var nm = String(d.display || d.name || ("device " + d.id))
                    .replace(/&/g, "&amp;").replace(/</g, "&lt;");
                var sel2 = (preselectId != null && String(d.id) === String(preselectId)) ? " selected" : "";
                return '<option value="' + d.id + '"' + sel2 + ">" + nm + "</option>";
            }).join("");
            sel.innerHTML = opts;
            if (cfRefStatus) { cfRefStatus.textContent = pdus.length ? "" : "No PDUs in this rack."; }
            refreshConfirmEnabled();
        });
    }

    if (showCfSection && cfRackSel) {
        // Initial PDU list for the current rack; preselect the referenced PDU
        // if the widget already has one (reopen).
        loadPdusInto(cfPduSel, cfRackSel.value || rackId, currentSourceDev);
        cfRackSel.addEventListener("change", function () {
            loadPdusInto(cfPduSel, cfRackSel.value, null);
        });
        // Fill manual inputs from the widget's existing manual cf (reopen).
        if (currentManualCf) {
            cfManualInputs.forEach(function (inp) {
                var k = inp.getAttribute("data-field-key");
                if (k in currentManualCf && currentManualCf[k] != null) {
                    inp.value = currentManualCf[k];
                }
            });
        }
        if (cfPduSel) { cfPduSel.addEventListener("change", refreshConfirmEnabled); }
        cfManualInputs.forEach(function (inp) {
            inp.addEventListener("input", refreshConfirmEnabled);
        });
        overlay.querySelectorAll("input[name=nbx-rd-pducf-mode]").forEach(function (r) {
            r.addEventListener("change", refreshConfirmEnabled);
        });
        refreshConfirmEnabled();
    }

    var decidedRef = { cancelled: false };
    var wired = wireModal(overlay, decidedRef);

    overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
        btn.addEventListener("click", function () {
            rdTrace("feed.bind.cancel", { rackId: rackId, label: widget.label });
        });
    });

    confirmBtn.addEventListener("click", function () {
        if (selected) {
            if (selected.source === "real") {
                widget.real_power_feed_id = selected.id;
                widget.planned_power_feed_id = null;
            } else {
                widget.planned_power_feed_id = selected.id;
                widget.real_power_feed_id = null;
            }
            rdTrace("feed.bind.confirm", {
                rackId: rackId, pduName: widget.label,
                feedId: selected.id, feedName: selected.name, feedSource: selected.source,
                voltage: selected.voltage, amperage: selected.amperage, phase: selected.phase,
            });
        }
        // Custom-field capture (docs/pdu-distribution-spec.md §6): reference
        // a real PDU (live cf) XOR manual entry -- each mode clears the other.
        if (showCfSection) {
            var mode = pduCfMode();
            if (mode === "reference") {
                widget.power_source_device_id = (cfPduSel && cfPduSel.value)
                    ? parseInt(cfPduSel.value, 10) : null;
                widget.power_config = null;
            } else if (mode === "manual") {
                var cf = {};
                cfManualInputs.forEach(function (inp) {
                    var k = inp.getAttribute("data-field-key");
                    var v = inp.value;
                    if (v !== "" && v != null) { cf[k] = v; }
                });
                widget.power_config = Object.keys(cf).length
                    ? { source: "manual", custom_fields: cf } : null;
                widget.power_source_device_id = null;
            }
            rdTrace("pdu.cf.confirm", {
                rackId: rackId, mode: mode,
                sourceDev: widget.power_source_device_id,
                manualCf: (widget.power_config && widget.power_config.custom_fields) || null,
            });
        }
        if (content) {
            var btn = content.querySelector(".nbx-rd-power-btn");
            if (btn) { btn.classList.add("has-config"); }
        }
        markDirty();
        wired.requestHide();
    });

    wired.show();
}

// One <input>/<select> for a planning_fields schema entry (docs/pdu-
// distribution-spec.md §5): `type` in {number, text, choice}. Never a
// hardcoded cf name -- `f.key` drives both the DOM lookup and the
// custom_fields dict key written on confirm.
// A placement field bound to a device custom field arrives with that field's
// own NetBox type (integer, select, object, ...), so each gets the input that
// type needs; the server coerces and validates whatever is submitted.
function planningFieldInputHtml(f, cssClass) {
    var cls = cssClass || "nbx-rd-rackpower-field";
    function esc(s) { return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/"/g, "&quot;"); }
    var attrs = ' data-field-key="' + esc(f.key) + '"';
    var labels = f.choice_labels || {};

    if (f.type === "choice" || f.type === "select" || f.type === "multiselect") {
        var multi = f.type === "multiselect";
        var opts = (multi ? "" : '<option value="">—</option>') + (f.choices || []).map(function (c) {
            return '<option value="' + esc(c) + '">' + esc(labels[c] || c) + "</option>";
        }).join("");
        return '<select class="form-select form-select-sm ' + cls + '"' + attrs
            + (multi ? " multiple size=\"3\"" : "") + ">" + opts + "</select>";
    }
    if (f.type === "boolean") {
        return '<select class="form-select form-select-sm ' + cls + '"' + attrs + ">"
            + '<option value="">—</option><option value="true">Yes</option>'
            + '<option value="false">No</option></select>';
    }
    if (f.type === "object" || f.type === "multiobject") {
        var many = f.type === "multiobject";
        return '<div class="nbx-rd-object-pick d-flex flex-column gap-1">'
            + '<input type="search" class="form-control form-control-sm" data-rd-object-search'
            + ' placeholder="Search ' + esc(f.object_type || "objects") + "…\">"
            + '<select class="form-select form-select-sm ' + cls + '"' + attrs
            + ' data-api-url="' + esc(f.api_url || "") + '"' + (many ? " multiple size=\"3\"" : "")
            + ">" + (many ? "" : '<option value="">—</option>') + "</select></div>";
    }
    if (f.type === "longtext" || f.type === "json") {
        return '<textarea rows="2" class="form-control form-control-sm ' + cls + '"' + attrs
            + (f.type === "json" ? ' placeholder="{&quot;key&quot;: &quot;value&quot;}"' : "")
            + "></textarea>";
    }
    var type = "text";
    var extra = "";
    if (f.type === "number" || f.type === "decimal") { type = "number"; extra = ' step="any"'; }
    if (f.type === "integer") { type = "number"; extra = ' step="1"'; }
    if (f.min != null) { extra += ' min="' + f.min + '"'; }
    if (f.max != null) { extra += ' max="' + f.max + '"'; }
    if (f.type === "date") { type = "date"; }
    if (f.type === "datetime") { type = "datetime-local"; }
    if (f.type === "url") { type = "url"; }
    return '<input type="' + type + '" class="form-control form-control-sm ' + cls + '"'
        + attrs + extra + ">";
}

// ---- Manual per-PSU feed-leg override (this file's own feature) --------
// A device not yet cabled (a planned add, or one this design moves) is
// attributed to legs automatically by the distribution engine: one PSU ->
// leg a, two or more -> a+b (docs/pdu-distribution-spec.md). A planner who
// wants a specific device on C/D instead sets it here, per PSU, ordered by
// PSU index. Contract: `preferred_feed_legs` is a list of lowercase
// single-letter legs (e.g. ["b"], ["a","b"]), one entry per PSU in index
// order; empty/absent means "automatic". Stored on the widget and forwarded
// on save by buildRackPayload (rack.js); the server/model own everything
// past that.

// PSU count for a tile: the SAME "name:draw:conn|..." blob the hover card
// reads off data-power (stamped by the server for an existing/cabled
// device, or by the palette for a fresh catalog drop -- see palette.js).
// A tile with no power-port information at all still gets ONE select
// rather than none, per the agreed UX.
function tilePsuCount(content) {
    var raw = content && content.getAttribute("data-power");
    if (!raw) { return 1; }
    var n = raw.split("|").filter(function (s) { return s !== ""; }).length;
    return n > 0 ? n : 1;
}

// The tile's rack's distinct feed legs, `{letter, name}` sorted by letter --
// read from the same live-preferred distribution the power bar/chip strip
// use (power_heatmap.js), so this dialog never disagrees with what the rack
// is showing right now. [] when the rack has no distribution yet, or is not
// resolvable (window.NbxRdPowerHeatmap absent, e.g. a read-only elevation).
function tileRackLegs(content) {
    var block = content && content.closest(".nbx-rd-rack-block");
    var rackId = block && block.getAttribute("data-rack-id");
    if (!rackId || !window.NbxRdPowerHeatmap
            || typeof window.NbxRdPowerHeatmap.getLegs !== "function") {
        return [];
    }
    try {
        return window.NbxRdPowerHeatmap.getLegs(rackId) || [];
    } catch (e) {
        return [];
    }
}

// One <select> per PSU: "Automatic" plus every leg, labelled "A — Feed A".
function feedLegSelectHtml(legs, psuIndex) {
    var opts = '<option value="">Automatic</option>' + legs.map(function (l) {
        var safeName = String(l.name || "").replace(/&/g, "&amp;").replace(/</g, "&lt;");
        return '<option value="' + l.letter + '">' + l.letter.toUpperCase() + " — " + safeName
            + "</option>";
    }).join("");
    return '<div class="mb-2"><label class="form-label small mb-0">PSU ' + (psuIndex + 1)
        + "</label>"
        + '<select class="form-select form-select-sm nbx-rd-placement-leg-field" data-psu-index="'
        + psuIndex + '">' + opts + "</select></div>";
}

// ---- The per-tile planning-attributes dialog ---------------------------
// The values a planner sets on a PLANNED device, declared per deployment in
// the `placement_fields` config (planning_fields.py) and stored on the
// placement's planning_data. The rail supplies the sticky defaults; this
// dialog is where a single tile departs from them. Nothing here knows a
// custom-field name -- the schema drives the inputs, `f.key` drives the blob.
// The built-in Power section (feed-leg override, above) is independent of
// that schema -- it renders whenever the tile's rack has 2+ legs, even for
// a deployment that declares no placement_fields at all.
function showPlacementFieldsDialog(widget, content, kind) {
    var fields = placementFieldsFor(kind || widget.kind || "add");
    var legs = tileRackLegs(content);
    var showPower = legs.length >= 2;
    if (!fields.length && !showPower) { return; }
    var current = widget.planning_data || {};
    var currentLegs = Array.isArray(widget.preferred_feed_legs) ? widget.preferred_feed_legs : [];
    var psuCount = showPower ? tilePsuCount(content) : 0;

    var overlay = document.createElement("div");
    overlay.className = "modal fade nbx-rd-placement-modal";
    overlay.setAttribute("tabindex", "-1");
    var rowsHtml = fields.map(function (f) {
        var safeLabel = String(f.label || f.key).replace(/&/g, "&amp;").replace(/</g, "&lt;");
        return '<div class="mb-2"><label class="form-label small mb-0">' + safeLabel
            + (f.required ? ' <span class="text-danger">*</span>' : "")
            + "</label>" + planningFieldInputHtml(f, "nbx-rd-placement-field") + "</div>";
    }).join("");
    var powerHtml = "";
    if (showPower) {
        var selectsHtml = "";
        for (var i = 0; i < psuCount; i++) { selectsHtml += feedLegSelectHtml(legs, i); }
        powerHtml = '<div class="nbx-rd-placement-power mt-2 pt-2 border-top">'
            + '<div class="form-label small mb-1 fw-semibold">Power</div>'
            + selectsHtml + "</div>";
    }
    // An add's name lives in the tile's hidden .nbx-rd-name-input (rack.js).
    // On a 1U tile this dialog's button covers that input's pencil, so the
    // name is offered here as well and written back through the same input.
    var nameInput = (kind || widget.kind || "add") === "add" && content
        ? content.querySelector(".nbx-rd-name-input") : null;
    var nameHtml = nameInput
        ? '<div class="mb-2"><label class="form-label small mb-0">Name</label>'
            + '<input type="text" class="form-control form-control-sm nbx-rd-placement-name"></div>'
        : "";
    var title = (widget.proposed_name || widget.label || "device").replace(/</g, "&lt;");
    overlay.innerHTML =
        '<div class="modal-dialog modal-dialog-centered">'
        + '<div class="modal-content">'
        + '<div class="modal-header">'
        + '<h5 class="modal-title">Planning attributes — ' + title + "</h5>"
        + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
        + "</div>"
        + '<div class="modal-body">' + nameHtml + rowsHtml + powerHtml
        + '<div class="form-text">Carried on the planned device when this design is applied.</div>'
        + "</div>"
        + '<div class="modal-footer">'
        + '<button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>'
        + '<button type="button" class="btn btn-primary" data-rd-placement-confirm>Apply</button>'
        + "</div></div></div>";
    document.body.appendChild(overlay);

    // Pre-fill from whatever the tile already carries, so reopening the
    // dialog after a reload shows the stored values rather than the rail's.
    overlay.querySelectorAll(".nbx-rd-placement-field").forEach(function (input) {
        var key = input.getAttribute("data-field-key");
        if (key && current[key] != null && !input.hasAttribute("data-api-url")) {
            setPlacementFieldInput(input, current[key]);
        }
    });
    // Object pickers fill themselves: the stored ids are fetched with the
    // first page of results and come back selected.
    wireObjectPickers(overlay, current);
    overlay.querySelectorAll(".nbx-rd-placement-leg-field").forEach(function (sel) {
        var idx = parseInt(sel.getAttribute("data-psu-index"), 10);
        if (currentLegs[idx]) { sel.value = currentLegs[idx]; }
    });

    var nameField = overlay.querySelector(".nbx-rd-placement-name");
    if (nameField) { nameField.value = widget.proposed_name || ""; }

    var decided = { cancelled: false };
    var wired = wireModal(overlay, decided);
    overlay.querySelector("[data-rd-placement-confirm]").addEventListener("click", function () {
        var data = readPlacementFieldInputs(overlay, ".nbx-rd-placement-field");
        widget.planning_data = data;
        var legsSet = false;
        if (showPower) {
            var picked = Array.prototype.slice
                .call(overlay.querySelectorAll(".nbx-rd-placement-leg-field"))
                .sort(function (a, b) {
                    return parseInt(a.getAttribute("data-psu-index"), 10)
                        - parseInt(b.getAttribute("data-psu-index"), 10);
                })
                .map(function (sel) { return sel.value || ""; });
            // Two PSUs on one leg is not a plan the server accepts (the
            // model rejects a duplicate leg), and the live recompute then
            // dropped the device's draw without a word. Refuse it here.
            var chosen = picked.filter(function (v) { return v !== ""; });
            var dupError = overlay.querySelector(".nbx-rd-placement-leg-error");
            if (chosen.length !== new Set(chosen).size) {
                if (!dupError) {
                    dupError = document.createElement("div");
                    dupError.className = "nbx-rd-placement-leg-error text-danger small";
                    dupError.textContent = "Each PSU needs a different feed leg.";
                    overlay.querySelector(".nbx-rd-placement-power").appendChild(dupError);
                }
                return;
            }
            if (dupError) { dupError.remove(); }
            legsSet = picked.some(function (v) { return v !== ""; });
            if (legsSet) {
                widget.preferred_feed_legs = picked;
            } else {
                delete widget.preferred_feed_legs;
            }
        }
        if (nameField && nameField.value !== (widget.proposed_name || "")) {
            nameInput.value = nameField.value;
            nameInput.dispatchEvent(new Event("input", { bubbles: true }));
        }
        stampPlanningAttr(widget, content);
        if (widget._rdAttrStash === undefined) { widget._rdAttrStash = null; }
        if (content) {
            var btn = content.querySelector(".nbx-rd-placement-btn");
            if (btn) {
                btn.classList.toggle("has-config", Object.keys(data).length > 0 || legsSet);
            }
        }
        markDirty();
        wired.requestHide();
    });
    wired.show();
}

// Keep a tile's hover-card planning rows in step with its widget. The
// server stamps `data-planning` for every slot it renders (rack_block.html
// via the slot_planning filter); this is the in-session counterpart, for an
// add that does not exist server-side yet or whose values just changed in
// the dialog. Same JSON [[label, value], ...] shape, built from the same
// schema, so the hover card cannot tell the two apart.
function stampPlanningAttr(widget, content) {
    if (!content) { return; }
    var data = widget.planning_data || {};
    var pairs = PLACEMENT_FIELDS
        .filter(function (f) {
            var v = data[f.key];
            return v !== undefined && v !== null && v !== "" && !(Array.isArray(v) && !v.length);
        })
        .map(function (f) { return [f.label || f.key, planningDisplay(f, data[f.key])]; });
    if (pairs.length) {
        if (content.getAttribute("data-rd-planning-orig") === null) {
            // Remember what the server rendered before overwriting it, so
            // undoing the override can put it back verbatim.
            content.setAttribute("data-rd-planning-orig",
                                 content.getAttribute("data-planning") || "");
        }
        content.setAttribute("data-planning", JSON.stringify(pairs));
        return;
    }
    // The widget carries no override. That is NOT the same as "this tile has
    // nothing to show": the server writes an existing device's OWN custom
    // fields into the same attribute, and this function runs over every tile
    // on every settle pass. Clearing here wiped them off the whole rack
    // (user 2026-08-31). Only undo an override this session actually made.
    var original = content.getAttribute("data-rd-planning-orig");
    if (original === null) { return; }
    if (original) {
        content.setAttribute("data-planning", original);
    } else {
        content.removeAttribute("data-planning");
    }
    content.removeAttribute("data-rd-planning-orig");
}

// The tile affordance that opens it. Added to every `add` tile -- both a
// freshly dropped one and one rehydrated on load -- whenever the deployment
// declares any placement field for that kind.
// `kind` is the tile's EFFECTIVE kind. It has to be passed for a same-rack
// move: that tile keeps `widget.kind === "existing"` and signals the move
// through its CSS state alone, so the widget cannot answer for itself.
function attachPlacementFieldsButton(widget, content, kind) {
    if (!content || content.querySelector(".nbx-rd-placement-btn")) { return; }
    var hasFields = placementFieldsFor(kind || widget.kind || "add").length > 0;
    var hasPower = tileRackLegs(content).length >= 2;
    if (!hasFields && !hasPower) { return; }
    // A fresh drop inherits the rail defaults but has no server-rendered
    // attribute yet, so the hover card would come up empty until a reload.
    stampPlanningAttr(widget, content);
    var btn = document.createElement("button");
    btn.type = "button";
    var filled = Object.keys(widget.planning_data || {}).length > 0
        || (Array.isArray(widget.preferred_feed_legs) && widget.preferred_feed_legs.length > 0);
    btn.className = "nbx-rd-placement-btn" + (filled ? " has-config" : "");
    btn.title = "Planning attributes";
    btn.setAttribute("aria-label", "Planning attributes");
    btn.innerHTML = '<i class="mdi mdi-tag-outline" aria-hidden="true"></i>';
    content.appendChild(btn);
    btn.addEventListener("click", function (e) {
        e.preventDefault();
        e.stopPropagation();
        showPlacementFieldsDialog(widget, content, kind);
    });
}

// The per-rack power dialog: copy-from-rack is always available; the
// manual-entry fields are rendered from the `planning_fields.rack` schema
// (docs/pdu-distribution-spec.md §5) -- an empty schema shows NO manual cf
// inputs at all, only the copy-from-rack row. Saved immediately via POST
// rack-power/ (the rack is persistent design data -- it does not wait for
// the layout Save, per the plan).
function buildRackPowerDialog(rackId, rackName, existing) {
    var overlay = document.createElement("div");
    overlay.className = "modal fade nbx-rd-power-modal";
    overlay.setAttribute("tabindex", "-1");
    var racks = racksInDom().filter(function (r) { return String(r.id) !== String(rackId); });
    // Manual cf planning inputs only ever reach a user's distribution
    // script (docs/pdu-distribution-spec.md §5): the native "none" and
    // "builtin" engines never read a custom field, so offering these
    // inputs outside "script" mode would promise an effect the active
    // engine cannot deliver. "Copy from rack" stays available in every
    // mode -- feeds are native data.
    var scriptMode = DISTRIBUTION_MODE === "script";
    var fields = scriptMode ? ((PLANNING_FIELDS && PLANNING_FIELDS.rack) || []) : [];
    var fieldsHtml = fields.map(function (f) {
        var safeLabel = String(f.label || f.key).replace(/&/g, "&amp;").replace(/</g, "&lt;");
        return '<div class="col"><label class="form-label small mb-0">' + safeLabel + "</label>"
            + planningFieldInputHtml(f) + "</div>";
    }).join("");
    overlay.innerHTML =
        '<div class="modal-dialog modal-dialog-centered">'
        + '<div class="modal-content">'
        + '<div class="modal-header">'
        + '<h5 class="modal-title">' + "Rack power (planning input)"
        + (rackName ? " — " + rackName.replace(/</g, "&lt;") : "") + "</h5>"
        + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
        + "</div>"
        + '<div class="modal-body">'
        // This rack's CURRENT planned supply, listed before anything can be
        // changed: these rows size the capacity bar, and until 0.21.0 they
        // were invisible everywhere, so a feed copied by mistake inflated
        // the bar with nothing to point at and no way to remove it.
        + '<div class="nbx-rd-rackpower-planned mb-3">'
        + '<div class="fw-bold small mb-1">Planned feeds for this rack</div>'
        + '<div class="nbx-rd-planned-list"><div class="text-muted small">Loading…</div></div>'
        + "</div>"
        + '<div class="form-check">'
        + '<input class="form-check-input" type="radio" name="nbx-rd-rackpower-mode" id="nbx-rd-rackpower-copy" value="copy_rack"'
        + (fields.length ? "" : " checked") + ">"
        + '<label class="form-check-label" for="nbx-rd-rackpower-copy">Copy from rack</label>'
        + "</div>"
        + '<div class="nbx-rd-rackpower-copy-fields ms-4 mb-2">'
        + '<div class="row g-2 align-items-center">'
        + '<div class="col"><select class="form-select form-select-sm nbx-rd-rackpower-rack">'
        + rackOptionsHtml(racks) + "</select></div>"
        + '<div class="col-auto"><button type="button" class="btn btn-sm btn-outline-secondary nbx-rd-rackpower-load">Load</button></div>'
        + "</div>"
        + '<div class="form-text nbx-rd-rackpower-copy-status"></div>'
        + "</div>"
        + (fields.length
            ? ('<div class="form-check mt-2">'
                + '<input class="form-check-input" type="radio" name="nbx-rd-rackpower-mode" id="nbx-rd-rackpower-manual" value="manual" checked>'
                + '<label class="form-check-label" for="nbx-rd-rackpower-manual">Manual entry</label>'
                + "</div>"
                + '<div class="row g-2 ms-1 mt-1">' + fieldsHtml + "</div>")
            : (!scriptMode
                ? '<div class="form-text mt-2">Manual planning fields are hidden because distribution_mode is not "script" — only a distribution script reads them.</div>'
                : ""))
        + "</div>"
        + '<div class="modal-footer">'
        + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
        + '<button type="button" class="btn btn-sm btn-primary" data-rd-rackpower-confirm>Save</button>'
        + "</div>"
        + "</div></div>";
    document.body.appendChild(overlay);

    var plannedListEl = overlay.querySelector(".nbx-rd-planned-list");

    function renderPlannedFeeds() {
        return fetchPlannedFeeds(rackId).then(function (feeds) {
            if (!plannedListEl) { return; }
            if (!feeds.length) {
                plannedListEl.innerHTML =
                    '<div class="text-muted small">None — this rack is sized '
                    + "from its real feeds, or from the flat fallback.</div>";
                return;
            }
            plannedListEl.innerHTML = feeds.map(function (f) {
                return '<div class="d-flex align-items-center justify-content-between'
                    + ' border-bottom py-1">'
                    + '<span class="small">'
                    + String(feedRowLabel(f)).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                    + "</span>"
                    + '<button type="button" class="btn btn-sm btn-ghost-danger'
                    + ' nbx-rd-planned-del" data-feed-id="' + f.id + '"'
                    + ' title="Remove this planned feed">'
                    + '<i class="mdi mdi-close" aria-hidden="true"></i></button>'
                    + "</div>";
            }).join("");
            plannedListEl.querySelectorAll(".nbx-rd-planned-del").forEach(function (btn) {
                btn.addEventListener("click", function () {
                    btn.disabled = true;
                    deletePlannedFeed(btn.getAttribute("data-feed-id")).then(function (res) {
                        createToast(
                            "success", "Removed",
                            (res && res.unbound)
                                ? "Planned feed removed; " + res.unbound
                                    + " PDU(s) lost their binding."
                                : "Planned feed removed.");
                        renderPlannedFeeds();
                        refreshLivePower();
                    }).catch(function (err) {
                        btn.disabled = false;
                        createToast("danger", "Error",
                            "Could not remove that feed: " + String(err));
                    });
                });
            });
        });
    }
    renderPlannedFeeds();

    var copyRadio = overlay.querySelector("#nbx-rd-rackpower-copy");
    var manualRadio = overlay.querySelector("#nbx-rd-rackpower-manual");
    var rackSelect = overlay.querySelector(".nbx-rd-rackpower-rack");
    var loadBtn = overlay.querySelector(".nbx-rd-rackpower-load");
    var statusEl = overlay.querySelector(".nbx-rd-rackpower-copy-status");

    var loadedCf = (existing && existing.custom_fields) || {};
    var copiedFrom = (existing && existing.copied_from) || null;
    // The source rack's feeds, previewed by Load and cloned as PLANNED feeds
    // by the confirm handler (docs/pdu-distribution-spec.md §6.3): a
    // greenfield rack is normally fed like its provisioned siblings, so the
    // copy carries the supply, not just the planning custom fields.
    var loadedFeeds = [];

    function setFieldValues(cf) {
        fields.forEach(function (f) {
            var el = overlay.querySelector('.nbx-rd-rackpower-field[data-field-key="' + f.key + '"]');
            if (el && cf && cf[f.key] != null) { el.value = cf[f.key]; }
        });
    }

    if (existing) {
        if (existing.source === "copy_rack" || !manualRadio) { copyRadio.checked = true; }
        else { manualRadio.checked = true; }
        setFieldValues(existing.custom_fields || {});
        if (copiedFrom && copiedFrom.rack_id != null && rackSelect) {
            rackSelect.value = String(copiedFrom.rack_id);
        }
    }

    function syncMode() {
        overlay.querySelector(".nbx-rd-rackpower-copy-fields").style.opacity = copyRadio.checked ? "1" : ".5";
    }
    copyRadio.addEventListener("change", syncMode);
    if (manualRadio) { manualRadio.addEventListener("change", syncMode); }
    syncMode();

    loadBtn.addEventListener("click", function () {
        var srcRackId = rackSelect.value;
        if (!srcRackId) { statusEl.textContent = "Pick a rack first."; return; }
        copyRadio.checked = true;
        syncMode();
        statusEl.textContent = "Loading…";
        fetchPowerSource(srcRackId, "rack").then(function (data) {
            if (!data || !data.custom_fields) {
                statusEl.textContent = "Could not load that rack's power fields.";
                return;
            }
            loadedCf = data.custom_fields || {};
            setFieldValues(loadedCf);
            var srcRackName = (rackSelect.selectedOptions[0] || {}).textContent || "";
            // T1.5c (D31): keep the DOM's dash-form id (matches rackSelect's
            // own option values, populated from racksInDom() below, so the
            // reopen prefill at rackSelect.value = ... above still matches)
            // -- not a bare parsed int. postCopyFeeds converts to the
            // server's colon form at its own wire boundary.
            copiedFrom = { rack_id: srcRackId, rack_name: srcRackName };
            // Preview BOTH halves of the copy: the feeds this rack will
            // inherit (created as planned feeds on Save) and how many
            // planning custom fields came across.
            loadedFeeds = (data.feeds || []).slice();
            var cfCount = Object.keys(loadedCf).filter(function (k) {
                return loadedCf[k] !== null && loadedCf[k] !== "";
            }).length;
            var bits = ["Loaded from " + srcRackName + "."];
            if (loadedFeeds.length) {
                bits.push("On Save this rack's planned feeds are REPLACED by: "
                    + loadedFeeds.map(feedRowLabel).join(", ") + ".");
            } else {
                bits.push("That rack has no feeds to copy — your planned feeds "
                    + "are left as they are.");
            }
            bits.push(cfCount + " power field(s) copied.");
            statusEl.textContent = bits.join(" ");
        });
    });

    var decidedRef = { cancelled: false };
    var wired = wireModal(overlay, decidedRef);

    overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
        btn.addEventListener("click", function () {
            rdTrace("dist.dialog.cancel", { rackId: rackId, kind: "rack" });
        });
    });

    overlay.querySelector("[data-rd-rackpower-confirm]").addEventListener("click", function () {
        var mode = copyRadio.checked ? "copy_rack" : "manual";
        var cf;
        if (mode === "copy_rack") {
            cf = loadedCf;
        } else {
            cf = {};
            fields.forEach(function (f) {
                var el = overlay.querySelector('.nbx-rd-rackpower-field[data-field-key="' + f.key + '"]');
                if (!el) { return; }
                var v = el.value;
                cf[f.key] = (f.type === "number") ? (v !== "" ? parseFloat(v) : null) : (v || "");
            });
        }
        var cfg = {
            source: mode,
            copied_from: mode === "copy_rack" ? copiedFrom : null,
            custom_fields: cf,
        };
        rdTrace("dist.dialog.confirm", { rackId: rackId, kind: "rack", source: mode, fields: cf });
        // Copy mode carries the supply as well: clone the source rack's feeds
        // as planned feeds, then store the planning cf. Both persist at once
        // (this is design data, not layout), and the power bar + bank chips
        // refresh LIVE off the recompute -- no layout Save, no reload.
        var copySource = (mode === "copy_rack" && copiedFrom && copiedFrom.rack_id != null)
            ? copiedFrom.rack_id : null;
        var feedsCopied = copySource != null
            ? postCopyFeeds(rackId, copySource).then(function (res) {
                return {
                    copied: (res && res.feeds) ? res.feeds.length : 0,
                    deleted: (res && res.deleted) || 0,
                    unbound: (res && res.unbound) || 0,
                };
            })
            : Promise.resolve({ copied: 0, deleted: 0, unbound: 0 });
        feedsCopied.then(function (count) {
            return postRackPower(rackId, cfg).then(function () { return count; });
        }).then(function (count) {
            createToast(
                "success", "Saved",
                count.copied
                    ? ("Rack power settings saved; " + count.copied + " feed(s) copied"
                        + (count.deleted ? ", " + count.deleted + " replaced" : "")
                        + (count.unbound
                            ? " (" + count.unbound + " PDU(s) lost their binding)" : "")
                        + ".")
                    : "Rack power settings saved.");
            var btn = document.querySelector(
                '.nbx-rd-rack-block[data-rack-id="' + rackId + '"] [data-rd-rack-power-btn]');
            if (btn) { btn.classList.add("has-config"); }
            refreshLivePower();
        }).catch(function (err) {
            createToast("danger", "Error", "Could not save rack power: " + String(err));
        });
        wired.requestHide();
    });

    wired.show();
}

// Reads the design context first (rack_block.html's rd-rackpower-<id>
// json_script -- views.py's _project_rack_bundle) so opening the dialog on
// a freshly-loaded page needs no round trip; only a rack with no embedded
// element at all (shouldn't happen in the editor) falls back to the GET.
function showRackPowerDialog(rackId, rackName) {
    rdTrace("dist.dialog.open", { rackId: rackId, kind: "rack" });
    var embedded = document.getElementById("rd-rackpower-" + rackId);
    if (embedded) {
        var existing = null;
        try { existing = JSON.parse(embedded.textContent || "null"); } catch (e) { existing = null; }
        buildRackPowerDialog(rackId, rackName, existing);
        return;
    }
    getRackPower(rackId).then(function (data) {
        buildRackPowerDialog(rackId, rackName, data ? data.power_config : null);
    });
}

export {
    setPowerHooks,
    PLACEMENT_FIELDS,
    postSaveToastKey,
    railPlacementData,
    stampAttributionAttrs,
    restoreAttributionAttrs,
    applyRailToMove,
    clearRailFromMove,
    looksLikePdu,
    autoBindPduFeed,
    showPduPowerDialog,
    stampPlanningAttr,
    attachPlacementFieldsButton,
    showRackPowerDialog,
};
