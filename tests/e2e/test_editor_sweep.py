#!/usr/bin/env python3
"""Deterministic, self-provisioning Playwright e2e regression: sweep an
EXISTING full-depth device across every 0.5U row of both rack faces
(including cross-face hops and a deliberately rejected onto-occupied drop),
asserting a fixed set of invariants after every step.

DETERMINISM: unlike the original version of this file, no step here uses a
real pixel-based Playwright mouse drag. Every move is driven through the
in-page ``window.__rdSweep`` shim injected below, which fires editor.js's
REAL GridStack event handlers (``dragstart``/``change``/``dragstop`` for a
same-grid move, ``dragstart`` + a synthesised destination ``dropped`` for a
cross-face move — mirroring editor.js's own ``homeInto`` primitive and the
real GridStack cross-grid drop sequence) with EXACT grid coordinates. There
is no pixel geometry, no timing race, and no dependency on GridStack's pixel
snapping.

SELF-PROVISIONING: ``setUpClass`` creates its OWN throwaway manufacturer,
device role, site, three device types (a 1U half-depth + a 2U half-depth
"obstacle" pair, and a 4U full-depth device to sweep), a fresh modest rack
(16U by default), and three devices at KNOWN positions leaving KNOWN free
gaps — so both "move to a free slot" and "move onto an occupied slot" (which
must be rejected and snap back) are deterministically reachable within a
full 0.5U sweep. A throwaway design with NO placements is created over that
rack (the rack's own real devices already render as ``existing`` tiles).
``tearDownClass`` deletes the design, then the created devices/types/rack/
role/manufacturer/site, in dependency order, best-effort. If DCIM creation
is blocked (permissions/validation), the suite FALLS BACK to discovering an
existing rack with a positioned full-depth device via the API, and skips
cleanly if none is found. Which path ran is logged and asserted in the test
output.

Prerequisites (auto-detected — the suite SKIPS cleanly, never fails, when
any is missing): the ``playwright`` package + a Chrome channel installed,
and a reachable dev server on ``RD_BASE``. Config (RD_BASE/RD_USER/RD_PASS)
comes from the environment exactly as ``dev/config.sh`` exports it.

Run via ``dev/e2e.sh tests.e2e.test_editor_sweep``.
"""
import json
import os
import unittest
import urllib.error
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# Configuration (matches dev/config.sh)
# ---------------------------------------------------------------------------
BASE = os.environ.get("RD_BASE", "http://127.0.0.1:8000").rstrip("/")
USER = os.environ.get("RD_USER", "rd_shot")
PASS = os.environ.get("RD_PASS", "ShotPass12345!")

# Modest so a FULL (non-sampled) 0.5U sweep of both faces stays fast while
# still being a real multi-row rack.
RACK_U_HEIGHT = int(os.environ.get("RD_SWEEP_RACK_U_HEIGHT", "20"))
# Settle wait after each shim-driven move. The shim fires editor.js's real
# handlers synchronously; the only async work is editor.js's own
# `window.setTimeout(refreshGhosts, 0)` / `setTimeout(thawAllTiles, 0)`, so a
# short fixed wait reliably lets those zero-delay timers flush.
STEP_SETTLE_MS = 60


# ---------------------------------------------------------------------------
# Prerequisite guard — SKIP CLEANLY (do not fail) when the environment is not
# ready (mirrors test_editor_e2e._check_prereqs).
# ---------------------------------------------------------------------------
def _check_prereqs():
    try:
        import playwright.sync_api  # noqa: F401
    except Exception as exc:  # pragma: no cover - env-dependent
        return False, f"playwright not importable ({exc})"

    try:
        req = urllib.request.Request(f"{BASE}/login/", method="GET")
        with urllib.request.urlopen(req, timeout=4) as resp:
            if resp.status >= 500:
                return False, f"dev server at {BASE} returned {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            return False, f"dev server at {BASE} returned {exc.code}"
    except Exception as exc:
        return False, f"dev server at {BASE} not reachable ({exc})"

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            b = p.chromium.launch(channel="chrome", headless=True)
            b.close()
    except Exception as exc:  # pragma: no cover - env-dependent
        return False, f"headless Chrome unavailable ({exc})"

    return True, ""


_PREREQ_OK, _PREREQ_REASON = _check_prereqs()


# ---------------------------------------------------------------------------
# In-page harness. Built per-class once the fixture's rack id is known (the
# rack is provisioned dynamically, unlike the fixed RACK_PK env vars the
# older discovery suites used). Everything is scoped to ONE rack block.
# ---------------------------------------------------------------------------
HARNESS_JS_TEMPLATE = r"""
window.__rdSweep = (function () {
    var RACK_PK = "%(rack_pk)s";
    var root = document.getElementById("rd-rack-" + RACK_PK);

    var baseWidgets = JSON.parse(
        (document.getElementById("rd-editor-data-" + RACK_PK) || {}).textContent || "[]");

    function frontGrid() { return document.getElementById("nbx-rd-grid-front-" + RACK_PK).gridstack; }
    function rearGrid() { return document.getElementById("nbx-rd-grid-rear-" + RACK_PK).gridstack; }
    function gridFor(face) { return face === "front" ? frontGrid() : rearGrid(); }
    function hostFor(face) { return document.getElementById("nbx-rd-grid-" + face + "-" + RACK_PK); }

    function tileEl(idx) {
        return root.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
    }

    function fireHandler(grid, name, arg) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler[name];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: name }, arg); });
    }
    function fireDropped(grid, newNode) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler["dropped"];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: "dropped" }, null, newNode); });
    }

    // Low-level, collision-engine-BYPASSING position write: mutate the
    // gridstack node's x/y directly, then re-render via GridStack's own
    // `_writePosAttr` (the same private helper GridStack itself calls to
    // paint a node's gs-x/gs-y attrs + CSS position after ITS OWN internal
    // collision pass runs). We deliberately do NOT call the public
    // `grid.update()` here: when the destination cell is blocked by a
    // frozen/locked neighbour (every other tile is frozen for the duration
    // of a "drag" -- see freezeAllTiles/onDragStart), `update()`'s
    // collision-avoidance (`_fixCollisions`/`moveNode`) recurses trying to
    // push the (unpushable, locked) neighbour and blows the call stack --
    // confirmed by reproducing it. A REAL mouse drag never hits this because
    // GridStack's native DnD pipeline does not route through the public
    // `update()` API mid-drag. This is safe for our purposes because
    // editor.js's OWN overlap detection (`tileOverlapsOther`, invoked from
    // `maybePromptMove` on `dragstop`/`dropped`) does its own independent
    // manual scan over live grid items -- it does not depend on GridStack's
    // engine having resolved collisions for us, so the reject/snap-back path
    // still runs exactly as it would on a real drag.
    function fastSetY(grid, el, newGsY) {
        var node = el.gridstackNode;
        node.x = 0;
        node.y = newGsY;
        // These grids run in GridStack's `float:true` mode, which tracks each
        // node's `_orig` (x/y) snapshot and will silently "pack" it back
        // toward `_orig` the next time ANY repack pass runs (e.g. GridStack's
        // internal `_packNodes`, triggered by editor.js's own `thaw()` -> a
        // plain `grid.update()` call on an unrelated, previously-frozen
        // tile). Since we mutate `node.y` directly (bypassing GridStack's own
        // drag lifecycle, which normally refreshes `_orig` at each drag
        // step), `_orig` would still point at the position this node had
        // when it was FIRST added to this grid engine -- so a later repack
        // silently reverts our move. Confirmed by reproducing it: a
        // cross-face move landed at the right y synchronously, then silently
        // snapped back moments later once `thaw()`'s repack ran. Syncing
        // `_orig` to the position we just wrote makes it a no-op for any
        // future repack.
        node._orig = { x: node.x, y: node.y };
        grid._writePosAttr(el, node);
    }

    // Deterministic SAME-GRID move: fires the real dragstart -> (position
    // write) -> change -> dragstop sequence a same-face drag performs.
    function moveTile(idx, newGsY) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var grid = el.gridstackNode.grid;
        fireHandler(grid, "dragstart", el);
        fastSetY(grid, el, newGsY);
        fireHandler(grid, "change", []);
        fireHandler(grid, "dragstop", el);
        return true;
    }

    // Deterministic CROSS-FACE move. Real GridStack does NOT fire dragstop
    // for a cross-grid drag -- only `dropped` on the destination grid, after
    // the browser has already moved the element's DOM node into the
    // destination container and adopted it into the destination engine. We
    // reproduce that exactly, mirroring editor.js's own `homeInto()`
    // primitive (used for its script-driven cross-grid re-homes):
    //   1. fire `dragstart` on the SOURCE grid (sets tileInFlight + freezes
    //      every other tile, exactly like a real drag start)
    //   2. detach the tile from the source grid's engine (keep the DOM node)
    //   3. re-parent the DOM node into the destination grid's host
    //   4. adopt it at a KNOWN-FREE slot (row 0 -- never occupied by our
    //      fixtures) via `makeWidget`, so GridStack's own registration-time
    //      collision pass has nothing to push against, then reposition to
    //      the real target row via the same collision-bypassing
    //      `fastSetY` used above (for the same reason: the target row may
    //      be blocked by a frozen/locked neighbour).
    //   5. fire the destination grid's registered `dropped` handler with a
    //      synthesised newNode `{el}` (onPaletteDrop's non-palette branch
    //      only ever reads `newNode.el`; `previousNode` is never read at
    //      all, so `null` is safe -- confirmed by reading editor.js).
    function moveTileToFace(idx, face, newGsY) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var srcGrid = el.gridstackNode.grid;
        fireHandler(srcGrid, "dragstart", el);
        var destGrid = gridFor(face);
        var destHost = hostFor(face);
        srcGrid.removeWidget(el, false);
        if (el.parentNode !== destHost) { destHost.appendChild(el); }
        el.setAttribute("gs-x", "0");
        el.setAttribute("gs-y", "0");
        destGrid.makeWidget(el);
        fastSetY(destGrid, el, newGsY);
        var newNode = el.gridstackNode || { el: el };
        fireDropped(destGrid, newNode);
        return true;
    }

    function faceOf(el) {
        if (el.closest("#nbx-rd-grid-front-" + RACK_PK)) { return "front"; }
        if (el.closest("#nbx-rd-grid-rear-" + RACK_PK)) { return "rear"; }
        return "other";
    }

    // Phase 3 (spec §2.2 live mid-drag tracking test): split a same-grid move
    // into a BEGIN half (dragstart -> position write -> change, mirroring a
    // real drag's per-cell tick) and an END half (dragstop), so a test can
    // inspect the shadow's position/class BETWEEN the two -- i.e. while the
    // gesture is still open, before the drop settles.
    var _dragEl = null;
    function dragBeginAndMove(idx, newGsY) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var grid = el.gridstackNode.grid;
        fireHandler(grid, "dragstart", el);
        fastSetY(grid, el, newGsY);
        fireHandler(grid, "change", []);
        _dragEl = el;
        return true;
    }
    function dragEnd() {
        if (!_dragEl) { return false; }
        var grid = _dragEl.gridstackNode.grid;
        fireHandler(grid, "dragstop", _dragEl);
        _dragEl = null;
        return true;
    }

    // Click the × affordance on the tile at `idx` -- the same flag-for-
    // removal / cancel-move toggle a real click drives.
    function clickRemove(idx) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var btn = el.querySelector(".nbx-rd-remove-btn");
        if (!btn) { return false; }
        btn.click();
        return true;
    }

    // Every owned opposite-face hatch (shadow or ghost-mirror) matching
    // `label`, with its FULL class list -- used to assert the state-tinted
    // rendering (spec §3) and the owner-identity attributes (spec §7 goal 5),
    // not just live/ghost + face/y like `snapshot` above.
    function hatchesFor(label) {
        var out = [];
        root.querySelectorAll("[data-rd-derived-opp]").forEach(function (el) {
            var l = (el.querySelector(".nbx-rd-label") || {}).textContent || null;
            if (l !== label) { return; }
            var n = el.gridstackNode;
            out.push({
                face: faceOf(el), y: n ? n.y : null, h: n ? n.h : null,
                classes: Array.prototype.slice.call(el.classList),
                ownerWidx: el.getAttribute("data-rd-owner-widx"),
                ownerRack: el.getAttribute("data-rd-owner-rack"),
            });
        });
        return out;
    }

    // A live, interactive device tile: excludes derived opposite-face
    // hatches, move-out ghosts and temp ghosts.
    function isLiveTile(el) {
        if (el.getAttribute("data-rd-derived-opp")) { return false; }
        if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return false; }
        if (el.hasAttribute("data-rd-temp-ghost")) { return false; }
        return true;
    }

    // Full invariant snapshot for the tile at `idx`, matched additionally by
    // `label` (an opposite-face hatch element carries no widget-index of its
    // own, so hatch lookup keys off the swept device's stable label).
    function snapshot(idx, label) {
        var tiles = Array.prototype.slice.call(
            root.querySelectorAll('.grid-stack-item[data-widget-index="' + idx + '"]')
        ).filter(isLiveTile);
        var tile = tiles[0] || null;

        var dis = 0, tot = 0, disIdx = [];
        root.querySelectorAll(".grid-stack-item").forEach(function (el) {
            if (el.getAttribute("data-rd-derived-opp")) { return; }
            if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
            if (el.classList.contains("nbx-rd-state-remove")) { return; }
            tot++;
            if (el.classList.contains("ui-draggable-disabled")) {
                dis++; disIdx.push(el.getAttribute("data-widget-index"));
            }
        });

        var out = {
            existsCount: tiles.length, frozen: dis, frozenTotal: tot, frozenIdx: disIdx,
            face: null, y: null, h: null, overlap: [], hatchLive: [], hatchGhost: [],
            tileClasses: [],
        };
        if (tile) {
            out.face = faceOf(tile);
            out.tileClasses = Array.prototype.slice.call(tile.classList);
            var n = tile.gridstackNode;
            out.y = n ? n.y : null;
            out.h = n ? n.h : null;
            var grid = gridFor(out.face);
            if (grid && n) {
                grid.getGridItems().forEach(function (el) {
                    if (el === tile) { return; }
                    if (el.getAttribute("data-rd-derived-opp")) { return; }
                    if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                    if (el.hasAttribute("data-rd-temp-ghost")) { return; }
                    var on = el.gridstackNode;
                    if (!on) { return; }
                    if (n.y < on.y + on.h && on.y < n.y + n.h) {
                        out.overlap.push(el.getAttribute("data-widget-index"));
                    }
                });
            }
        }
        root.querySelectorAll("[data-rd-derived-opp]").forEach(function (el) {
            var l = (el.querySelector(".nbx-rd-label") || {}).textContent || null;
            if (l !== label) { return; }
            var n = el.gridstackNode;
            var rec = {
                face: faceOf(el), y: n ? n.y : null,
                classes: Array.prototype.slice.call(el.classList),
            };
            if (el.classList.contains("nbx-rd-opposite-ghost")) { out.hatchGhost.push(rec); }
            else { out.hatchLive.push(rec); }
        });
        return out;
    }

    function tileInfo(idx) {
        var el = tileEl(idx);
        if (!el) { return null; }
        var n = el.gridstackNode;
        return {
            classes: Array.prototype.slice.call(el.classList),
            y: n ? n.y : null, h: n ? n.h : null, face: faceOf(el),
        };
    }

    // ---- Full-world snapshot + diff (every entity, not just the subject) --
    // A per-step check scoped to "the subject's own position/shadow/model
    // invariants" provably misses a bystander tile picking up a stale class
    // tint or an owner-identity attribute drifting -- exactly the class of
    // bug (stale shadow tint, orphaned mirror) users keep finding by hand in
    // two clicks that a long scoped sweep sails past. worldSnapshot captures
    // EVERY .grid-stack-item in this rack (skipping nothing): its kind, its
    // full nbx-* class list (sorted, so class-ORDER never spuriously
    // differs), geometry, owner identity, and tooltip title. diffWorlds then
    // asserts every entity NOT owned by the swept subject is byte-for-byte
    // identical between two snapshots -- the swept subject is the only thing
    // ALLOWED to change, and only it is excluded from the comparison.
    function worldSnapshot() {
        var out = [];
        root.querySelectorAll(".grid-stack-item").forEach(function (el) {
            var n = el.gridstackNode;
            var gsY = (n && n.y != null) ? n.y : parseInt(el.getAttribute("gs-y"), 10);
            var gsH = (n && n.h != null) ? n.h : parseInt(el.getAttribute("gs-h"), 10);
            var kind;
            if (el.getAttribute("data-rd-derived-opp")) {
                kind = el.classList.contains("nbx-rd-opposite-ghost") ? "ghost-mirror" : "shadow";
            } else if (el.hasAttribute("data-rd-temp-ghost")) {
                kind = "temp-ghost";
            } else if (el.classList.contains("nbx-rd-state-move_out_ghost")) {
                kind = "ghost";
            } else {
                kind = "body";
            }
            var span = el.querySelector(".nbx-rd-label");
            var content = el.querySelector(".grid-stack-item-content");
            var classes = [];
            el.classList.forEach(function (c) { if (c.indexOf("nbx-") === 0) { classes.push(c); } });
            classes.sort();
            out.push({
                face: faceOf(el), kind: kind,
                label: span ? span.textContent : "",
                gsY: isNaN(gsY) ? null : gsY, gsH: isNaN(gsH) ? null : gsH,
                classes: classes,
                ownerWidx: el.getAttribute("data-rd-owner-widx"),
                widx: el.getAttribute("data-widget-index"),
                title: content ? (content.getAttribute("title") || "") : "",
                displaced: el.classList.contains("nbx-rd-displaced"),
            });
        });
        out.sort(function (a, b) {
            function key(x) {
                return [x.face, x.kind, x.label, x.widx || "", x.ownerWidx || "", x.gsY].join("|");
            }
            var ka = key(a), kb = key(b);
            return ka < kb ? -1 : (ka > kb ? 1 : 0);
        });
        return out;
    }

    // Identity across two snapshots: face+kind+label(+ownerWidx for a hatch,
    // which can share a label with its owner) -- deliberately NOT position,
    // so a genuine move is a "changed" entity, never a spurious
    // removed+added pair.
    function _entityKey(e) {
        var extra = (e.kind === "shadow" || e.kind === "ghost-mirror") ? ("#" + (e.ownerWidx || "")) : "";
        return e.face + "|" + e.kind + "|" + e.label + extra;
    }

    // A ghost's displaced/stripe state is SUBJECT-COUPLED presentation
    // (spec §4.3.3/§4.3.5: it collapses while the swept subject occupies
    // its rows and restores when the subject leaves), so for ghost-kind
    // entities the `displaced` flag and its class are normalized out of
    // the bystander comparison -- their POSITION is still strictly checked.
    function _isGhostKind(e) {
        return e.kind === "ghost" || e.kind === "temp-ghost" || e.kind === "ghost-mirror";
    }
    function _classesForDiff(e) {
        if (!_isGhostKind(e)) { return e.classes.join(","); }
        return e.classes.filter(function (c) { return c !== "nbx-rd-displaced"; }).join(",");
    }

    // `subjectLabel` (nullable): entities with this label are EXEMPT from
    // the diff (the subject is expected to change). Pass null/"" to compare
    // EVERYTHING (e.g. a rejected drop, where nothing -- subject included --
    // may differ).
    function diffWorlds(prev, cur, subjectLabel) {
        var prevByKey = {}, curByKey = {};
        prev.forEach(function (e) { prevByKey[_entityKey(e)] = e; });
        cur.forEach(function (e) { curByKey[_entityKey(e)] = e; });
        function exempt(e) { return !!subjectLabel && e.label === subjectLabel; }
        var violations = [];
        Object.keys(prevByKey).forEach(function (k) {
            var before = prevByKey[k];
            if (exempt(before)) { return; }
            var after = curByKey[k];
            if (!after) {
                violations.push({ kind: "bystander_vanished", key: k, before: before });
                return;
            }
            var fields = _isGhostKind(before)
                ? ["face", "gsY", "gsH"]
                : ["face", "gsY", "gsH", "displaced"];
            fields.forEach(function (f) {
                if (before[f] !== after[f]) {
                    violations.push({
                        kind: "bystander_field_changed", key: k, field: f,
                        before: before[f], after: after[f],
                    });
                }
            });
            var bc = _classesForDiff(before), ac = _classesForDiff(after);
            if (bc !== ac) {
                violations.push({ kind: "bystander_classes_changed", key: k, before: bc, after: ac });
            }
            if (before.title !== after.title) {
                violations.push({
                    kind: "bystander_title_changed", key: k,
                    before: before.title, after: after.title,
                });
            }
        });
        Object.keys(curByKey).forEach(function (k) {
            if (!prevByKey[k] && !exempt(curByKey[k])) {
                violations.push({ kind: "bystander_appeared", key: k, after: curByKey[k] });
            }
        });
        return violations;
    }

    return {
        baseWidgets: baseWidgets,
        moveTile: moveTile,
        moveTileToFace: moveTileToFace,
        snapshot: snapshot,
        tileInfo: tileInfo,
        dragBeginAndMove: dragBeginAndMove,
        dragEnd: dragEnd,
        clickRemove: clickRemove,
        hatchesFor: hatchesFor,
        worldSnapshot: worldSnapshot,
        diffWorlds: diffWorlds,
    };
})();
"""


