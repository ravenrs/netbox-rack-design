"""Small helpers shared by the e2e suites.

``pick_role`` exists because a palette drop now REQUIRES a role: dcim.Device.role
is mandatory, so the editor refuses an add while the toolbar's Role select is
empty (an add without one could never be applied). Every suite that drops from
the palette therefore picks a role first, exactly as a planner must.
"""

# Sets the toolbar Role select to a real device role. The select is a NetBox
# dynamic (TomSelect) field whose options only load on open, so the option is
# added directly; the editor reads nothing but the underlying <select>'s value
# and selected option text at drop time (rack.js finishAdd), which is what this
# sets. Returns the role id, or null when the deployment has no role at all.
_PICK_ROLE_JS = """async (slug) => {
    const q = slug ? ('?slug=' + encodeURIComponent(slug)) : '?limit=1';
    const r = await fetch('/api/dcim/device-roles/' + q, {credentials: 'same-origin'});
    if (!r.ok) { return null; }
    const role = ((await r.json()).results || [])[0];
    const sel = document.getElementById('id_device_role');
    if (!role || !sel) { return null; }
    let opt = [...sel.options].find(o => o.value === String(role.id));
    if (!opt) {
        opt = new Option(role.name, String(role.id), true, true);
        sel.add(opt);
    }
    sel.value = String(role.id);
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return role.id;
}"""

# Empties the Role select again, for tests of the refusal itself.
_CLEAR_ROLE_JS = """() => {
    const sel = document.getElementById('id_device_role');
    if (!sel) { return false; }
    sel.value = '';
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return true;
}"""


def pick_role(page, slug=None):
    """Select a device role in the editor toolbar. ``slug`` picks a specific
    role; without it, any role the deployment has."""
    return page.evaluate(_PICK_ROLE_JS, slug)


def clear_role(page):
    """Leave the editor toolbar's Role select empty."""
    return page.evaluate(_CLEAR_ROLE_JS)


# Runs on every page the context opens, before the page's own scripts: once
# the DOM is there, pick a role if the editor's Role select is empty. The fetch
# is a network request, so a test's `goto(..., wait_until="networkidle")` does
# not return until it has settled -- the role is in place before any drop.
_ROLE_AUTOPICK_INIT = """(() => {
    if (window.__rdRoleAutopick) { return; }
    window.__rdRoleAutopick = true;
    document.addEventListener('DOMContentLoaded', async () => {
        const sel = document.getElementById('id_device_role');
        if (!sel || sel.value) { return; }
        try {
            const r = await fetch('/api/dcim/device-roles/?limit=1',
                                  {credentials: 'same-origin'});
            const role = ((await r.json()).results || [])[0];
            if (!role) { return; }
            let opt = [...sel.options].find(o => o.value === String(role.id));
            if (!opt) {
                opt = new Option(role.name, String(role.id), true, true);
                sel.add(opt);
            }
            sel.value = String(role.id);
        } catch (e) { /* no role API: the drop will say so itself */ }
    });
})();"""


def install_role_autopick(context):
    """Make every editor page this browser context opens start with a role
    picked, as a planner would before dropping anything. For suites whose
    subject is something else (moves, trays, sweeps) and that drop from the
    palette along the way."""
    context.add_init_script(_ROLE_AUTOPICK_INIT)
