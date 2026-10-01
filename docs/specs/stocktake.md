# Stocktake: count a storage location

Once this ships, you can stand at a drawer or box with your phone, open its storage location in Tally and count what is really there. Each item shows its expected on-hand quantity, a large number field and a **Matches** tick. When you save, Tally writes one stock movement for each difference, with the reason "Stocktake". It also records when each item and the storage location were last counted. You can walk a whole group such as `C1` one storage location at a time. The dashboard lists the storage locations that are due a count, so the database stays true to what is in the drawers.

| Status | Research score | Depends on |
| --- | --- | --- |
| Proposed | 21/30, ranked 7 of 8 | Nothing new |

## Why

- [InvenTree #4034](https://github.com/inventree/InvenTree/issues/4034): the maintainer's own stocktake design. Count per location, record the date, know when stock was last checked.
- [InvenTree #1694](https://github.com/inventree/InvenTree/issues/1694): the original "Feature: Stocktake" request, later built in PR #4345.
- [InvenTree stocktake docs](https://docs.inventree.org/en/latest/part/stocktake/): a mature tool ships a "Count Stock" action.
- [Homebox #113](https://github.com/sysadminsmedia/homebox/issues/113): a request to find items not checked recently, now shipped.
- [EEVblog forum thread](https://www.eevblog.com/forum/chat/electronic-componentsstash-inventory-system/): people give up on parts databases because they are "too much work to keep updated".
- [Hackaday on Binner](https://hackaday.com/2025/04/16/binner-makes-workshop-parts-organization-easy/): the upkeep sends people back to spreadsheets.

It fits Tally because every item already has a permanent home and every stock change already goes through `record_movement()`.

## What it does

1. Under **Storage**, open a group and expand a storage location. Its action row gains **Count stock here**.
2. The count page lists every item in that storage location by name. Each row shows the part number, the expected quantity with its unit, a number field and a **Matches** tick.
3. At the bench on a phone, the rows stack into cards. Tick **Matches** for each item that agrees, and type the real count for each one that does not. Leave a row blank if you did not count it.
4. **Save count** checks every row first. If one is wrong, the page comes back with everything you typed and the bad row marked. Otherwise Tally writes one stock movement per difference (counted minus on hand, reason "Stocktake") and records the count time on each counted item. The storage location is stamped only when every item in it was counted.
5. A flash sums it up, for example "Counted 6 items in C1 S03: 2 differences recorded."
6. From a group, **Count this group** starts walk mode at the first storage location that holds items. The buttons read **Save and next location**, **Skip** and **Stop counting**. Each save is kept even if you stop halfway. Walk mode passes over empty storage locations.
7. An empty storage location shows "Nothing should be here" and **Confirm empty**, which stamps it.
8. The item page shows "Last counted 2026-09-14" or "Never counted" under the on-hand figure. Stocktake movements appear in its history like any other.
9. The dashboard gains a **Due a count** card. It lists storage locations holding an item that was never counted or not counted for 180 days, oldest first, and each one links to its count page.

## Out of scope

- Estimating counts from weight or reel length (see [InvenTree #8009](https://github.com/inventree/InvenTree/issues/8009)).
- Barcode or QR scanning while counting. The QR labels spec could later make a scanned label open the count page.
- Stocktake snapshots, value reports or variance reports.
- Moving an item to another storage location during a count.
- Counting several storage locations in one form.
- A full "due" list page, or a setting for the due interval.

## Design

### Data

```sql
ALTER TABLE items ADD COLUMN last_counted_at TEXT;
ALTER TABLE locations ADD COLUMN last_counted_at TEXT;
```

Add both in `init_db()` before the `per_build` block, where no transaction is open, and follow the guarded `per_build` migration, not the bare `image_path` and `keywords` checks. `init_db()` runs before every request in both gunicorn workers, so an unguarded second `ALTER TABLE` fails with "duplicate column name". When either column is missing, run `BEGIN IMMEDIATE`, read `PRAGMA table_info(items)` (through `item_column_names()`, shared with the scan spec; add it if it is not there yet) and `PRAGMA table_info(locations)` again, add only what is still missing, then `commit()`. Existing rows get `NULL`, which means "never counted". Nothing is backfilled, because no past movement proves a physical count.

Stamps are full timestamps from `datetime.now(timezone.utc).isoformat()`, like every other time Tally stores. A date alone would sort below that day's timestamps and never move the `/api/version` marker. Pages show the date with `[:10]`, as `dashboard.html` does with `updated_at`. One save uses one `now` for all its stamps.

Add the new constant `COUNT_DUE_DAYS = 180` next to `TOLERANCE`. Work out the cutoff in Python as `(datetime.now(timezone.utc) - timedelta(days=COUNT_DUE_DAYS)).isoformat()`, adding `timedelta` to the `datetime` import.

**Due a count** comes from the items, so an item added after a count makes its storage location due again. `locations.last_counted_at` is for display and **Confirm empty** only.

```sql
SELECT l.id, l.code, l.label, count(i.id) AS item_count, min(coalesce(i.last_counted_at, '')) AS oldest
FROM locations l JOIN items i ON i.location_id = l.id
GROUP BY l.id HAVING oldest < ? ORDER BY oldest, l.code LIMIT 8
```

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/locations/<int:location_id>/count` | New, `count_location`. Renders the count form. `?walk=1` turns on walk mode. Unknown id: 404. |
| POST | `/locations/<int:location_id>/count` | New. Validates, writes variances through `record_movement()`, stamps, then redirects. |
| GET | `/locations` | Existing `locations`. Gains the count links. Also passes the first storage location with items in the open group. |
| GET | `/` | Existing `dashboard`. Gains the due query above and a total count. |
| GET | `/items/<int:item_id>` | Existing `item_detail`. `i.*` already picks up the column. |
| GET | `/api/version` | Existing `version`. Add `UNION ALL SELECT last_counted_at FROM items` and `UNION ALL SELECT last_counted_at FROM locations`, so a count with no differences and **Confirm empty** both refresh live pages. |

How the POST works:

- Load the rows as `SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.location_id=? ORDER BY i.name COLLATE NOCASE`, both before and after the lock. `record_movement()` reads `item["code"]`, so `SELECT * FROM items` would raise `IndexError`.
- Each row sends `count-<id>`, `match-<id>` and a hidden `expected-<id>`. The server loops over the location's items, not the form keys.
- Parsing: replace a single comma with a dot, then use `float` with the `math.isfinite` guard from `stock_checkout`, and reject values below zero. A ticked **Matches** with a blank field, or a field equal to expected within `TOLERANCE`, counts as expected. A ticked **Matches** with a different number is an error for that row.
- On any error, re-render `stocktake.html` with status 200 and the values from `request.form`, as `new_item` does. Mark bad rows with `aria-invalid="true"` and a `badge danger`. Nothing is written.
- Then `with conn:` and `BEGIN IMMEDIATE`, as `build_project` does, and read the quantities again. For each counted row:
  - if the current quantity is within `TOLERANCE` of expected, write `counted - current` when that is above `TOLERANCE`, then stamp;
  - if the current quantity already equals the count, such as after a double tap, stamp it and say nothing;
  - otherwise skip the row and name it in the flash: "changed since you opened this page, count it again".
- Stamp the location only if no row was blank or skipped.
- In walk mode the form posts to `url_for('count_location', location_id=..., walk=1)`. The redirect goes to the next storage location with items, with `walk=1`. After the last one it goes to `url_for('locations', group=code.split()[0])` with a flash saying the group is done. **Skip** is a plain link to the next one.
- Move the drawer sort key out of `locations()` into a new helper, `drawer_order(row)`, so walk order matches the list. The QR labels spec uses the same helper in `select_locations()`; whichever ships first adds it.
- No feature flag. A count is a stock movement, and `stock_checkout` and `quick_add` are not gated. `edit_components` stays on `item_detail`.

### Pages

- New `templates/stocktake.html`, extending `base.html`. It reuses `back`, `page-head`, `eyebrow`, `table-wrap` with `data-label` cells, `td.num`, `check`, `badge neutral`/`warn`/`danger`, `button primary`, `button quiet`, `form-actions`, `empty`, and the `qty` and `icon` macros from `templates/_macros.html`.
- Render the hidden `expected` value and a `data-expected` attribute from the raw float (`{{ item['quantity'] }}`), never `|qty`, which rounds to three decimals.
- Every number field is `type="number" min="0" step="any"`. Use `inputmode="numeric"` only when the unit is `pcs` and the expected quantity is whole. Otherwise use `decimal`.
- New classes in `static/app.css`: `.count-input` gives a full-width field at least 44px tall with a larger font. `.count-match` is a modifier on `.check` that makes the **Matches** label a 44px tap target. Both use the existing colour tokens and keep the global `:focus-visible` outline.
- No `data-live` on this page, so live refresh cannot reload it mid-count.
- `templates/locations.html`: add **Count stock here** to `drawer-actions`. Add the "counted YYYY-MM-DD" date to the existing item-count badge text ("6 items · counted 2026-09-14"), rather than adding a fifth child to the `.drawer summary` grid. Show **Count this group** only under `{% if active and not query %}`, and only when the group has a storage location with items.
- `templates/dashboard.html`: a **Due a count** `card` with `card-head` (title, total due, link to **Storage**) and `record-list` inside the existing `stack`. Copy the low-stock card's structure.
- `templates/item_detail.html`: a `<small>` line in `quantity-panel`.
- `templates/base.html`: map `'count_location': 'locations'` in `nav_active`. No new `nav_groups` or `tabs` entry. Bump `v=` on `app.css` and `app.js`.

### Script

A small block in `static/app.js`, guarded by the new `[data-stocktake]` attribute:

- Ticking **Matches** copies `data-expected` into the field and makes it read-only.
- Typing shows the difference in a `[data-variance]` cell as a "+2" or "-3" badge, using `formatQuantity()` for display only.
- On submit, a field with `validity.badInput` (for example a comma the browser would not accept) is marked and the submit stops, so a typed value is never sent as blank without warning.

The form works without JavaScript. No third-party library.

### Webhooks

Reuse the existing events through `record_movement()`: `stock.in` for a gain and `stock.out` for a loss, as the adjustment on `item_detail` does. `reason` is `"Stocktake"`, so automations can tell counts apart. Webhooks fire only when there is a difference.

## Edge cases

- **Partial count:** blank rows without a tick get no movement and no stamp, and the storage location is not stamped.
- **Fractional units:** 12.5 m counted as 12.25 m records -0.25. An on-hand of 0.1+0.2 still matches.
- **A count of 0:** valid. It records a loss of the whole on-hand quantity. Blank never means zero.
- **Double tap or replay:** rows already at the counted value are stamped silently, with no second movement.
- **Two workers at once:** `BEGIN IMMEDIATE` serialises them. The second sees changed quantities and skips those rows.
- **Negative on-hand:** the count still sets the real figure.
- **New item after a count:** its storage location is due again.
- **Fresh install:** empty storage locations never appear under **Due a count**. The card shows at most 8 rows and the total.
- **Confirm empty after an item was added:** the server sees items under the lock and re-renders the form.

## Tests

New `tests/test_stocktake.py`. Its `setUp` follows `tests/test_builds.py`: a temporary `DB_PATH`, `LAYOUT = "example"`, and items posted to `/items/new` in `4L 01`.

- `test_count_page_lists_the_storage_location`: names, raw expected values, `step="any"`, no `data-live`. A `pcs` item with a fractional on-hand gets `inputmode="decimal"`.
- `test_difference_writes_one_stocktake_movement`: 8 against 10 leaves 8 and one movement of -2. With `dispatch_webhooks` mocked, the events are `["stock.out"]`.
- `test_match_stamps_without_moving_stock`: no movement, `items.last_counted_at` set, `updated_at` unchanged, and the `/api/version` marker changes.
- `test_blank_rows_are_left_alone`: uncounted items are untouched and `locations.last_counted_at` stays `NULL`.
- `test_full_count_stamps_the_location`: the location is stamped and leaves **Due a count**. Adding a new item there makes it due again.
- `test_invalid_counts_change_nothing`: `-1`, `nan`, `inf`, `abc`, and a tick with a different number each return 200 with the row marked, the other typed values filled back in, and stock unchanged.
- `test_changed_stock_is_skipped`: a stale `expected` is skipped and named in the flash.
- `test_replay_records_once`: two identical posts give one movement, and the second flash does not say "changed".
- `test_count_waits_for_a_concurrent_change_and_skips_it`: modelled on `test_build_waits_for_a_concurrent_build_and_then_sees_the_shortfall`. Another connection holds `BEGIN IMMEDIATE` and runs `UPDATE items SET quantity=quantity-2 WHERE id=1`. The thread stays alive until `commit()`, then the row is named and no Stocktake movement exists.
- `test_fractional_units`: metres are recorded exactly, a difference under `TOLERANCE` writes nothing, "12,5" parses as 12.5, and 0.1+0.2 matches.
- `test_walk_moves_to_the_next_location`: `Location` is the next storage location with items in `drawer_order`, with `walk=1`. A blank save records nothing and still advances. The last one returns to the group.
- `test_confirm_empty_stamps_the_location`: stamps it and changes the version marker.
- `test_item_page_shows_last_counted`: "Never counted", then the date.
- `test_migration_adds_columns`: recreate `items` and `locations` without the columns in `executescript`, as `test_migration_is_all_or_nothing_and_renames_legacy_webhooks` does, then call `init_db()` twice. Both columns exist and are `NULL`.

## Docs to update

- `README.md` Features: "**Stocktake.** Walk a storage location or a whole group on your phone, tick what matches, type what doesn't. Differences become stock movements, and the dashboard shows what is due a count."
- `CONTEXT.md`: add **Stocktake** ("A physical count of one storage location's items. Each difference from on hand becomes a stock movement with the reason Stocktake. _Avoid_: Audit.") and **Last counted** ("When an item, or every item in a storage location, was last physically counted.").
- `CONTRIBUTING.md`: no change. There are no new setup steps or dependencies.

## Open questions

- **Shortfall event:** should a loss found by a count send `stock.lost` instead of `stock.out`? Or should counts get a new `stock.counted` event in `add_webhook`'s `events` set?
- **"Everything else matches":** should v1 have one button that ticks every blank row? It is quick but easy to press by mistake.
- **Due interval:** is a fixed 180 days right, or should it become a setting under **Settings**?
