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
 * Three views:
 *   - Always on: the per-rack power bar (draw / capacity / util%, ok/warn/
 *     critical), recomputed live from the tiles' own draw.
 *   - Always on: the per-PDU/bank distribution chip strip (docs/pdu-
 *     distribution-spec.md). Unlike the bar, WHICH bank a device's draw lands in
 *     is decided by the server distribution engine (real cabling / a custom
 *     distribution_script) -- logic the browser must not duplicate. So on the
 *     SAME mutation signal, the editor re-runs that engine over the unsaved
 *     layout via the read-only recompute-distribution endpoint (see
 *     requestLiveDistribution + NbxRdEditor.recomputeDistribution) and this file
 *     re-renders the chips from the fresh result -- live, but nothing persisted.
 *     On the read-only elevation (no editor present) the chips show the static
 *     server-rendered distribution and simply do not live-update.
 *   - Toggle "Power heatmap": per-device consumption "health bar" filled
 *     left->right to the device's share of the rack's BIGGEST consumer
 *     (biggest = 100% red, others proportionally toward green). It shows that
 *     device's OWN contribution only -- bank load/breaker is carried by the chip
 *     strip, never restated on the tiles. A device with a power port but no known
 *     draw reads as a neutral hatch, since absence of data is not zero draw.
 *     Off restores the normal styling exactly (the chip strip stays).
 *
 * Read-only: pure view layer -- no widget state, no dirty flag, nothing saved.
 * (A freshly-dropped palette add also counts live: the catalog palette fetches
 * each type's projected draw from the device-type-power endpoint and stamps it
 * on the row, so the drop carries the same `data-draw-w` a real device does.)
 */
