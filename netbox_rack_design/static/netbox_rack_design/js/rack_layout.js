/* Rack-floor alignment for the multi-rack workspace (editor + read-only
 * elevation).
 *
 * The workspace lays every visible rack block out in a horizontal row. Blocks
 * are content-sized and the row is top-aligned, so racks of different heights
 * used to hang from a common CEILING: a 42U and a 47U rack started at the same
 * y and the taller one simply ran further down. That reads backwards -- the
 * taller rack should look taller -- and it puts U1 of one rack level with U6 of
 * the next, so nothing can be compared across racks by eye (user 2026-08-28).
 *
 * Bottom-aligning the blocks themselves (align-items:flex-end) would align
 * whatever sits BELOW the grids -- the non-racked tray, whose height varies
 * with its contents -- not the rack floors. So the alignment is applied where
 * the floor actually is: each block gets a `--nbx-rd-floor-pad` equal to how
 * much shorter its rack is than the tallest visible one, and `.nbx-rd-face-row`
 * spends it as padding-top (see editor.css). The ruler and the grid share that
 * row, so the U numbers travel with their units.
 *
 * Recomputed whenever the visible set or a rack's rendered height changes:
 * blocks added/removed, hidden/shown by the Design-racks panel or the legend
 * filter, a face toggled, GridStack re-sizing its cells on a window resize.
 */
(function () {
    "use strict";

    var PAD = "--nbx-rd-floor-pad";

    function visibleBlocks(scroll) {
        return Array.prototype.filter.call(
            scroll.querySelectorAll(".nbx-rd-rack-block"),
            function (block) {
                if (block.classList.contains("hidden")) { return false; }
                return block.offsetParent !== null;
            }
        );
    }

    /* A block's rack height in px: the tallest RENDERED face grid. Both faces
       are the same rack, but one of them may be toggled off. */
    function rackHeight(block) {
        var tallest = 0;
        Array.prototype.forEach.call(block.querySelectorAll(".nbx-rd-face"), function (face) {
            if (face.offsetParent === null) { return; }
            var wrap = face.querySelector(".nbx-rd-grid-wrap");
            if (!wrap) { return; }
            var h = wrap.getBoundingClientRect().height;
            if (h > tallest) { tallest = h; }
        });
        return tallest;
    }

    function align(scroll) {
        var blocks = visibleBlocks(scroll);
        // Measure unpadded, always: a rack that was the short one last pass must
        // not be measured with the pad it was given then.
        blocks.forEach(function (block) { block.style.removeProperty(PAD); });
        if (blocks.length < 2) { return; }

        var heights = blocks.map(rackHeight);
        var tallest = Math.max.apply(null, heights);
        if (!(tallest > 0)) { return; }

        blocks.forEach(function (block, i) {
            var pad = Math.round(tallest - heights[i]);
            if (pad > 0) { block.style.setProperty(PAD, pad + "px"); }
        });
    }

    function init() {
        var scroll = document.getElementById("nbx-rd-racks-scroll");
        if (!scroll) { return; }
        // Chassis pages lay bays out, not rack faces -- nothing to align.
        if (scroll.classList.contains("nbx-rd-chassis-scroll")) { return; }

        var queued = false;
        function schedule() {
            if (queued) { return; }
            queued = true;
            window.requestAnimationFrame(function () {
                queued = false;
                align(scroll);
            });
        }

        schedule();
        window.addEventListener("load", schedule);
        window.addEventListener("resize", schedule);

        // Blocks appearing/disappearing, and the `hidden` class the Design-racks
        // panel and legend filter toggle. Our own writes are inline styles on the
        // block, which this observer deliberately does not watch -- no feedback.
        if (window.MutationObserver) {
            new window.MutationObserver(schedule).observe(scroll, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ["class"],
            });
        }
        // GridStack re-lays its cells (window resize, cellHeight change) without
        // touching any class, so watch the rendered heights directly.
        if (window.ResizeObserver) {
            var ro = new window.ResizeObserver(schedule);
            Array.prototype.forEach.call(
                scroll.querySelectorAll(".nbx-rd-grid-wrap"),
                function (wrap) { ro.observe(wrap); }
            );
        }

        // Exposed for the e2e suite and for any code that re-renders blocks.
        window.NbxRdRackLayout = { align: function () { align(scroll); } };
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
