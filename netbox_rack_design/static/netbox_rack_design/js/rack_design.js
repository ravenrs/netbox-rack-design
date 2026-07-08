/*
 * Read-only initialiser for the projected MULTI-RACK elevation.
 *
 * The read-only evaluation view (design_elevation.html) renders the SAME per-
 * rack blocks as the editor (inc/rack_block.html, editable=False): every scoped
 * rack side by side, BOTH Front and Rear faces, the full-depth opposite-face
 * hatch and the hover card — but with NO edit affordances. This module is the
 * viewer counterpart of editor.js:
 *
 *   - Every rack grid (front / rear / tray) is initialised with staticGrid:true
 *     so GridStack only lays the slots out by their gs-y / gs-h attributes and
 *     allows no dragging, resizing or dropping.
 *   - Per-rack Front/Rear toggles show/hide a single face (a pure view control,
 *     never an edit); the last visible face of a rack can't be hidden.
 *   - A single body-appended hover card shows each tile's name / role / tenant,
 *     read straight from the data-* attributes (no network calls).
 *
 * No state is ever mutated and there is no save endpoint.
 */
(function () {
    "use strict";

    if (typeof GridStack === "undefined") {
        return;
    }

    var root = document.getElementById("rd-elevation");
    if (!root) {
        return;
    }

    // ---- Static GridStack init for every rack grid -------------------------
    function initGrid(el) {
        if (!el) {
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

    root.querySelectorAll(".grid-stack").forEach(initGrid);

    // ---- Per-rack independent Front/Rear toggles (view-only) ---------------
    root.querySelectorAll(".nbx-rd-rack-block").forEach(function (block) {
        var rackId = block.getAttribute("data-rack-id");
        var faceFront = document.getElementById("nbx-rd-face-front-" + rackId);
        var faceRear = document.getElementById("nbx-rd-face-rear-" + rackId);
        var btnFront = block.querySelector("[data-rd-show-front]");
        var btnRear = block.querySelector("[data-rd-show-rear]");

        function faceVisible(faceEl) {
            return !!faceEl && faceEl.style.display !== "none";
        }
        function setFace(faceEl, btnEl, on) {
            if (faceEl) { faceEl.style.display = on ? "" : "none"; }
            if (btnEl) {
                btnEl.classList.toggle("active", on);
                btnEl.setAttribute("aria-pressed", on ? "true" : "false");
            }
        }
        function toggleFace(faceEl, btnEl, otherFaceEl) {
            if (!faceEl) { return; }
            var on = faceVisible(faceEl);
            if (on && !faceVisible(otherFaceEl)) { return; }   // keep one face on
            setFace(faceEl, btnEl, !on);
        }
        if (btnFront) {
            btnFront.addEventListener("click", function () {
                toggleFace(faceFront, btnFront, faceRear);
            });
        }
        if (btnRear) {
            btnRear.addEventListener("click", function () {
                toggleFace(faceRear, btnRear, faceFront);
            });
        }
    });

    // ---- Shared device hover card (name / role / tenant) -------------------
    // One body-appended floating card, shown on hover over any device tile in
    // any rack block (and the trays). It reads ONLY data-* attributes stamped on
    // each tile — no network calls. Mirrors the editor's hover card.
    (function () {
        var hcard = document.createElement("div");
        hcard.className = "nbx-rd-hovercard";
        hcard.setAttribute("role", "tooltip");
        hcard.style.display = "none";
        document.body.appendChild(hcard);
        var currentContent = null;

        function hideCard() {
            hcard.style.display = "none";
            currentContent = null;
        }

        function fillCard(content) {
            var name = content.getAttribute("data-name");
            var deviceType = content.getAttribute("data-device-type-name");
            var role = content.getAttribute("data-role-name");
            var tenant = content.getAttribute("data-tenant-name");
            if (!name && !deviceType && !role && !tenant) { return false; }
            hcard.textContent = "";
            if (name) {
                var n = document.createElement("div");
                n.className = "nbx-rd-hovercard-name";
                n.textContent = name;
                hcard.appendChild(n);
            }
            [["Type", deviceType], ["Role", role], ["Tenant", tenant]].forEach(function (pair) {
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

        root.addEventListener("pointerover", function (e) {
            var content = e.target.closest && e.target.closest(".grid-stack-item-content");
            if (!content || content === currentContent) { return; }
            if (!fillCard(content)) { hideCard(); return; }
            currentContent = content;
            positionCard(content);
        });
        root.addEventListener("pointerout", function (e) {
            var content = e.target.closest && e.target.closest(".grid-stack-item-content");
            if (!content) { return; }
            if (e.relatedTarget && content.contains(e.relatedTarget)) { return; }
            hideCard();
        });
        window.addEventListener("scroll", hideCard, true);
    })();
})();
