# Printable QR labels

Once this ships, you can print a sheet or roll of labels for your storage locations and stick one on each drawer, box or shelf. Each label shows the location code in large type, its description and a QR code. Point any phone's camera at the code and Tally opens the stock in that drawer, ready to record use or receipt. Items can have their own label, which opens the item page. Printing goes through the browser's normal print dialog, so the container needs no printer drivers and the phone needs no app.

| Status | Research score | Depends on |
| --- | --- | --- |
| Shipped | 25/30, ranked 3 of 8 | Nothing new |

## Why

- [PartKeepr #21](https://github.com/partkeepr/PartKeepr/issues/21): "Label/Barcode/Inlay Printing" was opened in 2011. It was still open, with 16 comments, when the repository was archived.
- [Homebox #436](https://github.com/sysadminsmedia/homebox/issues/436): the "Better Labeling/Tagging" epic has 12 thumbs-up, 5 hearts, 17 comments and 88 sub-issues.
- [Homebox #9](https://github.com/sysadminsmedia/homebox/issues/9): "Location QR Codes" drew 15 comments and shipped.
- [Part-DB #1239](https://github.com/Part-DB/Part-DB-server/issues/1239): a university workshop wants location labels that list the stored parts next to the location QR code.
- [PartsBox "ID Anything"](https://partsbox.com/id-anything.html): you scan any QR code with a phone camera and the part or location opens. PartsBox sells on this.
- [diyAudio requirements thread](https://www.diyaudio.com/community/threads/requirements-and-feature-requests-for-a-parts-inventory-management-system.355454/): a hobbyist plans to print QR codes on a cheap Brother thermal printer.

It fits Tally because the pinned reportlab 5.0.1 already includes `reportlab.graphics.barcode.qr.QrCodeWidget`, so labels can use the same `canvas.Canvas` + `send_file` path as `export_rows`, with no new dependency and no build step.

## What it does

1. On **Storage**, open a group (for example `C1`) or search for some locations, then choose **Print labels**. From the all-groups view, **Print all labels** covers every storage location.
2. A dialog shows the address the QR codes will use, with a warning if a phone cannot reach it. It asks for a label preset, how many labels to skip on a part-used sheet, and whether to draw outlines for a test print.
3. **Make labels PDF** opens a PDF in the browser. It prints at actual size by default. Print sheets from a computer, because iOS AirPrint may scale the page.
4. Each drawer in the list also has its own **Print label** action, for one replacement label.
5. On an item page, **Print label** makes one label for that item, with its name, part number, location code and a QR code that opens the item page. **Add** on the new item form already lands on the item page, so you can print the label straight after receiving stock.
6. At the bench, using a phone:
   - Point the phone's camera at a drawer label and tap the link.
   - If the shared access password is required, sign in. Tally then returns to the scanned link.
   - **Stock** opens filtered to that storage location, showing only the items stored there.
   - Tap **Add** on an item, choose **Stock out** or **Stock in** in the checkout list, then **Record movement**. You stay on the same drawer for the next movement.
   - If the drawer is empty, **Add component here** opens the new item form with the location already chosen.
7. Under **Settings → Labels**, you set the address the QR codes point to and the default preset.

The **Print labels** and **Print label** buttons only appear when **Export CSV and PDF** is on in Settings.

## Out of scope

- A scanner inside the app. The phone's own camera app does the scanning.
- Reading distributor barcodes, or making Code128 or DataMatrix codes.
- Sending jobs directly to Brother, Dymo or Niimbot printers, or over IPP.
- A label layout designer, or custom presets entered in the UI.
- QR codes that record a stock movement when scanned. Scanning only opens a page.
- Labels for projects or builds.
- Printing a label automatically when stock is received in **Quick add**.

## Design

The work splits into three PRs, and PR 1 can be picked up alone:

- **PR 1:** drawer labels on the A4 3 × 7 preset, `/l/<code>`, `/stock?location=`, `labels.base_url` with its warning, the exports gating and the sign-in round-trip test.
- **PR 2:** item labels, the other presets, `skip`, and the CONTRIBUTING section.
- **PR 3 (optional):** listing a drawer's items on its label, and the Reports card.

### Data

None. There is no schema change, so `init_db` stays as it is. Two new keys go in the existing `settings` table through `set_setting`:

- `labels.base_url`: an optional absolute address, such as `https://tally.example.lan`. A new validator, `valid_base_url` (new), requires `valid_webhook_url` to pass and rejects any query string, fragment or params. Trailing slashes are stripped before the value is saved.
- `labels.preset`: the key of the default preset.

Presets are a new constant, `LABEL_PRESETS` (new), in `app/main.py`. Each preset gives the page size, columns, rows, label size, margins and gaps in millimetres. A roll preset is one label per page, with the page the same size as the label. Suggested set:

- A4 sheet, 3 × 7 labels of 63.5 × 38.1 mm (the default, PR 1)
- A4 sheet, 5 × 13 labels of 38.1 × 21.2 mm
- Letter sheet, 3 × 10 labels of 66.7 × 25.4 mm
- Roll, 62 × 29 mm
- Roll, 29 × 90 mm

Existing databases need no migration. If no preset is stored, the first preset is used. If no base URL is stored, `request.host_url` is used.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/locations/labels.pdf` | New (`location_labels_pdf`). Makes the labels PDF. It accepts `group`, `q` or `code` to pick locations, plus `preset`, `skip`, `outlines` and (PR 3) `contents`. |
| GET | `/items/<int:item_id>/label.pdf` | New (`item_label_pdf`). Makes one item label. It accepts `preset` and `outlines`. Returns 404 for a missing item. |
| GET | `/l/<path:code>` | New (`scan_location`). The address in a drawer's QR code. It tries an exact `code=?` match first. If that fails, it tries `code=? COLLATE NOCASE`, which only counts when exactly one row matches. On a match it redirects to `stock` with `location=<id>`. Otherwise it flashes a message and redirects to `locations` with `q=<code>`. |
| GET | `/stock?location=<id>` | Existing `stock`. It gains an exact `l.id=?` filter, which combines with `q`. Today's `q` filter uses `LIKE`, so `A1` also matches `A10`. An unknown id flashes "That storage location no longer exists." and redirects to `locations`. |
| POST | `/stock/checkout` | Existing `stock_checkout`. It reads an optional `location` field. On success and on both error paths it redirects to `url_for("stock", location=...)` when that id exists, and to `stock` otherwise. |
| GET | `/items/<int:item_id>` | Existing `item_detail`. This is the address in an item's QR code. It already offers **Use in stock checkout** and **Receive stock**. |
| GET | `/settings` | Existing `settings`. It gains the Labels card. |
| POST | `/settings/labels` | New (`update_labels`). Saves the base URL and the default preset, following the pattern of `update_features`. |

Gating: add `location_labels_pdf` and `item_label_pdf` to `restricted` in `ensure_database` under the existing `exports` flag. `scan_location` stays ungated. Add `exports_enabled` (new) to `feature_context` so templates can hide the buttons. The order list spec needs the same key; whichever ships first adds it.

Helpers:

- `select_locations(group, q, code)` (new). This moves the row selection out of `locations()` unchanged: the first word of the code is the group, a `q` search ignores `group` and matches `casefold()` against `code + ' ' + label` in code order, and the group view sorts with `drawer_order(row)` (new, shared with the stocktake spec): by kind (small, medium, large drawer, other) and then by code. `code` selects one exact location. Both `locations()` and `location_labels_pdf` call it, so the labels print in the order the page shows. `item_label_pdf` does not use it.
- `label_url(endpoint, **values)` (new). It joins `labels.base_url`, when set, to the path from `url_for`, without doubling or dropping slashes. Otherwise it uses `url_for(..., _external=True)`.
- `label_address_warning(address)` (new). It returns a reason when the address is `localhost`, `127.0.0.1`, `::1` or `0.0.0.0`, or a single-label host with no dot (such as `tally`), or when it is `http` while the request's `X-Forwarded-Proto` says `https`.

Drawing: the QR code is a `QrCodeWidget` (error correction level M) inside a `reportlab.graphics.shapes.Drawing`, placed with `renderPDF.draw`. Text is fitted with `reportlab.pdfbase.pdfmetrics.stringWidth`. Long text shrinks to a minimum size and is then cut short with an ellipsis. Both PDF routes call `pdf.setViewerPreference('PrintScaling', 'None')` and send the file inline (`as_attachment=False`).

### Pages

- `templates/locations.html`:
  - Add **Print labels** to `.page-actions` in both the group view and the all-groups view.
  - Add a new `<dialog id="print-labels">` holding a GET `.form`, following the existing `add-location` dialog. Use `label` fields, a `select` for the preset, a number field for skip, `.check` checkboxes, a `.hint` (print at 100%, outlines for a test print), the resolved address with `.badge.warn` when `label_address_warning` returns a reason, and `.form-actions` with `.button.primary` and `.button.quiet`.
  - Add a **Print label** `.button.small` link to each `.drawer-actions`, using `icon('download', 'small')`.
  - Change the existing **View stock** link from `url_for('stock', q=location['code'])` to `url_for('stock', location=location['id'])`, so it matches what a scan shows.
- `templates/item_detail.html`: add a **Print label** `.button` to `.detail-actions`.
- `templates/stock.html`: when `location` is set, show a `.back` link to all stock and a `.storage-heading` with the code and description. Add a hidden `location` input to both the search form and the checkout form. When the drawer is empty, the `.empty` block links to `new_item` with `location=<id>`, but only when `add_components` is on.
- `templates/settings.html`: add a new "Labels" `.card` with a `.password-form` holding the base URL and preset fields. Add a `.copy` line showing the resolved address, with `.badge.warn` and the reason when there is a warning.
- `templates/reports.html` (PR 3): an optional `.report-card` for storage labels, with a `.button-group` linking to **Storage**.
- Navigation: no change to `nav_groups` or `tabs` in `templates/base.html`.

### Script

None. The dialog opens with an inline `showModal()`, as the add-location dialog does. reportlab draws the QR codes on the server.

### Webhooks

None. Printing and scanning record no stock movements.

## Edge cases

- **Codes with spaces or slashes.** `url_for` percent-encodes them (`/l/C1%20S01`), and `<path:code>` accepts slashes.
- **Case.** `new_location` stores codes in upper case, but `seed_layout` inserts layout codes as written. `locations.code` is UNIQUE and case-sensitive, so a custom layout could hold both `a1` and `A1`. The exact-then-unique-NOCASE lookup handles this.
- **Shared password.** `ensure_database` redirects to `access` with `next=request.full_path`. That value is decoded and ends in `?` (`/l/C1 S01?`). werkzeug encodes it again on the way back, so the scan returns to the drawer after sign-in. The session cookie is not permanent, so a phone browser may ask again after it restarts.
- **Unreachable address.** With no base URL set, the QR codes use whatever Host header reached gunicorn. There is no `ProxyFix`. The dialog and Settings show the warning before anyone prints a full sheet.
- **Reverse proxy.** A base URL can include a path prefix. `label_url` joins it cleanly.
- **Skip count.** Clamp `skip` to between 0 and one less than the number of labels per sheet. An unknown `preset` falls back to the default.
- **Item lists on labels (PR 3).** These go stale as stock moves. List what fits, then print "+N more". Leave the list off small presets.
- **Fonts.** Helvetica covers Ω, µ and ± in reportlab 5.0.1, because reportlab switches to its built-in Symbol font for characters outside WinAnsi. Characters outside WinAnsi, Symbol and ZapfDingbats, such as CJK or emoji in item names, print as wrong or missing glyphs.
- **Printer scaling.** `PrintScaling /None` makes Chromium's viewer default to actual size. Other viewers and iOS may still fit the page to the printer, so the outlines option exists for a calibration print.
- **Large prints.** The example layout has 141 storage locations, which is 7 sheets on the 3 × 7 preset. That is fine for one request.
- **Locations that no longer exist.** No route edits a code today. If a database edit removes or changes one anyway, old labels and bookmarks reach the friendly fallback, not a 500 error.

## Tests

New file `tests/test_labels.py`, set up like `tests/test_builds.py`: a temporary `main.DB_PATH`, `main.LAYOUT = "example"` and `main.app.test_client()`. Pages are counted with a regex for `/Type /Page\b`.

- `/locations/labels.pdf?group=C1` returns 200 with `application/pdf`, a body starting with `%PDF`, and `/PrintScaling /None`. The expected page count comes from the C1 rows in the database: 44 drawers give 3 pages on the 3 × 7 preset.
- With `skip=20` there are 64 slots, so the PDF has 4 pages.
- Unknown `preset` values and out-of-range `skip` values do not cause an error.
- `select_locations` gives the same codes, in the same order, as `/locations?group=C1` and `/locations?q=S0`.
- `label_url` produces `http://localhost/l/C1%20S01` with no base URL. With `labels.base_url` set to `https://tally.example.lan/parts`, it produces `https://tally.example.lan/parts/l/C1%20S01`.
- `/l/c1%20s01` redirects to `/stock?location=<id>`. An unknown code redirects to `/locations?q=...` and flashes a message. With both `a1` and `A1` present, `/l/a1` finds `a1` exactly.
- `/stock?location=<id>` lists only that storage location's items. The test creates `A1` and `A10`, each with one item. `/stock?location=999999` redirects to `/locations`.
- A checkout posted with `location=<id>` redirects to `/stock?location=<id>`, on success and on an over-quantity error.
- `/items/<id>/label.pdf` returns a PDF for an item named "10 kΩ ±5% resistor". A missing item returns 404.
- `POST /settings/labels` rejects `ftp://`, `javascript:`, `https://tally.lan/?x=1` and `https://tally.lan/#a`. It saves `https://tally.example.lan/parts/` as `https://tally.example.lan/parts`.
- `GET /settings` with `base_url="http://127.0.0.1:8000"` shows `badge warn`. With a dotted base URL set, it does not.
- With `feature.exports` set to `0`, both PDF routes return 403 and the buttons are hidden. `/l/C1%20S01` still redirects.
- In `tests/test_access.py`: set `main.LAYOUT = "example"` in this test and restore it afterwards. A fresh client requesting `/l/C1%20S01` is sent to `/access`. `parse_qs` gives `next` as `/l/C1 S01?`. Posting the password to that redirect target and following redirects ends on the filtered Stock page for `C1 S01`.

## Docs to update

- `README.md` Features: add a **Labels** bullet ("Print QR labels for drawers and parts; scan one with any phone camera to open that drawer's stock").
- `README.md` First steps: add "print labels for your storage" after step 1. Under Configuration, mention the label address setting and the warning.
- `CONTRIBUTING.md`: add a short "Adding a label size" section explaining `LABEL_PRESETS`, its millimetre units and how to check one with outlines.
- `CONTEXT.md`: add **Label**: "A printed sticker for a storage location or item, carrying its code and a QR code that opens it in Tally." _Avoid_: tag, sticker. A distributor's code on a parts bag is a **Bag label** (see the scan spec), not a Label. Note that the database column `locations.label` is the location's description, which the UI calls "Description".

## Open questions

- Should a drawer's QR code open the filtered **Stock** page, as proposed here, or a new page for that storage location?
- Should the label address be a setting only? The alternative is to trust `X-Forwarded-Host` through werkzeug's `ProxyFix`, which changes what the app trusts from the network.
- Which presets ship by default, and should Letter be included for users outside the UK?
- If non-Latin item names matter, should a TrueType font such as DejaVu Sans be bundled as a static file? It would apply to `export_rows` too.
- Should listing a drawer's items (PR 3) be on or off by default?
