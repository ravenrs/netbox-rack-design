/*
 * Power heatmap toggle (docs/power-projection-spec.md §3).
 *
 * When the toolbar's "Power heatmap" checkbox is on, every device tile is
 * colored on a green->red gradient by its share of its rack's TOTAL projected
 * draw, and the normal state tints are neutralized (via the .nbx-rd-heatmap
 * class + !important rules in editor.css). Devices with no power data get a
 * neutral hatch instead of green.
 *
 * Design note: the heat color is written as the CSS custom property
 * `--nbx-rd-heat` on each tile's content, and the heatmap CSS paints
 * `background-color: var(--nbx-rd-heat) !important` ONLY while the rack block
 * carries .nbx-rd-heatmap. Toggling the class off therefore restores the
 * server-rendered styling automatically -- we never overwrite (or need to
 * restore) the tiles' own inline background.
 *
 * Read-only: this is a pure view layer. It changes no widget state, sets no
 * dirty flag, and saves nothing.
 */
(function () {
    "use strict";

    // widget-index -> {draw, known} from the per-rack embedded payload.
    function drawMap(block) {
        var rid = block.getAttribute("data-rack-id");
        var el = document.getElementById("rd-editor-data-" + rid);
        var map = {};
        if (!el) { return map; }
        try {
            JSON.parse(el.textContent || "[]").forEach(function (w, i) {
                map[i] = {
                    draw: (w && typeof w.draw_w === "number") ? w.draw_w : 0,
                    known: !!(w && w.draw_known),
                };
            });
        } catch (e) { /* malformed payload -> empty map */ }
        return map;
    }

    // share in [0,1] -> green(120deg) .. red(0deg).
    function heatColor(share) {
        var s = Math.max(0, Math.min(1, share));
        var hue = Math.round(120 * (1 - s));
        return "hsl(" + hue + ", 70%, 45%)";
    }

    function applyBlock(block, on) {
        block.classList.toggle("nbx-rd-heatmap", on);
        if (!on) {
            block.querySelectorAll(".grid-stack-item").forEach(function (tile) {
                tile.classList.remove("nbx-rd-heat-unknown");
            });
            return;
        }
        var map = drawMap(block);
        // Scale each device's fill bar to the RACK's biggest consumer: the
        // largest device fills 100% (red), the rest proportionally shorter and
        // greener -- so you can actually pick out the hogs. (Normalizing to the
        // rack total instead makes every bar tiny/green when load is spread.)
        var maxDraw = 0;
        Object.keys(map).forEach(function (k) {
            if (map[k].draw > maxDraw) { maxDraw = map[k].draw; }
        });
        block.querySelectorAll(".grid-stack-item").forEach(function (tile) {
            var content = tile.querySelector(".grid-stack-item-content");
            if (!content) { return; }
            var idx = parseInt(tile.getAttribute("data-widget-index"), 10);
            var info = (!isNaN(idx) && map[idx]) ? map[idx] : { draw: 0, known: false };
            // Powered device with no draw data -> neutral hatch, never a bar.
            if (!info.known && info.draw === 0) {
                tile.classList.add("nbx-rd-heat-unknown");
                content.style.removeProperty("--nbx-rd-heat-pct");
                return;
            }
            tile.classList.remove("nbx-rd-heat-unknown");
            var share = maxDraw > 0 ? info.draw / maxDraw : 0;
            content.style.setProperty("--nbx-rd-heat-pct", (share * 100).toFixed(1) + "%");
            content.style.setProperty("--nbx-rd-heat-col", heatColor(share));
        });
    }

    function apply(on) {
        document.body.classList.toggle("nbx-rd-heatmap-active", on);
        document.querySelectorAll(".nbx-rd-rack-block").forEach(function (block) {
            applyBlock(block, on);
        });
    }

    // ---- Power-bar hover popover -------------------------------------------
    // A readable, immediate popover for the per-rack power bar (native `title`
    // is slow and a long device list is unreadable in it). Shows draw/capacity
    // and, when present, the scrollable list of devices whose power ports are
    // not connected.
    var popEl = null;

    function ensurePop() {
        if (!popEl) {
            popEl = document.createElement("div");
            popEl.className = "nbx-rd-power-pop";
            document.body.appendChild(popEl);
        }
        return popEl;
    }

    function hidePop() {
        if (popEl) { popEl.style.display = "none"; }
    }

    function showPop(bar, x, y) {
        var draw = bar.getAttribute("data-rd-power-draw");
        var cap = bar.getAttribute("data-rd-power-capacity");
        var util = bar.getAttribute("data-rd-power-util");
        var unconn = bar.getAttribute("data-rd-power-unconnected");
        var pop = ensurePop();
        // Compact header only -- the unconnected devices are shown by
        // highlighting their tiles in the rack (a long list is unreadable).
        var html = '<div class="nbx-rd-power-pop-head">' + draw + " / " + cap
            + " W · " + util + "%</div>";
        if (unconn) {
            var count = unconn.split("|").length;
            html += '<div class="nbx-rd-power-pop-sub">⚠ ' + count
                + " device(s) with power ports not connected"
                + " — highlighted in the rack</div>";
        }
        pop.innerHTML = html;
        pop.style.display = "block";
        var pad = 12;
        var w = pop.offsetWidth;
        var h = pop.offsetHeight;
        var left = Math.min(x + pad, window.innerWidth - w - pad);
        var top = Math.min(y + pad, window.innerHeight - h - pad);
        pop.style.left = Math.max(pad, left) + "px";
        pop.style.top = Math.max(pad, top) + "px";
    }

    // The tiles in this bar's rack whose device is in the unconnected list,
    // matched by the STABLE identity label (never the assigned-name overlay).
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

    function initPop() {
        document.querySelectorAll(".nbx-rd-power-bar").forEach(function (bar) {
            // Suppress the native tooltip in favor of the popover + highlight.
            bar.removeAttribute("title");
            bar.addEventListener("mouseenter", function (e) {
                showPop(bar, e.clientX, e.clientY);
                setFlagged(bar, true);
            });
            bar.addEventListener("mousemove", function (e) {
                showPop(bar, e.clientX, e.clientY);
            });
            bar.addEventListener("mouseleave", function () {
                hidePop();
                setFlagged(bar, false);
            });
        });
    }

    function init() {
        initPop();
        var toggle = document.querySelector("[data-rd-power-heatmap]");
        if (!toggle) { return; }
        toggle.addEventListener("change", function () {
            apply(toggle.checked);
        });
        if (toggle.checked) { apply(true); }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
