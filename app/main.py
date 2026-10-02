import os
import math
import uuid
import hashlib
import secrets
import json
import sqlite3
import csv
import threading
import urllib.request
from io import BytesIO
from urllib.parse import urlparse
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, flash, g, jsonify, redirect, render_template, request, url_for, session, send_file, send_from_directory
from werkzeug.security import check_password_hash, generate_password_hash
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from app.webhooks import init_outbox, enqueue_event, start_worker
from app import providers as part_providers
from app.matching import ranked, suggested_homes
from app.bag_labels import parse_bag_label
from app.catalogue import FAMILIES, classify, common_suggestions, parse_catalogue

DB_PATH = Path(os.environ.get("STORAGE_DB", "data/storage.db"))
UPLOAD_DIR = Path(os.environ.get("STORAGE_UPLOADS", "data/uploads"))
# Locations to create on first run: a bundled layout name (see app/layouts) or a path to a JSON file.
LAYOUT = os.environ.get("TALLY_LAYOUT", "")
LAYOUT_DIR = Path(__file__).parent / "layouts"
DEFAULT_CATALOGUE = Path(__file__).parent / "catalogue.json"
MAX_CATALOGUE_BYTES = 5 * 1024 * 1024

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
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS catalogue (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, family TEXT NOT NULL DEFAULT 'generic', manufacturer TEXT DEFAULT '', part_number TEXT DEFAULT '', attributes TEXT NOT NULL DEFAULT '{}');
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
    if replace:
        conn.execute("DELETE FROM catalogue")
    conn.executemany(
        """INSERT INTO catalogue(name, family, manufacturer, part_number, attributes) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(name) DO UPDATE SET family=excluded.family, manufacturer=excluded.manufacturer, part_number=excluded.part_number, attributes=excluded.attributes""",
        [(entry["name"], entry["family"], entry["manufacturer"], entry["part_number"], json.dumps(entry["attributes"], ensure_ascii=False)) for entry in entries],
    )

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
    restricted = {"new_item": "add_components", "export_inventory": "exports", "export_bom": "exports"}
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

@app.context_processor
def feature_context():
    return {"images_enabled": enabled("images"), "auth_enabled": auth_enabled()}

def image_upload(field_name):
    """Save a user-supplied component/location image under an unguessable local name."""
    upload = request.files.get(field_name)
    if not upload or not upload.filename:
        return ""
    suffix = ALLOWED_IMAGE_TYPES.get(upload.mimetype)
    if not suffix:
        raise ValueError("Use an SVG, PNG, JPEG, WebP, or GIF image.")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}{suffix}"
    upload.save(UPLOAD_DIR / filename)
    return filename

def valid_webhook_url(value):
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password

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
    "export.created": "Export downloaded",
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

@app.route("/")
def dashboard():
    conn = db()
    stats = conn.execute("SELECT (SELECT count(*) FROM items) items, (SELECT count(*) FROM locations) locations, (SELECT count(*) FROM items WHERE minimum_quantity IS NOT NULL AND quantity <= minimum_quantity) low_stock").fetchone()
    recent = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id ORDER BY i.updated_at DESC LIMIT 8").fetchall()
    low = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.minimum_quantity IS NOT NULL AND i.quantity <= i.minimum_quantity ORDER BY i.quantity LIMIT 8").fetchall()
    projects = conn.execute("SELECT * FROM projects ORDER BY created_at DESC LIMIT 4").fetchall()
    return render_template("dashboard.html", stats=stats, recent=recent, low=low, projects=projects, buildable=project_build_counts(conn))

@app.route("/components")
@app.route("/items")
def items():
    query = request.args.get("q", "").strip()
    view = request.args.get("view", "tiles")
    if view not in {"tiles", "list"}:
        view = "tiles"
    sql = "SELECT i.*, l.code, l.label AS location_label FROM items i JOIN locations l ON l.id=i.location_id"
    args = []
    if query:
        sql += " WHERE i.name LIKE ? OR i.part_number LIKE ? OR l.code LIKE ?"
        args = [f"%{query}%"] * 3
    sql += " ORDER BY i.name COLLATE NOCASE"
    return render_template("items.html", items=db().execute(sql, args).fetchall(), query=query, view=view)