@unittest.skipUnless(_PREREQ_OK, f"editor sweep prerequisites not met: {_PREREQ_REASON}")
class EditorSweepTestCase(unittest.TestCase):
    """Sweeps a real, self-provisioned full-depth device across every 0.5U
    row of both rack faces via the deterministic ``__rdSweep`` shim,
    collecting every invariant violation across the whole sweep and failing
    once at the end with the complete list."""

    # ------------------------------------------------------------------
    # REST plumbing: session-cookie auth (from the Playwright login) plus
    # the csrftoken cookie sent back as X-CSRFToken.
    # ------------------------------------------------------------------
    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    # ------------------------------------------------------------------
    # Primary fixture: our OWN manufacturer/role/site/device-types/rack with
    # devices at KNOWN positions (a 4U full-depth device to sweep, plus a 1U
    # front and 2U rear "obstacle" device so onto-occupied drops are
    # deterministically reachable during the sweep).
    # ------------------------------------------------------------------
    @staticmethod
    def _u_to_gsy(rack_u_height, u_position, gs_h):
        """Mirror of editor.js's uPositionToGsY for an ASCENDING-units rack
        (desc_units=false, which is what we provision)."""
        if gs_h > 2:
            return rack_u_height * 2 - u_position * 2 - gs_h + 2
        return rack_u_height * 2 - u_position * 2

    @classmethod
    def _provision_primary(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E Sweep Mfr {suffix}", "slug": f"e2e-sweep-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E Sweep Role {suffix}", "slug": f"e2e-sweep-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Sweep Site {suffix}", "slug": f"e2e-sweep-site-{suffix}",
            "status": "active"})
        dt_half1 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-1U-Half-{suffix}",
            "slug": f"e2e-1u-half-{suffix}", "u_height": 1, "is_full_depth": False})
        dt_half2 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-2U-Half-{suffix}",
            "slug": f"e2e-2u-half-{suffix}", "u_height": 2, "is_full_depth": False})
        dt_full4 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-4U-Full-{suffix}",
            "slug": f"e2e-4u-full-{suffix}", "u_height": 4, "is_full_depth": True})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Sweep Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": RACK_U_HEIGHT})

        # This dev instance has a pre-existing global custom field
        # ("warranty_type", text-typed) whose configured default is a
        # non-string value, so leaving it unset on a POST 400s. Not our
        # plugin's concern -- just satisfy it explicitly on every device we
        # create so provisioning doesn't spuriously fall back.
        cf_override = {"custom_fields": {"warranty_type": ""}}

        # Swept device: 4U full-depth at U6-9, front.
        dev_full = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-sweep-full-{suffix}", "device_type": dt_full4["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "6.0", "face": "front", "status": "active", **cf_override})
        # Front obstacle: 1U half-depth at U14, front (rejects a front drop
        # onto it). Positioned with a full swept-device-height of headroom
        # BELOW it (towards gs-row-increasing / low-U direction): editor.js's
        # own cancelMove/tileOverlapsOther revert path calls GridStack's
        # public grid.update(), whose collision-avoidance pushes the
        # colliding node PAST a locked blocker by the blocker's own height --
        # if there isn't enough room left in the (modest) rack to complete
        # that push, GridStack's push-then-boundary-clamp cycle recurses
        # forever (confirmed by reproducing a call-stack overflow with a
        # tightly-packed 16U rack). Generous headroom avoids that entirely.
        dev_obst_front = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-sweep-obst-front-{suffix}", "device_type": dt_half1["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "14.0", "face": "front", "status": "active", **cf_override})
        # Rear obstacle: 2U half-depth at U16, rear (rejects a rear drop onto
        # it) -- same headroom reasoning as the front obstacle above.
        dev_obst_rear = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-sweep-obst-rear-{suffix}", "device_type": dt_half2["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "16.0", "face": "rear", "status": "active", **cf_override})

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"], rack=rack["id"],
            device_types=[dt_half1["id"], dt_half2["id"], dt_full4["id"]],
            devices=[dev_full["id"], dev_obst_front["id"], dev_obst_rear["id"]],
        )
        cls._owns_dcim = True
        cls._rack_id = rack["id"]
        cls._rack_u_height = RACK_U_HEIGHT
        cls._swept_device_id = dev_full["id"]
        cls._swept_u_height = 4
        # Known origin (page-load) gs-row of the swept device -- what a
        # rejected drop must snap back to.
        cls._swept_orig_gsy = cls._u_to_gsy(RACK_U_HEIGHT, 6, 8)
        # A gs-row that overlaps the front obstacle (U14, gsH=2 -> rows 12-13)
        # while still within the swept device's normal sweep range, used for
        # the explicit rejected-drop assertion.
        cls._front_obstacle_reject_row = 8  # spans [8,16) -> includes rows 12-13

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"sweep-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

    # ------------------------------------------------------------------
    # Fallback: discover an EXISTING rack with a positioned full-depth
    # device via the API. No obstacle guarantees, so the explicit
    # onto-occupied assertion is skipped on this path (logged), but the
    # general 0.5U sweep + invariants still run in full.
    # ------------------------------------------------------------------
    @classmethod
    def _provision_fallback(cls):
        cls._owns_dcim = False
        racks = cls._api("GET", "/api/dcim/racks/?limit=50")["results"]
        for rack in racks:
            rid = rack["id"]
            devs = cls._api(
                "GET", f"/api/dcim/devices/?rack_id={rid}&limit=0")["results"]
            devs = [d for d in devs if d.get("position") is not None]
            fd = None
            for d in devs:
                dt = cls._api(
                    "GET", f"/api/dcim/device-types/{d['device_type']['id']}/")
                if dt.get("is_full_depth"):
                    fd = d
                    break
            if fd is None:
                continue
            cls._rack_id = rid
            cls._rack_u_height = int(rack["u_height"])
            cls._swept_device_id = fd["id"]
            cls._swept_u_height = None  # discovered dynamically from baseWidgets
            cls._front_obstacle_reject_row = None  # no known obstacle on this path
            design = cls._api("POST", "/api/plugins/rack-design/designs/", {
                "title": f"sweep-fallback-{uuid.uuid4()}",
                "site": rack["site"]["id"], "racks": [rid]})
            cls._design_id = design["id"]
            cls.editor_url = (
                f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rid}/")
            return
        raise unittest.SkipTest(
            "primary DCIM provisioning failed AND no fallback rack with a "
            "positioned full-depth device was found via API discovery")

    @classmethod
    def _provision_fixture(cls):
        try:
            cls._provision_primary()
            cls._fixture_path = (
                "primary: created own manufacturer/role/site/rack/device-types/"
                "devices (4U full-depth swept device + 1U front / 2U rear "
                "obstacles)")
        except unittest.SkipTest:
            raise
        except Exception as exc:
            cls._fixture_path = f"fallback via API discovery (primary failed: {exc})"
            print(f"[sweep] {cls._fixture_path}")
            cls._provision_fallback()
        print(f"[sweep] fixture path: {cls._fixture_path}")

    # ------------------------------------------------------------------
    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
                cls._design_id = None
            created = getattr(cls, "_created", None)
            if created:
                for did in created.get("devices", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/devices/{did}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                if created.get("rack") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{created['rack']}/")
                    except Exception:
                        pass
                if created.get("role") is not None:
                    try:
                        cls._api(
                            "DELETE", f"/api/dcim/device-roles/{created['role']}/")
                    except Exception:
                        pass
                if created.get("manufacturer") is not None:
                    try:
                        cls._api(
                            "DELETE",
                            f"/api/dcim/manufacturers/{created['manufacturer']}/")
                    except Exception:
                        pass
                if created.get("site") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
                    except Exception:
                        pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._created = None
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1600, "height": 1400})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._storage = cls._api_ctx.storage_state()
            cls._csrf = next(
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
            cls._provision_fixture()
            cls.HARNESS_JS = HARNESS_JS_TEMPLATE % {"rack_pk": cls._rack_id}
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    # ------------------------------------------------------------------
    def _load_editor(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)  # let GridStack finish init
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    def widx(self, **match):
        widgets = self.page.evaluate("() => window.__rdSweep.baseWidgets")
        for idx, w in enumerate(widgets):
            if w.get("opposite_face"):
                continue
            if all(w.get(k) == v for k, v in match.items()):
                return idx, w
        self.fail(f"no widget matching {match} in baseWidgets: {widgets}")

    # ------------------------------------------------------------------
    # Per-step invariant check (deterministic INVARIANTS, not one-shot
    # assertions): accumulates violations instead of failing at the first
    # one, so a single run surfaces every broken case.
    # ------------------------------------------------------------------
    def _check(self, idx, label, full_depth, face_hint, row, phase, violations):
        if self.errors:
            for e in self.errors:
                violations.append(dict(
                    phase=phase, face=face_hint, row=row,
                    kind="page_error", detail=e))
            self.errors = []

        # Full-world diff (upgrade, 2026-07-08): a check scoped to the
        # subject alone provably misses a bystander tile drifting (a stale
        # class tint, an owner-identity attribute change) -- compare EVERY
        # entity in the rack against the previous step's snapshot; only the
        # swept subject itself is exempt.
        world = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")
        if getattr(self, "_prevWorld", None) is not None:
            world_violations = self.page.evaluate(
                "([prev, cur, lbl]) => window.__rdSweep.diffWorlds(prev, cur, lbl)",
                [self._prevWorld, world, label])
            for wv in world_violations:
                violations.append(dict(phase=phase, face=face_hint, row=row,
                                        kind="world_" + wv["kind"], detail=wv))
        self._prevWorld = world

        snap = self.page.evaluate(
            f"() => window.__rdSweep.snapshot('{idx}', {json.dumps(label)})")

        if snap["existsCount"] != 1:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="tile_count",
                detail=f"expected 1 live tile, found {snap['existsCount']}"))
        if snap["frozen"] > 0:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="lockup",
                detail=f"{snap['frozen']}/{snap['frozenTotal']} tiles left "
                       f"ui-draggable-disabled: {snap['frozenIdx']}"))
        if snap["overlap"]:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="overlap",
                detail=f"swept tile overlaps real tile(s): {snap['overlap']}"))
        if full_depth and snap["face"] is not None:
            opp = "rear" if snap["face"] == "front" else "front"
            live = snap["hatchLive"]
            if len(live) != 1:
                violations.append(dict(
                    phase=phase, face=face_hint, row=row, kind="hatch_count",
                    detail=f"expected 1 live opposite-face shadow, found "
                           f"{len(live)}: {live}"))
            else:
                h0 = live[0]
                if h0["face"] != opp:
                    violations.append(dict(
                        phase=phase, face=face_hint, row=row, kind="hatch_face",
                        detail=f"live shadow on {h0['face']}, expected {opp}: {h0}"))
                elif h0["y"] != snap["y"]:
                    violations.append(dict(
                        phase=phase, face=face_hint, row=row, kind="hatch_y",
                        detail=f"live shadow y={h0['y']} != tile y={snap['y']}: {h0}"))
                else:
                    # Phase 3 (spec §3, §7 goal 3): the shadow's state class
                    # must follow whichever "nbx-rd-state-*" class the OWNER
                    # tile itself currently carries at this step (existing at
                    # its origin U, move_in once swept away from it) -- never
                    # a stale class left over from a prior step. Tightens the
                    # position-only check above to position+class.
                    owner_state = next(
                        (c for c in snap["tileClasses"]
                         if c.startswith("nbx-rd-state-")), None)
                    if owner_state is not None and owner_state not in h0["classes"]:
                        violations.append(dict(
                            phase=phase, face=face_hint, row=row, kind="hatch_state_class",
                            detail=f"live shadow missing owner's state class "
                                   f"{owner_state!r}: {h0['classes']}"))

        # Phase 1 read-model cross-check (docs/editor-behavior-spec.md §2/§6):
        # independently rebuild the model from the settled DOM and assert its
        # own I1/I2 invariants agree with the hand-rolled checks above.
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : ['window.__rdModel missing'])")
        for mv in model_violations:
            violations.append(dict(
                phase=phase, face=face_hint, row=row, kind="rd_model", detail=mv))
        return snap

    def _assert_clean(self, violations):
        by_kind = {}
        for v in violations:
            by_kind.setdefault(v["kind"], []).append(v)
        lines = [f"{len(violations)} invariant violation(s); by kind: "
                 f"{ {k: len(vs) for k, vs in by_kind.items()} }"]
        for kind, vs in by_kind.items():
            ex = vs[0]
            lines.append(f"  [{kind}] x{len(vs)} — e.g. {ex}")
        self.assertEqual(violations, [], "\n" + "\n".join(lines))

    # =====================================================================
    # Sweep an EXISTING full-depth device across every 0.5U row of both
    # faces, including cross-face hops and a deliberately rejected
    # onto-occupied drop.
    # =====================================================================
    def test_sweep_existing_fulldepth_device(self):
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._swept_device_id)
        label = w.get("label")
        u_height = self._swept_u_height or float(w.get("u_height") or 1)
        h_gs = round(u_height * 2)
        max_row = self._rack_u_height * 2 - h_gs
        violations = []
        steps = 0

        # FRONT: every 0.5U row, no sampling.
        for row in range(0, max_row + 1):
            self.page.evaluate(f"() => window.__rdSweep.moveTile('{idx}', {row})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self._check(idx, label, True, "front", row, "front_sweep", violations)
            steps += 1

        # Explicit rejected-drop assertion (guards Bug B): dropping onto the
        # occupied front obstacle's row must snap back to the device's LAST
        # VALID slot (where this drag began -- here the sweep's final row), AND
        # the live shadow must follow it there, not be left stranded at the
        # rejected slot. (User ruling 2026-07-15: a rejected re-drag of an
        # already-moved device returns to its last valid slot, NOT all the way
        # to the device's origin -- see rejectDrop in editor.js.)
        if getattr(self, "_front_obstacle_reject_row", None) is not None:
            pre_reject = self.page.evaluate(
                f"() => window.__rdSweep.snapshot('{idx}', {json.dumps(label)})")
            last_valid_y = pre_reject["y"]
            self.page.evaluate(
                f"() => window.__rdSweep.moveTile("
                f"'{idx}', {self._front_obstacle_reject_row})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            snap = self._check(
                idx, label, True, "front", self._front_obstacle_reject_row,
                "explicit_reject", violations)
            if snap["y"] != last_valid_y:
                violations.append(dict(
                    phase="explicit_reject", face="front",
                    row=self._front_obstacle_reject_row, kind="reject_snapback",
                    detail=f"drop onto occupied row "
                           f"{self._front_obstacle_reject_row} should have "
                           f"snapped back to its LAST valid slot y={last_valid_y}, "
                           f"got y={snap['y']}"))
            steps += 1

        # Cross to rear.
        self.page.evaluate(
            f"() => window.__rdSweep.moveTileToFace('{idx}', 'rear', {max_row})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self._check(idx, label, True, "rear", max_row, "cross_to_rear", violations)
        steps += 1

        # REAR: every 0.5U row, no sampling.
        for row in range(0, max_row + 1):
            self.page.evaluate(f"() => window.__rdSweep.moveTile('{idx}', {row})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self._check(idx, label, True, "rear", row, "rear_sweep", violations)
            steps += 1

        # Cross-face alternation phase (front<->rear), landing at varied rows.
        current = "rear"
        for i in range(8):
            target = "front" if current == "rear" else "rear"
            row = (i * 3) % (max_row + 1)
            self.page.evaluate(
                f"() => window.__rdSweep.moveTileToFace('{idx}', '{target}', {row})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            snap = self._check(
                idx, label, True, target, row, "cross_alternate", violations)
            current = snap.get("face") or target
            steps += 1

        print(f"\n=== SWEEP SUMMARY (existing full-depth device) ===")
        print(f"  fixture path: {self._fixture_path}")
        print(f"  device: {label!r}, u_height={u_height}, rack_u_height="
              f"{self._rack_u_height}")
        print(f"  total steps: {steps}")
        print(f"  total violations: {len(violations)}")
        self._assert_clean(violations)

    # =====================================================================
    # Regression (user bug 2026-07-15): a device that has ALREADY been moved
    # this session (a move_in at slot A) and is then dragged onto an OCCUPIED
    # slot must snap back to A -- its LAST valid position -- not to its
    # original saved slot O, and nothing else may drift. The user hit "moved a
    # device onto an occupied slot and everything went to hell": the two-step
    # revert target was wrong (reverting a re-dragged move_in undid the earlier
    # accepted move instead of just rejecting the second drag).
    # =====================================================================
    def _mover_pos(self, idx):
        return self.page.evaluate(
            """(idx) => {
                const el = document.querySelector(
                    `.grid-stack-item[data-widget-index="${idx}"]:not([data-rd-derived-opp])`);
                if (!el) { return null; }
                const n = el.gridstackNode;
                const face = el.closest('[data-rd-face]') &&
                    el.closest('[data-rd-face]').getAttribute('data-rd-face');
                return {
                    face: face, y: n && n.y, h: n && n.h,
                    state: [...el.classList].filter(c => c.startsWith('nbx-rd-state')),
                };
            }""", idx)

    def test_moved_device_rejected_second_drop_returns_to_last_valid_slot(self):
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._swept_device_id)
        gs_h = self._swept_u_height * 2  # 4U -> 8 rows

        # Step 1: a VALID move O(U6) -> A(U2). The device becomes a move_in at A.
        slot_a = self._u_to_gsy(self._rack_u_height, 2, gs_h)
        self.page.evaluate(f"() => window.__rdSweep.moveTile('{idx}', {slot_a})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        at_a = self._mover_pos(idx)
        self.assertEqual(
            [at_a["face"], at_a["y"], at_a["h"]], ["front", slot_a, gs_h],
            f"step-1 valid move should land the device at slot A: {at_a}")
        self.assertIn("nbx-rd-state-move_in", at_a["state"], at_a)
        world_at_a = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")

        # Step 2: an ILLEGAL second drag A -> a row overlapping the occupied
        # front/rear obstacles. It must be REJECTED and snap back to A.
        self.page.evaluate(
            f"() => window.__rdSweep.moveTile('{idx}', {self._front_obstacle_reject_row})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.assertEqual(self.errors, [], f"console errors on rejected 2nd drop: {self.errors}")

        after = self._mover_pos(idx)
        self.assertEqual(
            [after["face"], after["y"], after["h"]], ["front", slot_a, gs_h],
            f"a move_in dropped onto an OCCUPIED slot must return to its LAST "
            f"valid slot A (y={slot_a}), not elsewhere: got {after}")

        # Nothing may have drifted versus state A (subject included).
        world_after = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")
        world_viol = self.page.evaluate(
            "([p, c]) => window.__rdSweep.diffWorlds(p, c, null)",
            [world_at_a, world_after])
        self.assertEqual(
            world_viol, [],
            f"rejected 2nd drop must leave the world identical to state A: {world_viol}")

        model_viol = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(
            model_viol, [], f"read-model invariants must be clean: {model_viol}")

    # =====================================================================
    # Regression (user bug 2026-07-15, CROSS-FACE variant): a device moved to a
    # valid slot A on the FRONT (a move_in) and then dragged onto an OCCUPIED
    # slot on the REAR must snap back to A on the FRONT -- not fall to cancelMove
    # -> ghost. The first cut only snapped back when the drop face matched the
    # pre-drag face; a cross-face reject slipped through. The log that pinned it:
    # rejectDrop{preDragFace:"front", curFace:"rear"} -> cancelMove.
    # =====================================================================
    def test_moved_device_rejected_cross_face_drop_returns_to_last_valid_slot(self):
        if getattr(self, "_front_obstacle_reject_row", None) is None:
            self.skipTest("no known obstacle on the fallback fixture")
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._swept_device_id)
        gs_h = self._swept_u_height * 2

        # Step 1: valid FRONT move O(U6) -> A(U2).
        slot_a = self._u_to_gsy(self._rack_u_height, 2, gs_h)
        self.page.evaluate(f"() => window.__rdSweep.moveTile('{idx}', {slot_a})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        at_a = self._mover_pos(idx)
        self.assertEqual(
            [at_a["face"], at_a["y"]], ["front", slot_a],
            f"step-1 move should land on the FRONT at A: {at_a}")

        # Step 2: illegal CROSS-FACE drag to the REAR, onto rows overlapping the
        # rear obstacle (U16, 2U). Must snap back to A on the FRONT.
        rear_reject_row = 6  # dev_full is 4U -> rows 6..13 overlap the rear obstacle
        self.page.evaluate(
            f"() => window.__rdSweep.moveTileToFace('{idx}', 'rear', {rear_reject_row})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.assertEqual(self.errors, [], f"console errors on cross-face reject: {self.errors}")

        after = self._mover_pos(idx)
        self.assertEqual(
            [after["face"], after["y"]], ["front", slot_a],
            f"a cross-face rejected drop must return the device to its last "
            f"valid slot A on the FRONT (y={slot_a}), got {after}")

        model_viol = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(
            model_viol, [], f"read-model invariants must be clean: {model_viol}")

    # =====================================================================
    # Regression (user bug 2026-07-15, variant 2): a RELOADED move_in (kind
    # "move_in", loaded from a saved move -- its st.origUPosition == the move
    # TARGET, ghost at the real origin) that is re-dragged onto an OCCUPIED
    # slot must snap back to its move_in slot A, NOT revert to the ghost / real
    # origin O. The first fix gated snap-back on "pre-drag != origin", which is
    # always false for a move_in (origin==target), so it fell to cancelMove ->
    # ghost. Fix: snap to pre-drag unconditionally (rejectDrop). See
    # docs/editor-known-issues.md.
    # =====================================================================
    def test_reloaded_move_in_rejected_drop_returns_to_move_in_slot_not_ghost(self):
        if getattr(self, "_front_obstacle_reject_row", None) is None:
            self.skipTest("no known obstacle on the fallback fixture")
        # Fresh design over the SAME rack, carrying a PRE-SAVED within-rack move
        # (dev_full U6 -> U2), so on load dev_full is a reloaded move_in at U2
        # with its move-out ghost at U6.
        suffix = uuid.uuid4().hex[:8]
        a_u = 2
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"reload-movein-{suffix}", "site": self._created["site"],
            "racks": [self._rack_id]})
        extra_design_id = design["id"]
        try:
            self._api("POST", "/api/plugins/rack-design/placements/", {
                "design": extra_design_id, "kind": "move",
                "device": self._swept_device_id, "target_rack": self._rack_id,
                "target_position": float(a_u), "target_face": "front"})
            self.editor_url = (
                f"{BASE}/plugins/rack-design/designs/{extra_design_id}/editor/"
                f"{self._rack_id}/")
            self._load_editor()

            gs_h = self._swept_u_height * 2
            a_gsy = self._u_to_gsy(self._rack_u_height, a_u, gs_h)

            # Find the reloaded move_in body tile by device identity + class
            # (robust to the widget payload's kind spelling).
            idx = self.page.evaluate(
                """(devId) => {
                    const el = [...document.querySelectorAll(
                        '.grid-stack-item.nbx-rd-state-move_in')].find(t =>
                            String(t.getAttribute('data-rd-device-id')) === String(devId)
                            && !t.getAttribute('data-rd-derived-opp'));
                    return el ? el.getAttribute('data-widget-index') : null;
                }""", self._swept_device_id)
            self.assertIsNotNone(idx, "reloaded move_in tile not found on load")

            def pos():
                return self.page.evaluate(
                    """(i) => {
                        const el = document.querySelector(
                            `.grid-stack-item[data-widget-index="${i}"]:not([data-rd-derived-opp])`);
                        const n = el && el.gridstackNode;
                        return n ? {y: n.y, h: n.h} : null;
                    }""", idx)

            at_a = pos()
            self.assertEqual(
                at_a and at_a["y"], a_gsy,
                f"reloaded move_in should load at its target slot A: {at_a}")

            # Illegal re-drag onto the occupied obstacle -> must return to A
            # (its move_in slot), NOT to the ghost/real origin U6.
            self.page.evaluate(
                f"() => window.__rdSweep.moveTile('{idx}', {self._front_obstacle_reject_row})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.assertEqual(self.errors, [], f"console errors: {self.errors}")

            after = pos()
            self.assertEqual(
                after and after["y"], a_gsy,
                f"a RELOADED move_in dropped onto an occupied slot must return to "
                f"its move_in slot A (y={a_gsy}), not the ghost/origin: got {after}")

            model_viol = self.page.evaluate("() => window.__rdModel.check()")
            self.assertEqual(
                model_viol, [], f"read-model invariants must be clean: {model_viol}")
        finally:
            try:
                self._api("DELETE",
                          f"/api/plugins/rack-design/designs/{extra_design_id}/")
            except Exception:
                pass


@unittest.skipUnless(_PREREQ_OK, f"editor sweep prerequisites not met: {_PREREQ_REASON}")
class EditorDensePackRejectTestCase(unittest.TestCase):
    """E8 (docs/editor-behavior-spec.md §4.7/§6): drop a real device onto
    units already occupied by ANOTHER live device, on a rack packed so
    densely that every unit is filled -- the isp26 -> U2 regression that used
    to throw ``RangeError: Maximum call stack size exceeded`` deep inside
    GridStack's own collision cascade. Asserts the move is rejected (the
    dragged tile snaps back to its exact original row), that ZERO other
    tiles changed position (a full before/after position snapshot of every
    tile in the rack, not just the mover), zero page errors, and that the
    Phase 1/2 read-model's own invariant check (``window.__rdModel.check()``)
    comes back clean.

    Deterministic and self-provisioning, same style as
    ``EditorSweepTestCase`` above: its own manufacturer/role/site/device
    type/rack/devices/design, created fresh and torn down in
    ``tearDownClass``. No real mouse drag -- the move is driven through the
    same ``window.__rdSweep.moveTile`` shim (fires editor.js's REAL
    dragstart/change/dragstop handlers with an exact target row), reusing
    ``HARNESS_JS_TEMPLATE`` from above.
    """

    # A small rack, COMPLETELY filled front-face with 1U devices -- "densely
    # packed" per the spec, with zero free rows to absorb any push cascade.
    DENSE_RACK_U_HEIGHT = int(os.environ.get("RD_DENSE_RACK_U_HEIGHT", "10"))

    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    @staticmethod
    def _u_to_gsy(rack_u_height, u_position, gs_h):
        """Mirror of editor.js's uPositionToGsY for an ASCENDING-units rack
        (desc_units=false, which is what we provision)."""
        if gs_h > 2:
            return rack_u_height * 2 - u_position * 2 - gs_h + 2
        return rack_u_height * 2 - u_position * 2

    @classmethod
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E Dense Mfr {suffix}", "slug": f"e2e-dense-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E Dense Role {suffix}", "slug": f"e2e-dense-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Dense Site {suffix}", "slug": f"e2e-dense-site-{suffix}",
            "status": "active"})
        dt = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Dense-1U-{suffix}",
            "slug": f"e2e-dense-1u-{suffix}", "u_height": 1, "is_full_depth": False})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Dense Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": cls.DENSE_RACK_U_HEIGHT})

        # See EditorSweepTestCase._provision_primary: this dev instance 400s
        # on a device POST that omits this custom field's default.
        cf_override = {"custom_fields": {"warranty_type": ""}}

        # Fill EVERY unit of the front face with its own 1U device -- zero
        # free rows anywhere in the rack.
        device_ids = []
        for u in range(1, cls.DENSE_RACK_U_HEIGHT + 1):
            dev = cls._api("POST", "/api/dcim/devices/", {
                "name": f"e2e-dense-u{u}-{suffix}", "device_type": dt["id"],
                "role": role["id"], "site": site["id"], "rack": rack["id"],
                "position": f"{u}.0", "face": "front", "status": "active",
                **cf_override})
            device_ids.append(dev["id"])

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"], rack=rack["id"],
            device_types=[dt["id"]], devices=device_ids,
        )
        cls._rack_id = rack["id"]

        # Mover: the device at U1. Target: U5's row -- occupied by another
        # live device, deep inside the packed stack on both sides.
        cls._mover_device_id = device_ids[0]
        cls._mover_orig_gsy = cls._u_to_gsy(cls.DENSE_RACK_U_HEIGHT, 1, 2)
        cls._reject_target_gsy = cls._u_to_gsy(cls.DENSE_RACK_U_HEIGHT, 5, 2)

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"densepack-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
                cls._design_id = None
            created = getattr(cls, "_created", None)
            if created:
                for did in created.get("devices", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/devices/{did}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                if created.get("rack") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{created['rack']}/")
                    except Exception:
                        pass
                if created.get("role") is not None:
                    try:
                        cls._api(
                            "DELETE", f"/api/dcim/device-roles/{created['role']}/")
                    except Exception:
                        pass
                if created.get("manufacturer") is not None:
                    try:
                        cls._api(
                            "DELETE",
                            f"/api/dcim/manufacturers/{created['manufacturer']}/")
                    except Exception:
                        pass
                if created.get("site") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
                    except Exception:
                        pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._created = None
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1600, "height": 1400})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._storage = cls._api_ctx.storage_state()
            cls._csrf = next(
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
            cls._provision_fixture()
            cls.HARNESS_JS = HARNESS_JS_TEMPLATE % {"rack_pk": cls._rack_id}
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    def _load_editor(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)  # let GridStack finish init
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    def widx(self, **match):
        widgets = self.page.evaluate("() => window.__rdSweep.baseWidgets")
        for idx, w in enumerate(widgets):
            if w.get("opposite_face"):
                continue
            if all(w.get(k) == v for k, v in match.items()):
                return idx, w
        self.fail(f"no widget matching {match} in baseWidgets: {widgets}")

    # ------------------------------------------------------------------
    # Full-rack position snapshot: {widget_index: (face, y, h)} for every
    # LIVE tile (excludes derived opposite-face hatches, move-out ghosts and
    # temp ghosts -- none of which exist in this fixture anyway, since it has
    # no full-depth devices and no prior moves, but excluding them keeps this
    # generic/robust against any future change to what a "tile" renders as).
    # ------------------------------------------------------------------
    def _snapshot_all(self):
        return self.page.evaluate("""() => {
            var root = document.getElementById("rd-rack-%s");
            var out = {};
            root.querySelectorAll(".grid-stack-item").forEach(function (el) {
                if (el.getAttribute("data-rd-derived-opp")) { return; }
                if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                if (el.hasAttribute("data-rd-temp-ghost")) { return; }
                var idx = el.getAttribute("data-widget-index");
                var n = el.gridstackNode;
                var face = el.closest("#nbx-rd-grid-front-%s") ? "front"
                    : el.closest("#nbx-rd-grid-rear-%s") ? "rear" : "other";
                out[idx] = [face, n ? n.y : null, n ? n.h : null];
            });
            return out;
        }""" % (self._rack_id, self._rack_id, self._rack_id))

    # =====================================================================
    # E8: drop onto live-occupied units on a fully packed rack -> snap-back,
    # zero other tiles moved, zero console errors, clean read-model.
    # =====================================================================
    def test_drop_onto_occupied_units_on_packed_rack_is_rejected(self):
        self._load_editor()
        mover_idx, mover_w = self.widx(
            kind="existing", face="front", device_id=self._mover_device_id)

        before = self._snapshot_all()
        self.assertEqual(
            len(before), self.DENSE_RACK_U_HEIGHT,
            f"expected {self.DENSE_RACK_U_HEIGHT} live tiles pre-move, got "
            f"{len(before)}: {before}")
        # Issue #22: full-world diff net (same helper the two long sweep
        # classes use) -- a REJECTED drop must leave EVERY entity byte-
        # identical (classes, geometry, title, owner identity), not merely
        # every LIVE tile's position (which `_snapshot_all` above already
        # checked): pass subjectLabel=null so nothing, mover included, is
        # exempt from the comparison.
        world_before = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")

        # Drag the U1 device onto U5's row -- occupied by another live
        # device, on an otherwise completely full rack (Bug: this used to
        # blow GridStack's call stack via the _fixCollisions<->moveNode
        # push cascade trying to find somewhere to shove the occupant).
        self.page.evaluate(
            f"() => window.__rdSweep.moveTile("
            f"'{mover_idx}', {self._reject_target_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        self.assertEqual(
            self.errors, [], f"page/console errors during the rejected drop: "
                              f"{self.errors}")

        after = self._snapshot_all()
        self.assertEqual(
            before, after,
            "a rejected drop on a fully packed rack must leave EVERY tile "
            "(mover included) at its exact prior position -- nothing else "
            "may move as a side effect")

        world_after = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur]) => window.__rdSweep.diffWorlds(prev, cur, null)",
            [world_before, world_after])
        self.assertEqual(
            world_violations, [],
            f"full-world diff must be empty after a rejected drop (nothing "
            f"may differ, subject included): {world_violations}")

        mover_after = after.get(str(mover_idx))
        self.assertIsNotNone(mover_after, "mover tile disappeared from the DOM")
        self.assertEqual(
            mover_after, ["front", self._mover_orig_gsy, 2],
            f"rejected mover must snap back to its exact original slot, "
            f"got {mover_after}")

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(
            model_violations, [],
            f"read-model invariants must be clean after a rejected drop: "
            f"{model_violations}")

        print("\n=== E8 SUMMARY (dense-pack rejected drop) ===")
        print(f"  rack_u_height={self.DENSE_RACK_U_HEIGHT}, tiles={len(before)}")
        print(f"  mover snapped back to y={mover_after[1]}")


@unittest.skipUnless(_PREREQ_OK, f"editor sweep prerequisites not met: {_PREREQ_REASON}")
class EditorHatchOverlapNoPushTestCase(unittest.TestCase):
    """Regression for the Phase 2 refresh-cycle push cascade (spec §4.1 "No
    GridStack push", found live on design 6 rack 526): after a legal move of
    a FULL-DEPTH device by LESS than its own height, the post-drop refresh
    re-derives two opposite-face hatches whose rows OVERLAP -- the device's
    live shadow at its new rows and its origin ghost's mirror at the old
    rows. ``rangeOccupied`` deliberately ignores other hatches (a hatch must
    not block a hatch; the overlap is correct rendering), but GridStack's
    engine still saw the second insert as a collision: ``addWidget ->
    Engine.addNode -> _fixCollisions`` pushed the new hatch past the first
    and cascaded ``moveNode`` relocations across every REAL tile below on
    that face (200+ collateral moves on a dense rack). The fix runs the
    whole refresh-cycle grid-mutation phase under Phase 2's push
    suppression.

    This fixture makes that hatch insertion COLLIDE by construction (the
    prior suites' rear faces had free rows around the mirror slots, so the
    hatches always landed collision-free): a 4U full-depth front device
    whose 1U move leaves overlapping rear hatches immediately ABOVE a solid
    stack of five real 1U rear devices. Asserts after the move: zero rear
    bodies changed position (full snapshot diff), both hatches at their
    exact computed rows, no NEW read-model violations vs the pre-move
    baseline, zero page errors.

    Deterministic and self-provisioning, same style as the suites above;
    the move is driven through ``window.__rdSweep.moveTile``.
    """

    RACK_U = 16

    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    @staticmethod
    def _u_to_gsy(rack_u_height, u_position, gs_h):
        """Mirror of editor.js's uPositionToGsY for an ASCENDING-units rack."""
        if gs_h > 2:
            return rack_u_height * 2 - u_position * 2 - gs_h + 2
        return rack_u_height * 2 - u_position * 2

    @classmethod
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E Hatch Mfr {suffix}", "slug": f"e2e-hatch-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E Hatch Role {suffix}", "slug": f"e2e-hatch-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Hatch Site {suffix}", "slug": f"e2e-hatch-site-{suffix}",
            "status": "active"})
        dt_full = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Hatch-4U-Full-{suffix}",
            "slug": f"e2e-hatch-4u-full-{suffix}", "u_height": 4,
            "is_full_depth": True})
        dt_half = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Hatch-1U-Half-{suffix}",
            "slug": f"e2e-hatch-1u-half-{suffix}", "u_height": 1,
            "is_full_depth": False})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Hatch Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": cls.RACK_U})

        # See EditorSweepTestCase._provision_primary re this custom field.
        cf_override = {"custom_fields": {"warranty_type": ""}}

        device_ids = []
        # Mover: 4U full-depth at front U6-9 (rear mirror rows 14-21).
        dev_full = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-hatch-full-{suffix}", "device_type": dt_full["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "6.0", "face": "front", "status": "active", **cf_override})
        device_ids.append(dev_full["id"])
        # Five real 1U rear devices at U1-5 (rear rows 22-31): a solid stack
        # DIRECTLY below the mirror rows, so any engine push cascade of the
        # overlapping hatches would visibly relocate them.
        for u in range(1, 6):
            dev = cls._api("POST", "/api/dcim/devices/", {
                "name": f"e2e-hatch-rear-u{u}-{suffix}", "device_type": dt_half["id"],
                "role": role["id"], "site": site["id"], "rack": rack["id"],
                "position": f"{u}.0", "face": "rear", "status": "active",
                **cf_override})
            device_ids.append(dev["id"])

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"], rack=rack["id"],
            device_types=[dt_full["id"], dt_half["id"]], devices=device_ids,
        )
        cls._rack_id = rack["id"]
        cls._mover_device_id = dev_full["id"]
        # Origin U6 -> gs-row 14 (rows 14-21); target U7 -> gs-row 12 (rows
        # 12-19): a 1U move, so the origin ghost's mirror rows (14-21) overlap
        # the live shadow's new rows (12-19) on the REAR face at rows 14-19.
        cls._mover_orig_gsy = cls._u_to_gsy(cls.RACK_U, 6, 8)
        cls._mover_target_gsy = cls._u_to_gsy(cls.RACK_U, 7, 8)

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"hatchoverlap-{suffix}", "site": site["id"],
            "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
                cls._design_id = None
            created = getattr(cls, "_created", None)
            if created:
                for did in created.get("devices", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/devices/{did}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                if created.get("rack") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{created['rack']}/")
                    except Exception:
                        pass
                if created.get("role") is not None:
                    try:
                        cls._api(
                            "DELETE", f"/api/dcim/device-roles/{created['role']}/")
                    except Exception:
                        pass
                if created.get("manufacturer") is not None:
                    try:
                        cls._api(
                            "DELETE",
                            f"/api/dcim/manufacturers/{created['manufacturer']}/")
                    except Exception:
                        pass
                if created.get("site") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
                    except Exception:
                        pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._created = None
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1600, "height": 1400})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._storage = cls._api_ctx.storage_state()
            cls._csrf = next(
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
            cls._provision_fixture()
            cls.HARNESS_JS = HARNESS_JS_TEMPLATE % {"rack_pk": cls._rack_id}
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    def _load_editor(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)  # let GridStack finish init
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    def widx(self, **match):
        widgets = self.page.evaluate("() => window.__rdSweep.baseWidgets")
        for idx, w in enumerate(widgets):
            if w.get("opposite_face"):
                continue
            if all(w.get(k) == v for k, v in match.items()):
                return idx, w
        self.fail(f"no widget matching {match} in baseWidgets: {widgets}")

    def _snapshot_all(self):
        """{widget_index: (face, y, h)} for every LIVE tile in the block."""
        return self.page.evaluate("""() => {
            var root = document.getElementById("rd-rack-%s");
            var out = {};
            root.querySelectorAll(".grid-stack-item").forEach(function (el) {
                if (el.getAttribute("data-rd-derived-opp")) { return; }
                if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                if (el.hasAttribute("data-rd-temp-ghost")) { return; }
                var idx = el.getAttribute("data-widget-index");
                var n = el.gridstackNode;
                var face = el.closest("#nbx-rd-grid-front-%s") ? "front"
                    : el.closest("#nbx-rd-grid-rear-%s") ? "rear" : "other";
                out[idx] = [face, n ? n.y : null, n ? n.h : null];
            });
            return out;
        }""" % (self._rack_id, self._rack_id, self._rack_id))

    # =====================================================================
    # Legal 1U move of a full-depth device -> its live shadow hatch and its
    # origin ghost's mirror hatch OVERLAP on the rear face, directly above a
    # solid stack of real rear devices. Nothing but the mover may move.
    # =====================================================================
    def test_overlapping_hatches_never_push_rear_bodies(self):
        self._load_editor()
        mover_idx, mover_w = self.widx(
            kind="existing", face="front", device_id=self._mover_device_id)
        label = mover_w.get("label")

        baseline_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(
            baseline_violations, [],
            f"fresh fixture must have a clean read-model baseline: "
            f"{baseline_violations}")

        before = self._snapshot_all()
        # 1 front full-depth + 5 rear 1U devices.
        self.assertEqual(
            len(before), 6, f"expected 6 live tiles pre-move: {before}")
        # Issue #22: full-world diff net -- this is a LEGAL move (the mover
        # itself, its shadow and its origin ghost are all EXPECTED to
        # change), so the mover's own label is exempt; every bystander
        # (the 5 rear bodies, and their absence of any stray class/title
        # drift) must still be byte-identical.
        world_before = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")

        # A LEGAL move up by 1U (front and rear target rows are free) --
        # accepted, so the refresh derives the two overlapping rear hatches.
        self.page.evaluate(
            f"() => window.__rdSweep.moveTile("
            f"'{mover_idx}', {self._mover_target_gsy})")
        # Two settle waits: the refresh runs on chained zero-delay timers
        # (dragstop -> thaw -> scheduleRefresh -> recompute).
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        self.assertEqual(
            self.errors, [],
            f"page/console errors during the hatch-overlap move: {self.errors}")

        after = self._snapshot_all()
        world_after = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur, lbl]) => window.__rdSweep.diffWorlds(prev, cur, lbl)",
            [world_before, world_after, label])
        self.assertEqual(
            world_violations, [],
            f"full-world diff: no bystander entity may drift after the "
            f"legal hatch-overlapping move (mover exempt): {world_violations}")
        moved = {
            k: (before[k], after.get(k))
            for k in before
            if after.get(k) != before[k]
        }
        self.assertEqual(
            set(moved.keys()), {str(mover_idx)},
            f"only the mover may change position; collateral moves: "
            f"{ {k: v for k, v in moved.items() if k != str(mover_idx)} }")
        self.assertEqual(
            after.get(str(mover_idx)), ["front", self._mover_target_gsy, 8],
            f"mover should sit at its accepted target row "
            f"{self._mover_target_gsy}: {after.get(str(mover_idx))}")

        # Both rear hatches must sit at their exact computed rows: the live
        # shadow at the mover's NEW rows, the ghost mirror at its OLD rows --
        # overlapping, un-pushed.
        hatches = self.page.evaluate(
            f"() => window.__rdSweep.snapshot('{mover_idx}', {json.dumps(label)})")
        live_pos = [{"face": h["face"], "y": h["y"]} for h in hatches["hatchLive"]]
        ghost_pos = [{"face": h["face"], "y": h["y"]} for h in hatches["hatchGhost"]]
        self.assertEqual(
            live_pos, [{"face": "rear", "y": self._mover_target_gsy}],
            f"live shadow hatch must sit at rear row {self._mover_target_gsy}: "
            f"{hatches['hatchLive']}")
        self.assertEqual(
            ghost_pos, [{"face": "rear", "y": self._mover_orig_gsy}],
            f"ghost mirror hatch must sit at rear row {self._mover_orig_gsy}: "
            f"{hatches['hatchGhost']}")

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        new_violations = [
            v for v in model_violations if v not in baseline_violations]
        self.assertEqual(
            new_violations, [],
            f"no NEW read-model violations may appear after the move: "
            f"{new_violations}")

        print("\n=== HATCH-OVERLAP SUMMARY (no-push regression) ===")
        print(f"  mover moved {self._mover_orig_gsy} -> {self._mover_target_gsy}"
              f" (1U up; hatches overlap rear rows "
              f"{self._mover_orig_gsy}-{self._mover_target_gsy + 8 - 1})")
        print(f"  rear stack intact: {len(before) - 1} tiles unmoved")


