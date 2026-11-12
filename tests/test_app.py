import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import app


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = app.DB_PATH
        app.DB_PATH = Path(self.tmp.name) / "test.db"
        app.init_db()

    def tearDown(self):
        app.DB_PATH = self.old_db
        self.tmp.cleanup()

    def test_shift_before_eight_belongs_to_previous_day(self):
        cfg = dict(app.DEFAULT_CONFIG)
        with patch.object(app, "datetime") as mocked:
            mocked.now.return_value = datetime(2026, 10, 8, 7, 30).astimezone()
            mocked.strptime = datetime.strptime
            mocked.combine = datetime.combine
            mocked.min = datetime.min
            start, _, label = app.shift_bounds(None, cfg)
        self.assertEqual(label, "2026-10-07")
        self.assertEqual(datetime.fromtimestamp(start).hour, 8)

    def test_period_stats_reconstructs_online_time(self):
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(1,'A')").lastrowid
            con.executemany(
                "INSERT INTO status_events(receiver_id,at,status) VALUES(?,?,?)",
                [(rid, 100, "online"), (rid, 200, "offline"), (rid, 250, "online")],
            )
            con.execute("INSERT INTO packet_samples(receiver_id,at,packets) VALUES(?,?,?)", (rid, 150, 42))
            with patch.object(app.time, "time", return_value=300):
                stats = app.period_stats(con, rid, 100, 300)
        self.assertEqual(stats["online_seconds"], 150)
        self.assertEqual(stats["outages"], 1)
        self.assertEqual(stats["packets"], 42)

    def test_receiver_packet_delta_and_offline_transition(self):
        cfg = dict(app.DEFAULT_CONFIG, health_mode="messages", offline_after_seconds=30)
        base = 1_700_000_000
        snapshots = [
            {"acList": [{"Icao": "ABC123", "CMsgs": 10}]},
            {"acList": [{"Icao": "ABC123", "CMsgs": 15}]},
            {"acList": [{"Icao": "ABC123", "CMsgs": 15}]},
        ]
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(1,'A')").lastrowid

        with patch.object(app, "request_json", side_effect=snapshots), \
             patch.object(app, "send_notification"):
            with patch.object(app.time, "time", return_value=base):
                with app.db() as con:
                    receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                app.poll_receiver(receiver, cfg)
            with patch.object(app.time, "time", return_value=base + 10):
                with app.db() as con:
                    receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                app.poll_receiver(receiver, cfg)
            with patch.object(app.time, "time", return_value=base + 45):
                with app.db() as con:
                    receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                app.poll_receiver(receiver, cfg)

        with app.db() as con:
            receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
            packets = con.execute("SELECT SUM(packets) n FROM packet_samples WHERE receiver_id=?", (rid,)).fetchone()["n"]
        self.assertEqual(receiver["status"], "offline")
        self.assertEqual(packets, 5)

    def test_http_health_mode_stays_online_without_aircraft_messages(self):
        cfg = dict(app.DEFAULT_CONFIG, health_mode="http", offline_after_seconds=30)
        base = 1_700_000_000
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(1,'A')").lastrowid

        with patch.object(app, "request_json", return_value={"acList": []}), \
             patch.object(app, "send_notification"):
            with patch.object(app.time, "time", return_value=base):
                with app.db() as con:
                    receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                app.poll_receiver(receiver, cfg)
            with patch.object(app.time, "time", return_value=base + 60):
                with app.db() as con:
                    receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                app.poll_receiver(receiver, cfg)

        with app.db() as con:
            receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
        self.assertEqual(receiver["status"], "online")

    def test_discovery_supports_classic_vrs_unique_id(self):
        payload = {
            "Receivers": [
                {"UniqueId": 7, "Name": "Основной"},
                {"id": 8, "name": "Резервный"},
                {"Name": "Служебная запись без ID"},
            ]
        }
        with patch.object(app, "request_json", return_value=payload):
            receivers = app.discover_receivers(dict(app.DEFAULT_CONFIG))
        self.assertEqual([(row["feed_id"], row["name"]) for row in receivers], [
            (7, "Основной"), (8, "Резервный")
        ])

    def test_webadmin_feed_statuses_are_parsed(self):
        payload = {"Response": {"Feeds": [{
            "Id": 64, "Name": "64", "Connection": 2,
            "ConnDesc": "Установлено", "Msgs": 1_023_291,
        }]}}
        with patch.object(app, "request_json", return_value=payload):
            statuses = app.fetch_vrs_feed_statuses(dict(app.DEFAULT_CONFIG))
        self.assertEqual(statuses[64], {
            "id": 64, "name": "64", "connection": 2,
            "connection_status": "Установлено", "messages": 1_023_291,
        })

    def test_webadmin_status_and_total_messages_drive_receiver(self):
        cfg = dict(app.DEFAULT_CONFIG)
        base = 1_700_000_000
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(64,'64')").lastrowid

        connected = {
            "id": 64, "name": "64", "connection": 2,
            "connection_status": "Установлено", "messages": 1_000,
        }
        with patch.object(app, "request_json", return_value={"acList": []}), \
             patch.object(app, "send_notification"):
            with patch.object(app.time, "time", return_value=base):
                with app.db() as con:
                    receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                app.poll_receiver(receiver, cfg, connected)
            connected["messages"] = 1_025
            with patch.object(app.time, "time", return_value=base + 15):
                with app.db() as con:
                    receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                app.poll_receiver(receiver, cfg, connected)

        with app.db() as con:
            receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
            packets = con.execute("SELECT COALESCE(SUM(packets),0) n FROM packet_samples WHERE receiver_id=?", (rid,)).fetchone()["n"]
        self.assertEqual(receiver["status"], "online")
        self.assertEqual(receiver["vrs_connection_status"], "Установлено")
        self.assertEqual(receiver["vrs_total_messages"], 1_025)
        self.assertEqual(packets, 25)

    def test_webadmin_failure_does_not_turn_total_into_daily_messages(self):
        cfg = dict(app.DEFAULT_CONFIG, offline_after_seconds=30)
        base = 1_700_000_000
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(64,'64')").lastrowid
            receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()

        with patch.object(app.time, "time", return_value=base):
            app.record_vrs_status_failure(receiver, cfg, RuntimeError("WebAdmin запускается"))
        with app.db() as con:
            con.execute(
                "INSERT INTO packet_samples(receiver_id,at,packets) VALUES(?,?,?)",
                (rid, base + 1, 1_023_291),
            )

        first_status = {
            "id": 64, "name": "64", "connection": 2,
            "connection_status": "Установлено", "messages": 1_023_291,
        }
        with patch.object(app, "request_json", return_value={"acList": []}), \
             patch.object(app, "send_notification"), \
             patch.object(app.time, "time", return_value=base + 15):
            with app.db() as con:
                receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
            app.poll_receiver(receiver, cfg, first_status)

        with app.db() as con:
            receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
            observed = datetime.fromtimestamp(base + 15).astimezone()
            shift_date = (observed if observed.hour >= cfg["shift_hour"] else observed - app.timedelta(days=1)).date().isoformat()
            daily = con.execute(
                "SELECT messages FROM daily_message_totals WHERE receiver_id=? AND shift_date=?",
                (rid, shift_date),
            ).fetchone()["messages"]
        self.assertEqual(receiver["vrs_total_messages"], 1_023_291)
        self.assertEqual(receiver["vrs_counter_initialized"], 1)
        self.assertEqual(daily, 0)

    def test_vrs_counter_reset_keeps_saved_total_and_daily_data(self):
        cfg = dict(app.DEFAULT_CONFIG)
        base = 1_700_000_000
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(64,'64')").lastrowid

        status = {
            "id": 64, "name": "64", "connection": 2,
            "connection_status": "Установлено", "messages": 1_000,
        }
        with patch.object(app, "request_json", return_value={"acList": []}), \
             patch.object(app, "send_notification"):
            for offset, raw_messages in ((0, 1_000), (15, 1_025), (30, 7), (45, 12)):
                status["messages"] = raw_messages
                with patch.object(app.time, "time", return_value=base + offset):
                    with app.db() as con:
                        receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
                    app.poll_receiver(receiver, cfg, status)

        with app.db() as con:
            receiver = con.execute("SELECT * FROM receivers WHERE id=?", (rid,)).fetchone()
            daily = con.execute(
                "SELECT messages FROM daily_message_totals WHERE receiver_id=?", (rid,)
            ).fetchone()["messages"]
        self.assertEqual(receiver["vrs_last_raw_messages"], 12)
        self.assertEqual(receiver["vrs_total_messages"], 1_037)
        self.assertEqual(daily, 37)

    def test_dashboard_reports_last_minute_packet_rate(self):
        with app.db() as con:
            rid = con.execute(
                "INSERT INTO receivers(feed_id,name,status,status_since) VALUES(1,'A','online',100)"
            ).lastrowid
            con.executemany(
                "INSERT INTO packet_samples(receiver_id,at,packets) VALUES(?,?,?)",
                [(rid, 130, 5), (rid, 150, 12), (rid, 199, 8)],
            )
        with patch.object(app, "load_config", return_value=dict(app.DEFAULT_CONFIG)), \
             patch.object(app.time, "time", return_value=200):
            payload = app.dashboard_payload()
        self.assertEqual(payload["receivers"][0]["packet_rate"], 20)

    def test_notifications_are_persisted_and_newest_first(self):
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(3,'Север')").lastrowid
            con.executemany(
                "INSERT INTO status_events(receiver_id,at,status,reason) VALUES(?,?,?,?)",
                [(rid, 100, "offline", "Нет данных"), (rid, 200, "online", "Восстановлен")],
            )
        notifications = app.notifications_payload()
        self.assertEqual([item["status"] for item in notifications], ["online", "offline"])
        self.assertEqual(notifications[0]["receiver_name"], "Север")

    def test_military_aircraft_is_saved_and_report_contains_route(self):
        cfg = dict(app.DEFAULT_CONFIG)
        observed = datetime(2026, 10, 8, 9, 0).astimezone()
        timestamp = int(observed.timestamp())
        with app.db() as con:
            rid = con.execute("INSERT INTO receivers(feed_id,name) VALUES(9,'Военный канал')").lastrowid
            app.record_military_aircraft(con, rid, [{
                "Icao": "ABC123", "Mil": True, "Call": "TEST01", "Reg": "RF-00001",
                "Type": "IL76", "Alt": 25000, "Spd": 410, "From": "UAAA Алматы",
                "Stops": ["UACC Астана"], "To": "UUEE Москва", "CMsgs": 77,
            }, {"Icao": "CIV001", "Mil": False, "Call": "CIVIL"}], timestamp, 8)
        messages = app.military_report_messages("2026-10-08", cfg)
        report = "\n".join(messages)
        self.assertIn("TEST01", report)
        self.assertIn("UAAA Алматы → UACC Астана → UUEE Москва", report)
        self.assertNotIn("CIVIL", report)

    def test_telegram_link_is_normalized_to_channel_username(self):
        self.assertEqual(app.normalize_telegram_chat("https://t.me/polnyijinglebell5"), "@polnyijinglebell5")

    def test_telegram_ssl_context_keeps_certificate_verification_enabled(self):
        app._telegram_ssl_contexts.clear()
        context = app.telegram_ssl_context()
        self.assertEqual(context.verify_mode, app.ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_vrs_credentials_persist_in_config_file(self):
        old_config_path = app.CONFIG_PATH
        app.CONFIG_PATH = Path(self.tmp.name) / "config.json"
        try:
            app.save_config({"vrs_username": "admin", "vrs_password": "secret-password"})
            loaded = app.load_config()
        finally:
            app.CONFIG_PATH = old_config_path
        self.assertEqual(loaded["vrs_username"], "admin")
        self.assertEqual(loaded["vrs_password"], "secret-password")

    def test_self_signed_mode_is_explicit_and_scoped(self):
        app._telegram_ssl_contexts.clear()
        insecure = app.telegram_ssl_context(True)
        secure = app.telegram_ssl_context(False)
        self.assertEqual(insecure.verify_mode, app.ssl.CERT_NONE)
        self.assertFalse(insecure.check_hostname)
        self.assertEqual(secure.verify_mode, app.ssl.CERT_REQUIRED)
        self.assertTrue(secure.check_hostname)


if __name__ == "__main__":
    unittest.main()
