# Contributing

Thanks for helping. Bug reports, small fixes and new storage layouts are all welcome. For bigger changes, open an issue first so we can agree on the approach. Researched proposals for the next features live in [`docs/specs`](docs/specs/README.md); pick one up the same way.

## Setup

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt
TALLY_LAYOUT=example STORAGE_DB=data/dev.db .venv/bin/flask --app app.main run --debug
.venv/bin/python -m unittest
```

Styles live in `static/app.css`. Tailwind only supplies the reset; `npm run build:css` regenerates it.

## Guidelines

- Keep it one container and one SQLite file, with no build step needed to run it.
- Add or update a test in `tests/` for behaviour changes.
- `CONTEXT.md` defines the words the app uses (item, variant, storage location). Please stick to them in the UI.
- Pages must work on a phone, by keyboard, and in light and dark themes.

## Adding a bag label format

Bag labels are read by `parse_bag_label` in `app/bag_labels.py`. Add a branch that returns `manufacturer`, `part_number`, `supplier`, `supplier_sku` and `quantity`, or `None` when the text is not your format, and add a made-up fixture and a test in `tests/test_scan.py`. Add the label's opening characters to `labelStart` in `static/quick_add.js` so the live search skips it.

Photo decoding uses [zxing-wasm](https://github.com/Sec-ant/zxing-wasm), copied into `static/vendor/zxing-wasm/` and committed, so running Tally needs no build step. After changing the pinned version in `package.json`, run `npm install && npm run vendor:zxing` and commit the result.

## Adding a label size

Label sizes are the entries of `LABEL_PRESETS` in `app/main.py`. Each gives the page size, columns, rows, label size, top-left margin and gap between labels, all in millimetres. A roll is one label per page, with the page the same size as the label. Print a test sheet with **Draw outlines for a test print** ticked, at 100% scale, and check the outlines sit on your label edges.

## Sharing a storage layout

Layouts are JSON files in `app/layouts/`. Each location needs a `code` and a `label`, and can also have a `kind` and search `keywords`. `groups` names the code prefixes, and `family_homes` maps a component family to the location where new parts of that family go by default. See `example.json`.

## Importing a BOM

A project page takes a BOM CSV from KiCad, EasyEDA or JLCPCB tooling. The file can be UTF-8, UTF-16 or cp1252, and use `,`, `;` or tab. The header row is found in the first 10 rows by these column names, ignoring case:

| Field | Column names |
| --- | --- |
| designator | Reference, References, Designator |
| quantity | Qty, Quantity |
| value | Value, Comment, Name |
| footprint | Footprint, Package |
| mpn | MPN, Manufacturer Part, Manufacturer Part Number |
| supplier code | LCSC Part #, LCSC, Supplier Part, JLCPCB Part # |

A file needs a designator column and a value, MPN or supplier code column. A file can be up to 5 MB and 300 grouped lines (`MAX_BOM_BYTES` and `MAX_BOM_LINES` in `app/main.py`). Rows marked DNP are skipped. The review keeps no state on the server, so each step posts the whole review back, and importing never moves stock.

## Sharing a parts catalogue

New upload types go through an allowlist with a first-bytes check, never the browser's claimed type alone.

Quick add suggests known parts from a catalogue kept in the database. It starts from `app/catalogue.json`; replace or extend it under **Settings → Quick add catalogue** by uploading a file or entering a URL (the URL is remembered, so one click refreshes it). Entries are matched by name, so re-importing updates them.

A JSON catalogue is a list of items, or an object with an `items` list. Each item needs a `name` and can have a `family` (one of the component families in `app/catalogue.py`, otherwise `generic`), `manufacturer`, `part_number` and an `attributes` object keyed by that family's field names. A CSV catalogue needs a `name` column; `family`, `manufacturer` and `part_number` are optional, and every other column becomes an attribute. **Download catalogue** exports the current catalogue in the JSON format.

Once a catalogue entry is added as an item, its resistor or capacitor `value` and `value_unit` attributes become searchable by magnitude. Accepted notations include `4k7`, `4.7k`, `4700R`, `4700 Ω`, `100n`, `0.1u`, `0.1µF` and `100 nF`. Quick add's Common devices list still matches catalogue entries by name, manufacturer and part number only.
