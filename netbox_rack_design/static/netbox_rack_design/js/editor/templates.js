/*
 * The Templates tab (PLAN-templates.md Sec 2/3, D14/D19/D22/D24): a
 * reusable rack layout ("our standard ToR") or a TemplateGroup ("compute
 * pod") that stamps its devices onto a design rack. Sibling of palette.js --
 * same left-rail-section shape, same fetch-and-render-a-list-of-cards
 * pattern -- but the payload it drags in is a whole rack's worth of devices
 * at SERVER-COMPUTED positions, not one device type at a cursor position, so
 * it does not go through GridStack's own drag-in machinery (setupDragIn):
 * there is no single clone the size of one device to hand it, and the
 * landing slots are not the cursor's business at all here (D19 -- the
 * server already validated them against this rack's current occupancy).
 * Native HTML5 drag/drop supplies just enough (dragstart/dragover/drop) to
 * know WHICH template and WHICH rack; everything about where each device
 * lands comes back from preview-template.
 *
 * Two entry points, one materializer (D24):
 *   - drag a template card onto a rack block -> stamps that ONE rack
 *     (repetition's common case: a single template, no choices to make);
 *   - click a template OR group card -> a dialog. A template's dialog is a
 *     checklist (which racks, all get the SAME content); a group's is a
 *     mapping table (D14: one row per member template, each with its OWN
 *     target rack -- a group is a CORRESPONDENCE, not repeated content).
 * Both paths end at stampToRacks(), which calls preview-template (read-only,
 * writes nothing -- D19) and hands the response straight to each target
 * rack controller's stampTemplateItems(); nothing is written until the
 * ordinary design-level Save.
 */
import { getCsrfToken, createToast, rackKeyToServer } from "rd/core.js";
import { controllersByRackId } from "rd/registry.js";

// ---- Rack identity: DOM form <-> the "r:<pk>"/"p:<pk>" wire form -----------
// rackKeyToServer() (editor/core.js, D31) hands back a NUMBER for a real rack
// (byte-compatible with every other save-layout caller) and a "p:<pk>" STRING
// for a planned one. Both preview-template's `racks` field/response keys AND
// from-design's `rack` body field (T4.2) are always the string form (D27) --
// a bare number would be sent as JSON `123`, not `"r:123"`, and (more
// importantly) could never be used to look a response back up by key. Module-
// level (not per-setup-function) because both the Templates tab and the
// Save-as-template button need the ALWAYS-STRING form.
function rackKeyForApi(domRackId) {
    var v = rackKeyToServer(domRackId);
    return (typeof v === "number") ? ("r:" + v) : v;
}

// ---- Bootstrap-modal helper shared by every dialog in this module ----------
// Same shape as every other editor dialog (dialogs.js, palette.js's
// favorite-set modals): built fresh at open time, removed on hide, a
// transition-safe requestHide so a fast click can never strand it. Module-
// level so the Save-as-template dialog (below) can reuse it verbatim instead
// of a second copy that could drift.
function buildModal(title, bodyHtml, confirmText) {
    var overlay = document.createElement("div");
    overlay.className = "modal fade nbx-rd-apply-template-modal";
    overlay.setAttribute("tabindex", "-1");
    overlay.innerHTML =
        '<div class="modal-dialog modal-dialog-centered modal-lg">'
        + '<div class="modal-content">'
        + '<div class="modal-header">'
        + '<h5 class="modal-title"></h5>'
        + '<button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>'
        + "</div>"
        + '<div class="modal-body">' + bodyHtml + "</div>"
        + '<div class="modal-footer">'
        + '<button type="button" class="btn btn-sm btn-link" data-bs-dismiss="modal">Cancel</button>'
        + '<button type="button" class="btn btn-sm btn-primary" data-rd-apply-template-confirm></button>'
        + "</div>"
        + "</div></div>";
    overlay.querySelector(".modal-title").textContent = title;
    overlay.querySelector("[data-rd-apply-template-confirm]").textContent = confirmText;
    document.body.appendChild(overlay);

    var ctor = (window.bootstrap && window.bootstrap.Modal) || window.Modal;
    var modal = ctor ? new ctor(overlay) : null;
    var shownDone = false, hidePending = false;
    overlay.addEventListener("shown.bs.modal", function () {
        shownDone = true;
        if (hidePending && modal) { modal.hide(); }
    });
    overlay.addEventListener("hide.bs.modal", function () {
        if (overlay.contains(document.activeElement)) { document.activeElement.blur(); }
    });
    overlay.addEventListener("hidden.bs.modal", function () { overlay.remove(); });
    return {
        overlay: overlay,
        requestHide: function () {
            if (!modal) { overlay.remove(); return; }
            if (shownDone) { modal.hide(); } else { hidePending = true; }
        },
        show: function () { if (modal) { modal.show(); } else { overlay.remove(); } },
    };
}

