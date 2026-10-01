# Order list

Once this ships, the dashboard's low-stock flag becomes a list you can act on. A new Order list page suggests every item at or below its minimum, with a quantity to order. You can add any other item by hand, and push a project's shortages in from the project page. After you place an order, you tick the entries and mark them ordered, with the supplier, order number and expected date. The item page then shows them as "on order", and they leave the suggestions. When the parcel arrives, one tap on Received records the stock movement and closes the entry. You can export the list as a CSV, or as Mouser part-list text. Tally needs no distributor account or API key: the exports are plain files and text you can take to any distributor.

| Status | Research score | Depends on |
| --- | --- | --- |
| Proposed | 23/30, ranked 5 of 8 | Nothing new |

## Why

- [Part-DB #938](https://github.com/Part-DB/Part-DB-server/issues/938): users have no view of items below minimum. They want a filterable list they can export as CSV for supplier carts.
- [Part-DB #217](https://github.com/Part-DB/Part-DB-server/issues/217): asks for a "waiting to arrive" status for ordered parts, kept apart from available stock.
- [Part-DB #1427](https://github.com/Part-DB/Part-DB-server/issues/1427): asks for an ordering helper that combines what is needed, exports CSV, tracks order numbers and updates stock on receipt.
- [InvenTree #2946](https://github.com/inventree/InvenTree/issues/2946): "Just in time purchasing", which suggests what to order from low stock.
- [InvenTree discussion #2668](https://github.com/inventree/InvenTree/discussions/2668): users complain about clicking the cart on hundreds of rows one by one. The maintainer wants an "Order Required Parts" button.
- [Mouser BOM help](https://www.mouser.com/en/help/tools/how-to-create-a-new-bom): Mouser's part-list paste takes `PARTNUMBER|QTY` lines.

It fits Tally because it reuses what already exists: the low-stock predicate in `dashboard()`, the shortfall from `project_plan()`, `record_movement()` for receiving and `export_rows()` for the CSV. All it adds is one small table.

## What it does

1. **Order list** is a new entry in the sidebar's Work group. The page has three sections: Suggested, To order and On order.
2. **Suggested** lists every item where `quantity <= minimum_quantity` that has no To order or On order entry. Each row shows on hand, the minimum, the storage location code and a suggested quantity that brings stock up to twice the minimum, rounded up and never less than 1. Each row has a checkbox and an editable quantity. **Add selected** turns the ticked rows into order entries.
3. **Add an item**: a search box (the same match as `find_stock()`) adds any item with a quantity. The item page gets an **Add to order list** button.
4. **Project shortages**: the project page gets **Add shortages to order list** next to Build. It adds what ×N is short, less anything already on order.
5. **To order** lists the open entries. You can change the quantity or remove each one. Tick entries, fill in the supplier, order number and expected date, and press **Mark ordered**. This works on a phone with no script.
6. **Exports** (when the `exports` feature is on) cover the To order entries:
   - a CSV with part number, quantity, customer reference (the storage location code), manufacturer and component name
   - Mouser part-list text in `PARTNUMBER|QTY` form, in a read-only text area you can copy. It uses the item's manufacturer part number.
7. **On order** lists the ordered entries, grouped by supplier, with the order number and the expected date. Entries past their expected date get a "Late" badge. Each entry has a **Received** form with the quantity filled in.
8. **Item page**: while an item has entries on order, it shows "On order: 50 pcs, expected 12 Oct", which is the total and the earliest expected date.
9. **Dashboard**: low items stay in the Low stock card and in its count, because they really are low. Those on order carry an "On order" badge.

**On a phone at the bench:** the dashboard's Low stock card links to Order list. Tick the suggestions, tap Add selected, and export later at the desk. When the parcel arrives, open Order list and tap Received on each entry. The flash message names the drawer code, as Quick add does, so the parts go straight home.

**Delivery.** Build this as two pull requests. PR 1: the table, the Order list page, adding from Suggested, search and the item page, Mark ordered, Received, the CSV export, and the item and dashboard badges. PR 2: Add shortages from the project page, the Mouser text and Copy button, the `order.added` webhook and the reports card.

## Out of scope

- Free-text entries for parts that are not yet items. Add the item first with `new_item`.
- A Digi-Key FastAdd cart link. FastAdd needs Digi-Key part numbers, and items only record them once the scan spec's `items.supplier_sku` ships (see Open questions). Digi-Key users can take the CSV instead.
- Distributor APIs, prices, stock checks, accounts and automatic ordering.
- More than one supplier or supplier SKU per item, and price comparison.
- Backorders and partial deliveries that leave a remainder open.
- Purchase order PDFs, budgets and approvals.
- LCSC and other upload formats until someone checks their columns.

## Design

### Data

Add the table and index to the `CREATE TABLE IF NOT EXISTS` script in `init_db()`:

```sql
CREATE TABLE IF NOT EXISTS order_entries (
  id INTEGER PRIMARY KEY,
  item_id INTEGER NOT NULL REFERENCES items(id),
  quantity REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'wanted',   -- wanted | ordered | received
  source TEXT NOT NULL DEFAULT '',         -- 'Low stock', 'Blinky ×3', 'Added by hand'
  supplier TEXT NOT NULL DEFAULT '',
  order_ref TEXT NOT NULL DEFAULT '',
  expected_on TEXT,                        -- ISO date or NULL
  created_at TEXT NOT NULL,
  ordered_at TEXT,
  received_at TEXT,
  received_quantity REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS order_entries_one_wanted ON order_entries(item_id) WHERE status='wanted';
```

**How existing databases migrate.** `init_db()` runs before every request, so an existing database gets the empty table on its first request after the upgrade. No existing column changes, so v1 needs no `PRAGMA table_info` / `ALTER` step. Any later item column (see Open questions) follows the `item_columns` check in `init_db()`.

The partial index allows one To order entry per item. Adding again uses `ON CONFLICT(item_id) WHERE status='wanted' DO UPDATE`. After Mark ordered, an item can have a new To order entry and more than one On order entry.

New helpers in `app/main.py`:

- `low_stock(conn)` holds the predicate `dashboard()` uses today. Both pages call it.
- `suggested_order_quantity(item)` returns `max(1, math.ceil(2 * minimum - quantity - TOLERANCE))`, reusing `TOLERANCE` so float noise does not round 5.0000000001 up to 6.
- `mouser_text(entries)` builds the paste text (PR 2).

`feature_context()` gains `exports_enabled: enabled('exports')`. The QR labels spec adds the same key; whichever ships first adds it. The `/api/version` query gains `UNION ALL` branches over `order_entries.created_at`, `ordered_at` and `received_at`, so the live dashboard picks up the badges.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/orders` | New `order_list`: the three sections, plus `?q=` search with `find_stock()`. Builds the Mouser text only when `enabled('exports')`. |
| POST | `/orders` | New `add_order_entries`: reads `request.form.getlist('item_id')` and, for each, `quantity_<id>`. Each quantity must be finite and above 0, and each item must exist, or nothing is written. |
| POST | `/orders/<int:entry_id>` | New `update_order_entry`: `UPDATE ... WHERE id=? AND status='wanted'`. |
| POST | `/orders/<int:entry_id>/delete` | New `delete_order_entry`: `DELETE ... WHERE id=? AND status='wanted'`. |
| POST | `/orders/ordered` | New `mark_ordered`: `UPDATE ... SET status='ordered', supplier=?, order_ref=?, expected_on=?, ordered_at=? WHERE id IN (...) AND status='wanted'`. Flashes how many rows actually changed. |
| POST | `/orders/<int:entry_id>/receive` | New `receive_order_entry`: inside `BEGIN IMMEDIATE`, `UPDATE ... SET status='received', received_at=?, received_quantity=? WHERE id=? AND status='ordered'` is the guard, then `record_movement(conn, item, qty, "Order received", "stock.in")` with the item joined to its location. Follows `undo_build()`. |
| POST | `/projects/<int:project_id>/order-shortages` | New `order_shortages` (PR 2): reads `quantity` with `builds_wanted()`, runs `project_plan()`, subtracts open ordered quantity per item, merges the rest, and returns through `back_to_project()`. |
| GET | `/orders/export.<format>` | New `export_orders`: `csv` through `export_rows()`, and `txt` for Mouser text (PR 2). Add it to the `restricted` map in `ensure_database()` under `exports`. |

Reused routes: `item_detail`, `project_detail` and `dashboard` (they only gain buttons and badges), and `add_webhook` (it gains one event).

### Pages

- **New `templates/orders.html`.** Uses `page-head`, `page-actions`, `card`, `card-head`, `section-head`, `record-list`, `record`, `record-actions`, `table-wrap` with `td data-label`, `inline-qty`, `badge` (`warn` for Late, `neutral` for source), `button` with `small`, `quiet`, `primary` and `danger`, `button-group`, `form-grid`, `check`, `empty`, plus the `qty` and `icon` macros from `templates/_macros.html`. Rows sit in tables, so inputs use the HTML `form=` attribute: Suggested rows carry `name="item_id" value="{{id}}"` and `name="quantity_{{id}}"` with `form="add-suggested"`, and To order checkboxes use `form="mark-ordered"`. The search results and the item page post the same names for one item. The Received form uses a `form details` disclosure. Wrap exports and the Mouser text area in `{% if exports_enabled %}`. Do not set `data-live`, because the page holds inputs.
- **`templates/base.html`.** Add `('order_list', 'Order list', 'cart')` to the Work group in `nav_groups`. The `i-cart` symbol already exists. Leave `tabs` at five.
- **`templates/item_detail.html`.** Add **Add to order list** to `detail-actions`, and the on-order line to the `quantity-panel`.
- **`templates/dashboard.html`.** Add an Order list link to the Low stock `card-head`, and an "On order" `badge` on rows that are on order.
- **`templates/project_detail.html`** (PR 2). Inside `build-form`, add a `button` with `formaction="{{url_for('order_shortages', project_id=...)}}"` and `data-order-shortages`. Do not add `keep_count`, because the form's own `quantity` input carries the count, as Check stock does. Render it whenever `plan` has lines, disabled when `short_lines` is empty.
- **`templates/settings.html`.** Add the new event to the webhook select.
- **`templates/reports.html`.** Add a `report-card` for the order list exports, guarded by `exports_enabled`.

### Script

- The build planner's `update()` in `static/app.js` sets `disabled` on `[data-order-shortages]` from the same `short` flag it uses for the Build button.
- A small `[data-copy]` handler adds a Copy button next to the Mouser text area. The button is rendered `hidden` and revealed only when `window.isSecureContext && navigator.clipboard`. Tally is often served over plain HTTP on the LAN, where the API is missing, so there the text area stays selectable by hand.
- Bump the `v=` query on the `app.js` script tag in `templates/base.html` (and on `app.css` if styles change).

No third-party library.

### Webhooks

- **New:** `order.added`, sent once per request with the added entries (item id, name, part number, location code, quantity, source). Add it to `events` in `add_webhook()` and to the select in `settings.html`.
- **Reused:** `stock.in` on receipt, which `record_movement()` sends anyway.

## Edge cases

- With a minimum of 0 and 0 on hand, the item is low and the suggestion is 1.
- An item restocked some other way drops out of Suggested. Its To order entry stays and shows the current on-hand quantity. Stock received through Quick add, including a scanned bag label, does not close an On order entry. Receive ordered parts with Received here, or they are counted twice.
- Adding an item that already has a To order entry replaces its quantity.
- Add shortages subtracts what is already on order for each item, then keeps the larger of the existing To order quantity and the remainder. Lines left at 0 or less are skipped. If nothing is left, it flashes "Nothing to add for ×N".
- Editing or deleting an entry that is no longer To order is refused with a flash. Mark ordered with nothing ticked flashes and writes nothing. A second Mark ordered on the same entries changes nothing.
- A blank or invalid `expected_on` (checked with `date.fromisoformat()`) is stored as NULL. Late compares against the UTC date, as the app's timestamps do. Nothing changes when an entry is late.
- A second tap on Received, or two gunicorn workers receiving at once, records one movement. The second request flashes "That entry was already received."
- A received quantity of 0, negative, `nan` or text is refused, and so is an unknown entry.
- Items with no part number still export to CSV with a blank part number. The Mouser text skips them, and the page lists what it skipped.
- With `exports` off, the export routes return 403. The page shows no export buttons, Mouser text or report card.

## Tests

New file `tests/test_orders.py`, with a `setUp` like `tests/test_builds.py` (temporary `DB_PATH`, `LAYOUT = "example"`, items created through `/items/new`). Here the items also get `minimum_quantity` and `part_number`.

PR 1:

- Low items appear in Suggested with `2 × minimum − on hand`. Items without a minimum do not. Minimum 0 with 0 on hand suggests 1. A fractional case (for example 2.5 m minimum, 0 on hand, suggests 5) is not rounded up.
- Ticking rows 1 and 3 of 3 gives each its own quantity. Adding an item twice leaves one `wanted` row with the later quantity.
- Invalid quantities (`0`, `-1`, `nan`, `abc`) and an unknown `item_id` flash an error and write nothing.
- Mark ordered sets supplier, order_ref, expected_on and `status='ordered'`. A bad date stores NULL. With no `entry_id` nothing is written. Editing or deleting an ordered entry is refused.
- The item page shows "On order" with the total of two ordered entries. A low item that is on order stays in the dashboard count and shows the badge. It leaves Suggested.
- Received adds a `movements` row with reason "Order received" and raises `items.quantity`. Posting again adds nothing.
- `test_receive_waits_for_a_concurrent_receive_and_then_records_nothing`, modelled on `test_undo_waits_for_a_concurrent_undo_and_then_returns_nothing`.
- The CSV has the header row and the location code as customer reference. With `feature.exports` set to `0`, the export returns 403 and `/orders` has no export links.
- `init_db()` run twice leaves one `order_entries` table and index.

PR 2:

- After `add_line(1, '2')` and `add_line(2, '3')`, posting `/projects/1/order-shortages` with `quantity=3` adds each shortfall. Posting again does not double. After the entries are marked ordered, posting again adds nothing. A count where nothing is short flashes and writes nothing.
- The `txt` export is `PN|QTY` lines and skips items with no part number. It is missing from `/orders` when exports are off.
- `order.added` reaches `dispatch_webhooks`, checked with `unittest.mock` as in `test_builds.py`. `add_webhook` accepts the event.

## Docs to update

- **README.md:** add an "Order list" feature bullet, and extend the "Low stock" bullet to mention it.
- **CONTRIBUTING.md:** no change.
- **CONTEXT.md:**
  - add **Order entry**: an item and a quantity to buy, which is to order, on order or received. _Avoid_: line (that means a bill of materials row), purchase order.
  - add **On order**: an order entry that has been placed but not yet received.

## Open questions

- Should the suggested quantity always be twice the minimum, or should items get a `reorder_quantity` column through the `item_columns` ALTER pattern?
- Should the order list use `items.supplier` and `items.supplier_sku` from the scan bag labels spec (`scan-bag-receive.md`)? They would allow a Digi-Key FastAdd link later: base `https://www.digikey.com/classic/ordering/fastadd.aspx` with `part1..N`, `qty1..N` and `cref1..N` built by `urllib.parse.urlencode`, with a POST form for long lists. There is no item edit page today, so a SKU is only set by a scan or on a new item.
- When less arrives than was ordered, should the entry close (as in v1) or keep the remainder on order?
- Should project shortages merge by the larger quantity (as proposed) or add to what is already there?
- Before PR 2 merges, someone must check Mouser's paste format, its delimiter, whether it accepts manufacturer part numbers and whether it needs a sign-in.
- Are `order.placed` and `order.received` events wanted too, or is `stock.in` enough?
