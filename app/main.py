import os
import math
import uuid
import hashlib
import secrets
import json
import sqlite3
import csv
import re
import tempfile
import zipfile
import threading
import urllib.request
from io import BytesIO
from urllib.parse import urlparse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, flash, g, jsonify, redirect, render_template, request, url_for, session, send_file, send_from_directory
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import check_password_hash, generate_password_hash
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.units import mm
from reportlab.graphics import renderPDF
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.barcode.qr import QrCodeWidget
from app.webhooks import init_outbox, enqueue_event, start_worker
from app import providers as part_providers
from app.matching import parse_value, suggested_homes, value_ranked
from app.bag_labels import parse_bag_label
from app.bom import TEXT_FIELDS, package_of, parse_bom, prepare_items, match_line
from app.catalogue import FAMILIES, classify, common_suggestions, item_summary, parse_catalogue

DB_PATH = Path(os.environ.get("STORAGE_DB", "data/storage.db"))
UPLOAD_DIR = Path(os.environ.get("STORAGE_UPLOADS", "data/uploads"))
# Locations to create on first run: a bundled layout name (see app/layouts) or a path to a JSON file.
LAYOUT = os.environ.get("TALLY_LAYOUT", "")
LAYOUT_DIR = Path(__file__).parent / "layouts"
DEFAULT_CATALOGUE = Path(__file__).parent / "catalogue.json"
MAX_CATALOGUE_BYTES = 5 * 1024 * 1024
MAX_BOM_BYTES = 5 * 1024 * 1024
MAX_BOM_LINES = 300
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024

