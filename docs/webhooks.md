# Webhooks

Configure destinations under **Settings → Webhooks**. Choose a named event or
**All events** (`*`). Multiple destinations can subscribe to the same event.
Existing stock/build subscriptions continue to work.

## Finding an item with LEDs

Subscribe your controller to `item.found`, then press **Locate item** on an item's
page. This sends a JSON POST with the item and its permanent storage home. Map
`location.code` (for example `C1 S01`) or `location.id` to the LED to flash.
Opening an item page separately emits `item.viewed`; subscribe to that if you want
opening the page to flash its location automatically. Search suggestions and live
refresh polling do not flash every matching drawer.

For an external client, POST `/items/42/find` with `Content-Type: application/json`
and `{}`. It returns the item/location payload and emits `item.found`. When access
is enabled the client needs an authenticated session, just like the UI. GET does
not trigger this action. Missing items return 404 and emit no event.

Example webhook (additional item fields omitted):

```json
{
  "id": "e75c0bdd-1ad6-44e4-a9a2-5d5adcc8e34c",
  "event": "item.found",
  "occurred_at": "2026-10-01T12:00:00+00:00",
  "item": {"id": 42, "name": "100 nF capacitor", "location": "C1 S01", "location_id": 7},
  "location": {"id": 7, "code": "C1 S01", "label": "Capacitors", "kind": "small drawer"}
}
```

## Events

| Event | Trigger |
| --- | --- |
| `stock.in` | Stock in |
| `stock.out` | Stock out |
| `stock.returned` | Stock returned |
| `stock.lost` | Stock lost / damaged |
| `item.created` | Item created |
| `item.found` | Item located |
| `item.viewed` | Item opened |
| `location.created` | Storage location created |
| `project.created` | Project created |
| `project.line_added` | Project line added |
| `project.line_updated` | Project line updated |
| `project.line_removed` | Project line removed |
| `project.built` | Project built |
| `project.build_undone` | Project build undone |
| `catalogue.imported` | Catalogue imported |
| `settings.updated` | Settings updated |
| `webhook.created` | Webhook added |
| `webhook.deleted` | Webhook removed |
| `access.unlocked` | Signed in |
| `access.locked` | Signed out |
| `export.created` | Export downloaded |
| `order.added` | Order list entries added |

Item creation emits `item.created` and, when initial quantity is positive,
`stock.in`. Saving an existing project line emits `project.line_updated`.
Builds emit one stock event per line followed by the build event. Undo emits
`stock.returned` per returned item and `project.build_undone` once; repeating an
undo emits nothing. Stock events and delivery rows commit atomically with their stock changes; only committed deliveries are sent.
Validation failures and database rollbacks emit no mutation events. Initial
layout/catalogue seeding and database migrations emit no events.

Adding to the order list emits `order.added` once per request, with `entries` (item id, name, part number, location, quantity and source). Receiving an entry emits `stock.in`.

Item events contain `item` and `location`; stock events keep their existing
`item.location` code, `quantity_change`, `unit`, `reason`, and `project` fields.
Project line events contain `project`, `item`, `location`, and (except removal)
`per_build`. Build events contain `project`, `build_id`, `quantity`, and `lines`.
Location creation contains `location`; catalogue import contains `count` and
`replace`. Settings changes contain `section` (and authentication's `enabled`).
Webhook addition contains `subscription.event`; removal contains `webhook_id`.
Exports contain `filename` and `format`. Access events contain only the envelope.
Passwords and password hashes are never included.

## Delivery

Each HTTP(S) destination receives `Content-Type: application/json` and
`User-Agent: Tally/1.0`. Every event has an ID and UTC occurrence timestamp. Named
and wildcard subscriptions receive the same envelope for that event.

Events and pending deliveries are persisted in SQLite in the same transaction
as the action. Destinations are snapshotted at event creation, so later subscription
changes do not alter already queued deliveries. Events are recorded even when
there are no subscribers; adding a subscription does not replay history.

A background worker starts on the first app request in each process and polls
once a second. SQLite leases coordinate multiple Gunicorn workers. A crashed
worker's lease expires after 30 seconds. HTTP requests time out after four seconds;
only 2xx responses count as success. Failures retry with exponential backoff from
two seconds up to one hour, without a retry limit. Pending deliveries survive
app restarts and resume after the first request. No separate service is required.

Delivery is at least once: a crash after the receiver accepts a request but before
Tally records success can deliver it again. Deduplicate by the envelope's `id`,
also supplied in `X-Tally-Event-ID`. Retries preserve this ID and the original
payload. Delivery order is not guaranteed. There are no signatures. For LEDs,
receivers can ignore events whose `occurred_at` is too old to be useful.

Events and delivery history are retained in the database; no automatic pruning
is performed. Use destinations you control; payloads can include item/project
notes. Local HTTP URLs are supported for workshop devices.
