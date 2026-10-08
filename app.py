"""VRS Monitor: dependency-free receiver monitoring web application."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import logging
import mimetypes
import os
import secrets
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT))
DATA_DIR = ROOT / "data"
STATIC_DIR = BUNDLE_ROOT / "static"
CONFIG_PATH = ROOT / "config.json"
DB_PATH = DATA_DIR / "vrs_monitor.db"
LOG_PATH = DATA_DIR / "vrs_monitor.log"

DEFAULT_CONFIG = {
    "vrs_url": "http://127.0.0.1:8080/VirtualRadar",
    "poll_seconds": 15,
    "offline_after_seconds": 90,
    "health_mode": "messages",
    "shift_hour": 8,
    "listen_host": "0.0.0.0",
    "listen_port": 8090,
    "vrs_username": "",
    "vrs_password": "",
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "webhook_url": "",
    "military_report_enabled": False,
    "military_report_time": "08:05",
    "military_report_chat": "@polnyijinglebell5",
    "telegram_allow_self_signed": False,
}

config_lock = threading.RLock()
poll_now = threading.Event()
stop_event = threading.Event()
_telegram_ssl_contexts: dict[bool, ssl.SSLContext] = {}


def load_config() -> dict[str, Any]:
    with config_lock:
        cfg = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                logging.exception("Не удалось прочитать config.json")
        return cfg


def save_config(changes: dict[str, Any]) -> dict[str, Any]:
    allowed = set(DEFAULT_CONFIG)
    cfg = load_config()
    for key, value in changes.items():
        if key in allowed:
            cfg[key] = value
    cfg["poll_seconds"] = max(5, min(3600, int(cfg["poll_seconds"])))
    cfg["offline_after_seconds"] = max(cfg["poll_seconds"] * 2, int(cfg["offline_after_seconds"]))
    cfg["shift_hour"] = max(0, min(23, int(cfg["shift_hour"])))
    cfg["listen_port"] = max(1, min(65535, int(cfg["listen_port"])))
    cfg["health_mode"] = "http" if cfg.get("health_mode") == "http" else "messages"
    cfg["military_report_enabled"] = bool(cfg.get("military_report_enabled"))
    cfg["telegram_allow_self_signed"] = bool(cfg.get("telegram_allow_self_signed"))
    report_time = str(cfg.get("military_report_time", "08:05"))
    try:
        datetime.strptime(report_time, "%H:%M")
    except ValueError:
        report_time = "08:05"
    cfg["military_report_time"] = report_time
    cfg["vrs_url"] = str(cfg["vrs_url"]).rstrip("/")
    with config_lock:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    poll_now.set()
    return cfg


@contextmanager
def db():
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS receivers (
                id INTEGER PRIMARY KEY,
                feed_id INTEGER NOT NULL UNIQUE,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown',
                status_since INTEGER,
                last_seen INTEGER,
                last_packet_at INTEGER,
                total_packets INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                counters_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS status_events (
                id INTEGER PRIMARY KEY,
                receiver_id INTEGER NOT NULL REFERENCES receivers(id),
                at INTEGER NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                UNIQUE(receiver_id, at, status)
            );
            CREATE INDEX IF NOT EXISTS ix_events_receiver_at
                ON status_events(receiver_id, at);
            CREATE TABLE IF NOT EXISTS packet_samples (
                id INTEGER PRIMARY KEY,
                receiver_id INTEGER NOT NULL REFERENCES receivers(id),
                at INTEGER NOT NULL,
                packets INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_samples_receiver_at
                ON packet_samples(receiver_id, at);
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY,
                receiver_id INTEGER REFERENCES receivers(id),
                at INTEGER NOT NULL,
                channel TEXT NOT NULL,
                success INTEGER NOT NULL,
                detail TEXT
            );
            CREATE TABLE IF NOT EXISTS military_sightings (
                id INTEGER PRIMARY KEY,
                receiver_id INTEGER NOT NULL REFERENCES receivers(id),
                shift_date TEXT NOT NULL,
                aircraft_key TEXT NOT NULL,
                icao TEXT,
                callsign TEXT,
                registration TEXT,
                aircraft_type TEXT,
                model TEXT,
                operator TEXT,
                country TEXT,
                first_seen INTEGER NOT NULL,
                last_seen INTEGER NOT NULL,
                max_altitude INTEGER,
                max_speed INTEGER,
                from_airport TEXT,
                to_airport TEXT,
                stops TEXT,
                messages INTEGER NOT NULL DEFAULT 0,
                UNIQUE(receiver_id, shift_date, aircraft_key)
            );
            CREATE INDEX IF NOT EXISTS ix_military_sightings_shift
                ON military_sightings(shift_date, first_seen);
            CREATE TABLE IF NOT EXISTS report_runs (
                report_key TEXT PRIMARY KEY,
                at INTEGER NOT NULL,
                success INTEGER NOT NULL,
                detail TEXT
            );
            """
        )