@unittest.skipUnless(_PREREQ_OK, f"editor sweep prerequisites not met: {_PREREQ_REASON}")
class EditorShadowOwnershipTestCase(unittest.TestCase):
    """Phase 3 (docs/editor-behavior-spec.md §7 "Phase 3", §2.1/§2.2, §3):
    shadows/ghost-mirror hatches are owned by their device/ghost, moved in
    place, live-tracked mid-drag, and state-tinted. Deterministic and self-
    provisioning, same style as ``EditorDensePackRejectTestCase`` above: its
    own manufacturer/role/site/device-types/rack/devices/design, torn down in
    ``tearDownClass``. No real mouse drag -- driven through
    ``window.__rdSweep``'s shims (reused from ``HARNESS_JS_TEMPLATE``).

    Fixture: TWO full-depth devices on one rack, far enough apart that they
    never interact --
      * ``dev_full_a`` (3U, front, U6-8): a clean footprint (empty rear) used
        for the remove-state and live mid-drag tests.
      * ``dev_full_b`` (3U, front, U14-16) + ``dev_conflict`` (1U, REAR, U15,
        created directly via the DCIM API so it exists BEFORE the editor ever
        loads): a pre-existing double-booked opposite face used for the
        conflict-shadow test (bug 4c) -- Phase 2's ``rdCanPlaceAt`` already
        blocks any NEW placement like this, so the only way to reach the
        scenario is a server-loaded layout that already violates it.
    """

    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    @staticmethod
    def _u_to_gsy(rack_u_height, u_position, gs_h):
        """Mirror of editor.js's uPositionToGsY for an ASCENDING-units rack."""
        if gs_h > 2:
            return rack_u_height * 2 - u_position * 2 - gs_h + 2
        return rack_u_height * 2 - u_position * 2

    @classmethod
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E Shadow Mfr {suffix}", "slug": f"e2e-shadow-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E Shadow Role {suffix}", "slug": f"e2e-shadow-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Shadow Site {suffix}", "slug": f"e2e-shadow-site-{suffix}",
            "status": "active"})
        dt_full3 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Shadow-3U-Full-{suffix}",
            "slug": f"e2e-shadow-3u-full-{suffix}", "u_height": 3, "is_full_depth": True})
        # dev_full_b's device type starts NOT full-depth so NetBox's own
        # occupancy validation allows placing dev_conflict on its mirrored
        # rear rows below -- then we flip the flag to full-depth via a type
        # PATCH (a metadata edit that does not re-validate existing device
        # placements), retroactively creating the double-booking. This
        # mirrors how the REAL bug (design 6 rack 526, dra4-sl-isp28) arose:
        # a pre-existing layout drifting out of sync with a device type's
        # is_full_depth flag, not something reachable via the editor's own
        # drag/drop/palette paths (Phase 2's rdCanPlaceAt already blocks any
        # NEW placement like this).
        dt_full3b = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Shadow-3U-FullB-{suffix}",
            "slug": f"e2e-shadow-3u-fullb-{suffix}", "u_height": 3, "is_full_depth": False})
        dt_half1 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Shadow-1U-Half-{suffix}",
            "slug": f"e2e-shadow-1u-half-{suffix}", "u_height": 1, "is_full_depth": False})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Shadow Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": RACK_U_HEIGHT})

        cf_override = {"custom_fields": {"warranty_type": ""}}

        dev_full_a = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-shadow-clean-{suffix}", "device_type": dt_full3["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "6.0", "face": "front", "status": "active", **cf_override})
        dev_full_b = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-shadow-conflict-owner-{suffix}", "device_type": dt_full3b["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "14.0", "face": "front", "status": "active", **cf_override})
        # Pre-existing double-booking (bug 4c): a real body already sitting on
        # dev_full_b's future mirrored (rear) rows -- placeable now because
        # dt_full3b is not YET flagged full-depth.
        dev_conflict = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-shadow-conflict-{suffix}", "device_type": dt_half1["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "15.0", "face": "rear", "status": "active", **cf_override})
        # Flip the flag retroactively -- both devices are now double-booked
        # in a server-loaded layout the editor never had a chance to reject.
        cls._api("PATCH", f"/api/dcim/device-types/{dt_full3b['id']}/",
                  {"is_full_depth": True})

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"], rack=rack["id"],
            device_types=[dt_full3["id"], dt_full3b["id"], dt_half1["id"]],
            devices=[dev_full_a["id"], dev_full_b["id"], dev_conflict["id"]],
        )
        cls._rack_id = rack["id"]
        cls._rack_u_height = RACK_U_HEIGHT
        cls._clean_device_id = dev_full_a["id"]
        cls._clean_label = dev_full_a["name"]
        cls._conflict_owner_device_id = dev_full_b["id"]
        cls._conflict_owner_label = dev_full_b["name"]
        cls._conflict_device_label = dev_conflict["name"]
        cls._clean_orig_gsy = cls._u_to_gsy(RACK_U_HEIGHT, 6, 6)

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"shadow-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
                cls._design_id = None
            created = getattr(cls, "_created", None)
            if created:
                for did in created.get("devices", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/devices/{did}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                if created.get("rack") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{created['rack']}/")
                    except Exception:
                        pass
                if created.get("role") is not None:
                    try:
                        cls._api(
                            "DELETE", f"/api/dcim/device-roles/{created['role']}/")
                    except Exception:
                        pass
                if created.get("manufacturer") is not None:
                    try:
                        cls._api(
                            "DELETE",
                            f"/api/dcim/manufacturers/{created['manufacturer']}/")
                    except Exception:
                        pass
                if created.get("site") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
                    except Exception:
                        pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._created = None
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1600, "height": 1400})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._storage = cls._api_ctx.storage_state()
            cls._csrf = next(
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
            cls._provision_fixture()
            cls.HARNESS_JS = HARNESS_JS_TEMPLATE % {"rack_pk": cls._rack_id}
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    def _load_editor(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)  # let GridStack finish init
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    def widx(self, **match):
        widgets = self.page.evaluate("() => window.__rdSweep.baseWidgets")
        for idx, w in enumerate(widgets):
            if w.get("opposite_face"):
                continue
            if all(w.get(k) == v for k, v in match.items()):
                return idx, w
        self.fail(f"no widget matching {match} in baseWidgets: {widgets}")

    # =====================================================================
    # Live mid-drag shadow tracking (spec §2.2): between dragstart+change
    # (candidate position written, gesture still open) and dragstop (drop
    # settles), the dragged full-depth device's OWN shadow must ALREADY sit
    # at the candidate mirrored rows -- not lag until the drop/settle pass.
    # =====================================================================
    def test_mid_drag_shadow_tracks_candidate_position(self):
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._clean_device_id)

        candidate_gsy = self._clean_orig_gsy - 2  # 1U up within the rack
        self.assertGreaterEqual(candidate_gsy, 0, "fixture has no headroom above")

        # Issue #22: full-world diff net -- this is a legal move of
        # `_clean_label`, exempt below; every OTHER entity (the pre-existing
        # conflict fixture included) must be byte-identical after the drag.
        world_before = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")

        # Fire dragBeginAndMove (dragstart -> position write -> change) AND
        # read back the shadow + owner-tile state in the SAME synchronous JS
        # turn -- a separate round-trip would let editor.js's debounced
        # scheduleRefresh (setTimeout 0) run in between and settle the
        # gesture early, defeating the "BETWEEN change and dragstop" window
        # this test means to inspect.
        mid = self.page.evaluate(f"""() => {{
            var began = window.__rdSweep.dragBeginAndMove('{idx}', {candidate_gsy});
            var hatches = window.__rdSweep.hatchesFor({json.dumps(self._clean_label)});
            var tile = window.__rdSweep.tileInfo('{idx}');
            return {{began: began, hatches: hatches, tileClasses: tile ? tile.classes : []}};
        }}""")
        self.assertTrue(mid["began"], "dragBeginAndMove could not find the tile")

        # BETWEEN dragstart+change and dragstop: the shadow must already be at
        # the candidate mirrored rows, on the rear face, same y as the body,
        # carrying whichever "nbx-rd-state-*" class the OWNER tile itself
        # currently has (existing at its origin U, move_in once dragged away).
        live_mid = [h for h in mid["hatches"] if "nbx-rd-opposite-ghost" not in h["classes"]]
        self.assertEqual(
            len(live_mid), 1,
            f"expected exactly one live shadow mid-drag, got {mid['hatches']}")
        self.assertEqual(
            live_mid[0]["face"], "rear",
            f"mid-drag shadow should be on the rear face: {live_mid[0]}")
        self.assertEqual(
            live_mid[0]["y"], candidate_gsy,
            f"mid-drag shadow should already track the candidate row "
            f"{candidate_gsy} BEFORE drop: {live_mid[0]}")
        owner_state = next(
            (c for c in mid["tileClasses"] if c.startswith("nbx-rd-state-")), None)
        self.assertIsNotNone(owner_state, f"owner tile has no state class: {mid['tileClasses']}")
        self.assertIn(
            owner_state, live_mid[0]["classes"],
            f"mid-drag shadow should keep the owner's state class "
            f"{owner_state!r}: {live_mid[0]}")

        ended = self.page.evaluate("() => window.__rdSweep.dragEnd()")
        self.assertTrue(ended, "dragEnd could not find the in-flight tile")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        # After settle: the shadow is still exactly there (atomic commit, spec
        # §4.1), and the read-model agrees.
        final_hatches = self.page.evaluate(
            f"() => window.__rdSweep.hatchesFor({json.dumps(self._clean_label)})")
        live_final = [h for h in final_hatches if "nbx-rd-opposite-ghost" not in h["classes"]]
        self.assertEqual(len(live_final), 1)
        self.assertEqual(live_final[0]["face"], "rear")
        self.assertEqual(live_final[0]["y"], candidate_gsy)
        self.assertEqual(self.errors, [], f"console errors during drag: {self.errors}")
        # This fixture's rack ALSO carries the class's own permanent, pre-
        # existing conflict fixture (dev_full_b/dev_conflict) elsewhere on
        # the same rack -- expected and covered by
        # test_conflict_shadow_rendered_and_reported. Only assert no NEW
        # violation appeared for the device THIS test actually dragged.
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        own_violations = [v for v in model_violations if self._clean_label in v]
        self.assertEqual(
            own_violations, [],
            f"read-model must be clean for the dragged device: {model_violations}")

        world_after = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur, lbl]) => window.__rdSweep.diffWorlds(prev, cur, lbl)",
            [world_before, world_after, self._clean_label])
        self.assertEqual(
            world_violations, [],
            f"full-world diff: no bystander entity (incl. the pre-existing "
            f"conflict fixture) may drift after this legal drag: {world_violations}")

    # =====================================================================
    # Remove-state shadow (spec §7 Phase 3 bug 4a): flagging a full-depth
    # EXISTING device for removal must render its opposite-face shadow as a
    # crossed-out, remove-tinted hatch -- not silently leave it (the old
    # recomputeOpposites skipped st.removed devices entirely, which the
    # read-model's I2 check reported as "has no shadow").
    # =====================================================================
    def test_remove_state_shadow_is_crossed_out(self):
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._clean_device_id)

        # Issue #22: full-world diff net -- flagging `_clean_label` for
        # removal is the legal change under test (exempt below); nothing
        # else in the rack (incl. the permanent conflict fixture) may drift.
        world_before = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")

        clicked = self.page.evaluate(f"() => window.__rdSweep.clickRemove('{idx}')")
        self.assertTrue(clicked, "clickRemove could not find the × button")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        tile_classes = self.page.evaluate(
            f"() => window.__rdSweep.tileInfo('{idx}').classes")
        self.assertIn("nbx-rd-state-remove", tile_classes)

        hatches = self.page.evaluate(
            f"() => window.__rdSweep.hatchesFor({json.dumps(self._clean_label)})")
        live = [h for h in hatches if "nbx-rd-opposite-ghost" not in h["classes"]]
        self.assertEqual(
            len(live), 1,
            f"remove-flagged full-depth device must still have exactly one "
            f"owned shadow, got {hatches}")
        self.assertIn(
            "nbx-rd-state-remove", live[0]["classes"],
            f"remove-flagged device's shadow must be remove-tinted: {live[0]}")
        self.assertIn(
            "nbx-rd-opposite-remove", live[0]["classes"],
            f"remove-flagged device's shadow must carry the remove-tint "
            f"CSS hook: {live[0]}")
        self.assertIn(
            "nbx-rd-opposite-crossed", live[0]["classes"],
            f"remove-flagged device's shadow must be crossed-out per spec §3: "
            f"{live[0]}")

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        i2_for_device = [
            v for v in model_violations
            if self._clean_label in v and "no shadow" in v]
        self.assertEqual(
            i2_for_device, [],
            f"a remove-flagged full-depth device must NOT be reported as "
            f"having no shadow: {model_violations}")

        world_after = self.page.evaluate("() => window.__rdSweep.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur, lbl]) => window.__rdSweep.diffWorlds(prev, cur, lbl)",
            [world_before, world_after, self._clean_label])
        self.assertEqual(
            world_violations, [],
            f"full-world diff: no bystander entity may drift after flagging "
            f"a device for removal: {world_violations}")

    # =====================================================================
    # Regression (user bug 2026-07-15): a device flagged for removal must
    # FREE its slot in the plan -- another device can then be moved onto it.
    # The bug: flagRemove() added `nbx-rd-state-remove` on top of the base
    # `nbx-rd-state-existing` class, and the read-model's first-match state
    # derivation returned `existing`, so the removed device still read as a
    # LIVE body and rdCanPlaceAt kept BLOCKING its own being-vacated slot (the
    # dropped tile snapped back). Worst for full-depth gear, whose OPPOSITE
    # face is validated too. Fix = state precedence (remove wins over existing)
    # in rdStateFromClassList. See docs/editor-known-issues.md.
    # =====================================================================
    def test_removed_fulldepth_frees_slot_for_placement(self):
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._clean_device_id)

        clicked = self.page.evaluate(f"() => window.__rdSweep.clickRemove('{idx}')")
        self.assertTrue(clicked, "clickRemove could not find the × button")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        result = self.page.evaluate(
            """(label) => {
                const m = window.__rdModel.build();
                const mine = m.devices.filter(d => d.label === label);
                const states = mine.map(d => ({face: d.face, state: d.state}));
                // The removed full-depth device occupies rows on BOTH faces;
                // pick its front rows and ask whether a full-depth tile could
                // now land there. Pass a detached element as the mover so it
                // never matches (i.e. never self-excludes) the removed device.
                const front = mine.find(d => d.face === 'front') || mine[0];
                const probe = document.createElement('div');
                let verdict = null;
                if (front && front.y != null) {
                    const v = window.__rdModel.canPlaceAt(
                        probe, front.rackId, 'front', front.y, front.rows, true);
                    verdict = { ok: v.ok, blockers: (v.blockers||[]).map(
                        b => ({label: b.device && b.device.label, state: b.device && b.device.state})) };
                }
                return { states, verdict, violations: window.__rdModel.check() };
            }""",
            self._clean_label)

        # 1) The removed device reads as `remove` on EVERY face copy (never a
        #    lingering live `existing` -- the core of the bug).
        self.assertTrue(result["states"], "removed device vanished from read-model")
        for s in result["states"]:
            self.assertEqual(
                s["state"], "remove",
                f"remove-flagged device face {s['face']} must read 'remove', "
                f"not '{s['state']}': {result['states']}")

        # 2) A full-depth tile can now be placed on the freed rows -- no blocker
        #    from the being-removed device on either face.
        self.assertIsNotNone(result["verdict"], "could not resolve freed rows")
        self.assertTrue(
            result["verdict"]["ok"],
            f"a full-depth tile must be placeable on the slot freed by a "
            f"removal, but canPlaceAt rejected it: {result['verdict']}")
        # This world has a PERMANENT conflict fixture (a deliberate I1), so the
        # invariant check is never globally empty -- just assert the removal did
        # not create a NEW violation naming the removed device (e.g. a spurious
        # duplicate-live-body I4).
        offending = [v for v in result["violations"] if self._clean_label in v]
        self.assertEqual(
            offending, [],
            f"flagging {self._clean_label!r} for removal must not create a "
            f"violation naming it: {offending}")

    # =====================================================================
    # Regression (user bug 2026-07-15, task #31): after a full-depth device is
    # MOVED, its client-created opposite-face hatch must (a) be UNIQUE -- no
    # duplicate/orphan hatch (the I1 "shadow overlaps shadow" the user hit) --
    # and (b) carry the OWNER's identity + power data so it renders like the
    # SERVER hatch: shows the device type on the normal view and FILLS on the
    # heatmap. The bug: makeOppositeElement only set the name span, so the hatch
    # was blank (no data-device-type-name / data-draw-w -> heatmap couldn't fill
    # or label it), and a face-changing move could leave a second orphan hatch.
    # See docs/editor-known-issues.md.
    # =====================================================================
    def test_moved_fulldepth_shadow_is_unique_and_carries_identity(self):
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._clean_device_id)

        # Move the 3U full-depth device from U6 to the free U10 slot (front),
        # clear of the conflict fixture at U14-16.
        target_gsy = self._u_to_gsy(self._rack_u_height, 10, 6)
        moved = self.page.evaluate(
            f"() => window.__rdSweep.moveTile('{idx}', {target_gsy})")
        self.assertTrue(moved, "moveTile could not move the full-depth device")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        result = self.page.evaluate(
            """(idx) => {
                const block = document.querySelector('.nbx-rd-rack-block');
                const owner = [...block.querySelectorAll('.grid-stack-item')].find(t =>
                    String(t.getAttribute('data-widget-index')) === String(idx)
                    && !t.getAttribute('data-rd-derived-opp'));
                const oc = owner && owner.querySelector('.grid-stack-item-content');
                // The device's LIVE opposite shadow (its move_in/existing body's
                // hatch). A full-depth MOVE also leaves the move-out GHOST's own
                // mirror hatch at the old rows (nbx-rd-state-move_out_ghost,
                // owned as ghostShadows[idx]) -- that is a SEPARATE, expected
                // element, so exclude it here; we assert on the body's shadow.
                const hatches = [...block.querySelectorAll(
                    '.grid-stack-item[data-rd-derived-opp]')].filter(h =>
                        String(h.getAttribute('data-rd-owner-widx')) === String(idx)
                        && !h.classList.contains('nbx-rd-state-move_out_ghost'));
                return {
                    ownerState: owner ? [...owner.classList].filter(
                        c => c.startsWith('nbx-rd-state')) : null,
                    ownerDtName: oc && oc.getAttribute('data-device-type-name'),
                    ownerDrawW: oc && oc.getAttribute('data-draw-w'),
                    hatchCount: hatches.length,
                    ownerY: owner && owner.gridstackNode && owner.gridstackNode.y,
                    allDerivedOpp: [...block.querySelectorAll(
                        '.grid-stack-item[data-rd-derived-opp]')].map(h => ({
                            ownerWidx: h.getAttribute('data-rd-owner-widx'),
                            y: h.gridstackNode && h.gridstackNode.y,
                            inEngine: !!(h.gridstackNode && h.gridstackNode.grid),
                            cls: [...h.classList].filter(c => c.startsWith('nbx-rd-state')
                                || c === 'nbx-rd-opposite').join(' '),
                        })),
                    hatches: hatches.map(h => {
                        const c = h.querySelector('.grid-stack-item-content');
                        return {
                            dtName: c && c.getAttribute('data-device-type-name'),
                            drawW: c && c.getAttribute('data-draw-w'),
                            y: h.gridstackNode && h.gridstackNode.y,
                            inEngine: !!(h.gridstackNode && h.gridstackNode.grid),
                            face: h.closest('[data-rd-face]') &&
                                h.closest('[data-rd-face]').getAttribute('data-rd-face'),
                        };
                    }),
                    violations: window.__rdModel.check(),
                };
            }""", idx)

        self.assertIn(
            "nbx-rd-state-move_in", result["ownerState"] or [],
            f"the device should be a move_in after the move: {result['ownerState']}")
        # (a) exactly ONE opposite hatch for this owner -- no duplicate/orphan.
        self.assertEqual(
            result["hatchCount"], 1,
            f"a moved full-depth device must have exactly ONE opposite hatch, "
            f"got {result['hatchCount']}: hatches={result['hatches']} "
            f"ownerY={result['ownerY']} allDerivedOpp={result['allDerivedOpp']}")
        # (b) the hatch mirrors the owner's identity + draw (renders the type,
        #     fills on the heatmap) -- was blank before the fix.
        self.assertTrue(
            result["ownerDtName"], "owner tile should carry a device-type-name")
        h = result["hatches"][0]
        self.assertEqual(
            h["dtName"], result["ownerDtName"],
            f"opposite hatch must carry the owner's device type: {h}")
        self.assertEqual(
            h["drawW"], result["ownerDrawW"],
            f"opposite hatch must carry the owner's draw: {h}")
        # No shadow-overlap (I1) violation naming the moved device.
        offending = [v for v in result["violations"]
                     if self._clean_label in v and "shadow" in v.lower()]
        self.assertEqual(
            offending, [],
            f"no shadow-overlap (I1) violation may name the moved device: {offending}")

    # =====================================================================
    # Conflict shadow (spec §7 Phase 3 bug 4c): a full-depth device's
    # mirrored rows are ALREADY occupied by a real opposite-face body in a
    # server-loaded layout (unreachable via the editor's own drag/drop/
    # palette paths, which Phase 2's rdCanPlaceAt already blocks) -- the
    # shadow must still be VISIBLY rendered, conflict-styled, overlapping;
    # the read-model must report it as an I1 overlap naming BOTH devices,
    # never silently as "has no shadow".
    # =====================================================================
    def test_conflict_shadow_rendered_and_reported(self):
        self._load_editor()
        idx, w = self.widx(
            kind="existing", face="front", device_id=self._conflict_owner_device_id)

        hatches = self.page.evaluate(
            f"() => window.__rdSweep.hatchesFor({json.dumps(self._conflict_owner_label)})")
        live = [h for h in hatches if "nbx-rd-opposite-ghost" not in h["classes"]]
        self.assertEqual(
            len(live), 1,
            f"the double-booked device must still have its own shadow "
            f"rendered (visible, not skipped), got {hatches}")
        self.assertEqual(live[0]["face"], "rear")
        self.assertIn(
            "nbx-rd-opposite-conflict", live[0]["classes"],
            f"a shadow whose mirrored rows are occupied by a real body must "
            f"be conflict-styled: {live[0]}")

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        conflict_violations = [
            v for v in model_violations
            if self._conflict_owner_label in v and self._conflict_device_label in v]
        self.assertTrue(
            conflict_violations,
            f"expected an I1-style violation naming BOTH "
            f"{self._conflict_owner_label!r} and {self._conflict_device_label!r}, "
            f"got: {model_violations}")
        no_shadow_violations = [
            v for v in model_violations
            if self._conflict_owner_label in v and "no shadow" in v]
        self.assertEqual(
            no_shadow_violations, [],
            f"the double-booked device must be reported as a conflict, "
            f"never as 'has no shadow': {model_violations}")

        print("\n=== SHADOW-OWNERSHIP SUMMARY ===")
        print(f"  conflict violation(s): {conflict_violations}")


