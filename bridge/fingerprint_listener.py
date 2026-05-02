import threading
import time
import logging
import requests
from typing import Optional, Dict, Any

logger = logging.getLogger("bridge.fingerprint_listener")


class FingerprintListener:
    def __init__(
        self,
        device_connector,
        server_url: str,
        bridge_token: str,
        device_id: str,
        offline_handler,
        door_controller,
        poll_interval: float = 1.0,
    ) -> None:
        self.device = device_connector
        self.server_url = server_url.rstrip("/")
        self.bridge_token = bridge_token
        self.device_id = device_id
        self.offline = offline_handler
        self.door = door_controller
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if not self.device.connected:
                    time.sleep(self.poll_interval)
                    continue
                events = self.device.get_attendance()
                for ev in events:
                    try:
                        self._handle_event(ev)
                    except Exception:
                        logger.exception("Error handling event")
            except Exception:
                logger.exception("Listener crashed, continuing")
            time.sleep(self.poll_interval)

    def _handle_event(self, event: Dict[str, Any]) -> None:
        fid = event.get("user_id") or event.get("fingerprint_id")
        ts = event.get("timestamp")
        if not fid:
            logger.debug("Skipping event without fingerprint id: %s", event)
            return

        payload = {"fingerprint_id": fid, "device_id": self.device_id}
        headers = {"X-Bridge-Token": self.bridge_token, "Content-Type": "application/json"}
        url = f"{self.server_url}/access/verify"
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=5)
            if r.status_code == 200:
                data = r.json()
                allowed = bool(data.get("allowed"))
                if allowed:
                    logger.info("Access granted for %s", fid)
                    ok = self.door.trigger_unlock()
                    logger.debug("Trigger unlock result: %s", ok)
                else:
                    logger.info("Access denied for %s: %s", fid, data.get("reason"))
            else:
                logger.warning("Cloud returned non-200: %s", r.status_code)
                self.offline.log_attendance_cache(fid, ts or "", {"http_status": r.status_code, "text": r.text})
        except Exception as exc:
            logger.warning("Network error sending to cloud: %s; caching locally", exc)
            self.offline.log_attendance_cache(fid, ts or "", {"error": str(exc)})