export function setupTemplates(root) {
    var listEl = document.getElementById("nbx-rd-templates-list");
    var statusEl = document.getElementById("nbx-rd-templates-status");
    if (!listEl) { return; }

    var templatesUrl = root.getAttribute("data-templates-url")
        || "/api/plugins/rack-design/templates/";
    var templateGroupsUrl = root.getAttribute("data-template-groups-url")
        || "/api/plugins/rack-design/template-groups/";
    // Derived from the save URL exactly like editor.js derives
    // recompute-distribution's URL -- one design pk, three sibling actions.
    var saveUrl = root.getAttribute("data-save-url") || "";
    var previewTemplateUrl = saveUrl
        ? saveUrl.replace(/save-layout\/?$/, "preview-template/") : "";

    function setStatus(msg) {
        if (statusEl) { statusEl.textContent = msg || ""; }
    }

    function collectRackOptions() {
        return Array.prototype.slice.call(
            document.querySelectorAll(".nbx-rd-rack-block")
        ).map(function (block) {
            var domId = block.getAttribute("data-rack-id");
            var titleEl = block.querySelector(".nbx-rd-rack-block-title a");
            return {
                domId: domId,
                key: rackKeyForApi(domId),
                name: titleEl ? titleEl.textContent.trim() : ("Rack " + domId),
            };
        });
    }

    // The editor's CURRENT unsaved edits, so preview-template's occupancy
    // check sees what the planner is actually looking at, not just what is
    // already saved (D19). NbxRdEditor is assigned later in editor.js's own
    // module body than this setup call runs, but never before the FIRST
    // drag/click a planner could make, so a lazy read here is correct.
    function currentLayout() {
        var api = window.NbxRdEditor;
        return (api && typeof api.buildLayoutPayload === "function")
            ? api.buildLayoutPayload() : null;
    }

    // Bulk-resolve the device-type facts preview-template's response omits
    // (u_height, is_full_depth, a display model name) -- mirrors palette.js's
    // own "NOTE: not brief" fetch, for the same reason: the brief serializer
    // sizes every tile at 1U. `ids` may repeat across racks/blades; the query
    // string just repeats too, which the API tolerates fine at this scale (a
    // template is a handful of devices, not hundreds).
    function fetchDeviceTypeInfo(ids) {
        var uniq = [];
        var seen = {};
        (ids || []).forEach(function (id) {
            if (id == null || seen[id]) { return; }
            seen[id] = true;
            uniq.push(id);
        });
        if (!uniq.length) { return Promise.resolve({}); }
        var url = "/api/dcim/device-types/?limit=" + uniq.length
            + uniq.map(function (id) { return "&id=" + encodeURIComponent(id); }).join("");
        return fetch(url, {
            credentials: "same-origin",
            headers: { "Accept": "application/json" },
        }).then(function (resp) {
            return resp.ok ? resp.json() : { results: [] };
        }).then(function (data) {
            var map = {};
            (data.results || []).forEach(function (dt) {
                var manuf = (dt.manufacturer && (dt.manufacturer.name || dt.manufacturer.display)) || "";
                map[dt.id] = {
                    u_height: (dt.u_height != null) ? dt.u_height : 1,
                    is_full_depth: !!dt.is_full_depth,
                    model: (manuf ? manuf + " " : "") + (dt.model || dt.display || ("type " + dt.id)),
                };
            });
            return map;
        }).catch(function () { return {}; });
    }

    function entryToItem(entry, dtInfo) {
        var info = dtInfo[entry.device_type] || {};
        return {
            device_type_id: entry.device_type,
            u_height: info.u_height || 1,
            is_full_depth: !!info.is_full_depth,
            model: info.model,
            device_role_id: entry.device_role,
            tenant_id: entry.tenant,
            position: entry.position,
            face: entry.face,
            label_text: entry.label || info.model,
            name: entry.name,
            name_collision: entry.name_collision,
            // D20 provenance, carried from preview-template through to the
            // stamped tile so save-layout can persist it (rack.js's
            // stampTemplateItems reads these two off the item).
            from_template: entry.from_template,
            from_template_version: entry.from_template_version,
            // D8: everything a template placement carries except rack/
            // position/power, including the deployment's config-declared
            // planning fields -- applied to the stamped tile the same way
            // attachPlacementFieldsButton applies a manual add's, so it
            // survives the drop instead of silently coming out empty.
            planning_data: entry.planning_data || null,
        };
    }

    // Every device_type referenced anywhere in a preview-template response
    // (top-level items AND blades -- a blade's own type is worth knowing for
    // the dialog's summary even though it is not placed, see stampRackEntries).
    // A preview-template response mixes per-rack item arrays (keyed by the
    // namespaced rack key, models.rack_key(): "r:<pk>" / "p:<pk>") with
    // top-level metadata keys -- "skipped", "already_stamped", and whatever
    // gets added next. Recognise a rack key POSITIVELY rather than
    // blacklisting the metadata ones: the blacklist shape ("skip 'skipped'")
    // broke the moment "already_stamped" was added, with
    // `(data[key] || []).forEach is not a function` killing the whole module.
    function rackKeysIn(data) {
        return Object.keys(data || {}).filter(function (key) {
            return /^[rp]:\d+$/.test(key) && Array.isArray(data[key]);
        });
    }

    function deviceTypeIdsIn(data) {
        var ids = [];
        rackKeysIn(data).forEach(function (key) {
            (data[key] || []).forEach(function (entry) {
                ids.push(entry.device_type);
                (entry.blades || []).forEach(function (b) { ids.push(b.device_type); });
            });
        });
        return ids;
    }

    // Just the CHASSIS device types that actually carry blades -- the set
    // rdBayItemsForRack's bay-name check (below) needs to validate against,
    // narrower than deviceTypeIdsIn so a template with no chassis at all
    // never issues the device-bay-templates request below.
    function chassisDeviceTypeIdsIn(data) {
        var ids = [];
        rackKeysIn(data).forEach(function (key) {
            (data[key] || []).forEach(function (entry) {
                if ((entry.blades || []).length) { ids.push(entry.device_type); }
            });
        });
        return ids;
    }

    // Bulk-resolve which bay NAMES actually exist on a device type (D32: a
    // blade may not exist in a template without its chassis, but nothing
    // upstream of here guarantees the stamped chassis's REAL device type
    // still has the bay a template placement named -- device types drift
    // after a template is built). A name preview-template's response carries
    // that this call does not confirm is surfaced as a warning by
    // stampRackEntries, never silently placed against a bay that is not
    // there and never silently dropped without a trace.
    function fetchDeviceBayNames(deviceTypeIds) {
        var uniq = [];
        var seen = {};
        (deviceTypeIds || []).forEach(function (id) {
            if (id == null || seen[id]) { return; }
            seen[id] = true;
            uniq.push(id);
        });
        if (!uniq.length) { return Promise.resolve({}); }
        var url = "/api/dcim/device-bay-templates/?limit=500"
            + uniq.map(function (id) { return "&device_type_id=" + encodeURIComponent(id); }).join("");
        return fetch(url, {
            credentials: "same-origin",
            headers: { "Accept": "application/json" },
        }).then(function (resp) {
            return resp.ok ? resp.json() : { results: [] };
        }).then(function (data) {
            var map = {};
            (data.results || []).forEach(function (bt) {
                var dtId = bt.device_type && (bt.device_type.id != null ? bt.device_type.id : bt.device_type);
                if (dtId == null) { return; }
                if (!map[dtId]) { map[dtId] = {}; }
                map[dtId][bt.name] = true;
            });
            return map;
        }).catch(function () { return {}; });
    }

    // Place one rack's fitted entries as unsaved tiles via that rack's own
    // controller. `domId` addresses controllersByRackId directly (it is
    // keyed by the SAME DOM rack id every initRack registers itself under).
    // `bayNamesByDeviceType` (from fetchDeviceBayNames) gates which blades
    // are handed to the controller at all -- a bay name that does not exist
    // on the stamped chassis's device type is reported here, exactly like an
    // island warning at save-as-template time (PLAN-templates.md D32):
    // visible, never a silent drop. Returns true if the rack was found and
    // stamped.
    function stampRackEntries(domId, entries, dtInfo, bayNamesByDeviceType) {
        var controller = controllersByRackId[domId];
        if (!controller || typeof controller.stampTemplateItems !== "function") { return false; }
        var missingBays = [];
        var items = (entries || []).map(function (entry) {
            var item = entryToItem(entry, dtInfo);
            var knownBays = (bayNamesByDeviceType || {})[entry.device_type] || {};
            item.blades = (entry.blades || []).filter(function (b) {
                if (knownBays[b.target_bay_name]) { return true; }
                missingBays.push(
                    (b.name || (dtInfo[b.device_type] || {}).model || ("device type " + b.device_type))
                        + " (bay “" + b.target_bay_name + "”)"
                );
                return false;
            }).map(function (b) {
                var info = dtInfo[b.device_type] || {};
                return {
                    device_type_id: b.device_type,
                    model: info.model,
                    target_bay_name: b.target_bay_name,
                    device_role_id: b.device_role,
                    tenant_id: b.tenant,
                    name: b.name,
                    name_collision: b.name_collision,
                    label: b.label,
                    planning_data: b.planning_data,
                    from_template: b.from_template,
                    from_template_version: b.from_template_version,
                };
            });
            return item;
        });
        controller.stampTemplateItems(items);
        if (missingBays.length) {
            createToast(
                "danger", "Template",
                missingBays.length + " blade" + (missingBays.length === 1 ? "" : "s")
                    + " could not be placed -- no such bay on the stamped chassis: "
                    + missingBays.join(", ")
            );
        }
        return true;
    }

    // Read-only POST to preview-template. Resolves to {ok, data} -- `data` is
    // the parsed body either way, so a 400's field errors are still visible.
    function previewTemplate(templateId, rackKeys) {
        if (!previewTemplateUrl) { return Promise.resolve({ ok: false, data: null }); }
        var body = { template: templateId, racks: rackKeys };
        var layout = currentLayout();
        if (layout) { body.layout = layout; }
        return fetch(previewTemplateUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify(body),
        }).then(function (resp) {
            return resp.text().then(function (text) {
                var data = null;
                try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
                return { ok: resp.ok, data: data };
            });
        }).catch(function () { return { ok: false, data: null }; });
    }

    function previewErrorMessage(data) {
        if (!data) { return "Could not reach the server."; }
        var parts = [];
        Object.keys(data).forEach(function (k) {
            var v = data[k];
            parts.push(k + ": " + (Array.isArray(v) ? v.join(" ") : v));
        });
        return parts.length ? parts.join(" ") : "The preview could not be computed.";
    }

    // Drag a template card straight onto one rack: no dialog, D24's common
    // case. Skip is reported via a toast -- never a silent no-op, matching
    // the brief's "which racks were skipped" requirement even on the single-
    // rack path.
    function applyTemplateToOneRack(templateId, templateName, domId) {
        var key = rackKeyForApi(domId);
        previewTemplate(templateId, [key]).then(function (res) {
            if (!res.ok) {
                createToast("danger", "Template", previewErrorMessage(res.data));
                return;
            }
            var data = res.data || {};
            var skipped = (data.skipped || []).find(function (s) { return s.rack === key; });
            if (skipped) {
                createToast(
                    "danger", "Template",
                    "“" + templateName + "” does not fit here: " + skipped.reason
                );
                return;
            }
            // Gap 1 (T3.6): the drag path has no confirm step of its own --
            // the drop IS the commit, to an unsaved tile the planner can
            // still remove -- so the warning fires alongside the stamp
            // rather than blocking it, exactly like the two dialogs below.
            var already = (data.already_stamped || {})[key];
            if (already && already.length) {
                createToast(
                    "warning", "Template",
                    "“" + templateName + "”: this rack " + alreadyStampedNote(already) + "."
                );
            }
            Promise.all([
                fetchDeviceTypeInfo(deviceTypeIdsIn(data)),
                fetchDeviceBayNames(chassisDeviceTypeIdsIn(data)),
            ]).then(function (results) {
                stampRackEntries(domId, data[key], results[0], results[1]);
            });
        });
    }

    // T3.6 gap 1: preview-template's "already_stamped" entry for one rack --
    // [{version, count}, ...] -- rendered as one line. Multiple entries mean
    // multiple DISTINCT from_template_version values are already sitting in
    // this rack (a drifted template stamped more than once), so every one
    // is named, never collapsed into a single count that would hide the
    // drift. Never used to block anything (D19/the T3.6 brief: two
    // identical ToR blocks in one rack can be a legitimate ask) -- purely
    // informational, so the planner decides with the fact in front of them
    // instead of finding it later in the peer-conflicts panel.
    function alreadyStampedNote(entries) {
        if (!entries || !entries.length) { return ""; }
        var parts = entries.map(function (e) {
            return e.count + " device" + (e.count === 1 ? "" : "s") + " (version " + e.version + ")";
        });
        return "already has " + parts.join(", ") + " from this template";
    }

    function fitSummary(entries) {
        var positions = (entries || []).map(function (e) { return e.position; });
        if (!positions.length) { return "OK"; }
        var lo = Math.min.apply(null, positions), hi = Math.max.apply(null, positions);
        return "U" + hi + (hi !== lo ? "–U" + lo : "") + " · OK";
    }

    // ---- Single-template dialog: a checklist of racks (D24 "which racks") -
    function openSingleTemplateDialog(template) {
        var racks = collectRackOptions();
        if (!racks.length) {
            createToast("info", "Template", "This design has no racks yet.");
            return;
        }
        setStatus("Computing fit…");
        previewTemplate(template.id, racks.map(function (r) { return r.key; })).then(function (res) {
            setStatus("");
            if (!res.ok) {
                createToast("danger", "Template", previewErrorMessage(res.data));
                return;
            }
            var data = res.data || {};
            var skippedByKey = {};
            (data.skipped || []).forEach(function (s) { skippedByKey[s.rack] = s.reason; });

            var rows = racks.map(function (r) {
                var fits = !skippedByKey[r.key];
                var li = document.createElement("label");
                li.className = "list-group-item d-flex align-items-center gap-2";
                var cb = document.createElement("input");
                cb.type = "checkbox";
                cb.className = "form-check-input flex-shrink-0";
                cb.checked = fits;
                cb.disabled = !fits;
                cb.setAttribute("data-rd-apply-rack", r.domId);
                var text = document.createElement("span");
                text.className = "flex-grow-1";
                text.textContent = r.name;
                var status = document.createElement("span");
                status.className = "small " + (fits ? "text-muted" : "text-danger");
                status.textContent = fits
                    ? fitSummary(data[r.key])
                    : ("doesn't fit: " + skippedByKey[r.key]);
                li.appendChild(cb);
                li.appendChild(text);
                li.appendChild(status);
                // Gap 1 (T3.6): shown BEFORE the confirm click, same
                // "already_stamped" data the drag path warns from, so a
                // checklist re-application of the same template is visible
                // right here rather than only after the tiles land.
                var already = (data.already_stamped || {})[r.key];
                if (already && already.length) {
                    var warn = document.createElement("span");
                    warn.className = "small text-warning w-100";
                    warn.textContent = alreadyStampedNote(already);
                    li.appendChild(warn);
                }
                return li;
            });

            var dlg = buildModal(
                "Apply “" + template.name + "”",
                '<div class="list-group list-group-flush nbx-rd-apply-template-list"></div>',
                "Apply"
            );
            var listHost = dlg.overlay.querySelector(".nbx-rd-apply-template-list");
            rows.forEach(function (li) { listHost.appendChild(li); });

            dlg.overlay.querySelector("[data-rd-apply-template-confirm]").addEventListener("click", function () {
                var checked = Array.prototype.slice.call(
                    dlg.overlay.querySelectorAll('[data-rd-apply-rack]:checked')
                ).map(function (cb) { return cb.getAttribute("data-rd-apply-rack"); });
                if (!checked.length) { dlg.requestHide(); return; }
                Promise.all([
                    fetchDeviceTypeInfo(deviceTypeIdsIn(data)),
                    fetchDeviceBayNames(chassisDeviceTypeIdsIn(data)),
                ]).then(function (results) {
                    checked.forEach(function (domId) {
                        var r = racks.find(function (x) { return x.domId === domId; });
                        if (r) { stampRackEntries(domId, data[r.key], results[0], results[1]); }
                    });
                });
                dlg.requestHide();
            });
            dlg.show();
        });
    }

    // ---- Group dialog: one row per member template, each with ITS OWN -----
    // target rack select (D14: correspondence, not repetition). Each row's
    // preview runs independently (preview-template takes one template at a
    // time) -- a known consequence is that the naming pass's de-duplication
    // (D19's whole reason to batch it server-side) only covers ONE member's
    // devices at a time, not the whole group, so a rare cross-member name
    // collision is possible. It is not silent: every stamped tile still
    // carries its own live name_collision warning + editable name field, the
    // same surface a manual add's collision uses, so the planner sees and
    // can fix it before Save.
    function openGroupDialog(group) {
        setStatus("Loading group…");
        fetch(templatesUrl + "?group_id=" + encodeURIComponent(group.id), {
            credentials: "same-origin",
            headers: { "Accept": "application/json" },
        }).then(function (resp) {
            return resp.ok ? resp.json() : { results: [] };
        }).then(function (data) {
            setStatus("");
            var members = (data.results || []).slice().sort(function (a, b) {
                return (a.order || 0) - (b.order || 0);
            });
            if (!members.length) {
                createToast("info", "Template group", "This group has no templates yet.");
                return;
            }
            var racks = collectRackOptions();
            if (!racks.length) {
                createToast("info", "Template group", "This design has no racks yet.");
                return;
            }
            // T3.6 gap 2 (PLAN-templates.md §6 "Still open": "Group apply
            // when there are fewer target racks than members"). D14: a
            // TemplateGroup is an ORDERED correspondence, member 1 -> rack
            // A, member 2 -> rack B -- with fewer racks in the design than
            // the group has members, that correspondence cannot exist
            // without silently reusing a rack for more than one member,
            // which is exactly the "spine and storage collide" failure D14
            // exists to avoid. Refused HERE, before the dialog even opens,
            // for the same reason the server refuses the whole apply
            // (api/views.py's preview_template, "group" branch): the
            // failure to design against is a planner believing the WHOLE
            // pod landed when only part of it did, and a dialog that opens
            // showing (silently doubled-up) rows invites exactly that
            // read.
            if (racks.length < members.length) {
                var unmappedNames = members.slice(racks.length).map(function (m) {
                    return m.name;
                });
                createToast(
                    "danger", "Template group",
                    "This design has " + racks.length + " rack(s) but “" + group.name
                        + "” has " + members.length + " templates -- no target rack for: "
                        + unmappedNames.join(", ")
                        + ". Add more racks to this design before applying this group."
                );
                return;
            }

            var dlg = buildModal(
                "Apply group “" + group.name + "”",
                '<table class="table table-sm nbx-rd-apply-group-table">'
                + "<thead><tr><th>Template</th><th>Target rack</th><th>Fit</th></tr></thead>"
                + "<tbody></tbody></table>",
                "Apply"
            );
            var tbody = dlg.overlay.querySelector("tbody");
            // Per-row state: the last preview() result for that row's chosen
            // rack, kept so Apply doesn't have to re-fetch what a rack-select
            // change already fetched.
            var rowState = members.map(function () { return { data: null, key: null }; });

            var trs = members.map(function (member, idx) {
                var tr = document.createElement("tr");
                var nameTd = document.createElement("td");
                nameTd.textContent = member.name;
                var selectTd = document.createElement("td");
                var select = document.createElement("select");
                select.className = "form-select form-select-sm";
                racks.forEach(function (r, ri) {
                    var opt = document.createElement("option");
                    opt.value = r.domId;
                    opt.textContent = r.name;
                    // Default: one distinct rack per member, in order, so the
                    // common "N members, N target racks" group opens with a
                    // sensible correspondence already picked -- the planner
                    // is choosing/confirming a mapping, not building one from
                    // a blank select every row.
                    if (ri === idx % racks.length) { opt.selected = true; }
                    select.appendChild(opt);
                });
                selectTd.appendChild(select);
                var fitTd = document.createElement("td");
                fitTd.className = "small text-muted";
                fitTd.textContent = "…";
                tr.appendChild(nameTd);
                tr.appendChild(selectTd);
                tr.appendChild(fitTd);

                function recompute() {
                    var domId = select.value;
                    var key = racks.find(function (r) { return r.domId === domId; }).key;
                    fitTd.textContent = "…";
                    fitTd.className = "small text-muted";
                    previewTemplate(member.id, [key]).then(function (res) {
                        if (!res.ok) {
                            rowState[idx] = { data: null, key: key };
                            fitTd.textContent = previewErrorMessage(res.data);
                            fitTd.className = "small text-danger";
                            return;
                        }
                        var data = res.data || {};
                        rowState[idx] = { data: data, key: key };
                        var skipped = (data.skipped || []).find(function (s) { return s.rack === key; });
                        // Gap 1 (T3.6): the same "already_stamped" note the
                        // single-template dialog shows, per row, so a group
                        // re-application onto a rack that already carries
                        // this member is visible BEFORE Apply here too.
                        var already = (data.already_stamped || {})[key];
                        var alreadyText = (already && already.length)
                            ? (" -- " + alreadyStampedNote(already)) : "";
                        if (skipped) {
                            fitTd.textContent = "doesn't fit: " + skipped.reason;
                            fitTd.className = "small text-danger";
                        } else {
                            fitTd.textContent = fitSummary(data[key]) + alreadyText;
                            fitTd.className = "small " + (alreadyText ? "text-warning" : "text-muted");
                        }
                    });
                }
                select.addEventListener("change", recompute);
                recompute();
                return tr;
            });
            trs.forEach(function (tr) { tbody.appendChild(tr); });

            dlg.overlay.querySelector("[data-rd-apply-template-confirm]").addEventListener("click", function () {
                var ids = [];
                var chassisIds = [];
                rowState.forEach(function (row) {
                    if (row.data) {
                        ids = ids.concat(deviceTypeIdsIn(row.data));
                        chassisIds = chassisIds.concat(chassisDeviceTypeIdsIn(row.data));
                    }
                });
                Promise.all([
                    fetchDeviceTypeInfo(ids),
                    fetchDeviceBayNames(chassisIds),
                ]).then(function (results) {
                    rowState.forEach(function (row) {
                        if (!row.data || !row.key) { return; }
                        var skipped = (row.data.skipped || []).some(function (s) { return s.rack === row.key; });
                        if (skipped) { return; }
                        var r = racks.find(function (x) { return x.key === row.key; });
                        if (r) { stampRackEntries(r.domId, row.data[row.key], results[0], results[1]); }
                    });
                });
                dlg.requestHide();
            });
            dlg.show();
        }).catch(function () {
            setStatus("");
            createToast("danger", "Template group", "Could not load this group's templates.");
        });
    }

    // ---- Card rendering + drag wiring --------------------------------------
    function buildCard(kind, obj) {
        var card = document.createElement("div");
        card.className = "list-group-item list-group-item-action nbx-rd-template-item";
        card.setAttribute("data-rd-template-kind", kind);
        card.setAttribute("data-rd-template-id", obj.id);
        var title = document.createElement("div");
        title.className = "nbx-rd-template-name";
        title.textContent = obj.name;
        card.appendChild(title);
        if (kind === "group") {
            var badge = document.createElement("span");
            badge.className = "badge text-bg-secondary ms-1";
            badge.textContent = "group";
            title.appendChild(badge);
        }
        if (obj.description) {
            var desc = document.createElement("div");
            desc.className = "nbx-rd-template-meta text-muted small";
            desc.textContent = obj.description;
            card.appendChild(desc);
        }
        // Only a single template drags directly onto a rack (D24): a group's
        // correspondence needs the mapping dialog, so it is click-only --
        // making it draggable too would invite dropping it on one rack and
        // silently picking just its first member, which is exactly the
        // "spine ends up in the storage rack" failure D14 exists to avoid.
        if (kind === "template") {
            card.setAttribute("draggable", "true");
            card.addEventListener("dragstart", function (event) {
                dragPayload = { id: obj.id, name: obj.name };
                event.dataTransfer.effectAllowed = "copy";
                // Firefox refuses to start a native drag with no data set.
                event.dataTransfer.setData("text/plain", obj.name);
            });
            card.addEventListener("dragend", function () { dragPayload = null; });
        }
        card.addEventListener("click", function () {
            if (kind === "group") { openGroupDialog(obj); }
            else { openSingleTemplateDialog(obj); }
        });
        return card;
    }

    var dragPayload = null;   // {id, name} of the template card currently mid-drag

    // Every rendered rack block is a drop target. Wired once at setup time --
    // the same set of blocks initRack already walked, so no MutationObserver
    // is needed (a rack block never appears without a full page reload).
    Array.prototype.slice.call(document.querySelectorAll(".nbx-rd-rack-block"))
        .forEach(function (block) {
            block.addEventListener("dragover", function (event) {
                if (!dragPayload) { return; }
                event.preventDefault();
                event.dataTransfer.dropEffect = "copy";
            });
            block.addEventListener("drop", function (event) {
                if (!dragPayload) { return; }
                event.preventDefault();
                var domId = block.getAttribute("data-rack-id");
                applyTemplateToOneRack(dragPayload.id, dragPayload.name, domId);
                dragPayload = null;
            });
        });

    // ---- Load + render the list ---------------------------------------------
    function render(templates, groups) {
        listEl.innerHTML = "";
        if (!templates.length && !groups.length) {
            setStatus("No templates yet.");
            return;
        }
        setStatus("");
        groups.forEach(function (g) { listEl.appendChild(buildCard("group", g)); });
        templates.forEach(function (t) { listEl.appendChild(buildCard("template", t)); });
    }

    setStatus("Loading…");
    Promise.all([
        fetch(templatesUrl + "?limit=200", {
            credentials: "same-origin", headers: { "Accept": "application/json" },
        }).then(function (r) { return r.ok ? r.json() : { results: [] }; }).catch(function () {
            return { results: [] };
        }),
        fetch(templateGroupsUrl + "?limit=200", {
            credentials: "same-origin", headers: { "Accept": "application/json" },
        }).then(function (r) { return r.ok ? r.json() : { results: [] }; }).catch(function () {
            return { results: [] };
        }),
    ]).then(function (results) {
        // Only GROUP-LESS templates get their own top-level card -- a
        // template that belongs to a group is reached through the group's
        // own apply dialog (D14), never twice over.
        var templates = (results[0].results || []).filter(function (t) { return !t.group; });
        var groups = results[1].results || [];
        render(templates, groups);
    }).catch(function () {
        setStatus("Could not load templates.");
    });
}