DISPLACE_HARNESS_JS_TEMPLATE = r"""
window.__rdDisplace = (function () {
    var RACK_PK = "%(rack_pk)s";
    var root = document.getElementById("rd-rack-" + RACK_PK);

    var baseWidgets = JSON.parse(
        (document.getElementById("rd-editor-data-" + RACK_PK) || {}).textContent || "[]");

    function frontGrid() { return document.getElementById("nbx-rd-grid-front-" + RACK_PK).gridstack; }
    function rearGrid() { return document.getElementById("nbx-rd-grid-rear-" + RACK_PK).gridstack; }
    function gridFor(face) { return face === "front" ? frontGrid() : rearGrid(); }
    function hostFor(face) { return document.getElementById("nbx-rd-grid-" + face + "-" + RACK_PK); }

    function tileEl(idx) {
        return root.querySelector('.grid-stack-item[data-widget-index="' + idx + '"]');
    }

    function fireHandler(grid, name, arg) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler[name];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: name }, arg); });
    }
    function fireDropped(grid, newNode) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler["dropped"];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: "dropped" }, null, newNode); });
    }

    // Same collision-engine-bypassing position write as HARNESS_JS_TEMPLATE
    // above (see there for the full rationale).
    function fastSetY(grid, el, newGsY) {
        var node = el.gridstackNode;
        node.x = 0;
        node.y = newGsY;
        node._orig = { x: node.x, y: node.y };
        grid._writePosAttr(el, node);
    }

    function moveTile(idx, newGsY) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var grid = el.gridstackNode.grid;
        fireHandler(grid, "dragstart", el);
        fastSetY(grid, el, newGsY);
        fireHandler(grid, "change", []);
        fireHandler(grid, "dragstop", el);
        return true;
    }

    // Palette-style drop at an EXACT target row (dt_add_sweep's
    // dropPaletteItem always lands at row 0; this variant repositions to
    // `gsY` -- mirroring moveTileToFace's "adopt at a known-free row first,
    // then reposition" two-step -- BEFORE firing `dropped`, so an add can
    // land exactly on a vacating slot).
    function dropPaletteItemAt(dtId, uHeight, label, isFullDepth, face, gsY) {
        var grid = gridFor(face);
        var clone = document.createElement("div");
        clone.className = "grid-stack-item nbx-rd-palette-item";
        clone.setAttribute("data-device-type-id", String(dtId));
        clone.setAttribute("data-u-height", String(uHeight));
        clone.setAttribute("data-label", label);
        clone.setAttribute("data-is-full-depth", isFullDepth ? "true" : "false");
        var content = document.createElement("div");
        content.className = "grid-stack-item-content";
        clone.appendChild(content);

        var gsH = Math.max(1, Math.round(uHeight * 2));
        var added = grid.addWidget(clone, { x: 0, y: 0, w: 1, h: gsH });
        var el = added || clone;
        fastSetY(grid, el, gsY);
        var newNode = el.gridstackNode || { el: el };
        fireDropped(grid, newNode);
        return el.getAttribute("data-widget-index");
    }

    function faceOf(el) {
        if (el.closest("#nbx-rd-grid-front-" + RACK_PK)) { return "front"; }
        if (el.closest("#nbx-rd-grid-rear-" + RACK_PK)) { return "rear"; }
        return "other";
    }

    function tileInfo(idx) {
        var el = tileEl(idx);
        if (!el) { return null; }
        var n = el.gridstackNode;
        return {
            classes: Array.prototype.slice.call(el.classList),
            y: n ? n.y : null, h: n ? n.h : null, face: faceOf(el),
        };
    }

    // Click the ×/remove button on tile `idx` (cancels a move, flags a
    // removal, or cancels an add depending on the tile's state).
    function clickRemove(idx) {
        var el = tileEl(idx);
        if (!el) { return false; }
        var btn = el.querySelector(".nbx-rd-remove-btn");
        if (!btn) { return false; }
        btn.click();
        return true;
    }

    // The visible name state of tile `idx`: its stable identity label, the
    // mutable display-name overlay (null when none), and whether the identity
    // is hidden behind the overlay.
    function tileNames(idx) {
        var el = tileEl(idx);
        if (!el) { return null; }
        var identity = el.querySelector(".nbx-rd-label");
        var display = el.querySelector(".nbx-rd-name-display");
        return {
            identity: identity ? identity.textContent : null,
            identityHidden: identity ? identity.classList.contains("nbx-rd-label-hidden") : null,
            display: display ? display.textContent : null,
        };
    }

    // Every move_out_ghost body tile (temp or persistent) matching `label`,
    // WITH its stripe state -- the assertion surface for the displacement
    // stripe (spec §3, §4.3): whether it is collapsed (`nbx-rd-displaced`)
    // and the stripe child's tooltip.
    function ghostInfo(label) {
        var out = [];
        root.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (el) {
            if (el.getAttribute("data-rd-derived-opp")) { return; }
            var span = el.querySelector(".nbx-rd-label");
            if (!span || span.textContent !== label) { return; }
            // The stripe bar lives OUTSIDE the tile (spec §3, ruling
            // 2026-07-09), associated by owner label + face data attributes.
            var stripe = root.querySelector(
                '.nbx-rd-stripe[data-rd-stripe-for="' + label + '"]'
                + '[data-rd-stripe-face="' + faceOf(el) + '"]');
            out.push({
                face: faceOf(el),
                classes: Array.prototype.slice.call(el.classList),
                displaced: el.classList.contains("nbx-rd-displaced"),
                stripeTitle: stripe ? stripe.getAttribute("title") : null,
            });
        });
        return out;
    }

    // The full-depth ghost's owned mirror hatch (data-rd-owner-widx) on the
    // opposite face -- separate from ghostInfo because a mirror hatch never
    // carries a `.nbx-rd-label` matching the device's label directly on
    // itself the same way (it is looked up by owner identity instead).
    function ghostMirrorInfo(ownerWidx) {
        // NOTE: `data-rd-owner-widx` is NOT unique to the ghost's mirror -- a
        // full-depth device's OWN live shadow (tracking its current position)
        // carries the SAME owner widget-index (they share one widget-index
        // across the device's whole lifecycle in this rack). Scope to the
        // ghost-mirror-specific CSS hook (`nbx-rd-opposite-ghost`) so this
        // never accidentally matches the device's own current shadow.
        var el = root.querySelector(
            '[data-rd-derived-opp][data-rd-owner-widx="' + ownerWidx
            + '"].nbx-rd-opposite-ghost');
        if (!el) { return null; }
        // The mirror's stripe bar lives OUTSIDE the tile too, associated by
        // owner widget-index + this (opposite) face.
        var stripe = root.querySelector(
            '.nbx-rd-stripe[data-rd-stripe-owner-widx="' + ownerWidx + '"]'
            + '[data-rd-stripe-face="' + faceOf(el) + '"]');
        return {
            face: faceOf(el),
            classes: Array.prototype.slice.call(el.classList),
            displaced: el.classList.contains("nbx-rd-displaced"),
            stripeTitle: stripe ? stripe.getAttribute("title") : null,
        };
    }

    // A brand-new add's widget-index is only assigned once its displace
    // dialog (if any) is confirmed -- `dropPaletteItemAt`'s own return value
    // is void until then. Look the tile up afterward by its distinguishing
    // `.nbx-rd-label` text instead (only a live, non-derived-opp tile).
    function findByLabel(label) {
        var hit = null;
        root.querySelectorAll(".grid-stack-item").forEach(function (el) {
            if (hit) { return; }
            if (el.getAttribute("data-rd-derived-opp")) { return; }
            if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
            var span = el.querySelector(".nbx-rd-label");
            if (span && span.textContent === label) { hit = el.getAttribute("data-widget-index"); }
        });
        return hit;
    }

    function hasDisplaceDialog() {
        return !!document.querySelector(".nbx-rd-displace-modal");
    }
    function confirmDisplaceDialog() {
        var btn = document.querySelector(".nbx-rd-displace-modal [data-rd-displace-confirm]");
        if (!btn) { return false; }
        btn.click();
        return true;
    }
    function cancelDisplaceDialog() {
        var btn = document.querySelector(".nbx-rd-displace-modal .btn-link[data-bs-dismiss]");
        if (!btn) { return false; }
        btn.click();
        return true;
    }
    // Dismiss any OTHER modal left open so it never accumulates across
    // steps within one test. NOTE (2026-07-08): dismissing the §4a rename
    // dialog ABORTS the move (spec §4a) -- it only ever LOOKED harmless
    // before because a Bootstrap transition bug swallowed the dismissal
    // entirely (the dialog stayed open, the cancel never ran). A move that
    // must SURVIVE must use applyRenameDialogs below instead.
    function dismissAllOtherModals() {
        document.querySelectorAll(".modal.show, .modal.fade").forEach(function (el) {
            if (el.classList.contains("nbx-rd-displace-modal")) { return; }
            var btn = el.querySelector("[data-bs-dismiss='modal']");
            if (btn) { btn.click(); }
        });
    }

    // APPLY every open §4a rename dialog with its default (keep-name)
    // choice: confirms the move (a dismissal would abort it).
    function applyRenameDialogs() {
        var n = 0;
        document.querySelectorAll(".nbx-rd-move-modal [data-rd-move-apply]").forEach(function (btn) {
            btn.click();
            n++;
        });
        return n;
    }

    // APPLY the open §4a rename dialog choosing "Set a new name" = `name`
    // (the user-typed rename path, vs applyRenameDialogs' keep-name default).
    function applyRenameDialogsWithName(name) {
        var n = 0;
        document.querySelectorAll(".nbx-rd-move-modal").forEach(function (modal) {
            var newRadio = modal.querySelector("#nbx-rd-move-new");
            var input = modal.querySelector(".nbx-rd-move-new-input");
            var apply = modal.querySelector("[data-rd-move-apply]");
            if (!newRadio || !input || !apply) { return; }
            newRadio.checked = true;
            newRadio.dispatchEvent(new Event("change", {bubbles: true}));
            input.value = name;
            apply.click();
            n++;
        });
        return n;
    }

    // Geometry of every displacement stripe naming `label` (spec §3, user
    // ruling 2026-07-09: the stripe is a bar OUTSIDE the rack frame): each
    // bar's bounding box plus the face grid's, so a test can assert the bar
    // hangs off the grid's RIGHT edge and vertically spans the displaced
    // rows. Works against BOTH geometries (pre-fix in-tile child, post-fix
    // wrap-level bar) so the same probe captures the pre-fix failure.
    function stripeGeometry(label) {
        var out = [];
        root.querySelectorAll(".nbx-rd-stripe").forEach(function (bar) {
            var title = bar.getAttribute("title") || "";
            if (title.indexOf(label) === -1) { return; }
            var wrap = bar.closest(".nbx-rd-grid-wrap");
            var grid = wrap ? wrap.querySelector(".grid-stack") : bar.closest(".grid-stack");
            var r = bar.getBoundingClientRect();
            var g = grid ? grid.getBoundingClientRect() : null;
            out.push({
                title: title,
                face: bar.getAttribute("data-rd-stripe-face")
                    || (grid ? (grid.getAttribute("data-face") || null) : null),
                bar: { left: r.left, right: r.right, top: r.top, bottom: r.bottom, height: r.height },
                grid: g ? { left: g.left, right: g.right, top: g.top, bottom: g.bottom, height: g.height } : null,
            });
        });
        return out;
    }

    // Bounding boxes of every move-out ghost BODY tile matching `label`
    // (per face) -- the vertical span a face's stripe bar must align to.
    function ghostBoxes(label) {
        var out = [];
        root.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (el) {
            if (el.getAttribute("data-rd-derived-opp")) { return; }
            var span = el.querySelector(".nbx-rd-label");
            if (!span || span.textContent !== label) { return; }
            var host = el.closest(".grid-stack");
            var r = el.getBoundingClientRect();
            out.push({
                face: host ? (host.getAttribute("data-face") || null) : null,
                top: r.top, bottom: r.bottom, height: r.height,
                left: r.left, right: r.right,
            });
        });
        return out;
    }

    return {
        baseWidgets: baseWidgets,
        moveTile: moveTile,
        dropPaletteItemAt: dropPaletteItemAt,
        tileInfo: tileInfo,
        ghostInfo: ghostInfo,
        ghostMirrorInfo: ghostMirrorInfo,
        findByLabel: findByLabel,
        hasDisplaceDialog: hasDisplaceDialog,
        confirmDisplaceDialog: confirmDisplaceDialog,
        cancelDisplaceDialog: cancelDisplaceDialog,
        dismissAllOtherModals: dismissAllOtherModals,
        applyRenameDialogs: applyRenameDialogs,
        applyRenameDialogsWithName: applyRenameDialogsWithName,
        clickRemove: clickRemove,
        tileNames: tileNames,
        stripeGeometry: stripeGeometry,
        ghostBoxes: ghostBoxes,
    };
})();
"""