(function () {
    "use strict";

    // localStorage key remembering the "Power heatmap" toggle so it survives the
    // page reload a Save triggers (user 2026-07-31).
    var HEATMAP_PREF_KEY = "nbxRdPowerHeatmap";

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

    // ---- distribution model (docs/pdu-distribution-spec.md) -----------------

    // Live per-rack Distribution cache: {rackId: Distribution|null}, filled by the
    // debounced server recompute (requestLiveDistribution) so the per-bank chips
    // refresh as tiles change, exactly like the power bar. Empty until the first
    // edit -- the initial paint uses the server-rendered static blob below.
    var liveDist = {};

    // The Distribution for a rack: the live-recomputed one if we have it (any edit
    // has happened), else the static JSON the server emitted at render time
    // (`#rd-distribution-<rackId>`). null = no resolvable PDUs (per-device heatmap).
    function readDistribution(block) {
        var rackId = block.getAttribute("data-rack-id") || "";
        if (Object.prototype.hasOwnProperty.call(liveDist, rackId)) {
            return liveDist[rackId];
        }
        var el = document.getElementById("rd-distribution-" + rackId);
        if (!el) { return null; }
        try {
            var d = JSON.parse(el.textContent);
            return (d && d.pdus) ? d : null;
        } catch (e) {
            return null;
        }
    }

    // Why a rack has (or has not) a distribution: {state, engine, script, detail}.
    // Same two sources as the distribution itself -- the live recompute first,
    // then the server-rendered blob. Without this a rack whose script just threw
    // looks exactly like a rack with no PDUs, which cost hours to tell apart
    // (user 2026-08-28: a silent breakage is not acceptable).
    var liveDistStatus = {};

    function readDistStatus(block) {
        var rackId = block.getAttribute("data-rack-id") || "";
        if (Object.prototype.hasOwnProperty.call(liveDistStatus, rackId)) {
            return liveDistStatus[rackId];
        }
        var el = document.getElementById("rd-diststatus-" + rackId);
        if (!el) { return null; }
        try {
            return JSON.parse(el.textContent) || null;
        } catch (e) {
            return null;
        }
    }

    // Index a Distribution into { byName: {deviceName: {feeds, hottest}}, banks }.
    // A device charged to BOTH legs records BOTH feeds (so the tile shows the A
    // and B accent), and keeps the hottest bank for its fill color.
    function indexBanks(dist) {
        var byName = {};
        var banks = [];
        Object.keys(dist.pdus).forEach(function (pduName) {
            var pdu = dist.pdus[pduName];
            Object.keys(pdu.banks).forEach(function (bankId) {
                var bank = pdu.banks[bankId];
                var load = (bank.allocated_power || 0) + (bank.planned_power || 0);
                var info = {
                    pdu: pduName, feed: pdu.feed_name, feedLetter: pdu.feed_letter,
                    phase: pdu.phase || 1,
                    bank: bankId, util: bank.util_pct || 0, state: bank.state || "ok",
                    load: load, max: bank.max_power || 0,
                };
                banks.push(info);
                (bank.devices || []).forEach(function (d) {
                    var e = byName[d.name] || (byName[d.name] = { feeds: {}, hottest: null });
                    e.feeds[info.feedLetter] = true;
                    if (!e.hottest || info.util > e.hottest.util) { e.hottest = info; }
                });
            });
        });
        banks.sort(function (a, b) {
            return a.feedLetter === b.feedLetter
                ? (a.bank - b.bank) : (a.feedLetter < b.feedLetter ? -1 : 1);
        });
        return { byName: byName, banks: banks };
    }

    // Tag a tile with EVERY feed leg it lands on (blue A edge / orange B edge,
    // both when redundant) so CSS can show the A/B split. Falsy `feeds` clears.
    function setFeedClasses(tile, feeds) {
        tile.classList.toggle("nbx-rd-feed-a", !!(feeds && feeds.a));
        tile.classList.toggle("nbx-rd-feed-b", !!(feeds && feeds.b));
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
        tile.classList.remove("nbx-rd-heat-unknown", "nbx-rd-feed-a", "nbx-rd-feed-b");
        var content = tile.querySelector(".grid-stack-item-content");
        if (content) {
            content.style.removeProperty("--nbx-rd-heat-pct");
            content.style.removeProperty("--nbx-rd-heat-col");
        }
    }

    // Instant hover tooltip (no native-title delay): a shared fixed element shown
    // immediately on mouseenter, so the alarm warnings appear at once instead of
    // after the browser's ~1s title delay (user 2026-07-17).
    var tipEl = null;
    function ensureTip() {
        if (!tipEl) {
            tipEl = document.createElement("div");
            tipEl.className = "nbx-rd-dist-tip";
            document.body.appendChild(tipEl);
        }
        return tipEl;
    }
    function showTip(text, x, y) {
        var t = ensureTip();
        t.textContent = text;
        t.style.display = "block";
        var pad = 12;
        t.style.left = Math.max(pad, Math.min(x + pad, window.innerWidth - t.offsetWidth - pad)) + "px";
        t.style.top = Math.max(pad, Math.min(y + pad, window.innerHeight - t.offsetHeight - pad)) + "px";
    }
    function hideTip() { if (tipEl) { tipEl.style.display = "none"; } }
    function attachInstantTip(el, text) {
        el.addEventListener("mouseenter", function (e) { showTip(text, e.clientX, e.clientY); });
        el.addEventListener("mousemove", function (e) { showTip(text, e.clientX, e.clientY); });
        el.addEventListener("mouseleave", hideTip);
    }

    // Per-bank breaker legend (script mode): a compact strip of chips under the
    // power bar -- a feed A/B color key (explaining the blue/orange tile edges),
    // one chip per PDU bank colored by state (used/breaker W), plus an instant
    // ⚠ tooltip listing rack overload/limit warnings. Falsy `dist` removes it.
    // The visible half of "no silent failures": when there are no chips, say why.
    // `failed` is an error the user must act on (the script's own exception, and
    // which script it was); `empty` explains which PDU was unusable; `off` says
    // the feature is switched off rather than leaving the strip blank, which is
    // exactly the confusion this was written for.
    function renderDistNotice(block, status) {
        var existing = block.querySelector(".nbx-rd-dist-notice");
        if (!status || status.state === "ok") {
            if (existing) { existing.remove(); }
            return;
        }
        var el = existing || document.createElement("div");
        el.className = "nbx-rd-dist-notice nbx-rd-dist-notice-" + status.state;
        var icon = status.state === "failed" ? "mdi-alert-circle" : "mdi-information-outline";
        var label = status.state === "failed"
            ? "Per-bank distribution unavailable — the "
                + (status.engine === "script" ? "distribution script" : "builtin engine")
                + " failed"
            : (status.state === "off"
                ? "Per-bank distribution is off"
                : "No per-bank distribution for this rack");
        el.innerHTML = '<i class="mdi ' + icon + '" aria-hidden="true"></i> '
            + '<span class="nbx-rd-dist-notice-label"></span>';
        el.querySelector(".nbx-rd-dist-notice-label").textContent = label;
        var tip = [status.detail || label];
        if (status.script) { tip.push("Script: " + status.script); }
        attachInstantTip(el, tip.join("\n"));
        if (!existing) {
            var bar = block.querySelector(".nbx-rd-power-bar");
            if (bar && bar.parentNode) { bar.parentNode.insertBefore(el, bar.nextSibling); }
            else { block.insertBefore(el, block.firstChild); }
        }
    }

    function renderDistLegend(block, dist) {
        var existing = block.querySelector(".nbx-rd-dist-legend");
        if (!dist) { if (existing) { existing.remove(); } return; }
        var idx = indexBanks(dist);
        var legend = existing || document.createElement("div");
        legend.className = "nbx-rd-dist-legend";
        // The PDU header itself carries the feed color (A blue / B orange), which
        // matches each tile's accent edge -- so no separate key row is needed.
        // Group the bank chips by PDU so one PDU's banks stack under each other
        // (one column per PDU), rather than one long flat row.
        var order = [];
        var byPdu = {};
        idx.banks.forEach(function (b) {
            if (!byPdu[b.pdu]) { byPdu[b.pdu] = []; order.push(b.pdu); }
            byPdu[b.pdu].push(b);
        });
        var cols = order.map(function (pduName) {
            var first = byPdu[pduName][0];
            // Header carries the feed color (A blue / B orange) + a 3φ flag for
            // three-phase PDUs; it matches the tiles' accent edge = the key.
            var head = first.feed + (first.phase === 3 ? " 3φ" : "");
            var chips = byPdu[pduName].map(function (b) {
                // A mini "health bar" per bank: the fill is the load/breaker
                // ratio, colored by state -- same idea as the rack power bar.
                var w = Math.max(0, Math.min(100, b.util || 0));
                return '<span class="nbx-rd-dist-chip nbx-rd-dist-' + b.state + '">'
                    + '<span class="nbx-rd-dist-fill" style="width:' + w.toFixed(1) + '%"></span>'
                    + '<span class="nbx-rd-dist-label">B' + b.bank + ": "
                    + Math.round(b.load) + "/" + Math.round(b.max) + " W</span>"
                    + "</span>";
            }).join("");
            return '<div class="nbx-rd-dist-pdu">'
                + '<span class="nbx-rd-dist-pdu-head nbx-rd-feedhead-' + first.feedLetter
                + '">' + head + "</span>" + chips + "</div>";
        }).join("");
        legend.innerHTML = cols;
        var rack = dist.rack || {};
        if (rack.alarm && (rack.warnings || []).length) {
            var alarm = document.createElement("span");
            alarm.className = "nbx-rd-dist-alarm";
            alarm.textContent = "⚠ " + rack.warnings.length;
            attachInstantTip(alarm, rack.warnings.join("\n"));
            legend.appendChild(alarm);
        }
        if (!existing) {
            var bar = block.querySelector(".nbx-rd-power-bar");
            if (bar && bar.parentNode) { bar.parentNode.insertBefore(legend, bar.nextSibling); }
            else { block.insertBefore(legend, block.firstChild); }
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
                tile.classList.remove("nbx-rd-heat-unknown",
                    "nbx-rd-feed-a", "nbx-rd-feed-b");
            });
            // The bank chip strip is always-on (rendered by recomputeAll); the
            // heatmap toggle only controls the per-tile tint, so leave it in place.
            if (trace) { rdT("apply", { rackId: rackId, on: false }); }
            return;
        }
        var tiles = countingTiles(block);
        var maxDraw = 0;
        tiles.forEach(function (t) {
            var d = tileDraw(t);
            if (d > maxDraw) { maxDraw = d; }
        });
        // Per-bank distribution (script mode): when the server emitted a
        // Distribution for this rack, the heat SUBJECT becomes the PDU/bank --
        // each consumer tile is tinted by the load-vs-breaker of the bank it
        // lands on (not its own rack share). Absent -> the per-device path.
        var dist = readDistribution(block);
        var banksIdx = dist ? indexBanks(dist) : null;
        if (trace) {
            rdT("apply", {
                rackId: rackId, on: true, countingTiles: tiles.length,
                maxDraw: maxDraw, distribution: !!dist,
                banks: banksIdx ? banksIdx.banks.length : 0,
            });
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
            // Feed legs still come from the Distribution: the A/B edge accent says
            // WHICH leg a device lands on, which is identity, not load.
            if (banksIdx) {
                var entry = banksIdx.byName[visibleName(content)];
                setFeedClasses(tile, entry ? entry.feeds : null);
            }
            // The fill is this device's OWN contribution -- its share of the rack's
            // biggest consumer, green(low) -> red(top). It deliberately does NOT
            // restate its bank's utilization (user ruling 2026-08-18): tinting every
            // tile in a hot bank the same red erased all per-device signal, so a
            // 300 W switch painted identically to an 800 W server. Bank health is
            // carried by the per-bank chips, which is where it belongs.
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
            // The per-bank chip strip is always-on (like the power bar), rendered
            // from readDistribution() which prefers the live-recomputed cache.
            renderDistLegend(block, readDistribution(block));
            renderDistNotice(block, readDistStatus(block));
            if (on) { applyHeat(block, true); }
        });
        observers.forEach(function (o, i) { o.observe(blocks[i], OBS_OPTS); });
    }

    // ---- live per-bank distribution (server round-trip, no persist) ---------
    //
    // The bar sums each tile's own draw locally, but the per-bank distribution is
    // produced by the server engine (builtin or a custom distribution_script)
    // reading real cabling/ports -- logic the browser must not duplicate. So on
    // the SAME mutation signal that drives the bar, we ask the editor to re-run
    // that engine over the unsaved layout (recompute-distribution endpoint), cache
    // the result in liveDist, and re-render. Debounced + sequenced so only the
    // freshest response wins; a null result keeps the last-known distribution.
    var liveDistPending = null;
    var liveDistSeq = 0;
    function requestLiveDistribution() {
        var editor = window.NbxRdEditor;
        if (!editor || typeof editor.recomputeDistribution !== "function") { return; }
        if (liveDistPending) { window.clearTimeout(liveDistPending); }
        liveDistPending = window.setTimeout(function () {
            liveDistPending = null;
            var seq = ++liveDistSeq;
            editor.recomputeDistribution().then(function (res) {
                // Drop a stale response (a newer edit already fired) or a null one
                // (the request failed) -- never blank the chips on a hiccup.
                if (seq !== liveDistSeq || !res) { return; }
                var dists = res.distributions || {};
                Object.keys(dists).forEach(function (rackId) {
                    var d = dists[rackId];
                    liveDist[rackId] = (d && d.pdus) ? d : null;
                });
                // An edit can BREAK the engine (a script that trips over the new
                // layout), so the reason travels with every recompute -- the
                // notice must appear the moment it starts failing, not only on
                // the next page load.
                var statuses = res.distribution_status || {};
                Object.keys(statuses).forEach(function (rackId) {
                    liveDistStatus[rackId] = statuses[rackId] || null;
                });
                // Capacity/thresholds come from the server (feed derating maths
                // over real AND planned feeds, which the browser must not
                // duplicate), so a feed added mid-session moves the bar's
                // denominator without a Save. The DRAW stays client-side: it is
                // already live off the tiles and must not lag behind a drag.
                var powers = res.power || {};
                Object.keys(powers).forEach(function (rackId) {
                    var p = powers[rackId];
                    if (!p || p.capacity_w == null) { return; }
                    var block = document.querySelector(
                        '.nbx-rd-rack-block[data-rack-id="' + rackId + '"]');
                    var bar = block && block.querySelector(".nbx-rd-power-bar");
                    if (!bar) { return; }
                    bar.setAttribute("data-rd-power-capacity", Math.round(p.capacity_w));
                    if (p.warn_pct != null) {
                        bar.setAttribute("data-rd-power-warn", p.warn_pct);
                    }
                    if (p.critical_pct != null) {
                        bar.setAttribute("data-rd-power-critical", p.critical_pct);
                    }
                });
                recomputeAll();
            });
        }, 200);
    }

    function scheduleRecompute() {
        if (pending) { window.clearTimeout(pending); }
        pending = window.setTimeout(function () {
            pending = null;
            recomputeAll();
        }, 80);
        // Kick the server recompute off the raw mutation (its own longer debounce),
        // NOT from recomputeAll -- recomputeAll writes DOM with observers detached,
        // so applying the result never re-triggers this and cannot loop.
        requestLiveDistribution();
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
        // Paint the always-on power bar + per-bank chip strip once from the
        // server-rendered static distribution, before any edit (which then swaps
        // in live-recomputed numbers). Uses no server round-trip.
        recomputeAll();
        var toggle = document.querySelector("[data-rd-power-heatmap]");
        if (toggle) {
            // Remember the on/off choice so a Save -- which reloads the page --
            // does NOT silently switch the heatmap off, forcing the user to
            // re-enable it every time (user 2026-07-31).
            try {
                if (window.localStorage.getItem(HEATMAP_PREF_KEY) === "1") {
                    toggle.checked = true;
                }
            } catch (e) { /* storage unavailable -- fall back to the default */ }
            toggle.addEventListener("change", function () {
                try {
                    window.localStorage.setItem(
                        HEATMAP_PREF_KEY, toggle.checked ? "1" : "0");
                } catch (e) { /* ignore */ }
                applyHeatAll(toggle.checked);
            });
            if (toggle.checked) { applyHeatAll(true); }
        }
    }

    // Public hook: a feed change (define/copy a planned feed) mutates no tile, so
    // the MutationObserver never fires -- the dialogs call this to pull fresh
    // capacity + chips without a Save or a reload.
    window.NbxRdPowerHeatmap = {
        refresh: function () { requestLiveDistribution(); recomputeAll(); },
    };

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
