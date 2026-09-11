/*
 * The shared device hover card -- extracted verbatim from editor.js.
 *
 * One body-appended floating card, shown on hover over any device tile in any
 * rack block (and the trays). It reads ONLY data-* attributes stamped on each
 * tile: no network calls, and nothing of the editor's state. `root` (the
 * #rd-editor element) is the one thing it needed from the closure, so it comes
 * in as an argument.
 */

// ---- Shared device hover card (name / role / tenant) -------------------
// One body-appended floating card, shown on hover over any device tile in
// any rack block (and the trays). It reads ONLY data-* attributes stamped on
// each tile — no network calls.
export function initHoverCard(root) {
    var hcard = document.createElement("div");
    hcard.className = "nbx-rd-hovercard";
    hcard.setAttribute("role", "tooltip");
    hcard.style.display = "none";
    document.body.appendChild(hcard);
    var currentContent = null;

    // The tiles carry a native `title` (rack_block.html, and the stripe bars
    // below) so the read-only elevation -- which never loads this file --
    // still answers "what is this". In the editor that tooltip is strictly
    // worse than the card and, appearing on the browser's own ~1s delay,
    // pops up ON TOP of it. So the card takes the title away for as long as
    // it is showing and hands it straight back afterwards; anything the
    // card cannot describe (fillCard returned false) keeps its tooltip.
    var titleHiddenOn = null;

    function suppressNativeTooltip(el) {
        if (titleHiddenOn === el) { return; }
        restoreNativeTooltip();
        var title = el.getAttribute("title");
        if (title === null) { return; }
        el.setAttribute("data-rd-title", title);
        el.removeAttribute("title");
        titleHiddenOn = el;
    }

    function restoreNativeTooltip() {
        if (!titleHiddenOn) { return; }
        var title = titleHiddenOn.getAttribute("data-rd-title");
        if (title !== null) {
            titleHiddenOn.setAttribute("title", title);
            titleHiddenOn.removeAttribute("data-rd-title");
        }
        titleHiddenOn = null;
    }

    function hideCard() {
        hcard.style.display = "none";
        currentContent = null;
        restoreNativeTooltip();
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
        // The role the device has today, when the design re-attributes it.
        var oldRole = content.getAttribute("data-old-role");
        var newName = content.getAttribute("data-new-name");
        var movedTo = content.getAttribute("data-moved-to");
        var power = content.getAttribute("data-power");
        // Chain provenance/conflict (PLAN-design-chains.md §8.4/§8.5): an
        // inherited tile's hover card names the source design (the whole
        // point of the marking -- it draws like a normal device, so the
        // card is where the "this came from upstream" story lives); a
        // conflict tile's card explains WHY, in the planner's own words
        // from the server (never blocking, per §8.2).
        var sourceDesignName = content.getAttribute("data-source-design-name");
        var conflictReason = content.getAttribute("data-conflict-reason");
        // Apply markers (apply-projection phase): the same "explain the
        // marking on hover" pattern -- an ordinary existing device this
        // design did not create, but some OTHER design's apply already
        // holds, names that design so a planner who did not create it
        // can see whose plan holds the slot.
        var reservedByDesignTitle = content.getAttribute("data-reserved-by-design-title");
        // Chassis occupancy (spec §10.4): the rack view answers "what is in
        // there / is there room" without trying to edit it.
        var baysUsed = content.getAttribute("data-bays-used");
        var baysTotal = content.getAttribute("data-bays-total");
        var bayOccupants = content.getAttribute("data-bay-occupants");
        // The deployment's config-declared planning fields, already
        // resolved to [[label, value], ...] -- by the server for a rendered
        // slot (any state: a real device's custom field, or a planned add's
        // planning_data) and by stampPlanningAttr for an in-session add.
        // Nothing here knows a field name.
        var planning = [];
        try { planning = JSON.parse(content.getAttribute("data-planning") || "[]"); }
        catch (e) { planning = []; }
        if (!name && !deviceType && !role && !tenant && !power && !baysTotal
            && !planning.length && !sourceDesignName && !conflictReason
            && !reservedByDesignTitle) { return false; }
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
            ["Old role", (oldRole && oldRole !== role) ? oldRole : null],
            ["Tenant", tenant],
            ["Old tenant", (oldTenant && oldTenant !== tenant) ? oldTenant : null],
            ["To", movedTo],
            ["Bays", baysTotal ? (baysUsed + " of " + baysTotal + " used") : null],
            ["Source", sourceDesignName],
            ["Conflict", conflictReason],
            ["Reserved by", reservedByDesignTitle],
        ].concat(planning).forEach(function (pair) {
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
        // Bay occupants, one per line -- a chassis with eight blades would be
        // unreadable squeezed onto the single "Bays" row above.
        if (bayOccupants) {
            bayOccupants.split(", ").forEach(function (entry) {
                var row = document.createElement("div");
                row.className = "nbx-rd-hovercard-row nbx-rd-hovercard-bay";
                row.textContent = entry;
                hcard.appendChild(row);
            });
        }

        // Power supplies: one row per PSU (name + allocated draw), an
        // "(nc)" marker on any port not cabled to power. data-power is
        // "name:draw:conn|..." (conn 1/0, blank for a catalog template).
        if (power) {
            var total = 0;
            power.split("|").forEach(function (entry) {
                var parts = entry.split(":");
                var pname = parts[0];
                var draw = parseInt(parts[1], 10) || 0;
                var conn = parts[2];
                total += draw;
                var prow = document.createElement("div");
                prow.className = "nbx-rd-hovercard-row";
                var pkey = document.createElement("span");
                pkey.className = "nbx-rd-hovercard-key";
                pkey.textContent = "PS " + pname;
                var pval = document.createElement("span");
                pval.textContent = draw + " W"
                    + (conn === "0" ? " (nc)" : "");
                prow.appendChild(pkey);
                prow.appendChild(pval);
                hcard.appendChild(prow);
            });
            var trow = document.createElement("div");
            trow.className = "nbx-rd-hovercard-row";
            var tkey = document.createElement("span");
            tkey.className = "nbx-rd-hovercard-key";
            tkey.textContent = "Allocated";
            var tval = document.createElement("span");
            tval.textContent = total + " W";
            trow.appendChild(tkey);
            trow.appendChild(tval);
            hcard.appendChild(trow);
        }
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
        suppressNativeTooltip(content);
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
}