@unittest.skipUnless(_PREREQ_OK, f"editor sweep prerequisites not met: {_PREREQ_REASON}")
class EditorDisplacementTestCase(unittest.TestCase):
    """Phase 4 (docs/editor-behavior-spec.md §7 "Phase 4", §4.3, §8): dropping
    a device onto a vacating slot (a move-out ghost) triggers a confirmation
    dialog (always AFTER validation already passed), and on confirm the
    displaced device's rendering collapses to a side reservation stripe
    (restored the moment the new occupant moves away/is cancelled). Moving a
    device back onto its OWN ghost is a silent revert (spec §4.4/§8.3).

    Deterministic and self-provisioning, same style as
    ``EditorShadowOwnershipTestCase``: its own manufacturer/role/site/rack/
    device-types/devices/design, torn down in ``tearDownClass``. No real
    mouse drag -- driven through ``window.__rdDisplace``'s shims.

    Fixture: TWO full-depth (2U) devices on one rack's front face, far
    enough apart to never interact directly -- ``dev_a`` (U6-7, the one
    whose ghost gets displaced) and ``dev_b`` (U14-15, the mover). A 1U
    half-depth device TYPE is also registered for the palette-add scenario
    (E11) -- it is never itself a Device, only dragged in as a fresh add.
    """

    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    @staticmethod
    def _u_to_gsy(rack_u_height, u_position, gs_h):
        """Mirror of editor.js's uPositionToGsY for an ASCENDING-units rack."""
        if gs_h > 2:
            return rack_u_height * 2 - u_position * 2 - gs_h + 2
        return rack_u_height * 2 - u_position * 2

    @classmethod
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E Displace Mfr {suffix}", "slug": f"e2e-displace-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E Displace Role {suffix}", "slug": f"e2e-displace-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E Displace Site {suffix}", "slug": f"e2e-displace-site-{suffix}",
            "status": "active"})
        dt_full2 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Displace-2U-Full-{suffix}",
            "slug": f"e2e-displace-2u-full-{suffix}", "u_height": 2, "is_full_depth": True})
        dt_half1 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-Displace-1U-Half-{suffix}",
            "slug": f"e2e-displace-1u-half-{suffix}", "u_height": 1, "is_full_depth": False})
        rack = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E Displace Rack {suffix}", "site": site["id"],
            "status": "active", "u_height": RACK_U_HEIGHT})

        cf_override = {"custom_fields": {"warranty_type": ""}}

        dev_a = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-displace-old-{suffix}", "device_type": dt_full2["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "6.0", "face": "front", "status": "active", **cf_override})
        dev_b = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-displace-new-{suffix}", "device_type": dt_full2["id"],
            "role": role["id"], "site": site["id"], "rack": rack["id"],
            "position": "14.0", "face": "front", "status": "active", **cf_override})

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"], rack=rack["id"],
            device_types=[dt_full2["id"], dt_half1["id"]],
            devices=[dev_a["id"], dev_b["id"]],
        )
        cls._rack_id = rack["id"]
        cls._rack_u_height = RACK_U_HEIGHT
        cls._dev_a_id = dev_a["id"]
        cls._dev_a_label = dev_a["name"]
        cls._dev_b_id = dev_b["id"]
        cls._dev_b_label = dev_b["name"]
        cls._dt_half_id = dt_half1["id"]
        # dev_a: U6-7 (2U) -> gs-h 4; free landing slot for its move-away: U10-11.
        cls._dev_a_orig_gsy = cls._u_to_gsy(RACK_U_HEIGHT, 6, 4)
        cls._dev_a_target_gsy = cls._u_to_gsy(RACK_U_HEIGHT, 10, 4)
        cls._dev_b_orig_gsy = cls._u_to_gsy(RACK_U_HEIGHT, 14, 4)
        # A second free landing slot (for E7's "move NEW away again" step).
        cls._free_gsy = cls._u_to_gsy(RACK_U_HEIGHT, 17, 4)

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"displace-{suffix}", "site": site["id"], "racks": [rack["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/{rack['id']}/")

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
                cls._design_id = None
            created = getattr(cls, "_created", None)
            if created:
                for did in created.get("devices", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/devices/{did}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                if created.get("rack") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{created['rack']}/")
                    except Exception:
                        pass
                if created.get("role") is not None:
                    try:
                        cls._api(
                            "DELETE", f"/api/dcim/device-roles/{created['role']}/")
                    except Exception:
                        pass
                if created.get("manufacturer") is not None:
                    try:
                        cls._api(
                            "DELETE",
                            f"/api/dcim/manufacturers/{created['manufacturer']}/")
                    except Exception:
                        pass
                if created.get("site") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
                    except Exception:
                        pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._created = None
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1600, "height": 1400})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._storage = cls._api_ctx.storage_state()
            cls._csrf = next(
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
            cls._provision_fixture()
            cls.HARNESS_JS = DISPLACE_HARNESS_JS_TEMPLATE % {"rack_pk": cls._rack_id}
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    def _load_editor(self):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)  # let GridStack finish init
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    def widx(self, **match):
        widgets = self.page.evaluate("() => window.__rdDisplace.baseWidgets")
        for idx, w in enumerate(widgets):
            if w.get("opposite_face"):
                continue
            if all(w.get(k) == v for k, v in match.items()):
                return idx, w
        self.fail(f"no widget matching {match} in baseWidgets: {widgets}")

    def model_check(self):
        return self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")

    def _create_ghost_for_dev_a(self):
        """Move dev_a away to its free landing slot -- leaves a move-out
        ghost (+ mirror hatch, dev_a is full-depth) at its origin U6-7."""
        idx_a, _ = self.widx(kind="existing", face="front", device_id=self._dev_a_id)
        moved = self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_a}', {self._dev_a_target_gsy})")
        self.assertTrue(moved, "moveTile could not find dev_a")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        # dev_a becoming a MOVE (still kind "existing") also opens the §4a
        # rename dialog. APPLY it (keep-name): a dismissal ABORTS the move
        # per §4a -- masked before 2026-07-08 by the Bootstrap transition
        # bug that swallowed dismissals of a freshly-opened modal.
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        return idx_a

    # =====================================================================
    # E5 (spec §4.3): drop NEW onto OLD's ghost slot -> dialog -> confirm ->
    # NEW styled move_in at full width, OLD collapses to a red side stripe
    # naming OLD, its full-depth mirror hatch collapses too.
    # =====================================================================
    def test_e5_displace_ghost_confirm(self):
        self._load_editor()
        idx_a = self._create_ghost_for_dev_a()
        idx_b, _ = self.widx(kind="existing", face="front", device_id=self._dev_b_id)

        moved = self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._dev_a_orig_gsy})")
        self.assertTrue(moved, "moveTile could not find dev_b")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        has_dialog = self.page.evaluate("() => window.__rdDisplace.hasDisplaceDialog()")
        self.assertTrue(has_dialog, "expected a displacement confirm dialog to appear")

        confirmed = self.page.evaluate("() => window.__rdDisplace.confirmDisplaceDialog()")
        self.assertTrue(confirmed, "could not find/click the displace-confirm button")
        self.page.wait_for_timeout(300)  # bootstrap fade-out
        # APPLY the follow-up rename dialog (a dismissal would abort the move).
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        info_b = self.page.evaluate(f"() => window.__rdDisplace.tileInfo('{idx_b}')")
        self.assertIn("nbx-rd-state-move_in", info_b["classes"], info_b)
        self.assertNotIn("nbx-rd-displaced", info_b["classes"], info_b)

        ghosts = self.page.evaluate(
            f"() => window.__rdDisplace.ghostInfo({json.dumps(self._dev_a_label)})")
        self.assertEqual(len(ghosts), 1, ghosts)
        self.assertTrue(ghosts[0]["displaced"], f"OLD's ghost must collapse: {ghosts}")
        self.assertIn(self._dev_a_label, ghosts[0]["stripeTitle"] or "", ghosts)

        mirror = self.page.evaluate(
            f"() => window.__rdDisplace.ghostMirrorInfo('{idx_a}')")
        self.assertIsNotNone(mirror, "full-depth OLD's ghost mirror hatch must exist")
        self.assertTrue(
            mirror["displaced"],
            f"OLD's mirror hatch must ALSO collapse (both full-depth): {mirror}")

        violations = [v for v in self.model_check() if self._dev_a_label in v or self._dev_b_label in v]
        self.assertEqual(violations, [], f"read-model must stay clean: {violations}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

        print("\n=== E5 SUMMARY (displace ghost, confirm) ===")
        print(f"  NEW classes: {info_b['classes']}")
        print(f"  OLD ghost:   {ghosts[0]}")
        print(f"  OLD mirror:  {mirror}")

    # =====================================================================
    # E6 (spec §4.3.5): same as E5 but CANCEL at the dialog -> full revert:
    # NEW back at its origin, OLD's ghost rendering untouched, no stripe.
    # =====================================================================
    def test_e6_displace_ghost_cancel(self):
        self._load_editor()
        idx_a = self._create_ghost_for_dev_a()
        idx_b, _ = self.widx(kind="existing", face="front", device_id=self._dev_b_id)

        moved = self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._dev_a_orig_gsy})")
        self.assertTrue(moved, "moveTile could not find dev_b")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        has_dialog = self.page.evaluate("() => window.__rdDisplace.hasDisplaceDialog()")
        self.assertTrue(has_dialog, "expected a displacement confirm dialog to appear")

        cancelled = self.page.evaluate("() => window.__rdDisplace.cancelDisplaceDialog()")
        self.assertTrue(cancelled, "could not find/click the displace-cancel button")
        self.page.wait_for_timeout(300)
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        info_b = self.page.evaluate(f"() => window.__rdDisplace.tileInfo('{idx_b}')")
        self.assertEqual(info_b["y"], self._dev_b_orig_gsy, f"NEW must snap back: {info_b}")
        self.assertIn("nbx-rd-state-existing", info_b["classes"], info_b)

        ghosts = self.page.evaluate(
            f"() => window.__rdDisplace.ghostInfo({json.dumps(self._dev_a_label)})")
        self.assertEqual(len(ghosts), 1, ghosts)
        self.assertFalse(
            ghosts[0]["displaced"],
            f"a cancelled displacement must leave OLD's ghost untouched: {ghosts}")
        self.assertIsNone(ghosts[0]["stripeTitle"])

        self.assertEqual(self.errors, [], f"console errors: {self.errors}")
        print("\n=== E6 SUMMARY (displace ghost, cancel) ===")
        print(f"  NEW classes: {info_b['classes']}")
        print(f"  OLD ghost:   {ghosts[0]}")

    # =====================================================================
    # E7 (spec §4.3.5): E5, then move NEW away again -> OLD's ghost
    # rendering (and its full-depth mirror) is restored.
    # =====================================================================
    def test_e7_displace_ghost_then_move_new_away(self):
        self._load_editor()
        idx_a = self._create_ghost_for_dev_a()
        idx_b, _ = self.widx(kind="existing", face="front", device_id=self._dev_b_id)

        self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._dev_a_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdDisplace.confirmDisplaceDialog()")
        self.page.wait_for_timeout(300)
        # APPLY the follow-up rename (a dismissal would abort the move).
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        ghosts_mid = self.page.evaluate(
            f"() => window.__rdDisplace.ghostInfo({json.dumps(self._dev_a_label)})")
        self.assertTrue(ghosts_mid and ghosts_mid[0]["displaced"], ghosts_mid)

        # Move dev_b away again, to an unrelated free slot.
        moved_away = self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._free_gsy})")
        self.assertTrue(moved_away, "moveTile could not find dev_b for its second move")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        ghosts_after = self.page.evaluate(
            f"() => window.__rdDisplace.ghostInfo({json.dumps(self._dev_a_label)})")
        self.assertEqual(len(ghosts_after), 1, ghosts_after)
        self.assertFalse(
            ghosts_after[0]["displaced"],
            f"moving NEW away must restore OLD's ghost rendering: {ghosts_after}")

        mirror_after = self.page.evaluate(
            f"() => window.__rdDisplace.ghostMirrorInfo('{idx_a}')")
        self.assertIsNotNone(mirror_after)
        self.assertFalse(
            mirror_after["displaced"],
            f"OLD's mirror hatch must also be restored: {mirror_after}")

        self.assertEqual(self.errors, [], f"console errors: {self.errors}")
        print("\n=== E7 SUMMARY (displace ghost, then move NEW away) ===")
        print(f"  OLD ghost after:  {ghosts_after[0]}")
        print(f"  OLD mirror after: {mirror_after}")

    # =====================================================================
    # Stripe geometry (spec §3, user ruling 2026-07-09): the displacement
    # stripe is a bar OUTSIDE the rack frame -- hanging off the face grid's
    # RIGHT edge, vertically spanning exactly the displaced rows (NetBox
    # core reservation-bar look, recoloured red) -- NOT a sliver inside the
    # occupying tile next to its x button. Front bar on the front grid,
    # mirror bar (full-depth OLD) on the rear grid. Removed on restore (E7).
    # =====================================================================
    def test_e5_stripe_bar_outside_rack_frame(self):
        self._load_editor()
        idx_a = self._create_ghost_for_dev_a()
        idx_b, _ = self.widx(kind="existing", face="front", device_id=self._dev_b_id)

        self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._dev_a_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdDisplace.confirmDisplaceDialog()")
        self.page.wait_for_timeout(300)
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        bars = self.page.evaluate(
            f"() => window.__rdDisplace.stripeGeometry({json.dumps(self._dev_a_label)})")
        # dev_a is full-depth and dev_b is too, so BOTH faces get a bar.
        self.assertEqual(
            sorted(b["face"] for b in bars), ["front", "rear"],
            f"expected one stripe bar per face: {bars}")

        ghost_boxes = self.page.evaluate(
            f"() => window.__rdDisplace.ghostBoxes({json.dumps(self._dev_a_label)})")
        front_ghost = next((g for g in ghost_boxes if g["face"] == "front"), None)
        self.assertIsNotNone(front_ghost, ghost_boxes)

        for bar in bars:
            self.assertIsNotNone(bar["grid"], bar)
            # OUTSIDE the rack frame: the bar's left edge sits at/right of the
            # grid's right border (small tolerance for the border itself).
            self.assertGreaterEqual(
                bar["bar"]["left"], bar["grid"]["right"] - 1,
                f"stripe bar must hang OUTSIDE the grid's right edge, not "
                f"inside a tile: {bar}")
            # Vertically aligned to the displaced rows: same top/height as the
            # (front) ghost body tile's span, +-3px. Both faces' grids are
            # top-aligned, so the front ghost's OFFSET within its grid is the
            # expected offset within each bar's own grid.
            expected_offset = front_ghost["top"] - next(
                b["grid"]["top"] for b in bars if b["face"] == "front")
            self.assertLessEqual(
                abs((bar["bar"]["top"] - bar["grid"]["top"]) - expected_offset), 3,
                f"stripe bar must start at the displaced rows: {bar}, "
                f"expected offset {expected_offset}")
            self.assertLessEqual(
                abs(bar["bar"]["height"] - front_ghost["height"]), 3,
                f"stripe bar must span exactly the displaced rows: {bar}, "
                f"ghost height {front_ghost['height']}")
            self.assertIn(self._dev_a_label, bar["title"], bar)
            # Width ~1.5x the original 7px (user adjustment 2026-07-09).
            self.assertGreaterEqual(
                bar["bar"]["right"] - bar["bar"]["left"], 9,
                f"stripe bar must be ~10px wide (was 7px): {bar}")

        # ---- Hover surface is interactive and shows the displaced device's
        # info via the editor's device hover-card (user adjustment
        # 2026-07-09: a bare title tooltip was too weak -- the same card
        # shown for device tiles must appear, populated with OLD's data).
        # Wait out the dialogs' fade so no (even mid-transition) modal overlay
        # covers the bar during the hit test -- the editor removes the modal
        # element from the DOM once fully hidden.
        self.page.wait_for_function(
            "() => !document.querySelector('.nbx-rd-move-modal, .nbx-rd-displace-modal')",
            timeout=5000)
        hover = self.page.evaluate(
            """() => {
                const bar = document.querySelector(
                    '.nbx-rd-stripe[data-rd-stripe-face="front"]');
                if (!bar) { return {error: 'front stripe bar not found'}; }
                const r = bar.getBoundingClientRect();
                const hit = document.elementFromPoint(
                    r.left + r.width / 2, r.top + r.height / 2);
                const cs = getComputedStyle(bar);
                bar.dispatchEvent(new PointerEvent('pointerover', {bubbles: true}));
                const card = document.querySelector('.nbx-rd-hovercard');
                return {
                    hitIsBar: hit === bar,
                    hitDesc: hit ? {tag: hit.tagName,
                                    cls: (hit.className || '').toString().slice(0, 100),
                                    z: getComputedStyle(hit).zIndex} : null,
                    barBox: {left: r.left, top: r.top, w: r.width, h: r.height},
                    pointerEvents: cs.pointerEvents,
                    title: bar.getAttribute('title'),
                    cardVisible: card ? card.style.display !== 'none' : false,
                    cardText: card ? card.textContent : null,
                };
            }""")
        self.assertNotIn("error", hover, hover)
        self.assertTrue(hover["hitIsBar"], f"bar must be the actual hover target: {hover}")
        self.assertEqual(hover["pointerEvents"], "auto", hover)
        self.assertIn(self._dev_a_label, hover["title"] or "", hover)
        self.assertTrue(
            hover["cardVisible"],
            f"hovering the bar must show the device hover-card: {hover}")
        self.assertIn(
            self._dev_a_label, hover["cardText"] or "",
            f"the hover-card must name the displaced device: {hover}")

        # E7 leg: move NEW away -> the bars are removed with the restore.
        self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._free_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        bars_after = self.page.evaluate(
            f"() => window.__rdDisplace.stripeGeometry({json.dumps(self._dev_a_label)})")
        self.assertEqual(
            bars_after, [],
            f"restoring OLD must remove its stripe bars: {bars_after}")

        self.assertEqual(self.errors, [], f"console errors: {self.errors}")
        print("\n=== STRIPE-BAR SUMMARY (outside-the-frame geometry) ===")
        print(f"  bars: {bars}")

    # =====================================================================
    # The device that TOOK a displaced slot must render ABOVE the collapsed
    # displaced tile, so its OWN fill + name read as the occupant (user
    # ruling 2026-07-16: "оверхитинг должен быть от девайса который встал на
    # место девайса, лейбл тоже"). The displaced tile used to be z-index:4
    # with a dirty/remove dashed outline -- it stacked OVER the incoming
    # occupant and blanked its name (a colored, nameless box). It must sit
    # at/below the occupant and draw no outline (the external stripe marks it).
    # =====================================================================
    def test_displaced_tile_sits_below_occupant(self):
        self._load_editor()
        self._create_ghost_for_dev_a()
        idx_b, _ = self.widx(kind="existing", face="front", device_id=self._dev_b_id)
        self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._dev_a_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdDisplace.confirmDisplaceDialog()")
        self.page.wait_for_timeout(300)
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        z = self.page.evaluate("""() => {
            const disp = document.querySelector('.grid-stack-item.nbx-rd-displaced');
            if (!disp) return {err: 'no displaced tile'};
            const dn = disp.gridstackNode;
            const occ = [...disp.closest('.grid-stack').querySelectorAll('.grid-stack-item')]
                .find(t => t !== disp
                    && !t.classList.contains('nbx-rd-displaced')
                    && !t.classList.contains('nbx-rd-opposite')
                    && t.gridstackNode && t.gridstackNode.y === (dn && dn.y));
            const zi = el => { const v = getComputedStyle(el).zIndex; return v === 'auto' ? 0 : parseInt(v, 10); };
            const dc = disp.querySelector('.grid-stack-item-content');
            return {
                dispZ: zi(disp), occZ: occ ? zi(occ) : null, hasOcc: !!occ,
                dispOutline: dc ? getComputedStyle(dc).outlineStyle : null,
            };
        }""")
        self.assertNotIn("err", z, z)
        self.assertTrue(z["hasOcc"], f"no occupant tile at the displaced slot: {z}")
        self.assertLessEqual(
            z["dispZ"], z["occZ"],
            f"a displaced tile must sit at/below the occupant that took its "
            f"slot, never above it (was z-index:4, blanking the occupant): {z}")
        self.assertEqual(
            z["dispOutline"], "none",
            f"the displaced placeholder must draw no outline over the "
            f"occupant (the external stripe marks it): {z}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # =====================================================================
    # Tile label = ASSIGNED name + identity hover card + ghost<->body hover
    # link (three user rulings 2026-07-10, spec §3 rendering additions).
    # =====================================================================
    def test_rename_updates_visible_label_card_and_ghost_link(self):
        self._load_editor()
        idx_b, w_b = self.widx(kind="existing", face="front", device_id=self._dev_b_id)
        new_name = "renamed-dev-b-42"

        # Move dev_b to a free slot; rename it via the dialog's NEW-name path.
        moved = self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._free_gsy})")
        self.assertTrue(moved, "moveTile could not find dev_b")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        n = self.page.evaluate(
            f"() => window.__rdDisplace.applyRenameDialogsWithName({json.dumps(new_name)})")
        self.assertEqual(n, 1, "expected exactly one rename dialog")
        self.page.wait_for_function(
            "() => !document.querySelector('.nbx-rd-move-modal, .nbx-rd-displace-modal')",
            timeout=5000)
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        # (1) The tile's VISIBLE label is the assigned name; the stable
        # identity span keeps the device's real name (hidden).
        labels = self.page.evaluate(
            f"""() => {{
                const el = document.querySelector(
                    '.grid-stack-item[data-widget-index="{idx_b}"]');
                const identity = el.querySelector('.nbx-rd-label');
                const display = el.querySelector('.nbx-rd-name-display');
                return {{
                    identityText: identity ? identity.textContent : null,
                    identityHidden: identity
                        ? identity.classList.contains('nbx-rd-label-hidden') : null,
                    displayText: display ? display.textContent : null,
                    visibleText: display && display.offsetParent !== null
                        ? display.textContent
                        : (identity && identity.offsetParent !== null
                            ? identity.textContent : null),
                }};
            }}""")
        self.assertEqual(
            labels["displayText"], new_name,
            f"the tile must SHOW the assigned name: {labels}")
        self.assertEqual(
            labels["identityText"], self._dev_b_label,
            f"the identity span must keep the device's real name: {labels}")
        self.assertTrue(labels["identityHidden"], labels)
        self.assertEqual(labels["visibleText"], new_name, labels)

        # (2) The hover card tells the identity story: NEW name + OLD name.
        card = self.page.evaluate(
            f"""() => {{
                const el = document.querySelector(
                    '.grid-stack-item[data-widget-index="{idx_b}"]');
                const content = el.querySelector('.grid-stack-item-content');
                content.dispatchEvent(new PointerEvent('pointerover', {{bubbles: true}}));
                const c = document.querySelector('.nbx-rd-hovercard');
                return {{
                    visible: c ? c.style.display !== 'none' : false,
                    text: c ? c.textContent : null,
                }};
            }}""")
        self.assertTrue(card["visible"], card)
        self.assertIn(new_name, card["text"] or "", card)
        self.assertIn(
            self._dev_b_label, card["text"] or "",
            f"the card must show the device's real (old) name too: {card}")

        # (3) Ghost <-> body hover link: hovering the move_in body highlights
        # the origin ghost; leaving clears it; and the reverse direction.
        link = self.page.evaluate(
            f"""() => {{
                const body = document.querySelector(
                    '.grid-stack-item[data-widget-index="{idx_b}"]');
                const ghost = document.querySelector(
                    '.grid-stack-item.nbx-rd-state-move_out_ghost'
                    + '[data-rd-device-id="{self._dev_b_id}"]');
                if (!ghost) {{ return {{error: 'ghost not found by device id'}}; }}
                const out = {{}};
                body.dispatchEvent(new PointerEvent('pointerover', {{bubbles: true}}));
                out.ghostLinkedOnBodyHover =
                    ghost.classList.contains('nbx-rd-hover-linked');
                body.dispatchEvent(new PointerEvent('pointerout', {{bubbles: true}}));
                out.ghostClearedOnLeave =
                    !ghost.classList.contains('nbx-rd-hover-linked');
                ghost.dispatchEvent(new PointerEvent('pointerover', {{bubbles: true}}));
                out.bodyLinkedOnGhostHover =
                    body.classList.contains('nbx-rd-hover-linked');
                ghost.dispatchEvent(new PointerEvent('pointerout', {{bubbles: true}}));
                out.bodyClearedOnLeave =
                    !body.classList.contains('nbx-rd-hover-linked');
                return out;
            }}""")
        self.assertNotIn("error", link, link)
        self.assertTrue(link["ghostLinkedOnBodyHover"], link)
        self.assertTrue(link["ghostClearedOnLeave"], link)
        self.assertTrue(link["bodyLinkedOnGhostHover"], link)
        self.assertTrue(link["bodyClearedOnLeave"], link)

        violations = [v for v in self.model_check() if self._dev_b_label in v]
        self.assertEqual(violations, [], f"read-model must stay clean: {violations}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")
        print("\n=== RENAME LABEL/CARD/LINK SUMMARY ===")
        print(f"  labels: {labels}")
        print(f"  card text: {card['text']}")
        print(f"  link: {link}")

    # =====================================================================
    # Cancelling a move (× on the move_in tile) must revert the NAME too,
    # not just the position (user bug 2026-07-14: a renamed move reverted its
    # slot but the tile kept showing "<design>-<name>" instead of the real
    # device name).
    # =====================================================================
    def test_cancel_move_reverts_assigned_name(self):
        self._load_editor()
        idx_b, w_b = self.widx(kind="existing", face="front", device_id=self._dev_b_id)

        # Move dev_b and rename it -> the tile shows the assigned name overlay.
        self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._free_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate(
            f"() => window.__rdDisplace.applyRenameDialogsWithName("
            f"{json.dumps('renamed-cancel-9')})")
        self.page.wait_for_function(
            "() => !document.querySelector('.nbx-rd-move-modal')", timeout=5000)
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        before = self.page.evaluate(f"() => window.__rdDisplace.tileNames('{idx_b}')")
        self.assertEqual(before["display"], "renamed-cancel-9", before)
        self.assertTrue(before["identityHidden"], before)

        # × on the move_in tile -> cancelMove: position AND name revert.
        self.assertTrue(
            self.page.evaluate(f"() => window.__rdDisplace.clickRemove('{idx_b}')"),
            "could not click × on the move_in tile")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        after = self.page.evaluate(f"() => window.__rdDisplace.tileNames('{idx_b}')")
        self.assertIsNone(
            after["display"],
            f"the assigned-name overlay must be gone after cancel: {after}")
        self.assertEqual(
            after["identity"], self._dev_b_label,
            f"the identity label must be the real device name: {after}")
        self.assertFalse(
            after["identityHidden"],
            f"the identity label must be visible again after cancel: {after}")
        info = self.page.evaluate(f"() => window.__rdDisplace.tileInfo('{idx_b}')")
        self.assertIn("nbx-rd-state-existing", info["classes"], info)
        self.assertEqual(
            info["y"], self._dev_b_orig_gsy,
            f"the device must be back at its origin row: {info}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_palette_add_shows_assigned_name(self):
        """A palette ADD's tile shows the ASSIGNED name once one exists
        (typed into the inline input, or auto-filled by the naming engine)
        -- never the device-type model while a name is set."""
        self._load_editor()
        # Drop a fresh palette add at a free slot (harness primitive).
        self.page.evaluate(
            f"() => window.__rdDisplace.dropPaletteItemAt("
            f"{self._dt_half_id}, 1, 'palette-add-under-test', false, 'front', "
            f"{self._u_to_gsy(self._rack_u_height, 3, 2)})")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)
        typed_name = "typed-add-name-7"
        result = self.page.evaluate(
            f"""() => {{
                // The freshest add tile: the one carrying the inline name input.
                const input = document.querySelector('.nbx-rd-name-input');
                if (!input) {{ return {{error: 'no add name input found'}}; }}
                const content = input.closest('.grid-stack-item-content');
                input.value = {json.dumps(typed_name)};
                input.dispatchEvent(new Event('input', {{bubbles: true}}));
                const identity = content.querySelector('.nbx-rd-label');
                const display = content.querySelector('.nbx-rd-name-display');
                return {{
                    identityText: identity ? identity.textContent : null,
                    identityHidden: identity
                        ? identity.classList.contains('nbx-rd-label-hidden') : null,
                    displayText: display ? display.textContent : null,
                }};
            }}""")
        self.assertNotIn("error", result, result)
        self.assertEqual(
            result["displayText"], typed_name,
            f"the add tile must SHOW the assigned name, not the type model: {result}")
        self.assertTrue(result["identityHidden"], result)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # =====================================================================
    # SAVED displacement, ON-LOAD rendering parity (spec §3/§4.3, parity
    # ruling 2026-07-09): a displacement persisted to the DB (a move of OLD
    # + an add of NEW at OLD's origin rows) must render collapsed + striped
    # the moment the editor LOADS -- not only after an in-session gesture
    # ran displaceOne. Pre-fix: two full tiles composited (the user's live
    # report was the read-only elevation, but the editor's on-load render
    # had the same hole).
    # =====================================================================
    def test_saved_displacement_renders_on_load(self):
        # Persist the displacement via the API: OLD (dev_a) moves U6 -> U10;
        # NEW (a catalog add, full-depth like OLD) lands on the vacated U6.
        move = self._api("POST", "/api/plugins/rack-design/placements/", {
            "design": self._design_id,
            "kind": "move",
            "device": self._dev_a_id,
            "target_rack": self._rack_id,
            "target_position": 10,
            "target_face": "front",
        })
        new_label = "e2e-saved-displace-new"
        add = self._api("POST", "/api/plugins/rack-design/placements/", {
            "design": self._design_id,
            "kind": "add",
            "device_type": self._created["device_types"][0],  # 2U full-depth
            "target_rack": self._rack_id,
            "target_position": 6,
            "target_face": "front",
            "proposed_name": new_label,
        })
        try:
            self._load_editor()   # FRESH page: only the saved state renders.
            self.page.wait_for_timeout(300)  # first refreshGhosts settle

            # OLD's persistent ghost is collapsed on load.
            ghosts = self.page.evaluate(
                f"() => window.__rdDisplace.ghostInfo({json.dumps(self._dev_a_label)})")
            self.assertEqual(len(ghosts), 1, ghosts)
            self.assertTrue(
                ghosts[0]["displaced"],
                f"a SAVED displacement must render collapsed ON LOAD: {ghosts}")
            self.assertIn(self._dev_a_label, ghosts[0]["stripeTitle"] or "", ghosts)

            # The outside stripe bars exist on BOTH faces (OLD is full-depth,
            # NEW is full-depth) and hang outside the grids.
            bars = self.page.evaluate(
                f"() => window.__rdDisplace.stripeGeometry({json.dumps(self._dev_a_label)})")
            self.assertEqual(
                sorted(b["face"] for b in bars), ["front", "rear"],
                f"expected one on-load stripe bar per face: {bars}")
            for bar in bars:
                self.assertGreaterEqual(
                    bar["bar"]["left"], bar["grid"]["right"] - 1, bar)

            # NEW renders as the single live full tile at those rows.
            new_idx = self.page.evaluate(
                f"() => window.__rdDisplace.findByLabel({json.dumps(new_label)})")
            self.assertIsNotNone(new_idx, "NEW's add tile must render on load")
            info_new = self.page.evaluate(
                f"() => window.__rdDisplace.tileInfo('{new_idx}')")
            self.assertIn("nbx-rd-state-add", info_new["classes"], info_new)
            self.assertNotIn("nbx-rd-displaced", info_new["classes"], info_new)

            violations = [
                v for v in self.model_check()
                if self._dev_a_label in v or new_label in v
            ]
            self.assertEqual(violations, [], f"read-model must stay clean: {violations}")
            self.assertEqual(self.errors, [], f"console errors: {self.errors}")
            print("\n=== SAVED-DISPLACEMENT ON-LOAD SUMMARY ===")
            print(f"  OLD ghost: {ghosts[0]}")
            print(f"  bars: {[(b['face'], b['bar']['left'], b['grid']['right']) for b in bars]}")
        finally:
            # Leave the shared class design pristine for the other tests.
            for placement in (add, move):
                try:
                    self._api(
                        "DELETE",
                        f"/api/plugins/rack-design/placements/{placement['id']}/")
                except Exception:
                    pass

    # =====================================================================
    # E11 (spec §4.3, §4.8): a fresh palette-add landing on a vacating slot
    # follows the same flow, styled `add` (not `move_in`). The add is only
    # 1U/half-depth here, so ONLY the front stripe should appear -- OLD's
    # full-depth mirror hatch is untouched (nothing occupies the rear).
    # =====================================================================
    def test_e11_displace_ghost_via_palette_add(self):
        self._load_editor()
        idx_a = self._create_ghost_for_dev_a()

        # dropPaletteItemAt's OWN return value is void here: onPaletteDrop
        # opens the displace dialog and returns BEFORE assigning a
        # widget-index -- that only happens inside finishAdd, run from the
        # dialog's confirm callback (spec §4.3.4). Look the tile up by label
        # once it exists.
        self.page.evaluate(
            f"() => window.__rdDisplace.dropPaletteItemAt("
            f"'{self._dt_half_id}', 1, 'e2e-displace-add', false, 'front', "
            f"{self._dev_a_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        has_dialog = self.page.evaluate("() => window.__rdDisplace.hasDisplaceDialog()")
        self.assertTrue(has_dialog, "expected a displacement confirm dialog for the add")

        confirmed = self.page.evaluate("() => window.__rdDisplace.confirmDisplaceDialog()")
        self.assertTrue(confirmed, "could not find/click the displace-confirm button")
        self.page.wait_for_timeout(300)
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        new_idx = self.page.evaluate(
            "() => window.__rdDisplace.findByLabel('e2e-displace-add')")
        self.assertIsNotNone(new_idx, "the add's tile could not be found by label after confirm")
        info_new = self.page.evaluate(f"() => window.__rdDisplace.tileInfo('{new_idx}')")
        self.assertIsNotNone(info_new, "the add's tile must exist after confirm")
        self.assertIn("nbx-rd-state-add", info_new["classes"], info_new)

        ghosts = self.page.evaluate(
            f"() => window.__rdDisplace.ghostInfo({json.dumps(self._dev_a_label)})")
        self.assertEqual(len(ghosts), 1, ghosts)
        self.assertTrue(ghosts[0]["displaced"], f"OLD's ghost must collapse: {ghosts}")

        mirror = self.page.evaluate(
            f"() => window.__rdDisplace.ghostMirrorInfo('{idx_a}')")
        self.assertIsNotNone(mirror)
        self.assertFalse(
            mirror["displaced"],
            f"a half-depth add must NOT touch OLD's rear mirror hatch: {mirror}")

        self.assertEqual(self.errors, [], f"console errors: {self.errors}")
        print("\n=== E11 SUMMARY (displace ghost via palette add) ===")
        print(f"  NEW classes: {info_new['classes']}")
        print(f"  OLD ghost:   {ghosts[0]}")
        print(f"  OLD mirror:  {mirror}")

    # =====================================================================
    # T3 (spec §4.1 "One occupant per vacated slot", ruling 2026-07-08):
    # once NEW occupies OLD's vacated (ghost) slot, NEW's live body claim
    # must block ANY further placement there -- a second device dropped on
    # the same rows is rejected outright (no dialog, no stacking).
    # =====================================================================
    def test_second_device_blocked_on_taken_vacated_slot(self):
        self._load_editor()
        self._create_ghost_for_dev_a()
        idx_b, _ = self.widx(kind="existing", face="front", device_id=self._dev_b_id)

        # First occupant: dev_b onto OLD's ghost rows, dialog confirmed.
        self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_b}', {self._dev_a_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdDisplace.confirmDisplaceDialog()")
        self.page.wait_for_timeout(300)
        # APPLY the follow-up rename (a dismissal would abort the move).
        self.page.evaluate("() => window.__rdDisplace.applyRenameDialogs()")
        # Wait for BOTH answered dialogs' overlays to be fully torn down
        # (Bootstrap removes them ~300ms after the hide transition) so the
        # no-dialog assertion below can only be satisfied by the SECOND
        # drop's own behavior, never fooled by a fading leftover overlay.
        self.page.wait_for_function(
            "() => document.querySelectorAll("
            "'.nbx-rd-displace-modal, .nbx-rd-move-modal').length === 0",
            timeout=5000)
        info_b = self.page.evaluate(f"() => window.__rdDisplace.tileInfo('{idx_b}')")
        self.assertEqual(info_b["y"], self._dev_a_orig_gsy,
                         f"first occupant must be committed: {info_b}")

        # Second device: a palette add dropped onto the SAME rows -- must be
        # rejected (NEW's live body blocks; no second plan into one slot).
        self.page.evaluate(
            f"() => window.__rdDisplace.dropPaletteItemAt("
            f"'{self._dt_half_id}', 1, 'e2e-displace-second', false, 'front', "
            f"{self._dev_a_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        has_dialog = self.page.evaluate("() => window.__rdDisplace.hasDisplaceDialog()")
        self.assertFalse(
            has_dialog,
            "a drop onto an ALREADY-TAKEN vacated slot must be rejected "
            "before any dialog (live body claim blocks)")
        second_idx = self.page.evaluate(
            "() => window.__rdDisplace.findByLabel('e2e-displace-second')")
        self.assertIsNone(
            second_idx,
            "the second device must NOT register on the taken vacated slot")

        # The first occupant is untouched.
        info_b2 = self.page.evaluate(f"() => window.__rdDisplace.tileInfo('{idx_b}')")
        self.assertEqual(info_b2["y"], self._dev_a_orig_gsy, info_b2)
        self.assertEqual(self.model_check(), [], "read-model must stay clean")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

        print("\n=== T3 SUMMARY (second device onto taken vacated slot) ===")
        print(f"  first occupant intact at y={info_b2['y']}; second rejected")

    # =====================================================================
    # Self-return (spec §4.4/§8.3): moving a device back onto its OWN
    # origin ghost is a SILENT revert -- no dialog, ghost gone, device state
    # restored.
    # =====================================================================
    def test_self_return_onto_own_ghost_is_silent(self):
        self._load_editor()
        idx_a = self._create_ghost_for_dev_a()

        moved_back = self.page.evaluate(
            f"() => window.__rdDisplace.moveTile('{idx_a}', {self._dev_a_orig_gsy})")
        self.assertTrue(moved_back, "moveTile could not find dev_a for its self-return")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        has_dialog = self.page.evaluate("() => window.__rdDisplace.hasDisplaceDialog()")
        self.assertFalse(has_dialog, "a self-return onto one's own ghost must never dialog")

        info_a = self.page.evaluate(f"() => window.__rdDisplace.tileInfo('{idx_a}')")
        self.assertEqual(info_a["y"], self._dev_a_orig_gsy, info_a)
        self.assertIn("nbx-rd-state-existing", info_a["classes"], info_a)
        self.assertNotIn("nbx-rd-displaced", info_a["classes"], info_a)

        ghosts = self.page.evaluate(
            f"() => window.__rdDisplace.ghostInfo({json.dumps(self._dev_a_label)})")
        self.assertEqual(ghosts, [], f"the ghost must be gone entirely: {ghosts}")

        violations = [v for v in self.model_check() if self._dev_a_label in v]
        self.assertEqual(violations, [], f"read-model must stay clean: {violations}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

        print("\n=== SELF-RETURN SUMMARY ===")
        print(f"  dev_a classes: {info_a['classes']}")


# ---------------------------------------------------------------------------
# Cross-rack sweep harness. Two racks rendered side by side; every helper is
# rack-parameterized and the moving subject is tracked by its LABEL (its
# `data-widget-index` is re-stamped on every cross-rack adoption, so the
# index is not a stable identity across hops -- the label span is).
# ---------------------------------------------------------------------------
CROSSRACK_HARNESS_JS_TEMPLATE = r"""
window.__rdX = (function () {
    var RACKS = [%(rack_a)s, %(rack_b)s, %(rack_c)s];
    var root = document.getElementById("rd-editor");

    function blockFor(rackId) {
        return document.querySelector('.nbx-rd-rack-block[data-rack-id="' + rackId + '"]');
    }
    function hostFor(rackId, face) {
        return document.getElementById("nbx-rd-grid-" + face + "-" + rackId);
    }
    function gridFor(rackId, face) { return hostFor(rackId, face).gridstack; }

    function fireHandler(grid, name, arg) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler[name];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: name }, arg); });
    }
    function fireDropped(grid, newNode) {
        var handlers = grid._gsEventHandler && grid._gsEventHandler["dropped"];
        var list = Array.isArray(handlers) ? handlers : (handlers ? [handlers] : []);
        list.forEach(function (h) { h({ type: "dropped" }, null, newNode); });
    }

    // Same collision-engine-bypassing position write as HARNESS_JS_TEMPLATE
    // (see there for the full rationale incl. the _orig float-pack hazard).
    function fastSetY(grid, el, newGsY) {
        var node = el.gridstackNode;
        node.x = 0;
        node.y = newGsY;
        node._orig = { x: node.x, y: node.y };
        grid._writePosAttr(el, node);
    }

    // The subject's LIVE body tile, by label: never a derived hatch, never a
    // ghost, never a temp ghost. Exactly one must exist at any settle point.
    function tileByLabel(label) {
        var hit = null;
        root.querySelectorAll(".grid-stack-item").forEach(function (el) {
            if (hit) { return; }
            if (el.getAttribute("data-rd-derived-opp")) { return; }
            if (el.hasAttribute("data-rd-temp-ghost")) { return; }
            if (el.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
            var span = el.querySelector(".nbx-rd-label");
            if (span && span.textContent === label) { hit = el; }
        });
        return hit;
    }

    function whereIs(el) {
        var host = el.closest(".grid-stack");
        if (!host) { return { rackId: null, face: null }; }
        var block = el.closest(".nbx-rd-rack-block");
        return {
            rackId: block ? parseInt(block.getAttribute("data-rack-id"), 10) : null,
            face: host.getAttribute("data-face") || null,
        };
    }

    // Move the subject (by label) to (rackId, face, gsY). Same-grid moves use
    // the moveTile primitive; anything else (cross-face OR cross-rack) uses
    // the cross-grid sequence mirroring a real GridStack grid-to-grid drag:
    // dragstart(source) -> removeWidget(source, keep DOM) -> re-parent ->
    // makeWidget at the EXACT target row (registration fires the real `added`
    // DOM event, which is what drives editor.js's cross-rack adoption +
    // drop-time validation) -> `dropped` on the destination. Registering at
    // the target row directly -- unlike HARNESS_JS_TEMPLATE's register-at-
    // row-0 -- is deliberate: the drop decision (maybePromptMove, fired from
    // the destination's `added` handler for a cross-rack hop) must be made at
    // the REAL candidate position, exactly like a live drop.
    function moveTo(label, rackId, face, gsY) {
        var el = tileByLabel(label);
        if (!el) { return { ok: false, reason: "subject not found" }; }
        var at = whereIs(el);
        var destGrid = gridFor(rackId, face);
        var destHost = hostFor(rackId, face);
        if (at.rackId === rackId && at.face === face) {
            var grid = el.gridstackNode.grid;
            fireHandler(grid, "dragstart", el);
            fastSetY(grid, el, gsY);
            fireHandler(grid, "change", []);
            fireHandler(grid, "dragstop", el);
            return { ok: true, mode: "same-grid" };
        }
        var srcGrid = el.gridstackNode.grid;
        fireHandler(srcGrid, "dragstart", el);
        srcGrid.removeWidget(el, false);
        if (el.parentNode !== destHost) { destHost.appendChild(el); }
        el.setAttribute("gs-x", "0");
        el.setAttribute("gs-y", String(gsY));
        destGrid.makeWidget(el);
        // The engine registered the node; pin it to the exact requested row
        // (registration-time bound-fixing may have shifted it) -- but only if
        // the drop was NOT already rejected-and-re-homed by the destination's
        // `added` handler (cancelMove/restoreTile relocates the DOM node).
        if (el.parentNode === destHost && el.gridstackNode
                && el.gridstackNode.y !== gsY) {
            fastSetY(destGrid, el, gsY);
        }
        var newNode = el.gridstackNode || { el: el };
        fireDropped(destGrid, newNode);
        return { ok: true, mode: "cross-grid" };
    }

    // ---- Cursor-governed placement primitives (spec §4.1 hard rule,
    // 2026-07-08 ruling). The editor tracks the REAL pointer during a
    // gesture (document-level pointermove); these synthesize the same
    // events so the deterministic shim can drive the exact vendor-fallback
    // sequence of the live bug: preview/placeholder parked at the last
    // VALID slot while the CURSOR hovers (and releases over) illegal rows.
    function pointerAt(x, y) {
        document.dispatchEvent(new MouseEvent("pointermove", {
            clientX: x, clientY: y, bubbles: true }));
    }
    function rowCenterY(host, row) {
        var r = host.getBoundingClientRect();
        var maxRow = parseInt(host.getAttribute("gs-max-row"), 10)
            || (host.gridstack && host.gridstack.opts.maxRow);
        return r.top + (row + 0.5) * (r.height / maxRow);
    }
    // The live-bug replay: grab `label`'s tile (pointer armed on its top
    // edge), start the drag, move the CURSOR over (rackId, face, cursorGsY)
    // -- the ILLEGAL rows -- then let GridStack's fallback adopt the tile at
    // `landGsY` (the last-valid slot the vendor placeholder stayed on) and
    // fire the drop. Per the spec rule the release must snap the tile back
    // home, because the cursor's rows are illegal at release. Returns
    // whether the deny indicator was visible while the cursor hovered the
    // illegal rows (assertable by the caller).
    function moveToWithCursorFallback(label, rackId, face, landGsY, cursorGsY) {
        var el = tileByLabel(label);
        if (!el) { return { ok: false, reason: "subject not found" }; }
        var srcGrid = el.gridstackNode.grid;
        var tr = el.getBoundingClientRect();
        // Arm the pointer on the tile's TOP edge (grab offset = 0 rows).
        pointerAt(tr.left + tr.width / 2, tr.top + 2);
        fireHandler(srcGrid, "dragstart", el);
        var destGrid = gridFor(rackId, face);
        var destHost = hostFor(rackId, face);
        // The CURSOR moves over the illegal rows and stays there.
        pointerAt(destHost.getBoundingClientRect().left + 20,
                  rowCenterY(destHost, cursorGsY));
        var denyVisible = !!document.querySelector(".nbx-rd-cursor-deny");
        // GridStack's fallback: the tile is adopted at the last-valid slot
        // (NOT the cursor rows) -- registration fires the real `added`
        // event, exactly like the vendor cross-grid drop path.
        srcGrid.removeWidget(el, false);
        if (el.parentNode !== destHost) { destHost.appendChild(el); }
        el.setAttribute("gs-x", "0");
        el.setAttribute("gs-y", String(landGsY));
        destGrid.makeWidget(el);
        if (el.parentNode === destHost && el.gridstackNode
                && el.gridstackNode.y !== landGsY) {
            fastSetY(destGrid, el, landGsY);
        }
        var newNode = el.gridstackNode || { el: el };
        fireDropped(destGrid, newNode);
        return { ok: true, denyVisible: denyVisible };
    }

    function countRenameDialogs() {
        return document.querySelectorAll(".nbx-rd-move-modal").length;
    }

    // Dismiss the (single, freshly-opened) rename dialog via its Cancel
    // button (spec §4a: any dismissal other than Apply aborts the move --
    // issue #17). Returns false if no rename dialog is open.
    function cancelRenameDialogViaCancelButton() {
        var btn = document.querySelector(".nbx-rd-move-modal .btn-link[data-bs-dismiss]");
        if (!btn) { return false; }
        btn.click();
        return true;
    }

    // Same, via the modal's x close button instead of the Cancel link --
    // both are wired to the same finishCancel()+requestHide() handler.
    function cancelRenameDialogViaCloseButton() {
        var btn = document.querySelector(".nbx-rd-move-modal .btn-close[data-bs-dismiss]");
        if (!btn) { return false; }
        btn.click();
        return true;
    }

    // Issue #21 -- displacement dialog helpers, rack-qualified variants of
    // the ones DISPLACE_HARNESS_JS_TEMPLATE already has (single-rack there;
    // this harness spans multiple rack blocks).
    function hasDisplaceDialog() {
        return !!document.querySelector(".nbx-rd-displace-modal");
    }
    function confirmDisplaceDialog() {
        var btn = document.querySelector(".nbx-rd-displace-modal [data-rd-displace-confirm]");
        if (!btn) { return false; }
        btn.click();
        return true;
    }
    function cancelDisplaceDialog() {
        var btn = document.querySelector(".nbx-rd-displace-modal .btn-link[data-bs-dismiss]");
        if (!btn) { return false; }
        btn.click();
        return true;
    }

    // Every move_out_ghost body tile (temp or persistent) matching `label`,
    // across ALL racks, with its stripe/displaced state -- the assertion
    // surface for the displacement stripe (spec §3, §4.3).
    function ghostInfo(label) {
        var out = [];
        RACKS.forEach(function (rackId) {
            var block = blockFor(rackId);
            if (!block) { return; }
            block.querySelectorAll(".grid-stack-item.nbx-rd-state-move_out_ghost").forEach(function (el) {
                if (el.getAttribute("data-rd-derived-opp")) { return; }
                var span = el.querySelector(".nbx-rd-label");
                if (!span || span.textContent !== label) { return; }
                var host = el.closest(".grid-stack");
                var face = host ? host.getAttribute("data-face") : null;
                // The stripe bar lives OUTSIDE the tile (spec §3, ruling
                // 2026-07-09), associated by owner label + face.
                var stripe = block.querySelector(
                    '.nbx-rd-stripe[data-rd-stripe-for="' + label + '"]'
                    + '[data-rd-stripe-face="' + (face || "") + '"]');
                out.push({
                    rackId: rackId, face: face,
                    widx: el.getAttribute("data-widget-index"),
                    classes: Array.prototype.slice.call(el.classList),
                    displaced: el.classList.contains("nbx-rd-displaced"),
                    stripeTitle: stripe ? stripe.getAttribute("title") : null,
                });
            });
        });
        return out;
    }

    // A full-depth ghost's owned mirror hatch (opposite face), by owner
    // rack + owner LABEL -- a derived-opp hatch's `.nbx-rd-label` always
    // matches its owner's (confirmed live: ghost body tiles do not carry
    // `data-widget-index`, unlike normal live tiles, so owner identity here
    // is matched by label + the ghost-mirror-specific CSS hook
    // (`nbx-rd-opposite-ghost`) instead of `data-rd-owner-widx`).
    function ghostMirrorInfo(ownerRackId, ownerLabel) {
        var block = blockFor(ownerRackId);
        if (!block) { return null; }
        var hit = null;
        block.querySelectorAll("[data-rd-derived-opp].nbx-rd-opposite-ghost").forEach(function (el) {
            if (hit) { return; }
            var span = el.querySelector(".nbx-rd-label");
            if (span && span.textContent === ownerLabel) { hit = el; }
        });
        if (!hit) { return null; }
        var host = hit.closest(".grid-stack");
        var face = host ? host.getAttribute("data-face") : null;
        // Mirror's stripe bar lives OUTSIDE the tile, associated by owner
        // label + this (opposite) face.
        var stripe = block.querySelector(
            '.nbx-rd-stripe[data-rd-stripe-for="' + ownerLabel + '"]'
            + '[data-rd-stripe-face="' + (face || "") + '"]');
        return {
            face: face,
            classes: Array.prototype.slice.call(hit.classList),
            displaced: hit.classList.contains("nbx-rd-displaced"),
            stripeTitle: stripe ? stripe.getAttribute("title") : null,
        };
    }

    // Palette drag-in with CURSOR mechanics (spec §4.1 palette context,
    // ruling 2026-07-08). Mirrors a real user gesture the way the editor's
    // pointer tracker sees it: (1) pointerdown ON the palette item (arms
    // the palette gesture), (2) pointer moves over (rackId, face,
    // cursorGsY), (3) GridStack registers the clone at `landGsY` -- the
    // engine's fallback slot -- and fires `dropped`, (4) pointerup.
    // Returns whether the deny indicator was visible while the cursor
    // hovered the target rows.
    function dropPaletteItemAtWithCursor(rackId, dtId, uHeight, label, isFullDepth,
                                         face, landGsY, cursorGsY) {
        var grid = gridFor(rackId, face);
        var host = hostFor(rackId, face);
        var clone = document.createElement("div");
        clone.className = "grid-stack-item nbx-rd-palette-item";
        clone.setAttribute("data-device-type-id", String(dtId));
        clone.setAttribute("data-u-height", String(uHeight));
        clone.setAttribute("data-label", label);
        clone.setAttribute("data-is-full-depth", isFullDepth ? "true" : "false");
        var content = document.createElement("div");
        content.className = "grid-stack-item-content";
        clone.appendChild(content);
        // The pointerdown must BUBBLE to the document-level tracker.
        document.body.appendChild(clone);
        clone.dispatchEvent(new MouseEvent("pointerdown", {
            clientX: 5, clientY: 5, bubbles: true }));
        pointerAt(host.getBoundingClientRect().left + 20,
                  rowCenterY(host, cursorGsY));
        var denyVisible = !!document.querySelector(".nbx-rd-cursor-deny");
        var gsH = Math.max(1, Math.round(uHeight * 2));
        var added = grid.addWidget(clone, { x: 0, y: 0, w: 1, h: gsH });
        var el = added || clone;
        fastSetY(grid, el, landGsY);
        fireDropped(grid, el.gridstackNode || { el: el });
        document.dispatchEvent(new MouseEvent("pointerup", {
            clientX: 0, clientY: 0, bubbles: true }));
        return { denyVisible: denyVisible };
    }

    function saveDisabled() {
        var b = document.getElementById("rd-editor-save");
        return b ? b.hasAttribute("disabled") : null;
    }

    // Palette-style add at an exact slot on a rack (same primitive as the
    // displacement harness's dropPaletteItemAt, rack-parameterized).
    function dropPaletteItemAt(rackId, dtId, uHeight, label, isFullDepth, face, gsY) {
        var grid = gridFor(rackId, face);
        var clone = document.createElement("div");
        clone.className = "grid-stack-item nbx-rd-palette-item";
        clone.setAttribute("data-device-type-id", String(dtId));
        clone.setAttribute("data-u-height", String(uHeight));
        clone.setAttribute("data-label", label);
        clone.setAttribute("data-is-full-depth", isFullDepth ? "true" : "false");
        var content = document.createElement("div");
        content.className = "grid-stack-item-content";
        clone.appendChild(content);
        var gsH = Math.max(1, Math.round(uHeight * 2));
        var added = grid.addWidget(clone, { x: 0, y: 0, w: 1, h: gsH });
        var el = added || clone;
        fastSetY(grid, el, gsY);
        fireDropped(grid, el.gridstackNode || { el: el });
        return true;
    }

    // Answer any open dialogs so the sweep can keep moving: a displacement
    // confirm is CONFIRMED (the sweep deliberately plans over vacating
    // slots), a move-rename dialog is APPLIED with its default (keep name).
    function answerDialogs() {
        var out = { displaced: 0, renamed: 0 };
        document.querySelectorAll(".nbx-rd-displace-modal [data-rd-displace-confirm]").forEach(function (btn) {
            btn.click();
            out.displaced++;
        });
        document.querySelectorAll(".nbx-rd-move-modal [data-rd-move-apply]").forEach(function (btn) {
            btn.click();
            out.renamed++;
        });
        return out;
    }

    // Position snapshot of EVERY tile in both racks: bodies, derived
    // hatches, move-out ghosts (temp + persistent). Keyed for comparison by
    // label + kind + (for hatches) owner identity.
    function snapshotAll() {
        var out = [];
        RACKS.forEach(function (rackId) {
            var block = blockFor(rackId);
            if (!block) { return; }
            block.querySelectorAll(".grid-stack-item").forEach(function (el) {
                var span = el.querySelector(".nbx-rd-label");
                var label = span ? span.textContent : "";
                var host = el.closest(".grid-stack");
                var face = host ? (host.getAttribute("data-face") || "") : "";
                var n = el.gridstackNode;
                var y = (n && n.y != null) ? n.y : parseInt(el.getAttribute("gs-y"), 10);
                var kind = el.getAttribute("data-rd-derived-opp") ? "hatch"
                    : (el.hasAttribute("data-rd-temp-ghost")
                       || el.classList.contains("nbx-rd-state-move_out_ghost")) ? "ghost"
                    : "body";
                var state = null;
                el.classList.forEach(function (c) {
                    if (c.indexOf("nbx-rd-state-") === 0) { state = c; }
                });
                out.push({
                    rackId: rackId, face: face, y: isNaN(y) ? null : y,
                    label: label, kind: kind, state: state,
                    displaced: el.classList.contains("nbx-rd-displaced"),
                });
            });
        });
        return out;
    }

    // (b) shadow-class-matches-owner check: for every owned DEVICE shadow
    // (derived hatch that is NOT a ghost mirror and NOT collapsed to a
    // stripe), find its owner body by the stamped identity and require the
    // owner's nbx-rd-state-* class to be present on the shadow.
    function shadowOwnerMismatches() {
        var out = [];
        root.querySelectorAll("[data-rd-derived-opp]").forEach(function (el) {
            if (el.classList.contains("nbx-rd-opposite-ghost")) { return; }
            if (el.classList.contains("nbx-rd-displaced")) { return; }
            var ownerRack = el.getAttribute("data-rd-owner-rack");
            var ownerWidx = el.getAttribute("data-rd-owner-widx");
            if (ownerRack == null || ownerWidx == null) { return; }
            var block = blockFor(ownerRack);
            if (!block) { return; }
            var owner = null;
            block.querySelectorAll(
                '.grid-stack-item[data-widget-index="' + ownerWidx + '"]'
            ).forEach(function (cand) {
                if (owner) { return; }
                if (cand.getAttribute("data-rd-derived-opp")) { return; }
                if (cand.hasAttribute("data-rd-temp-ghost")) { return; }
                if (cand.classList.contains("nbx-rd-state-move_out_ghost")) { return; }
                owner = cand;
            });
            if (!owner) { return; }
            var ownerState = null;
            owner.classList.forEach(function (c) {
                if (c.indexOf("nbx-rd-state-") === 0) { ownerState = c; }
            });
            if (ownerState && !el.classList.contains(ownerState)) {
                var span = el.querySelector(".nbx-rd-label");
                out.push({
                    label: span ? span.textContent : "",
                    ownerState: ownerState,
                    shadowClasses: Array.prototype.slice.call(el.classList),
                });
            }
        });
        return out;
    }

    function subjectInfo(label) {
        var el = tileByLabel(label);
        if (!el) { return null; }
        var at = whereIs(el);
        var n = el.gridstackNode;
        return {
            rackId: at.rackId, face: at.face,
            y: (n && n.y != null) ? n.y : parseInt(el.getAttribute("gs-y"), 10),
            classes: Array.prototype.slice.call(el.classList),
        };
    }

    // ---- Full-world snapshot + diff, RACK-QUALIFIED (every entity in every
    // rack, not just the swept subject) -- same rationale/shape as
    // HARNESS_JS_TEMPLATE's worldSnapshot/diffWorlds above, extended with a
    // `rack` field since this harness spans multiple rack blocks.
    function worldSnapshot() {
        var out = [];
        RACKS.forEach(function (rackId) {
            var block = blockFor(rackId);
            if (!block) { return; }
            block.querySelectorAll(".grid-stack-item").forEach(function (el) {
                var host = el.closest(".grid-stack");
                var face = host ? (host.getAttribute("data-face") || "") : "";
                var n = el.gridstackNode;
                var gsY = (n && n.y != null) ? n.y : parseInt(el.getAttribute("gs-y"), 10);
                var gsH = (n && n.h != null) ? n.h : parseInt(el.getAttribute("gs-h"), 10);
                var kind;
                if (el.getAttribute("data-rd-derived-opp")) {
                    kind = el.classList.contains("nbx-rd-opposite-ghost") ? "ghost-mirror" : "shadow";
                } else if (el.hasAttribute("data-rd-temp-ghost")) {
                    kind = "temp-ghost";
                } else if (el.classList.contains("nbx-rd-state-move_out_ghost")) {
                    kind = "ghost";
                } else {
                    kind = "body";
                }
                var span = el.querySelector(".nbx-rd-label");
                var content = el.querySelector(".grid-stack-item-content");
                var classes = [];
                el.classList.forEach(function (c) { if (c.indexOf("nbx-") === 0) { classes.push(c); } });
                classes.sort();
                out.push({
                    rack: rackId, face: face, kind: kind,
                    label: span ? span.textContent : "",
                    gsY: isNaN(gsY) ? null : gsY, gsH: isNaN(gsH) ? null : gsH,
                    classes: classes,
                    ownerWidx: el.getAttribute("data-rd-owner-widx"),
                    ownerRack: el.getAttribute("data-rd-owner-rack"),
                    widx: el.getAttribute("data-widget-index"),
                    title: content ? (content.getAttribute("title") || "") : "",
                    displaced: el.classList.contains("nbx-rd-displaced"),
                });
            });
        });
        out.sort(function (a, b) {
            function key(x) {
                return [x.rack, x.face, x.kind, x.label, x.widx || "", x.ownerWidx || "", x.gsY].join("|");
            }
            var ka = key(a), kb = key(b);
            return ka < kb ? -1 : (ka > kb ? 1 : 0);
        });
        return out;
    }

    function _entityKey(e) {
        var extra = (e.kind === "shadow" || e.kind === "ghost-mirror")
            ? ("#" + (e.ownerRack || "") + "/" + (e.ownerWidx || "")) : "";
        return e.rack + "|" + e.face + "|" + e.kind + "|" + e.label + extra;
    }

    // Ghost displaced/stripe state is SUBJECT-COUPLED presentation (spec
    // §4.3.3/§4.3.5) -- normalized out for ghost-kind entities, same as
    // HARNESS_JS_TEMPLATE's diffWorlds. Position stays strictly checked.
    function _isGhostKind(e) {
        return e.kind === "ghost" || e.kind === "temp-ghost" || e.kind === "ghost-mirror";
    }
    function _classesForDiff(e) {
        if (!_isGhostKind(e)) { return e.classes.join(","); }
        return e.classes.filter(function (c) { return c !== "nbx-rd-displaced"; }).join(",");
    }

    // `subjectLabel` nullable: entities with this label are exempt (the
    // subject is expected to change). Pass null/"" to require EVERYTHING,
    // subject included, to be identical (e.g. a rejected drop).
    function diffWorlds(prev, cur, subjectLabel) {
        var prevByKey = {}, curByKey = {};
        prev.forEach(function (e) { prevByKey[_entityKey(e)] = e; });
        cur.forEach(function (e) { curByKey[_entityKey(e)] = e; });
        function exempt(e) { return !!subjectLabel && e.label === subjectLabel; }
        var violations = [];
        Object.keys(prevByKey).forEach(function (k) {
            var before = prevByKey[k];
            if (exempt(before)) { return; }
            var after = curByKey[k];
            if (!after) {
                violations.push({ kind: "bystander_vanished", key: k, before: before });
                return;
            }
            var fields = _isGhostKind(before)
                ? ["face", "gsY", "gsH"]
                : ["face", "gsY", "gsH", "displaced"];
            fields.forEach(function (f) {
                if (before[f] !== after[f]) {
                    violations.push({
                        kind: "bystander_field_changed", key: k, field: f,
                        before: before[f], after: after[f],
                    });
                }
            });
            var bc = _classesForDiff(before), ac = _classesForDiff(after);
            if (bc !== ac) {
                violations.push({ kind: "bystander_classes_changed", key: k, before: bc, after: ac });
            }
            if (before.title !== after.title) {
                violations.push({
                    kind: "bystander_title_changed", key: k,
                    before: before.title, after: after.title,
                });
            }
        });
        Object.keys(curByKey).forEach(function (k) {
            if (!prevByKey[k] && !exempt(curByKey[k])) {
                violations.push({ kind: "bystander_appeared", key: k, after: curByKey[k] });
            }
        });
        return violations;
    }

    return {
        moveTo: moveTo,
        moveToWithCursorFallback: moveToWithCursorFallback,
        countRenameDialogs: countRenameDialogs,
        cancelRenameDialogViaCancelButton: cancelRenameDialogViaCancelButton,
        cancelRenameDialogViaCloseButton: cancelRenameDialogViaCloseButton,
        hasDisplaceDialog: hasDisplaceDialog,
        confirmDisplaceDialog: confirmDisplaceDialog,
        cancelDisplaceDialog: cancelDisplaceDialog,
        ghostInfo: ghostInfo,
        ghostMirrorInfo: ghostMirrorInfo,
        dropPaletteItemAt: dropPaletteItemAt,
        dropPaletteItemAtWithCursor: dropPaletteItemAtWithCursor,
        saveDisabled: saveDisabled,
        answerDialogs: answerDialogs,
        snapshotAll: snapshotAll,
        shadowOwnerMismatches: shadowOwnerMismatches,
        subjectInfo: subjectInfo,
        worldSnapshot: worldSnapshot,
        diffWorlds: diffWorlds,
    };
})();
"""


@unittest.skipUnless(_PREREQ_OK, f"editor sweep prerequisites not met: {_PREREQ_REASON}")
class EditorCrossRackSweepTestCase(unittest.TestCase):
    """The full CROSS-RACK 0.5U sweep the original spec asked for: two loaded
    racks rendered side by side, a subject moved 0.5U at a time across ALL
    rows of BOTH faces of BOTH racks (alternating front/rear per row), with
    the full invariant net asserted after EVERY step over ALL tiles:

      (a) no OTHER tile's (face, y) ever changes vs the pre-sweep baseline
          -- spec §4.1's "no other tile may change position during any
          gesture", checked at every settle point;
      (b) every owned device shadow carries its OWNER's current
          nbx-rd-state-* class (never a stale tint from a prior step);
      (c) zero page errors;
      (d) window.__rdModel.check() is clean.

    Violations are accumulated across the whole sweep and reported together.

    Subjects: (1) an EXISTING full-depth device homed on rack A -- its
    cross-rack hops exercise adoption, rejection (rack B has immovable
    obstacles + a full-depth shadow), and the rejected-foreign-drop revert
    seam that produced the live stale-shadow-tint bug; (2) a NEW palette-
    added 1U half-depth device -- swept across both faces of rack A only,
    because the editor's acceptWidgets policy deliberately never lets an
    unsaved add cross racks (isForeignRealTile requires existing/move_in).

    Rack B is loaded per the spec: a full-depth device WITH its opposite-
    face shadow (so the sweep crosses shadow rows on both faces), real
    bodies on both faces, and a move-out ghost (created in-editor during
    setup by moving a fixture device, exactly like a user would).
    """

    RACK_U = 16

    @classmethod
    def _api(cls, method, path, payload=None):
        headers = {"X-CSRFToken": cls._csrf, "Accept": "application/json"}
        kwargs = {"method": method, "headers": headers}
        if payload is not None:
            kwargs["data"] = payload
        resp = cls._api_ctx.request.fetch(f"{BASE}{path}", **kwargs)
        body = resp.text()
        if resp.status >= 400:
            raise RuntimeError(f"{method} {path} -> HTTP {resp.status}: {body[:500]}")
        return json.loads(body) if body.strip() else None

    @classmethod
    def _u_to_gsy(cls, u_position, gs_h):
        if gs_h > 2:
            return cls.RACK_U * 2 - u_position * 2 - gs_h + 2
        return cls.RACK_U * 2 - u_position * 2

    @classmethod
    def _provision_fixture(cls):
        suffix = uuid.uuid4().hex[:8]
        mfr = cls._api("POST", "/api/dcim/manufacturers/", {
            "name": f"E2E XRack Mfr {suffix}", "slug": f"e2e-xrack-mfr-{suffix}"})
        role = cls._api("POST", "/api/dcim/device-roles/", {
            "name": f"E2E XRack Role {suffix}", "slug": f"e2e-xrack-role-{suffix}",
            "color": "9e9e9e"})
        site = cls._api("POST", "/api/dcim/sites/", {
            "name": f"E2E XRack Site {suffix}", "slug": f"e2e-xrack-site-{suffix}",
            "status": "active"})
        dt_full2 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-XRack-2U-Full-{suffix}",
            "slug": f"e2e-xrack-2u-full-{suffix}", "u_height": 2, "is_full_depth": True})
        dt_full3 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-XRack-3U-Full-{suffix}",
            "slug": f"e2e-xrack-3u-full-{suffix}", "u_height": 3, "is_full_depth": True})
        dt_half1 = cls._api("POST", "/api/dcim/device-types/", {
            "manufacturer": mfr["id"], "model": f"E2E-XRack-1U-Half-{suffix}",
            "slug": f"e2e-xrack-1u-half-{suffix}", "u_height": 1, "is_full_depth": False})
        rack_a = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E XRack A {suffix}", "site": site["id"],
            "status": "active", "u_height": cls.RACK_U})
        rack_b = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E XRack B {suffix}", "site": site["id"],
            "status": "active", "u_height": cls.RACK_U})
        # Rack C: empty third rack, used only by the multi-hop chain test
        # (A -> B -> C -> A) to prove homecoming works no matter how many
        # intermediate racks a device passed through.
        rack_c = cls._api("POST", "/api/dcim/racks/", {
            "name": f"E2E XRack C {suffix}", "site": site["id"],
            "status": "active", "u_height": cls.RACK_U})

        cf_override = {"custom_fields": {"warranty_type": ""}}

        # Rack A: the swept subject -- 2U full-depth at U6 front.
        dev_subject = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-xrack-subject-{suffix}", "device_type": dt_full2["id"],
            "role": role["id"], "site": site["id"], "rack": rack_a["id"],
            "position": "6.0", "face": "front", "status": "active", **cf_override})
        # Rack B, loaded: full-depth 3U at U8 front (its shadow occupies the
        # mirrored rear rows -- the exact target of the live bug's gesture),
        # a 1U front body high up, a 1U rear body low down, and a 1U device
        # that setup will MOVE in-editor to leave a move-out ghost.
        dev_b_full = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-xrack-bfull-{suffix}", "device_type": dt_full3["id"],
            "role": role["id"], "site": site["id"], "rack": rack_b["id"],
            "position": "8.0", "face": "front", "status": "active", **cf_override})
        dev_b_front = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-xrack-bfront-{suffix}", "device_type": dt_half1["id"],
            "role": role["id"], "site": site["id"], "rack": rack_b["id"],
            "position": "2.0", "face": "front", "status": "active", **cf_override})
        dev_b_rear = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-xrack-brear-{suffix}", "device_type": dt_half1["id"],
            "role": role["id"], "site": site["id"], "rack": rack_b["id"],
            "position": "13.0", "face": "rear", "status": "active", **cf_override})
        dev_b_ghost = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-xrack-bghost-{suffix}", "device_type": dt_half1["id"],
            "role": role["id"], "site": site["id"], "rack": rack_b["id"],
            "position": "5.0", "face": "front", "status": "active", **cf_override})
        # Issue #21: a FULL-DEPTH ghost source in rack B, U3 front (rows
        # 24-27 on both faces -- clear of every other rack-B fixture AND of
        # rows 0-3, which several existing cross-rack tests treat as the
        # rack's known-free landing zone for the subject) -- used only by
        # the dedicated cross-rack displacement-on-adoption test, so OLD's
        # mirror-hatch collapse (both OLD and NEW full-depth) is actually
        # exercised, not just the half-depth OLD case already covered by
        # the half-depth bghost fixture above.
        dev_b_ghost_full = cls._api("POST", "/api/dcim/devices/", {
            "name": f"e2e-xrack-bghostfull-{suffix}", "device_type": dt_full2["id"],
            "role": role["id"], "site": site["id"], "rack": rack_b["id"],
            "position": "3.0", "face": "front", "status": "active", **cf_override})

        cls._created = dict(
            manufacturer=mfr["id"], role=role["id"], site=site["id"],
            racks=[rack_a["id"], rack_b["id"], rack_c["id"]],
            device_types=[dt_full2["id"], dt_full3["id"], dt_half1["id"]],
            devices=[dev_subject["id"], dev_b_full["id"], dev_b_front["id"],
                     dev_b_rear["id"], dev_b_ghost["id"], dev_b_ghost_full["id"]],
        )
        cls._rack_a = rack_a["id"]
        cls._rack_b = rack_b["id"]
        cls._rack_c = rack_c["id"]
        cls._site_id = site["id"]
        cls._subject_label = dev_subject["name"]
        cls._subject_device_id = dev_subject["id"]
        cls._subject_orig_gsy = cls._u_to_gsy(6, 4)
        cls._bfull_label = dev_b_full["name"]
        # dev_b_full: 3U at U8 -> gsH 6 -> gs rows [12, 18) on BOTH faces.
        cls._bfull_gsy = cls._u_to_gsy(8, 6)
        cls._bfront_label = dev_b_front["name"]   # 1U at rack B U2 front
        cls._bghost_label = dev_b_ghost["name"]
        cls._bghost_orig_gsy = cls._u_to_gsy(5, 2)     # rows 22-23 front
        cls._bghost_target_gsy = cls._u_to_gsy(12, 2)  # rows 8-9 front, free
        cls._dt_half_id = dt_half1["id"]
        cls._bghostfull_label = dev_b_ghost_full["name"]
        cls._bghostfull_orig_gsy = cls._u_to_gsy(3, 4)   # rows 24-27, free both faces
        cls._bghostfull_away_gsy = cls._u_to_gsy(6, 4)   # rows 18-21, free both faces

        design = cls._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"xrack-{suffix}", "site": site["id"],
            "racks": [rack_a["id"], rack_b["id"], rack_c["id"]]})
        cls._design_id = design["id"]
        cls.editor_url = (
            f"{BASE}/plugins/rack-design/designs/{cls._design_id}/editor/"
            f"{rack_a['id']}/")

    @classmethod
    def _cleanup_class(cls):
        try:
            if getattr(cls, "_design_id", None) is not None:
                try:
                    cls._api(
                        "DELETE",
                        f"/api/plugins/rack-design/designs/{cls._design_id}/")
                except Exception:
                    pass
                cls._design_id = None
            created = getattr(cls, "_created", None)
            if created:
                for did in created.get("devices", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/devices/{did}/")
                    except Exception:
                        pass
                for tid in created.get("device_types", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/device-types/{tid}/")
                    except Exception:
                        pass
                for rid in created.get("racks", []):
                    try:
                        cls._api("DELETE", f"/api/dcim/racks/{rid}/")
                    except Exception:
                        pass
                if created.get("role") is not None:
                    try:
                        cls._api(
                            "DELETE", f"/api/dcim/device-roles/{created['role']}/")
                    except Exception:
                        pass
                if created.get("manufacturer") is not None:
                    try:
                        cls._api(
                            "DELETE",
                            f"/api/dcim/manufacturers/{created['manufacturer']}/")
                    except Exception:
                        pass
                if created.get("site") is not None:
                    try:
                        cls._api("DELETE", f"/api/dcim/sites/{created['site']}/")
                    except Exception:
                        pass
        finally:
            for closer in (
                lambda: cls._api_ctx.close(),
                lambda: cls._browser.close(),
                lambda: cls._pw.stop(),
            ):
                try:
                    closer()
                except Exception:
                    pass

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._pw = sync_playwright().start()
        cls._browser = cls._pw.chromium.launch(channel="chrome", headless=True)
        cls._design_id = None
        cls._created = None
        cls._api_ctx = cls._browser.new_context(
            viewport={"width": 1600, "height": 1400})
        try:
            pg = cls._api_ctx.new_page()
            pg.goto(f"{BASE}/login/", wait_until="networkidle")
            pg.fill("#id_username", USER)
            pg.fill("#id_password", PASS)
            pg.click("button[type=submit]")
            pg.wait_for_load_state("networkidle")
            pg.close()
            cls._storage = cls._api_ctx.storage_state()
            cls._csrf = next(
                (c["value"] for c in cls._api_ctx.cookies()
                 if c["name"] == "csrftoken"), "")
            cls._provision_fixture()
            cls.HARNESS_JS = CROSSRACK_HARNESS_JS_TEMPLATE % {
                "rack_a": cls._rack_a, "rack_b": cls._rack_b, "rack_c": cls._rack_c}
        except BaseException:
            cls._cleanup_class()
            raise

    @classmethod
    def tearDownClass(cls):
        cls._cleanup_class()

    def _load_editor(self, url=None):
        self.ctx = self._browser.new_context(
            storage_state=self._storage, viewport={"width": 1600, "height": 1400})
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on(
            "console",
            lambda m: self.errors.append(f"{m.type}: {m.text}")
            if m.type == "error" else None)
        self.page.on("pageerror", lambda e: self.errors.append(f"PAGEERROR: {e}"))
        resp = self.page.goto(url or self.editor_url, wait_until="networkidle")
        self.assertIsNotNone(resp, "no response loading the editor URL")
        self.assertEqual(resp.status, 200, f"editor URL returned {resp.status}")
        self.page.wait_for_selector("#rd-editor", timeout=15000)
        self.page.wait_for_timeout(1000)
        self.page.add_script_tag(content=self.HARNESS_JS)

    def tearDown(self):
        if getattr(self, "ctx", None):
            self.ctx.close()

    # -- setup step shared by every test: create rack B's move-out ghost ----
    def _create_rack_b_ghost(self):
        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._bghost_label)}, "
            f"{self._rack_b}, 'front', {self._bghost_target_gsy})")
        self.assertTrue(r.get("ok"), f"ghost setup move failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

    # -- setup step for the dedicated displacement-on-adoption test (issue
    # #21): move rack B's FULL-DEPTH ghost source away, leaving a ghost (+
    # its own mirror hatch, since it is full-depth) at its origin U15 front.
    def _create_rack_b_full_ghost(self):
        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._bghostfull_label)}, "
            f"{self._rack_b}, 'front', {self._bghostfull_away_gsy})")
        self.assertTrue(r.get("ok"), f"full-depth ghost setup move failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

    # -- baseline: positions of every non-subject tile -----------------------
    def _baseline(self, subject_label):
        snap = self.page.evaluate("() => window.__rdX.snapshotAll()")
        base = {}
        for t in snap:
            if t["label"] == subject_label:
                continue
            key = (t["rackId"], t["label"], t["kind"], t["face"])
            base[key] = t["y"]
        return base

    def _check_step(self, subject_label, baseline, where, violations,
                     world_exempt_subject=True):
        if self.errors:
            for e in self.errors:
                violations.append(dict(where=where, kind="page_error", detail=e))
            self.errors = []

        snap = self.page.evaluate("() => window.__rdX.snapshotAll()")
        seen = {}
        for t in snap:
            if t["label"] == subject_label:
                continue
            key = (t["rackId"], t["label"], t["kind"], t["face"])
            seen[key] = t["y"]
        for key, y in baseline.items():
            if key not in seen:
                violations.append(dict(
                    where=where, kind="tile_missing",
                    detail=f"{key} present in baseline, missing now"))
            elif seen[key] != y:
                violations.append(dict(
                    where=where, kind="tile_moved",
                    detail=f"{key} moved: baseline y={y}, now y={seen[key]}"))
        for key in seen:
            if key not in baseline:
                violations.append(dict(
                    where=where, kind="tile_extra",
                    detail=f"unexpected non-subject tile appeared: {key}"))

        mismatches = self.page.evaluate(
            "() => window.__rdX.shadowOwnerMismatches()")
        for m in mismatches:
            violations.append(dict(
                where=where, kind="shadow_state_mismatch", detail=str(m)))

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        for mv in model_violations:
            violations.append(dict(where=where, kind="rd_model", detail=mv))

        # Full-world diff (upgrade, 2026-07-08): the checks above are scoped
        # to position + shadow-class-matches-owner; compare EVERY entity
        # (across BOTH racks) against the previous step's snapshot so a
        # bystander picking up a stale class/title, or an owner-identity
        # attribute drifting, cannot slip through a long sweep unnoticed.
        # Only the swept subject itself is exempt.
        world = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        if getattr(self, "_prevWorld", None) is not None:
            exempt_label = subject_label if world_exempt_subject else None
            world_violations = self.page.evaluate(
                "([prev, cur, lbl]) => window.__rdX.diffWorlds(prev, cur, lbl)",
                [self._prevWorld, world, exempt_label])
            for wv in world_violations:
                violations.append(dict(where=where, kind="world_" + wv["kind"], detail=wv))
        self._prevWorld = world

    def _assert_clean(self, violations, steps, title):
        print(f"\n=== CROSS-RACK SWEEP SUMMARY ({title}) ===")
        print(f"  total steps: {steps}")
        print(f"  total violations: {len(violations)}")
        by_kind = {}
        for v in violations:
            by_kind.setdefault(v["kind"], []).append(v)
        lines = [f"{len(violations)} violation(s); by kind: "
                 f"{ {k: len(vs) for k, vs in by_kind.items()} }"]
        for kind, vs in by_kind.items():
            for ex in vs[:3]:
                lines.append(f"  [{kind}] e.g. {ex}")
        self.assertEqual(violations, [], "\n" + "\n".join(lines))

    # Soft (accumulating, non-raising) variant of _assert_homecoming_contract
    # for use INSIDE the sweep loop, where a single failed row must not abort
    # the rest of the sweep -- every violation across the whole run is
    # reported together, same discipline as _check_step.
    def _check_homecoming_contract_soft(self, label, expect_ghost, where, violations):
        snap = self.page.evaluate("() => window.__rdX.snapshotAll()")
        bodies = [t for t in snap if t["label"] == label and t["kind"] == "body"]
        ghosts = [t for t in snap if t["label"] == label and t["kind"] == "ghost"]
        if len(bodies) != 1:
            violations.append(dict(
                where=where, kind="homecoming_body_count",
                detail=f"expected 1 body for {label!r}, found {len(bodies)}: {bodies}"))
        want = 1 if expect_ghost else 0
        if len(ghosts) != want:
            violations.append(dict(
                where=where, kind="homecoming_ghost_count",
                detail=f"expected {want} ghost(s) for {label!r} at {where}, "
                       f"found {len(ghosts)}: {ghosts}"))

    def _sweep(self, subject_label, gs_h, racks, violations,
               home_rack_id=None, home_face=None, home_orig_gsy=None):
        """Move the subject 0.5U at a time across every row of both faces of
        every rack in `racks`, alternating front/rear per row. When
        `home_rack_id` is given, every step taken back in that rack AFTER
        having visited a different rack first (a "return to origin" leg,
        spec §4.6) additionally asserts the homecoming contract: exactly one
        live body entity for the subject anywhere, and its origin ghost
        present iff this step did NOT land exactly back on the true origin
        slot."""
        max_row = self.RACK_U * 2 - gs_h
        steps = 0
        left_home_once = False
        for rack_id in racks:
            if home_rack_id is not None and rack_id != home_rack_id:
                left_home_once = True
            for row in range(0, max_row + 1):
                face = "front" if row % 2 == 0 else "rear"
                before = self.page.evaluate(
                    f"() => window.__rdX.subjectInfo({json.dumps(subject_label)})")
                rack_before = before["rackId"] if before else None
                self.page.evaluate(
                    f"() => window.__rdX.moveTo({json.dumps(subject_label)}, "
                    f"{rack_id}, '{face}', {row})")
                self.page.wait_for_timeout(STEP_SETTLE_MS)
                answered = self.page.evaluate("() => window.__rdX.answerDialogs()")
                self.page.wait_for_timeout(STEP_SETTLE_MS)
                where = f"rack={rack_id} {face} row={row}"
                # Spec §4.1 ruling (2026-07-08): EVERY committed cross-rack
                # move runs the full dialog pipeline. Any future commit path
                # that skips it fails loudly here. The exact-true-origin
                # homecoming is the one deliberate exception (silent restore,
                # spec §4.4/§8.3).
                after = self.page.evaluate(
                    f"() => window.__rdX.subjectInfo({json.dumps(subject_label)})")
                rack_after = after["rackId"] if after else None
                crossed_and_committed = (
                    rack_before is not None and rack_after == rack_id
                    and rack_before != rack_id)
                at_true_origin = (
                    home_rack_id is not None and rack_id == home_rack_id
                    and face == home_face and row == home_orig_gsy)
                if crossed_and_committed and not at_true_origin:
                    total = (answered or {}).get("renamed", 0) \
                        + (answered or {}).get("displaced", 0)
                    if total < 1:
                        violations.append(dict(
                            where=where, kind="dialog_pipeline_skipped",
                            detail="a committed cross-rack move opened no "
                                   "rename/displacement dialog"))
                self._check_step(subject_label, self._base, where, violations)
                if home_rack_id is not None and rack_id == home_rack_id and left_home_once:
                    self._check_homecoming_contract_soft(
                        subject_label, not at_true_origin, where, violations)
                steps += 1
        return steps

    # -- homecoming contract (spec §4.6, docs/editor-conformance-matrix.md):
    # exactly one BODY entity for `label` in the WHOLE dom (never two, never
    # zero), zero move-out ghosts for it (temp or persistent), the model
    # clean, and (if `expect_ghost`) exactly one ghost still marking its
    # TRUE origin instead. Used by both the full-homecoming and near-miss
    # regression tests below, and reusable by any future return-to-origin
    # assertion.
    def _assert_homecoming_contract(self, label, expect_ghost=False):
        snap = self.page.evaluate("() => window.__rdX.snapshotAll()")
        bodies = [t for t in snap if t["label"] == label and t["kind"] == "body"]
        ghosts = [t for t in snap if t["label"] == label and t["kind"] == "ghost"]
        hatches = [t for t in snap if t["label"] == label and t["kind"] == "hatch"]
        self.assertEqual(
            len(bodies), 1,
            f"expected exactly 1 live body entity for {label!r} in the "
            f"whole DOM, found {len(bodies)}: {bodies}")
        self.assertEqual(
            len(ghosts), (1 if expect_ghost else 0),
            f"expected {'1' if expect_ghost else '0'} move-out ghost(s) for "
            f"{label!r}, found {len(ghosts)}: {ghosts}")
        # A "hatch" (data-rd-derived-opp) covers BOTH a live device's own
        # opposite-face shadow AND a ghost's opposite-face mirror -- both are
        # legitimately present at once when a ghost still marks the true
        # origin (expect_ghost=True): the ghost's own mirror, plus the
        # revived body's own shadow at its new position.
        max_hatches = 2 if expect_ghost else 1
        self.assertLessEqual(
            len(hatches), max_hatches,
            f"expected at most {max_hatches} owned opposite-face hatch(es) "
            f"for {label!r}, found {len(hatches)}: {hatches}")
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(
            model_violations, [],
            f"read-model invariants must be clean after homecoming: "
            f"{model_violations}")

    # =====================================================================
    # Homecoming (spec §4.6, docs/editor-conformance-matrix.md — the
    # confirmed live 2-step bug, 2026-07-08): a full-depth EXISTING device
    # moved cross-rack (A -> B) and then dragged back onto its OWN origin
    # ghost's exact slot (B -> A, same U/face it started at) must fully
    # clear the ghost + adopted copy and restore the ORIGINAL entry as a
    # single `existing` tile -- never leave an orphan shadow in B, a stale
    # ghost in A, and a duplicate body in A (the five-entity incident).
    # =====================================================================
    def test_homecoming_return_to_exact_origin_is_silent_and_clean(self):
        self._load_editor()
        self._create_rack_b_ghost()
        world_before = self.page.evaluate("() => window.__rdX.worldSnapshot()")

        # Hop 1: rack A (true origin) -> rack B, a free front slot.
        r1 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', 0)")
        self.assertTrue(r1.get("ok"), f"hop 1 moveTo failed: {r1}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        mid = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertEqual(mid["rackId"], self._rack_b, mid)
        self.assertIn("nbx-rd-state-move_in", mid["classes"], mid)

        # Hop 2 (the exact user repro): rack B -> rack A, dropped BACK onto
        # its own origin ghost's exact rows.
        r2 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_a}, 'front', {self._subject_orig_gsy})")
        self.assertTrue(r2.get("ok"), f"hop 2 (homecoming) moveTo failed: {r2}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject tile lost after homecoming")
        self.assertEqual(info["rackId"], self._rack_a, info)
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], self._subject_orig_gsy, info)
        self.assertIn("nbx-rd-state-existing", info["classes"], info)
        self.assertNotIn("nbx-rd-state-move_in", info["classes"], info)

        self.assertEqual(
            self.errors, [], f"page/console errors during homecoming: {self.errors}")
        self._assert_homecoming_contract(self._subject_label, expect_ghost=False)

        # Full-world check: after the round trip, every BYSTANDER entity in
        # BOTH racks must be byte-for-byte identical to before the gesture
        # (the subject is exempt -- it legitimately left and came back).
        world_after = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur, lbl]) => window.__rdX.diffWorlds(prev, cur, lbl)",
            [world_before, world_after, self._subject_label])
        self.assertEqual(
            world_violations, [],
            f"bystander entities changed across the homecoming round trip: "
            f"{world_violations}")

    # =====================================================================
    # Near-miss homecoming (spec §4.4/§4.6 decision, docs/editor-behavior-
    # spec.md §4.4: "moving D back onto its own ghost = plain revert"):
    # returning to the origin RACK but a DIFFERENT U must NOT create a
    # second entity for the device -- it revives the ORIGINAL entry as an
    # ordinary move away from its true origin (ghost stays at the real
    # origin, the tile renders `move_in` at the new U).
    # =====================================================================
    def test_homecoming_near_miss_reuses_original_entry(self):
        self._load_editor()
        self._create_rack_b_ghost()
        world_before = self.page.evaluate("() => window.__rdX.worldSnapshot()")

        r1 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', 0)")
        self.assertTrue(r1.get("ok"), f"hop 1 moveTo failed: {r1}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # Hop 2: rack B -> rack A, but NOT the exact origin row (near miss).
        near_miss_gsy = self._subject_orig_gsy + 4
        r2 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_a}, 'front', {near_miss_gsy})")
        self.assertTrue(r2.get("ok"), f"hop 2 (near-miss) moveTo failed: {r2}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject tile lost after near-miss return")
        self.assertEqual(info["rackId"], self._rack_a, info)
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], near_miss_gsy, info)
        self.assertIn("nbx-rd-state-move_in", info["classes"], info)

        self.assertEqual(
            self.errors, [],
            f"page/console errors during near-miss homecoming: {self.errors}")
        # A single body entity, reviving the original entry -- plus its
        # ghost, which stays at the TRUE origin (never destroyed just
        # because the device came back to the same RACK).
        self._assert_homecoming_contract(self._subject_label, expect_ghost=True)

        world_after = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur, lbl]) => window.__rdX.diffWorlds(prev, cur, lbl)",
            [world_before, world_after, self._subject_label])
        self.assertEqual(
            world_violations, [],
            f"bystander entities changed across the near-miss round trip: "
            f"{world_violations}")

    # =====================================================================
    # Multi-hop chain (spec §4.6: "This falls out of ownership ... for any
    # number of hops"): A -> B -> C -> A. homecomingAdopt must still find the
    # TRUE origin (rack A) at the final hop even though the device's most
    # recent stop was rack C, never rack A directly before this drop --
    # proving the fix is DEVICE-IDENTITY based (findOwnGhostEntryIndex scans
    # the DESTINATION rack for its own ghost by device_id), not a
    # same-rack-only or "one hop back" special case.
    # =====================================================================
    def test_homecoming_after_three_hop_chain_still_finds_true_origin(self):
        self._load_editor()
        self._create_rack_b_ghost()
        world_before = self.page.evaluate("() => window.__rdX.worldSnapshot()")

        # Hop 1: A (true origin) -> B.
        r1 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', 0)")
        self.assertTrue(r1.get("ok"), f"hop 1 moveTo failed: {r1}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # Hop 2: B -> C (never touches A -- rack A's ghost must survive
        # untouched through this second, unrelated adoption).
        r2 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_c}, 'front', 0)")
        self.assertTrue(r2.get("ok"), f"hop 2 moveTo failed: {r2}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        mid = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertEqual(mid["rackId"], self._rack_c, mid)
        self.assertIn("nbx-rd-state-move_in", mid["classes"], mid)
        # Exactly one body world-wide even mid-chain (rack B's adopted copy
        # must be retired the instant the device left it for rack C).
        self._assert_homecoming_contract(self._subject_label, expect_ghost=True)

        # Hop 3 (the homecoming): C -> A, dropped exactly on the TRUE
        # origin's ghost -- A was never the immediate previous rack.
        r3 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_a}, 'front', {self._subject_orig_gsy})")
        self.assertTrue(r3.get("ok"), f"hop 3 (homecoming) moveTo failed: {r3}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject lost after the 3-hop homecoming")
        self.assertEqual(info["rackId"], self._rack_a, info)
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], self._subject_orig_gsy, info)
        self.assertIn("nbx-rd-state-existing", info["classes"], info)
        self.assertNotIn("nbx-rd-state-move_in", info["classes"], info)

        self.assertEqual(
            self.errors, [],
            f"page/console errors during the 3-hop homecoming: {self.errors}")
        self._assert_homecoming_contract(self._subject_label, expect_ghost=False)

        world_after = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur, lbl]) => window.__rdX.diffWorlds(prev, cur, lbl)",
            [world_before, world_after, self._subject_label])
        self.assertEqual(
            world_violations, [],
            f"bystander entities changed across the 3-hop chain: {world_violations}")

    # =====================================================================
    # Regression (user bug 2026-07-15): a device moved out to another rack,
    # then dragged BACK to its origin rack whose old slot is now OCCUPIED by
    # another device, must NOT overlap. homecomingAdopt committed the device to
    # its origin slot with no legality check -> I1 "a75(body) overlaps
    # b15(body)". The gate: decline the homecoming when the origin slot is
    # taken, and the rejected cross-rack drop returns the device to where it
    # came from -- never an overlap.
    # =====================================================================
    def test_homecoming_into_occupied_origin_never_overlaps(self):
        self._load_editor()
        subj = self._subject_label

        # 1. Move the subject A -> B (top rows 0-3 are the known-free zone).
        r1 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, {self._rack_b}, 'front', 0)")
        self.assertTrue(r1.get("ok"), f"step-1 A->B move failed: {r1}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # 2. Move a rack-B device INTO the subject's now-vacated origin slot in
        #    rack A, so the subject can no longer come home to it.
        r2 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._bfront_label)}, "
            f"{self._rack_a}, 'front', {self._subject_orig_gsy})")
        self.assertTrue(r2.get("ok"), f"step-2 occupy-origin move failed: {r2}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # 3. Homecoming attempt: drag the subject B -> A onto its (now occupied)
        #    origin slot. Must be REJECTED with NO overlap.
        r3 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, "
            f"{self._rack_a}, 'front', {self._subject_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        self.assertEqual(self.errors, [], f"console errors on homecoming reject: {self.errors}")
        violations = self.page.evaluate("() => window.__rdModel.check()")
        overlap = [v for v in violations if "overlaps" in v]
        self.assertEqual(
            overlap, [],
            f"a homecoming onto an OCCUPIED origin must never commit an overlap: {overlap}")

    # =====================================================================
    # Regression (user bug 2026-07-16, task #34): a device moved out to another
    # rack, then dragged back to its ORIGIN rack onto an OCCUPIED NON-origin
    # slot, must NOT overlap. homecomingAdopt revives the origin entry at the
    # DROP position but never repositions the tile; it validated the (free)
    # ORIGIN slot rather than the DROP slot, so an occupied drop elsewhere in
    # the origin rack slipped through and committed an overlap
    # (I1 "a36(body) overlaps b15(body)"). The gate must validate WHERE THE TILE
    # LANDED: an occupied drop declines the homecoming and routes through the
    # reject path (returns the device to its last position).
    #
    # CAVEAT (harness fidelity): __rdX.moveTo drives a cross-rack move by BOTH
    # makeWidget (-> `added` -> homecomingAdopt) AND fireDropped (-> `dropped`
    # -> maybePromptMove). So maybePromptMove ALWAYS runs here as a backstop and
    # reverts an occupied drop regardless of the homecomingAdopt decision --
    # i.e. this test does NOT fail on the pre-fix (origin-slot) validation; it
    # asserts the END-TO-END no-overlap invariant through the full harness
    # pipeline, not the homecomingAdopt gate in isolation. The live bug is a
    # REAL mouse drag (only `added` fires) where maybePromptMove did not run;
    # faithfully reproducing that needs an added-only harness path or a real
    # drag. See docs/editor-known-issues.md #34.
    # =====================================================================
    def test_move_back_to_origin_rack_onto_occupied_nonorigin_slot_never_overlaps(self):
        self._load_editor()
        subj = self._subject_label

        # 1. Move the subject A -> B (top rows 0-3 are the known-free zone). Its
        #    true origin (U6) in rack A is now free.
        r1 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, {self._rack_b}, 'front', 0)")
        self.assertTrue(r1.get("ok"), f"step-1 A->B move failed: {r1}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # 2. Occupy a NON-origin slot in rack A (U10, clear of the subject's U6
        #    origin) with a rack-B device (1U).
        occ_gsy = self._u_to_gsy(10, 2)
        r2 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._bfront_label)}, "
            f"{self._rack_a}, 'front', {occ_gsy})")
        self.assertTrue(r2.get("ok"), f"step-2 occupy-nonorigin move failed: {r2}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # 3. Drag the subject B -> A onto that OCCUPIED non-origin slot (U10; the
        #    subject is 2U so it overlaps the 1U occupant). Its origin U6 is FREE,
        #    so the old origin-slot check let this commit an overlap. Must REJECT.
        subj_gsy = self._u_to_gsy(10, 4)
        self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, "
            f"{self._rack_a}, 'front', {subj_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        self.assertEqual(self.errors, [], f"console errors on reject: {self.errors}")
        violations = self.page.evaluate("() => window.__rdModel.check()")
        overlap = [v for v in violations if "overlaps" in v]
        self.assertEqual(
            overlap, [],
            f"a drop onto an OCCUPIED non-origin slot in the origin rack must "
            f"never commit an overlap: {overlap}")

    # =====================================================================
    # A device whose HOME is rack A, moved to B and SAVED, then RELOADED (so
    # it is a PERSISTENT move_in in B with a real move-out ghost in A -- the
    # in-session crossRack/originRackId bookkeeping is gone). Dragging it back
    # toward its home A onto an OCCUPIED non-origin slot must be REJECTED and
    # the device returned to its LAST POSITION (rack B) STILL AS A move_in.
    #
    # The bug (user repro 2026-07-16, design 581 sg2-sl-b15 rack 321->318):
    # onDragStart records the CURRENT rack (B) as the device's origin for a
    # reloaded move_in (`originRackId = st.crossRack ? st.originRackId :
    # rackId`, with crossRack false after reload), so rejectDrop's multi-hop
    # reclaim (srcRackId !== originRackId) never fires and cancelMove's
    # branch (a) restoreTile()s the tile back into B as a PLAIN `existing`
    # device -- silently rewriting the plan: on save B would gain a native
    # device and A's planned move would vanish. Must stay a move_in.
    #
    # Unlike the overlap in the sibling test above, the tile's KIND after the
    # reject is deterministic through the harness pipeline (it does NOT depend
    # on which of `added`/`dropped` ran), so this DOES fail test-first on the
    # buggy origin computation. See docs/editor-known-issues.md.
    # =====================================================================
    def test_reloaded_move_in_rejected_homecoming_stays_move_in_not_existing(self):
        suffix = uuid.uuid4().hex[:8]
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"xrack-reload-rej-{suffix}", "site": self._site_id,
            "racks": [self._rack_a, self._rack_b]})
        reload_design_id = design["id"]
        reload_editor_url = (
            f"{BASE}/plugins/rack-design/designs/{reload_design_id}/editor/"
            f"{self._rack_a}/")
        try:
            self._load_editor(reload_editor_url)

            # 1. Move subject A -> B (front rows 0-3 are the known-free zone).
            r1 = self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
                f"{self._rack_b}, 'front', 0)")
            self.assertTrue(r1.get("ok"), f"A->B move failed: {r1}")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            # 2. Save (writes the 'move' DesignPlacement + a persistent move-out
            #    ghost in A) and let editor.js's doSave() reload the page. Same
            #    navigation dance as test_homecoming_after_save_and_reload.
            with self.page.expect_navigation(wait_until="networkidle", timeout=20000):
                self.page.evaluate(
                    "() => document.getElementById('rd-editor-save').click()")
            self.page.wait_for_selector("#rd-editor", timeout=15000)
            self.page.wait_for_timeout(500)
            self.page.add_script_tag(content=self.HARNESS_JS)

            reloaded = self.page.evaluate(
                f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
            self.assertIsNotNone(reloaded, "subject missing after save+reload")
            self.assertEqual(reloaded["rackId"], self._rack_b, reloaded)
            self.assertIn("nbx-rd-state-move_in", reloaded["classes"], reloaded)

            # 3. Occupy a NON-origin slot in rack A (U10; the subject's origin
            #    is U6) with a rack-B 1U device, so the homecoming DROP target
            #    is taken.
            occ_gsy = self._u_to_gsy(10, 2)
            r2 = self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(self._bfront_label)}, "
                f"{self._rack_a}, 'front', {occ_gsy})")
            self.assertTrue(r2.get("ok"), f"occupy-nonorigin move failed: {r2}")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            # 4. Drag the RELOADED move_in subject B -> A onto that OCCUPIED
            #    slot (2U at U10 overlaps the 1U occupant). Must be rejected.
            subj_gsy = self._u_to_gsy(10, 4)
            self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
                f"{self._rack_a}, 'front', {subj_gsy})")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            self.assertEqual(
                self.errors, [], f"console errors on reject: {self.errors}")
            # The user's live repro showed BOTH an I1 overlap AND an I2 "full-
            # depth but has no shadow" (the existing-conversion desynced shadow
            # ownership) -- so assert the model is FULLY clean, not merely
            # overlap-free.
            violations = self.page.evaluate("() => window.__rdModel.check()")
            self.assertEqual(
                violations, [],
                f"rejected homecoming left the model with violations: {violations}")

            info = self.page.evaluate(
                f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
            self.assertIsNotNone(info, "subject lost after rejected homecoming")
            # LAST POSITION: back in the source rack B ...
            self.assertEqual(
                info["rackId"], self._rack_b,
                f"a rejected homecoming must return the device to its last "
                f"position (rack B), not elsewhere: {info}")
            # ... and STILL a planned move (move_in), NOT rewritten to existing.
            self.assertIn(
                "nbx-rd-state-move_in", info["classes"],
                f"a rejected cross-rack re-drag of a reloaded move_in must keep "
                f"its move_in identity, not be converted to a native existing "
                f"device of the source rack: {info}")
            self.assertNotIn("nbx-rd-state-existing", info["classes"], info)
        finally:
            if getattr(self, "ctx", None):
                self.ctx.close()
                self.ctx = None
            try:
                self._api(
                    "DELETE",
                    f"/api/plugins/rack-design/designs/{reload_design_id}/")
            except Exception:
                pass

    # =====================================================================
    # A tile RECLAIMED to its source rack after a rejected cross-rack drop,
    # then re-dragged WITHIN that rack onto an illegal slot, must snap back
    # locally -- NOT fly to its home rack. (User repro 2026-07-16, design 581
    # sg2-sl-b15: after two rejected 321->318 hops that each reclaimed b15
    # into 321, a THIRD within-321 illegal move sent b15 to rack 318.)
    #
    # Root cause: reclaimFromReject recreates the tile as crossRack:true with
    # srcRackId:null. A subsequent WITHIN-rack reject then takes neither
    # rejectDrop.multiHopReclaim (needs srcRackId) nor havePreSnapBack (its
    # guard was !crossRack) -> it falls to cancelMove branch (a), which
    # re-homes the "live cross-rack" tile into its origin rack. A within-rack
    # reject must return the tile to its pre-drag slot in the SAME rack.
    # =====================================================================
    def test_reclaimed_tile_within_rack_reject_stays_local_not_flies_home(self):
        self._load_editor()
        subj = self._subject_label

        # 1. Move subject A -> B (front rows 0-3 free): a cross-rack move_in
        #    living in B, with subject's move-out ghost left at its A origin.
        r1 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, {self._rack_b}, 'front', 0)")
        self.assertTrue(r1.get("ok"), f"step-1 A->B move failed: {r1}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # 2. Occupy a NON-origin slot in rack A (U10; clear of subject's U6
        #    origin) so the B->A homecoming target is taken.
        occ_gsy = self._u_to_gsy(10, 2)
        r2 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._bfront_label)}, "
            f"{self._rack_a}, 'front', {occ_gsy})")
        self.assertTrue(r2.get("ok"), f"step-2 occupy-nonorigin move failed: {r2}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # 3. Drag subject B -> A onto that OCCUPIED slot -> rejected -> the tile
        #    is RECLAIMED back into rack B (crossRack:true, srcRackId:null).
        r3 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, "
            f"{self._rack_a}, 'front', {self._u_to_gsy(10, 4)})")
        self.assertTrue(r3.get("ok"), f"step-3 rejected B->A move failed: {r3}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        after_reclaim = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(subj)})")
        self.assertIsNotNone(after_reclaim, "subject lost after reclaim")
        self.assertEqual(
            after_reclaim["rackId"], self._rack_b,
            f"after a rejected B->A drop the subject must be reclaimed into "
            f"rack B: {after_reclaim}")

        # 4. Now a WITHIN-B illegal move: drop the reclaimed subject onto rack
        #    B's full-depth device (U8, rows 12-17) -- occupied -> must reject.
        r4 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, "
            f"{self._rack_b}, 'front', {self._u_to_gsy(8, 4)})")
        self.assertTrue(r4.get("ok"), f"step-4 within-B move failed: {r4}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        self.assertEqual(
            self.errors, [], f"console errors on within-rack reject: {self.errors}")
        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(subj)})")
        self.assertIsNotNone(info, "subject lost after within-rack reject")
        # THE BUG: a within-rack reject must NOT fly the tile to its home rack.
        self.assertEqual(
            info["rackId"], self._rack_b,
            f"a WITHIN-rack illegal move of a reclaimed tile must snap back in "
            f"the SAME rack (B), never fly to its home rack: {info}")
        violations = self.page.evaluate("() => window.__rdModel.check()")
        self.assertEqual(
            violations, [],
            f"within-rack reject of a reclaimed tile left violations: {violations}")

    # =====================================================================
    # Homecoming to the true origin must CLEAR the move's project-prefixed
    # name (user bug 2026-07-16): a committed move stamps a "<design>-<name>"
    # display overlay on the tile; dragging the device back onto its own
    # origin fully reverts the move, so the tile must show the device's REAL
    # identity again -- not the planned name. cancelMove already clears this
    # (2026-07-14 fix), but homecomingAdopt re-tagged the tile `existing`
    # without resetting the overlay, so the prefix stuck.
    #
    # The proposed name lives in a `.nbx-rd-name-display` span that hides the
    # real `.nbx-rd-label`; setTileDisplayName("") removes it. The test
    # asserts that overlay is present after the named move and GONE after the
    # homecoming.
    # =====================================================================
    def _subject_name_overlay(self, label):
        # The visible "<design>-<name>" overlay text on the subject's BODY
        # tile (not its opposite-face hatch), or None if the real identity is
        # showing.
        return self.page.evaluate(
            """(label) => {
                const els = Array.from(document.querySelectorAll('.grid-stack-item'));
                for (const el of els) {
                    if (el.classList.contains('nbx-rd-opposite')) continue;
                    if (el.getAttribute('data-rd-derived-opp')) continue;
                    if (el.getAttribute('data-rd-temp-ghost')) continue;
                    if (el.classList.contains('nbx-rd-state-move_out_ghost')) continue;
                    const lab = el.querySelector('.nbx-rd-label');
                    if (!lab || lab.textContent !== label) continue;
                    const disp = el.querySelector('.nbx-rd-name-display');
                    return disp ? disp.textContent : null;
                }
                return undefined;
            }""",
            label)

    def test_homecoming_to_origin_clears_move_proposed_name(self):
        self._load_editor()
        subj = self._subject_label

        # 1. Move subject A -> B and APPLY the rename dialog -> the tile now
        #    carries the "<design>-<name>" proposed-name overlay.
        r1 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, {self._rack_b}, 'front', 0)")
        self.assertTrue(r1.get("ok"), f"A->B move failed: {r1}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        overlay_moved = self._subject_name_overlay(subj)
        self.assertIsNotNone(
            overlay_moved,
            "precondition: a committed move should show a project-prefixed "
            "name overlay")

        # 2. Drag subject B -> A back onto its OWN origin slot (free) ->
        #    homecoming -> full revert.
        r2 = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(subj)}, "
            f"{self._rack_a}, 'front', {self._subject_orig_gsy})")
        self.assertTrue(r2.get("ok"), f"homecoming move failed: {r2}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(subj)})")
        self.assertIsNotNone(info, "subject lost after homecoming")
        self.assertEqual(info["rackId"], self._rack_a, info)
        self.assertIn("nbx-rd-state-existing", info["classes"], info)

        # 3. The move's project-prefixed name overlay must be GONE.
        overlay_home = self._subject_name_overlay(subj)
        self.assertIsNone(
            overlay_home,
            f"homecoming to the origin must clear the move's project-prefixed "
            f"name; tile still shows overlay {overlay_home!r}")
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # =====================================================================
    # After a homecoming, the revived tile must be a PROPER existing device so
    # that re-dragging it cross-rack again is adopted cleanly. (User repro
    # 2026-07-16, design 581 sg2-sl-b15 -> sg2-a5545-fire-1 corruption.)
    #
    # The bug: homecomingAdopt re-tagged the tile's DOM to `existing` but left
    # state[idx].widget.kind == 'move_out_ghost'. That DOM/state contradiction
    # made the NEXT cross-rack drag accepted-by-class (makeAccept sees the
    # `existing` DOM class) yet not-adopted-by-kind (onDragStart's eligibility
    # checks widget.kind -> tileInFlight stays null). The unadopted tile still
    # fired `dropped` in the destination, whose onPaletteDrop read the tile's
    # data-widget-index -- a value in the SOURCE rack's namespace -- as an
    # index into the DESTINATION rack's state[], mutating whatever unrelated
    # device sat at that index (I4 "device has 2 live entities", I1 overlap).
    # =====================================================================
    def test_homecomed_tile_redragged_cross_rack_is_adopted_not_corrupting(self):
        # Needs a PERSISTENT (saved+reloaded) move-out ghost, whose entry has
        # widget.kind == 'move_out_ghost' -- an in-session temp ghost keeps
        # kind 'existing', so it never exhibits the stale-kind contradiction.
        suffix = uuid.uuid4().hex[:8]
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"xrack-homecome-redrag-{suffix}", "site": self._site_id,
            "racks": [self._rack_a, self._rack_b]})
        reload_design_id = design["id"]
        reload_editor_url = (
            f"{BASE}/plugins/rack-design/designs/{reload_design_id}/editor/"
            f"{self._rack_a}/")
        subj = self._subject_label
        try:
            self._load_editor(reload_editor_url)

            # 1. subject A -> B, SAVE (persistent move + move-out ghost in A),
            #    reload -> subject is a reloaded move_in in B.
            r1 = self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(subj)}, "
                f"{self._rack_b}, 'front', 0)")
            self.assertTrue(r1.get("ok"), f"A->B move failed: {r1}")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            with self.page.expect_navigation(wait_until="networkidle", timeout=20000):
                self.page.evaluate(
                    "() => document.getElementById('rd-editor-save').click()")
            self.page.wait_for_selector("#rd-editor", timeout=15000)
            self.page.wait_for_timeout(500)
            self.page.add_script_tag(content=self.HARNESS_JS)

            reloaded = self.page.evaluate(
                f"() => window.__rdX.subjectInfo({json.dumps(subj)})")
            self.assertIsNotNone(reloaded, "subject missing after save+reload")
            self.assertEqual(reloaded["rackId"], self._rack_b, reloaded)

            # 2. subject B -> A back onto its OWN origin (free) -> homecoming
            #    (revives the PERSISTENT move-out ghost entry).
            r2 = self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(subj)}, "
                f"{self._rack_a}, 'front', {self._subject_orig_gsy})")
            self.assertTrue(r2.get("ok"), f"homecoming move failed: {r2}")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            home = self.page.evaluate(
                f"() => window.__rdX.subjectInfo({json.dumps(subj)})")
            self.assertEqual(home["rackId"], self._rack_a, home)
            self.assertIn("nbx-rd-state-existing", home["classes"], home)

            # 3. Re-drag the homecomed subject A -> B again. With the stale
            #    widget.kind it is not adopted and mis-indexes rack B's state[];
            #    a proper revive adopts it as a clean move_in.
            r3 = self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(subj)}, "
                f"{self._rack_b}, 'front', 0)")
            self.assertTrue(r3.get("ok"), f"re-drag A->B move failed: {r3}")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            self.assertEqual(self.errors, [], f"console errors: {self.errors}")
            violations = self.page.evaluate("() => window.__rdModel.check()")
            self.assertEqual(
                violations, [],
                f"re-dragging a homecomed tile cross-rack corrupted the model: "
                f"{violations}")
            info = self.page.evaluate(
                f"() => window.__rdX.subjectInfo({json.dumps(subj)})")
            self.assertIsNotNone(info, "subject lost after re-drag")
            self.assertEqual(info["rackId"], self._rack_b, info)
            self.assertIn(
                "nbx-rd-state-move_in", info["classes"],
                f"a homecomed tile re-dragged cross-rack must be adopted as a "
                f"move_in, not left unadopted with a stale ghost kind: {info}")
        finally:
            if getattr(self, "ctx", None):
                self.ctx.close()
                self.ctx = None
            try:
                self._api(
                    "DELETE",
                    f"/api/plugins/rack-design/designs/{reload_design_id}/")
            except Exception:
                pass

    # =====================================================================
    # Persistent ghost after a page reload (spec §4.6): the confirmed live
    # bug's in-session bookkeeping (tileInFlight.originRackId/crossRack) does
    # NOT survive a reload -- state[] is rehydrated from the server's JSON
    # payload, which carries no such runtime flags. A SAVED move still
    # leaves a real (server-rendered) persistent move-out ghost at the
    # origin, and dragging the reloaded move_in tile back onto it must
    # STILL home correctly, proving the fix's device-identity ghost lookup
    # (findOwnGhostEntryIndex) -- not the in-session hop chain -- is what
    # actually gates it.
    # =====================================================================
    def test_homecoming_after_save_and_reload_persistent_ghost(self):
        # A DEDICATED throwaway design (over the SAME class-fixture racks/
        # devices): this test really SAVES a move (a real DesignPlacement),
        # which must never leak into the other tests sharing this class's
        # own design/placements. Deleted in `finally` regardless of outcome.
        suffix = uuid.uuid4().hex[:8]
        design = self._api("POST", "/api/plugins/rack-design/designs/", {
            "title": f"xrack-reload-{suffix}", "site": self._site_id,
            "racks": [self._rack_a, self._rack_b]})
        reload_design_id = design["id"]
        reload_editor_url = (
            f"{BASE}/plugins/rack-design/designs/{reload_design_id}/editor/"
            f"{self._rack_a}/")
        try:
            self._load_editor(reload_editor_url)
            self._create_rack_b_ghost()

            r1 = self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
                f"{self._rack_b}, 'front', 0)")
            self.assertTrue(r1.get("ok"), f"hop moveTo failed: {r1}")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            # Save (writes a real 'move' DesignPlacement + persists a move-out
            # ghost in rack A); editor.js's own doSave() reloads the page on a
            # 200 response. Dispatched via JS (not Playwright's .click()) --
            # the Django Debug Toolbar's floating panel intercepts pointer
            # events on this dev instance and blocks a real synthetic click.
            # editor.js's doSave() RELOADS the page on a 200. That reload is a
            # real navigation that lands asynchronously AFTER the save POST
            # settles -- so a bare wait_for_load_state("networkidle") returns on
            # the PRE-reload page, and the reload then destroys the execution
            # context under a later add_script_tag / evaluate ("Execution
            # context was destroyed ... navigation"). Wait for the reload
            # navigation ITSELF to commit + settle before touching the page
            # again, so every subsequent evaluate runs on the stable fresh page.
            with self.page.expect_navigation(wait_until="networkidle", timeout=20000):
                self.page.evaluate(
                    "() => document.getElementById('rd-editor-save').click()")
            self.page.wait_for_selector("#rd-editor", timeout=15000)
            self.page.wait_for_timeout(500)
            self.page.add_script_tag(content=self.HARNESS_JS)

            reloaded = self.page.evaluate(
                f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
            self.assertIsNotNone(reloaded, "subject missing after save+reload")
            self.assertEqual(reloaded["rackId"], self._rack_b, reloaded)
            self.assertIn("nbx-rd-state-move_in", reloaded["classes"], reloaded)

            # Drag the RELOADED move_in tile back to rack A's exact original
            # slot -- tileInFlight's in-session bookkeeping is gone (fresh
            # page load), so this exercises the device-identity ghost lookup
            # alone.
            r2 = self.page.evaluate(
                f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
                f"{self._rack_a}, 'front', {self._subject_orig_gsy})")
            self.assertTrue(r2.get("ok"), f"homecoming moveTo failed: {r2}")
            self.page.wait_for_timeout(STEP_SETTLE_MS)
            self.page.evaluate("() => window.__rdX.answerDialogs()")
            self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

            info = self.page.evaluate(
                f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
            self.assertIsNotNone(
                info, "subject lost after homecoming from a reloaded persistent move")
            self.assertEqual(info["rackId"], self._rack_a, info)
            self.assertEqual(info["face"], "front", info)
            self.assertEqual(info["y"], self._subject_orig_gsy, info)
            self.assertIn("nbx-rd-state-existing", info["classes"], info)

            self.assertEqual(
                self.errors, [],
                f"page/console errors during reload homecoming: {self.errors}")
            self._assert_homecoming_contract(self._subject_label, expect_ghost=False)
        finally:
            if getattr(self, "ctx", None):
                self.ctx.close()
                self.ctx = None
            try:
                self._api(
                    "DELETE",
                    f"/api/plugins/rack-design/designs/{reload_design_id}/")
            except Exception:
                pass

    # =====================================================================
    # T1 (spec §4.1 "Cursor-governed placement", user repro 2026-07-08,
    # design 6 dra4-sl-isp29 F11 -> F08 rear): during a cross-rack drag the
    # user hovers FREE rows first (vendor placeholder parks there), then
    # moves the cursor UP over OCCUPIED rows (a full-depth device's rear
    # shadow) and RELEASES there. The vendor fallback committed the device
    # at the placeholder's last-valid slot; per the spec rule the release
    # must instead be a FULL snap-back home (the cursor's rows are illegal
    # at release), world byte-identical.
    # =====================================================================
    def test_cursor_release_over_occupied_rows_snaps_back(self):
        self._load_editor()
        self._create_rack_b_ghost()
        world_before = self.page.evaluate("() => window.__rdX.worldSnapshot()")

        # Land slot (the vendor fallback's last-valid): rack B REAR rows 0-3,
        # free. Cursor slot at release: row 14 -- inside the 3U full-depth
        # device's rear shadow rows (12-17), illegal.
        r = self.page.evaluate(
            f"() => window.__rdX.moveToWithCursorFallback("
            f"{json.dumps(self._subject_label)}, {self._rack_b}, 'rear', 0, 14)")
        self.assertTrue(r.get("ok"), f"moveToWithCursorFallback failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject tile lost after fallback release")
        self.assertEqual(
            info["rackId"], self._rack_a,
            f"release over ILLEGAL cursor rows must snap the tile back home "
            f"(never commit at the placeholder's last-valid slot): {info}")
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], self._subject_orig_gsy, info)
        self.assertIn("nbx-rd-state-existing", info["classes"], info)

        # Deny indicator must have been visible while the cursor hovered the
        # illegal rows (spec: deny indicator at the cursor rows).
        self.assertTrue(
            r.get("denyVisible"),
            "deny indicator was not rendered at the cursor's illegal rows "
            "mid-drag")

        self.assertEqual(
            self.errors, [],
            f"page/console errors during cursor-fallback release: {self.errors}")
        # Full snap-back: the world (subject INCLUDED) must be byte-identical.
        world_after = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur]) => window.__rdX.diffWorlds(prev, cur, null)",
            [world_before, world_after])
        self.assertEqual(
            world_violations, [],
            f"world changed across a rejected cursor-fallback release: "
            f"{world_violations}")
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(model_violations, [], model_violations)

    # =====================================================================
    # T2 (spec §4.1 pipeline + user ruling 2026-07-08): EVERY committed
    # cross-rack move must run the full dialog pipeline -- the §4a rename
    # dialog must actually OPEN for a legal cross-rack drop (the live bug's
    # fallback path committed without it; this asserts the dialog on the
    # normal path so any commit path that skips the pipeline fails loudly).
    # =====================================================================
    def test_committed_cross_rack_move_fires_rename_dialog(self):
        self._load_editor()
        self._create_rack_b_ghost()
        # The setup move above opened (and answered) its OWN rename dialog;
        # wait for its fading overlay to be fully removed so the assertion
        # below can only be satisfied by a FRESH dialog from the cross-rack
        # move itself.
        self.page.wait_for_function(
            "() => window.__rdX.countRenameDialogs() === 0", timeout=5000)

        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', 0)")
        self.assertTrue(r.get("ok"), f"moveTo failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        dialogs = self.page.evaluate("() => window.__rdX.countRenameDialogs()")
        self.assertGreaterEqual(
            dialogs, 1,
            "a committed cross-rack move MUST open the §4a rename dialog "
            "(no commit path may skip the dialog pipeline)")

        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)
        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject lost after dialog-confirmed move")
        self.assertEqual(info["rackId"], self._rack_b, info)
        self.assertIn("nbx-rd-state-move_in", info["classes"], info)
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(model_violations, [], model_violations)

    # =====================================================================
    # Issue #17 (spec §4.1 "cancel -> revert()"): the §4a rename dialog that
    # a committed cross-rack move opens must FULLY undo the move when
    # dismissed via Cancel -- the device goes back to its origin rack/face/
    # position, the whole world (every entity, not just the subject) is
    # byte-identical to the pre-gesture snapshot, and no dialog is left in
    # the DOM. Shared by both dismissal affordances (Cancel button, x close)
    # -- both are wired to the exact same finishCancel()+requestHide()
    # handler in showMoveNameDialog.
    # =====================================================================
    def _assert_rename_dialog_cancel_fully_reverts(self, dismiss_fn_name):
        self._load_editor()
        self._create_rack_b_ghost()
        # The setup move above opened (and answered) its OWN rename dialog;
        # wait for its fading overlay to be fully removed so this test's
        # dialog is unambiguously the one opened by ITS OWN gesture below.
        self.page.wait_for_function(
            "() => window.__rdX.countRenameDialogs() === 0", timeout=5000)

        world_before = self.page.evaluate("() => window.__rdX.worldSnapshot()")

        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', 0)")
        self.assertTrue(r.get("ok"), f"moveTo failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        dialogs = self.page.evaluate("() => window.__rdX.countRenameDialogs()")
        self.assertGreaterEqual(
            dialogs, 1,
            "a committed cross-rack move MUST open the §4a rename dialog "
            "before this test can exercise its cancel path")

        dismissed = self.page.evaluate(f"() => window.__rdX.{dismiss_fn_name}()")
        self.assertTrue(dismissed, f"could not find/click the {dismiss_fn_name} affordance")
        # Bootstrap's fade-out + our own hidden.bs.modal cleanup.
        self.page.wait_for_function(
            "() => window.__rdX.countRenameDialogs() === 0", timeout=5000)
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # No dialog left open anywhere in the DOM (Cancel/x must not strand
        # the modal on screen -- the transition-safe requestHide guard).
        open_modals = self.page.evaluate(
            "() => document.querySelectorAll('.modal.show').length")
        self.assertEqual(open_modals, 0, "a dialog was left open in the DOM after cancel")

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject lost after the cancelled move")
        self.assertEqual(info["rackId"], self._rack_a, f"device must revert to its origin rack: {info}")
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], self._subject_orig_gsy, f"device must revert to its origin position: {info}")
        self.assertIn("nbx-rd-state-existing", info["classes"], info)

        world_after = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur]) => window.__rdX.diffWorlds(prev, cur, null)",
            [world_before, world_after])
        self.assertEqual(
            world_violations, [],
            f"world must be byte-identical to the pre-gesture snapshot after "
            f"a cancelled rename dialog (full revert): {world_violations}")

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(model_violations, [], model_violations)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # =====================================================================
    # Ghost <-> body hover link, CROSS-RACK (user ruling 2026-07-10): a
    # committed cross-rack move leaves the ghost in rack A and the move_in
    # body in rack B -- hovering either must highlight the other across
    # blocks (both live in the same DOM; identity via data-rd-device-id).
    # =====================================================================
    def test_hover_links_ghost_and_body_cross_rack(self):
        self._load_editor()
        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', 0)")
        self.assertTrue(r.get("ok"), f"moveTo failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        link = self.page.evaluate(
            f"""() => {{
                const did = "{self._subject_device_id}";
                let body = null, ghost = null;
                document.querySelectorAll(
                    '.grid-stack-item[data-rd-device-id="' + did + '"]'
                ).forEach(el => {{
                    if (el.classList.contains('nbx-rd-state-move_out_ghost')) {{
                        ghost = el;
                    }} else if (!el.getAttribute('data-rd-derived-opp')) {{
                        body = el;
                    }}
                }});
                if (!body || !ghost) {{
                    return {{error: 'pair not found', body: !!body, ghost: !!ghost}};
                }}
                const bodyRack = body.closest('.nbx-rd-rack-block')
                    .getAttribute('data-rack-id');
                const ghostRack = ghost.closest('.nbx-rd-rack-block')
                    .getAttribute('data-rack-id');
                const out = {{bodyRack, ghostRack}};
                body.dispatchEvent(new PointerEvent('pointerover', {{bubbles: true}}));
                out.ghostLinked = ghost.classList.contains('nbx-rd-hover-linked');
                body.dispatchEvent(new PointerEvent('pointerout', {{bubbles: true}}));
                out.ghostCleared = !ghost.classList.contains('nbx-rd-hover-linked');
                ghost.dispatchEvent(new PointerEvent('pointerover', {{bubbles: true}}));
                out.bodyLinked = body.classList.contains('nbx-rd-hover-linked');
                ghost.dispatchEvent(new PointerEvent('pointerout', {{bubbles: true}}));
                out.bodyCleared = !body.classList.contains('nbx-rd-hover-linked');
                return out;
            }}""")
        self.assertNotIn("error", link, link)
        # The pair genuinely spans two racks (the cross-rack part of the rule).
        self.assertEqual(link["ghostRack"], str(self._rack_a), link)
        self.assertEqual(link["bodyRack"], str(self._rack_b), link)
        self.assertTrue(link["ghostLinked"], f"cross-rack ghost must highlight: {link}")
        self.assertTrue(link["ghostCleared"], link)
        self.assertTrue(link["bodyLinked"], f"cross-rack body must highlight: {link}")
        self.assertTrue(link["bodyCleared"], link)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    def test_rename_dialog_cancel_button_fully_reverts_cross_rack_move(self):
        self._assert_rename_dialog_cancel_fully_reverts("cancelRenameDialogViaCancelButton")

    def test_rename_dialog_close_x_fully_reverts_cross_rack_move(self):
        self._assert_rename_dialog_cancel_fully_reverts("cancelRenameDialogViaCloseButton")

    # =====================================================================
    # Issue #21 (spec §4.3, cross-rack context): a device dragged FROM rack A
    # dropped onto a VACATING slot (a move-out ghost) in rack B must run the
    # exact same displacement contract as the same-rack case already covered
    # by EditorDisplacementTestCase's E5/E6 -- validation passes first, the
    # displacement dialog fires, confirm collapses OLD to the red "was:"
    # stripe (+ mirror hatch, since both OLD and NEW are full-depth here) and
    # renders NEW as move_in, and model.clean() stays satisfied throughout.
    # =====================================================================
    def test_cross_rack_drop_onto_vacating_slot_displaces_confirm(self):
        self._load_editor()
        self._create_rack_b_full_ghost()
        self.page.wait_for_function(
            "() => window.__rdX.countRenameDialogs() === 0", timeout=5000)

        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', {self._bghostfull_orig_gsy})")
        self.assertTrue(r.get("ok"), f"moveTo failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        # (1) displacement dialog appears AFTER validation (the drop was not
        # rejected -- a ghost claim never blocks per §4.2).
        has_dialog = self.page.evaluate("() => window.__rdX.hasDisplaceDialog()")
        self.assertTrue(
            has_dialog,
            "a cross-rack drop onto a vacating slot must open the "
            "displacement confirm dialog")

        confirmed = self.page.evaluate("() => window.__rdX.confirmDisplaceDialog()")
        self.assertTrue(confirmed, "could not find/click the displace-confirm button")
        self.page.wait_for_timeout(300)  # bootstrap fade-out

        # (2) confirming the displacement chains into the §4a rename dialog
        # (every committed cross-rack move runs the full dialog pipeline) --
        # apply it with the default (keep-name) choice.
        rename_dialogs = self.page.evaluate("() => window.__rdX.countRenameDialogs()")
        self.assertGreaterEqual(
            rename_dialogs, 1,
            "the rename dialog must also fire for a displaced cross-rack adoption")
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        # (3) NEW renders as move_in at the slot; OLD collapses to the red
        # "was:" stripe (both bodies AND, since both are full-depth, their
        # mirror hatches on the opposite face).
        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject lost after the displaced adoption")
        self.assertEqual(info["rackId"], self._rack_b, info)
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], self._bghostfull_orig_gsy, info)
        self.assertIn("nbx-rd-state-move_in", info["classes"], info)
        self.assertNotIn("nbx-rd-displaced", info["classes"], info)

        ghosts = self.page.evaluate(
            f"() => window.__rdX.ghostInfo({json.dumps(self._bghostfull_label)})")
        self.assertEqual(len(ghosts), 1, ghosts)
        old_ghost = ghosts[0]
        self.assertTrue(old_ghost["displaced"], f"OLD's ghost must collapse: {old_ghost}")
        self.assertIn(
            self._bghostfull_label, old_ghost["stripeTitle"] or "",
            f"stripe hover must name OLD: {old_ghost}")

        mirror = self.page.evaluate(
            f"() => window.__rdX.ghostMirrorInfo({self._rack_b}, "
            f"{json.dumps(self._bghostfull_label)})")
        self.assertIsNotNone(mirror, "full-depth OLD's ghost mirror hatch must exist")
        self.assertTrue(
            mirror["displaced"],
            f"OLD's mirror hatch must ALSO collapse (both full-depth): {mirror}")

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(model_violations, [], model_violations)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # Issue #21 cancel variant: at the displacement dialog, CANCEL instead of
    # confirming -> full revert (spec §4.3.5: cancel -> revert()), NEW back
    # at its rack-A origin, OLD's ghost/mirror rendering untouched.
    def test_cross_rack_drop_onto_vacating_slot_displaces_cancel(self):
        self._load_editor()
        self._create_rack_b_full_ghost()
        self.page.wait_for_function(
            "() => window.__rdX.countRenameDialogs() === 0", timeout=5000)
        world_before = self.page.evaluate("() => window.__rdX.worldSnapshot()")

        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'front', {self._bghostfull_orig_gsy})")
        self.assertTrue(r.get("ok"), f"moveTo failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        has_dialog = self.page.evaluate("() => window.__rdX.hasDisplaceDialog()")
        self.assertTrue(has_dialog, "expected a displacement confirm dialog to appear")

        cancelled = self.page.evaluate("() => window.__rdX.cancelDisplaceDialog()")
        self.assertTrue(cancelled, "could not find/click the displace-cancel button")
        self.page.wait_for_timeout(300)
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        open_modals = self.page.evaluate(
            "() => document.querySelectorAll('.modal.show').length")
        self.assertEqual(open_modals, 0, "a dialog was left open in the DOM after cancel")

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject lost after the cancelled displacement")
        self.assertEqual(info["rackId"], self._rack_a, f"device must revert to its origin rack: {info}")
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], self._subject_orig_gsy, info)
        self.assertIn("nbx-rd-state-existing", info["classes"], info)

        ghosts = self.page.evaluate(
            f"() => window.__rdX.ghostInfo({json.dumps(self._bghostfull_label)})")
        self.assertEqual(len(ghosts), 1, ghosts)
        self.assertFalse(
            ghosts[0]["displaced"],
            f"a cancelled displacement must leave OLD's ghost rendering untouched: {ghosts}")

        mirror = self.page.evaluate(
            f"() => window.__rdX.ghostMirrorInfo({self._rack_b}, "
            f"{json.dumps(self._bghostfull_label)})")
        self.assertIsNotNone(mirror)
        self.assertFalse(
            mirror["displaced"],
            f"OLD's mirror hatch must also remain untouched: {mirror}")

        world_after = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur]) => window.__rdX.diffWorlds(prev, cur, null)",
            [world_before, world_after])
        self.assertEqual(
            world_violations, [],
            f"world must be byte-identical to the pre-gesture snapshot after "
            f"a cancelled cross-rack displacement (full revert): {world_violations}")

        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(model_violations, [], model_violations)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # =====================================================================
    # P1 (spec §4.1 cursor-governed placement, PALETTE context, ruling
    # 2026-07-08): a palette drag-in whose CURSOR is over occupied rows at
    # release must create NO add at all -- the drag-in is discarded cleanly
    # (no widget, no dirty residue, world byte-identical), with the red deny
    # indicator having been shown at the cursor rows mid-drag. Currently
    # the engine's gray placeholder parks at a fallback slot and the drop
    # commits an add there.
    # =====================================================================
    def test_palette_release_over_occupied_rows_creates_no_add(self):
        self._load_editor()
        world_before = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        save_before = self.page.evaluate("() => window.__rdX.saveDisabled()")

        # Cursor at rack B REAR row 14 -- inside the 3U full-depth device's
        # rear shadow rows (12-17), illegal for any body. Engine fallback
        # registers the clone at rear row 0 (free).
        r = self.page.evaluate(
            f"() => window.__rdX.dropPaletteItemAtWithCursor({self._rack_b}, "
            f"'{self._dt_half_id}', 1, 'e2e-pal-deny', false, 'rear', 0, 14)")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)

        snap = self.page.evaluate("() => window.__rdX.snapshotAll()")
        residue = [t for t in snap if t["label"] == "e2e-pal-deny"]
        self.assertEqual(
            residue, [],
            "a palette release over ILLEGAL cursor rows must create NO add "
            f"at all -- found: {residue}")
        self.assertTrue(
            r.get("denyVisible"),
            "deny indicator was not rendered at the cursor's illegal rows "
            "during the palette drag")

        # No dirty residue: a discarded drag-in must not arm Save.
        save_after = self.page.evaluate("() => window.__rdX.saveDisabled()")
        self.assertEqual(
            save_after, save_before,
            "a discarded palette drag-in must leave the Save button state "
            f"untouched (before={save_before}, after={save_after})")

        world_after = self.page.evaluate("() => window.__rdX.worldSnapshot()")
        world_violations = self.page.evaluate(
            "([prev, cur]) => window.__rdX.diffWorlds(prev, cur, null)",
            [world_before, world_after])
        self.assertEqual(
            world_violations, [],
            f"world changed across a discarded palette drag-in: {world_violations}")
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(model_violations, [], model_violations)
        self.assertEqual(
            self.errors, [],
            f"page/console errors during the discarded palette drag: {self.errors}")

    # =====================================================================
    # P3 (spec §4.1 palette context x §4.3/§4.8): a cursor-armed palette
    # drag released ON a vacated (ghost) slot shows NO deny (a ghost never
    # blocks) and runs the displacement dialog -> confirm -> add committed
    # with `add` styling, ghost collapsed to the stripe.
    # =====================================================================
    def test_palette_cursor_release_on_vacated_slot_fires_displacement(self):
        self._load_editor()
        self._create_rack_b_ghost()

        # The bghost temp ghost marks rows 22-23 (front, rack B). Cursor AND
        # land position both there -- a normal on-target release.
        r = self.page.evaluate(
            f"() => window.__rdX.dropPaletteItemAtWithCursor({self._rack_b}, "
            f"'{self._dt_half_id}', 1, 'e2e-pal-disp', false, 'front', "
            f"{self._bghost_orig_gsy}, {self._bghost_orig_gsy})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.assertFalse(
            r.get("denyVisible"),
            "a vacated (ghost) slot never blocks -- the deny indicator must "
            "NOT show over it")
        answered = self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)
        self.assertGreaterEqual(
            answered.get("displaced", 0), 1,
            f"the displacement dialog must fire for a palette add landing "
            f"on a vacated slot: {answered}")

        info = self.page.evaluate(
            "() => window.__rdX.subjectInfo('e2e-pal-disp')")
        self.assertIsNotNone(info, "the palette add did not register")
        self.assertEqual(info["rackId"], self._rack_b, info)
        self.assertEqual(info["y"], self._bghost_orig_gsy, info)
        self.assertIn("nbx-rd-state-add", info["classes"], info)
        self.assertEqual(
            self.errors, [],
            f"page/console errors during the palette displacement: {self.errors}")

    # =====================================================================
    # Live bug (2026-07-10, Petr: "dropped on 23, landed on 22"): a palette
    # add whose CURSOR is over the LOWER half of a whole unit must still land
    # ON that unit -- not one unit lower. A unit spans two 0.5U rows and a
    # pointer at the unit's visual centre floors to its lower row; with the
    # palette gesture's grabRows==0 the raw 0.5U row was used as the tile top,
    # so a centre/lower-half release fell a unit low. Cursor governance must
    # snap a whole-U add to the U-grid: pointing anywhere inside a unit lands
    # the device ON it.
    # =====================================================================
    def test_palette_cursor_lower_half_lands_on_that_unit(self):
        self._load_editor()
        # U12 front = rows 8-9 (free); the unit below, U11 = rows 10-11 (free).
        target_top = self._u_to_gsy(12, 2)    # 8 -- U12's top 0.5U row
        cursor_lower_half = target_top + 1    # 9 -- U12's LOWER 0.5U row (its centre)
        # The drag HELPER's grab offset makes the engine park the clone a 0.5U
        # row off (here ON the cursor row, an ODD row -> a HALF-unit slot), and
        # because the cursor is INSIDE that engine span the old code trusted it
        # (`inSpan`) and committed the half-unit landing. Force exactly that.
        engine_low = cursor_lower_half        # 9 -- odd helper park, cursor in-span
        r = self.page.evaluate(
            f"() => window.__rdX.dropPaletteItemAtWithCursor({self._rack_b}, "
            f"'{self._dt_half_id}', 1, 'e2e-pal-align', false, 'front', "
            f"{engine_low}, {cursor_lower_half})")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.assertFalse(
            r.get("denyVisible"),
            "an empty in-rack unit must not raise the deny indicator")
        info = self.page.evaluate(
            "() => window.__rdX.subjectInfo('e2e-pal-align')")
        self.assertIsNotNone(info, "the palette add did not register")
        self.assertEqual(info["rackId"], self._rack_b, info)
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(
            info["y"], target_top,
            f"a palette add whose cursor is over U12 (rows {target_top}-"
            f"{target_top + 1}) must land ON U12 (gs-y {target_top}), not a unit "
            f"lower; got gs-y {info['y']}")
        self.assertIn("nbx-rd-state-add", info["classes"], info)
        model_violations = self.page.evaluate(
            "() => (window.__rdModel ? window.__rdModel.check() : "
            "['window.__rdModel missing'])")
        self.assertEqual(model_violations, [], model_violations)
        self.assertEqual(self.errors, [], f"console errors: {self.errors}")

    # =====================================================================
    # Targeted regression (the live bug, 2026-07-08): a foreign (cross-rack)
    # device dropped onto the rear rows held by a full-depth device's SHADOW
    # is rejected -- and after the rejection EVERYTHING must be exactly as
    # before the gesture: the foreign tile back home, the shadow's class
    # matching its owner (existing, never a stale move_in tint), no tile
    # moved, model clean.
    # =====================================================================
    def test_foreign_drop_onto_shadow_rejected_restores_all(self):
        self._load_editor()
        self._create_rack_b_ghost()
        base = self._baseline(self._subject_label)
        violations = []
        # Seed the full-world baseline BEFORE the gesture: a rejected drop is
        # a full revert, so nothing -- subject included -- may differ after.
        self._prevWorld = self.page.evaluate("() => window.__rdX.worldSnapshot()")

        # Drop the rack-A subject onto rack B's REAR rows where the 3U
        # full-depth device's shadow sits (rows 12-17). Blocked by the
        # shadow claim -> must be rejected and fully reverted.
        r = self.page.evaluate(
            f"() => window.__rdX.moveTo({json.dumps(self._subject_label)}, "
            f"{self._rack_b}, 'rear', {self._bfull_gsy})")
        self.assertTrue(r.get("ok"), f"moveTo failed: {r}")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)

        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(self._subject_label)})")
        self.assertIsNotNone(info, "subject tile lost after rejected drop")
        self.assertEqual(info["rackId"], self._rack_a, info)
        self.assertEqual(info["face"], "front", info)
        self.assertEqual(info["y"], self._subject_orig_gsy, info)
        self.assertIn("nbx-rd-state-existing", info["classes"], info)

        self._check_step(
            self._subject_label, base, "foreign-drop-on-shadow", violations,
            world_exempt_subject=False)
        self._assert_clean(violations, 1, "targeted foreign-drop regression")

    # =====================================================================
    # Full cross-rack sweep: EXISTING full-depth subject, both racks, both
    # faces, alternating front/rear, every 0.5U row.
    # =====================================================================
    def test_sweep_existing_fulldepth_across_racks(self):
        self._load_editor()
        self._create_rack_b_ghost()
        self._base = self._baseline(self._subject_label)
        violations = []
        steps = self._sweep(
            self._subject_label, 4, [self._rack_a, self._rack_b, self._rack_a],
            violations, home_rack_id=self._rack_a, home_face="front",
            home_orig_gsy=self._subject_orig_gsy)
        self._assert_clean(violations, steps, "existing full-depth subject")

    # =====================================================================
    # Full sweep: NEW palette-added 1U half-depth subject. Swept across both
    # faces of rack A only: the editor's acceptWidgets policy deliberately
    # never adopts an unsaved add into another rack (isForeignRealTile
    # requires existing/move_in), so rack A is the add's whole world.
    # =====================================================================
    def test_sweep_palette_add_across_faces(self):
        self._load_editor()
        self._create_rack_b_ghost()
        add_label = "e2e-xrack-addsub"
        # Drop the add at a known-free rack-A front slot (U12 -> rows 8-9...
        # for a 1U tile gsY = 32-24 = 8; subject body sits at rows 18-21).
        self.page.evaluate(
            f"() => window.__rdX.dropPaletteItemAt({self._rack_a}, "
            f"'{self._dt_half_id}', 1, {json.dumps(add_label)}, false, "
            f"'front', 8)")
        self.page.wait_for_timeout(STEP_SETTLE_MS)
        self.page.evaluate("() => window.__rdX.answerDialogs()")
        self.page.wait_for_timeout(STEP_SETTLE_MS * 3)
        info = self.page.evaluate(
            f"() => window.__rdX.subjectInfo({json.dumps(add_label)})")
        self.assertIsNotNone(info, "palette add did not register")

        self._base = self._baseline(add_label)
        violations = []
        steps = self._sweep(add_label, 2, [self._rack_a], violations)
        self._assert_clean(violations, steps, "palette-add subject")


if __name__ == "__main__":
    unittest.main(verbosity=2)
