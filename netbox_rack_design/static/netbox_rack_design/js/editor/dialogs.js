/*
 * The editor's three Bootstrap modal dialogs -- extracted verbatim from
 * editor.js, where they sat contiguously.
 *
 * `getCsrfToken`/`createToast` come from editor/core.js. `previewName`
 * stays in editor.js: it closes over real per-rack controller state
 * (`collectPendingNames()` -> `controllersByRackId`), not template-derived
 * DOM, so it cannot be lazily re-derived the way `designTitle` is below.
 * Instead `showMoveNameDialog` -- the one dialog that calls it --
 * takes it as an explicit injected parameter from its one real call site
 * (editor.js, the §4a move-drop handler).
 */

import { getCsrfToken, createToast } from "rd/core.js";

// Guarded: this runs at MODULE EVALUATION, and editor.js deliberately
// no-ops when #rd-editor is absent (its `if (!root) return`). An unguarded
// getAttribute here would turn that graceful no-op into a TypeError.
const rootEl = document.getElementById("rd-editor");
const designTitle = (rootEl && rootEl.getAttribute("data-design-title")) || "";

function showMoveNameDialog(oldName, currentName, previewCtx, onConfirm, onCancel, previewName) {
    var keepName = (designTitle ? designTitle + "-" : "") + (oldName || "");

    var overlay = document.createElement("div");
    overlay.className = "modal fade nbx-rd-move-modal";
    overlay.setAttribute("tabindex", "-1");
    overlay.innerHTML =
        '<div class="modal-dialog modal-dialog-centered modal-sm">'
        + '<div class="modal-content">'
        + '<div class="modal-header">'
        + '<h5 class="modal-title">' + "Name this move" + "</h5>"
        + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
        + "</div>"
        + '<div class="modal-body">'
        + '<div class="form-check">'
        + '<input class="form-check-input" type="radio" name="nbx-rd-move-name" id="nbx-rd-move-keep" value="keep" checked>'
        + '<label class="form-check-label" for="nbx-rd-move-keep">Keep the old name</label>'
        + '<div class="form-text"><code></code></div>'
        + "</div>"
        + '<div class="form-check mt-2">'
        + '<input class="form-check-input" type="radio" name="nbx-rd-move-name" id="nbx-rd-move-new" value="new">'
        + '<label class="form-check-label" for="nbx-rd-move-new">Set a new name</label>'
        + "</div>"
        + '<div class="input-group input-group-sm mt-1">'
        + '<input type="text" class="form-control nbx-rd-move-new-input" '
        + 'placeholder="New name" disabled>'
        + '<span class="input-group-text nbx-rd-move-new-warning" '
        + 'style="display:none" title="A device with this name already exists in the site.">'
        + '<i class="mdi mdi-alert" aria-hidden="true"></i></span>'
        + "</div>"
        + '<div class="form-text">Template tokens are dotted model paths, e.g. '
        + "<code>{design.name}</code>, <code>{device.site.name}</code>.</div>"
        + "</div>"
        + '<div class="modal-footer">'
        + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
        + '<button type="button" class="btn btn-sm btn-primary" data-rd-move-apply>Apply</button>'
        + "</div>"
        + "</div></div>";
    document.body.appendChild(overlay);

    // Fill the keep-name preview + the new-name input's starting value.
    overlay.querySelector(".form-text code").textContent = keepName;
    var keepRadio = overlay.querySelector("#nbx-rd-move-keep");
    var newRadio = overlay.querySelector("#nbx-rd-move-new");
    var newInput = overlay.querySelector(".nbx-rd-move-new-input");
    var newWarn = overlay.querySelector(".nbx-rd-move-new-warning");
    // `currentName` is `w.proposed_name` (the STORED value): empty means
    // either "never touched" or "keep-name was chosen" -- both prefill
    // blank, so there is nothing left to compare against `keepName` for
    // (that comparison used to matter when keep-name stored the decorated
    // string itself; it no longer does).
    var hasCustomName = !!currentName;
    newInput.value = hasCustomName ? currentName : "";

    // Mirrors widget.nameUserSet from the add path (editor.js ~5948): once
    // the user has typed into the field, no preview response may overwrite
    // it again, no matter how many times the radios get toggled. A value
    // loaded from an EARLIER session's rename (currentName, prefilled
    // above) must count as "user-set" from the start too -- the add
    // path's nameUserSet lives on the widget and survives a reopen for
    // free, but this dialog is rebuilt fresh every open, so there is no
    // other signal that currentName was a human's choice, not a blank
    // slate. Without this, opening the dialog on an already-renamed
    // placement silently replaced that name with a fresh engine
    // suggestion (bug caught in review, phase 3).
    var userEdited = hasCustomName;
    // Guards against firing the same preview request twice in a row (e.g.
    // keep -> rename -> keep -> rename without an edit in between).
    var previewRequested = false;

    function applyWarn(exists) {
        if (!newWarn) { return; }
        newWarn.style.display = exists ? "" : "none";
    }

    function requestPreview() {
        if (!previewCtx || userEdited || previewRequested) { return; }
        previewRequested = true;
        previewName({
            kind: "move",
            device: previewCtx.device,
            device_role: previewCtx.device_role,
            tenant: previewCtx.tenant,
            target_rack: previewCtx.target_rack,
            target_position: previewCtx.target_position,
            target_face: previewCtx.target_face,
        }).then(function (data) {
            // A null result (unreachable endpoint, no permission, any
            // failure) leaves the field exactly as it was -- never
            // blanked, never a placeholder.
            if (!data || userEdited) { return; }
            if (data.name) { newInput.value = data.name; }
            applyWarn(!!data.exists_in_site);
        });
    }

    function syncEnabled() {
        newInput.disabled = !newRadio.checked;
        if (newRadio.checked) {
            newInput.focus();
            requestPreview();
        }
    }
    keepRadio.addEventListener("change", syncEnabled);
    newRadio.addEventListener("change", syncEnabled);
    newInput.addEventListener("focus", function () {
        newRadio.checked = true;
        syncEnabled();
    });
    newInput.addEventListener("input", function () {
        userEdited = true;
        applyWarn(false);
    });
    // Selecting rename with it already checked (dialog opened with rename
    // preselected) never runs the "change" handler above, so fire once
    // up front too. Keep-name preselected/confirmed issues NO request at
    // all -- requestPreview() only ever runs from this rename-only path.
    if (newRadio.checked) { requestPreview(); }

    var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
    var modal = ctor ? new ctor(overlay) : null;
    var decided = false;

    // Bootstrap's hide() SILENTLY bails while the show-fade transition
    // is still running (`_isTransitioning`) -- confirmed live
    // (2026-07-08): an Apply click within ~150ms of the dialog opening
    // ran onConfirm but left the modal on screen FOREVER. Queue the
    // hide until 'shown.bs.modal' has fired, so a fast click can never
    // strand the dialog.
    var shownDone = false, hidePending = false;
    overlay.addEventListener("shown.bs.modal", function () {
        shownDone = true;
        if (hidePending && modal) { modal.hide(); }
    });
    function requestHide() {
        if (!modal) { overlay.remove(); return; }
        if (shownDone) { modal.hide(); } else { hidePending = true; }
    }

    // `stored` is what reaches `proposed_name`: empty for keep-name, the
    // typed value verbatim for a rename. `display` is what the tile
    // SHOWS: the decorated "<design>-<old name>" for keep-name, the same
    // typed value for a rename. They diverge on keep-name -- that is the
    // whole point of this phase (contract: empty proposed_name means
    // "keep the device's current name"; the decoration is a render-time
    // detail, never stored).
    function finishConfirm(stored, display) {
        if (decided) { return; }
        decided = true;
        if (typeof onConfirm === "function") { onConfirm(stored, display); }
    }
    function finishCancel() {
        if (decided) { return; }
        decided = true;
        if (typeof onCancel === "function") { onCancel(); }
    }

    overlay.querySelector("[data-rd-move-apply]").addEventListener("click", function () {
        // A blank rename input (rename selected but nothing typed) falls
        // back to the same keep-name outcome as the keep radio: stored
        // empty, displayed decorated. An empty string is a legitimate,
        // meaningful stored value now -- never coerced back to keepName.
        var typed = newRadio.checked ? newInput.value.trim() : "";
        finishConfirm(typed, typed || keepName);
        requestHide();
    });
    // Bootstrap sets aria-hidden on the modal as it hides, which triggers a
    // console warning if DOM focus is still inside (e.g. the Apply/Cancel
    // button that was just clicked). Move focus out before that happens,
    // regardless of which dismissal path (Apply, Cancel, ×, Esc, backdrop)
    // triggered the hide.
    overlay.addEventListener("hide.bs.modal", function () {
        if (overlay.contains(document.activeElement)) {
            document.activeElement.blur();
        }
    });
    // Explicit Cancel/× handling (same reasoning as the displace dialog
    // below): call our own finishCancel + transition-safe hide rather
    // than relying on Bootstrap's document-level dismiss delegation,
    // which routes through the same transition-guarded hide() and can
    // strand the dialog on a fast click.
    overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
        btn.addEventListener("click", function () {
            finishCancel();
            requestHide();
        });
    });
    // Any dismissal that wasn't Apply (Cancel button, ×, Esc, backdrop) aborts
    // the move. `decided` is already set if Apply ran, so this is a no-op then.
    overlay.addEventListener("hidden.bs.modal", function () {
        finishCancel();
        overlay.remove();
    });

    if (modal) {
        modal.show();
    } else {
        // No Bootstrap JS: degrade to confirming the default without blocking.
        finishConfirm(keepName);
        overlay.remove();
    }
}

