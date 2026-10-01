# Import a CAD bill of materials

Once this ships, you can upload the BOM CSV that KiCad, EasyEDA or JLCPCB tooling exports on a project page, so you no longer search for 60 items one at a time. Tally reads the file and groups references such as R211-R218 into one line with a count. For each line it suggests an item, first by part number and then by value and package. On a review table you accept each suggestion, choose another item, search for one, open the new item form for a part you do not stock yet, or skip the line. One press adds all the accepted lines to the project's bill of materials. Importing never moves stock. The existing build planner shows what is short, and Build xN takes the stock.

| Status | Research score | Depends on |
| --- | --- | --- |
| Proposed | 23/30, ranked 4 of 8 | Nothing new |

## Why

- [Part-DB README](https://github.com/Part-DB/Part-DB-server): lists BOM import for projects from KiCad as a headline feature.
- [Part-DB issue #778](https://github.com/Part-DB/Part-DB-server/issues/778): a real import bug where one value had two footprints. People use this feature and hit its edge cases.
- [Binner BOM wiki](https://github.com/replaysMike/Binner/wiki/BOM-(Bill-of-Materials)): accepts KiCad, EasyEDA and other EDA exports.
- [PartsBox KiCad integration](https://partsbox.com/blog/kicad-integration-05-2025.html): PartsBox shipped BOM matching on its free plans because exported BOMs carry little more than "10k".
- [Hacker News comment](https://news.ycombinator.com/item?id=37146685): "import BOMs to see if I have all the parts".
- [bomi PR #1](https://github.com/somebox/bomi/pull/1): header heuristics for LCSC and JLCPCB column names, and reference grouping.

It fits Tally because it needs only the stdlib `csv` module, the matcher in `app/matching.py` and Jinja forms, and it writes to the `project_items` table the build planner already reads.

## What it does

1. On a project page, the **Add a line** card gets a second block, **Import a BOM**, with a file picker and an **Import** button.
2. Tally works out the encoding, the delimiter and the header row. It recognises the KiCad, EasyEDA and JLCPCB column names in the alias table under Design.
3. Rows that share a value, footprint, part number and supplier code become one line. The line's per-build quantity is the sum of the Qty column. When there is no Qty column, it is the number of designators.
4. The review page lists each line with its designators, value, package and part number, a **Match** select, a **Per build** field and a status badge:
   - **Part number** (neutral): the MPN or LCSC code equals an item's part number.
   - **Name match** (warn): a fuzzy match on value and package.
   - **No match** (danger): no item was found. The select defaults to **Skip this line**.
   - **Already a line, ×N** (neutral): the chosen item is already in the bill of materials. Its per-build quantity N will be replaced.
5. The Match select offers up to five candidate items and **Skip this line**. Each option reads "name · on hand and unit · storage location code", for example "10 kΩ resistor 0805 · 120 pcs · R-03".
6. To pick an item outside the top five, type in the line's **Search** field and press **Match again**. That line is then ranked against your words.
7. When the add_components feature is on, a line can show **New item**. It opens the existing new item form in a new tab (`target="_blank"`), with the name prefilled as "value package", for example "10k 0805". You set the component family and attributes on the form. Back on the review, **Match again** re-matches every line you have not set by hand.
8. **Add N lines** checks every accepted line first. If any line is invalid, nothing is written and the review comes back with that line marked. Otherwise all lines are written at once and you return to the project page. The flash message says how many lines were added and how many skipped, and that stock is only taken when you build.

**At the bench on a phone:** export the CSV on the desktop and save it to Files or Drive. Open the project on the phone, tap **Choose file** and pick the CSV. The review table stacks into one card per line using the existing `td[data-label]` layout, with a full-width select and quantity field. Tap **Add N lines**, then **Check stock** for xN.

## Out of scope

- XLSX files, KiCad XML or netlists, and any KiCad plugin or HTTP library API.
- Creating items in bulk from the review, or prefilling attributes on the new item form.
- Storing designators or the source MPN on a line.
- Re-import diffing, such as removing lines that are no longer in the file.
- Supplier lookups, prices and ordering.
- Taking stock at import time. That stays with Build xN.
- Remembering a "this MPN means this item" mapping between imports.

## Design

### Data

None. Lines go into the existing `project_items` table with `quantity = 0`, using the upsert from `project_detail`:

```sql
INSERT INTO project_items(project_id, item_id, quantity, per_build) VALUES (?, ?, 0, ?)
ON CONFLICT(project_id, item_id) DO UPDATE SET per_build=excluded.per_build;
```

`init_db` is unchanged, and existing databases need no migration. The review is stateless, so either gunicorn worker can serve each step. Nothing is kept in the session or in a temporary table.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/projects/<int:project_id>/import` | New `import_bom`. When a `file` is uploaded (multipart), it parses and matches the file and renders the review. When there is no file, this is **Match again**: it reads the review fields and re-matches the lines that were not set by hand. |
| POST | `/projects/<int:project_id>/import/commit` | New `commit_bom_import`. Validates every accepted line, adds together lines that point at the same item, and upserts them in one `with conn:` block. On success it redirects with `back_to_project`. |
| GET | `/projects/<int:project_id>` | Existing `project_detail`. Shows builds possible, need, on hand and short. |
| GET | `/items/new?name=` | Existing `new_item`. Prefills the name only. |

Both new routes return 404 for an unknown project and sit behind the access gate in `ensure_database`. The upload follows `import_catalogue`: `request.files`, `upload.read(MAX_BOM_BYTES + 1)`, and `flash` on a `ValueError`. There are two new constants: `MAX_BOM_BYTES = 5 * 1024 * 1024` and `MAX_BOM_LINES = 300` grouped lines.

**Review fields.** The review form is URL-encoded, with no `enctype`. Werkzeug's multipart limits (`MAX_FORM_PARTS`, `MAX_FORM_MEMORY_SIZE`) would turn a 300-line review into a raw 413. The fields are:

- `lines`: a JSON list. Each entry holds `designators`, `value`, `footprint`, `mpn`, `supplier_code`, `quantity` and `suggested`, the item id that matching proposed, or null.
- `item_<n>`: an item id, or `skip`.
- `per_build_<n>`: the per-build quantity.
- `q_<n>`: the optional search text.

A line counts as set by hand when `item_<n>` differs from `suggested` or `q_<n>` is filled in. Both routes parse `lines` defensively. They reject malformed JSON, anything that is not a list of objects, more than `MAX_BOM_LINES` entries, or strings longer than 200 characters, with a flash error and no writes. Values from `lines` are used only for display and matching. Item ids are always checked against `items`.

**Parsing (new `app/bom.py`, stdlib only).**

- `parse_bom(raw)` decodes UTF-16 when the file starts with a UTF-16 byte order mark. Otherwise it tries `utf-8-sig`, then `cp1252` for files re-saved in Excel. A NUL byte outside UTF-16 means a binary file and is rejected. It tries `,`, `;` and tab in turn, and keeps the first delimiter that gives a recognised header in the first 10 rows. A header is recognised when it has a designator column and a value, MPN or supplier code column. A `csv.Error` becomes a `ValueError`.
- Header aliases are matched ignoring case:

| Field | Column names |
| --- | --- |
| designator | Reference, References, Designator |
| quantity | Qty, Quantity |
| value | Value, Comment, Name |
| footprint | Footprint, Package |
| mpn | MPN, Manufacturer Part, Manufacturer Part Number |
| supplier code | LCSC Part #, LCSC, Supplier Part, JLCPCB Part # |

- `expand_designators(text)` turns "R1-R4, R7" into five references.

**Matching.**

- `value_text(text)` (new) rewrites component values the same way on both sides. It runs on the line's value and on each item's match text, which is the name, manufacturer, part number and attribute values. `µ` and `μ` become `u`. `Ω` and "ohm" are dropped. The unit letters F and H after a prefix are dropped:

| Written | Becomes |
| --- | --- |
| 10k, 10K, 10kΩ, 10 kΩ, 10k ohm | 10 k |
| 4k7, 4K7 | 4.7 k |
| 4R7, 4.7Ω | 4.7 |
| 100n, 100nF, 100 nF | 100 n |
| 10u, 10uF, 10µF, 10 μF | 10 u |

- `match_line(line, items)` (new) first looks for a part number equal, ignoring case, to the MPN or supplier code. This runs only when the stripped code is not empty, `~` or `-`, and it never matches an empty `part_number`. Fuzzy matching is skipped for lines that this step resolves. Other lines are scored on their `value_text`, or on `q_<n>` when one is given. A package taken from the footprint (`0805` from `R_0805_2012Metric`, or a leading `SOT-23` or `SOIC-8` token) moves candidates that contain it up the list.
- Speed: item rows are loaded once per request, with the `find_stock` SQL plus `attributes`. Each item's match text is rewritten and normalised once. A new `score_tokens(wanted, words)` in `app/matching.py` holds the body of `score()`, and `score()` calls it, so its behaviour is unchanged. Only items that contain the line's number token are scored. The target is 300 lines against 5,000 items in under 5 seconds, well inside gunicorn's 30-second timeout.

### Pages

- `templates/project_detail.html`: inside the existing `<aside class="card">`, after the Add a line results, add a `<div class="bom-import">` with `<h2>Import a BOM</h2>`, a `copy card-intro` paragraph and a multipart form. The form has `accept=".csv,.tsv,.txt,text/csv,text/comma-separated-values,text/tab-separated-values,application/vnd.ms-excel,text/plain"` (only a hint, because the server checks the content) and a `button primary` with `icon('file')`. `.split.wide` keeps exactly two grid children. A new `.bom-import` rule in `static/app.css` adds `margin-top`, `padding-top` and `border-top: 1px solid var(--border)`.
- `templates/bom_review.html` (new): extends `base.html`, imports `icon` from `_macros.html`, and has no `data-live`, because it is a POST-rendered page. It reuses `back`, `page-head`, `eyebrow`, `lede`, `section-head`, `badge neutral|warn|danger`, `table-wrap` with `td data-label`, `inline-qty`, `button small quiet`, `form-actions` and `empty`, plus the `qty` filter. **Match again** is a second submit with `formaction` set to `import_bom` and `formnovalidate`. Each select has `aria-label="Item for {{designators}} ({{value}})"`. Each per-build input has `aria-label="Quantity of {{value}} per build"`, as on `project_detail.html`. `import_bom` passes `can_add_items=enabled('add_components')`, because `feature_context` does not supply it.
- `templates/base.html`: add `'import_bom': 'projects'` and `'commit_bom_import': 'projects'` to `nav_active`. No `nav_groups` or `tabs` change.

The page uses native form controls only, so it works by keyboard and in both themes with the existing tokens.

### Script

One guard in the live-refresh check in `static/app.js`. Skip the reload when any `input[type=file]` has files selected. Otherwise, adding a new item in another tab, or a stock movement from another device, changes `/api/version` and reloads the project page, which clears the chosen CSV. This matters most on phones, where focus leaves the file input when the picker closes.

### Webhooks

None. Adding lines moves no stock, and `project.built` still fires when you build.

## Edge cases

- Rows marked DNP, "Exclude from BOM" or "Populate = no" are skipped and counted on the review.
- Preamble or footer rows with no designator and no quantity are ignored.
- The same value with two footprints stays as two lines, as in Part-DB issue #778.
- When Qty and the designator count disagree, Qty wins and the line shows a `badge warn`.
- When two lines resolve to the same item, their per-build quantities are added together on commit, and the flash message says so.
- An item that is already a line has its per-build quantity replaced, as **Add line** does. Importing the same file twice therefore gives the same result.
- These files fail with a flash message and write nothing: binary data, no recognisable header, more than 5 MB, or more than `MAX_BOM_LINES` lines.
- An item id that no longer exists, or a per-build quantity that is zero, negative, NaN or not a number, fails the whole commit. The review is re-rendered with that line marked `badge danger` and every other choice kept. The checks match `update_project_line`.
- Fractional quantities, such as wire in metres, are accepted.
- With **Skip this line** chosen everywhere, nothing is written, and the flash message says so.

## Tests

`tests/test_bom_import.py` (new). Its `setUp` follows `BuildPlannerTests` in `tests/test_builds.py`: a temporary `DB_PATH`, the `example` layout, items posted to `/items/new` (with `part_number` values such as `C25804`), and a project. Files are posted the way `QuickAddTests.test_catalogue_imports_from_file_and_url` in `tests/test_quick_add.py` does it: `data={"file": (io.BytesIO(raw), "bom.csv")}, content_type="multipart/form-data"`. Errors are asserted with `class="flash error`.

- `test_kicad_csv_is_grouped_and_matched_by_part_number`: "R1, R2, R3" shows as one line with per build 3, the item is preselected, and `project_items` stays empty.
- `test_utf16_tab_and_cp1252_files_are_read`: an EasyEDA UTF-16 tab file and an Excel cp1252 file give the same lines.
- `test_semicolon_file_with_quoted_designators`: `"R1, R2";10k;...` parses with `;`.
- `test_lcsc_code_matches_part_number`: "LCSC Part #" `C25804` matches.
- `test_values_match_items_written_either_way`: "10k" with `R_0805_2012Metric` matches items named "10k resistor 0805" and "10 kΩ resistor 0805" ahead of a 1206 variant. "100n" matches "100nF capacitor", and "4k7" matches "4K7".
- `test_blank_mpn_does_not_match_items_without_part_number`: MPN `~` or empty gives no **Part number** badge.
- `test_commit_adds_lines_without_moving_stock`: item quantities and the `movements` count are unchanged, `per_build` is set, and the project page shows builds possible.
- `test_skipped_duplicate_and_existing_lines`: skipped lines are absent, two lines on one item are summed, and an existing line is replaced.
- `test_bad_line_writes_nothing`: an unknown item id or an invalid quantity on one line re-renders the review with `badge danger` and leaves `project_items` untouched.
- `test_bad_files_write_nothing`: binary data, no header, oversize and too many lines.
- `test_tampered_lines_field_is_rejected`: invalid JSON, a non-list, more than `MAX_BOM_LINES` entries and non-numeric ids each give a flash error and no writes.
- `test_match_again_keeps_hand_choices`: a changed select and a `q_<n>` search survive **Match again**, and the untouched lines are re-matched.
- `test_new_item_link_follows_add_components`: the link is present only when the feature is on.
- `test_commit_of_max_lines_succeeds`: 300 lines commit URL-encoded.
- `test_items_are_normalised_once_per_request`: patching `normalise` where `app/bom.py` looks it up shows one call per item plus one per line.
- `test_dnp_rows_are_skipped`.
- `BomParserTests`: `expand_designators` on ranges, commas and spaces, and the `value_text` table.

## Docs to update

- `README.md`: extend the **Projects** bullet with "Import a KiCad, EasyEDA or JLCPCB BOM CSV and match it to your items."
- `CONTRIBUTING.md`: add an "Importing a BOM" section after "Sharing a parts catalogue", with the header alias table and the 5 MB and 300-line limits.
- `CONTEXT.md`: add **Designator**: a board reference such as R12 in a CAD BOM. Tally groups designators into one line and does not store them. _Avoid_: Reference.

## Open questions

- Should v1 store designators? That needs a `designators TEXT NOT NULL DEFAULT ''` column on `project_items`, added in `init_db` through the `PRAGMA table_info(project_items)` check, so the BOM export and later imports can show them.
- Should value matching ship with `value_text` in `app/bom.py`, or wait for `parse_value` and `value_token` from the parametric search spec (`parametric-search.md`)? Whichever ships second must reuse the first, so Tally has one set of value rules.
- Should LCSC codes match only `part_number`, or do items need a supplier code field?
- Is 300 grouped lines the right cap, given the speed target?
