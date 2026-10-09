// Execution plan tab (PLAN-execution-steps.md Sec. 6).
//
// A plain ES module, loaded standalone by design_execution_plan.html. It reads
// the bootstrap JSON (#rd-plan-data), draws an "Unscheduled" tray and ordered
// step cards, simulates through POST .../simulate-steps/ and persists through
// POST .../save-steps/. Drag and drop is plain HTML5 DnD (NetBox ships no
// SortableJS). The power bar and bank chips reuse the editor's own CSS classes
// (editor.css: nbx-rd-power-*, nbx-rd-dist-*); nothing is restyled here.

const DEBOUNCE_MS = 400;
const KIND_GLYPH = { add: "+", remove: "−", move: "→" };

const root = document.querySelector("[data-rd-plan-root]");
const dataEl = document.getElementById("rd-plan-data");

if (root && dataEl) {
    init(JSON.parse(dataEl.textContent));
}

function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
}

// NetBox exposes the token as window.CSRF_TOKEN; the cookie is the fallback.
function csrfToken() {
    if (window.CSRF_TOKEN) { return window.CSRF_TOKEN; }
    const m = document.cookie.match(/(?:^|; )csrftoken=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : "";
}

function init(boot) {
    const actions = new Map(boot.actions.map((a) => [a.id, a]));
    const rackNames = boot.rackNames || {};
    const canEdit = !!boot.canEdit;
    let uidSeq = 0;
    const newStep = (src) => ({
        uid: ++uidSeq,
        id: src ? src.id : null,
        title: src ? src.title : "",
        actions: [],
        cells: {},   // rack key -> simulate result
        stale: {},   // rack key -> true while the cell is outdated
    });

    const steps = boot.steps.map(newStep);
    const byId = new Map(steps.map((s) => [s.id, s]));
    const tray = [];
    for (const a of boot.actions) {
        const s = a.step ? byId.get(a.step) : null;
        (s ? s.actions : tray).push(a.id);
    }

    // ---- simulate / save plumbing --------------------------------------

    const spinner = document.querySelector("[data-rd-plan-spinner]");
    const errorBox = document.querySelector("[data-rd-plan-error]");
    let inflight = null;      // AbortController of the running simulate
    let timer = null;
    let pending = null;       // {from, to, racks:Set} accumulated since the last good response
    let saveChain = Promise.resolve();
    let busy = 0;

    function setBusy(delta) {
        busy += delta;
        spinner.classList.toggle("d-none", busy <= 0 && !timer);
        root.dataset.rdPlanBusy = busy > 0 || timer ? "1" : "0";
    }

    function showError(text) {
        errorBox.textContent = text || "";
        errorBox.classList.toggle("d-none", !text);
    }

    async function api(url, body, signal) {
        const resp = await fetch(url, {
            method: "POST",
            credentials: "same-origin",
            signal,
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": csrfToken(),
                Accept: "application/json",
            },
            body: JSON.stringify(body),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            const detail = data.detail || JSON.stringify(data);
            throw new Error(resp.status + ": " + detail);
        }
        return data;
    }

    const order = () => steps.map((s) => s.actions.slice());

    // Merge a simulate response into the cells. ``scope`` is the step range and
    // rack set that were asked for; stale flags inside it are cleared.
    function applyResult(result, scope) {
        for (const entry of result.steps) {
            const step = steps[entry.index - 1];
            if (!step) { continue; }
            for (const [key, res] of Object.entries(entry.racks)) {
                step.cells[key] = res;
                delete step.stale[key];
            }
        }
        if (scope) {
            for (let i = scope.from; i <= scope.to; i++) {
                const step = steps[i - 1];
                if (!step) { continue; }
                const touched = stepRacks(step);
                for (const key of Object.keys(step.stale)) {
                    if (scope.racks === null || scope.racks.has(key)) {
                        if (!touched.includes(key)) { delete step.stale[key]; delete step.cells[key]; }
                    }
                }
            }
        }
    }

    async function simulate(scope) {
        if (!steps.length) { return; }
        if (inflight) { inflight.abort(); }
        const ctl = new AbortController();
        inflight = ctl;
        setBusy(1);
        try {
            const body = { steps: order() };
            if (scope) {
                body.from_step = scope.from;
                body.to_step = scope.to;
                if (scope.racks) { body.racks = [...scope.racks]; }
            }
            const result = await api(boot.urls.simulate, body, ctl.signal);
            if (ctl.signal.aborted) { return false; }
            applyResult(result, scope);
            showError("");
            render();
            return true;
        } catch (err) {
            if (err.name === "AbortError") { return false; }
            showError("Simulation failed: " + err.message);
            return false;
        } finally {
            if (inflight === ctl) { inflight = null; }
            setBusy(-1);
        }
    }

    // ---- save status indicator -------------------------------------------
    // EVERY edit funnels through save(); the indicator reflects the chain:
    // "Saving…" while any request is in flight, "Saved HH:MM" after the last
    // one succeeded, "Not saved — Retry" when it failed.

    const statusEl = document.querySelector("[data-rd-plan-save-status]");
    const hhmm = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });

    function setStatus(state, when) {
        if (!statusEl) { return; }
        statusEl.classList.toggle("text-danger", state === "error");
        statusEl.classList.toggle("text-muted", state !== "error");
        statusEl.dataset.rdPlanSaveState = state;
        if (state === "saving") {
            statusEl.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" '
                + 'aria-hidden="true"></span>Saving\u2026';
        } else if (state === "saved") {
            statusEl.textContent = "Saved " + hhmm(when || new Date());
        } else if (state === "error") {
            statusEl.innerHTML = 'Not saved \u2014 <a href="#" class="text-danger fw-bold" '
                + 'data-rd-plan-save-retry>Retry</a>';
        } else {
            statusEl.textContent = "";
        }
    }

    let savesQueued = 0;
    let lastSaveFailed = false;

    function save() {
        if (!canEdit) { return saveChain; }
        savesQueued += 1;
        setStatus("saving");
        saveChain = saveChain.then(async () => {
            setBusy(1);
            try {
                const result = await api(boot.urls.save, {
                    steps: steps.map((s) => ({ id: s.id, title: s.title, placements: s.actions })),
                });
                result.steps.forEach((saved, i) => { if (steps[i]) { steps[i].id = saved.id; } });
                lastSaveFailed = false;
            } catch (err) {
                lastSaveFailed = true;
            } finally {
                savesQueued -= 1;
                if (savesQueued === 0) {
                    setStatus(lastSaveFailed ? "error" : "saved", new Date());
                }
                setBusy(-1);
            }
        });
        return saveChain;
    }

    if (statusEl) {
        statusEl.addEventListener("click", (ev) => {
            if (!ev.target.closest("[data-rd-plan-save-retry]")) { return; }
            ev.preventDefault();
            save();
        });
        const stamps = boot.steps.map((s) => Date.parse(s.lastUpdated)).filter((t) => !isNaN(t));
        if (stamps.length) { setStatus("saved", new Date(Math.max(...stamps))); }
    }

    // After a drop: grey the cells that can change, then debounce one request.
    function markDirty(from, to, rackKeys) {
        const lo = Math.max(1, Math.min(from, to));
        const hi = Math.min(steps.length, Math.max(from, to));
        for (let i = lo; i <= hi; i++) {
            for (const key of rackKeys) { steps[i - 1].stale[key] = true; }
        }
        if (!pending) { pending = { from: lo, to: hi, racks: new Set() }; }
        pending.from = Math.min(pending.from, lo);
        pending.to = Math.max(pending.to, hi);
        rackKeys.forEach((k) => pending.racks.add(k));
        if (inflight) { inflight.abort(); inflight = null; }
        clearTimeout(timer);
        timer = setTimeout(flush, DEBOUNCE_MS);
        setBusy(0);
        render();
    }

    async function flush() {
        timer = null;
        const scope = pending;
        pending = null;
        if (scope && steps.length) {
            const ok = await simulate(scope);
            if (ok === false) {
                // Aborted by a newer drag (or failed): keep what this request
                // covered so the next one still includes it.
                if (!pending) { pending = scope; }
                else {
                    pending.from = Math.min(pending.from, scope.from);
                    pending.to = Math.max(pending.to, scope.to);
                    scope.racks.forEach((k) => pending.racks.add(k));
                }
            }
        }
        if (!timer) { await save(); }
        setBusy(0);
    }

    // ---- model helpers --------------------------------------------------

    function stepRacks(step) {
        const keys = new Set();
        for (const id of step.actions) { actions.get(id).racks.forEach((k) => keys.add(k)); }
        return [...keys].sort((a, b) => (rackNames[a] || a).localeCompare(rackNames[b] || b));
    }

    const locate = (id) => {
        const si = steps.findIndex((s) => s.actions.includes(id));
        return si; // -1 = tray
    };

    const VERBS = { remove: "Remove", move: "Move", add: "Add" };
    const stepTitleFor = (id) => {
        const a = actions.get(id);
        return ((VERBS[a.kind] || "") + " " + (a.name || "")).trim().slice(0, 100);
    };

    // Drop an action on a step / the tray (``target``), or into the gap at
    // position ``gapAt`` (a NEW step is created there). A step emptied by the
    // drag disappears -- except when the action went back to the tray.
    function moveAction(id, target, beforeId, gapAt) {
        const srcStep = locate(id) >= 0 ? steps[locate(id)] : null;
        let dest = null;
        if (gapAt != null) {
            dest = newStep(null);
            dest.title = stepTitleFor(id);
            steps.splice(gapAt, 0, dest);
        } else if (target !== "tray") {
            dest = steps.find((s) => s.uid === Number(target));
        }
        const list = dest ? dest.actions : tray;
        const src = srcStep ? srcStep.actions : tray;
        src.splice(src.indexOf(id), 1);
        let at = beforeId != null ? list.indexOf(beforeId) : -1;
        if (at < 0) { at = list.length; }
        list.splice(at, 0, id);
        if (srcStep === dest && gapAt == null) {  // reorder inside one container
            render();
            save();
            return;
        }
        let lo = dest ? steps.indexOf(dest) + 1 : steps.length;
        if (srcStep) { lo = Math.min(lo, steps.indexOf(srcStep) + 1); }
        if (dest && srcStep && !srcStep.actions.length) {
            steps.splice(steps.indexOf(srcStep), 1);
        }
        markDirty(Math.min(lo, steps.length), steps.length, actions.get(id).racks);
    }

    // Move a whole step so it lands at gap ``gapAt`` (0..n, counted before the move).
    function moveStep(uid, gapAt) {
        const from = steps.findIndex((s) => s.uid === uid);
        if (from < 0) { return; }
        gapAt = Math.max(0, Math.min(steps.length, gapAt));
        const to = gapAt > from ? gapAt - 1 : gapAt;
        if (to === from) { return; }
        const [step] = steps.splice(from, 1);
        steps.splice(to, 0, step);
        const lo = Math.min(from, to);
        const hi = Math.max(from, to);
        const racks = new Set();
        for (let i = lo; i <= hi; i++) { stepRacks(steps[i]).forEach((k) => racks.add(k)); }
        pendingFocus = uid;
        markDirty(lo + 1, hi + 1, racks);
        save();
    }

    // ---- default order, auto-order -------------------------------------

    function allActions() {
        const ids = tray.slice();
        steps.forEach((s) => { ids.push(...s.actions); });
        return ids;
    }

    // Create plan's default (E6): one step per action, in the order the
    // placements were created in the editor ("Move srv-105").
    function layoutDefault() {
        const ids = allActions().sort((a, b) => (actions.get(a).seq || 0) - (actions.get(b).seq || 0)
            || a - b);
        steps.length = 0;
        tray.length = 0;
        for (const id of ids) {
            const s = newStep(null);
            s.actions = [id];
            s.title = stepTitleFor(id);
            steps.push(s);
        }
    }

    // Auto-order: the SERVER proposes the order by simulating it
    // (POST .../auto-order/); every step stays a unit and keeps its title, a step
    // that stays red is split into single-action steps, unscheduled actions join
    // as steps of their own. The result goes through save() and is re-simulated.
    let autoRunning = false;

    async function autoOrder() {
        if (autoRunning) { return; }
        autoRunning = true;
        const btn = document.querySelector("[data-rd-plan-auto]");
        const label = btn ? btn.innerHTML : "";
        if (btn) {
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status" '
                + 'aria-hidden="true"></span>Auto-order';
        }
        setBusy(1);
        try {
            let result;
            try {
                result = await api(boot.urls.autoOrder, { steps: order() });
            } catch (err) {
                showError("Auto-order failed: " + err.message);
                return;
            }
            showError("");
            if (inflight) { inflight.abort(); inflight = null; }
            clearTimeout(timer);
            timer = null;
            pending = null;
            const owner = new Map();
            for (const s of steps) { for (const id of s.actions) { owner.set(id, s); } }
            const next = result.steps.map((ids) => {
                const src = owner.get(ids[0]);
                const same = src && src.actions.length === ids.length
                    && ids.every((id) => owner.get(id) === src);
                const s = newStep(same ? src : null);
                s.title = src ? src.title : stepTitleFor(ids[0]);
                s.actions = ids.slice();
                return s;
            });
            tray.length = 0;
            steps.splice(0, steps.length, ...next);
            render();
            await save();
            await simulate(null);
        } finally {
            setBusy(-1);
            autoRunning = false;
            if (btn) { btn.innerHTML = label; }
            render();
        }
    }

    async function createPlan() {
        layoutDefault();
        render();
        await save();
        await simulate(null);
    }

    // Reset plan: back to what Create plan builds, after a confirmation.
    async function resetPlan() {
        if (!window.confirm("Reset the plan to the order the actions were created in the editor? "
            + "Your step order and titles will be lost.")) { return; }
        if (inflight) { inflight.abort(); inflight = null; }
        clearTimeout(timer);
        timer = null;
        pending = null;
        layoutDefault();
        render();
        await save();
        await simulate(null);
    }

    // ---- rendering ------------------------------------------------------

    // The label sits UNDER the bar, in the body colour: on the bar itself it
    // straddled the fill edge and was unreadable on one of the two halves.
    function powerBar(power) {
        const util = Math.round(power.util_pct || 0);
        const pct = Math.max(0, Math.min(100, util));
        const text = Math.round(power.draw_w) + " / " + Math.round(power.capacity_w) + " W · " + util + "%";
        return '<div class="rd-plan-power" title="' + esc(text) + '">'
            + '<div class="nbx-rd-power-bar nbx-rd-power-' + esc(power.state || "ok") + ' rd-plan-power-bar">'
            + '<div class="nbx-rd-power-fill" style="width:' + pct + '%"></div></div>'
            + '<div class="rd-plan-power-label">' + esc(text) + "</div></div>";
    }

    // Bank chips grouped by PDU and feed leg, as power_heatmap.js renderDistLegend.
    function bankChips(dist) {
        if (!dist || !dist.pdus) { return ""; }
        const byLeg = {};
        const legs = [];
        Object.keys(dist.pdus).forEach((pduName) => {
            const pdu = dist.pdus[pduName];
            const key = pdu.feed_letter || ("pdu:" + pduName);
            if (!byLeg[key]) { byLeg[key] = []; legs.push(key); }
            const chips = Object.keys(pdu.banks).sort().map((bankId) => {
                const bank = pdu.banks[bankId];
                const load = (bank.allocated_power || 0) + (bank.planned_power || 0);
                const w = Math.max(0, Math.min(100, bank.util_pct || 0));
                return '<span class="nbx-rd-dist-chip nbx-rd-dist-' + esc(bank.state || "ok") + '">'
                    + '<span class="nbx-rd-dist-fill" style="width:' + w.toFixed(1) + '%"></span>'
                    + '<span class="nbx-rd-dist-label">B' + esc(bankId) + ": " + Math.round(load) + "/"
                    + Math.round(bank.max_power || 0) + " W</span></span>";
            }).join("");
            const head = esc(pdu.feed_name || pduName) + ((pdu.phase || 1) === 3 ? " 3φ" : "");
            byLeg[key].push('<div class="nbx-rd-dist-pdu"><span class="nbx-rd-dist-pdu-head '
                + 'nbx-rd-feedhead-' + esc(pdu.feed_letter || "") + '">' + head + "</span>" + chips + "</div>");
        });
        if (!legs.length) { return ""; }
        return '<div class="nbx-rd-dist-legend">' + legs.map(
            (k) => '<div class="nbx-rd-dist-leg">' + byLeg[k].join("") + "</div>").join("") + "</div>";
    }

    function badges(problems) {
        if (!problems.length) {
            return '<span class="badge bg-green-lt" data-rd-plan-ok>OK</span>';
        }
        const seen = new Set();
        return problems.map((p) => {
            const line = p.code + "\u0000" + (p.detail || "");
            if (seen.has(line)) { return ""; }
            seen.add(line);
            const cls = p.severity === "error" ? "bg-red text-white rd-plan-badge-error"
                : "bg-yellow text-dark rd-plan-badge-warning";
            return '<div class="rd-plan-problem"><span class="badge ' + cls + '" data-code="' + esc(p.code)
                + '" title="' + esc(p.detail) + '">' + esc(p.code) + "</span>"
                + (p.detail ? '<span class="rd-plan-detail" data-rd-detail>' + esc(p.detail) + "</span>" : "")
                + "</div>";
        }).join("");
    }

    function rackCell(step, key) {
        const res = step.cells[key];
        const stale = step.stale[key] || !res;
        let body;
        if (!res) {
            body = '<div class="text-muted small">…</div>';
        } else {
            body = powerBar(res.power) + bankChips(res.distribution)
                + '<div class="rd-plan-badges">' + badges(res.problems) + "</div>";
        }
        return '<div class="rd-plan-rack' + (stale ? " rd-plan-outdated" : "") + '" data-rd-rack="'
            + esc(key) + '"><div class="rd-plan-rack-name">' + esc(rackNames[key] || key) + "</div>"
            + body + "</div>";
    }

    function where(w) {
        return w ? esc(w.name) + (w.u ? " U" + esc(w.u) : "") : "";
    }

    function actionRow(id) {
        const a = actions.get(id);
        const route = a.kind === "add" ? where(a.to)
            : a.kind === "remove" ? where(a.from)
                : where(a.from) + " → " + where(a.to);
        const draggable = canEdit;
        return '<div class="rd-plan-action" data-rd-action="'
            + a.id + '" data-kind="' + esc(a.kind) + '"' + (draggable ? ' draggable="true"' : "") + ">"
            + '<span class="rd-plan-kind rd-plan-kind-' + esc(a.kind) + '">' + (KIND_GLYPH[a.kind] || "?")
            + "</span><span>" + esc(a.name) + "</span>"
            + '<span class="rd-plan-where">' + route + "</span></div>";
    }

    // One pill per step: the worst severity over its racks.
    function stepPill(step) {
        const all = Object.values(step.cells).flatMap((c) => c.problems || []);
        if (!Object.keys(step.cells).length) { return ""; }
        const errors = all.filter((p) => p.severity === "error");
        if (errors.length) {
            const codes = errors.map((p) => p.code);
            const text = codes.some((c) => c === "u_conflict" || c === "bay_conflict") ? "Conflict"
                : codes.some((c) => c === "rack_over" || c === "bank_over" || c === "pdu_over") ? "Overload"
                    : codes[0];
            return '<span class="badge bg-red text-white ms-2" data-rd-step-status="error">'
                + esc(text) + "</span>";
        }
        if (all.length) {
            return '<span class="badge bg-yellow text-dark ms-2" data-rd-step-status="warning">Warning</span>';
        }
        return '<span class="badge bg-green-lt ms-2" data-rd-step-status="ok">OK</span>';
    }

    function stepCard(step, i) {
        const cells = stepRacks(step).map((k) => rackCell(step, k)).join("");
        const title = !canEdit
            ? '<span class="fw-bold">' + esc(step.title) + "</span>"
            : '<input type="text" class="form-control form-control-sm rd-plan-title" maxlength="100"'
              + ' placeholder="Window title" data-rd-title value="' + esc(step.title) + '">';
        const del = canEdit && !step.actions.length
            ? '<button type="button" class="btn btn-sm btn-ghost-danger ms-auto" data-rd-del-step '
              + 'title="Delete the empty step"><i class="mdi mdi-close"></i></button>' : "";
        const handle = canEdit
            ? '<span class="rd-plan-handle" data-rd-step-handle draggable="true" tabindex="0" role="button"'
              + ' title="Drag to reorder (or press Arrow up / Arrow down)"'
              + ' aria-label="Move step ' + (i + 1) + '"><i class="mdi mdi-drag" aria-hidden="true"></i></span>'
            : "";
        return '<div class="card rd-plan-step" data-rd-step data-step-index="' + (i + 1)
            + '" data-step-uid="' + step.uid + '">'
            + '<div class="card-header d-flex align-items-center gap-2">' + handle
            + '<span class="fw-bold">Step ' + (i + 1)
            + "</span>" + title + stepPill(step) + del + "</div>"
            + '<div class="rd-plan-body"><div class="rd-plan-drop" data-rd-drop="' + step.uid + '">'
            + step.actions.map((id) => actionRow(id)).join("")
            + (step.actions.length ? "" : '<div class="text-muted small p-2">Drop actions here</div>')
            + "</div>"
            + (cells ? '<div class="rd-plan-racks">' + cells + "</div>" : "")
            + "</div></div>";
    }

    let pendingFocus = null;   // step uid whose handle regains focus after a render

    function render() {
        const wasFocused = document.activeElement && document.activeElement.matches
            && document.activeElement.matches("[data-rd-title]");
        if (wasFocused) { return; }   // never destroy the input being typed in
        let html = "";
        if (!steps.length) {
            html += '<div class="text-center py-4" data-rd-empty>'
                + (boot.actions.length
                    ? "<p class=\"text-muted\">This design has no execution plan yet.</p>"
                      + (canEdit ? '<button type="button" class="btn btn-primary" data-rd-create>'
                          + "Create plan</button>" : "")
                    : '<p class="text-muted">This design has no actions to schedule.</p>')
                + "</div>";
        } else {
            html += '<div class="card rd-plan-tray" data-rd-tray><div class="card-header">'
                + '<span class="fw-bold">Unscheduled</span><span class="badge bg-secondary-lt ms-2">'
                + tray.length + '</span></div><div class="rd-plan-drop" data-rd-drop="tray">'
                + tray.map((id) => actionRow(id)).join("")
                + (tray.length ? "" : '<div class="text-muted small p-2">Nothing unscheduled</div>')
                + "</div></div>";
            const gap = (k) => (canEdit ? '<div class="rd-plan-gap" data-rd-gap="' + k + '"></div>' : "");
            html += steps.map((st, i) => gap(i) + stepCard(st, i)).join("") + gap(steps.length);
        }
        root.innerHTML = html;
        if (pendingFocus != null) {
            const h = root.querySelector('[data-step-uid="' + pendingFocus + '"] [data-rd-step-handle]');
            pendingFocus = null;
            if (h) { h.focus(); }
        }
        const addBtn = document.querySelector("[data-rd-plan-add-step]");
        const autoBtn = document.querySelector("[data-rd-plan-auto]");
        const resetBtn = document.querySelector("[data-rd-plan-reset]");
        if (resetBtn) { resetBtn.classList.toggle("d-none", !steps.length); }
        if (addBtn) { addBtn.disabled = !steps.length; }
        if (autoBtn) { autoBtn.disabled = autoRunning || !boot.actions.length; }
    }

    // ---- events ----------------------------------------------------------

    let dragId = null;      // action being dragged
    let dragStep = null;    // step (uid) being dragged by its handle

    function clearDrag() {
        dragId = null;
        dragStep = null;
        root.classList.remove("rd-plan-dragging-action", "rd-plan-dragging-step");
        root.querySelectorAll(".rd-plan-dragging, .rd-plan-over").forEach(
            (el) => el.classList.remove("rd-plan-dragging", "rd-plan-over"));
    }

    root.addEventListener("dragstart", (ev) => {
        const handle = ev.target.closest && ev.target.closest("[data-rd-step-handle]");
        if (handle) {
            const card = handle.closest("[data-rd-step]");
            dragStep = Number(card.dataset.stepUid);
            card.classList.add("rd-plan-dragging");
            root.classList.add("rd-plan-dragging-step");
            ev.dataTransfer.effectAllowed = "move";
            ev.dataTransfer.setData("text/plain", "step:" + dragStep);
            if (ev.dataTransfer.setDragImage) { ev.dataTransfer.setDragImage(card, 16, 16); }
            return;
        }
        const row = ev.target.closest && ev.target.closest("[data-rd-action]");
        if (!row) { return; }
        dragId = Number(row.dataset.rdAction);
        row.classList.add("rd-plan-dragging");
        root.classList.add("rd-plan-dragging-action");
        ev.dataTransfer.effectAllowed = "move";
        ev.dataTransfer.setData("text/plain", String(dragId));
    });
    root.addEventListener("dragend", clearDrag);

    // The element a drag may land on: a gap (both kinds), a step's action list
    // (actions) or a step card (steps: before / after by its half).
    function dropTarget(ev) {
        const t = ev.target.closest ? ev.target : ev.target.parentElement;
        if (!t || (dragId == null && dragStep == null)) { return null; }
        const gap = t.closest("[data-rd-gap]");
        if (gap) { return { el: gap, gap: Number(gap.dataset.rdGap) }; }
        if (dragId != null) {
            const zone = t.closest("[data-rd-drop]");
            return zone ? { el: zone, zone } : null;
        }
        const card = t.closest("[data-rd-step]");
        if (!card) { return null; }
        const box = card.getBoundingClientRect();
        const idx = Number(card.dataset.stepIndex) - 1;
        return { el: card, gap: ev.clientY < box.top + box.height / 2 ? idx : idx + 1 };
    }

    root.addEventListener("dragover", (ev) => {
        const hit = dropTarget(ev);
        if (!hit) { return; }
        ev.preventDefault();
        ev.dataTransfer.dropEffect = "move";
        root.querySelectorAll(".rd-plan-over").forEach((el) => el.classList.remove("rd-plan-over"));
        if (hit.el.matches("[data-rd-gap], [data-rd-drop]")) { hit.el.classList.add("rd-plan-over"); }
    });
    root.addEventListener("drop", (ev) => {
        const hit = dropTarget(ev);
        if (!hit) { return; }
        ev.preventDefault();
        const id = dragId;
        const stepUid = dragStep;
        clearDrag();
        if (stepUid != null) { moveStep(stepUid, hit.gap); return; }
        if (hit.gap != null) { moveAction(id, null, null, hit.gap); return; }
        const row = ev.target.closest("[data-rd-action]");
        let before = null;
        if (row && Number(row.dataset.rdAction) !== id) {
            const box = row.getBoundingClientRect();
            before = ev.clientY < box.top + box.height / 2
                ? Number(row.dataset.rdAction)
                : (row.nextElementSibling && row.nextElementSibling.dataset.rdAction
                    ? Number(row.nextElementSibling.dataset.rdAction) : null);
        }
        moveAction(id, hit.zone.dataset.rdDrop, before);
    });

    // Keyboard: Arrow up / down on a step's handle moves the whole step.
    root.addEventListener("keydown", (ev) => {
        const handle = ev.target.closest && ev.target.closest("[data-rd-step-handle]");
        if (!handle || (ev.key !== "ArrowUp" && ev.key !== "ArrowDown")) { return; }
        ev.preventDefault();
        const uid = Number(handle.closest("[data-rd-step]").dataset.stepUid);
        const at = steps.findIndex((st) => st.uid === uid);
        moveStep(uid, ev.key === "ArrowUp" ? at - 1 : at + 2);
    });

    root.addEventListener("click", (ev) => {
        if (ev.target.closest("[data-rd-create]")) { createPlan(); return; }
        const del = ev.target.closest("[data-rd-del-step]");
        if (del) {
            const uid = Number(del.closest("[data-rd-step]").dataset.stepUid);
            const at = steps.findIndex((s) => s.uid === uid);
            if (at >= 0 && !steps[at].actions.length) {
                steps.splice(at, 1);
                render();
                save();
            }
        }
    });
    // Title edits: debounced while typing, flushed on change (blur / Enter).
    let titleTimer = null;
    let titleDirty = false;
    function commitTitle(input) {
        const uid = Number(input.closest("[data-rd-step]").dataset.stepUid);
        const step = steps.find((s) => s.uid === uid);
        if (step) { step.title = input.value; }
    }
    root.addEventListener("input", (ev) => {
        const input = ev.target.closest && ev.target.closest("[data-rd-title]");
        if (!input) { return; }
        commitTitle(input);
        titleDirty = true;
        clearTimeout(titleTimer);
        titleTimer = setTimeout(() => { titleDirty = false; save(); }, 600);
    });
    root.addEventListener("change", (ev) => {
        const input = ev.target.closest && ev.target.closest("[data-rd-title]");
        if (!input) { return; }
        commitTitle(input);
        clearTimeout(titleTimer);
        if (titleDirty) { titleDirty = false; save(); }
    });

    const addStep = document.querySelector("[data-rd-plan-add-step]");
    if (addStep) {
        addStep.addEventListener("click", () => {
            steps.push(newStep(null));
            render();
            save();
        });
    }
    const auto = document.querySelector("[data-rd-plan-auto]");
    if (auto) { auto.addEventListener("click", () => { autoOrder(); }); }
    const resetBtn = document.querySelector("[data-rd-plan-reset]");
    if (resetBtn) { resetBtn.addEventListener("click", () => { resetPlan(); }); }

    // ---- first load -------------------------------------------------------

    render();
    root.dataset.rdPlanBusy = "0";
    if (steps.length) {
        setBusy(1);
        simulate(null).finally(() => setBusy(-1));
    }
}
