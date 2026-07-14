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
 * (A freshly-dropped palette add has no client-side draw yet, so it counts as
 * 0 W live until the design is saved and reloaded; moves/removes of real
 * devices update fully live.)
 */
(function () {
    "use strict";

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

    // ---- heatmap fill bars (live) ------------------------------------------

    function applyHeat(block, on) {
        block.classList.toggle("nbx-rd-heatmap", on);
        if (!on) {
            block.querySelectorAll(".grid-stack-item").forEach(function (tile) {
                tile.classList.remove("nbx-rd-heat-unknown");
            });
            return;
        }
        var tiles = countingTiles(block);
        var maxDraw = 0;
        tiles.forEach(function (t) {
            var d = tileDraw(t);
            if (d > maxDraw) { maxDraw = d; }
        });
        tiles.forEach(function (tile) {
            var content = tile.querySelector(".grid-stack-item-content");
            if (!content) { return; }
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
        });
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
        document.body.classList.toggle("nbx-rd-heatmap-active", on);
        blocks.forEach(function (block) { applyHeat(block, on); });
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
