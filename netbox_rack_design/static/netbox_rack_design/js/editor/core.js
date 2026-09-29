/*
 * CSRF-token resolution and the shared Bootstrap toast helper -- extracted
 * verbatim from editor.js.
 *
 * Both closed over template-derived state that stays behind in editor.js
 * (`root`, `toastContainer`); since neither export can take that state as an
 * argument without changing its signature or every call site, each re-derives
 * it with a lazy `document.getElementById` lookup instead, exactly as those
 * elements are looked up once at editor.js load time.
 */

function getCsrfToken() {
    // Guarded, unlike the closure version: editor.js returned early when
    // #rd-editor was absent, so `root` was never null by the time this ran.
    // This module has no such gate, and the whole point of the function is the
    // fallback chain below -- a throw on the first step would defeat it.
    var rootEl = document.getElementById("rd-editor");
    var fromAttr = rootEl && rootEl.getAttribute("data-csrf-token");
    if (fromAttr) { return fromAttr; }
    if (typeof window.netbox_csrf_token !== "undefined" && window.netbox_csrf_token) {
        return window.netbox_csrf_token;
    }
    var input = document.querySelector("[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
}

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
    (document.getElementById("rd-editor-toasts") || document.body).appendChild(el);

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


// T1.5c (PLAN-templates.md D31): the DOM carries a rack's identity as the
// colon-free "r-<pk>"/"p-<pk>" form (templatetags/rack_design.py's
// rack_dom_id -- ":" is a CSS selector metacharacter, so the DOM avoids it
// on principle even though no live selector site needs the guard today).
// The SERVER speaks models.rack_key()'s colon form, "r:<pk>"/"p:<pk>"
// (api/views.py's parse_rack_id/parse_real_rack_id accept that or a legacy
// bare digit string/int -- never the dash form). Anything read off a DOM
// attribute and then sent to the server -- a JSON payload field, a query
// param -- must go through this translation first, or a well-formed
// dash-form id would look "malformed" to the server instead of correctly
// naming its rack. A bare digit string (no "r-"/"p-" prefix) passes through
// unchanged: that shape is still legal (legacy real-rack pk) both in the
// DOM (chassis's data-real-rack-id) and on the wire.
function rackKeyToServer(domRackId) {
    if (domRackId == null) { return domRackId; }
    var s = String(domRackId);
    if (s.slice(0, 2) === "r-" || s.slice(0, 2) === "p-") {
        return s[0] + ":" + s.slice(2);
    }
    // A bare pk means a real rack, and it goes back out as a NUMBER, exactly
    // as it did before planned racks existed. The server accepts the string
    // form too, so this is not a correctness fix -- it keeps the wire format
    // for real racks byte-identical to what every existing caller and test
    // already expects. (test_editor_cross_rack_add asserts `[1637]`, not
    // `['1637']`.) Same principle as the asymmetric DOM id: a real rack is
    // untouched by the planned-rack work, so nothing that already worked has
    // to change.
    if (/^\d+$/.test(s)) { return parseInt(s, 10); }
    return s;
}

// A handful of call sites hit core NetBox's OWN REST API directly (e.g.
// /api/dcim/devices/?rack_id=), not this plugin's rack_key()-aware
// endpoints -- core has no idea what a PlannedRack is and wants a plain
// integer dcim.Rack pk. Returns that int for an "r-<pk>" (or legacy bare
// digit) DOM id, or null for a "p-<pk>" one -- a planned rack can never
// have a real device in it (T1.5c/D28), so callers should treat null as
// "nothing to fetch," never guess at a pk.
function rackDomIdToRealPk(domRackId) {
    if (domRackId == null) { return null; }
    var s = String(domRackId);
    if (s.slice(0, 2) === "p-") { return null; }
    var digits = (s.slice(0, 2) === "r-") ? s.slice(2) : s;
    var n = parseInt(digits, 10);
    return isNaN(n) ? null : n;
}

export { getCsrfToken, createToast, rackKeyToServer, rackDomIdToRealPk };
