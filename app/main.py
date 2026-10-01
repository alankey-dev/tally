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
from app.matching import ranked, suggested_homes
from app.catalogue import FAMILIES, catalogue_item, classify, common_suggestions

DB_PATH = Path(os.environ.get("STORAGE_DB", "data/storage.db"))
UPLOAD_DIR = Path(os.environ.get("STORAGE_UPLOADS", "data/uploads"))
# Locations to create on first run: a bundled layout name (see app/layouts) or a path to a JSON file.
LAYOUT = os.environ.get("TALLY_LAYOUT", "")
LAYOUT_DIR = Path(__file__).parent / "layouts"

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
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS locations (id INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, label TEXT NOT NULL, notes TEXT DEFAULT '', image_path TEXT DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, name TEXT NOT NULL, manufacturer TEXT DEFAULT '', part_number TEXT DEFAULT '', quantity REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'pcs', minimum_quantity REAL, location_id INTEGER NOT NULL REFERENCES locations(id), notes TEXT DEFAULT '', image_path TEXT DEFAULT '', updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS movements (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), quantity_change REAL NOT NULL, reason TEXT NOT NULL, occurred_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS stock_receipts (token TEXT PRIMARY KEY, item_id INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT DEFAULT '', image_path TEXT DEFAULT '', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS project_items (project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE, item_id INTEGER NOT NULL REFERENCES items(id), quantity REAL NOT NULL, PRIMARY KEY(project_id, item_id));
    CREATE TABLE IF NOT EXISTS webhooks (id INTEGER PRIMARY KEY, event TEXT NOT NULL, destination_url TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """)
    item_columns = {row["name"] for row in conn.execute("PRAGMA table_info(items)")}
    if "family" not in item_columns:
        conn.execute("ALTER TABLE items ADD COLUMN family TEXT NOT NULL DEFAULT 'generic'")
    if "attributes" not in item_columns:
        conn.execute("ALTER TABLE items ADD COLUMN attributes TEXT NOT NULL DEFAULT '{}'")
    if "image_path" not in item_columns:
        conn.execute("ALTER TABLE items ADD COLUMN image_path TEXT DEFAULT ''")
    location_columns = {row["name"] for row in conn.execute("PRAGMA table_info(locations)")}
    if "image_path" not in location_columns:
        conn.execute("ALTER TABLE locations ADD COLUMN image_path TEXT DEFAULT ''")
    if "keywords" not in location_columns:
        conn.execute("ALTER TABLE locations ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
    if LAYOUT and conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
        seed_layout(conn, load_layout(LAYOUT))
    elif LAYOUT and not conn.execute("SELECT 1 FROM settings WHERE key='layout.groups'").fetchone():
        # Storage created before layouts existed: keep it, but take the layout's names and hints.
        seed_layout(conn, load_layout(LAYOUT), existing=True)
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

def layout_setting(key):
    row = db().execute("SELECT value FROM settings WHERE key=?", (f"layout.{key}",)).fetchone()
    return json.loads(row["value"]) if row else {}

@app.before_request
def ensure_database():
    init_db()
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

@app.route("/access", methods=["GET", "POST"])
def access():
    password = db().execute("SELECT value FROM settings WHERE key='access.password_hash'").fetchone()
    if not auth_enabled() or not password:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        if check_password_hash(password["value"], request.form.get("password", "")):
            session["access_unlocked"] = access_token(password["value"])
            target = request.args.get("next", "")
            return redirect(target if target.startswith("/") and not target.startswith("//") else url_for("dashboard"))
        flash("That password is not correct.", "error")
    return render_template("access.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.pop("access_unlocked", None)
    flash("Signed out.", "success")
    return redirect(url_for("access"))

def dispatch_webhooks(event, payload):
    destinations = db().execute("SELECT destination_url FROM webhooks WHERE event=? AND active=1", (event,)).fetchall()
    if not destinations:
        return
    body = json.dumps({"event": event, "occurred_at": datetime.now(timezone.utc).isoformat(), **payload}).encode()
    def send(url):
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": "Parts-Ledger/1.0"}), timeout=4).close()
        except Exception:
            app.logger.warning("Webhook delivery failed for %s", url)
    for destination in destinations:
        threading.Thread(target=send, args=(destination["destination_url"],), daemon=True).start()

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
    return render_template("dashboard.html", stats=stats, recent=recent, low=low, projects=projects)

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


@app.route("/quick-add", methods=["GET", "POST"])
def quick_add():
    query = request.values.get("q", "").strip()[:200]
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
            conn = db()
            with conn:
                inserted = conn.execute("INSERT OR IGNORE INTO stock_receipts(token,item_id) VALUES (?,?)", (token, selected["id"])).rowcount
                if inserted:
                    record_movement(conn, selected, quantity, "Stock received", "stock.in")
            flash(f"Added {quantity:g} {selected['unit']} · {selected['name']} · {selected['code']}" if inserted else "This addition was already saved.", "success")
            return redirect(url_for("quick_add", q=query))
    matches = find_stock(query)[:8] if len(query) >= 2 else []
    catalogue = common_suggestions(query) if len(query) >= 2 else []
    locations = db().execute("SELECT * FROM locations ORDER BY code").fetchall()
    homes = suggested_homes(query, locations) if len(query) >= 2 else []
    context = dict(query=query, matches=matches, homes=homes, catalogue=catalogue, selected=selected, token=str(uuid.uuid4()))
    return render_template("quick_results.html" if request.args.get("fragment") else "quick_add.html", **context)

@app.route("/api/item-guide")
def item_guide():
    query = request.args.get("q", "").strip()[:200]
    family_key = classify(query)
    family = FAMILIES[family_key]
    return jsonify(family=family_key, label=family["label"], home=layout_setting("family_homes").get(family_key, ""), fields=family["fields"], suggestions=common_suggestions(query))

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
    initial = suggestion or {"name": suggested_name, "manufacturer": "", "part_number": "", "attributes": {}}
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
            manufacturer = request.form.get("manufacturer", "").strip() or attributes.pop("manufacturer", "")
            part_number = request.form.get("part_number", "").strip() or attributes.pop("part_number", "")
            try:
                image_path = image_upload("image") if enabled("images") else ""
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("item_form.html", item=None, locations=locations, selected_location=suggested_location, suggested_name=initial["name"], suggested_manufacturer=initial.get("manufacturer", ""), suggested_part_number=initial.get("part_number", ""), suggested_attributes=initial.get("attributes", {}), families=FAMILIES, family_homes=layout_setting("family_homes"), selected_family=selected_family)
            cur = conn.execute("INSERT INTO items(name, manufacturer, part_number, quantity, unit, minimum_quantity, location_id, notes, image_path, updated_at, family, attributes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (name, manufacturer, part_number, quantity, request.form.get("unit", "pcs").strip() or "pcs", minimum, location_id, request.form.get("notes", "").strip(), image_path, now, selected_family, json.dumps(attributes, ensure_ascii=False)))
            if quantity: conn.execute("INSERT INTO movements(item_id, quantity_change, reason, occurred_at) VALUES (?, ?, ?, ?)", (cur.lastrowid, quantity, "Initial stock", now))
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
    return render_template("item_form.html", item=None, locations=locations, selected_location=suggested_location, suggested_name=initial["name"], suggested_manufacturer=initial.get("manufacturer", ""), suggested_part_number=initial.get("part_number", ""), suggested_attributes=initial.get("attributes", {}), families=FAMILIES, family_homes=layout_setting("family_homes"), selected_family=selected_family)

@app.route("/items/<int:item_id>", methods=["GET", "POST"])
def item_detail(item_id):
    conn = db()
    item = conn.execute("SELECT i.*, l.code, l.label AS location_label FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone()
    if not item: abort(404)
    if request.method == "POST":
        change = float(request.form.get("quantity_change") or 0)
        if change == 0: flash("Enter a non-zero stock change.", "error")
        else:
            reason = request.form.get("reason", "Stock adjustment").strip() or "Stock adjustment"
            record_movement(conn, item, change, reason, "stock.in" if change > 0 else "stock.out")
            conn.commit(); flash("Stock updated.", "success")
            return redirect(url_for("item_detail", item_id=item_id))
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
            db().execute("INSERT INTO locations(code, kind, label, notes, image_path, created_at) VALUES (?, ?, ?, ?, ?, ?)", (code, request.form.get("kind", "custom storage"), label, request.form.get("notes", ""), image_path, datetime.now(timezone.utc).isoformat())); db().commit(); flash("Storage location added.", "success")
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
                conn.commit()
                flash("Project created.", "success")
                return redirect(url_for("project_detail", project_id=project_id))
            except ValueError as exc:
                flash(str(exc), "error")
    rows = conn.execute("SELECT p.*, count(pi.item_id) AS component_count FROM projects p LEFT JOIN project_items pi ON pi.project_id=p.id GROUP BY p.id ORDER BY p.created_at DESC").fetchall()
    return render_template("projects.html", projects=rows)

@app.route("/projects/<int:project_id>", methods=["GET", "POST"])
def project_detail(project_id):
    conn = db()
    project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project:
        abort(404)
    if request.method == "POST":
        item_id = request.form.get("item_id", type=int)
        try:
            quantity = float(request.form.get("quantity", "1"))
            if not item_id or not math.isfinite(quantity) or quantity <= 0:
                raise ValueError()
            item = conn.execute("SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id WHERE i.id=?", (item_id,)).fetchone()
            if item is None or item["quantity"] < quantity:
                raise ValueError()
            with conn:
                conn.execute("INSERT INTO project_items(project_id,item_id,quantity) VALUES (?,?,?) ON CONFLICT(project_id,item_id) DO UPDATE SET quantity=quantity+excluded.quantity", (project_id, item_id, quantity))
                record_movement(conn, item, -quantity, f"Used in project: {project['title']}", "project.component_added", {"id": project_id, "title": project["title"]})
            flash("Component added to project and removed from stock.", "success")
        except (ValueError, TypeError):
            flash("Choose a component with an available quantity.", "error")
        return redirect(url_for("project_detail", project_id=project_id))
    query = request.args.get("q", "").strip()
    component_sql = "SELECT i.*, l.code FROM items i JOIN locations l ON l.id=i.location_id"
    args = []
    if query:
        component_sql += " WHERE i.name LIKE ? OR i.part_number LIKE ?"
        args = [f"%{query}%"] * 2
    component_sql += " ORDER BY i.name COLLATE NOCASE LIMIT 30"
    project_items = conn.execute("SELECT pi.*, i.name, i.part_number, i.unit, i.image_path, l.code FROM project_items pi JOIN items i ON i.id=pi.item_id JOIN locations l ON l.id=i.location_id WHERE pi.project_id=? ORDER BY i.name", (project_id,)).fetchall()
    return render_template("project_detail.html", project=project, project_items=project_items, components=conn.execute(component_sql, args).fetchall(), query=query)

@app.route("/projects/<int:project_id>/bom.<format>")
def export_bom(project_id, format):
    project = db().execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not project or format not in {"csv", "pdf"}:
        abort(404)
    rows = db().execute("SELECT i.name, i.part_number, pi.quantity, i.unit, l.code FROM project_items pi JOIN items i ON i.id=pi.item_id JOIN locations l ON l.id=i.location_id WHERE pi.project_id=? ORDER BY i.name", (project_id,)).fetchall()
    return export_rows(rows, ["Component", "Part number", "Quantity", "Unit", "Location"], f"{project['title']}-bom", format, f"Bill of materials · {project['title']}")

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

@app.route("/settings")
def settings():
    flags = {flag: enabled(flag) for flag in ("images", "add_components", "edit_components", "exports")}
    webhooks = db().execute("SELECT * FROM webhooks ORDER BY event, id").fetchall()
    return render_template("settings.html", webhooks=webhooks, flags=flags, auth_enabled=auth_enabled())

@app.route("/settings/webhooks", methods=["POST"])
def add_webhook():
    event, destination_url = request.form.get("event", ""), request.form.get("destination_url", "").strip()
    events = {"stock.in", "stock.out", "stock.returned", "stock.lost", "project.component_added"}
    if event not in events or not valid_webhook_url(destination_url):
        flash("Choose an event and enter a valid http(s) destination URL.", "error")
    else:
        db().execute("INSERT INTO webhooks(event, destination_url, created_at) VALUES (?, ?, ?)", (event, destination_url, datetime.now(timezone.utc).isoformat())); db().commit()
        flash("Webhook destination added.", "success")
    return redirect(url_for("settings"))

@app.route("/settings/webhooks/<int:webhook_id>/delete", methods=["POST"])
def delete_webhook(webhook_id):
    db().execute("DELETE FROM webhooks WHERE id=?", (webhook_id,)); db().commit()
    flash("Webhook destination removed.", "success")
    return redirect(url_for("settings"))

@app.route("/settings/features", methods=["POST"])
def update_features():
    for flag in ("images", "add_components", "edit_components", "exports"):
        set_setting(f"feature.{flag}", "1" if request.form.get(flag) else "0")
    db().commit(); flash("Feature settings saved.", "success")
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
        db().commit(); flash("Access password saved.", "success")
    return redirect(url_for("settings"))

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
        db().commit()
        flash("Authentication enabled." if should_enable else "Authentication disabled.", "success")
    return redirect(url_for("settings"))

@app.route("/settings/backup")
def download_backup():
    if not DB_PATH.exists():
        abort(404)
    return send_file(DB_PATH, mimetype="application/x-sqlite3", as_attachment=True, download_name="tally-backup.sqlite3")
