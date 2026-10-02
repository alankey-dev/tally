"""SQLite transactional event outbox and leased, retryable webhook delivery."""
import json
import sqlite3
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone

_workers = set()
_worker_lock = threading.Lock()


def init_outbox(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, event TEXT NOT NULL, occurred_at TEXT NOT NULL, body TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS webhook_deliveries (
            id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES events(id),
            destination_url TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            available_at REAL NOT NULL DEFAULT 0, lease_token TEXT, delivered_at TEXT,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS webhook_delivery_pending
            ON webhook_deliveries(delivered_at, available_at);
    """)


def enqueue_event(conn, event, payload):
    """Do not commit here: domain write, event and destination snapshots are atomic."""
    event_id = str(uuid.uuid4())
    occurred_at = datetime.now(timezone.utc).isoformat()
    body = json.dumps({**payload, 'id': event_id, 'event': event, 'occurred_at': occurred_at})
    conn.execute('INSERT INTO events VALUES (?,?,?,?)', (event_id, event, occurred_at, body))
    conn.execute("""INSERT INTO webhook_deliveries(event_id, destination_url)
        SELECT ?, destination_url FROM webhooks WHERE active=1 AND (event=? OR event='*')""", (event_id, event))
    return event_id


def deliver_one(path, logger, now=None):
    """Lease one delivery across workers; crashed workers' leases expire after 30s."""
    now = time.time() if now is None else now
    with sqlite3.connect(path, timeout=5) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute("""SELECT d.*, e.body FROM webhook_deliveries d JOIN events e ON e.id=d.event_id
            WHERE delivered_at IS NULL AND available_at<=? ORDER BY d.id LIMIT 1""", (now,)).fetchone()
        if row is None:
            return False
        token = uuid.uuid4().hex
        conn.execute('UPDATE webhook_deliveries SET lease_token=?, available_at=?, attempts=attempts+1 WHERE id=?', (token, now + 30, row['id']))
    error = None
    try:
        req = urllib.request.Request(row['destination_url'], data=row['body'].encode(), headers={
            'Content-Type': 'application/json', 'User-Agent': 'Tally/1.0',
            'X-Tally-Event-ID': row['event_id'],
        })
        with urllib.request.urlopen(req, timeout=4) as response:
            if not 200 <= response.status < 300:
                raise ValueError(f'HTTP {response.status}')
    except Exception as exc:
        # Do not persist URLs or exception text that might expose URL tokens.
        error = type(exc).__name__
        logger.warning('Webhook delivery %s failed (%s); will retry', row['id'], error)
    with sqlite3.connect(path, timeout=5) as conn:
        if error:
            delay = min(3600, 2 ** min(row['attempts'] + 1, 12))
            conn.execute('UPDATE webhook_deliveries SET available_at=?, lease_token=NULL, last_error=? WHERE id=? AND lease_token=?', (time.time() + delay, error, row['id'], token))
        else:
            conn.execute('UPDATE webhook_deliveries SET delivered_at=?, lease_token=NULL, last_error=NULL WHERE id=? AND lease_token=?', (datetime.now(timezone.utc).isoformat(), row['id'], token))
    return True


def start_worker(path, logger):
    path = str(path.resolve())
    with _worker_lock:
        if path in _workers:
            return
        _workers.add(path)
    def run():
        while True:
            try:
                if deliver_one(path, logger):
                    continue
            except Exception:
                logger.exception('Webhook outbox worker failed; will retry')
            time.sleep(1)
    threading.Thread(target=run, name='tally-webhook-outbox', daemon=True).start()
