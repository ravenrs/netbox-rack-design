# Execution plan

▶ Video: [What's new in 1.2.0](https://youtu.be/8cgy2hknDYY)

The editor shows a design's **final** state. The work on site happens in an
order, and that order can overload a rack along the way even when the final
picture is fine. The **Execution plan** tab checks the work in the order you
will do it, one step at a time.

## The problem it solves

Example, with made-up numbers. R12 carries 6.5 kW of its 8 kW. The design adds
two servers of 1.3 kW each and removes an old device of 2.4 kW.

- The final state is 6.5 + 1.3 + 1.3 − 2.4 = **6.7 kW**. The editor shows green.
- If the team racks the two new servers first, R12 reaches 6.5 + 1.3 + 1.3 =
  **9.1 kW** before the old device leaves. Bank B2 trips.

The same happens with space. A new device can be placed in a unit that a
device removed in a later step still occupies. The final state is valid, but
the work is not possible in that order.

The plan checks every step on its own. Step 1 is simulated, then steps 1 and 2
together, and so on. A problem shows on the step where it happens.

## What it does not do

- **It does not change Apply.** Apply still writes the whole design at once,
  as it did before. A plan is a check and a checklist. Nothing in Apply reads
  the steps, and Apply does not wait for a plan to be green. See
  [Applying a design](apply.md).
- **It does not block anything.** A red step can be kept. The plan only shows
  the problem.
- **It writes nothing to NetBox devices or racks.** Simulation runs inside a
  transaction that is rolled back. Only the plan is saved: the design's steps,
  their titles, and the step of each action.

## Creating a plan

Open a design and choose the **Execution plan** tab. A design without a plan
shows **Create plan**.

**Create plan** makes one step per action, in the order the actions were
created in the editor. Each step is titled with the action, for example
`Add srv-101` or `Remove old-55`. This is the order the work is really done
in, so the first steps often show the hidden overload.

Actions without a step are in the **Unscheduled** tray at the top of the tab.
A new action starts in the tray.

If the design has no actions, the tab says so and offers no plan.

## Changing the order

All changes are drag and drop. Each change is saved right away (see
[Saving](#saving)).

| What you move | How |
|---|---|
| An action into another step | Drag the action onto that step, or onto a line between two steps. |
| An action into the tray | Drag it onto the **Unscheduled** tray. |
| An action into a new step | Drag it onto the gap between two steps. A new step is created there, titled after the action. |
| A whole step | Drag the **≡** handle on the step card, or focus the handle and press **Arrow up** or **Arrow down**. |

Rules for steps:

- **A step emptied by a drag disappears.** The exception is an action moved
  back to the tray: then the empty step stays. Delete it with its **×** button.
- **+ step** adds an empty step at the end. Only an empty step can be deleted.
- **The title** is free text, up to 100 characters. It is saved a moment after
  you stop typing, or when the field loses focus.

Keyboard: the arrow keys move a **whole step**. There is no keyboard way to
move a single action. Drag is the only way to do that.

## Auto-order

**Auto-order** asks the server for a safer order. The server simulates the
steps and reorders them with a greedy rule:

- Each step keeps its title and its actions. Steps are not merged.
- At each position the server takes the first step that adds no new problem
  on the racks it touches. If none is clean, it takes the one with the fewest
  new problems.
- A step that is still red and has several actions is split into single-action
  steps, and those are ordered too.
- Unscheduled actions join the plan as single-action steps.
- Removals go before moves, and moves before adds, when the rule has no other
  reason to choose.

The server makes at most 400 simulations per call. If it runs out, the rest of
the steps are sorted by kind only (removals, then moves, then adds).

Auto-order runs without a confirmation. The new order is saved and simulated
again. Press **Reset plan** to go back to the editor order.

## Reset plan

**Reset plan** rebuilds the plan the way **Create plan** does: one step per
action, in the editor's order. It asks for confirmation first. Your step order
and titles are lost.

## Saving

Every change is saved automatically. The status next to the toolbar shows:

- **Saving…** while a save is in flight.
- **Saved HH:MM** when the last save succeeded.
- **Not saved — Retry** when it failed. **Retry** sends the whole plan again.

A reload shows the last saved plan. Changes that were never saved are not on
the page after a reload.

Simulation is separate from saving. While a result is being computed, the
tab shows **Simulating…**. See [How a step is checked](#how-a-step-is-checked).

## How a step is checked

Each step card lists the racks its actions touch. For each rack the card
shows:

- **The power bar**: draw against capacity, in watts and percent, in the
  editor's colours. Yellow at the rack warning level (default 80%), red at the
  critical level (default 100%).
- **The bank chips**: one per PDU bank, grouped by PDU and feed leg, as in the
  editor's power heatmap.
- **Problem badges**: the codes of the problems found, with their detail.

Step *N* shows the racks after steps 1 to *N* are done, on top of the real
NetBox state. A cell turns grey after you drag, and it updates after a short
pause. A newer drag cancels a request that is still running.

Blades are not actions of their own. A blade is shown with its chassis and
follows the chassis' step. A stale placement is not shown in the tab.

### Step pills

The pill in each step header is the worst result of its racks.

| Pill | Colour | Shown when |
|---|---|---|
| **OK** | green | No problems on the step. |
| **Warning** | yellow | Only warnings, no errors. |
| **Overload** | red | An error of code `rack_over`, `bank_over` or `pdu_over`. |
| **Conflict** | red | An error of code `u_conflict` or `bay_conflict`. |
| the code | red | Any other error. It shows the first error code, for example `unpowered` or `engine_error`. |

When a step has both a conflict and an overload, the pill shows **Conflict**.
Open the problem badges to see both.

### Problem codes

| Code | Severity | Rule |
|---|---|---|
| `rack_over` | error | The rack's draw reaches its critical level (default 100% of capacity), or a distribution script reports a draw above the rack's power limitation. |
| `rack_near` | warning | The rack's draw is at or above its warning level (default 80%). |
| `bank_over` | error | A PDU bank is in the critical or overload state, the red states in the editor. |
| `bank_near` | warning | A PDU bank is in the warning state, the orange chip in the editor. |
| `pdu_over` | error | The banks of one PDU add up to more than the PDU's input draw. The editor has no red state for a whole PDU. |
| `u_conflict` | error | Two devices on the same face of a rack overlap in units, and at least one of them is planned in this design. A device that a later step removes or moves still occupies its units in the earlier steps, so a new device placed there is a conflict. |
| `bay_conflict` | error | A blade is planned into a chassis bay that is still occupied at that step. |
| `unpowered` | error | A PDU is removed while a device in the same rack is still cabled to it, and that device has no other live power path. |
| `redundancy_lost` | warning | A PDU is removed, and a device in the same rack still has one power path left (for example its second PSU on another PDU). |
| `engine_error` | error | The distribution engine failed. The detail is shown. The plan never hides a failed engine as "no data". |

A removed PDU does not supply power in the simulation. Its banks are not
counted, and a device cabled to it draws from its other paths only.

Problems are checked only for devices in the same rack as the removed PDU.

## Export

**Export** (on the tab) gives two files for the whole design. The button is
disabled when there is no plan.

- **Checklist (Markdown)**: a printable list. It has the design title, sites,
  status and NetBox link. Then one section per step, with its racks, the
  power after the step, its problems, and the actions as checkboxes. Each
  action lists from and to, serial and asset tag, power cabling, the status it
  leaves the device in, and a NetBox link. Blades are listed under their
  chassis. Unscheduled actions come last, in their own section.
- **Actions (CSV)**: one row per action, with these columns:
  `step`, `step_title`, `order`, `kind`, `device`, `type`, `from_rack`,
  `from_u`, `from_face`, `to_rack`, `to_u`, `to_face`, `pdu`, `outlet`, `bank`,
  `status_after`. Unscheduled actions have an empty `step`.

Both files come from the work-order API. See [API & integrations](api.md#execution-plan).

## Permissions

| Action | Permission on the design |
|---|---|
| See the tab, the steps and the Export | `view` |
| Simulate a proposed order, Auto-order (the call itself), work order and Export files | `view` |
| Save the plan (drag, titles, + step, delete step, Create plan, Reset plan) | `change` |
| Toolbar buttons (**+ step**, **Auto-order**, **Reset plan**) | `change`. Without it the toolbar is hidden, except **Export**. |

The compute-only calls (`simulate-steps`, `auto-order`) work with a read-only
API token, because they write nothing. See [API & integrations](api.md#execution-plan).

## Using it with Claude

The work order is also available to Claude through an MCP server. Claude can
read the plan, simulate a proposed order, and build a smart-hands ticket from
the work order. The server is read-only. Claude cannot change the plan.

See [Claude and MCP](mcp.md) for the setup.

## Limits

- **Peer designs are not in the simulation.** A step sees the real NetBox state
  plus this design only. Peer conflicts from other designs stay in the editor.
- **Only the racks an action touches are simulated.** Other racks keep their
  last result.
- **No cooling or thermal check.** Only power and space are checked.
- **The `unpowered` and `redundancy_lost` checks cover one rack.** A device in
  another rack that is cabled to a removed PDU is not checked.
- **No dates or calendar.** A step title is free text.
- **No "state at step N" in the editor.** Look at the plan to see a step.
- **Large plans take longer.** A plan of 20 steps on 2 racks can take several
  seconds to simulate. Only the changed steps and racks are simulated after a
  drag.
- **Auto-order only optimizes the checks above.** It does not know your site's
  rules, such as access windows or who must be present. Read the result before
  you use it.
