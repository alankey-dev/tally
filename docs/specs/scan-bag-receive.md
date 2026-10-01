# Scan bag labels to receive stock

Once this ships, you can receive a parcel by scanning the 2D code on each distributor bag. You can use a USB or Bluetooth barcode scanner, or take a photo with your phone. Tally reads the manufacturer part number, the quantity and the supplier SKU from the label. If one item matches, Quick add opens the receive form with the bag quantity filled in, and you press Enter to record the stock movement. If nothing matches, the New component link carries what Tally read, so you only add a name, a component family and a storage location.

| Status | Research score | Depends on |
| --- | --- | --- |
| Proposed | 26/30, ranked 2 of 8 | Nothing new |

## Why

- [InvenTree #677](https://github.com/inventree/InvenTree/issues/677): adding parts by hand from supplier bags is "extremely time-consuming". The issue proposes a phone scan that adds to existing stock or creates the part.
- [InvenTree #10714](https://github.com/inventree/InvenTree/issues/10714): a request to create a part from a Digi-Key scan that has no match. It has 11 comments.
- [Part-DB #937](https://github.com/Part-DB/Part-DB-server/issues/937): "Add inventory using scanner/barcode", using the EIGP114 quantity and MPN to fill in the add-stock form. It has 6 thumbs-up.
- [Homebox #386](https://github.com/sysadminsmedia/homebox/issues/386): a request to create items by scanning. It has 7 thumbs-up and 17 comments, and the requester offered to sponsor it.
- [Hacker News](https://news.ycombinator.com/item?id=37146685): "inputting stock is the hardest part about using a system like this". Scanning is called "an elegant solution".
- [InvenTree 0.13](https://inventree.org/blog/2023/10/29/barcodes): a mature tool added barcode receiving for Digi-Key, Mouser, LCSC and TME.

It fits Tally because a scan is just typed text going into the Quick add box that already exists, the parser is plain Python, and the receipt reuses the replay-safe `stock_receipts` token.

## What it does

At the bench, with a keyboard-wedge scanner:

1. Open **Quick add**. The "What are you putting away?" box has focus.
2. Scan the bag. The scanner types the label and presses Enter.
3. Tally reads the label:
   - **Exactly one item matches.** Tally opens the receive form for that item, with the bag quantity filled in and focus on the quantity.
   - **Several items match, or none.** The usual "Choose your variant" list appears, searched by the MPN. Every link carries the scanned values. "Describe this item" and the suggested homes open New component with the manufacturer, part number, quantity and supplier SKU filled in. If a catalogue entry has the same part number, its name, component family and attributes are used.
4. Press Enter. Tally records "Stock received". If the item has no supplier and SKU yet, it saves the scanned pair.
5. Tally goes back to an empty search box, ready for the next bag.

On a phone:

- A Bluetooth scanner paired with the phone works exactly as above.
- Without a scanner, tap **Scan a bag label** (new) under the search box. The camera opens through a file input. Take a photo of the code. Tally decodes it in the browser, puts the text in the box and submits it. This works over plain `http://host:8000`.

Text that is not a bag label, such as a typed name or a 1D EAN code, is searched as it is today.

## Out of scope

- A live camera viewfinder (`getUserMedia`). It needs HTTPS, and most installs run over plain HTTP on the LAN.
- Editing an item's supplier or SKU. Tally has no item edit form, so in v1 a saved SKU can't be changed in the UI (see Open questions).
- Storing lot codes, date codes, purchase order or invoice numbers.
- Receiving many bags as one batch through the stock checkout list.
- Other label formats, such as the Würth Data Matrix.
- Descriptions or prices from distributor APIs.
- Printing Tally's own labels. The QR labels spec covers that.

## Design

### Data

Add two columns in `init_db`:

```sql
ALTER TABLE items ADD COLUMN supplier TEXT NOT NULL DEFAULT '';
ALTER TABLE items ADD COLUMN supplier_sku TEXT NOT NULL DEFAULT '';
```

Put the block straight after the existing `item_columns` ALTERs and before the `per_build` block. No transaction is open at that point. Later in the function, the `UPDATE webhooks` statement and `seed_layout` start an implicit transaction, and `BEGIN IMMEDIATE` would fail after them. Copy the `project_columns()` pattern: add a helper, `item_column_names()` (new), that re-runs `PRAGMA table_info(items)`, and check the columns with it. The stocktake and attachments specs reuse it. If they are missing, run `BEGIN IMMEDIATE`, check again, run both ALTERs, then `conn.commit()`. The `item_columns` set is read before the lock, so it can't be used for the second check. Existing databases migrate on the first request through `ensure_database`, and existing rows get empty strings.

There is no new table. A new module, `app/bag_labels.py`, holds `parse_bag_label(text)` (new). It returns `{manufacturer, part_number, supplier, supplier_sku, quantity}`, returns `None` when the text is not a label, and raises `ValueError` when the text has a label header that it can't split:

- **ECIA / ISO 15434** (Digi-Key, Mouser, Farnell). The text starts with `[)>` and fields are split on GS, as Digi-Key's [label decoding post](https://forum.digikey.com/t/digikey-product-labels-decoding-digikey-barcodes/41097) describes. Read `1P` (MPN) and `Q` (quantity). If `30P` is present, the supplier is "Digi-Key" and `30P` is the SKU. Other ECIA labels get a blank supplier and SKU. `P` is the customer's own part number, so it is never used.
- **LCSC.** `{pbn:…}` or `{pc:…}`: the supplier is "LCSC", `pc` is the SKU, `pm` is the MPN and `qty` is the quantity.
- **TME.** `QTY:` with `PN:`, `MPN:` and `MFR:`: the supplier is "TME", `PN:` is the SKU and `MFR:` is the manufacturer.

The manufacturer stays blank for ECIA and LCSC labels unless a catalogue entry fills it. The parser must not rely on RS or EOT being present. Python counts `\x1c` to `\x1f` as whitespace, so split on explicit characters and never call `split()` with no argument.

**Matching.** Strip the scanned MPN. If it isn't empty, look for items where `trim(part_number) = ? COLLATE NOCASE`. If that finds nothing, try `trim(supplier_sku) = ? COLLATE NOCASE` with the scanned SKU, again only when the SKU isn't empty. Treat exactly one hit as a match. More than one hit is the "Choose your variant" case. Find the catalogue entry with the same `part_number` rule.

### Routes

There are no new routes.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/quick-add` (`quick_add`) | Parse `q` before the existing `[:200]` cut, because Digi-Key labels can be longer than 200 characters. On one match, redirect with `item`, `q` (the MPN, or the SKU when the label has no MPN), `quantity`, `supplier`, `supplier_sku`, `expected` and `scan=1`. Otherwise, redirect to `q=<MPN>` with the same values, plus `catalogue=<id>` when a catalogue entry matches. Skip this for `fragment=1` requests. |
| POST | `/quick-add` (`quick_add`) | Inside the existing `with conn:` block, and only when `inserted`, also run `UPDATE items SET supplier=?, supplier_sku=? WHERE id=? AND supplier='' AND supplier_sku=''`, and only when the scanned SKU isn't empty. With `scan=1`, if the quantity is more than 10 times `expected`, show the form again with "Check the quantity" and record nothing. With `scan=1`, redirect to `/quick-add` with no `q`. |
| GET, POST | `/items/new` (`new_item`) | New `manufacturer`, `part_number`, `quantity`, `supplier` and `supplier_sku` arguments fill in the form. The existing `catalogue` argument still takes priority for the name, component family, manufacturer, part number and attributes. The POST stores `supplier` and `supplier_sku`. |

### Pages

- `templates/quick_add.html`:
  - Raise `maxlength` on `#quick-query` to 1000.
  - Keep `autofocus` on `#quick-query` only when no item is selected. Today the search box comes first in the page, so it takes focus and the `.quantity-input` autofocus never applies.
  - Fill the quantity from `quantity`.
  - Carry `supplier`, `supplier_sku`, `expected` and `scan` in hidden inputs.
  - Add **Scan a bag label** under `.quick-hero`. It is a `<label class="button">` around a `visually-hidden` `<input type="file" accept="image/*" capture="environment">`, using the `icon` macro. Enter or Space on the focused input opens the picker.
- `templates/quick_results.html`: each `.quick-match` link and the "Describe this item" button carry the scanned values and `catalogue`.
- `templates/item_form.html`: add real `manufacturer` and `part_number` inputs to the Identity section, filled from `suggested_manufacturer` and `suggested_part_number`. Today they exist only as `attr_manufacturer` (microcontroller board) and `attr_part_number` (Semiconductor / IC), and `showFamily` in `static/app.js` disables them in every other family. That means a scanned MPN would be lost for most items. Remove those two entries from `FAMILIES` in `app/catalogue.py`, and point the microcontroller `refreshName` in `static/app.js` at the new manufacturer input. Also add hidden `supplier` and `supplier_sku` inputs, and fill the quantity from the argument.
- `templates/item_detail.html`: show "Supplier SKU" (with the supplier) as one more `div` in `.attribute-list`. Widen the condition to `attributes or item['family'] != 'generic' or item['supplier_sku']`. The "Type" row stays as it is.
- Messages use `.quick-status`, `.flash` and `icon`.
- Add one rule to `static/app.css`: `.button:has(> input:focus-visible) { outline: 2px solid var(--focus); outline-offset: 2px; }`. Without it, focus lands on the clipped input and no ring shows. Check the ring in light and dark themes.
- No nav change. Quick add is already in `nav_groups` and `tabs` in `templates/base.html`.

### Script

In `static/quick_add.js`, set up the new handlers before the existing `if (!input || !results) return`, because `#quick-results` is missing on the receive form.

- Skip the live fragment search while the box starts with `[)>`, `{pbn:`, `{pc:` or `QTY:`.
- In a `keydown` handler on `#quick-query`, call `preventDefault()` for Ctrl plus `BracketRight` and insert GS. Do the same for Ctrl plus `Digit6` (Ctrl+^), inserting RS. Swallow Ctrl plus `KeyD` (EOT). Wedge scanners send these control characters as Ctrl key presses, and Ctrl+D would otherwise open the bookmark dialog and lose the scan.
- In a `keydown` handler on `.quantity-input`, watch for a scan that lands there. That is `[` or `{` as the first key, or keys arriving faster than 30 ms apart for more than 4 keys in a row. When that happens, call `preventDefault()`, clear the quantity, move focus to `#quick-query` and send the rest of the keys there. The server check on `expected` is the backstop.
- Photo input: show "Reading the label…" in `#quick-status`. Downscale the photo to about 2000 px on the long edge with `createImageBitmap` or a canvas. Use native `BarcodeDetector` when `getSupportedFormats()` includes `data_matrix` and `qr_code`. Otherwise load the fallback decoder lazily. Set the box value and submit the form.

The fallback is **zxing-wasm** (MIT), using its IIFE reader build: a small JS loader plus a reader `.wasm` file of about 1 MB. It is needed because iOS Safari has no `BarcodeDetector`. Turn on its try-rotate option. Vendor both files under `static/vendor/zxing-wasm/` (new) and commit them. Point the loader at the local `.wasm` through its module override, because by default it fetches the file from a CDN. Add a pinned `zxing-wasm` devDependency and an `npm run vendor:zxing` copy script (new) beside `build:css`. Running Tally still needs no build step.

Ship this in two PRs:

1. Columns, `app/bag_labels.py`, Quick add, New component, keyboard handling and tests.
2. The photo path, the vendored files and the README note about HTTPS.

### Webhooks

None new. Receipts fire the existing `stock.in` through `record_movement`. New components log "Initial stock" with no webhook, as today.

## Edge cases

- The label header is there but the scanner dropped every GS. Show the error flash "Tally could not read this label. Set your scanner to send GS (Ctrl+])." Don't guess at the fields.
- The label has no MPN, or no SKU. Skip that comparison, so a blank never matches items with blank fields.
- Two items share the part number. Show "Choose your variant" and don't pick one.
- The quantity is missing, zero or not a number. Fill in 1, and let the existing check reject bad input.
- The next bag is scanned into the quantity field. The script redirects the keys to the search box. If keys still get through, the `expected` check stops the receipt.
- A bag is scanned twice by mistake. Each form gets a fresh token, and opening the receive form never moves stock, so recording it twice takes two confirmations.
- The item already has a supplier or SKU. Keep it, and save nothing new.
- `add_components` is off. Tally never redirects a scan to New component, so the existing 403 on that page doesn't change.
- The photo has no readable code. Show "No code found. Try closer, in better light."
- The decoder fails to load. Show "Could not load the scanner. Type the part number instead." The box stays usable.

## Tests

New `tests/test_scan.py`, with the same `setUp` as `tests/test_quick_add.py`:

- `parse_bag_label` reads made-up fixtures for Digi-Key (with `30P`), Mouser (no SKU), LCSC and TME. It handles labels with RS and EOT present or missing, returns `None` for plain text and EAN codes, and raises for a header with no GS.
- A label for a unique part number redirects to `item=<id>&quantity=<n>&q=<MPN>`. The form shows `value="<n>"`, and `#quick-query` has no `autofocus`.
- Matching ignores case and surrounding spaces. A label with an empty MPN doesn't match items with a blank `part_number`.
- A label longer than 200 characters still parses. With `fragment=1`, a label doesn't redirect.
- For an unknown MPN, the "Describe this item" link leads to `/items/new` with the part number and quantity filled in. Posting that form with the generic family stores `items.part_number` and `items.manufacturer`. With a matching catalogue entry, the link carries `catalogue=<id>`.
- Posting one token twice records a single "Stock received". A replay doesn't change the SKU, and an existing SKU is kept. With `scan=1`, the response redirects to `/quick-add` with no `q`.
- With `scan=1`, a quantity more than 10 times `expected` records nothing and shows "Check the quantity".
- A legacy database without the columns gains them, and its rows keep their quantities. A second `sqlite3.connect(main.DB_PATH)` sees both columns, as in `tests/test_builds.py`.
- A mocked `dispatch_webhooks` is called once with `stock.in`.
- The item page shows "Supplier SKU" for a generic item with no attributes.

## Docs to update

- `README.md`: add "**Scan to receive.**" to Features. Add a scanner note: turn on GS (Ctrl+]), and note that Tally handles Ctrl+D so the bookmark dialog doesn't open. Say that the photo path works without HTTPS.
- `CONTRIBUTING.md`: add "Adding a bag label format" (`app/bag_labels.py` plus a fixture in `tests/test_scan.py`), and how to run `npm run vendor:zxing`.
- `CONTEXT.md`: add **Bag label** (a distributor's 2D code on a parts bag) and **Supplier SKU** (the distributor's order code for an item, kept for reordering).

## Open questions

- Is one supplier and SKU per item enough, or should SKUs get their own table? Decide this with the order list spec (`reorder-list.md`).
- Should a supplier and SKU edit on the item page, gated by `edit_components`, ship in v1 or wait for the order list? The parametric search spec asks the same about attributes, so one item edit form could cover both.
- Should the lot and date code go into the stock movement reason, for example "Stock received · lot 1234"?
- Is 10 times `expected` the right limit for the quantity check?