function showDisplaceConfirmDialog(displaced, newLabel, onConfirm, onCancel) {
    var overlay = document.createElement("div");
    overlay.className = "modal fade nbx-rd-displace-modal";
    overlay.setAttribute("tabindex", "-1");
    overlay.innerHTML =
        '<div class="modal-dialog modal-dialog-centered modal-sm">'
        + '<div class="modal-content">'
        + '<div class="modal-header">'
        + '<h5 class="modal-title">Confirm placement</h5>'
        + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
        + "</div>"
        + '<div class="modal-body">'
        + '<p>This slot is occupied by <strong class="nbx-rd-displace-old"></strong> '
        + "(being removed or moved away). Place "
        + '<strong class="nbx-rd-displace-new"></strong> here?</p>'
        + "</div>"
        + '<div class="modal-footer">'
        + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
        + '<button type="button" class="btn btn-sm btn-primary" data-rd-displace-confirm>Place here</button>'
        + "</div>"
        + "</div></div>";
    document.body.appendChild(overlay);

    overlay.querySelector(".nbx-rd-displace-old").textContent =
        displaced.map(function (d) { return d.label; }).join(", ");
    overlay.querySelector(".nbx-rd-displace-new").textContent = newLabel || "";

    var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
    var modal = ctor ? new ctor(overlay) : null;
    var decided = false;

    // Transition-safe hide, same as showMoveNameDialog above: a hide()
    // issued while the show-fade is still running is silently dropped
    // by Bootstrap, stranding the dialog on screen forever.
    var shownDone = false, hidePending = false;
    overlay.addEventListener("shown.bs.modal", function () {
        shownDone = true;
        if (hidePending && modal) { modal.hide(); }
    });
    function requestHide() {
        if (!modal) { overlay.remove(); return; }
        if (shownDone) { modal.hide(); } else { hidePending = true; }
    }

    function finishConfirm() {
        if (decided) { return; }
        decided = true;
        if (typeof onConfirm === "function") { onConfirm(); }
    }
    function finishCancel() {
        if (decided) { return; }
        decided = true;
        if (typeof onCancel === "function") { onCancel(); }
    }

    overlay.querySelector("[data-rd-displace-confirm]").addEventListener("click", function () {
        finishConfirm();
        requestHide();
    });
    // Every `data-bs-dismiss="modal"` control (Cancel, the × close
    // button) explicitly calls finishCancel()+hide() itself, rather than
    // relying on Bootstrap's own document-level dismiss delegation to
    // reach OUR modal instance -- confirmed live that relying on it
    // alone left a dialog open forever with neither onConfirm nor
    // onCancel ever firing. Esc/backdrop dismissal (which never runs a
    // click handler at all) is still covered by the `hidden.bs.modal`
    // fallback below.
    overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
        btn.addEventListener("click", function () {
            finishCancel();
            requestHide();
        });
    });
    overlay.addEventListener("hide.bs.modal", function () {
        if (overlay.contains(document.activeElement)) {
            document.activeElement.blur();
        }
    });
    // Any dismissal that wasn't caught above (Esc, backdrop click) still
    // aborts the displacement. `decided` already guards against a
    // double-fire alongside the explicit handlers above.
    overlay.addEventListener("hidden.bs.modal", function () {
        finishCancel();
        overlay.remove();
    });

    if (modal) {
        modal.show();
    } else {
        // No Bootstrap JS: degrade to confirming without blocking, same
        // fallback showMoveNameDialog uses.
        finishConfirm();
        overlay.remove();
    }
}

