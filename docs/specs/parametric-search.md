# Parametric search on component attributes

Once this ships, you can find a part by what it is as well as by what it is called. Type `10k 0603` or `100nF 50V` into the Components search or Quick add and you get the matching resistor or capacitor. Values can be written however you like: `0.1u`, `100n` and `100 nF` all find the same capacitor. On the Components page you can pick a component family and narrow the list with chips built from that family's attributes, such as package or type. For resistors and capacitors you can also set a value range, for example capacitors from 10 µF.

| Status | Research score | Depends on |
| --- | --- | --- |
| Shipped | 21/30, ranked 8 of 8 | Nothing new |

## Why

- [Part-DB discussion #303](https://github.com/Part-DB/Part-DB-server/discussions/303): "Display parts parameter per category and quick search in categories" is Part-DB's highest-voted feature idea, with 10 upvotes.
- [Part-DB issue #386](https://github.com/Part-DB/Part-DB-server/issues/386): users ask for SI unit prefixes on parameters. It has 6 thumbs-up and duplicates in #598 and #1479.
- [InvenTree issue #5576](https://github.com/inventree/InvenTree/issues/5576): "[FR] Include parameters in search", with 18 comments.
- [InvenTree issue #4851](https://github.com/inventree/InvenTree/issues/4851): filter by parameter value (for example package 0402). It has 12 comments and was implemented in PR #9739.
- [Part-DB documentation](https://docs.part-db.de/): parametric search is listed as a headline feature.
- [PartsBox features](https://partsbox.com/features.html): "Table Filtering: filter by column values, tags, and full text search".

Every item already stores an `attributes` JSON object, and `FAMILIES` in `app/catalogue.py` already defines the fields, so this makes existing data searchable with no new dependency.

## What it does

1. On the Components page (`/items`), the search placeholder reads "Search by name, part number, value or location".
2. Typing `10k 0603` finds the 10 kΩ 0603 resistor. It does not find the 100 kΩ resistor or the 10 kΩ 0805 one. Value terms must match exactly. Other words still match fuzzily, as they do now. The Components list, the suggestions dropdown and Quick add all use the same matching.
3. Values are normalised. `4k7`, `4.7k`, `4700R` and `4700 Ω` are the same resistance. `0.1u`, `0.1µF`, `100n` and `100 nF` are the same capacitance. A bare number such as `4700` is still matched as text.
4. Value terms in an item's name count too. An item called "100 nF capacitor" that was saved as "Other item", with no attributes, is still found by `0.1u`.
5. Matches show a short summary of the variant, such as "4.7 kΩ · 0805", in the dropdown, in Quick add and on the Components page. That way two resistors with the same name but different packages can be told apart.
6. Below the toolbar, family chips list every component family that has at least one item, with a count, for example "Capacitor (12)". Choosing one limits the list to that family.
7. With a family chosen, a "Filters" disclosure holds one chip row per attribute, built from the values in stock (Package: 0603, 0805, 1206). Choosing a chip filters by it. Choosing another chip in the same row replaces it. Choosing the active chip removes it.
8. For resistors and capacitors, "From" and "To" fields accept values such as `10u` or `1k`. Either can be left empty. Choosing Apply keeps only items in that range.
9. Filters are kept in the URL, so a filtered view can be bookmarked. They also survive switching between Tiles and List, a new search, and applying a range. "Clear filters" resets them.
10. **At the bench on a phone:** a strip of resistors arrives. Open Quick add and type `4k7 0805`. The existing item appears at the top, with "4.7 kΩ · 0805" under its name. Tap it, enter the count and save. Or open Parts, tap Resistor, open Filters, tap 0805, and read the storage location from the card. Chip rows wrap onto new lines and never scroll sideways.

## Out of scope

- Value ranges for families other than resistor and capacitor. Their values are free text, such as `4–35 V`.
- Parsing voltage, tolerance, power or pitch into numbers. These stay as text chips and text matches.
- The new matching in `stock()` and the project line picker in `project_detail()`. Both keep their `LIKE` search. See Open questions.
- Matching catalogue entries by value. `common_suggestions()` is unchanged.
- Editing an item's attributes after it is created. No route does this today.
- Saved searches and sorting by value.

## Design

This ships as two PRs. PR 1 adds the parser, the matching and the summary, used by `find_stock()` (`/api/search` and Quick add), with no UI change beyond the summary. PR 2 adds the `/items` matching, chips, range and URL-kept filters.

### Data

None. No column is added. Values and search terms are worked out per row at query time from `name`, `family` and `attributes`, so a later parser fix applies at once and there is no backfill. `find_stock()` already loads every item, so the cost is the same, and `/items` does the same.

New functions in `app/matching.py`. If the BOM import spec has shipped, replace its `value_text` in `app/bom.py` with these, so Tally has one set of value rules:

- `parse_value(text, unit="")` (new) returns a float or `None`. It first applies `unicodedata.normalize("NFKC", ...)`, so the micro sign becomes μ and the ohm sign becomes Ω. Prefixes are case-sensitive: p, n, u, µ/μ, k, K, M, `meg`, G, and lowercase `m` only when `F` follows. Units are `F`, `Ω`/`ω`, `ohm` and `R`. RKM notation is limited to `\d{1,3}[pnuµμkKMR]\d{1,2}` (`4k7`, `2R2`, `4u7`). Every match must have no letter or digit directly before or after it. A space is allowed before a prefix only when a unit follows (`100 nF`, `4.7 kΩ`). Results are rounded to 6 significant figures.
- `value_token(number, unit)` (new) returns a canonical token rounded to 3 significant figures, using only `[a-z0-9]`: `4k7`, `100n`, `10u`, `1meg` for mega, `1m` for milli, and `r` or `f` as the decimal mark when there is no prefix (`2r2`, `0r`).
- `item_terms(row)` (new) returns the row's value tokens, taken from the name and from `value` plus `value_unit`, along with the other attribute values as text. A whole number followed by a unit is joined, so `50 V` becomes `50v`.
- `split_query(query)` (new) returns the value tokens in the query and the remaining text. The remaining text is joined the same way (`50 V` becomes `50v`).
- `value_ranked(query, rows)` (new) does the matching. If the query has no value tokens, rows are scored with the existing `ranked()`. The text scored is name, manufacturer, part number, location code and `item_terms(row)`. If the query has value tokens, a row with value tokens of its own must contain all of them, and the remaining text is then scored. With no remaining text, the row scores 1. A row with no value tokens is scored on the whole query, as today, so `1n41` still finds a 1N4148 diode. This stops `100n` matching `100k`, which `score()` would otherwise give 0.75.

New function in `app/catalogue.py`: `item_summary(row)` (new) joins `value` and `value_unit`, `voltage` and `package` with " · ", skipping any that are missing. It lives beside `FAMILIES` because `app/catalogue.py` already imports `app/matching.py`, so putting it in `app/matching.py` would make a circular import.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/items` and `/components` | Existing `items()`. SQL gains `WHERE i.family = ?` when `family` is a key in `FAMILIES`. The `LIKE` clause is removed. The rest happens in Python: `q` through `value_ranked()` (ordered by score, or by name when there is no `q`), `attr_<key>` (one value per attribute, with the key checked against the family's fields), and `min`/`max` (`>=`, `<=`, or both, applied only when `family` is resistor or capacitor). Rows are passed as dicts with `summary` added. Family counts come from `SELECT family, count(*) FROM items GROUP BY family`. |
| GET | `/api/search` | Existing `search()`. Uses `value_ranked()` through `find_stock()`. Same response shape, plus `summary`. |
| GET | `/quick-add` | Existing `quick_add()`. Gains the same matching through `find_stock()`. |

No new endpoints. `new_item()` is unchanged.

### Pages

- `templates/items.html`:
  - Add family chips below `.toolbar`. Add a new `<details class="filter-panel">` that holds the attribute rows and the range form. It is open when any attribute or range filter is active.
  - Attribute rows cover the family's fields except `value` and `value_unit`. Values are grouped by casefolded text with all whitespace removed, so `50 V` and `50V` give one chip. Each row shows the 8 most common values, then a "More" link (`more=<key>`) that shows all of them.
  - `items()` passes the current `filters` dict. Every chip, Tiles/List link and "Clear filters" link uses `url_for('items', **dict(filters, key=value))`, where `None` removes an argument.
  - The search form gains hidden `family`, `attr_*`, `min` and `max` inputs. The range form gains hidden `q`, `view`, `family` and `attr_*` inputs.
  - Tiles show `summary` in `.component-card-body p` when it is set. Otherwise they show the current part number text. The list view adds `summary` as a `<small>` under the name.
  - Reuse `icon` and `qty` from `templates/_macros.html`, `.button.small` for Apply, `.search-field` for the range inputs, `.badge.neutral` for counts, and `.empty` for no matches.
- `templates/quick_results.html`: add a `<span>` with `summary` to each `.quick-match`.
- `static/app.css`: add `.filter-chips` (new), a wrapping flex row of links styled like `.segmented a`, with `aria-current="true"` on the active chip. Also add `.filter-panel` (new), modelled on `form details`. Both use only existing tokens (`--surface-3`, `--text-2`, `--border`, `--radius-sm`), so light and dark themes work. Tap targets are at least 30 px tall.
- Chips are plain links and the range is a GET form, so everything works by keyboard and without JavaScript. Each chip row has an `aria-label` that uses the field label from `FAMILIES`.
- Nav: no change. The page is already `items` in `nav_groups` and `tabs` in `templates/base.html`.

### Script

`static/app.js`: in the `#inventory-search` suggestions handler, add `escapeHtml(result.summary)` to the second line when it is set. No library.

### Webhooks

None.

## Edge cases

- `1M`, `1meg` and `1 MΩ` are mega and give `1meg`. `1mF` gives `1m`. A bare `1m` is text and never a hard filter, so `1M` does not find a 1 mF capacitor, and a `1 m` cable still matches as text.
- `3M Kapton tape` gives `3meg` on both sides, so `3M` still finds it.
- Uppercase `N`, `P` and `U` are not prefixes, and the adjacency rule rejects `2n2222` and `1N4148`. Part numbers such as `ESP32-C3`, `Uno R3` and `NE555P` stay text.
- `0R` is 0 Ω, a valid value, not `None`.
- Package codes are text. `0603` against `0805` scores 0 in `score()`.
- A `min` or `max` that does not parse is ignored, with a flash message. So is a range given without a resistor or capacitor `family`. If `min` is greater than `max`, the two are swapped.
- An unknown `family` or attribute key is ignored.
- Malformed `attributes` JSON is treated as `{}`, as `item_detail()` already does. Values that are not strings are converted with `str()`.
- Items with no value attributes still match on value tokens in their name. They never match a range filter.
- Decimals with other units (`0.25 W`, `2.54 mm`) keep today's matching.

## Tests

New file `tests/test_parametric_search.py`. Its `setUp` follows `tests/test_quick_add.py`: a temporary `DB_PATH`, the `example` layout, and items posted to `/items/new` with `family` and `attr_*` fields.

- `parse_value`: `4k7`, `4.7k`, `4700R` and `4700 Ω` are equal. `0.1u`, `100n`, `100nF`, `100 nF` and `0.1µF` (micro sign) are 1e-7. `2R2` is 2.2 and `10K` is 10000. `0R` is 0. Junk gives `None`.
- `value_token`: `value_token(parse_value("10u"), "F") == "10u"`. `1M` gives `1meg` and `1mF` gives `1m`.
- `/api/search?q=10k 0603` returns the 10 kΩ 0603 resistor first and leaves out the 100 kΩ resistor and the 10 kΩ 0805 resistor. Its `summary` is "10 kΩ · 0603".
- `0.1uF` and `100n` return the same capacitor. `100n` does not return a 100 kΩ resistor. `1M` does not return a 1 mF capacitor.
- `100nF 50V` and `100 nF 50 V` both find the 50 V capacitor and leave out a 16 V one.
- `4700R` finds the 4.7 kΩ resistor. A generic "100 nF capacitor" is found by `0.1u`.
- Regressions: `1N4148`, `1n41`, `ESP32-C3`, `Uno R3` and `3M` find the matching seeded items. `2.2n` does not return a 2N2222.
- `/quick-add?q=4k7` lists both 4.7 kΩ resistors and shows `0805` on the 0805 one.
- `/items?q=10k 0603` leaves out the 10 kΩ 0805 resistor. `/items?family=capacitor&q=0805` shows no resistors.
- `/items?family=capacitor&attr_package=0805` lists only 0805 capacitors and shows chips only for packages in stock. `50 V` and `50V` give one chip.
- `/items?family=capacitor&min=10u` includes a 47 µF capacitor and leaves out 100 nF. `max=10u` includes a capacitor entered as 0.01 mF. `min=abc` returns 200 with a flash message. `min=10u` without `family` returns every item with a flash message.
- The range form and the Tiles link both keep `attr_package=0805`.
- A row updated to `attributes='{'` does not break `/items?family=resistor&attr_package=0805` or `/api/search`. Both return 200.
- The tile for an item with no part number shows its summary.
- `tests/test_quick_add.py` and `tests/test_builds.py` pass unchanged.

## Docs to update

- `README.md` features list: under **Permanent homes**, change "Search by name, part number, or location code" to include value and package, for example `10k 0603`. Add a line on filtering by component family and attribute.
- `CONTRIBUTING.md`, "Sharing a parts catalogue": say that resistor and capacitor `value`/`value_unit` attributes become searchable once an entry is added as an item. Quick add's Common devices list still matches catalogue entries by name, manufacturer and part number only. List the accepted notations.
- `CONTEXT.md`: under **Attribute**, add that a resistor's or capacitor's value is compared by magnitude, so `100n` and `0.1u` describe the same variant. No new term.

## Open questions

- Should `stock()` and the project line picker in `project_detail()` get `value_ranked()` too? `10k 0603` would help when adding lines to a bill of materials.
- Should bare numbers count as values once a resistor or capacitor family is chosen, so `4700` matches 4.7 kΩ in that view?
- Should a small "edit attributes" form on `item_detail()` ship first, so items with sparse attributes can be fixed without adding them again? The scan spec asks the same about supplier SKUs, so one item edit form could cover both.
- If large inventories make per-request parsing slow, should a stored, versioned value column be added later?
