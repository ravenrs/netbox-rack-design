/*
 * Power projection UI (docs/power-projection-spec.md §3) — LIVE.
 *
 * Reads each tile's own `data-draw-w` / `data-draw-known` (stamped by the
 * server and carried on the element, so they travel with a tile as it is moved
 * between racks), so the per-rack power bar and the heatmap recompute in the
 * browser as devices are shuffled -- no round-trip. A MutationObserver on each
 * rack block drives the live update (tiles added/removed/reparented, or flagged
 * for removal).
 *
 * Two views:
 *   - Always on: the per-rack power bar (draw / capacity / util%, ok/warn/
 *     critical), recomputed live.
 *   - Toggle "Power heatmap": per-device consumption "health bar" filled
 *     left->right to the device's share of the rack's BIGGEST consumer
 *     (biggest = 100% red, others proportionally toward green). Off restores
 *     the normal styling exactly.
 *
 * Read-only: pure view layer -- no widget state, no dirty flag, nothing saved.
 * (A freshly-dropped palette add also counts live: the catalog palette fetches
 * each type's projected draw from the device-type-power endpoint and stamps it
 * on the row, so the drop carries the same `data-draw-w` a real device does.)
 */
(function () {
    "use strict";

    // Dev-only tracer, shared with editor.js (window.__rdTrace, gated on a dev
    // build + the __rdDragTrace toggle; inert otherwise). Lets the heatmap's
    // per-tile render be watched alongside the drag lifecycle.
    function rdT(ev, data) {
        if (window.__rdTrace) { window.__rdTrace("heat." + ev, data || {}); }
    }

    // The visible name a tile currently shows: the rename overlay if present,
    // else the identity label unless it's hidden. "" means the tile renders
    // BLANK (the just-placed-on-a-removed-slot bug -- user 2026-07-16).
    function visibleName(content) {
        var disp = content.querySelector(".nbx-rd-name-display");
        if (disp && disp.textContent.trim()) { return disp.textContent.trim(); }
        var lab = content.querySelector(".nbx-rd-label");
        if (lab && !lab.classList.contains("nbx-rd-label-hidden")) {
            return (lab.textContent || "").trim();
        }
        return "";
    }

    // ---- draw model (read straight off the live tiles) ---------------------

    // Tiles that CONSUME power for the rack total: exclude the opposite-face
    // hatch, derived/temp ghosts, move-out ghosts, remove-flagged tiles and
    // palette clones. (PDUs already carry data-draw-w="0" from the server.)
    function countingTiles(block) {
        return Array.prototype.slice.call(
            block.querySelectorAll(".grid-stack-item")
        ).filter(function (t) {
            if (t.classList.contains("nbx-rd-palette-item")) { return false; }
            if (t.classList.contains("nbx-rd-opposite")) { return false; }
            if (t.getAttribute("data-rd-derived-opp")) { return false; }
            if (t.classList.contains("nbx-rd-state-move_out_ghost")) { return false; }
            if (t.getAttribute("data-rd-temp-ghost")) { return false; }
            if (t.classList.contains("nbx-rd-state-remove")) { return false; }
            return true;
        });
    }

    function tileDraw(tile) {
        var c = tile.querySelector(".grid-stack-item-content");
        var v = c ? parseFloat(c.getAttribute("data-draw-w")) : 0;
        return isNaN(v) ? 0 : v;
    }

    function tileKnown(tile) {
        var c = tile.querySelector(".grid-stack-item-content");
        return !c || c.getAttribute("data-draw-known") !== "0";
    }

    // share in [0,1] -> green(120deg) .. red(0deg).
    function heatColor(share) {
        var s = Math.max(0, Math.min(1, share));
        return "hsl(" + Math.round(120 * (1 - s)) + ", 70%, 45%)";
    }

    // ---- per-rack bar (live) -----------------------------------------------

    function updateBar(block) {
        var bar = block.querySelector(".nbx-rd-power-bar");
        if (!bar) { return; }
        var cap = parseFloat(bar.getAttribute("data-rd-power-capacity")) || 0;
        var warn = parseFloat(bar.getAttribute("data-rd-power-warn")) || 80;
        var crit = parseFloat(bar.getAttribute("data-rd-power-critical")) || 100;
        var draw = 0;
        countingTiles(block).forEach(function (t) { draw += tileDraw(t); });
        var util = cap > 0 ? draw / cap * 100 : 0;
        var rd = Math.round(draw);
        var ru = Math.round(util);
        bar.setAttribute("data-rd-power-draw", rd);
        bar.setAttribute("data-rd-power-util", ru);
        var fill = bar.querySelector(".nbx-rd-power-fill");
        if (fill) { fill.style.width = ru + "%"; }
        var unconn = bar.getAttribute("data-rd-power-unconnected");
        var label = bar.querySelector(".nbx-rd-power-label");
        if (label) {
            label.textContent = rd + " / " + Math.round(cap) + " W · " + ru + "%"
                + (unconn ? " ⚠ " + unconn.split("|").length : "");
        }
        var state = util >= crit ? "critical" : util >= warn ? "warn" : "ok";
        bar.classList.remove("nbx-rd-power-ok", "nbx-rd-power-warn",
            "nbx-rd-power-critical");
        bar.classList.add("nbx-rd-power-" + state);
    }

    // Wipe any heat styling off a tile (used when a tile must show NO fill).
    function clearHeat(tile) {
        tile.classList.remove("nbx-rd-heat-unknown");
        var content = tile.querySelector(".grid-stack-item-content");
        if (content) {
            content.style.removeProperty("--nbx-rd-heat-pct");
            content.style.removeProperty("--nbx-rd-heat-col");
        }
    }

    // ---- heatmap fill bars (live) ------------------------------------------

    // `trace` is passed only from the explicit toggle (applyHeatAll); the
    // mutation-driven recompute path leaves it falsy so heat.apply doesn't
    // flood the log on every DOM tick. The per-tile heat.blankName probe in
    // fill() still runs every time, so the bug is caught whenever it renders.
    function applyHeat(block, on, trace) {
        var rackId = block.getAttribute("data-rack-id") || block.id;
        block.classList.toggle("nbx-rd-heatmap", on);
        if (!on) {
            block.querySelectorAll(".grid-stack-item").forEach(function (tile) {
                tile.classList.remove("nbx-rd-heat-unknown");
            });
            if (trace) { rdT("apply", { rackId: rackId, on: false }); }
            return;
        }
        var tiles = countingTiles(block);
        var maxDraw = 0;
        tiles.forEach(function (t) {
            var d = tileDraw(t);
            if (d > maxDraw) { maxDraw = d; }
        });
        if (trace) {
            rdT("apply", { rackId: rackId, on: true, countingTiles: tiles.length, maxDraw: maxDraw });
        }
        function fill(tile) {
            var content = tile.querySelector(".grid-stack-item-content");
            if (!content) { return; }
            // A device flagged for REMOVAL or DISPLACED (being replaced) is
            // leaving this slot -- it must NOT paint a heat color. Its own label
            // is hidden (the displacement stripe carries the name), so a fill
            // reads as a colored NAMELESS tile, and its draw shouldn't count as
            // a live consumer (user bug 2026-07-16: "лейбла нет, хитмап не
            // верный"). Clear any prior fill and skip.
            if (tile.classList.contains("nbx-rd-state-remove")
                    || tile.classList.contains("nbx-rd-displaced")) {
                clearHeat(tile);
                return;
            }
            // Diagnostic (user bug 2026-07-16): a heatmap tile that gets a fill
            // but shows NO name -- the "colored nameless tile". The name may be
            // absent from the DOM OR just CSS-hidden (a displaced tile's label
            // is visibility:hidden, its name moved to an external stripe), so
            // check the COMPUTED rendering, not just the -hidden class. Report
            // the unit/label, its heat %, and WHY it's not shown.
            var lab = content.querySelector(".nbx-rd-label");
            var nameEl = content.querySelector(".nbx-rd-name-display") || lab;
            var shownOnTile = false;
            if (nameEl && (nameEl.textContent || "").trim()) {
                var ncs = window.getComputedStyle(nameEl);
                shownOnTile = ncs.display !== "none" && ncs.visibility !== "hidden"
                    && parseFloat(ncs.opacity || "1") > 0.01;
            }
            if (!shownOnTile) {
                rdT("namelessFill", {
                    label: lab ? (lab.textContent || "").trim() : null,
                    unitY: tile.gridstackNode ? tile.gridstackNode.y : null,
                    heatPct: content.style.getPropertyValue("--nbx-rd-heat-pct") || null,
                    state: (tile.className.match(/nbx-rd-state-[\w]+/) || [])[0],
                    displaced: tile.classList.contains("nbx-rd-displaced"),
                    labelVisibility: lab ? window.getComputedStyle(lab).visibility : null,
                    idx: tile.getAttribute("data-widget-index"),
                });
            }
            var draw = tileDraw(tile);
            if (draw === 0 && !tileKnown(tile)) {
                tile.classList.add("nbx-rd-heat-unknown");
                content.style.removeProperty("--nbx-rd-heat-pct");
                return;
            }
            tile.classList.remove("nbx-rd-heat-unknown");
            var share = maxDraw > 0 ? draw / maxDraw : 0;
            content.style.setProperty("--nbx-rd-heat-pct", (share * 100).toFixed(1) + "%");
            content.style.setProperty("--nbx-rd-heat-col", heatColor(share));
        }
        tiles.forEach(fill);
        // Opposite-face hatches are the SAME physical device on the other face;
        // they are excluded from countingTiles (so they never affect maxDraw or
        // the rack total), but they carry their owner's data-draw-w (stamped by
        // the editor's syncDeviceShadow) -- fill them too so a full-depth
        // device's consumption is visible on BOTH faces, not blank on the
        // mounted-away side (user bug 2026-07-15).
        Array.prototype.slice.call(
            block.querySelectorAll(".grid-stack-item.nbx-rd-opposite," +
                " .grid-stack-item[data-rd-derived-opp]")
        ).forEach(fill);
        // A removed/displaced BODY is excluded from countingTiles, so fill()
        // never re-touches it and its last fill would go STALE (keep a color
        // after the device was flagged to leave). Wipe heat off every
        // remove/displaced tile so none keeps a fill.
        Array.prototype.slice.call(
            block.querySelectorAll(".grid-stack-item.nbx-rd-state-remove," +
                " .grid-stack-item.nbx-rd-displaced")
        ).forEach(clearHeat);
    }

    function heatmapOn() {
        return document.body.classList.contains("nbx-rd-heatmap-active");
    }

    // ---- live recompute driven by DOM mutations ----------------------------

    var blocks = [];
    var observers = [];
    var OBS_OPTS = {
        childList: true, subtree: true, attributes: true,
        attributeFilter: ["class", "data-draw-w", "gs-y"],
    };
    var pending = null;

    function recomputeAll() {
        // Detach while we write (our own attribute/class edits would otherwise
        // re-trigger the observers), then reattach.
        observers.forEach(function (o) { o.disconnect(); });
        var on = heatmapOn();
        blocks.forEach(function (block) {
            updateBar(block);
            if (on) { applyHeat(block, true); }
        });
        observers.forEach(function (o, i) { o.observe(blocks[i], OBS_OPTS); });
    }

    function scheduleRecompute() {
        if (pending) { window.clearTimeout(pending); }
        pending = window.setTimeout(function () {
            pending = null;
            recomputeAll();
        }, 80);
    }

    function initObservers() {
        blocks = Array.prototype.slice.call(
            document.querySelectorAll(".nbx-rd-rack-block"));
        blocks.forEach(function (block) {
            var obs = new MutationObserver(scheduleRecompute);
            obs.observe(block, OBS_OPTS);
            observers.push(obs);
        });
    }

    // ---- heatmap toggle ----------------------------------------------------

    function applyHeatAll(on) {
        rdT("toggle", { on: !!on, blocks: blocks.length });
        document.body.classList.toggle("nbx-rd-heatmap-active", on);
        blocks.forEach(function (block) { applyHeat(block, on, true); });
    }

    // ---- power-bar hover: compact popover + pull out the unconnected tiles --

    var popEl = null;
    function ensurePop() {
        if (!popEl) {
            popEl = document.createElement("div");
            popEl.className = "nbx-rd-power-pop";
            document.body.appendChild(popEl);
        }
        return popEl;
    }
    function hidePop() { if (popEl) { popEl.style.display = "none"; } }
    function showPop(bar, x, y) {
        var draw = bar.getAttribute("data-rd-power-draw");
        var cap = bar.getAttribute("data-rd-power-capacity");
        var util = bar.getAttribute("data-rd-power-util");
        var unconn = bar.getAttribute("data-rd-power-unconnected");
        var pop = ensurePop();
        var html = '<div class="nbx-rd-power-pop-head">' + draw + " / " + cap
            + " W · " + util + "%</div>";
        if (unconn) {
            html += '<div class="nbx-rd-power-pop-sub">⚠ ' + unconn.split("|").length
                + " device(s) with power ports not connected — highlighted in the rack</div>";
        }
        pop.innerHTML = html;
        pop.style.display = "block";
        var pad = 12;
        var left = Math.min(x + pad, window.innerWidth - pop.offsetWidth - pad);
        var top = Math.min(y + pad, window.innerHeight - pop.offsetHeight - pad);
        pop.style.left = Math.max(pad, left) + "px";
        pop.style.top = Math.max(pad, top) + "px";
    }
    function flaggedTiles(bar) {
        var unconn = bar.getAttribute("data-rd-power-unconnected");
        if (!unconn) { return []; }
        var names = {};
        unconn.split("|").forEach(function (n) { names[n] = true; });
        var block = bar.closest(".nbx-rd-rack-block");
        if (!block) { return []; }
        return Array.prototype.slice.call(
            block.querySelectorAll(".grid-stack-item")
        ).filter(function (tile) {
            var lab = tile.querySelector(".nbx-rd-label");
            return lab && names[lab.textContent];
        });
    }
    function setFlagged(bar, on) {
        flaggedTiles(bar).forEach(function (tile) {
            tile.classList.toggle("nbx-rd-power-flagged", on);
        });
    }
    function initHover() {
        document.querySelectorAll(".nbx-rd-power-bar").forEach(function (bar) {
            bar.removeAttribute("title");
            bar.addEventListener("mouseenter", function (e) {
                showPop(bar, e.clientX, e.clientY);
                setFlagged(bar, true);
            });
            bar.addEventListener("mousemove", function (e) { showPop(bar, e.clientX, e.clientY); });
            bar.addEventListener("mouseleave", function () {
                hidePop();
                setFlagged(bar, false);
            });
        });
    }

    // ---- init --------------------------------------------------------------

    function init() {
        initObservers();
        initHover();
        var toggle = document.querySelector("[data-rd-power-heatmap]");
        if (toggle) {
            toggle.addEventListener("change", function () { applyHeatAll(toggle.checked); });
            if (toggle.checked) { applyHeatAll(true); }
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
