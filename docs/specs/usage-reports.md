# Usage reports and drawer layout suggestions

Once this ships, a **Usage** page shows which items you use most and which you never touch, over a period you choose. A second tab, **Rearrange**, suggests moving the busiest items into the drawers closest to your bench, and moving items you use together next to each other. It lists the moves as a checklist you tick off at the bench.

| Status | Research score | Depends on |
| --- | --- | --- |
| Proposed | Not scored | Nothing new |

## Why

- Tally records stock changes but not what you reach for. A perfboard or a breadboard can be the most used thing in the workshop and rarely change quantity, so stock movements alone miss the real habits.
- Permanent homes are only useful if the common parts are quick to reach. Cabinets grow by accident, so a resistor drawer ends up three cabinets from the bench.
- Stocktake and low stock say what you have. Nothing says what you actually use.

## What it does

1. **Usage** opens from the main navigation. A period selector offers 30 days, 90 days, 1 year and All time. The default is 90 days.
2. **Most used** lists the top 20 items by number of accesses in the period, with the last access, the quantity taken out and the current drawer.
3. **Least used** lists items with stock on hand and no access in the period, oldest last access first. Items never accessed show "Never".
4. Both lists can be grouped by component family or by storage group, and exported as CSV when exports are enabled.
5. An **access** is opening an item's page (from search, a list, a scanned label or a link) or any stock movement on it. Opening the same item again within a minute counts once. Initial stock, stocktake corrections and order receipts are not accesses, so counting a drawer does not make its parts look busy.
6. **Rearrange** ranks every item by use and proposes a new home for each item whose rank puts it in the wrong zone (see Design). Each proposal shows the item, the current drawer, the proposed drawer and the reason, for example "Used 14 times in 90 days, now in the 6th row of C3".
7. Nothing moves until you accept it. **Apply** on a proposal moves the item and writes a note in its history. **Mark as done** is the same action, for when you have already moved the parts by hand.
8. Items accessed within the same few minutes, such as when you start a build, count as **used together**. A perfboard, breadboards, jumper wires, an ESP32 and white LEDs that you pull out for most jobs end up as one cluster. Rearrange keeps frequently co-used items in the same cabinet and as close to the bench as it can. For example, if `C3 S01` is across the room and `C1 S25` is beside the bench, and you reach for both on most jobs, Rearrange proposes moving the `C3 S01` items near `C1 S25`.

## Out of scope

- Moving physical drawers or renumbering codes. Only item homes change.
- Automatic moves without confirmation.
- Forecasting demand.
- AI. Ranking and co-use are counting and sorting, so this feature needs no model. Choosing a home for a new part stays with Jev in Quick add, which suggests a family and drawer (a Pico goes with the microcontrollers).

## Design

### Data

One new table records accesses:

```sql
CREATE TABLE IF NOT EXISTS item_access (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE, accessed_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS item_access_item ON item_access(item_id, accessed_at);
```

`CREATE TABLE IF NOT EXISTS` is safe to run before every request, like the other tables in `init_db()`. Add `record_access(conn, item_id)` next to `record_movement()`. It skips the insert when the same item was accessed in the last minute. Call it from the item detail view, and from `record_movement()` for every reason except the ones listed as non-access in item 5 above. Keep those reasons in one constant, so a new reason cannot silently count as use. Timestamps are full UTC ISO strings, like every other time Tally stores. Existing history is not backfilled, so the page says "Collecting data since <date>" until a period is covered.

The report is a query over `item_access`:

```sql
SELECT item_id, count(*) AS accesses, max(accessed_at) AS last_accessed
FROM item_access WHERE accessed_at >= ? GROUP BY item_id
```

Quantity taken out comes from outgoing `movements` in the same period.

Rearrange needs to know how far each storage group is from the bench. Add one setting, stored in `settings`:

- `bench_distance`: for each storage group (a cabinet or box set, such as `C1`), a number from 1 (at the bench) upwards, edited in a new Settings section. Within a group, drawers rank by `drawer_order(row)`. Groups with no value count as farthest.

Co-use comes from `item_access`. Sort accesses by `accessed_at` and start a new use group whenever two accesses are more than 5 minutes apart. Each group is one job. For every pair of distinct items in a group, add 1 to their co-use count.

### Placement

Placement is deterministic and does not need a model:

1. Rank items by accesses over the chosen period.
2. Build clusters: items joined by a co-use count of at least 3 in the period form one cluster. A cluster is placed as a unit where it fits.
3. Rank locations by `bench_distance`, then `drawer_order(row)`. Assign clusters, busiest first, to the nearest locations with room, filling a cabinet before moving to the next so a cluster stays together. Locations are never over capacity: an item goes only to a location whose kind (small, medium, large) fits it.
4. Emit a proposal only when the assigned location differs from the current one and the move is worth it. The benefit is the item's access count multiplied by the drop in `bench_distance`, plus the co-use count multiplied by the drop in distance to its partners. Proposals below a minimum benefit are dropped, so the list does not churn for marginal gains.

Where a target location is full, the proposal is a swap with its least used item, so any set of proposals can be applied in any order without losing track of an item.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/usage` | New. Most and least used for `?days=`. |
| GET | `/usage.csv` | New. Same data as CSV, behind `exports_enabled`. |
| GET | `/usage/rearrange` | New. Lists proposals. |
| POST | `/usage/rearrange/<int:item_id>` | New. Applies one proposal, rechecking that the item is still where the page said. |

### Webhooks

Applying a proposal emits the existing item-updated event. No new event.

## Edge cases

- No accesses in the period: both lists show a plain empty state and Rearrange says there is not enough data to suggest anything. Rearrange needs at least 20 accesses in the period before it proposes moves.
- Fresh install with imported stock: initial stock is not an access and is ignored.
- Item deleted or moved since the page loaded: applying a stale proposal returns to the list with a message and changes nothing.
- A target drawer filled between page load and apply: reject, do not overfill.
- No `bench_distance` set: Rearrange asks you to set it first and proposes nothing.
- Two jobs close together in time merge into one use group. Accepted, because the 5 minute window is a heuristic.

## Tests

- Usage query ignores receipts, returns, stocktake and order receipts, and nets out undone builds.
- Period selector bounds, default period, and the empty state.
- Placement puts the most used item in the best location, keeps capacity, and skips moves under the rank gap.
- Applying a proposal moves the item once, is safe to repeat and rejects a stale page.
- Opening an item records one access, repeats within a minute are ignored, and a stocktake correction records none.
- Accesses within 5 minutes become a use group, and pairs outside that window do not.
- A co-used cluster lands in one cabinet, and the busiest cluster gets the cabinet nearest the bench.
- CSV export is gated by `exports_enabled`.

## Docs to update

- `README.md`: add Usage reports to Features.
- `CONTEXT.md`: define **Usage** (quantity taken out in a period) and **Proposed move**.
- `docs/specs/README.md`: add this spec to the table.

## Open questions

1. Is a distance per storage group enough, or do drawer heights within a cabinet matter too (eye level against the floor)?
2. Is 5 minutes the right window for "used together"?
3. Should scanning a drawer label count as an access for every item in that drawer, or only opening an item does?