def request_json(url: str, cfg: dict[str, Any], payload: dict[str, Any] | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json", "User-Agent": "VRS-Monitor/1.0"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if cfg.get("vrs_username"):
        token = base64.b64encode(f'{cfg["vrs_username"]}:{cfg.get("vrs_password", "")}'.encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8-sig"))


def discover_receivers(cfg: dict[str, Any]) -> list[sqlite3.Row]:
    data = request_json(f'{cfg["vrs_url"]}/ServerConfig.json', cfg)
    feeds = data.get("Receivers") or data.get("receivers") or []
    with db() as con:
        known_ids: list[int] = []
        for feed in feeds:
            if not isinstance(feed, dict):
                logging.warning("VRS вернул некорректную запись приёмника: %r", feed)
                continue
            # Classic VRS serialises ServerReceiverJson as UniqueId/Name, while
            # other releases and documentation use id/name or Id/Name.
            raw_id = next(
                (feed[key] for key in ("id", "Id", "UniqueId", "UniqueID", "uniqueId")
                 if key in feed and feed[key] is not None),
                None,
            )
            if raw_id is None:
                logging.warning("Пропущена запись VRS без ID: %r", feed)
                continue
            try:
                feed_id = int(raw_id)
            except (TypeError, ValueError):
                logging.warning("Пропущена запись VRS с неверным ID: %r", feed)
                continue
            raw_name = next(
                (feed[key] for key in ("name", "Name") if key in feed and feed[key]),
                f"Приёмник {feed_id}",
            )
            name = str(raw_name)
            known_ids.append(feed_id)
            con.execute(
                """INSERT INTO receivers(feed_id, name) VALUES(?, ?)
                   ON CONFLICT(feed_id) DO UPDATE SET name=excluded.name, enabled=1""",
                (feed_id, name),
            )
        if known_ids:
            marks = ",".join("?" for _ in known_ids)
            con.execute(f"UPDATE receivers SET enabled=0 WHERE feed_id NOT IN ({marks})", known_ids)
        return con.execute("SELECT * FROM receivers WHERE enabled=1 ORDER BY name").fetchall()


def set_status(con: sqlite3.Connection, receiver: sqlite3.Row, status: str, now: int, reason: str) -> bool:
    if receiver["status"] == status:
        return False
    con.execute(
        "UPDATE receivers SET status=?, status_since=?, last_error=? WHERE id=?",
        (status, now, reason if status == "offline" else None, receiver["id"]),
    )
    con.execute(
        "INSERT OR IGNORE INTO status_events(receiver_id, at, status, reason) VALUES(?,?,?,?)",
        (receiver["id"], now, status, reason),
    )
    return True


def record_military_aircraft(
    con: sqlite3.Connection, receiver_id: int, aircraft: list[dict[str, Any]], now: int, shift_hour: int
) -> None:
    local_now = datetime.fromtimestamp(now).astimezone()
    shift_day = local_now.date() if local_now.hour >= shift_hour else (local_now - timedelta(days=1)).date()
    for item in aircraft:
        if item.get("Mil") is not True:
            continue
        aircraft_key = str(item.get("Icao") or item.get("Id") or "").strip()
        if not aircraft_key:
            continue
        stops_value = item.get("Stops") or []
        stops = " → ".join(str(value) for value in stops_value) if isinstance(stops_value, list) else str(stops_value)
        altitude = item.get("Alt")
        speed = item.get("Spd")
        con.execute(
            """INSERT INTO military_sightings(
                   receiver_id,shift_date,aircraft_key,icao,callsign,registration,aircraft_type,
                   model,operator,country,first_seen,last_seen,max_altitude,max_speed,
                   from_airport,to_airport,stops,messages
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(receiver_id,shift_date,aircraft_key) DO UPDATE SET
                   callsign=COALESCE(NULLIF(excluded.callsign,''),military_sightings.callsign),
                   registration=COALESCE(NULLIF(excluded.registration,''),military_sightings.registration),
                   aircraft_type=COALESCE(NULLIF(excluded.aircraft_type,''),military_sightings.aircraft_type),
                   model=COALESCE(NULLIF(excluded.model,''),military_sightings.model),
                   operator=COALESCE(NULLIF(excluded.operator,''),military_sightings.operator),
                   country=COALESCE(NULLIF(excluded.country,''),military_sightings.country),
                   last_seen=excluded.last_seen,
                   max_altitude=CASE WHEN excluded.max_altitude IS NULL THEN military_sightings.max_altitude
                       WHEN military_sightings.max_altitude IS NULL OR excluded.max_altitude>military_sightings.max_altitude THEN excluded.max_altitude ELSE military_sightings.max_altitude END,
                   max_speed=CASE WHEN excluded.max_speed IS NULL THEN military_sightings.max_speed
                       WHEN military_sightings.max_speed IS NULL OR excluded.max_speed>military_sightings.max_speed THEN excluded.max_speed ELSE military_sightings.max_speed END,
                   from_airport=COALESCE(NULLIF(excluded.from_airport,''),military_sightings.from_airport),
                   to_airport=COALESCE(NULLIF(excluded.to_airport,''),military_sightings.to_airport),
                   stops=COALESCE(NULLIF(excluded.stops,''),military_sightings.stops),
                   messages=MAX(excluded.messages,military_sightings.messages)""",
            (
                receiver_id, shift_day.isoformat(), aircraft_key, str(item.get("Icao") or ""),
                str(item.get("Call") or "").strip(), str(item.get("Reg") or "").strip(),
                str(item.get("Type") or "").strip(), str(item.get("Mdl") or "").strip(),
                str(item.get("Op") or "").strip(), str(item.get("Cou") or "").strip(), now, now,
                int(altitude) if isinstance(altitude, (int, float)) else None,
                int(speed) if isinstance(speed, (int, float)) else None,
                str(item.get("From") or "").strip(), str(item.get("To") or "").strip(), stops,
                int(item.get("CMsgs", 0) or 0),
            ),
        )


def send_notification(receiver_id: int, receiver_name: str, status: str, reason: str, cfg: dict[str, Any]) -> None:
    moment = datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S")
    marker = "🔴" if status == "offline" else "🟢"
    text = f"{marker} {receiver_name}: {'ОТКЛЮЧЁН' if status == 'offline' else 'снова работает'}\n{moment}"
    if reason:
        text += f"\n{reason}"
    targets: list[tuple[str, str, dict[str, Any]]] = []
    if cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id"):
        url = f'https://api.telegram.org/bot{cfg["telegram_bot_token"]}/sendMessage'
        targets.append(("telegram", url, {"chat_id": cfg["telegram_chat_id"], "text": text}))
    if cfg.get("webhook_url"):
        targets.append(("webhook", cfg["webhook_url"], {
            "event": f"receiver.{status}", "receiver": receiver_name,
            "status": status, "reason": reason, "time": moment, "text": text,
        }))
    for channel, url, payload in targets:
        ok, detail = 1, "Отправлено"
        try:
            request = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "User-Agent": "VRS-Monitor/1.0"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                if response.status >= 300:
                    raise RuntimeError(f"HTTP {response.status}")
        except Exception as exc:  # notification failure must not stop monitoring
            ok, detail = 0, str(exc)[:500]
            logging.warning("Уведомление %s не отправлено: %s", channel, exc)
        with db() as con:
            con.execute(
                "INSERT INTO notifications(receiver_id, at, channel, success, detail) VALUES(?,?,?,?,?)",
                (receiver_id, int(time.time()), channel, ok, detail),
            )


def poll_receiver(receiver: sqlite3.Row, cfg: dict[str, Any]) -> None:
    now = int(time.time())
    changed: tuple[str, str] | None = None
    try:
        url = f'{cfg["vrs_url"]}/AircraftList.json?feed={receiver["feed_id"]}'
        data = request_json(url, cfg)
        aircraft = data.get("acList") or []
        current = {
            str(item.get("Icao") or item.get("Id")): int(item.get("CMsgs", 0) or 0)
            for item in aircraft if item.get("Icao") is not None or item.get("Id") is not None
        }
        previous = json.loads(receiver["counters_json"] or "{}")
        baseline_exists = bool(previous) or receiver["last_seen"] is not None
        packet_delta = 0
        if baseline_exists:
            for key, count in current.items():
                old = int(previous.get(key, 0))
                packet_delta += count - old if count >= old and key in previous else count

        with db() as con:
            fresh = con.execute("SELECT * FROM receivers WHERE id=?", (receiver["id"],)).fetchone()
            record_military_aircraft(con, receiver["id"], aircraft, now, int(cfg["shift_hour"]))
            last_packet = now if packet_delta > 0 else (fresh["last_packet_at"] or now)
            con.execute(
                """UPDATE receivers SET last_seen=?, last_packet_at=?, total_packets=total_packets+?,
                   counters_json=?, last_error=NULL WHERE id=?""",
                (now, last_packet, packet_delta, json.dumps(current, separators=(",", ":")), receiver["id"]),
            )
            if packet_delta:
                con.execute(
                    "INSERT INTO packet_samples(receiver_id, at, packets) VALUES(?,?,?)",
                    (receiver["id"], now, packet_delta),
                )
            healthy = cfg["health_mode"] == "http" or packet_delta > 0 or now - last_packet < int(cfg["offline_after_seconds"])
            target = "online" if healthy else "offline"
            reason = "Нет новых сообщений от VRS" if target == "offline" else "Поток данных восстановлен"
            if set_status(con, fresh, target, now, reason):
                # A normal application start must not look like a recovery alert.
                if not (fresh["status"] == "unknown" and target == "online"):
                    changed = (target, reason)
    except Exception as exc:
        error = f"VRS недоступен: {exc}"[:500]
        logging.warning("Ошибка опроса %s: %s", receiver["name"], exc)
        with db() as con:
            fresh = con.execute("SELECT * FROM receivers WHERE id=?", (receiver["id"],)).fetchone()
            con.execute("UPDATE receivers SET last_error=? WHERE id=?", (error, receiver["id"]))
            reference = fresh["last_seen"] or fresh["status_since"] or now
            if now - reference >= int(cfg["offline_after_seconds"]):
                if set_status(con, fresh, "offline", now, error):
                    changed = ("offline", error)
    if changed:
        send_notification(receiver["id"], receiver["name"], changed[0], changed[1], cfg)


def monitor_loop() -> None:
    while not stop_event.is_set():
        cfg = load_config()
        try:
            receivers = discover_receivers(cfg)
            for receiver in receivers:
                if stop_event.is_set():
                    break
                poll_receiver(receiver, cfg)
        except Exception as exc:
            logging.warning("VRS недоступен при получении списка приёмников: %s", exc)
            # Existing receivers still need to transition to offline.
            with db() as con:
                receivers = con.execute("SELECT * FROM receivers WHERE enabled=1").fetchall()
            for receiver in receivers:
                poll_receiver(receiver, cfg)
        maybe_send_scheduled_military_report(cfg)
        poll_now.wait(int(cfg["poll_seconds"]))
        poll_now.clear()


def shift_bounds(date_text: str | None, cfg: dict[str, Any]) -> tuple[int, int, str]:
    now = datetime.now().astimezone()
    hour = int(cfg["shift_hour"])
    if date_text:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
    else:
        day = now.date() if now.hour >= hour else (now - timedelta(days=1)).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=now.tzinfo).replace(hour=hour)
    end = start + timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp()), day.isoformat()