def secret_key():
    """Use SECRET_KEY when set; otherwise generate one beside the database and keep it."""
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = DB_PATH.parent / "secret_key"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write privately, then link into place, so concurrent workers agree on a single key.
        draft = path.with_name(f".secret_key.{os.getpid()}")
        with os.fdopen(os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as file:
            file.write(secrets.token_hex(32))
        try:
            os.link(draft, path)
        except FileExistsError:
            pass
        finally:
            draft.unlink()
    return path.read_text().strip()

app = Flask(__name__, template_folder="../templates", static_folder="../static")
app.config.update(SECRET_KEY=secret_key(), SESSION_COOKIE_SAMESITE="Lax")
ALLOWED_IMAGE_TYPES = {"image/svg+xml": ".svg", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
ATTACHMENT_TYPES = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
# First bytes of each attachment type. The browser's claimed type is ignored, since some Android pickers send PDFs as octet-stream.
ATTACHMENT_SIGNATURES = [(rb"%PDF-", "application/pdf", ".pdf"), (rb"\x89PNG\r\n\x1a\n", "image/png", ".png"), (rb"\xff\xd8\xff", "image/jpeg", ".jpg"), (rb"GIF8", "image/gif", ".gif"), (rb"RIFF.{4}WEBP", "image/webp", ".webp")]

def db():
    if "db" not in g:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_error):
    connection = g.pop("db", None)
    if connection: connection.close()

def init_db():
    conn = db()
    init_outbox(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS locations (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, notes TEXT DEFAULT '', image_path TEXT DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, manufacturer TEXT DEFAULT '', part_number TEXT DEFAULT '', quantity REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'pcs', minimum_quantity REAL, location_id INTEGER NOT NULL REFERENCES locations(id), notes TEXT DEFAULT '', image_path TEXT DEFAULT '', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS movements (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), quantity_change REAL NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS stock_receipts (token TEXT PRIMARY KEY, item_id INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '', image_path TEXT DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS project_items (project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id), quantity REAL NOT NULL DEFAULT 0, per_build REAL NOT NULL DEFAULT 0, PRIMARY KEY(project_id, item_id));
    CREATE TABLE IF NOT EXISTS builds (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, quantity INTEGER NOT NULL, lines TEXT NOT NULL, built_at TEXT NOT NULL, undone_at TEXT);
    CREATE TABLE IF NOT EXISTS webhooks (id INTEGER PRIMARY KEY, event TEXT NOT NULL, destination_url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS order_entries (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), quantity REAL NOT NULL, status TEXT NOT NULL DEFAULT 'wanted', source TEXT NOT NULL DEFAULT '', supplier TEXT NOT NULL DEFAULT '', order_ref TEXT NOT NULL DEFAULT '', expected_on TEXT, created_at TEXT NOT NULL, ordered_at TEXT, received_at TEXT, received_quantity REAL);
    CREATE UNIQUE INDEX IF NOT EXISTS order_entries_one_wanted ON order_entries(item_id) WHERE status='wanted';
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS catalogue (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, family TEXT NOT NULL DEFAULT 'generic', manufacturer TEXT DEFAULT '', part_number TEXT DEFAULT '', attributes TEXT NOT NULL DEFAULT '{}');
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
    """)
    item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "family" not in item_columns:
        conn.execute("ALTER TABLE items ADD COLUMN family TEXT NOT NULL DEFAULT 'generic'")
    if "attributes" not in item_columns:
        conn.execute("ALTER TABLE items ADD COLUMN attributes TEXT NOT NULL DEFAULT '{}'")
    if "image_path" not in item_columns:
        conn.execute("ALTER TABLE items ADD COLUMN image_path TEXT DEFAULT ''")
    def item_column_names():
        return {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if not {"supplier", "supplier_sku"} <= item_column_names():
        # Take the write lock and check again, so a second worker does not add a column twice.
        conn.execute("BEGIN IMMEDIATE")
        columns = item_column_names()
        if "supplier" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN supplier TEXT NOT NULL DEFAULT ''")
        if "supplier_sku" not in columns:
            conn.execute("ALTER TABLE items ADD COLUMN supplier_sku TEXT NOT NULL DEFAULT ''")
        conn.commit()
    location_columns = {row["name"] for row in conn.execute("PRAGMA table_info(locations)")}
    if "image_path" not in location_columns:
        conn.execute("ALTER TABLE locations ADD COLUMN image_path TEXT DEFAULT ''")
    if "keywords" not in location_columns:
        conn.execute("ALTER TABLE locations ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
    def location_column_names():
        return {row["name"] for row in conn.execute("PRAGMA table_info(locations)")}
    if "last_counted_at" not in item_column_names() or "last_counted_at" not in location_column_names():
        # Take the write lock and check again, so a second worker does not add a column twice.
        conn.execute("BEGIN IMMEDIATE")
        if "last_counted_at" not in item_column_names():
            conn.execute("ALTER TABLE items ADD COLUMN last_counted_at TEXT")
        if "last_counted_at" not in location_column_names():
            conn.execute("ALTER TABLE locations ADD COLUMN last_counted_at TEXT")
        conn.commit()
    def project_columns():
        return {row["name"] for row in conn.execute("PRAGMA table_info(project_items)")}
    if "per_build" not in project_columns():
        # A line's quantity is the stock it has consumed; per_build is what one build needs. Lines allocated
        # before build planning were taken from stock on the spot, so keep them as consumed, take them as the
        # recipe for one build, and log that build so it can be undone like any other. One write transaction
        # makes it all or nothing, and a second worker waits on the lock and then finds the column in place.
        conn.execute("BEGIN IMMEDIATE")
        if "per_build" not in project_columns():
            conn.execute("ALTER TABLE project_items ADD COLUMN per_build REAL NOT NULL DEFAULT 0")
            conn.execute("UPDATE project_items SET per_build=quantity")
            lines = {}
            for row in conn.execute("SELECT project_id, item_id, quantity FROM project_items ORDER BY project_id, item_id"):
                lines.setdefault(row["project_id"], []).append([row["item_id"], row["quantity"]])
            now = datetime.now(timezone.utc).isoformat()
            conn.executemany("INSERT INTO builds(project_id, quantity, lines, built_at) VALUES (?, 1, ?, ?)", [(project_id, json.dumps(rows), now) for project_id, rows in lines.items()])
        conn.commit()
    if conn.execute("SELECT 1 FROM webhooks WHERE event='project.component_added' LIMIT 1").fetchone():
        # Adding a line no longer moves stock, so destinations for the old project event now get the build instead.
        conn.execute("UPDATE webhooks SET event='project.built' WHERE event='project.component_added'")
    if LAYOUT and conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
        seed_layout(conn, load_layout(LAYOUT))
    elif LAYOUT and not conn.execute("SELECT 1 FROM settings WHERE key='layout.groups'").fetchone():
        # Storage created before layouts existed: keep it, but take the layout's names and hints.
        seed_layout(conn, load_layout(LAYOUT), existing=True)
    if not conn.execute("SELECT 1 FROM settings WHERE key='catalogue.seeded'").fetchone():
        save_catalogue(conn, parse_catalogue(DEFAULT_CATALOGUE.read_bytes()))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('catalogue.seeded', '1')")
    conn.commit()
    if "catalogue_id" not in item_column_names():
        # Link items to the catalogue entry they came from. Run after the seed, so an old database backfills against a full
        # catalogue, and only in the transaction that adds the column, so the backfill happens once.
        conn.execute("BEGIN IMMEDIATE")
        if "catalogue_id" not in item_column_names():
            conn.execute("ALTER TABLE items ADD COLUMN catalogue_id INTEGER")
            conn.execute("UPDATE items SET catalogue_id = (SELECT c.id FROM catalogue c WHERE c.name = items.name COLLATE NOCASE)")
        conn.commit()

def load_layout(name):
    path = LAYOUT_DIR / f"{name}.json" if (LAYOUT_DIR / f"{name}.json").exists() else Path(name)
    return json.loads(path.read_text())

def seed_layout(conn, layout, existing=False):
    if existing:
        conn.executemany(
            "UPDATE locations SET keywords=? WHERE code=? AND keywords=''",
            [(row["keywords"], row["code"]) for row in layout["locations"] if row.get("keywords")],
        )
    else:
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT INTO locations(code, kind, label, keywords, created_at) VALUES (?, ?, ?, ?, ?)",
            [(row["code"], row.get("kind", "storage"), row["label"], row.get("keywords", ""), now) for row in layout["locations"]],
        )
    for key in ("groups", "family_homes"):
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (f"layout.{key}", json.dumps(layout.get(key, {}))))

def save_catalogue(conn, entries, replace=False):
    """Add or update catalogue entries by name, keeping the ids that quick add links to."""
    conn.executemany(
        """INSERT INTO catalogue(name, family, manufacturer, part_number, attributes) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET name=excluded.name, family=excluded.family, manufacturer=excluded.manufacturer, part_number=excluded.part_number, attributes=excluded.attributes""",
        [(entry["name"], entry["family"], entry["manufacturer"], entry["part_number"], json.dumps(entry["attributes"], ensure_ascii=False)) for entry in entries],
    )
    if replace:
        kept = {entry["name"].casefold() for entry in entries}
        dropped = [row["id"] for row in conn.execute("SELECT id, name FROM catalogue") if row["name"].casefold() not in kept]
        for entry_id in dropped:
            # Shared attachments go down to each linked item, which then keeps them on its own.
            conn.execute(
                """INSERT INTO attachments(item_id, label, file_path, url, mimetype, size_bytes, created_at)
                   SELECT i.id, a.label, a.file_path, a.url, a.mimetype, a.size_bytes, a.created_at
                   FROM attachments a JOIN items i ON i.catalogue_id=a.catalogue_id WHERE a.catalogue_id=?""", (entry_id,))
            conn.execute("DELETE FROM attachments WHERE catalogue_id=?", (entry_id,))
            # An id can be reused by a later entry, so unlink every item whether or not it had attachments.
            conn.execute("UPDATE items SET catalogue_id=NULL WHERE catalogue_id=?", (entry_id,))
            conn.execute("DELETE FROM catalogue WHERE id=?", (entry_id,))

def catalogue_entries():
    rows = db().execute("SELECT * FROM catalogue ORDER BY name COLLATE NOCASE")
    return [{**dict(row), "attributes": json.loads(row["attributes"])} for row in rows]

def catalogue_item(entry_id):
    row = db().execute("SELECT * FROM catalogue WHERE id=?", (entry_id,)).fetchone() if str(entry_id or "").isdigit() else None
    return {**dict(row), "attributes": json.loads(row["attributes"])} if row else None

def fetch_catalogue(url):
    if not valid_webhook_url(url):
        raise ValueError("Enter a valid http(s) catalogue URL.")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Tally/1.0"}), timeout=10) as response:
            raw = response.read(MAX_CATALOGUE_BYTES + 1)
    except (OSError, ValueError) as error:
        raise ValueError(f"Could not download the catalogue: {error}") from error
    if len(raw) > MAX_CATALOGUE_BYTES:
        raise ValueError("The catalogue is larger than 5 MB.")
    return parse_catalogue(raw)

def layout_setting(key):
    row = db().execute("SELECT value FROM settings WHERE key=?", (f"layout.{key}",)).fetchone()
    return json.loads(row["value"]) if row else {}

@app.before_request
def ensure_database():
    init_db()
    if app.config.get("WEBHOOK_WORKER_ENABLED", True):
        start_worker(DB_PATH, app.logger)
    password = db().execute("SELECT value FROM settings WHERE key='access.password_hash'").fetchone()
    public_endpoints = {"access", "logout", "static"}
    if auth_enabled() and request.endpoint not in public_endpoints and (not password or session.get("access_unlocked") != access_token(password["value"])):
        return redirect(url_for("access", next=request.full_path))
    restricted = {"new_item": "add_components", "export_inventory": "exports", "export_locations": "exports", "export_bom": "exports", "export_orders": "exports", "location_labels_pdf": "exports", "item_label_pdf": "exports", "add_attachment": "edit_components", "delete_attachment": "edit_components"}
    if request.endpoint in restricted and not enabled(restricted[request.endpoint]):
        abort(403)
    if request.endpoint == "item_detail" and request.method == "POST" and not enabled("edit_components"):
        abort(403)

@app.template_filter("qty")
def format_quantity(value):
    """Show whole quantities without a trailing .0 and fractional ones without float noise."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return str(int(number)) if number == int(number) else f"{number:.3f}".rstrip("0").rstrip(".")

@app.template_filter("expected_date")
def expected_date(value):
    return date.fromisoformat(value).strftime("%-d %b")

@app.context_processor
def feature_context():
    return {"images_enabled": enabled("images"), "auth_enabled": auth_enabled(), "exports_enabled": enabled("exports")}

def save_upload(field_name, allowed, signatures=None):
    """Save an upload under an unguessable local name and return it with its type, or None if nothing was sent.
    With signatures, the type comes from the file's first bytes and the browser's claim is ignored."""
    upload = request.files.get(field_name)
    if not upload or not upload.filename:
        return None
    if signatures:
        head = upload.stream.read(16)
        upload.stream.seek(0)
        mimetype, suffix = next(((kind, ext) for pattern, kind, ext in signatures if re.match(pattern, head, re.DOTALL)), (None, None))
        if mimetype not in allowed:
            raise ValueError("Use a PDF, PNG, JPEG, WebP or GIF. On iPhone, pick the photo from Photos rather than Files.")
    else:
        mimetype = upload.mimetype
        suffix = allowed.get(mimetype)
        if not suffix:
            raise ValueError("Use an SVG, PNG, JPEG, WebP, or GIF image.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{suffix}"
    upload.save(UPLOAD_DIR / filename)
    return filename, mimetype

def image_upload(field_name):
    """Save a user-supplied component/location image under an unguessable local name."""
    saved = save_upload(field_name, ALLOWED_IMAGE_TYPES)
    return saved[0] if saved else ""

def valid_webhook_url(value):
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password

def valid_base_url(value):
    parsed = urlparse(value)
    return valid_webhook_url(value) and not (parsed.query or parsed.fragment or parsed.params)

def enabled(flag):
    row = db().execute("SELECT value FROM settings WHERE key=?", (f"feature.{flag}",)).fetchone()
    return row is None or row["value"] == "1"

def auth_enabled():
    row = db().execute("SELECT value FROM settings WHERE key='auth.enabled'").fetchone()
    return row is not None and row["value"] == "1"

def access_token(password_hash):
    # The session cookie is signed but readable, so it carries a digest rather than the password hash itself.
    return hashlib.sha256(password_hash.encode()).hexdigest()

def set_setting(key, value):
    db().execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

def provider_configs():
    """Each provider's saved settings, with `enabled` falling back to the provider's default."""
    stored = {row["key"]: row["value"] for row in db().execute("SELECT key, value FROM settings WHERE key LIKE 'provider.%'")}
    configs = {}
    for provider_id, provider in part_providers.PROVIDERS.items():
        config = {key: stored.get(f"provider.{provider_id}.{key}", "") for key, _label, _secret in provider["fields"]}
        flag = stored.get(f"provider.{provider_id}.enabled")
        config["enabled"] = provider["default_enabled"] if flag is None else flag == "1"
        configs[provider_id] = config
    return configs

@app.route("/access", methods=["GET", "POST"])
def access():
    password = db().execute("SELECT value FROM settings WHERE key='access.password_hash'").fetchone()
    if not auth_enabled() or not password:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if check_password_hash(password["value"], request.form.get("password", "")):
            session["access_unlocked"] = access_token(password["value"])
            dispatch_webhooks("access.unlocked", {})
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("dashboard"))
        flash("That password is not correct.", "error")
    return render_template("access.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("access_unlocked", None)
    dispatch_webhooks("access.locked", {})
    flash("Signed out.", "success")
    return redirect(url_for("access"))

WEBHOOK_EVENTS = {
    "stock.in": "Stock in", "stock.out": "Stock out", "stock.returned": "Stock returned",
    "stock.lost": "Stock lost / damaged", "item.created": "Item created",
    "item.found": "Item located", "item.viewed": "Item opened",
    "location.created": "Storage location created", "project.created": "Project created",
    "project.line_added": "Project line added", "project.line_updated": "Project line updated",
    "project.line_removed": "Project line removed", "project.built": "Project built",
    "project.build_undone": "Project build undone", "catalogue.imported": "Catalogue imported",
    "settings.updated": "Settings updated", "webhook.created": "Webhook added",
    "webhook.deleted": "Webhook removed", "access.unlocked": "Signed in", "access.locked": "Signed out",
    "export.created": "Export downloaded", "order.added": "Order list entries added",
}


def item_payload(item_id):
    row = db().execute("SELECT i.*, l.code, l.label AS location_label, l.kind FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone()
    if row is None:
        abort(404)
    item = dict(row)
    item["attributes"] = json.loads(item["attributes"] or "{}")
    item["location"] = item.pop("code")
    return {"item": item, "location": {"id": item["location_id"], "code": item["location"], "label": item["location_label"], "kind": item.pop("kind")}}


def dispatch_webhooks(event, payload):
    enqueue_event(db(), event, payload)


@app.after_request
def commit_read_events(response):
    # Read-only actions (view, locate, exports, login) may also append events.
    # Mutation routes commit their events together with their domain changes.
    if "db" in g:
        if response.status_code < 400:
            g.db.commit()
        else:
            g.db.rollback()
    return response


def record_movement(conn, item, change, reason, event, project=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("UPDATE items SET quantity=quantity+?, updated_at=? WHERE id=?", (change, now, item["id"]))
    conn.execute("INSERT INTO movements(item_id,quantity_change,reason,occurred_at) VALUES (?,?,?,?)", (item["id"], change, reason, now))
    dispatch_webhooks(event, {"item": {"id": item["id"], "name": item["name"], "part_number": item["part_number"], "location": item["code"]}, "quantity_change": change, "unit": item["unit"], "reason": reason, "project": project})

def low_stock(conn):
    """Every item at or below its minimum, lowest first."""
    return conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.minimum_quantity IS NOT NULL AND i.quantity <= i.minimum_quantity ORDER BY i.quantity").fetchall()

def on_order(conn):
    """The total quantity on order and the earliest expected date, keyed by item id."""
    return {row["item_id"]: row for row in conn.execute("SELECT item_id, sum(quantity) AS quantity, min(expected_on) AS expected_on FROM order_entries WHERE status='ordered' GROUP BY item_id")}

@app.route("/")
def dashboard():
    conn = db()
    stats = conn.execute("SELECT (SELECT count(*) FROM items) items, (SELECT count(*) FROM locations) locations").fetchone()
    recent = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id ORDER BY i.updated_at DESC LIMIT 8").fetchall()
    low = low_stock(conn)
    projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC LIMIT 4").fetchall()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=COUNT_DUE_DAYS)).isoformat()
    due = conn.execute("""SELECT l.id, l.code, l.label, count(i.id) AS item_count, min(coalesce(i.last_counted_at, '')) AS oldest
        FROM locations l JOIN items i ON i.location_id = l.id
        GROUP BY l.id HAVING oldest < ? ORDER BY oldest, l.code""", (cutoff,)).fetchall()
    return render_template("dashboard.html", stats=stats, low_count=len(low), recent=recent, low=low[:8], due=due[:8], due_count=len(due), ordered=on_order(conn), projects=projects, buildable=project_build_counts(conn))

@app.route("/components")
@app.route("/items")
def items():
    query = request.args.get("q", "").strip()
    view = request.args.get("view", "tiles")
    if view not in {"tiles", "list"}:
        view = "tiles"
    family = request.args.get("family") if request.args.get("family") in FAMILIES else None
    sql = "SELECT i.*, l.code, l.label AS location_label, (SELECT COUNT(*) FROM attachments a WHERE a.item_id=i.id OR (i.catalogue_id IS NOT NULL AND a.catalogue_id=i.catalogue_id)) AS attachment_count FROM items i JOIN locations l ON l.id=i.location_id"
    args = []
    if family:
        sql += " WHERE i.family = ?"
        args.append(family)
    rows = db().execute(sql + " ORDER BY i.name COLLATE NOCASE", args).fetchall()
    in_family = [{**row, "attributes": item_attributes(row)} for row in map(dict, rows)] if family else []
    shown = value_ranked(query, rows) if query else [dict(row) for row in rows]
    filters = {"q": query or None, "view": view if view != "tiles" else None, "family": family}
    fields = [field for field in FAMILIES[family]["fields"] if field[0] not in ("value", "value_unit")] if family else []
    compact = lambda text: re.sub(r"\s+", "", str(text)).casefold()
    for key, label, *_ in fields:
        wanted = request.args.get(f"attr_{key}", "")
        if wanted:
            filters[f"attr_{key}"] = wanted
            shown = [row for row in shown if compact(item_attributes(row).get(key, "")) == compact(wanted)]
    unit = {"capacitor": "F", "resistor": "Ω"}.get(family)
    limits = [request.args.get(name, "").strip() for name in ("min", "max")]
    if any(limits):
        bounds = [parse_value(limit, unit) if limit else None for limit in limits] if unit else [None, None]
        if not unit or any(limit and bound is None for limit, bound in zip(limits, bounds)):
            flash("Choose the Resistor or Capacitor family and a value such as 10u or 4k7 to filter by range.", "error")
        else:
            low, high = bounds
            if None not in bounds and low > high:
                low, high = high, low
            filters.update(min=limits[0] or None, max=limits[1] or None)
            shown = [row for row in shown if row_value(row, unit) is not None and (low is None or row_value(row, unit) >= low) and (high is None or row_value(row, unit) <= high)]
    chips = []
    for key, label, *_ in fields:
        counts = {}
        for row in in_family:
            text = row["attributes"].get(key, "").strip()
            if text:
                counts.setdefault(compact(text), {}).setdefault(text, 0)
                counts[compact(text)][text] += 1
        values = sorted(((max(texts, key=texts.get), sum(texts.values())) for texts in counts.values()), key=lambda pair: (-pair[1], pair[0].casefold()))
        chips.append({"key": key, "label": label, "values": values if request.args.get("more") == key else values[:8], "more": len(values) > 8 and request.args.get("more") != key})
    for row in shown:
        row["summary"] = item_summary(row)
    families = [(key, FAMILIES[key]["label"], count) for key, count in db().execute("SELECT family, count(*) FROM items GROUP BY family").fetchall() if key in FAMILIES]
    return render_template("items.html", items=shown, query=query, view=view, family=family, families=families, chips=chips, filters=filters, compact=compact, can_range=bool(unit),
                           filtering=any(name.startswith("attr_") or name in ("min", "max") for name in filters if filters[name]))


def item_attributes(row):
    """An item's attributes as text, or nothing when the stored JSON is not an object."""
    try:
        attributes = json.loads(row["attributes"] or "{}")
    except (ValueError, TypeError):
        return {}
    return {str(key): str(value) for key, value in attributes.items()} if isinstance(attributes, dict) else {}


def row_value(row, unit):
    """The number in an item's value and unit attributes, or None."""
    attributes = item_attributes(row)
    return parse_value(f"{attributes.get('value', '')} {attributes.get('value_unit', '')}", unit)

@app.route("/stock")
def stock():
    query = request.args.get("q", "").strip()
    location = None
    if request.args.get("location", type=int) is not None:
        location = db().execute("SELECT * FROM locations WHERE id=?", (request.args.get("location", type=int),)).fetchone()
        if location is None:
            flash("That storage location no longer exists.", "error")
            return redirect(url_for("locations"))
    sql = "SELECT i.*, l.code, l.label AS location_label FROM items i JOIN locations l ON l.id=i.location_id"
    where, args = [], []
    if query:
        where.append("(i.name LIKE ? OR i.part_number LIKE ? OR l.code LIKE ?)")
        args = [f"%{query}%"] * 3
    if location:
        where.append("l.id=?")
        args.append(location["id"])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY i.name COLLATE NOCASE"
    chosen = request.args.get("add", type=int)
    recent = db().execute("SELECT m.*, i.name, i.unit FROM movements m JOIN items i ON i.id=m.item_id ORDER BY m.occurred_at DESC LIMIT 10").fetchall()
    return render_template("stock.html", items=db().execute(sql, args).fetchall(), query=query, selected_id=chosen, recent=recent, location=location, can_add=enabled("add_components"))

def back_to_stock():
    location = request.form.get("location", type=int)
    if location is not None and db().execute("SELECT 1 FROM locations WHERE id=?", (location,)).fetchone():
        return redirect(url_for("stock", location=location))
    return redirect(url_for("stock"))

@app.route("/stock/checkout", methods=["POST"])
def stock_checkout():
    try:
        cart = json.loads(request.form.get("cart", "[]"))
        action = request.form.get("action", "stock-out")
        if action not in {"stock-in", "stock-out", "return", "lost"} or not isinstance(cart, list) or not cart:
            raise ValueError()
    except (ValueError, TypeError, json.JSONDecodeError):
        flash("Add at least one component and choose a stock action.", "error")
        return back_to_stock()
    direction = {"stock-in": 1, "stock-out": -1, "return": 1, "lost": -1}[action]
    labels = {"stock-in": ("Stock received", "stock.in"), "stock-out": ("Stock used", "stock.out"), "return": ("Stock returned", "stock.returned"), "lost": ("Marked lost or damaged", "stock.lost")}
    conn = db()
    prepared = []
    try:
        for line in cart:
            item_id, quantity = int(line["id"]), float(line["quantity"])
            if not math.isfinite(quantity) or quantity <= 0:
                raise ValueError()
            item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone()
            if item is None or (direction < 0 and item["quantity"] < quantity):
                raise ValueError()
            prepared.append((item, quantity))
    except (KeyError, ValueError, TypeError):
        flash("Check the quantities. Stock out and lost actions cannot take more than is on hand.", "error")
        return back_to_stock()
    with conn:
        for item, quantity in prepared:
            record_movement(conn, item, direction * quantity, labels[action][0], labels[action][1])
    flash(f"{labels[action][0]} for {len(prepared)} component{'s' if len(prepared) != 1 else ''}.", "success")
    return back_to_stock()

@app.route("/api/search")
def search():
    """Small, fast suggestions for the inventory's structured vocabulary."""
    query = request.args.get("q", "").strip()
    if len(query) < 2:
        return jsonify([])
    return jsonify(find_stock(query)[:8])


def find_stock(query):
    rows = db().execute("""SELECT i.*, l.code, l.label FROM items i
        JOIN locations l ON l.id=i.location_id ORDER BY i.name COLLATE NOCASE""").fetchall()
    return [{**row, "summary": item_summary(row)} for row in value_ranked(query, rows)]


def scan_values():
    """What a bag label read, carried through Quick add and New component as query or form values."""
    values = {key: request.values.get(key, "").strip()[:200] for key in ("quantity", "supplier", "supplier_sku", "expected", "scan", "manufacturer", "catalogue")}
    return {key: value for key, value in values.items() if value}


def scan_redirect(label):
    """Send a scanned label to its single matching item, or to the matches for its part number."""
    conn = db()
    sql = "SELECT i.id FROM items i WHERE trim(i.{0}) = ? COLLATE NOCASE"
    part_number = label["part_number"]
    hits = conn.execute(sql.format("part_number"), (part_number,)).fetchall() if part_number else []
    if not hits and label["supplier_sku"]:
        hits = conn.execute(sql.format("supplier_sku"), (label["supplier_sku"],)).fetchall()
    values = {"q": part_number or label["supplier_sku"], "quantity": label["quantity"], "supplier": label["supplier"],
              "supplier_sku": label["supplier_sku"], "expected": label["quantity"], "scan": 1}
    if len(hits) == 1:
        return redirect(url_for("quick_add", item=hits[0]["id"], **values))
    entry = conn.execute("SELECT id FROM catalogue WHERE trim(part_number) = ? COLLATE NOCASE", (part_number,)).fetchone() if part_number else None
    if entry:
        values["catalogue"] = entry["id"]
    return redirect(url_for("quick_add", manufacturer=label["manufacturer"] or None, **values))


@app.route("/quick-add", methods=["GET", "POST"])
def quick_add():
    query = request.values.get("q", "").strip()
    if request.method == "GET" and not request.args.get("fragment"):
        try:
            label = parse_bag_label(query)
        except ValueError:
            flash("Tally could not read this label. Set your scanner to send GS (Ctrl+]).", "error")
            query = ""
        else:
            if label:
                return scan_redirect(label)
    query = query[:200]
    session.setdefault("quick_csrf", uuid.uuid4().hex)
    selected = None
    selected_id = request.values.get("item")
    if selected_id:
        selected = db().execute("""SELECT i.*, l.code, l.label FROM items i
            JOIN locations l ON l.id=i.location_id WHERE i.id=?""", (selected_id,)).fetchone()
        if selected is None:
            abort(404)
    if request.method == "POST":
        if request.form.get("csrf") != session["quick_csrf"]:
            abort(400)
        token = request.form.get("token", "")
        try:
            uuid.UUID(token)
            quantity = float(request.form.get("quantity", ""))
            if not math.isfinite(quantity) or quantity <= 0 or selected is None:
                raise ValueError()
        except (ValueError, TypeError):
            flash("Choose an item and enter a quantity greater than zero.", "error")
        else:
            scanned = bool(request.form.get("scan"))
            try:
                expected = float(request.form.get("expected", ""))
            except ValueError:
                expected = 0
            if scanned and math.isfinite(expected) and expected > 0 and quantity > 10 * expected:
                flash(f"Check the quantity. The bag label says {expected:g}.", "error")
            else:
                conn = db()
                supplier, supplier_sku = request.form.get("supplier", "").strip()[:200], request.form.get("supplier_sku", "").strip()[:200]
                with conn:
                    inserted = conn.execute("INSERT OR IGNORE INTO stock_receipts(token,item_id) VALUES (?,?)", (token, selected["id"])).rowcount
                    if inserted:
                        record_movement(conn, selected, quantity, "Stock received", "stock.in")
                        if supplier_sku:
                            conn.execute("UPDATE items SET supplier=?, supplier_sku=? WHERE id=? AND supplier='' AND supplier_sku=''", (supplier, supplier_sku, selected["id"]))
                flash(f"Added {quantity:g} {selected['unit']} · {selected['name']} · {selected['code']}" if inserted else "This addition was already saved.", "success")
                return redirect(url_for("quick_add") if scanned else url_for("quick_add", q=query))
    matches = find_stock(query)[:8] if len(query) >= 2 else []
    catalogue = common_suggestions(query, catalogue_entries()) if len(query) >= 2 else []
    locations = db().execute("SELECT * FROM locations ORDER BY code").fetchall()
    homes = suggested_homes(query, locations) if len(query) >= 2 else []
    configs = provider_configs()
    active = [pid for pid, config in configs.items() if config["enabled"] and part_providers.is_configured(pid, config)]
    provider_results = provider_errors = None
    if request.args.get("providers") and len(query) >= 2 and active:
        provider_results, provider_errors = part_providers.search_all(query, configs)
        for results in provider_results.values():
            for entry in results:
                entry["link"] = url_for("new_item", name=entry["name"], family=entry["family"], manufacturer=entry["manufacturer"], part_number=entry["part_number"],
                                        **{f"attr_{key}": value for key, value in entry["attributes"].items()})
    scan = scan_values()
    new_args = {**scan, **({"part_number": query} if scan.get("scan") else {"name": query})}
    context = dict(query=query, scan=scan, new_args=new_args, matches=matches, homes=homes, catalogue=catalogue, selected=selected, token=str(uuid.uuid4()),
                   providers=part_providers.PROVIDERS, active_providers=active, provider_results=provider_results, provider_errors=provider_errors)
    return render_template("quick_results.html" if request.args.get("fragment") else "quick_add.html", **context)

@app.route("/api/item-guide")
def item_guide():
    query = request.args.get("q", "").strip()[:200]
    family_key = classify(query)
    family = FAMILIES[family_key]
    return jsonify(family=family_key, label=family["label"], home=layout_setting("family_homes").get(family_key, ""), fields=family["fields"], suggestions=common_suggestions(query, catalogue_entries()))

@app.route("/api/version")
def version():
    """A cheap change marker used by display and list views for live refresh."""
    marker = db().execute(
        """SELECT coalesce(max(changed), '') AS value FROM (
           SELECT updated_at AS changed FROM items
           UNION ALL SELECT occurred_at AS changed FROM movements
           UNION ALL SELECT created_at AS changed FROM locations
           UNION ALL SELECT created_at FROM order_entries
           UNION ALL SELECT ordered_at FROM order_entries
           UNION ALL SELECT received_at FROM order_entries
           UNION ALL SELECT value FROM settings WHERE key='attachments.changed'
           UNION ALL SELECT last_counted_at FROM items
           UNION ALL SELECT last_counted_at FROM locations
        )"""
    ).fetchone()["value"]
    return jsonify(version=marker)

@app.route("/components/new", methods=["GET", "POST"])
@app.route("/items/new", methods=["GET", "POST"])
def new_item():
    locations = db().execute("SELECT * FROM locations ORDER BY code").fetchall()
    suggestion = catalogue_item(request.args.get("catalogue"))
    suggested_name = request.args.get("name", "")[:200]
    selected_family = request.form.get("family") or (suggestion["family"] if suggestion else classify(suggested_name))
    if selected_family not in FAMILIES:
        selected_family = "generic"
    suggested_location = request.args.get("location", "")
    if not suggested_location and selected_family != "generic":
        home = layout_setting("family_homes").get(selected_family)
        row = next((location for location in locations if location["code"] == home), None)
        suggested_location = str(row["id"]) if row else ""
    scanned = {key: request.form.get(key, request.args.get(key, "")).strip()[:200] for key in ("supplier", "supplier_sku")}
    initial = suggestion or {
        "name": suggested_name, "manufacturer": request.args.get("manufacturer", "")[:200], "part_number": request.args.get("part_number", "")[:200],
        "attributes": {key.removeprefix("attr_")[:64]: value[:200] for key, value in request.args.items() if key.startswith("attr_")},
    }
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        error = None
        try:
            quantity = float(request.form.get("quantity") or 0)
            minimum = float(request.form["minimum_quantity"]) if request.form.get("minimum_quantity") else None
            location_id = int(request.form.get("location_id", ""))
            if not math.isfinite(quantity) or quantity < 0:
                raise ValueError()
            if minimum is not None and (not math.isfinite(minimum) or minimum < 0):
                raise ValueError()
            if not any(row["id"] == location_id for row in locations):
                raise ValueError()
        except (ValueError, TypeError):
            error = "Enter a valid quantity and choose a storage location."
        if error:
            flash(error, "error")
        elif not name:
            flash("An item needs a name.", "error")
        else:
            now = datetime.now(timezone.utc).isoformat()
            conn = db()
            attributes = {key.removeprefix("attr_"): value.strip() for key, value in request.form.items() if key.startswith("attr_") and value.strip()}
            manufacturer = request.form.get("manufacturer", "").strip()
            part_number = request.form.get("part_number", "").strip()
            try:
                image_path = image_upload("image") if enabled("images") else ""
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("item_form.html", item=None, locations=locations, selected_location=suggested_location, suggested_name=initial["name"], suggested_manufacturer=initial.get("manufacturer", ""), suggested_part_number=initial.get("part_number", ""), suggested_attributes=initial.get("attributes", {}), families=FAMILIES, family_homes=layout_setting("family_homes"), selected_family=selected_family, scanned=scanned)
            cur = conn.execute("INSERT INTO items(name, manufacturer, part_number, quantity, unit, minimum_quantity, location_id, notes, image_path, updated_at, family, attributes, supplier, supplier_sku, catalogue_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (name, manufacturer, part_number, quantity, request.form.get("unit", "pcs").strip() or "pcs", minimum, location_id, request.form.get("notes", "").strip(), image_path, now, selected_family, json.dumps(attributes, ensure_ascii=False), scanned["supplier"], scanned["supplier_sku"], suggestion["id"] if suggestion else None))
            if quantity: conn.execute("INSERT INTO movements(item_id, quantity_change, reason, occurred_at) VALUES (?, ?, ?, ?)", (cur.lastrowid, quantity, "Initial stock", now))
            dispatch_webhooks("item.created", item_payload(cur.lastrowid))
            if quantity:
                dispatch_webhooks("stock.in", {**item_payload(cur.lastrowid), "quantity_change": quantity, "unit": request.form.get("unit", "pcs"), "reason": "Initial stock", "project": None})
            conn.commit()
            flash("Item added.", "success")
            return redirect(url_for("item_detail", item_id=cur.lastrowid))
    if request.method == "POST":
        initial = {
            "name": request.form.get("name", ""),
            "manufacturer": request.form.get("manufacturer", ""),
            "part_number": request.form.get("part_number", ""),
            "attributes": {
                key.removeprefix("attr_"): value
                for key, value in request.form.items()
                if key.startswith("attr_")
            },
        }
        suggested_location = request.form.get("location_id", "")
    return render_template("item_form.html", item=None, locations=locations, selected_location=suggested_location, suggested_name=initial["name"], suggested_manufacturer=initial.get("manufacturer", ""), suggested_part_number=initial.get("part_number", ""), suggested_attributes=initial.get("attributes", {}), families=FAMILIES, family_homes=layout_setting("family_homes"), selected_family=selected_family, scanned=scanned)

@app.route("/items/<int:item_id>", methods=["GET", "POST"])
def item_detail(item_id):
    conn = db()
    item = conn.execute("SELECT i.*, l.code, l.label AS location_label FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone()
    if not item: abort(404)
    if request.method == "POST":
        change = float(request.form.get("quantity_change") or 0)
        if change == 0 or not math.isfinite(change): flash("Enter a non-zero stock change.", "error")
        else:
            reason = request.form.get("reason", "Stock adjustment").strip() or "Stock adjustment"
            record_movement(conn, item, change, reason, "stock.in" if change > 0 else "stock.out")
            conn.commit()
            flash("Stock updated.", "success")
            return redirect(url_for("item_detail", item_id=item_id))
    if request.method == "GET":
        dispatch_webhooks("item.viewed", item_payload(item_id))
    movements = conn.execute("SELECT * FROM movements WHERE item_id=? ORDER BY occurred_at DESC", (item_id,)).fetchall()
    try:
        attributes = json.loads(item["attributes"] or "{}")
    except (TypeError, json.JSONDecodeError):
        attributes = {}
    family = FAMILIES.get(item["family"], FAMILIES["generic"])
    attribute_labels = {field[0]: field[1] for field in family["fields"]}
    entry_id = item["catalogue_id"]
    attachments = conn.execute("SELECT * FROM attachments WHERE item_id=? OR (? IS NOT NULL AND catalogue_id=?) ORDER BY id", (item_id, entry_id, entry_id)).fetchall()
    linked_items = conn.execute("SELECT COUNT(*) FROM items WHERE catalogue_id=?", (entry_id,)).fetchone()[0] if entry_id else 0
    return render_template(
        "item_detail.html",
        attachments=attachments,
        linked_items=linked_items,
        can_attach=enabled("edit_components"),
        max_attachment_bytes=MAX_ATTACHMENT_BYTES,
        item=item,
        movements=movements,
        attributes=attributes,
        family=family,
        attribute_labels=attribute_labels,
        ordered=on_order(conn).get(item_id),
        suggested=suggested_order_quantity(item),
    )

@app.route("/items/<int:item_id>/find", methods=["POST"])
def find_item(item_id):
    payload = item_payload(item_id)
    dispatch_webhooks("item.found", payload)
    if request.is_json:
        return jsonify(payload)
    flash(f"Locate {payload['item']['name']} at {payload['location']['code']}.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


def drawer_order(row):
    return ({'small drawer': 0, 'medium drawer': 1, 'large drawer': 2}.get(row['kind'], 3), row['code'])

def select_locations(group="", q="", code=""):
    """The storage rows a page or label sheet shows: one code, a search, or a group in drawer order."""
    rows = db().execute("SELECT l.*, count(i.id) AS item_count FROM locations l LEFT JOIN items i ON i.location_id=l.id GROUP BY l.id ORDER BY l.code").fetchall()
    if code:
        return [row for row in rows if row['code'] == code]
    if q:
        return [row for row in rows if q.casefold() in (row['code'] + ' ' + row['label']).casefold()]
    if group:
        rows = [row for row in rows if row['code'].split()[0] == group]
    return sorted(rows, key=drawer_order)

@app.route("/locations")
def locations():
    rows = db().execute("SELECT l.*, count(i.id) AS item_count FROM locations l LEFT JOIN items i ON i.location_id=l.id GROUP BY l.id ORDER BY l.code").fetchall()
    groups = {}
    names = layout_setting("groups")
    for row in rows:
        key = row['code'].split()[0]
        groups.setdefault(key, {'name': names.get(key, key), 'rows': []})['rows'].append(row)
    groups = dict(sorted(groups.items(), key=lambda pair: (pair[0] not in names, list(names).index(pair[0]) if pair[0] in names else pair[0])))
    query = request.args.get('q', '').strip()
    active = request.args.get('group', '')
    selected = select_locations(active, query)
    address = label_base_url() or request.host_url
    count_first = next((row for row in selected if row['item_count']), None) if active and not query else None
    return render_template("locations.html", locations=selected, count_first=count_first, groups=groups, active=active, query=query, presets=LABEL_PRESETS, default_preset=default_preset(), label_address=address, label_warning=label_address_warning(address))

@app.route("/l/<path:code>")
def scan_location(code):
    """The address in a drawer's QR code: open that drawer's stock, forgiving of letter case."""
    conn = db()
    row = conn.execute("SELECT id FROM locations WHERE code=?", (code,)).fetchone()
    if row is None:
        rows = conn.execute("SELECT id FROM locations WHERE code=? COLLATE NOCASE", (code,)).fetchall()
        row = rows[0] if len(rows) == 1 else None
    if row is None:
        flash(f"No storage location has the code {code}.", "error")
        return redirect(url_for("locations", q=code))
    return redirect(url_for("stock", location=row["id"]))

@app.route("/locations/new", methods=["POST"])
def new_location():
    code, label = request.form.get("code", "").strip().upper(), request.form.get("label", "").strip()
    if not code or not label: flash("Location code and label are required.", "error")
    else:
        try:
            image_path = image_upload("image") if enabled("images") else ""
            location_id = db().execute("INSERT INTO locations(code, kind, label, notes, image_path, created_at) VALUES (?, ?, ?, ?, ?, ?)", (code, request.form.get("kind", "custom storage"), label, request.form.get("notes", ""), image_path, datetime.now(timezone.utc).isoformat())).lastrowid
            dispatch_webhooks("location.created", {"location": dict(db().execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone())})
            db().commit()
            flash("Storage location added.", "success")
        except sqlite3.IntegrityError: flash("That location code already exists.", "error")
        except ValueError as exc: flash(str(exc), "error")
    return redirect(url_for("locations"))

@app.route("/uploads/<path:filename>")
def uploaded_image(filename):
    attachment = db().execute("SELECT label FROM attachments WHERE file_path=? LIMIT 1", (filename,)).fetchone()
    name = f"{attachment['label']}{Path(filename).suffix}" if attachment else None
    response = send_from_directory(UPLOAD_DIR, filename, download_name=name)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if response.mimetype != "application/pdf":
        response.headers["Content-Security-Policy"] = "sandbox"
    return response

def touch_attachments(conn):
    """Move the live-refresh marker without touching any item."""
    conn.execute("INSERT INTO settings(key, value) VALUES ('attachments.changed', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (datetime.now(timezone.utc).isoformat(),))

@app.route("/items/<int:item_id>/attachments", methods=["POST"])
def add_attachment(item_id):
    conn = db()
    item = conn.execute("SELECT id, catalogue_id FROM items WHERE id=?", (item_id,)).fetchone()
    if not item: abort(404)
    back = redirect(url_for("item_detail", item_id=item_id))
    # Raise the cap for this request only, before the body is read, so other uploads keep their limits.
    request.max_content_length = MAX_ATTACHMENT_BYTES
    try:
        label, url, share = request.form.get("label", "").strip()[:120], request.form.get("url", "").strip(), request.form.get("share")
        upload = request.files.get("file")
    except RequestEntityTooLarge:
        flash("Files can be up to 20 MB.", "error")
        return back
    has_file = bool(upload and upload.filename)
    if has_file and url:
        flash("Choose a file or a link, not both.", "error")
        return back
    if not has_file and not url:
        flash("Choose a file or a link.", "error")
        return back
    if url and not valid_webhook_url(url):
        flash("Enter a valid http(s) link.", "error")
        return back
    label = label or (Path(upload.filename).stem[:120] if has_file else "") or "Datasheet"
    filename, mimetype, size = "", "", 0
    if has_file:
        try:
            filename, mimetype = save_upload("file", ATTACHMENT_TYPES, ATTACHMENT_SIGNATURES)
        except ValueError as exc:
            flash(str(exc), "error")
            return back
        size = (UPLOAD_DIR / filename).stat().st_size
    shared = bool(share and item["catalogue_id"])
    try:
        conn.execute("INSERT INTO attachments(item_id, catalogue_id, label, file_path, url, mimetype, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (None if shared else item_id, item["catalogue_id"] if shared else None, label, filename, url, mimetype, size, datetime.now(timezone.utc).isoformat()))
        touch_attachments(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        if filename: (UPLOAD_DIR / filename).unlink(missing_ok=True)
        raise
    flash("Attachment added.", "success")
    return back

@app.route("/items/<int:item_id>/attachments/<int:attachment_id>/delete", methods=["POST"])
def delete_attachment(item_id, attachment_id):
    conn = db()
    leftover = None
    with conn:
        # Take the write lock first and make the delete the guard, so a second tap waits and then finds nothing to remove.
        conn.execute("BEGIN IMMEDIATE")
        item = conn.execute("SELECT catalogue_id FROM items WHERE id=?", (item_id,)).fetchone()
        if not item: abort(404)
        row = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment_id,)).fetchone()
        if row and row["item_id"] != item_id and (row["catalogue_id"] is None or row["catalogue_id"] != item["catalogue_id"]):
            abort(404)
        removed = conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,)).rowcount
        if removed:
            touch_attachments(conn)
            if row["file_path"] and not conn.execute("SELECT 1 FROM attachments WHERE file_path=?", (row["file_path"],)).fetchone():
                leftover = row["file_path"]
    if leftover:
        (UPLOAD_DIR / leftover).unlink(missing_ok=True)
    flash("Attachment removed." if removed else "That attachment was already removed.", "success" if removed else "error")
    return redirect(url_for("item_detail", item_id=item_id))

@app.route("/projects", methods=["GET", "POST"])
def projects():
    conn = db()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        if not title:
            flash("A project needs a title.", "error")
        else:
            try:
                image_path = image_upload("image") if enabled("images") else ""
                project_id = conn.execute("INSERT INTO projects(title, description, image_path, created_at) VALUES (?, ?, ?, ?)", (title, request.form.get("description", "").strip(), image_path, datetime.now(timezone.utc).isoformat())).lastrowid
                dispatch_webhooks("project.created", {"project": dict(conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())})
                conn.commit()
                flash("Project created.", "success")
                return redirect(url_for("project_detail", project_id=project_id))
            except ValueError as exc:
                flash(str(exc), "error")
    rows = conn.execute("SELECT p.*, count(pi.item_id) AS component_count FROM projects p LEFT JOIN project_items pi ON pi.project_id=p.id GROUP BY p.id ORDER BY p.created_at DESC").fetchall()
    return render_template("projects.html", projects=rows, buildable=project_build_counts(conn))

# Quantities are floats, so a line that is exactly covered can come out a hair short; ignore differences this small.
TOLERANCE = 1e-9
COUNT_DUE_DAYS = 180
MAX_BUILDS = 1_000_000

def builds_wanted(values):
    """The number of builds being planned, from the query string or a form, or None when it is not a usable count."""
    wanted = values.get("quantity", type=int)
    return wanted if wanted and 1 <= wanted <= MAX_BUILDS else None

def back_to_project(project_id):
    """Return to the project page without losing the number of builds being planned."""
    wanted = builds_wanted(request.form)
    return redirect(url_for("project_detail", project_id=project_id, quantity=wanted if wanted and wanted > 1 else None))

def project_plan(conn, project_id, wanted=1):
    """Each bill-of-materials line with what `wanted` builds need, what is on hand, and the shortfall."""
    rows = conn.execute("""SELECT i.id, i.name, i.part_number, i.unit, i.quantity AS on_hand, l.code, pi.per_build, pi.quantity AS consumed
        FROM project_items pi JOIN items i ON i.id=pi.item_id JOIN locations l ON l.id=i.location_id
        WHERE pi.project_id=? ORDER BY i.name COLLATE NOCASE""", (project_id,)).fetchall()
    plan = []
    for row in rows:
        need = row["per_build"] * wanted
        short = need - row["on_hand"]
        plan.append({**dict(row), "need": need, "short": short if short > TOLERANCE else 0})
    return plan

def buildable(lines):
    """How many complete builds the stock on hand covers: the tightest line decides, and no lines means none."""
    counts = []
    for line in lines:
        ratio = line["on_hand"] / line["per_build"] if line["per_build"] > 0 else 0
        counts.append(math.floor(ratio + TOLERANCE) if math.isfinite(ratio) else 0)
    return max(0, min(counts, default=0))

def project_build_counts(conn):
    """Builds possible for every project that has a bill of materials, keyed by project id."""
    lines = {}
    for row in conn.execute("SELECT pi.project_id, pi.per_build, i.quantity AS on_hand FROM project_items pi JOIN items i ON i.id=pi.item_id"):
        lines.setdefault(row["project_id"], []).append(row)
    return {project_id: buildable(rows) for project_id, rows in lines.items()}

@app.route("/projects/<int:project_id>", methods=["GET", "POST"])
def project_detail(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        abort(404)
    if request.method == "POST":
        item_id = request.form.get("item_id", type=int)
        try:
            per_build = float(request.form.get("per_build", "1"))
            if not item_id or not math.isfinite(per_build) or per_build <= 0 or not conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                raise ValueError()
        except (ValueError, TypeError):
            flash("Choose a component and enter how many one build needs.", "error")
        else:
            existed = conn.execute("SELECT 1 FROM project_items WHERE project_id=? AND item_id=?", (project_id, item_id)).fetchone()
            conn.execute("INSERT INTO project_items(project_id, item_id, quantity, per_build) VALUES (?, ?, 0, ?) ON CONFLICT(project_id, item_id) DO UPDATE SET per_build=excluded.per_build", (project_id, item_id, per_build))
            dispatch_webhooks("project.line_updated" if existed else "project.line_added", {"project": dict(project), **item_payload(item_id), "per_build": per_build})
            conn.commit()
            flash("Line added. Stock is only taken when you build.", "success")
        return back_to_project(project_id)
    wanted = builds_wanted(request.args) or 1
    plan = project_plan(conn, project_id, wanted)
    builds = conn.execute("SELECT * FROM builds WHERE project_id=? ORDER BY built_at DESC, id DESC", (project_id,)).fetchall()
    query = request.args.get("q", "").strip()
    component_sql = "SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id"
    args = []
    if query:
        component_sql += " WHERE i.name LIKE ? OR i.part_number LIKE ?"
        args = [f"%{query}%"] * 2
    component_sql += " ORDER BY i.name COLLATE NOCASE LIMIT 30"
    return render_template("project_detail.html", project=project, plan=plan, wanted=wanted, max_builds=MAX_BUILDS, buildable=buildable(plan), builds=builds, components=conn.execute(component_sql, args).fetchall(), query=query)

def read_bom_lines(raw):
    """The `lines` field of a review form, checked, since it can be edited on the way back."""
    if not raw:
        raise ValueError("Choose a BOM file to import.")
    bad = ValueError("The review could not be read. Upload the file again.")
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise bad
    if not isinstance(data, list) or len(data) > MAX_BOM_LINES or not all(isinstance(entry, dict) for entry in data):
        raise bad
    lines = []
    for entry in data:
        line = {key: entry.get(key, "") for key in TEXT_FIELDS}
        quantity, suggested = entry.get("quantity"), entry.get("suggested")
        if (not all(isinstance(value, str) and len(value) <= 200 for value in line.values())
                or isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or not math.isfinite(quantity)
                or isinstance(suggested, bool) or not (suggested is None or isinstance(suggested, int))):
            raise bad
        lines.append({**line, "quantity": quantity, "suggested": suggested, "warn": bool(entry.get("warn"))})
    return lines

def bom_review(conn, project, lines, form=None, skipped=0, errors=None):
    """Render the review: each line with its candidate items. A line set by hand, or searched, keeps its choice."""
    index = prepare_items(conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id ORDER BY i.name COLLATE NOCASE"))
    by_id = {item["id"]: item for item in index["items"]}
    existing = {row["item_id"]: row["per_build"] for row in conn.execute("SELECT item_id, per_build FROM project_items WHERE project_id=?", (project["id"],))}
    rows = []
    for n, line in enumerate(lines):
        search = form.get(f"q_{n}", "").strip()[:200] if form else ""
        status, candidates = match_line(line, index, search)
        chosen = form.get(f"item_{n}", "skip") if form else None
        chosen = int(chosen) if str(chosen).isdigit() else None
        if form and (chosen != line["suggested"] or search):
            selected = chosen
        else:
            selected = line["suggested"] = candidates[0]["id"] if candidates else None
        if selected in by_id and all(item["id"] != selected for item in candidates):
            candidates.append(by_id[selected])
        rows.append({"n": n, "line": line, "status": status, "candidates": candidates, "selected": selected, "search": search,
                     "per_build": form.get(f"per_build_{n}", "") if form else format_quantity(line["quantity"]), "existing": existing.get(selected),
                     "error": (errors or {}).get(n), "name": " ".join(part for part in (line["value"], package_of(line["footprint"])) if part)})
    return render_template("bom_review.html", project=project, rows=rows, lines=[row["line"] for row in rows], skipped=skipped,
                           accepted=sum(row["selected"] is not None for row in rows), can_add_items=enabled("add_components"))

@app.route("/projects/<int:project_id>/import", methods=["POST"])
def import_bom(project_id):
    """Read an uploaded BOM and show the review, or, with no file, match the review's lines again."""
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        abort(404)
    upload = request.files.get("file")
    form, skipped = None, 0
    try:
        if upload and upload.filename:
            raw = upload.read(MAX_BOM_BYTES + 1)
            if len(raw) > MAX_BOM_BYTES:
                raise ValueError("The BOM is larger than 5 MB.")
            lines, skipped = parse_bom(raw)
            if not lines:
                raise ValueError("The file has no lines to import.")
            if len(lines) > MAX_BOM_LINES:
                raise ValueError(f"The BOM has more than {MAX_BOM_LINES} lines.")
            lines = [{**line, "suggested": None} for line in lines]
        else:
            lines, form, skipped = read_bom_lines(request.form.get("lines")), request.form, max(0, request.form.get("dnp", 0, type=int))
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("project_detail", project_id=project_id))
    return bom_review(conn, project, lines, form, skipped)

@app.route("/projects/<int:project_id>/import/commit", methods=["POST"])
def commit_bom_import(project_id):
    """Add every accepted line of the review to the bill of materials, or none if any line is invalid."""
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        abort(404)
    try:
        lines = read_bom_lines(request.form.get("lines"))
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("project_detail", project_id=project_id))
    ids = {row["id"] for row in conn.execute("SELECT id FROM items")}
    totals, errors, skipped = {}, {}, 0
    for n in range(len(lines)):
        choice = request.form.get(f"item_{n}", "skip")
        if choice == "skip":
            skipped += 1
            continue
        try:
            item_id, per_build = int(choice), float(request.form.get(f"per_build_{n}", ""))
            if item_id not in ids or not math.isfinite(per_build) or per_build <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            errors[n] = "Choose an item and enter how many one build needs."
        else:
            totals[item_id] = totals.get(item_id, 0) + per_build
    if errors:
        return bom_review(conn, project, lines, request.form, max(0, request.form.get("dnp", 0, type=int)), errors)
    accepted = len(lines) - skipped
    if totals:
        with conn:
            conn.executemany("INSERT INTO project_items(project_id, item_id, quantity, per_build) VALUES (?, ?, 0, ?) ON CONFLICT(project_id, item_id) DO UPDATE SET per_build=excluded.per_build",
                             [(project_id, item_id, per_build) for item_id, per_build in totals.items()])
        merged = accepted - len(totals)
        flash(f"Added {accepted} line{'' if accepted == 1 else 's'}, skipped {skipped}."
              + (f" {merged} shared an item and were added together." if merged else "") + " Stock is only taken when you build.", "success")
    else:
        flash("Nothing added: every line was skipped.", "success")
    return back_to_project(project_id)

@app.route("/projects/<int:project_id>/lines/<int:item_id>", methods=["POST"])
def update_project_line(project_id, item_id):
    conn = db()
    if not conn.execute("SELECT 1 FROM project_items WHERE project_id=? AND item_id=?", (project_id, item_id)).fetchone():
        abort(404)
    try:
        per_build = float(request.form.get("per_build", ""))
        if not math.isfinite(per_build) or per_build <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Enter how many one build needs, greater than zero.", "error")
    else:
        conn.execute("UPDATE project_items SET per_build=? WHERE project_id=? AND item_id=?", (per_build, project_id, item_id))
        dispatch_webhooks("project.line_updated", {"project": {"id": project_id}, **item_payload(item_id), "per_build": per_build})
        conn.commit()
        flash("Line updated.", "success")
    return back_to_project(project_id)

@app.route("/projects/<int:project_id>/lines/<int:item_id>/delete", methods=["POST"])
def delete_project_line(project_id, item_id):
    conn = db()
    if not conn.execute("DELETE FROM project_items WHERE project_id=? AND item_id=?", (project_id, item_id)).rowcount:
        abort(404)
    dispatch_webhooks("project.line_removed", {"project": {"id": project_id}, **item_payload(item_id)})
    conn.commit()
    flash("Line removed. Stock it has already used stays in the history.", "success")
    return back_to_project(project_id)

COUNT_ITEMS = "SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.location_id=? ORDER BY i.name COLLATE NOCASE"

def next_to_count(conn, location):
    """The next storage location in the same group, in drawer order, that holds items."""
    rows = select_locations(location["code"].split()[0])
    return next((row for row in rows if row["item_count"] and drawer_order(row) > drawer_order(location)), None)

def parse_count(raw):
    """A counted quantity from a form field, or None when it is not a finite number of zero or more."""
    try:
        value = float(raw.replace(",", ".", 1))
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None

@app.route("/locations/<int:location_id>/count", methods=["GET", "POST"])
def count_location(location_id):
    """Count what is in a storage location. Each difference becomes one stock movement, or nothing is written."""
    conn = db()
    location = conn.execute("SELECT * FROM locations WHERE id=?", (location_id,)).fetchone()
    if not location:
        abort(404)
    walk = request.args.get("walk") == "1"
    group = location["code"].split()[0]
    following = next_to_count(conn, location) if walk else None
    def show(items, errors=None, form=None):
        skip = url_for("count_location", location_id=following["id"], walk=1) if following else url_for("locations", group=group)
        return render_template("stocktake.html", location=location, items=items, walk=walk, skip=skip, has_next=bool(following), errors=errors or {}, form=form)
    def finish(message, category="success"):
        flash(message, category)
        if not walk:
            return redirect(url_for("locations", group=group))
        if following:
            return redirect(url_for("count_location", location_id=following["id"], walk=1))
        flash(f"Finished counting {group}.", "success")
        return redirect(url_for("locations", group=group))
    items = conn.execute(COUNT_ITEMS, (location_id,)).fetchall()
    if request.method == "GET":
        return show(items)
    now = datetime.now(timezone.utc).isoformat()
    if request.form.get("confirm_empty"):
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            occupied = conn.execute("SELECT 1 FROM items WHERE location_id=?", (location_id,)).fetchone()
            if not occupied:
                conn.execute("UPDATE locations SET last_counted_at=? WHERE id=?", (now, location_id))
        if occupied:
            return show(conn.execute(COUNT_ITEMS, (location_id,)).fetchall())
        return finish(f"Confirmed {location['code']} is empty.")
    counts, errors = {}, {}
    for item in items:
        raw, ticked = request.form.get(f"count-{item['id']}", "").strip(), bool(request.form.get(f"match-{item['id']}"))
        try:
            expected = float(request.form.get(f"expected-{item['id']}", ""))
            if not math.isfinite(expected):
                raise ValueError()
        except ValueError:
            expected = item["quantity"]
        value = parse_count(raw) if raw else None
        if raw and value is None:
            errors[item["id"]] = "Enter a number, zero or more."
        elif ticked and raw and abs(value - expected) > TOLERANCE:
            errors[item["id"]] = "Matches is ticked, but the count is different."
        elif ticked or raw:
            counts[item["id"]] = (expected if ticked else value, expected)
    if errors:
        flash("Check the marked rows. Nothing was saved.", "error")
        return show(items, errors, request.form)
    differences, skipped, blank = 0, [], 0
    with conn:
        # Hold the write lock while the quantities are read again, so a change made since the page opened is never overwritten.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(COUNT_ITEMS, (location_id,)).fetchall()
        for item in rows:
            if item["id"] not in counts:
                blank += 1
                continue
            counted, expected = counts[item["id"]]
            if abs(item["quantity"] - expected) <= TOLERANCE:
                change = counted - item["quantity"]
                if abs(change) > TOLERANCE:
                    record_movement(conn, item, change, "Stocktake", "stock.in" if change > 0 else "stock.out")
                    differences += 1
            elif abs(item["quantity"] - counted) > TOLERANCE:
                skipped.append(item["name"])
                continue
            conn.execute("UPDATE items SET last_counted_at=? WHERE id=?", (now, item["id"]))
        if rows and not blank and not skipped:
            conn.execute("UPDATE locations SET last_counted_at=? WHERE id=?", (now, location_id))
    done = len(rows) - blank - len(skipped)
    message = f"Counted {done} item{'s' if done != 1 else ''} in {location['code']}: {differences} difference{'s' if differences != 1 else ''} recorded."
    if skipped:
        message += f" {', '.join(skipped[:3])}{f' and {len(skipped) - 3} more' if len(skipped) > 3 else ''} changed since you opened this page, count it again."
    return finish(message, "error" if skipped else "success")

@app.route("/projects/<int:project_id>/build", methods=["POST"])
def build_project(project_id):
    """Take every line of the bill of materials from stock, `wanted` times over, or nothing at all."""
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        abort(404)
    wanted = builds_wanted(request.form)
    if not wanted:
        flash(f"Enter how many to build: a whole number from 1 to {MAX_BUILDS:,}.", "error")
        return back_to_project(project_id)
    reference = {"id": project_id, "title": project["title"]}
    with conn:
        # Hold the write lock from the stock check to the deductions, so two overlapping builds cannot both pass it.
        conn.execute("BEGIN IMMEDIATE")
        plan = project_plan(conn, project_id, wanted)
        short = [line for line in plan if line["short"] > 0]
        if plan and not short:
            for line in plan:
                record_movement(conn, line, -line["need"], f"Built {project['title']} ×{wanted}", "stock.out", reference)
                conn.execute("UPDATE project_items SET quantity=quantity+? WHERE project_id=? AND item_id=?", (line["need"], project_id, line["id"]))
            build_id = conn.execute("INSERT INTO builds(project_id, quantity, lines, built_at) VALUES (?, ?, ?, ?)", (project_id, wanted, json.dumps([[line["id"], line["need"]] for line in plan]), datetime.now(timezone.utc).isoformat())).lastrowid
            dispatch_webhooks("project.built", {"project": reference, "build_id": build_id, "quantity": wanted, "lines": [{"item": {"id": line["id"], "name": line["name"], "part_number": line["part_number"], "location": line["code"]}, "quantity_change": -line["need"], "unit": line["unit"]} for line in plan]})
    if not plan:
        flash("Add at least one line to the bill of materials first.", "error")
    elif short:
        detail = "; ".join(f"{line['name']} needs {format_quantity(line['need'])} {line['unit']}, {format_quantity(line['on_hand'])} on hand" for line in short[:3])
        more = f" and {len(short) - 3} more" if len(short) > 3 else ""
        flash(f"Not enough stock to build {wanted}: {detail}{more}.", "error")
    else:
        flash(f"Built {project['title']} ×{wanted}: {len(plan)} line{'s' if len(plan) != 1 else ''} taken from stock.", "success")
    return back_to_project(project_id)

@app.route("/projects/<int:project_id>/builds/<int:build_id>/undo", methods=["POST"])
def undo_build(project_id, build_id):
    """Return exactly what a build took, even if the bill of materials has changed since."""
    conn = db()
    with conn:
        # Take the write lock first and make marking the build undone the guard, so a second tap waits and then finds nothing to return.
        conn.execute("BEGIN IMMEDIATE")
        build = conn.execute("SELECT b.*, p.title FROM builds b JOIN projects p ON p.id=b.project_id WHERE b.id=? AND b.project_id=?", (build_id, project_id)).fetchone()
        if not build:
            abort(404)
        lines = json.loads(build["lines"])
        undone = conn.execute("UPDATE builds SET undone_at=? WHERE id=? AND undone_at IS NULL", (datetime.now(timezone.utc).isoformat(), build_id)).rowcount
        if undone:
            reason = f"Unbuilt {build['title']} ×{build['quantity']}"
            for item_id, quantity in lines:
                item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone()
                if item is None:
                    continue
                record_movement(conn, item, quantity, reason, "stock.returned", {"id": project_id, "title": build["title"]})
                conn.execute("UPDATE project_items SET quantity=max(quantity-?, 0) WHERE project_id=? AND item_id=?", (quantity, project_id, item_id))
        if undone:
            dispatch_webhooks("project.build_undone", {"project": {"id": project_id, "title": build["title"]}, "build_id": build_id, "quantity": build["quantity"], "lines": [{"item_id": item_id, "quantity_change": quantity} for item_id, quantity in lines]})
    if undone:
        flash(f"Build undone: {len(lines)} line{'s' if len(lines) != 1 else ''} returned to stock.", "success")
    else:
        flash("That build was already undone.", "error")
    return back_to_project(project_id)

@app.route("/projects/<int:project_id>/bom.<format>")
def export_bom(project_id, format):
    project = db().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or format not in {"csv", "pdf"}:
        abort(404)
    rows = db().execute("SELECT i.name, i.part_number, pi.per_build, pi.quantity, i.quantity, i.unit, l.code FROM project_items pi JOIN items i ON i.id=pi.item_id JOIN locations l ON l.id=i.location_id WHERE pi.project_id=? ORDER BY i.name COLLATE NOCASE", (project_id,)).fetchall()
    return export_rows(rows, ["Component", "Part number", "Per build", "Consumed", "On hand", "Unit", "Location"], f"{project['title']}-bom", format, f"Bill of materials · {project['title']}")

def suggested_order_quantity(item):
    """Enough to bring stock up to twice the minimum, rounded up and never less than 1."""
    return max(1, math.ceil(2 * (item["minimum_quantity"] or 0) - item["quantity"] - TOLERANCE))

def mouser_text(entries):
    """Mouser part-list text, one PARTNUMBER|QTY line each, and the names of the items it had to skip."""
    lines = [f"{entry['part_number']}|{format_quantity(entry['quantity'])}" for entry in entries if entry["part_number"]]
    return "\n".join(lines), [entry["name"] for entry in entries if not entry["part_number"]]

def add_order_entries_to(conn, wanted, replace=False):
    """Add (item, quantity, source) rows as To order entries; an item already To order takes the new quantity when replace is set, and the larger one otherwise."""
    now = datetime.now(timezone.utc).isoformat()
    for item, quantity, source in wanted:
        conn.execute("INSERT INTO order_entries(item_id, quantity, source, created_at) VALUES (?, ?, ?, ?) ON CONFLICT(item_id) WHERE status='wanted' DO UPDATE SET quantity=" + ("excluded.quantity" if replace else "max(quantity, excluded.quantity)") + ", source=excluded.source", (item["id"], quantity, source, now))
    dispatch_webhooks("order.added", {"entries": [{"item_id": item["id"], "name": item["name"], "part_number": item["part_number"], "location": item["code"], "quantity": quantity, "source": source} for item, quantity, source in wanted]})

def positive_quantity(value):
    """A finite quantity above 0 from a form value, or None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None

@app.route("/orders")
def order_list():
    conn = db()
    query = request.args.get("q", "").strip()
    entries = conn.execute("SELECT e.*, i.name, i.part_number, i.manufacturer, i.unit, i.quantity AS on_hand, i.minimum_quantity, l.code FROM order_entries e JOIN items i ON i.id=e.item_id JOIN locations l ON l.id=i.location_id WHERE e.status IN ('wanted', 'ordered') ORDER BY i.name COLLATE NOCASE").fetchall()
    open_ids = {entry["item_id"] for entry in entries}
    wanted = [entry for entry in entries if entry["status"] == "wanted"]
    ordered = sorted((entry for entry in entries if entry["status"] == "ordered"), key=lambda entry: (entry["supplier"].lower(), entry["expected_on"] or "9999"))
    suppliers = {}
    for entry in ordered:
        suppliers.setdefault(entry["supplier"], []).append(entry)
    mouser, skipped = mouser_text(wanted) if enabled("exports") else ("", [])
    return render_template("orders.html", suggested=[(item, suggested_order_quantity(item)) for item in low_stock(conn) if item["id"] not in open_ids], wanted=wanted, suppliers=suppliers, results=find_stock(query)[:10] if query else [], query=query, today=datetime.now(timezone.utc).date().isoformat(), mouser=mouser, skipped=skipped)

@app.route("/orders", methods=["POST"])
def add_order_entries():
    conn = db()
    wanted = []
    for item_id in request.form.getlist("item_id"):
        item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone() if item_id.isdigit() else None
        quantity = positive_quantity(request.form.get(f"quantity_{item_id}"))
        if item is None or quantity is None:
            flash("Choose existing items and enter a quantity above 0 for each.", "error")
            return redirect(url_for("order_list"))
        wanted.append((item, quantity, "Low stock" if item["minimum_quantity"] is not None and item["quantity"] <= item["minimum_quantity"] else "Added by hand"))
    if not wanted:
        flash("Tick at least one item to add.", "error")
    else:
        add_order_entries_to(conn, wanted, replace=True)
        conn.commit()
        flash(f"{len(wanted)} item{'s' if len(wanted) != 1 else ''} added to the order list.", "success")
    return redirect(url_for("order_list"))

@app.route("/orders/<int:entry_id>", methods=["POST"])
def update_order_entry(entry_id):
    quantity = positive_quantity(request.form.get("quantity"))
    if quantity is None:
        flash("Enter a quantity above 0.", "error")
    elif not db().execute("UPDATE order_entries SET quantity=? WHERE id=? AND status='wanted'", (quantity, entry_id)).rowcount:
        flash("That entry is no longer waiting to be ordered.", "error")
    return redirect(url_for("order_list"))

@app.route("/orders/<int:entry_id>/delete", methods=["POST"])
def delete_order_entry(entry_id):
    if not db().execute("DELETE FROM order_entries WHERE id=? AND status='wanted'", (entry_id,)).rowcount:
        flash("That entry is no longer waiting to be ordered.", "error")
    return redirect(url_for("order_list"))

@app.route("/orders/ordered", methods=["POST"])
def mark_ordered():
    ids = [int(value) for value in request.form.getlist("entry_id") if value.isdigit()]
    if not ids:
        flash("Tick the entries you ordered.", "error")
        return redirect(url_for("order_list"))
    try:
        expected = date.fromisoformat(request.form.get("expected_on", "").strip()).isoformat()
    except ValueError:
        expected = None
    changed = db().execute(f"UPDATE order_entries SET status='ordered', supplier=?, order_ref=?, expected_on=?, ordered_at=? WHERE id IN ({','.join('?' * len(ids))}) AND status='wanted'", (request.form.get("supplier", "").strip()[:200], request.form.get("order_ref", "").strip()[:200], expected, datetime.now(timezone.utc).isoformat(), *ids)).rowcount
    flash(f"{changed} entr{'y' if changed == 1 else 'ies'} marked ordered." if changed else "None of those entries were waiting to be ordered.", "success" if changed else "error")
    return redirect(url_for("order_list"))

@app.route("/orders/<int:entry_id>/receive", methods=["POST"])
def receive_order_entry(entry_id):
    """Close an On order entry and record the stock coming in, once."""
    conn = db()
    message, category = "That entry was already received.", "error"
    with conn:
        # Take the write lock first and make marking the entry received the guard, as undo_build() does.
        conn.execute("BEGIN IMMEDIATE")
        entry = conn.execute("SELECT * FROM order_entries WHERE id=?", (entry_id,)).fetchone()
        quantity = positive_quantity(request.form.get("quantity"))
        if entry is None:
            message = "That entry does not exist."
        elif quantity is None:
            message = "Enter the quantity that arrived, above 0."
        elif entry["status"] == "wanted":
            message = "That entry has not been ordered yet."
        elif conn.execute("UPDATE order_entries SET status='received', received_at=?, received_quantity=? WHERE id=? AND status='ordered'", (datetime.now(timezone.utc).isoformat(), quantity, entry_id)).rowcount:
            item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (entry["item_id"],)).fetchone()
            record_movement(conn, item, quantity, "Order received", "stock.in")
            message, category = f"Received {format_quantity(quantity)} {item['unit']} of {item['name']}. Put it in {item['code']}.", "success"
    flash(message, category)
    return redirect(url_for("order_list"))

@app.route("/projects/<int:project_id>/order-shortages", methods=["POST"])
def order_shortages(project_id):
    """Add what `wanted` builds are short, less what is already on order, to the order list."""
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        abort(404)
    wanted = builds_wanted(request.form)
    if not wanted:
        flash(f"Enter how many to build: a whole number from 1 to {MAX_BUILDS:,}.", "error")
        return back_to_project(project_id)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = []
        for line in project_plan(conn, project_id, wanted):
            remainder = line["short"] - conn.execute("SELECT coalesce(sum(quantity), 0) FROM order_entries WHERE item_id=? AND status='ordered'", (line["id"],)).fetchone()[0]
            if line["short"] > 0 and remainder > TOLERANCE:
                rows.append((line, remainder, f"{project['title']} ×{wanted}"))
        if rows:
            add_order_entries_to(conn, rows)
    flash(f"{len(rows)} item{'s' if len(rows) != 1 else ''} added to the order list." if rows else f"Nothing to add for ×{wanted}.", "success" if rows else "error")
    return back_to_project(project_id)

@app.route("/orders/export.<format>")
def export_orders(format):
    if format not in {"csv", "txt"}:
        abort(404)
    rows = db().execute("SELECT i.part_number, e.quantity, l.code, i.manufacturer, i.name FROM order_entries e JOIN items i ON i.id=e.item_id JOIN locations l ON l.id=i.location_id WHERE e.status='wanted' ORDER BY i.name COLLATE NOCASE").fetchall()
    if format == "csv":
        return export_rows(rows, ["Part number", "Quantity", "Customer reference", "Manufacturer", "Component"], "tally-order-list", format, "Order list")
    dispatch_webhooks("export.created", {"filename": "tally-order-list.txt", "format": format})
    return Response(mouser_text(rows)[0] + "\n", mimetype="text/plain", headers={"Content-Disposition": "attachment; filename=tally-order-list.txt"})

@app.route("/reports")
def reports():
    return render_template("reports.html")

@app.route("/reports/inventory.<format>")
def export_inventory(format):
    if format not in {"csv", "pdf"}:
        abort(404)
    rows = db().execute("SELECT i.name, i.manufacturer, i.part_number, i.quantity, i.unit, i.minimum_quantity, l.code FROM items i JOIN locations l ON l.id=i.location_id ORDER BY i.name COLLATE NOCASE").fetchall()
    return export_rows(rows, ["Component", "Manufacturer", "Part number", "On hand", "Unit", "Minimum", "Location"], "tally-inventory", format, "Inventory report")

@app.route("/reports/locations.<format>")
def export_locations(format):
    if format not in {"csv", "pdf"}:
        abort(404)
    rows = db().execute("SELECT l.code, l.label, l.kind, l.keywords, l.notes, count(i.id) AS items FROM locations l LEFT JOIN items i ON i.location_id=l.id GROUP BY l.id ORDER BY l.code").fetchall()
    return export_rows(rows, ["Code", "Label", "Kind", "Keywords", "Notes", "Components"], "tally-locations", format, "Storage locations")

LABEL_PRESETS = {
    # Sizes are millimetres. Sheets give columns, rows, label size, the top-left margin and the gap between labels.
    "a4-3x7": {"name": "A4 sheet, 3 × 7 labels of 63.5 × 38.1 mm", "page": (210, 297), "cols": 3, "rows": 7, "size": (63.5, 38.1), "margin": (9.75, 15.15), "gap": (0, 0)},
    "a4-5x13": {"name": "A4 sheet, 5 × 13 labels of 38.1 × 21.2 mm", "page": (210, 297), "cols": 5, "rows": 13, "size": (38.1, 21.2), "margin": (9.75, 10.7), "gap": (0, 0)},
    "letter-3x10": {"name": "Letter sheet, 3 × 10 labels of 66.7 × 25.4 mm", "page": (215.9, 279.4), "cols": 3, "rows": 10, "size": (66.7, 25.4), "margin": (7.9, 12.7), "gap": (0, 0)},
    "roll-62x29": {"name": "Roll, 62 × 29 mm", "page": (62, 29), "cols": 1, "rows": 1, "size": (62, 29), "margin": (0, 0), "gap": (0, 0)},
    "roll-29x90": {"name": "Roll, 29 × 90 mm", "page": (29, 90), "cols": 1, "rows": 1, "size": (29, 90), "margin": (0, 0), "gap": (0, 0)},
}

def setting_value(key):
    row = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else ""

def label_base_url():
    return setting_value("labels.base_url")

def default_preset():
    stored = setting_value("labels.preset")
    return stored if stored in LABEL_PRESETS else next(iter(LABEL_PRESETS))

def label_url(endpoint, **values):
    base = label_base_url()
    if not base:
        return url_for(endpoint, _external=True, **values)
    return base.rstrip("/") + url_for(endpoint, **values)

def label_address_warning(address):
    """Why phones may not reach the address the QR codes use, or an empty string."""
    parsed = urlparse(address)
    host = (parsed.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return "Phones cannot reach this address. Set the label address in Settings."
    if "." not in host and ":" not in host:
        return "This address has no domain, so some phones will not find it. Set the label address in Settings."
    if parsed.scheme == "http" and request.headers.get("X-Forwarded-Proto") == "https":
        return "Tally is served over https but this address is http. Set the label address in Settings."
    return ""

def fitted(text, font, size, width, minimum):
    """Shrink text to fit the width, then cut it short with an ellipsis."""
    while size > minimum and stringWidth(text, font, size) > width:
        size -= 0.5
    while len(text) > 1 and stringWidth(text, font, size) > width:
        text = text.rstrip("…")[:-1].rstrip() + "…"
    return text, size

def draw_label(pdf, x, y, width, height, title, lines, url, outline):
    """Draw one label with its box's bottom-left corner at x, y (all in points)."""
    pad = 2.5 * mm
    if outline:
        pdf.setLineWidth(0.3)
        pdf.rect(x, y, width, height)
    qr_size = min(height - pad, width * 0.45)
    widget = QrCodeWidget(url, barLevel="M")
    x0, y0, x1, y1 = widget.getBounds()
    drawing = Drawing(qr_size, qr_size, transform=[qr_size / (x1 - x0), 0, 0, qr_size / (y1 - y0), 0, 0])
    drawing.add(widget)
    renderPDF.draw(drawing, pdf, x + width - qr_size - pad / 2, y + (height - qr_size) / 2)
    room = width - qr_size - pad * 1.5 - pad / 2
    text, size = fitted(title, "Helvetica-Bold", min(height * 0.32, 24), room, 7)
    top = y + height - pad
    pdf.setFont("Helvetica-Bold", size)
    pdf.drawString(x + pad, top - size, text)
    top -= size * 1.3
    for line in lines:
        text, size = fitted(line, "Helvetica", min(height * 0.15, 9), room, 6)
        top -= size * 1.2
        if top < y:
            break
        pdf.setFont("Helvetica", size)
        pdf.drawString(x + pad, top, text)

def labels_pdf(labels, filename, skip=0):
    """One PDF of labels on the chosen preset, which prints at actual size."""
    preset = LABEL_PRESETS.get(request.args.get("preset"), LABEL_PRESETS[default_preset()])
    outline = bool(request.args.get("outlines"))
    per_sheet = preset["cols"] * preset["rows"]
    skip = max(0, min(skip, per_sheet - 1))
    page_w, page_h = (value * mm for value in preset["page"])
    width, height = (value * mm for value in preset["size"])
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=(page_w, page_h), pageCompression=1)
    pdf.setViewerPreference("PrintScaling", "None")
    for slot, (title, lines, url) in enumerate(labels, start=skip):
        if slot and slot % per_sheet == 0:
            pdf.showPage()
        column, row = slot % per_sheet % preset["cols"], slot % per_sheet // preset["cols"]
        x = (preset["margin"][0] + column * (preset["size"][0] + preset["gap"][0])) * mm
        y = page_h - (preset["margin"][1] + row * (preset["size"][1] + preset["gap"][1])) * mm - height
        draw_label(pdf, x, y, width, height, title, lines, url, outline)
    pdf.save(); output.seek(0)
    return send_file(output, mimetype="application/pdf", as_attachment=False, download_name=filename)

@app.route("/locations/labels.pdf")
def location_labels_pdf():
    rows = select_locations(request.args.get("group", ""), request.args.get("q", "").strip(), request.args.get("code", ""))
    if not rows:
        abort(404)
    labels = [(row["code"], [row["label"]], label_url("scan_location", code=row["code"])) for row in rows]
    return labels_pdf(labels, "tally-location-labels.pdf", request.args.get("skip", 0, type=int))

@app.route("/items/<int:item_id>/label.pdf")
def item_label_pdf(item_id):
    item = db().execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone()
    if not item:
        abort(404)
    lines = [value for value in (item["part_number"], item["code"]) if value]
    return labels_pdf([(item["name"], lines, label_url("item_detail", item_id=item_id))], f"tally-item-{item_id}-label.pdf")

def export_rows(rows, columns, filename, format, title):
    dispatch_webhooks("export.created", {"filename": f"{filename}.{format}", "format": format})
    if format == "csv":
        output = BytesIO()
        text = output.write
        # csv.writer needs a text stream; generate it separately and encode it for Flask.
        from io import StringIO
        csv_output = StringIO()
        writer = csv.writer(csv_output)
        writer.writerow(columns)
        writer.writerows([[row[index] if row[index] is not None else "" for index in range(len(columns))] for row in rows])
        output.write(csv_output.getvalue().encode())
        output.seek(0)
        return send_file(output, mimetype="text/csv", as_attachment=True, download_name=f"{filename}.csv")
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4, pageCompression=1)
    width, height = A4
    y = height - 48
    pdf.setFont("Helvetica-Bold", 16)
    pdf.drawString(40, y, title)
    y -= 28
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(40, y, "  |  ".join(columns))
    pdf.setFont("Helvetica", 8)
    for row in rows:
        y -= 14
        if y < 40:
            pdf.showPage(); y = height - 48; pdf.setFont("Helvetica", 8)
        values = [str(row[index] if row[index] is not None else "Not set")[:32] for index in range(len(columns))]
        pdf.drawString(40, y, "  |  ".join(values))
    pdf.save(); output.seek(0)
    return send_file(output, mimetype="application/pdf", as_attachment=True, download_name=f"{filename}.pdf")

SETTINGS_SECTIONS = {"general": "General", "labels": "Labels", "catalogue": "Catalogue", "providers": "Part search", "webhooks": "Webhooks", "security": "Access and backups"}

@app.route("/settings")
@app.route("/settings/<section>")
def settings(section="general"):
    if section not in SETTINGS_SECTIONS:
        abort(404)
    context = dict(section=section, sections=SETTINGS_SECTIONS)
    if section == "general":
        context["flags"] = {flag: enabled(flag) for flag in ("images", "add_components", "edit_components", "exports")}
    elif section == "labels":
        address = label_base_url() or request.host_url
        context.update(base_url=label_base_url(), preset=default_preset(), presets=LABEL_PRESETS, address=address, warning=label_address_warning(address))
    elif section == "webhooks":
        context["webhook_events"] = WEBHOOK_EVENTS
        context["webhooks"] = db().execute("SELECT * FROM webhooks ORDER BY event, id").fetchall()
    elif section == "catalogue":
        source = db().execute("SELECT value FROM settings WHERE key='catalogue.url'").fetchone()
        entries = catalogue_entries()
        context.update(catalogue_count=len(entries), catalogue_entries=entries, catalogue_url=source["value"] if source else "")
    elif section == "providers":
        context.update(providers=part_providers.PROVIDERS, provider_configs=provider_configs(), is_configured=part_providers.is_configured)
    elif section == "security":
        context["auth_enabled"] = auth_enabled()
    return render_template("settings.html", **context)

@app.route("/settings/catalogue", methods=["POST"])
def import_catalogue():
    """Load quick-add suggestions from an uploaded file or a URL, which is remembered for refreshing."""
    upload = request.files.get("file")
    url = request.form.get("url", "").strip()
    try:
        if upload and upload.filename:
            raw = upload.read(MAX_CATALOGUE_BYTES + 1)
            if len(raw) > MAX_CATALOGUE_BYTES:
                raise ValueError("The catalogue is larger than 5 MB.")
            entries = parse_catalogue(raw)
        elif url:
            entries = fetch_catalogue(url)
        else:
            raise ValueError("Choose a catalogue file or enter a URL.")
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("settings", section="catalogue"))
    conn = db()
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        save_catalogue(conn, entries, replace=bool(request.form.get("replace")))
        if url and not (upload and upload.filename):
            conn.execute("INSERT INTO settings(key, value) VALUES ('catalogue.url', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (url,))
        dispatch_webhooks("catalogue.imported", {"count": len(entries), "replace": bool(request.form.get("replace"))})
    flash(f"Catalogue updated with {len(entries)} item{'s' if len(entries) != 1 else ''}.", "success")
    return redirect(url_for("settings", section="catalogue"))

@app.route("/settings/catalogue.json")
def export_catalogue():
    items = [{key: entry[key] for key in ("name", "family", "manufacturer", "part_number", "attributes")} for entry in catalogue_entries()]
    dispatch_webhooks("export.created", {"filename": "tally-catalogue.json", "format": "json"})
    response = jsonify(items=items)
    response.headers["Content-Disposition"] = "attachment; filename=tally-catalogue.json"
    return response

@app.route("/settings/providers/<provider_id>", methods=["POST"])
def update_provider(provider_id):
    provider = part_providers.PROVIDERS.get(provider_id)
    if provider is None:
        abort(404)
    conn = db()
    with conn:
        set_setting(f"provider.{provider_id}.enabled", "1" if request.form.get("enabled") else "0")
        for key, _label, secret in provider["fields"]:
            value = request.form.get(key, "").strip()[:200]
            if value or not secret:
                set_setting(f"provider.{provider_id}.{key}", value)
            elif request.form.get(f"clear_{key}"):
                set_setting(f"provider.{provider_id}.{key}", "")
        dispatch_webhooks("settings.updated", {"section": "providers", "provider": provider_id})
    flash(f"{provider['name']} settings saved.", "success")
    return redirect(url_for("settings", section="providers"))

@app.route("/settings/webhooks", methods=["POST"])
def add_webhook():
    event, destination_url = request.form.get("event", ""), request.form.get("destination_url", "").strip()
    events = {*WEBHOOK_EVENTS, "*"}
    if event not in events or not valid_webhook_url(destination_url):
        flash("Choose an event and enter a valid http(s) destination URL.", "error")
    else:
        db().execute("INSERT INTO webhooks(event, destination_url, created_at) VALUES (?, ?, ?)", (event, destination_url, datetime.now(timezone.utc).isoformat()))
        dispatch_webhooks("webhook.created", {"subscription": {"event": event}})
        flash("Webhook destination added.", "success")
    return redirect(url_for("settings", section="webhooks"))

@app.route("/settings/webhooks/<int:webhook_id>/delete", methods=["POST"])
def delete_webhook(webhook_id):
    db().execute("DELETE FROM webhooks WHERE id=?", (webhook_id,))
    dispatch_webhooks("webhook.deleted", {"webhook_id": webhook_id})
    flash("Webhook destination removed.", "success")
    return redirect(url_for("settings", section="webhooks"))

@app.route("/settings/features", methods=["POST"])
def update_features():
    for flag in ("images", "add_components", "edit_components", "exports"):
        set_setting(f"feature.{flag}", "1" if request.form.get(flag) else "0")
    dispatch_webhooks("settings.updated", {"section": "features"})
    flash("Feature settings saved.", "success")
    return redirect(url_for("settings"))

@app.route("/settings/labels", methods=["POST"])
def update_labels():
    base_url, preset = request.form.get("base_url", "").strip().rstrip("/"), request.form.get("preset", "")
    if (base_url and not valid_base_url(base_url)) or preset not in LABEL_PRESETS:
        flash("Enter a label address like https://tally.example.lan, with no query or fragment, and choose a label size.", "error")
    else:
        set_setting("labels.base_url", base_url)
        set_setting("labels.preset", preset)
        dispatch_webhooks("settings.updated", {"section": "labels"})
        flash("Label settings saved.", "success")
    return redirect(url_for("settings", section="labels"))

@app.route("/settings/access", methods=["POST"])
def update_access_password():
    password = request.form.get("password", "")
    if len(password) < 10:
        flash("Use at least 10 characters for the access password.", "error")
    else:
        password_hash = generate_password_hash(password)
        set_setting("access.password_hash", password_hash)
        session["access_unlocked"] = access_token(password_hash)
        dispatch_webhooks("settings.updated", {"section": "access"})
        flash("Access password saved.", "success")
    return redirect(url_for("settings", section="security"))

@app.route("/settings/auth", methods=["POST"])
def update_auth():
    should_enable = bool(request.form.get("auth_enabled"))
    password = db().execute("SELECT value FROM settings WHERE key='access.password_hash'").fetchone()
    if should_enable and not password:
        flash("Set an access password before enabling authentication.", "error")
    else:
        set_setting("auth.enabled", "1" if should_enable else "0")
        if not should_enable:
            session.pop("access_unlocked", None)
        dispatch_webhooks("settings.updated", {"section": "authentication", "enabled": should_enable})
        flash("Authentication enabled." if should_enable else "Authentication disabled.", "success")
    return redirect(url_for("settings", section="security"))

@app.route("/settings/backup")
def download_backup():
    if not DB_PATH.exists():
        abort(404)
    dispatch_webhooks("export.created", {"filename": "tally-backup.sqlite3", "format": "sqlite3"})
    return send_file(DB_PATH, mimetype="application/x-sqlite3", as_attachment=True, download_name="tally-backup.sqlite3")

@app.route("/settings/backup.zip")
def download_full_backup():
    """The database and every uploaded file in one zip. Temporary files live beside the database, which has the room."""
    if not DB_PATH.exists():
        abort(404)
    handle, copy = tempfile.mkstemp(dir=DB_PATH.parent, suffix=".sqlite3")
    os.close(handle)
    handle, archive = tempfile.mkstemp(dir=DB_PATH.parent, suffix=".zip")
    os.close(handle)
    snapshot = sqlite3.connect(copy)
    db().backup(snapshot)
    snapshot.close()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.write(copy, "tally-backup.sqlite3")
        for path in sorted(UPLOAD_DIR.iterdir()) if UPLOAD_DIR.is_dir() else []:
            bundle.write(path, f"uploads/{path.name}")
    response = send_file(archive, mimetype="application/zip", as_attachment=True, download_name="tally-full-backup.zip")
    # A passthrough file response skips close callbacks, so turn that off to have the temporary files removed.
    response.direct_passthrough = False
    response.call_on_close(lambda: [Path(path).unlink(missing_ok=True) for path in (copy, archive)])
    return response
