# Attachments on items

Once this ships, you can attach a datasheet PDF, a pinout photo or a link to any item, each with a short label. The item page lists them, and one tap opens one. On a phone, PDFs open in the phone's own viewer. If an item was made from a quick add catalogue entry, you can share an attachment with that entry, and every item made from the same entry shows it. The Components list marks items that have attachments. A new full backup download puts the uploaded files and the database in one zip.

| Status | Research score | Depends on |
| --- | --- | --- |
| Shipped | 22/30, ranked 6 of 8 | Nothing new |

## Why

- [PartsBox features](https://partsbox.com/features.html): lists "Attach datasheets, CAD models, and documents to any record" as a core feature.
- [InvenTree attachments](https://docs.inventree.org/en/stable/concepts/attachments/): every part has an attachments tab that takes an uploaded file or an external link.
- [Part-DB issue #624](https://github.com/Part-DB/Part-DB-server/issues/624): users want one attachment shared across several parts (5 thumbs-up).
- [Part-DB issue #1374](https://github.com/Part-DB/Part-DB-server/issues/1374): a request to bulk-download every datasheet, which points to heavy use.
- [Binner](https://github.com/replaysMike/Binner): datasheet search and retrieval is a headline bullet.
- [EEVblog parts software thread](https://www.eevblog.com/forum/chat/electronic-parts-management-software/): users praise tools that let you "attach as many documents as you want to everything".

It fits Tally because `UPLOAD_DIR`, `image_upload()`, the `/uploads/<path:filename>` route and the `init_db` migration pattern already exist, and the feature needs only stdlib Python, SQLite and Jinja.

## What it does

1. Open an item. A new **Attachments** card, with the lede "Datasheets, pinouts and links", sits below the attributes and notes.
2. Each attachment shows its label and a badge: PDF, Image or Link. An attachment shared through the catalogue entry also shows a "Shared" badge.
3. Tap a label to open it in a new tab. A saved PDF is named after its label.
4. To add one, enter a label, then choose a file (PDF, PNG, JPEG, WebP or GIF) or paste an http(s) link, and press **Add**.
5. If the item came from a catalogue entry, tick **Share with every item from this catalogue entry**.
6. **Remove** deletes an item's own attachment. For a shared one, **Remove** opens a second step, **Remove from all N items**, so one mis-tap can't take it off every item.
7. In the Components list, items with at least one attachment, their own or shared, show a small file icon in tile and list views.
8. **Settings → Download full backup** saves a zip of the database and the uploads folder.

At the bench on a phone: open Components, tap the item, then tap "Datasheet". The phone's PDF viewer opens it. To add one there, tap the file field. The phone offers the camera, Photos or Files, so you can photograph the pinout card or pick a PDF you downloaded.

## Out of scope

- Fetching datasheets automatically from Octopart, Digi-Key, LCSC or similar.
- Attachments on storage locations, projects or lines.
- PDF thumbnails, an in-page viewer, or search inside files.
- Editing an attachment in place. Remove it and add it again.
- Restoring from the zip through the UI.
- Markers on quick add results or the stock page.

## Design

Ship it in three PRs. PR 1: item attachments, which covers files and links, the size cap, serving headers, the Components marker and the gunicorn timeout. PR 2: `items.catalogue_id`, sharing, and catalogue replace keeping ids. PR 3: the full backup zip.

Decisions taken: attachments ride on the `edit_components` flag; the backfill matches by exact name; `/settings/backup` stays as it is next to the new zip; files keep uuid names on disk and get a `download_name` taken from their label.

### Data

In `init_db`, add this to the `executescript` block (PR 1):

```sql
CREATE TABLE IF NOT EXISTS attachments (
  id INTEGER PRIMARY KEY,
  item_id INTEGER REFERENCES items(id),
  catalogue_id INTEGER REFERENCES catalogue(id),
  label TEXT NOT NULL,
  file_path TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '',
  mimetype TEXT NOT NULL DEFAULT '',
  size_bytes INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  CHECK ((item_id IS NULL) != (catalogue_id IS NULL)),
  CHECK ((file_path = '') != (url = ''))
);
CREATE INDEX IF NOT EXISTS attachments_item ON attachments(item_id);
CREATE INDEX IF NOT EXISTS attachments_catalogue ON attachments(catalogue_id);
CREATE INDEX IF NOT EXISTS attachments_file ON attachments(file_path);
```

PR 2 adds a link from items to catalogue entries, which items don't store today:

```sql
ALTER TABLE items ADD COLUMN catalogue_id INTEGER;
UPDATE items SET catalogue_id = (SELECT c.id FROM catalogue c WHERE c.name = items.name COLLATE NOCASE);
```

Put this block after the final `conn.commit()` in `init_db`. By then the catalogue seed (`catalogue.seeded`) has run, so an old database backfills against a full catalogue. There is also no implicit transaction left open by the webhook `UPDATE`, `seed_layout` or the seed. Wrap it the way `per_build` is wrapped: check `PRAGMA table_info(items)` through `item_column_names()` (shared with the scan spec; add it if it is not there yet), `BEGIN IMMEDIATE`, check again, alter, update, commit. The backfill runs only in the transaction that adds the column, so it happens once. Existing rows are otherwise unchanged.

`new_item` stores `suggestion["id"]` in `catalogue_id`. The form has no `action`, so `?catalogue=` survives the POST.

`save_catalogue(conn, entries, replace=True)` currently runs `DELETE FROM catalogue`, which changes every id. Change it to upsert by name with the existing `ON CONFLICT(name)`, then deal with the entries missing from the new file. For each one, copy its shared attachments down to each linked item (same `file_path`). Then run `UPDATE items SET catalogue_id=NULL WHERE catalogue_id IN (dropped ids)` for every dropped id, whether or not it had attachments, and delete those entries. `catalogue.id` has no `AUTOINCREMENT`, so SQLite can reuse an id, and this step stops a new entry from showing on unrelated items. `import_catalogue` runs `conn.execute("BEGIN IMMEDIATE")` inside its `with conn:`, as `build_project` does.

### Routes

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/items/<int:item_id>` | Existing `item_detail`. Also loads the item's attachments, its entry's shared ones, and N, the number of items linked to that entry. |
| POST | `/items/<int:item_id>/attachments` | New `add_attachment`. Takes `label`, `file` or `url`, and `share`. |
| POST | `/items/<int:item_id>/attachments/<int:attachment_id>/delete` | New `delete_attachment`. |
| GET | `/uploads/<path:filename>` | Existing `uploaded_image`. Adds headers and `download_name`. |
| GET | `/components`, `/items` | Existing `items`. Adds `attachment_count`. |
| GET | `/api/version` | Existing `version`. Also reads a new `attachments.changed` settings row. |
| GET, POST | `/items/new` | Existing `new_item`. Stores `catalogue_id`. |
| POST | `/settings/catalogue` | Existing `import_catalogue`. A replace keeps the ids of entries that remain. |
| GET | `/settings/backup.zip` | New `download_full_backup`. |
| GET | `/settings/backup` | Existing `download_backup`. Unchanged. |

Upload handling:

- Add a new `save_upload(field_name, allowed, signatures=None)` and have `image_upload` call it with `ALLOWED_IMAGE_TYPES` and no signatures. Image uploads, SVG included, behave as they do today.
- Add a new `ATTACHMENT_SIGNATURES`: `%PDF-` to `.pdf`, the PNG signature, `\xff\xd8\xff`, `GIF8`, and `RIFF....WEBP`. SVG is not in it. When signatures are passed, read the first 16 bytes, pick the type and suffix from them and ignore the browser's claimed type, since some Android pickers send PDFs as `application/octet-stream`. Then `upload.stream.seek(0)` and save.
- Add a new `MAX_ATTACHMENT_BYTES` (20 MB). In `add_attachment`, set `request.max_content_length` (Flask 3.1) before touching `request.files`, and catch `RequestEntityTooLarge` so it flashes "Files can be up to 20 MB." Do not set the global `MAX_CONTENT_LENGTH`, so other uploads keep today's limits.
- Add `--timeout 120` to the Dockerfile `CMD`. Sync workers are killed after 30 s, which a 20 MB upload over slow mobile data passes.
- Links go through the existing `valid_webhook_url()`.
- Add both new endpoints to the `restricted` map in `ensure_database` under `edit_components`.
- `delete_attachment` runs `BEGIN IMMEDIATE` inside `with conn:`. It deletes the row if it belongs to the item or its entry, and uses the `rowcount` as the guard, as `undo_build` does. Inside the lock it counts other rows with the same `file_path`. It unlinks the file only after commit, and only if that count was 0. If the id belongs to a different item, it returns 404. If the row is already gone, it flashes "That attachment was already removed." and redirects.
- Add and remove both write `attachments.changed` to `settings`, and `version()` adds `UNION ALL SELECT value FROM settings WHERE key='attachments.changed'`. That way a live Components page refreshes without moving items up the dashboard's recent list.
- `items` adds `(SELECT COUNT(*) FROM attachments a WHERE a.item_id=i.id OR (i.catalogue_id IS NOT NULL AND a.catalogue_id=i.catalogue_id)) AS attachment_count`.
- `uploaded_image` adds `X-Content-Type-Options: nosniff` to every response, and `Content-Security-Policy: sandbox` to every response that isn't a PDF. Chromium won't render a sandboxed PDF. If an attachment row matches the `file_path`, it passes `download_name=<label>.<ext>`, which keeps the file inline.
- `download_full_backup` copies the database with `sqlite3.Connection.backup` into a temporary file in `DB_PATH.parent`, not a small container `/tmp`. It zips that with `UPLOAD_DIR` (as `uploads/<name>`) using stdlib `zipfile`, sends the zip, and deletes both temporary files in `response.call_on_close`.

### Pages

- `templates/item_detail.html`: add a new `.card` after the notes, with an `h2` "Attachments", a `p.copy.card-intro` lede and a `.record-list`.
  - Each attachment is a `div.record`. Its first `div` holds an `<a target="_blank" rel="noopener">` with the `strong` label, plus a `small` showing the size or the link's host. A `.record-actions` div holds a `.badge neutral` for the type, a second one for "Shared", and the remove form.
  - Remove: for an item's own attachment, use `button small quiet danger`, as in the webhook list. For a shared one, the form wraps a `<details>` (styled by the existing `form details` rules) whose summary is "Remove" and whose button reads "Remove from all N items".
  - Add form: `<form class="adjust" method="post" enctype="multipart/form-data" action="{{url_for('add_attachment', item_id=item['id'])}}" data-max-bytes="…">`. It has a label input, a file `input` with `accept="application/pdf,image/png,image/jpeg,image/webp,image/gif"`, a url input, and a `.check` share box that appears only when the item has a catalogue entry. The form is hidden when `edit_components` is off.
  - An `.empty` line shows when there are none.
  - Use the `icon` macro with the existing `file` symbol from `templates/base.html`.
- `templates/items.html`: in tile view, add `icon('file', 'small')` inside `.component-meta`, with `.visually-hidden` text "Has attachments". In list view, add a `small` with the same text under the name.
- `templates/settings.html`: add a **Download full backup** button with `icon('download')` next to the existing backup button.
- No nav changes.

### Script

`static/app.js` gets a small new handler for `form[data-max-bytes]`. On submit, if `input[type="file"].files[0].size` is over the limit, it blocks the submit and shows the size message next to the field. The server's check stays as the fallback, because a server that rejects a large body early often shows up on a phone as a connection reset, not a redirect.

### Webhooks

None.

## Edge cases

- **Spoofed type.** A file whose first bytes match no signature is rejected, whatever type the browser claimed, and nothing is written.
- **SVG.** Refused for attachments. Existing SVG images still upload, and the new sandbox header contains them when opened directly.
- **HEIC.** Rejected with "Use a PDF, PNG, JPEG, WebP or GIF. On iPhone, pick the photo from Photos rather than Files."
- **Size.** Over 20 MB is refused before upload when the script runs, and with a flash message when it doesn't. Reverse proxies have their own body limit (nginx defaults to 1 MB).
- **File and link both given.** "Choose a file or a link, not both."
- **Neither given.** "Choose a file or a link."
- **Empty label.** Default to the file name without its extension, or to "Datasheet". Labels are capped at 120 characters, and Jinja escapes them.
- **Database write fails.** If the insert fails after the file was saved, delete the file.
- **Forged `share`.** Ignored on an item with no catalogue entry. The row is saved for the item only.
- **Double tap on Remove.** The second request flashes "already removed".
- **Missing file on disk.** This happens after restoring a database-only backup. The attachment is still listed, and `/uploads` returns 404.
- **Feature switch.** With `edit_components` off, the list is visible, the form is hidden, and both new routes return 403.
- **Item deletion.** No route deletes items today. Whoever adds one must remove the item's attachments too.

## Tests

New file `tests/test_attachments.py`. Its `setUp` follows `tests/test_builds.py` and also points `main.UPLOAD_DIR` at the temporary directory.

- **PDF upload.** A post containing `b"%PDF-1.4..."` creates one row and one file. The item page links to `/uploads/<name>`, which returns 200 with `application/pdf`, `nosniff`, no sandbox, and a filename built from the label. A PDF sent as `application/octet-stream` is saved as `.pdf`.
- **Rejected files.** `text/html`, SVG, HEIC and a spoofed PDF each flash an error and leave no row and no file. Item images still accept SVG.
- **Size cap.** With `MAX_ATTACHMENT_BYTES` patched small, the post flashes and saves nothing, while an item image larger than that cap still saves.
- **Links.** https is saved. `javascript:`, `ftp:` and URLs carrying credentials are rejected.
- **Defaults and both-given.** An empty label falls back as described. A file plus a link is rejected.
- **Removal.** Removing deletes the row and the file. A second remove flashes "already removed". Another item's attachment returns 404.
- **Feature switch.** With `edit_components` off, both routes return 403 and the form is absent.
- **Sharing.** Two items from `/items/new?catalogue=<id>` both show a shared attachment, and an unrelated item does not. A forged `share=1` on an item with no entry saves an item-only row. Removing from the second item removes it from both.
- **Catalogue replace.** Entries that remain keep their ids. A dropped entry's shared attachment is copied down to its items. Every linked item's `catalogue_id` is cleared, including items whose entry had no attachments. Removing one copied row leaves the file on disk while another row uses it.
- **Migration.** A database with the old `items` schema, an unseeded catalogue and a `project.component_added` webhook row gains `catalogue_id`, backfills exact-name matches, and keeps its rows when `init_db` runs twice. This mirrors `test_lines_allocated_before_build_planning_become_one_consumed_build`.
- **Live refresh.** `/api/version` changes after an add and after a remove.
- **Full backup.** `/settings/backup.zip` contains `uploads/<name>` and a database that opens with `sqlite3` and holds a row written just before the download.
- **Components marker.** `/components` marks an item with its own attachment and an item whose only attachment is shared, and does not mark an item with none.

## Docs to update

- **README.md:**
  - Add an **Attachments.** bullet to Features.
  - Change `STORAGE_UPLOADS` to "Uploaded images and attachments".
  - In Backups, describe the full backup zip.
  - Under "Exposing it to the internet", mention the proxy's upload size limit and the 120 s timeout.
- **CONTRIBUTING.md:** new upload types go through an allowlist with a first-bytes check.
- **CONTEXT.md:**
  - Add **Attachment**: "A labelled file or link kept with an item or shared through its catalogue entry." _Avoid_: Document, file.
  - Add **Catalogue entry**: "A known part that quick add prefills from."
  - Note that sharing works per catalogue entry, not per component family.
- **SECURITY.md:** the data volume now also holds attachments.

## Open questions

- Is 20 MB the right per-file cap, and should each install also have a total cap?
- Should the catalogue format accept a `datasheet` URL field that seeds shared link attachments on import?
