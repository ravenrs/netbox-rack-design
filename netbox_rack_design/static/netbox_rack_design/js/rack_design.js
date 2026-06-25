/*
 * Read-only initialiser for the projected rack elevation.
 *
 * Unlike netbox-reorder-rack (which wires drag/resize + a save endpoint), this
 * view is purely a viewer: every grid is initialised with staticGrid:true so
 * GridStack lays the slots out by their gs-y / gs-h attributes but allows no
 * dragging, resizing or dropping.
 */
(function () {
    "use strict";

    function initGrid(selector) {
        var el = document.querySelector(selector);
        if (!el || typeof GridStack === "undefined") {
            return;
        }
        GridStack.init(
            {
                cellHeight: 11,
                margin: 0,
                marginBottom: 1,
                column: 1,
                float: true,
                staticGrid: true,
                disableResize: true,
                disableDrag: true,
            },
            el
        );
    }

    function initAll() {
        initGrid("#nbx-rd-grid-front");
        initGrid("#nbx-rd-grid-rear");
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initAll);
    } else {
        initAll();
    }
})();