def status_at(con: sqlite3.Connection, receiver_id: int, at: int) -> str:
    row = con.execute(
        "SELECT status FROM status_events WHERE receiver_id=? AND at<=? ORDER BY at DESC LIMIT 1",
        (receiver_id, at),
    ).fetchone()
    return row["status"] if row else "unknown"


def period_stats(con: sqlite3.Connection, receiver_id: int, start: int, end: int) -> dict[str, Any]:
    effective_end = min(end, int(time.time()))
    state = status_at(con, receiver_id, start)
    cursor = start
    online = 0
    outages = 0
    events = con.execute(
        "SELECT at,status,reason FROM status_events WHERE receiver_id=? AND at>? AND at<? ORDER BY at",
        (receiver_id, start, effective_end),
    ).fetchall()
    for event in events:
        if state == "online":
            online += event["at"] - cursor
        if event["status"] == "offline" and state != "offline":
            outages += 1
        state, cursor = event["status"], event["at"]
    if state == "online":
        online += max(0, effective_end - cursor)
    packets = con.execute(
        "SELECT COALESCE(SUM(packets),0) value FROM packet_samples WHERE receiver_id=? AND at>=? AND at<?",
        (receiver_id, start, end),
    ).fetchone()["value"]
    return {"online_seconds": online, "outages": outages, "packets": packets}