function showRerunNamingDialog(lines, onConfirm, onCancel) {
    var overlay = document.createElement("div");
    overlay.className = "modal fade nbx-rd-rerun-naming-modal";
    overlay.setAttribute("tabindex", "-1");
    // Each line is built as DOM with the two names set via textContent,
    // NEVER interpolated into innerHTML: a `proposed_name` is text a
    // planner typed, so concatenating it into markup would execute it.
    // This is the same build-the-skeleton-then-textContent shape the
    // displace dialog above uses for its own two labels.
    var rowNodes = (lines || []).map(function (line) {
        var li = document.createElement("li");
        li.className = "nbx-rd-rerun-naming-line";
        li.setAttribute("data-rd-placement-id", line.placement_id);
        var oldCode = document.createElement("code");
        oldCode.textContent = line.old_name || "(unnamed)";
        var newCode = document.createElement("code");
        newCode.textContent = line.new_name || "(unnamed)";
        li.appendChild(oldCode);
        li.appendChild(document.createTextNode(" \u2192 "));
        li.appendChild(newCode);
        // The engine can hand back the very name it was asked to replace
        // (P11: both designs' counters legitimately land on the same
        // number), so the row says which lines actually change.
        if (line.unchanged) {
            var same = document.createElement("span");
            same.className = "text-muted small";
            same.title = "Re-running produced the same name; this line changes nothing.";
            same.textContent = " (unchanged)";
            li.appendChild(same);
        }
        if (line.still_colliding) {
            var warn = document.createElement("span");
            warn.className = "text-warning-emphasis small";
            warn.title = "This name is still claimed by a peer design after re-running.";
            warn.textContent = " (still collides)";
            li.appendChild(warn);
        }
        return li;
    });
    overlay.innerHTML =
        '<div class="modal-dialog modal-dialog-centered modal-sm">'
        + '<div class="modal-content">'
        + '<div class="modal-header">'
        + '<h5 class="modal-title">' + "Re-run naming" + "</h5>"
        + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
        + "</div>"
        + '<div class="modal-body">'
        + '<p class="small text-muted">These planned names collide with a peer '
        + "design; confirming renames every line below.</p>"
        + '<ul class="mb-0 ps-3 nbx-rd-rerun-naming-lines"></ul>'
        + "</div>"
        + '<div class="modal-footer">'
        + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
        + '<button type="button" class="btn btn-sm btn-primary" data-rd-rerun-naming-confirm>Confirm</button>'
        + "</div>"
        + "</div></div>";
    var listEl = overlay.querySelector(".nbx-rd-rerun-naming-lines");
    rowNodes.forEach(function (li) { listEl.appendChild(li); });
    document.body.appendChild(overlay);

    var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
    var modal = ctor ? new ctor(overlay) : null;
    var decided = false;

    var shownDone = false, hidePending = false;
    overlay.addEventListener("shown.bs.modal", function () {
        shownDone = true;
        if (hidePending && modal) { modal.hide(); }
    });
    function requestHide() {
        if (!modal) { overlay.remove(); return; }
        if (shownDone) { modal.hide(); } else { hidePending = true; }
    }

    function finishConfirm() {
        if (decided) { return; }
        decided = true;
        if (typeof onConfirm === "function") { onConfirm(); }
    }
    function finishCancel() {
        if (decided) { return; }
        decided = true;
        if (typeof onCancel === "function") { onCancel(); }
    }

    overlay.querySelector("[data-rd-rerun-naming-confirm]").addEventListener("click", function () {
        finishConfirm();
        requestHide();
    });
    overlay.querySelectorAll("[data-bs-dismiss='modal']").forEach(function (btn) {
        btn.addEventListener("click", function () {
            finishCancel();
            requestHide();
        });
    });
    overlay.addEventListener("hide.bs.modal", function () {
        if (overlay.contains(document.activeElement)) {
            document.activeElement.blur();
        }
    });
    overlay.addEventListener("hidden.bs.modal", function () {
        finishCancel();
        overlay.remove();
    });

    if (modal) {
        modal.show();
    } else {
        finishConfirm();
        overlay.remove();
    }
}


export { showMoveNameDialog, showDisplaceConfirmDialog, showRerunNamingDialog };
