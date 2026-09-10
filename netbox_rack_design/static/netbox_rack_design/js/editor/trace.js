/*
 * Dev-only drag-lifecycle tracer -- extracted verbatim from editor.js.
 *
 * Kept as its own module because it is the editor's most-called helper (46 call
 * sites) and the one piece with no editor state behind it at all: it reads two
 * window flags and writes to the console.
 *
 * `rdGestureId` cannot be exported directly -- an importer cannot reassign an
 * imported binding -- so the per-gesture counter is bumped through
 * rdNextGesture() instead of the `rdGestureId += 1` that used to sit inline in
 * onDragStart.
 */

// ---- Dev-only drag-lifecycle tracer ------------------------------------
// Structured console logging of the whole grab->drop pipeline, so the exact
// branch that fires on any gesture can be watched live. Gated TWICE so it
// can never surface on a production deployment:
//   1. window.__rdDebugAvailable -- set by the editor template ONLY on a
//      developer build (Django DEBUG on, or the Debug Toolbar installed;
//      see DesignEditorView / design_editor.html). Absent in production, so
//      the tracer is a hard no-op there regardless of the runtime toggle.
//   2. the runtime toggle window.__rdDragTrace (persisted in localStorage
//      so it survives reloads) -- flip it from the console to start/stop.
// Every entry is also pushed to window.__rdDragLog with a per-gesture id so
// one grab->drop can be dumped together (rdTraceDump()).
var rdGestureId = 0;
function rdTraceEnabled() {
    if (!window.__rdDebugAvailable) { return false; }
    if (window.__rdDragTrace != null) { return !!window.__rdDragTrace; }
    try { return window.localStorage.getItem("rdDragTrace") === "1"; }
    catch (e) { return false; }
}
function rdTrace(ev, data) {
    if (!rdTraceEnabled()) { return; }
    var entry = { g: rdGestureId, ev: ev, data: data || {} };
    try { (window.__rdDragLog = window.__rdDragLog || []).push(entry); } catch (e) { /* noop */ }
    try {
        // eslint-disable-next-line no-console
        console.log("[rd-drag]#" + rdGestureId + " " + ev, data || {});
    } catch (e) { /* noop */ }
}
if (window.__rdDebugAvailable) {
    // Shared tracer for the OTHER editor scripts (power_heatmap.js,
    // editor_panels.js, ...): same __rdDragTrace toggle + __rdDragLog
    // buffer, so one flip covers every script. They call
    // window.__rdTrace("<script>.<event>", data) -- inert in production
    // (this whole block only runs on a dev build).
    window.__rdTrace = rdTrace;
    // Console conveniences so the toggle is discoverable without reading src.
    window.__rdDragTraceOn = function () {
        window.__rdDragTrace = true;
        try { window.localStorage.setItem("rdDragTrace", "1"); } catch (e) { /* noop */ }
        return "rd-drag tracer ON";
    };
    window.__rdDragTraceOff = function () {
        window.__rdDragTrace = false;
        try { window.localStorage.setItem("rdDragTrace", "0"); } catch (e) { /* noop */ }
        return "rd-drag tracer OFF";
    };
    window.rdTraceDump = function () { return window.__rdDragLog || []; };
    try {
        // eslint-disable-next-line no-console
        console.log(
            "[rd-drag] tracer available -- __rdDragTraceOn() / "
            + "__rdDragTraceOff() (or set window.__rdDragTrace); "
            + "rdTraceDump() for the buffer");
    } catch (e) { /* noop */ }
}

// Bump the per-gesture id. Called once per grab, at the top of onDragStart, so
// one grab->drop shares an id in the log and rdTraceDump() can be read per
// gesture.
function rdNextGesture() {
    rdGestureId += 1;
    return rdGestureId;
}

export { rdTrace, rdNextGesture };