// ---- "Save rack as template" (PLAN-templates.md Sec 4, D18/D32, T4.2) ------
// One button per rack block (rack_block.html, editable only): asks for a
// name (and optionally a description/group), then POSTs to the from-design
// endpoint -- see that action's own docstring (api/views.py) for exactly
// what it captures; this button does not re-derive any of it. A PLANNED
// rack is refused there (D32: it has no devices, so it could only ever
// produce an empty template) -- the button stays enabled and just shows
// whatever message the server sends back, rather than re-deriving the rule
// here and risking it drifting from the one the server actually enforces.
export function setupSaveAsTemplate(root) {
    var templatesUrl = root.getAttribute("data-templates-url")
        || "/api/plugins/rack-design/templates/";
    var fromDesignUrl = templatesUrl.replace(/\/?$/, "/") + "from-design/";
    var templateGroupsUrl = root.getAttribute("data-template-groups-url")
        || "/api/plugins/rack-design/template-groups/";
    var designId = parseInt(root.getAttribute("data-design-id"), 10);

    // Field-keyed 400s (design/rack/group_id/name) render the same way
    // previewErrorMessage (above) renders preview-template's -- kept as its
    // own small copy since the two setup functions share no closure.
    function errorMessage(data) {
        if (!data) { return "Could not reach the server."; }
        var parts = [];
        Object.keys(data).forEach(function (k) {
            var v = data[k];
            parts.push(k + ": " + (Array.isArray(v) ? v.join(" ") : v));
        });
        return parts.length ? parts.join(" ") : "The template could not be saved.";
    }

    function postFromDesign(body) {
        return fetch(fromDesignUrl, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": getCsrfToken(),
            },
            body: JSON.stringify(body),
        }).then(function (resp) {
            return resp.text().then(function (text) {
                var data = null;
                try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
                return { ok: resp.ok, data: data };
            });
        }).catch(function () { return { ok: false, data: null }; });
    }

    function openDialog(domId, rackLabel) {
        var bodyHtml =
            '<div class="mb-2">'
            + '<label class="form-label small mb-1">Name</label>'
            + '<input type="text" class="form-control form-control-sm" data-rd-sat-name maxlength="100">'
            + "</div>"
            + '<div class="mb-2">'
            + '<label class="form-label small mb-1">Description (optional)</label>'
            + '<textarea class="form-control form-control-sm" data-rd-sat-description rows="2"></textarea>'
            + "</div>"
            + '<div class="mb-2">'
            + '<label class="form-label small mb-1">Group (optional)</label>'
            + '<select class="form-select form-select-sm" data-rd-sat-group>'
            + '<option value="">— none —</option>'
            + "</select>"
            + "</div>";
        var dlg = buildModal("Save “" + rackLabel + "” as template", bodyHtml, "Save");
        var nameInput = dlg.overlay.querySelector("[data-rd-sat-name]");
        var descInput = dlg.overlay.querySelector("[data-rd-sat-description]");
        var groupSelect = dlg.overlay.querySelector("[data-rd-sat-group]");

        // The group list is a convenience for the dropdown, not a
        // prerequisite for saving -- a failed fetch just leaves the picker
        // at "none", it never blocks the dialog from opening.
        fetch(templateGroupsUrl + "?limit=200", {
            credentials: "same-origin", headers: { "Accept": "application/json" },
        }).then(function (r) { return r.ok ? r.json() : { results: [] }; }).then(function (data) {
            (data.results || []).forEach(function (g) {
                var opt = document.createElement("option");
                opt.value = g.id;
                opt.textContent = g.name;
                groupSelect.appendChild(opt);
            });
        }).catch(function () { /* see comment above */ });

        dlg.overlay.querySelector("[data-rd-apply-template-confirm]").addEventListener("click", function () {
            var name = (nameInput.value || "").trim();
            if (!name) {
                nameInput.classList.add("is-invalid");
                nameInput.focus();
                return;
            }
            var body = { design: designId, rack: rackKeyForApi(domId), name: name };
            var description = (descInput.value || "").trim();
            if (description) { body.description = description; }
            if (groupSelect.value) { body.group_id = parseInt(groupSelect.value, 10); }

            postFromDesign(body).then(function (res) {
                if (!res.ok) {
                    // Includes the backend's dedicated PlannedRack-refusal
                    // message (from_design's docstring) verbatim -- never a
                    // generic "could not save" that hides WHY.
                    createToast("danger", "Save as template", errorMessage(res.data));
                    return;
                }
                var data = res.data || {};
                createToast(
                    "success", "Save as template",
                    "“" + name + "” saved (" + (data.placement_count || 0) + " device"
                        + (data.placement_count === 1 ? "" : "s") + ")."
                );
                // Warnings (D32: islands, a blade whose real chassis sits in
                // a different rack) name devices this template could NOT
                // represent -- each gets its OWN toast rather than being
                // folded into the success message, where a quick skim could
                // miss it, and never swallowed.
                (data.warnings || []).forEach(function (w) {
                    createToast("warning", "Save as template", w);
                });
            });
            dlg.requestHide();
        });
        dlg.show();
    }

    Array.prototype.slice.call(root.querySelectorAll("[data-rd-save-as-template-btn]"))
        .forEach(function (btn) {
            btn.addEventListener("click", function () {
                var block = btn.closest(".nbx-rd-rack-block");
                if (!block) { return; }
                var domId = block.getAttribute("data-rack-id");
                var titleEl = block.querySelector(".nbx-rd-rack-block-title a");
                var rackLabel = titleEl ? titleEl.textContent.trim() : ("Rack " + domId);
                openDialog(domId, rackLabel);
            });
        });
}
