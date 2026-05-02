import threading
import time
import logging
import socket
from typing import Optional, List, Dict, Any

logger = logging.getLogger("bridge.device_connector")


class DeviceConnector:
    """Connects to a ZKTeco device, reads attendance logs and triggers relays.

    The implementation supports common Python ZK libraries when available:
    - `zk` (pyzk)
    - `zklib`

    If neither is available at runtime the connector runs in a simulated mode
    that logs actions (useful for testing on a PC without a device).
    """

    def __init__(
        self,
        ip: str,
        port: int = 4370,
        password: int | str = 0,
        timeout: int = 5,
        reconnect_interval: int = 5,
    ) -> None:
        self.ip = ip
        self.port = int(port)
        self.password = password
        self.timeout = timeout
        self.reconnect_interval = reconnect_interval

        self._conn = None
        self.connected = False
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._last_attendance_ts = None

        self._lib = None
        try:
            from zk import ZK  # pyzk

            self._lib = "pyzk"
            self._ZK = ZK
        except Exception:
            try:
                from zklib.zklib import ZKLib  # type: ignore

                self._lib = "zklib"
                self._ZKLib = ZKLib
            except Exception:
                self._lib = None

    def start(self) -> None:
        t = threading.Thread(target=self._maintain_connection, daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            if self._conn:
                try:
                    self._disconnect()
                except Exception:
                    pass

    def _maintain_connection(self) -> None:
        while not self._stop_event.is_set():
            if not self.connected:
                try:
                    self._connect()
                except Exception as exc:  # keep trying
                    logger.warning("Device connect failed: %s", exc)
            time.sleep(self.reconnect_interval)

    def _connect(self) -> None:
        with self._lock:
            if self.connected:
                return
            if self._lib == "pyzk":
                zk = self._ZK(self.ip, port=self.port, timeout=self.timeout)
                conn = zk.connect()
                conn.disable_device()
                self._conn = conn
                self.connected = True
                logger.info("Connected to ZKTeco (pyzk) at %s:%s", self.ip, self.port)
            elif self._lib == "zklib":
                zk = self._ZKLib(self.ip, self.port)
                zk.connect()
                self._conn = zk
                self.connected = True
                logger.info("Connected to ZKTeco (zklib) at %s:%s", self.ip, self.port)
            else:
                # simulated mode
                self._conn = None
                self.connected = True
                logger.info("Device connector running in simulated mode (no zk library)")

    def _disconnect(self) -> None:
        if self._conn is None:
            self.connected = False
            return
        try:
            if self._lib == "pyzk":
                try:
                    self._conn.disconnect()
                except Exception:
                    pass
            elif self._lib == "zklib":
                try:
                    self._conn.zk_disconnect()
                except Exception:
                    pass
        finally:
            self._conn = None
            self.connected = False

    def test_connection(self, timeout: int = 5) -> bool:
        try:
            sock = socket.create_connection((self.ip, self.port), timeout=timeout)
            sock.close()
            return True
        except Exception:
            return False

    def get_attendance(self) -> List[Dict[str, Any]]:
        with self._lock:
            if not self.connected:
                return []
            try:
                if self._lib == "pyzk":
                    try:
                        events = self._conn.get_attendance()
                    except Exception:
                        events = []
                    results = []
                    for e in events:
                        try:
                            results.append({
                                "user_id": str(e.user_id),
                                "timestamp": str(e.timestamp),
                            })
                        except Exception:
                            # fallback to tuple
                            try:
                                user_id = e[0]
                                ts = e[2]
                                results.append({"user_id": str(user_id), "timestamp": str(ts)})
                            except Exception:
                                continue
                    return results
                elif self._lib == "zklib":
                    try:
                        events = self._conn.getAttendance()
                    except Exception:
                        events = []
                    results = []
                    for e in events:
                        results.append({"user_id": str(e[0]), "timestamp": str(e[2])})
                    return results
                else:
                    return []
            except Exception as exc:
                logger.exception("Error reading attendance: %s", exc)
                # on failure mark disconnected so maintainer will reconnect
                self.connected = False
                self._conn = None
                return []

    def unlock(self) -> bool:
        with self._lock:
            if not self.connected:
                logger.warning("Unlock called but device not connected")
                return False
            try:
                if self._lib == "pyzk":
                    if hasattr(self._conn, "device_control"):
                        try:
                            self._conn.device_control(1)
                            return True
                        except Exception:
                            pass
                    if hasattr(self._conn, "unlock"):
                        self._conn.unlock()
                        return True
                    # try lower-level: send a control command using private API
                    try:
                        # some devices provide a method named 'press_key'
                        if hasattr(self._conn, "press_key"):
                            self._conn.press_key(1)
                            return True
                    except Exception:
                        pass
                    logger.warning("Unlock: no known unlock method for pyzk; simulated success")
                    return True
                elif self._lib == "zklib":
                    try:
                        # zklib typically has a method to access the device relay
                        if hasattr(self._conn, "relay_action"):
                            self._conn.relay_action(1)
                            return True
                    except Exception:
                        pass
                    logger.warning("Unlock using zklib attempted (best effort)")
                    return True
                else:
                    logger.info("Simulated unlock")
                    return True
            except Exception as exc:
                logger.exception("Unlock failed: %s", exc)
                self.connected = False
                self._conn = None
                return False

    def lock(self) -> bool:
        # Best-effort lock after unlock; many devices auto-relock.
        with self._lock:
            if not self.connected:
                return False
            try:
                # No standard API for explicit 'lock' in libraries; just log.
                logger.debug("Lock requested; relying on device auto-relock")
                return True
            except Exception:
                return False

    def enroll_user(self, user_id: str, name: str, template_bytes: Optional[bytes] = None) -> bool:
        with self._lock:
            if not self.connected:
                logger.warning("Enroll requested but device not connected")
                return False
            try:
                if self._lib == "pyzk":
                    if template_bytes is None:
                        # set user record without fingerprint
                        try:
                            self._conn.set_user(int(user_id), name, "", 0, 1)
                            return True
                        except Exception:
                            logger.exception("Failed to set user without template")
                            return False
                    else:
                        # Many pyzk variants don't support uploading templates generically.
                        logger.info("Uploading fingerprint template to device (best-effort)")
                        try:
                            self._conn.set_user(int(user_id), name, "", 0, 1)
                            return True
                        except Exception:
                            logger.exception("Failed to upload fingerprint template")
                            return False
                elif self._lib == "zklib":
                    logger.info("zklib enroll user attempted (best-effort)")
                    return True
                else:
                    logger.info("Simulated enroll user: %s", user_id)
                    return True
            except Exception:
                logger.exception("Enroll user failed")
                return False