def dashboard_payload(date_text: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    start, end, label = shift_bounds(date_text, cfg)
    now = int(time.time())
    result = []
    with db() as con:
        receivers = con.execute("SELECT * FROM receivers WHERE enabled=1 ORDER BY name").fetchall()
        for receiver in receivers:
            stats = period_stats(con, receiver["id"], start, end)
            packet_rate = con.execute(
                "SELECT COALESCE(SUM(packets),0) value FROM packet_samples WHERE receiver_id=? AND at>?",
                (receiver["id"], now - 60),
            ).fetchone()["value"]
            result.append({
                "id": receiver["id"], "feed_id": receiver["feed_id"], "name": receiver["name"],
                "status": receiver["status"], "status_since": receiver["status_since"],
                "last_seen": receiver["last_seen"], "last_packet_at": receiver["last_packet_at"],
                "last_error": receiver["last_error"], "continuous_seconds": max(0, now - receiver["status_since"]) if receiver["status_since"] else 0,
                "packet_rate": packet_rate,
                **stats,
            })
    return {
        "server_time": now, "shift_date": label, "shift_start": start, "shift_end": end,
        "shift_hour": cfg["shift_hour"], "receivers": result,
    }


def events_payload(start: int, end: int, receiver_id: int | None = None) -> list[dict[str, Any]]:
    query = """SELECT e.id,e.at,e.status,e.reason,r.name receiver_name
               FROM status_events e JOIN receivers r ON r.id=e.receiver_id
               WHERE e.at>=? AND e.at<?"""
    params: list[Any] = [start, end]
    if receiver_id:
        query += " AND e.receiver_id=?"
        params.append(receiver_id)
    query += " ORDER BY e.at DESC LIMIT 1000"
    with db() as con:
        return [dict(row) for row in con.execute(query, params).fetchall()]


def notifications_payload(limit: int = 200) -> list[dict[str, Any]]:
    limit = max(1, min(1000, int(limit)))
    with db() as con:
        rows = con.execute(
            """SELECT e.id,e.at,e.status,e.reason,r.name receiver_name
               FROM status_events e JOIN receivers r ON r.id=e.receiver_id
               ORDER BY e.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def normalize_telegram_chat(value: str) -> str:
    chat = str(value or "").strip().rstrip("/")
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if chat.lower().startswith(prefix):
            chat = chat[len(prefix):]
            break
    if chat and not chat.startswith(("@", "-")) and not chat.isdigit():
        chat = "@" + chat
    return chat


def telegram_ssl_context(allow_self_signed: bool = False) -> ssl.SSLContext:
    """Build a verified TLS context enriched with the Windows trusted stores."""
    if allow_self_signed:
        if True not in _telegram_ssl_contexts:
            # Explicit opt-in, scoped exclusively to Telegram report requests.
            _telegram_ssl_contexts[True] = ssl._create_unverified_context()
        return _telegram_ssl_contexts[True]
    if False in _telegram_ssl_contexts:
        return _telegram_ssl_contexts[False]
    context = ssl.create_default_context()
    if sys.platform == "win32" and hasattr(ssl, "enum_certificates"):
        loaded: set[bytes] = set()
        for store_name in ("ROOT", "CA"):
            try:
                certificates = ssl.enum_certificates(store_name)
            except OSError:
                logging.warning("Не удалось прочитать хранилище сертификатов Windows: %s", store_name)
                continue
            for certificate, encoding, _trust in certificates:
                if encoding != "x509_asn" or certificate in loaded:
                    continue
                try:
                    context.load_verify_locations(cadata=ssl.DER_cert_to_PEM_cert(certificate))
                    loaded.add(certificate)
                except (ValueError, ssl.SSLError):
                    continue
        logging.info("Для Telegram загружено доверенных сертификатов Windows: %s", len(loaded))
    _telegram_ssl_contexts[False] = context
    return context


def last_completed_shift_date(now: datetime, shift_hour: int) -> str:
    today_boundary = now.replace(hour=shift_hour, minute=0, second=0, microsecond=0)
    completed_end = today_boundary if now >= today_boundary else today_boundary - timedelta(days=1)
    return (completed_end - timedelta(days=1)).date().isoformat()


def military_report_messages(shift_date: str, cfg: dict[str, Any]) -> list[str]:
    shift_start = datetime.strptime(shift_date, "%Y-%m-%d").date()
    start_text = datetime.combine(shift_start, datetime.min.time()).replace(hour=int(cfg["shift_hour"])).strftime("%d.%m.%Y %H:%M")
    end_text = (datetime.combine(shift_start, datetime.min.time()) + timedelta(days=1)).replace(hour=int(cfg["shift_hour"])).strftime("%d.%m.%Y %H:%M")
    with db() as con:
        rows = con.execute(
            """SELECT s.*,r.name receiver_name FROM military_sightings s
               JOIN receivers r ON r.id=s.receiver_id WHERE s.shift_date=?
               ORDER BY s.first_seen""",
            (shift_date,),
        ).fetchall()
    merged: dict[str, dict[str, Any]] = {}
    for source in rows:
        key = source["icao"] or source["aircraft_key"]
        item = merged.setdefault(key, dict(source) | {"receivers": set()})
        item["receivers"].add(source["receiver_name"])
        item["first_seen"] = min(item["first_seen"], source["first_seen"])
        item["last_seen"] = max(item["last_seen"], source["last_seen"])
        for field in ("max_altitude", "max_speed", "messages"):
            item[field] = max(item[field] or 0, source[field] or 0)
        for field in ("callsign", "registration", "aircraft_type", "model", "operator", "country", "from_airport", "to_airport", "stops"):
            if not item.get(field) and source[field]:
                item[field] = source[field]
    header = f"ВОЕННЫЕ САМОЛЁТЫ\nПериод: {start_text} — {end_text}\nОбнаружено: {len(merged)}\n"
    if not merged:
        return [header + "\nЗа этот период военные воздушные суда не обнаружены."]
    entries = []
    for number, item in enumerate(merged.values(), 1):
        identity = item.get("callsign") or item.get("registration") or item.get("icao") or item.get("aircraft_key")
        route_parts = [item.get("from_airport"), item.get("stops"), item.get("to_airport")]
        route = " → ".join(str(part) for part in route_parts if part) or "не указан VRS"
        first = datetime.fromtimestamp(item["first_seen"]).astimezone().strftime("%d.%m %H:%M:%S")
        last = datetime.fromtimestamp(item["last_seen"]).astimezone().strftime("%d.%m %H:%M:%S")
        details = [
            f"{number}. ✈ {identity}",
            f"ICAO: {item.get('icao') or '—'} | Регистрация: {item.get('registration') or '—'}",
            f"Тип: {item.get('aircraft_type') or '—'} {item.get('model') or ''}".rstrip(),
            f"Оператор/страна: {item.get('operator') or '—'} / {item.get('country') or '—'}",
            f"Маршрут: {route}",
            f"Наблюдался: {first} — {last}",
            f"Макс. высота: {item.get('max_altitude') or '—'} ft | скорость: {item.get('max_speed') or '—'} kt",
            f"Приёмники: {', '.join(sorted(item['receivers']))}",
        ]
        entries.append("\n".join(details))
    messages: list[str] = []
    current = header
    for entry in entries:
        addition = "\n\n" + entry
        if len(current) + len(addition) > 3900:
            messages.append(current)
            current = f"ВОЕННЫЕ САМОЛЁТЫ — продолжение\nПериод: {start_text} — {end_text}\n" + addition
        else:
            current += addition
    messages.append(current)
    return messages


def send_military_report(cfg: dict[str, Any], shift_date: str | None = None) -> dict[str, Any]:
    token = str(cfg.get("telegram_bot_token") or "").strip()
    chat = normalize_telegram_chat(str(cfg.get("military_report_chat") or ""))
    if not token:
        raise ValueError("Не указан Telegram Bot Token")
    if not chat:
        raise ValueError("Не указан получатель Telegram")
    if shift_date is None:
        shift_date = last_completed_shift_date(datetime.now().astimezone(), int(cfg["shift_hour"]))
    messages = military_report_messages(shift_date, cfg)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for message in messages:
        payload = json.dumps({"chat_id": chat, "text": message, "disable_web_page_preview": True}).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "User-Agent": "VRS-Monitor/1.0"})
        try:
            with urllib.request.urlopen(
                request, timeout=20,
                context=telegram_ssl_context(bool(cfg.get("telegram_allow_self_signed"))),
            ) as response:
                result = json.loads(response.read().decode("utf-8"))
                if response.status >= 300 or not result.get("ok"):
                    raise RuntimeError(result.get("description") or f"Telegram HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("description")
            except Exception:
                detail = None
            raise RuntimeError(detail or f"Telegram HTTP {exc.code}") from exc
    return {"ok": True, "shift_date": shift_date, "messages": len(messages), "chat": chat}


def maybe_send_scheduled_military_report(cfg: dict[str, Any]) -> None:
    if not cfg.get("military_report_enabled"):
        return
    now = datetime.now().astimezone()
    hours, minutes = (int(part) for part in str(cfg["military_report_time"]).split(":"))
    scheduled = now.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if now < scheduled:
        return
    report_key = f"military:{now.date().isoformat()}"
    with db() as con:
        previous = con.execute("SELECT * FROM report_runs WHERE report_key=?", (report_key,)).fetchone()
    if previous and (previous["success"] or int(time.time()) - previous["at"] < 600):
        return
    try:
        result = send_military_report(cfg)
        success, detail = 1, f'{result["chat"]}, сообщений: {result["messages"]}'
        logging.info("Военный отчёт отправлен: %s", detail)
    except Exception as exc:
        success, detail = 0, str(exc)[:500]
        logging.warning("Военный отчёт не отправлен: %s", exc)
    with db() as con:
        con.execute(
            """INSERT INTO report_runs(report_key,at,success,detail) VALUES(?,?,?,?)
               ON CONFLICT(report_key) DO UPDATE SET at=excluded.at,success=excluded.success,detail=excluded.detail""",
            (report_key, int(time.time()), success, detail),
        )


def report_csv(date_text: str | None) -> tuple[str, bytes]:
    data = dashboard_payload(date_text)
    stream = io.StringIO()
    writer = csv.writer(stream, delimiter=";")
    hour = int(data["shift_hour"])
    writer.writerow(["Отчёт VRS", f'{data["shift_date"]} {hour:02d}:00 — следующие сутки {hour:02d}:00'])
    writer.writerow(["Приёмник", "Состояние", "Время работы, ч", "Простои", "Пакеты", "Последний пакет", "Ошибка"])
    for item in data["receivers"]:
        last = datetime.fromtimestamp(item["last_packet_at"]).astimezone().strftime("%d.%m.%Y %H:%M:%S") if item["last_packet_at"] else "—"
        writer.writerow([
            item["name"], "Работает" if item["status"] == "online" else "Отключён",
            f'{item["online_seconds"] / 3600:.2f}'.replace(".", ","), item["outages"],
            item["packets"], last, item["last_error"] or "",
        ])
    return f'vrs-report-{data["shift_date"]}.csv', ("\ufeff" + stream.getvalue()).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "VRSMonitor/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.info("%s - %s", self.client_address[0], fmt % args)

    def authenticated(self) -> bool:
        user, password = os.getenv("RADAR_USER", ""), os.getenv("RADAR_PASSWORD", "")
        if not user:
            return True
        expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        if secrets.compare_digest(self.headers.get("Authorization", ""), expected):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="VRS Monitor"')
        self.end_headers()
        return False

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value: Any, status: int = 200) -> None:
        self.send_bytes(json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode()) if length else {}

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/health":
            self.send_json({"ok": True})
            return
        if not self.authenticated():
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/status":
                self.send_json(dashboard_payload(query.get("date", [None])[0]))
            elif parsed.path == "/api/events":
                cfg = load_config()
                start, end, _ = shift_bounds(query.get("date", [None])[0], cfg)
                receiver_id = int(query["receiver_id"][0]) if query.get("receiver_id") else None
                self.send_json(events_payload(start, end, receiver_id))
            elif parsed.path == "/api/notifications":
                limit = int(query.get("limit", [200])[0])
                self.send_json(notifications_payload(limit))
            elif parsed.path == "/api/settings":
                cfg = load_config()
                safe = dict(cfg)
                safe["vrs_password"] = "" if not cfg.get("vrs_password") else "********"
                safe["telegram_bot_token"] = "" if not cfg.get("telegram_bot_token") else "********"
                self.send_json(safe)
            elif parsed.path == "/api/report.csv":
                filename, content = report_csv(query.get("date", [None])[0])
                self.send_bytes(content, "text/csv; charset=utf-8", headers={
                    "Content-Disposition": f'attachment; filename="{filename}"'
                })
            else:
                self.serve_static(parsed.path)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_json({"error": str(exc)}, 400)
        except Exception as exc:
            logging.exception("Ошибка запроса")
            self.send_json({"error": str(exc)}, 500)

    def do_POST(self) -> None:
        if not self.authenticated():
            return
        try:
            if self.path == "/api/check":
                poll_now.set()
                self.send_json({"ok": True}, 202)
            elif self.path == "/api/military-report/test":
                self.send_json(send_military_report(load_config()))
            elif self.path == "/api/settings":
                incoming = self.read_json()
                old = load_config()
                for secret_key in ("vrs_password", "telegram_bot_token"):
                    if incoming.get(secret_key) == "********":
                        incoming[secret_key] = old.get(secret_key, "")
                cfg = save_config(incoming)
                safe = dict(cfg)
                for key in ("vrs_password", "telegram_bot_token"):
                    safe[key] = "" if not cfg.get(key) else "********"
                self.send_json(safe)
            else:
                self.send_json({"error": "Не найдено"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        target = (STATIC_DIR / relative).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            self.send_json({"error": "Не найдено"}, 404)
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        self.send_bytes(target.read_bytes(), content_type)


def main() -> None:
    parser = argparse.ArgumentParser(description="Монитор приёмников Virtual Radar Server")
    parser.add_argument("--host", help="Адрес прослушивания")
    parser.add_argument("--port", type=int, help="HTTP-порт")
    args = parser.parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler()],
    )
    init_db()
    if not CONFIG_PATH.exists():
        save_config({})
    cfg = load_config()
    thread = threading.Thread(target=monitor_loop, name="vrs-poller", daemon=True)
    thread.start()
    address = (args.host or str(cfg["listen_host"]), args.port or int(cfg["listen_port"]))
    server = ThreadingHTTPServer(address, Handler)
    logging.info("VRS Monitor запущен: http://%s:%s", *address)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        poll_now.set()
        server.server_close()


if __name__ == "__main__":
    main()
