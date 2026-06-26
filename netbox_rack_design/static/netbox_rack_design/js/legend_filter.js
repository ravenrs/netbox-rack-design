/*
 * Legend-as-filter for the projected rack elevation (read-only view and the
 * editor). Each legend entry is a checkbox tagged data-rd-state="<state>".
 * Unchecking a state hides every slot of that state (.nbx-rd-state-<state>):
 * grid tiles on either face and chips in the non-racked tray.
 *
 * Scope: a legend only filters slots within its own card, so multiple racks
 * rendered on one page keep independent filters. Legend swatches themselves
 * carry .nbx-rd-state-* too, so they're explicitly excluded.
 */
(function () {
    "use strict";

    function scopeFor(legend) {
        return legend.closest(".card-body") || legend.closest(".card") || document;
    }

    function applyFilter(legend) {
        var scope = scopeFor(legend);
        var boxes = legend.querySelectorAll("input[type=checkbox][data-rd-state]");
        boxes.forEach(function (box) {
            var state = box.getAttribute("data-rd-state");
            var hide = !box.checked;
            scope
                .querySelectorAll(".nbx-rd-state-" + state)
                .forEach(function (el) {
                    if (el.classList.contains("nbx-rd-swatch")) {
                        return; // never hide the legend's own swatch
                    }
                    el.classList.toggle("nbx-rd-filtered-out", hide);
                });
        });
    }

    function init() {
        document.querySelectorAll("[data-rd-legend]").forEach(function (legend) {
            legend.addEventListener("change", function (event) {
                var t = event.target;
                if (t && t.matches && t.matches("input[type=checkbox][data-rd-state]")) {
                    applyFilter(legend);
                }
            });
            applyFilter(legend); // honour any initially-unchecked boxes
        });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
