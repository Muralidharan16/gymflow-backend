import sqlite3
import threading
import time
import logging
import json
from typing import Optional, List, Dict, Any
import requests

logger = logging.getLogger("bridge.offline")


class OfflineHandler:
    def __init__(self, db_path: str, server_url: str, bridge_token: str, device_id: str):
        self.db_path = db_path
        self.server_url = server_url.rstrip("/")
        self.bridge_token = bridge_token
        self.device_id = device_id
        self._lock = threading.RLock()
        self._ensure_db()

    def _ensure_db(self) -> None:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    fingerprint_id TEXT PRIMARY KEY,
                    member_id TEXT,
                    name TEXT,
                    valid_until TEXT,
                    template BLOB
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS attendance_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint_id TEXT,
                    timestamp TEXT,
                    payload TEXT,
                    processed INTEGER DEFAULT 0
                )
                """
            )
            conn.commit()

    def _get_conn(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def add_or_update_member(self, fingerprint_id: str, member_id: str, name: str, valid_until: Optional[str], template: Optional[bytes]) -> None:
        with self._lock, self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "REPLACE INTO members (fingerprint_id, member_id, name, valid_until, template) VALUES (?, ?, ?, ?, ?)",
                (fingerprint_id, member_id, name, valid_until, template),
            )
            conn.commit()

    def get_member_by_fingerprint(self, fingerprint_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT fingerprint_id, member_id, name, valid_until FROM members WHERE fingerprint_id=?", (fingerprint_id,))
            r = cur.fetchone()
            if not r:
                return None
            return {"fingerprint_id": r[0], "member_id": r[1], "name": r[2], "valid_until": r[3]}

    def log_attendance_cache(self, fingerprint_id: str, timestamp: str, payload: Optional[Dict[str, Any]] = None) -> None:
        payload_json = json.dumps(payload or {})
        with self._lock, self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("INSERT INTO attendance_cache (fingerprint_id, timestamp, payload) VALUES (?, ?, ?)", (fingerprint_id, timestamp, payload_json))
            conn.commit()

    def get_unprocessed_attendance(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id, fingerprint_id, timestamp, payload FROM attendance_cache WHERE processed=0 ORDER BY id ASC LIMIT ?", (limit,))
            rows = cur.fetchall()
            results = []
            for r in rows:
                results.append({"id": r[0], "fingerprint_id": r[1], "timestamp": r[2], "payload": json.loads(r[3] or "{}")})
            return results

    def mark_attendance_processed(self, record_id: int) -> None:
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE attendance_cache SET processed=1 WHERE id=?", (record_id,))
            conn.commit()

    def sync_attendance_to_server(self) -> Dict[str, Any]:
        url = f"{self.server_url}/access/verify"
        headers = {"X-Bridge-Token": self.bridge_token, "Content-Type": "application/json"}
        processed = 0
        errors = []
        for item in self.get_unprocessed_attendance(200):
            data = {"fingerprint_id": item["fingerprint_id"], "device_id": self.device_id}
            try:
                r = requests.post(url, json=data, headers=headers, timeout=6)
                if r.status_code == 200:
                    self.mark_attendance_processed(item["id"])
                    processed += 1
                else:
                    errors.append({"id": item["id"], "status_code": r.status_code, "text": r.text})
            except Exception as exc:
                errors.append({"id": item["id"], "error": str(exc)})
        return {"processed": processed, "errors": errors}

    def sync_members_from_cloud(self, sync_endpoint: str) -> Dict[str, Any]:
        url = f"{self.server_url.rstrip('/')}{sync_endpoint}"
        headers = {"X-Bridge-Token": self.bridge_token}
        try:
            r = requests.get(url, headers=headers, timeout=10)
            r.raise_for_status()
            data = r.json()
            count = 0
            for m in data.get("members", []):
                fid = m.get("fingerprint_id")
                if not fid:
                    continue
                self.add_or_update_member(fid, m.get("member_id"), m.get("name"), m.get("valid_until"), None)
                count += 1
            return {"ok": True, "count": count}
        except Exception as exc:
            logger.exception("Failed to sync members: %s", exc)
            return {"ok": False, "error": str(exc)}
