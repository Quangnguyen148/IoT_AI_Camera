"""
state_store.py – Lưu trữ trạng thái thời gian thực và lịch sử log (SQLite + Memory)
Phục vụ đồng bộ dữ liệu giữa Gateway (T08), Webserver (T07) và Streamlit UI (T10).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

VN_TZ = timezone(timedelta(hours=7))
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "gateway_state.db"


def _now_iso() -> str:
    return datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")


class StateStore:
    """Thread-safe state & persistent audit log store."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self._lock = threading.Lock()
        self._db_path = db_path

        self._ai_active: bool = True
        self._mqtt_connected: bool = False
        self._mqtt_status_msg: str = "Chưa kết nối"
        self._latest_telemetry: dict = {
            "people_count": 0,
            "anomaly_detected": False,
            "llm_alert": "",
        }
        self._latest_gateway_json: dict = {}
        self._last_publish_iso: str = "Chưa có"
        self._total_published: int = 0

        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS telemetry_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    iso_time TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    people_count INTEGER NOT NULL,
                    anomaly_detected INTEGER NOT NULL,
                    llm_alert TEXT NOT NULL,
                    sent_to_cloud INTEGER NOT NULL,
                    raw_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS rpc_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    iso_time TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    device_name TEXT NOT NULL,
                    method TEXT NOT NULL,
                    params TEXT NOT NULL,
                    response_payload TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def set_ai_active(self, active: bool) -> None:
        with self._lock:
            self._ai_active = bool(active)

    def get_ai_active(self) -> bool:
        with self._lock:
            return self._ai_active

    def set_mqtt_status(self, connected: bool, message: str = "") -> None:
        with self._lock:
            self._mqtt_connected = bool(connected)
            self._mqtt_status_msg = message or ("Đã kết nối" if connected else "Mất kết nối")

    def record_telemetry(
        self,
        device_name: str,
        values: dict,
        gateway_envelope: dict,
        sent_to_cloud: bool,
    ) -> None:
        ts_ms = int(time.time() * 1000)
        iso_time = _now_iso()
        raw_str = json.dumps(gateway_envelope, ensure_ascii=False)

        with self._lock:
            self._latest_telemetry = dict(values)
            self._latest_gateway_json = dict(gateway_envelope)
            self._last_publish_iso = iso_time
            if sent_to_cloud:
                self._total_published += 1

            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO telemetry_logs (
                        ts_ms, iso_time, device_name, people_count,
                        anomaly_detected, llm_alert, sent_to_cloud, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ts_ms,
                        iso_time,
                        device_name,
                        int(values.get("people_count", 0)),
                        1 if values.get("anomaly_detected", False) else 0,
                        str(values.get("llm_alert", "")),
                        1 if sent_to_cloud else 0,
                        raw_str,
                    ),
                )
                conn.commit()

    def record_rpc(
        self,
        request_id: str,
        device_name: str,
        method: str,
        params: object,
        response_payload: object,
    ) -> None:
        ts_ms = int(time.time() * 1000)
        iso_time = _now_iso()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO rpc_logs (
                    ts_ms, iso_time, request_id, device_name,
                    method, params, response_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts_ms,
                    iso_time,
                    str(request_id),
                    device_name,
                    method,
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(response_payload, ensure_ascii=False),
                ),
            )
            conn.commit()

    def get_snapshot(self) -> dict:
        with self._lock:
            return {
                "ai_active": self._ai_active,
                "mqtt_connected": self._mqtt_connected,
                "mqtt_status_msg": self._mqtt_status_msg,
                "latest_telemetry": dict(self._latest_telemetry),
                "latest_gateway_json": dict(self._latest_gateway_json),
                "last_publish_iso": self._last_publish_iso,
                "total_published": self._total_published,
            }

    def get_recent_telemetry(self, limit: int = 100) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts_ms, iso_time, device_name, people_count,
                       anomaly_detected, llm_alert, sent_to_cloud, raw_json
                FROM telemetry_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "ts_ms": r["ts_ms"],
                    "iso_time": r["iso_time"],
                    "device_name": r["device_name"],
                    "people_count": r["people_count"],
                    "anomaly_detected": bool(r["anomaly_detected"]),
                    "llm_alert": r["llm_alert"],
                    "sent_to_cloud": bool(r["sent_to_cloud"]),
                    "raw_json": r["raw_json"],
                }
                for r in rows
            ]

    def get_recent_rpcs(self, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts_ms, iso_time, request_id, device_name,
                       method, params, response_payload
                FROM rpc_logs
                ORDER BY id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_logs(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM telemetry_logs")
            conn.execute("DELETE FROM rpc_logs")
            conn.commit()