/*
 * Legend-as-filter for the projected rack elevation (read-only view and the
 * editor). Each legend entry is a checkbox tagged EITHER data-rd-state="<state>"
 * (one of the five ProjectedSlotState members) OR data-rd-flag="<flag>" (a
 * boolean flag layered on top of a state, per PLAN-design-chains.md §8.4 --
 * `inherited` and `conflict` are flags, not new states, exactly like the
 * pre-existing `displaced` flag). A state checkbox hides every slot in that
 * state (.nbx-rd-state-<state>); a flag checkbox hides every slot carrying
 * that flag (.nbx-rd-flag-<flag>), REGARDLESS of its underlying state -- the
 * two kinds combine by OR: a slot hides if ANY unchecked box's class matches
 * it, so "hide Existing" and "hide Inherited" independently narrow the view
 * (an inherited existing-state slot needs both boxes checked to stay visible).
 *
 * Scope: a legend only filters slots within its own card, so multiple racks
 * rendered on one page keep independent filters. Legend swatches themselves
 * carry the same nbx-rd-state-<state> / nbx-rd-flag-<flag> classes too, so
 * they're explicitly excluded.
 */
(function () {
    "use strict";

    function scopeFor(legend) {
        return legend.closest(".card-body") || legend.closest(".card") || document;
    }

    function selectorFor(box) {
        var state = box.getAttribute("data-rd-state");
        if (state) { return ".nbx-rd-state-" + state; }
        var flag = box.getAttribute("data-rd-flag");
        if (flag) { return ".nbx-rd-flag-" + flag; }
        return null;
    }

    function applyFilter(legend) {
        var scope = scopeFor(legend);
        var boxes = legend.querySelectorAll(
            "input[type=checkbox][data-rd-state], input[type=checkbox][data-rd-flag]"
        );
        var allSelectors = [];
        var hiddenSelectors = [];
        boxes.forEach(function (box) {
            var sel = selectorFor(box);
            if (!sel) { return; }
            allSelectors.push(sel);
            if (!box.checked) { hiddenSelectors.push(sel); }
        });
        if (!allSelectors.length) { return; }
        // Recompute over every element any checkbox could affect, so an
        // element that no longer matches an unchecked selector is correctly
        // un-hidden too (not just newly-hidden elements).
        scope.querySelectorAll(allSelectors.join(",")).forEach(function (el) {
            if (el.classList.contains("nbx-rd-swatch")) {
                return; // never hide the legend's own swatch
            }
            var hide = hiddenSelectors.some(function (sel) { return el.matches(sel); });
            el.classList.toggle("nbx-rd-filtered-out", hide);
        });
    }

    function init() {
        document.querySelectorAll("[data-rd-legend]").forEach(function (legend) {
            legend.addEventListener("change", function (event) {
                var t = event.target;
                if (t && t.matches && t.matches(
                        "input[type=checkbox][data-rd-state], input[type=checkbox][data-rd-flag]")) {
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
