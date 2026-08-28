#!/usr/bin/env python3
"""calguard: alertas de escritorio para eventos próximos de Google Calendar.

Lee los eventos de las cuentas autenticadas en gogcli (`gog`) y dispara una
notificación de escritorio (notify-send) cuando un evento cruza uno de los
umbrales configurados. Solo usa la biblioteca estándar de Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

APP_NAME = "calguard"
GOG_TIMEOUT_SECONDS = 120
MAX_RESULTS = 500
CRITICAL_MINUTES = 15
DB_RETENTION_DAYS = 30
EXIT_OK = 0
EXIT_EMPTY = 3
EXIT_AUTH_REQUIRED = 4

log = logging.getLogger(APP_NAME)


def gog_bin() -> str:
    return os.environ.get("CALGUARD_GOG_BIN", "gog")


def default_config_path() -> Path:
    return Path(os.environ.get("CALGUARD_CONFIG", "~/.config/calguard/config.toml")).expanduser()


def default_db_path() -> Path:
    return Path(os.environ.get("CALGUARD_DB", "~/.local/state/calguard/calguard.db")).expanduser()


@dataclass
class Config:
    window_hours: int = 24
    poll_seconds: int = 60
    accounts: list[str] = field(default_factory=list)
    thresholds_minutes: list[int] = field(default_factory=lambda: [60, 15])
    only_keywords: list[str] = field(default_factory=list)
    skip_keywords: list[str] = field(default_factory=list)
    skip_all_day: bool = True
    notify: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        cfg = cls()
        path = path or default_config_path()
        if path.exists():
            data = tomllib.loads(path.read_text())
            unknown = set(data) - set(cfg.__dataclass_fields__)
            if unknown:
                log.warning("claves desconocidas en %s: %s", path, ", ".join(sorted(unknown)))
            for key in cfg.__dataclass_fields__:
                if key in data:
                    setattr(cfg, key, data[key])
        if not cfg.thresholds_minutes or any(t <= 0 for t in cfg.thresholds_minutes):
            raise ValueError("thresholds_minutes debe ser una lista de enteros positivos")
        if cfg.poll_seconds < 5:
            raise ValueError("poll_seconds debe ser >= 5")
        cfg.thresholds_minutes = sorted(set(cfg.thresholds_minutes), reverse=True)
        return cfg


@dataclass
class Event:
    account: str
    event_id: str
    summary: str
    start: datetime
    all_day: bool
    link: str = ""

    @property
    def dedup_key(self) -> str:
        return f"{self.account}|{self.event_id}|{self.start.isoformat()}"


def local_tz():
    return datetime.now().astimezone().tzinfo


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def first_conference_uri(raw: dict) -> str:
    conf = raw.get("conferenceData") or {}
    for entry in conf.get("entryPoints") or []:
        uri = entry.get("uri")
        if uri:
            return uri
    return ""


def parse_event(account: str, raw: dict) -> Event | None:
    event_id = raw.get("id")
    if not event_id:
        return None
    start_raw = raw.get("start") or {}
    all_day = False
    if start_raw.get("dateTime"):
        start = parse_datetime(start_raw["dateTime"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=local_tz())
    elif start_raw.get("date"):
        all_day = True
        start = datetime.fromisoformat(start_raw["date"]).replace(tzinfo=local_tz())
    else:
        return None
    link = raw.get("hangoutLink") or first_conference_uri(raw) or raw.get("htmlLink") or ""
    summary = (raw.get("summary") or "(sin título)").strip()
    return Event(account=account, event_id=event_id, summary=summary, start=start, all_day=all_day, link=link)


def matches_filters(summary: str, cfg: Config) -> bool:
    text = summary.lower()
    if cfg.only_keywords and not any(k.lower() in text for k in cfg.only_keywords):
        return False
    return not any(k.lower() in text for k in cfg.skip_keywords)


def pick_threshold(delta_minutes: float, thresholds: list[int]) -> int | None:
    crossed = [t for t in thresholds if delta_minutes <= t]
    return min(crossed) if crossed else None


def select_alerts(
    events: list[Event], now: datetime, cfg: Config, alerted: set[str]
) -> list[tuple[Event, int]]:
    out: list[tuple[Event, int]] = []
    for event in events:
        if event.all_day and cfg.skip_all_day:
            continue
        if not matches_filters(event.summary, cfg):
            continue
        delta_minutes = (event.start - now).total_seconds() / 60
        if delta_minutes < 0:
            continue
        threshold = pick_threshold(delta_minutes, cfg.thresholds_minutes)
        if threshold is None:
            continue
        if f"{event.dedup_key}|{threshold}" in alerted:
            continue
        out.append((event, threshold))
    return out


def run_gog(args: list[str]) -> subprocess.CompletedProcess | None:
    cmd = [gog_bin(), *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=GOG_TIMEOUT_SECONDS)
    except FileNotFoundError:
        log.error("no encontré el binario %r; instalá gogcli primero", gog_bin())
        return None
    except subprocess.TimeoutExpired:
        log.error("gog tardó más de %ss: %s", GOG_TIMEOUT_SECONDS, " ".join(args))
        return None


def parse_token_keys(keys: list[str]) -> list[str]:
    accounts: list[str] = []
    seen: set[str] = set()
    for key in keys:
        parts = key.split(":")
        # forma esperada: token:<cliente>:<email>
        if len(parts) >= 3 and parts[0] == "token":
            email = parts[-1]
            if email not in seen:
                seen.add(email)
                accounts.append(email)
    return accounts


def discover_accounts() -> list[str]:
    result = run_gog(["auth", "tokens", "list", "--json"])
    if result is None or result.returncode != EXIT_OK:
        if result is not None:
            log.error("gog auth tokens list falló: %s", result.stderr.strip()[:300])
        return []
    try:
        keys = json.loads(result.stdout).get("keys", [])
    except json.JSONDecodeError:
        log.error("no pude parsear la salida de 'gog auth tokens list'")
        return []
    return parse_token_keys(keys)


def extract_events(stdout: str) -> list[dict]:
    text = stdout.strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        log.error("salida JSON inválida de gog")
        return []
    if isinstance(obj, list):
        return [e for e in obj if isinstance(e, dict)]
    if isinstance(obj, dict):
        for key in ("items", "events", "results"):
            if isinstance(obj.get(key), list):
                return [e for e in obj[key] if isinstance(e, dict)]
    return []


def fetch_events(account: str, cfg: Config, now: datetime) -> list[Event]:
    end = now + timedelta(hours=cfg.window_hours)
    args = [
        "--readonly", "calendar", "events",
        "--all", "--all-pages", "--sort", "start",
        "--from", "now", "--to", end.isoformat(timespec="seconds"),
        "--max", str(MAX_RESULTS),
        "--json", "--results-only", "--no-input",
        "-a", account,
    ]
    result = run_gog(args)
    if result is None:
        return []
    if result.returncode == EXIT_EMPTY:
        return []
    if result.returncode == EXIT_AUTH_REQUIRED:
        log.warning("cuenta %s necesita re-auth: gog auth add %s --services calendar", account, account)
        return []
    if result.returncode != EXIT_OK:
        log.error("gog falló para %s (rc=%d): %s", account, result.returncode, result.stderr.strip()[:300])
        return []
    events = []
    for raw in extract_events(result.stdout):
        event = parse_event(account, raw)
        if event is not None:
            events.append(event)
    return events


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS alerted ("
        " key TEXT NOT NULL,"
        " threshold INTEGER NOT NULL,"
        " alerted_at TEXT NOT NULL,"
        " PRIMARY KEY (key, threshold))"
    )
    conn.commit()
    return conn


def load_alerted(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT key, threshold FROM alerted")
    return {f"{key}|{threshold}" for key, threshold in rows}


def mark_alerted(conn: sqlite3.Connection, key: str, threshold: int, now: datetime) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alerted (key, threshold, alerted_at) VALUES (?, ?, ?)",
        (key, threshold, now.isoformat(timespec="seconds")),
    )
    conn.commit()


def prune_alerted(conn: sqlite3.Connection, now: datetime) -> None:
    cutoff = now - timedelta(days=DB_RETENTION_DAYS)
    conn.execute("DELETE FROM alerted WHERE alerted_at < ?", (cutoff.isoformat(timespec="seconds"),))
    conn.commit()


def human_delta(minutes: int) -> str:
    if minutes >= 60:
        hours, rest = divmod(minutes, 60)
        return f"{hours} h {rest:02d} min" if rest else f"{hours} h"
    return f"{minutes} min"


def send_notification(event: Event, delta_minutes: float, cfg: Config, dry_run: bool) -> None:
    minutes = max(int(round(delta_minutes)), 0)
    title = f"{event.summary} — en {human_delta(minutes)}"
    body = f"{event.account} · {event.start.strftime('%H:%M')}"
    if event.link:
        body += f"\n{event.link}"
    if dry_run or not cfg.notify:
        log.info("[dry-run] %s | %s", title, body.replace("\n", " | "))
        return
    if not shutil.which("notify-send"):
        log.warning("notify-send no está disponible; evento perdido: %s (%s)", title, event.account)
        return
    urgency = "critical" if minutes <= CRITICAL_MINUTES else "normal"
    proc = subprocess.run(
        ["notify-send", "-a", APP_NAME, "-u", urgency, title, body],
        capture_output=True, text=True,
    )
    if proc.returncode != EXIT_OK:
        log.error("notify-send falló: %s", proc.stderr.strip()[:200])
    else:
        log.info("alerta emitida: %s (%s)", title, event.account)


def poll_once(cfg: Config, conn: sqlite3.Connection, dry_run: bool, now: datetime | None = None) -> int:
    now = now or datetime.now().astimezone()
    accounts = cfg.accounts or discover_accounts()
    if not accounts:
        log.warning("no hay cuentas: configurá 'accounts' o corré 'gog auth add <email> --services calendar'")
        return 0
    alerted = load_alerted(conn)
    sent = 0
    for account in accounts:
        events = fetch_events(account, cfg, now)
        log.debug("cuenta %s: %d eventos en la ventana", account, len(events))
        for event, threshold in select_alerts(events, now, cfg, alerted):
            delta_minutes = (event.start - now).total_seconds() / 60
            send_notification(event, delta_minutes, cfg, dry_run)
            mark_alerted(conn, event.dedup_key, threshold, now)
            alerted.add(f"{event.dedup_key}|{threshold}")
            sent += 1
    prune_alerted(conn, now)
    return sent


def run_daemon(cfg: Config, conn: sqlite3.Connection, dry_run: bool) -> None:
    log.info(
        "calguard iniciando: ventana=%dh poll=%ss umbrales=%s",
        cfg.window_hours, cfg.poll_seconds, cfg.thresholds_minutes,
    )
    while True:
        started = time.monotonic()
        try:
            poll_once(cfg, conn, dry_run)
        except Exception:
            log.exception("error en el ciclo de chequeo")
        elapsed = time.monotonic() - started
        time.sleep(max(cfg.poll_seconds - elapsed, 1))


def test_notify(cfg: Config) -> None:
    cfg.notify = True
    event = Event(
        account="prueba@example.com",
        event_id="test",
        summary="Evento de prueba de calguard",
        start=datetime.now().astimezone() + timedelta(minutes=10),
        all_day=False,
    )
    send_notification(event, 10.0, cfg, dry_run=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=APP_NAME, description="Alertas de escritorio para eventos de Google Calendar vía gogcli.")
    parser.add_argument("--config", type=Path, default=None, help="ruta al config TOML (default: ~/.config/calguard/config.toml)")
    parser.add_argument("--db", type=Path, default=None, help="ruta al SQLite de alertas emitidas")
    parser.add_argument("--dry-run", action="store_true", help="no dispara notificaciones; solo loguea")
    parser.add_argument("--debug", action="store_true", help="logging en nivel DEBUG")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="daemon: chequea cada poll_seconds (default)")
    sub.add_parser("once", help="un solo ciclo de chequeo")
    sub.add_parser("accounts", help="lista las cuentas que se van a monitorear")
    sub.add_parser("test-notify", help="dispara una notificación de prueba")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    try:
        cfg = Config.load(args.config)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        log.error("config inválida: %s", exc)
        return 1

    command = args.command or "run"
    if command == "accounts":
        accounts = cfg.accounts or discover_accounts()
        if not accounts:
            print("no hay cuentas configuradas ni autenticadas en gog")
            return 1
        for account in accounts:
            print(account)
        return 0
    if command == "test-notify":
        test_notify(cfg)
        return 0

    db_path = args.db or default_db_path()
    conn = open_db(db_path)
    try:
        if command == "once":
            poll_once(cfg, conn, args.dry_run)
            return 0
        run_daemon(cfg, conn, args.dry_run)
    except KeyboardInterrupt:
        log.info("calguard detenido")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
