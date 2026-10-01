import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.main as main


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous = main.DB_PATH, main.LAYOUT
        main.DB_PATH = Path(self.directory.name) / 'test.db'
        main.LAYOUT = 'example'
        self.client = main.app.test_client()
        self.client.post('/items/new', data=dict(name='LED', quantity=10, location_id=1))

    def tearDown(self):
        main.DB_PATH, main.LAYOUT = self.previous
        self.directory.cleanup()

    def test_registry_and_wildcard_are_available_in_settings(self):
        with mock.patch.object(main, 'dispatch_webhooks'):
            for event in [*main.WEBHOOK_EVENTS, '*']:
                self.client.post('/settings/webhooks', data=dict(event=event, destination_url='http://controller.local/led'))
        with main.app.app_context():
            self.assertEqual(main.db().execute('SELECT count(*) FROM webhooks').fetchone()[0], len(main.WEBHOOK_EVENTS) + 1)
        html = self.client.get('/settings/webhooks').get_data(as_text=True)
        for event in main.WEBHOOK_EVENTS:
            self.assertIn(f'value="{event}"', html)

    def test_find_reports_location_and_does_not_change_stock(self):
        with mock.patch.object(main, 'dispatch_webhooks') as dispatch:
            response = self.client.post('/items/1/find', json={})
            self.assertEqual(response.status_code, 200)
            event, payload = dispatch.call_args.args
            self.assertEqual(event, 'item.found')
            self.assertEqual(payload['item']['name'], 'LED')
            self.assertEqual(payload['location']['id'], 1)
            self.assertEqual(payload['location']['code'], payload['item']['location'])
            self.assertEqual(payload['item']['quantity'], 10)
            self.assertEqual(self.client.post('/items/999/find', json={}).status_code, 404)
            self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(self.client.get('/items/1/find').status_code, 405)
        self.assertIn('Locate item', self.client.get('/items/1').get_data(as_text=True))

    def test_delivery_matches_event_and_wildcard_but_not_inactive(self):
        with main.app.app_context():
            main.init_db()
            main.db().executemany('INSERT INTO webhooks(event,destination_url,active,created_at) VALUES (?,?,?,?)', [
                ('item.found', 'http://controller.local/find', 1, ''),
                ('*', 'http://controller.local/all', 1, ''),
                ('stock.out', 'http://controller.local/stock', 1, ''),
                ('item.found', 'http://controller.local/off', 0, ''),
            ])
            main.db().commit()
            main.dispatch_webhooks('item.found', main.item_payload(1))
            main.db().commit()
        from app import webhooks
        with mock.patch.object(webhooks.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.status = 204
            self.assertTrue(webhooks.deliver_one(main.DB_PATH, main.app.logger))
            self.assertTrue(webhooks.deliver_one(main.DB_PATH, main.app.logger))
            self.assertFalse(webhooks.deliver_one(main.DB_PATH, main.app.logger))
            self.assertEqual(send.call_count, 2)
            body = json.loads(send.call_args.args[0].data)
            self.assertEqual(body['event'], 'item.found')
            self.assertIn('id', body)
            self.assertIn('occurred_at', body)

    def test_failed_build_sends_no_stock_events(self):
        page = self.client.post('/projects', data=dict(title='Lights')).headers['Location']
        self.client.post(page, data=dict(item_id=1, per_build=2))
        original = main.record_movement
        def fail(conn, *args):
            original(conn, *args)
            raise sqlite3.OperationalError('disk full')
        with mock.patch.object(main, 'record_movement', side_effect=fail):
            with self.assertLogs(main.app.logger, level='ERROR'):
                self.assertEqual(self.client.post(page + '/build', data=dict(quantity=1)).status_code, 500)
        with main.app.app_context():
            self.assertEqual(main.db().execute("SELECT count(*) FROM events WHERE event='stock.out'").fetchone()[0], 0)
        with main.app.app_context():
            self.assertEqual(main.db().execute('SELECT quantity FROM items WHERE id=1').fetchone()[0], 10)

    def test_stock_and_event_commit_together(self):
        original = main.enqueue_event
        def check(conn, event, payload):
            if event == 'stock.out':
                with sqlite3.connect(main.DB_PATH) as other:
                    self.assertEqual(other.execute('SELECT quantity FROM items WHERE id=1').fetchone()[0], 10)
                    self.assertEqual(other.execute("SELECT count(*) FROM events WHERE event='stock.out'").fetchone()[0], 0)
            return original(conn, event, payload)
        with mock.patch.object(main, 'enqueue_event', side_effect=check):
            self.assertEqual(self.client.post('/items/1', data=dict(quantity_change=-2)).status_code, 302)
        with sqlite3.connect(main.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT quantity FROM items WHERE id=1').fetchone()[0], 8)
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE event='stock.out'").fetchone()[0], 1)

    def test_retry_keeps_event_id_and_survives_restart(self):
        from app import webhooks
        with main.app.app_context():
            main.db().execute("INSERT INTO webhooks(event,destination_url,created_at) VALUES ('item.found','http://controller.local/led','')")
            main.dispatch_webhooks('item.found', main.item_payload(1))
            main.db().commit()
        with mock.patch.object(webhooks.urllib.request, 'urlopen', side_effect=OSError('offline')):
            with self.assertLogs(main.app.logger, level='WARNING'):
                webhooks.deliver_one(main.DB_PATH, main.app.logger)
        with sqlite3.connect(main.DB_PATH) as conn:
            event_id, attempts, delivered = conn.execute('SELECT event_id,attempts,delivered_at FROM webhook_deliveries').fetchone()
            self.assertEqual((attempts, delivered), (1, None))
        self.assertFalse(webhooks.deliver_one(main.DB_PATH, main.app.logger))
        with mock.patch.object(webhooks.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.status = 200
            self.assertTrue(webhooks.deliver_one(main.DB_PATH, main.app.logger, now=10**12))
            self.assertEqual(json.loads(send.call_args.args[0].data)['id'], event_id)
        with sqlite3.connect(main.DB_PATH) as conn:
            self.assertIsNotNone(conn.execute('SELECT delivered_at FROM webhook_deliveries').fetchone()[0])

    def test_crashed_worker_lease_is_recovered(self):
        from app import webhooks
        with main.app.app_context():
            main.db().execute("INSERT INTO webhooks(event,destination_url,created_at) VALUES ('item.found','http://controller.local/led','')")
            main.dispatch_webhooks('item.found', main.item_payload(1))
            main.db().execute("UPDATE webhook_deliveries SET lease_token='crashed', available_at=200")
            main.db().commit()
        self.assertFalse(webhooks.deliver_one(main.DB_PATH, main.app.logger, now=199))
        with mock.patch.object(webhooks.urllib.request, 'urlopen') as send:
            send.return_value.__enter__.return_value.status = 200
            self.assertTrue(webhooks.deliver_one(main.DB_PATH, main.app.logger, now=201))

    def test_event_insert_failure_rolls_back_domain_change(self):
        with mock.patch.object(main, 'enqueue_event', side_effect=sqlite3.OperationalError('disk full')):
            with self.assertLogs(main.app.logger, level='ERROR'):
                self.assertEqual(self.client.post('/items/1', data=dict(quantity_change=-2)).status_code, 500)
        with sqlite3.connect(main.DB_PATH) as conn:
            self.assertEqual(conn.execute('SELECT quantity FROM items WHERE id=1').fetchone()[0], 10)
            self.assertEqual(conn.execute("SELECT count(*) FROM movements WHERE quantity_change=-2").fetchone()[0], 0)

    def test_workers_cannot_claim_the_same_delivery(self):
        import threading
        from app import webhooks
        with main.app.app_context():
            main.db().execute("INSERT INTO webhooks(event,destination_url,created_at) VALUES ('item.found','http://controller.local/led','')")
            main.dispatch_webhooks('item.found', main.item_payload(1))
            main.db().commit()
        claimed, release = threading.Event(), threading.Event()
        def hold(*args, **kwargs):
            claimed.set()
            release.wait(5)
            response = mock.MagicMock()
            response.__enter__.return_value.status = 200
            return response
        with mock.patch.object(webhooks.urllib.request, 'urlopen', side_effect=hold) as send:
            worker = threading.Thread(target=webhooks.deliver_one, args=(main.DB_PATH, main.app.logger))
            worker.start()
            try:
                self.assertTrue(claimed.wait(5))
                self.assertFalse(webhooks.deliver_one(main.DB_PATH, main.app.logger))
            finally:
                release.set()
                worker.join(5)
            self.assertEqual(send.call_count, 1)