@app.route("/stock")
def stock():
    query = request.args.get("q", "").strip()
    sql = "SELECT i.*, l.code, l.label AS location_label FROM items i JOIN locations l ON l.id=i.location_id"
    args = []
    if query:
        sql += " WHERE i.name LIKE ? OR i.part_number LIKE ? OR l.code LIKE ?"
        args = [f"%{query}%"] * 3
    sql += " ORDER BY i.name COLLATE NOCASE"
    chosen = request.args.get("add", type=int)
    recent = db().execute("SELECT m.*, i.name, i.unit FROM movements m JOIN items i ON i.id=m.item_id ORDER BY m.occurred_at DESC LIMIT 10").fetchall()
    return render_template("stock.html", items=db().execute(sql, args).fetchall(), query=query, selected_id=chosen, recent=recent)

@app.route("/stock/checkout", methods=["POST"])
def stock_checkout():
    try:
        cart = json.loads(request.form.get("cart", "[]"))
        action = request.form.get("action", "stock-out")
        if action not in {"stock-in", "stock-out", "return", "lost"} or not isinstance(cart, list) or not cart:
            raise ValueError()
    except (ValueError, TypeError, json.JSONDecodeError):
        flash("Add at least one component and choose a stock action.", "error")
        return redirect(url_for("stock"))
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
        return redirect(url_for("stock"))
    with conn:
        for item, quantity in prepared:
            record_movement(conn, item, direction * quantity, labels[action][0], labels[action][1])
    flash(f"{labels[action][0]} for {len(prepared)} component{'s' if len(prepared) != 1 else ''}.", "success")
    return redirect(url_for("stock"))

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
    return ranked(query, rows, lambda row: " ".join(row[key] or "" for key in ("name", "manufacturer", "part_number", "code")))


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
            cur = conn.execute("INSERT INTO items(name, manufacturer, part_number, quantity, unit, minimum_quantity, location_id, notes, image_path, updated_at, family, attributes, supplier, supplier_sku) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (name, manufacturer, part_number, quantity, request.form.get("unit", "pcs").strip() or "pcs", minimum, location_id, request.form.get("notes", "").strip(), image_path, now, selected_family, json.dumps(attributes, ensure_ascii=False), scanned["supplier"], scanned["supplier_sku"]))
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
    return render_template(
        "item_detail.html",
        item=item,
        movements=movements,
        attributes=attributes,
        family=family,
        attribute_labels=attribute_labels,
    )

@app.route("/items/<int:item_id>/find", methods=["POST"])
def find_item(item_id):
    payload = item_payload(item_id)
    dispatch_webhooks("item.found", payload)
    if request.is_json:
        return jsonify(payload)
    flash(f"Locate {payload['item']['name']} at {payload['location']['code']}.", "success")
    return redirect(url_for("item_detail", item_id=item_id))


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
    selected = groups.get(active, {}).get('rows', []) if active else rows
    selected = sorted(selected, key=lambda row: ({'small drawer': 0, 'medium drawer': 1, 'large drawer': 2}.get(row['kind'], 3), row['code']))
    if query:
        selected = [row for row in rows if query.casefold() in (row['code'] + ' ' + row['label']).casefold()]
    return render_template("locations.html", locations=selected, groups=groups, active=active, query=query)

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
    return send_from_directory(UPLOAD_DIR, filename)

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

@app.route("/reports")
def reports():
    return render_template("reports.html")

@app.route("/reports/inventory.<format>")
def export_inventory(format):
    if format not in {"csv", "pdf"}:
        abort(404)
    rows = db().execute("SELECT i.name, i.manufacturer, i.part_number, i.quantity, i.unit, i.minimum_quantity, l.code FROM items i JOIN locations l ON l.id=i.location_id ORDER BY i.name COLLATE NOCASE").fetchall()
    return export_rows(rows, ["Component", "Manufacturer", "Part number", "On hand", "Unit", "Minimum", "Location"], "tally-inventory", format, "Inventory report")

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

SETTINGS_SECTIONS = {"general": "General", "catalogue": "Catalogue", "providers": "Part search", "webhooks": "Webhooks", "security": "Access and backups"}

@app.route("/settings")
@app.route("/settings/<section>")
def settings(section="general"):
    if section not in SETTINGS_SECTIONS:
        abort(404)
    context = dict(section=section, sections=SETTINGS_SECTIONS)
    if section == "general":
        context["flags"] = {flag: enabled(flag) for flag in ("images", "add_components", "edit_components", "exports")}
    elif section == "webhooks":
        context["webhook_events"] = WEBHOOK_EVENTS
        context["webhooks"] = db().execute("SELECT * FROM webhooks ORDER BY event, id").fetchall()
    elif section == "catalogue":
        source = db().execute("SELECT value FROM settings WHERE key='catalogue.url'").fetchone()
        context.update(catalogue_count=db().execute("SELECT COUNT(*) FROM catalogue").fetchone()[0], catalogue_url=source["value"] if source else "")
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
