# Usage reports

Once this ships, a **Usage** page shows which items you check out most and which you never touch, over a period you choose. It also lists the items you use together, so you can see which parts belong near each other.

| Status | Research score | Depends on |
| --- | --- | --- |
| Proposed | Not scored | Nothing new |

## Why

- Every checkout, build and loss already goes through `record_movement()`, so Tally holds the usage history and shows none of it.
- A perfboard or breadboard can be the most used part in the workshop. Quantity and low stock do not show that.
- Knowing what is used most, and what is used together, is the information needed to decide where drawers should live. Moving drawers is a separate decision and is not part of this spec.

## What it does

1. **Usage** opens from the main navigation. A period selector offers 30 days, 90 days, 1 year and All time. The default is 90 days.
2. **Most used** lists the top 20 items by number of checkouts in the period, with the quantity taken out, the last checkout and the current drawer.
3. **Least used** lists items with stock on hand and no checkout in the period, oldest last checkout first. Items never checked out show "Never".
4. Both lists can be grouped by component family or by storage group, and exported as CSV when exports are enabled.
5. A **checkout** is any outgoing movement: stock out, a project build or a loss. Returns do not cancel it, so a perfboard taken out 30 times and returned 30 times counts 30. Receipts, returns, order receipts, stocktake corrections and initial stock are not checkouts.
6. **Used together** lists the pairs of items that most often leave in the same job, with the number of jobs. A job is one checkout action or one build. Items that share a project's bill of materials also count as used together, even if the project has not been built.
7. **Unaccounted for** lists items whose stocktakes found less than expected in the period, with the total shortfall. It is not part of the ranking. A drawer that is often short suggests parts are leaving without a recorded checkout.
8. A coverage line under the page title says how much of the period's stock reduction came from recorded checkouts, for example "82% of stock taken out was recorded". The rest shows up as stocktake shortfalls.

## Out of scope

- Suggesting or applying drawer moves, and any distance-to-bench setting. This spec only reports. A later spec can propose moves if the report shows they would help.
- Tracking item page views or label scans.
- AI. Counting needs no model. Choosing a home for a new part stays with Jev in Quick add.
- Tools that are borrowed and returned. That needs a different model (who has it, where it is now).
- Forecasting demand.

## Design

### Data

Add one nullable column so a job can be grouped exactly:

```sql
ALTER TABLE movements ADD COLUMN batch TEXT;
```

Follow the guarded migration used for the other `ALTER TABLE` columns, because `init_db()` runs before every request in both gunicorn workers and an unguarded second `ALTER TABLE` fails with "duplicate column name". Old rows keep `NULL` and are treated as a job of one.

`record_movement()` gains an optional `batch` argument. The stock checkout action, a project build and an undo each generate one batch token (`secrets.token_hex(8)`) and pass it to every row they write. Other callers pass nothing.

A constant lists the reasons that count as checkouts, so a new reason cannot silently count as use. Checkout reasons are the ones the stock action writes for use and loss, plus `Built …`.

### Queries

Most used:

```sql
SELECT item_id, count(*) AS checkouts, -sum(quantity_change) AS taken, max(occurred_at) AS last_out
FROM movements WHERE quantity_change < 0 AND occurred_at >= ? AND <checkout reason>
GROUP BY item_id ORDER BY checkouts DESC LIMIT 20
```

Used together: self-join `movements` on `batch` (and on `project_items.project_id` for bill-of-materials pairs), count distinct jobs per item pair, and keep pairs with at least 3 jobs.

Unaccounted for: sum negative `Stocktake` movements per item in the period.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/usage` | New. Renders the page for `?days=` and `?group=`. |
| GET | `/usage.csv` | New. Same data as CSV, behind `exports_enabled`. |

### Webhooks

None. The page only reads.

## Edge cases

- No checkouts in the period: each list shows a plain empty state and the coverage line is hidden.
- Movements written before the `batch` column exist group as one job each, so older history adds to frequency but not to used-together.
- An undone build writes positive movements, which are not checkouts. The original build stays counted, because the checkout happened.
- A deleted item drops out of every list.
- Large histories: queries use indexes on `movements(item_id, occurred_at)`. Add that index with `CREATE INDEX IF NOT EXISTS`.

## Tests

- Only the listed reasons count as checkouts, and returns do not cancel them.
- Most used and least used respect the period and the grouping.
- One checkout action and one build each write a single `batch`, and items in it appear as used together.
- Items sharing a project bill of materials appear as used together without a build.
- A stocktake shortfall appears under Unaccounted for and not in the ranking.
- The migration runs twice without error and leaves old rows with a `NULL` batch.
- CSV export is gated by `exports_enabled`.

## Docs to update

- `README.md`: add Usage reports to Features.
- `CONTEXT.md`: define **Checkout** and **Job**.
- `docs/specs/README.md`: this spec's row.

## Open questions

1. Is 3 jobs the right minimum before a pair counts as used together?
2. Should the default period be 90 days or a year, given that workshop use is bursty?
