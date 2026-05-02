#!/usr/bin/env python3
"""Test harness for the GymFlow ZKTeco bridge modules.

This script starts a small local HTTP server that simulates the GymFlow cloud
endpoints used by the bridge (POST /access/verify and GET /devices/sync-members),
then exercises the bridge modules to validate behavior in both online and
offline modes.
"""

import os
import sys
import time
import json
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ensure bridge modules are importable
ROOT = os.path.dirname(__file__)
sys.path.insert(0, ROOT)

from device_connector import DeviceConnector
from offline_handler import OfflineHandler
from door_controller import DoorController
from fingerprint_listener import FingerprintListener
from local_api import LocalAPI


class TestHandler(BaseHTTPRequestHandler):
    def _set_headers(self, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode())
        except Exception:
            payload = {}
        if self.path == "/access/verify":
            fid = payload.get("fingerprint_id") or payload.get("user_id")
            if fid in ("1", "m1"):
                resp = {"allowed": True, "member_id": "m1", "member_name": "Test Member", "subscription_end": "2026-12-31"}
            else:
                resp = {"allowed": False, "reason": "no_subscription"}
            self._set_headers(200)
            self.wfile.write(json.dumps(resp).encode())
            return
        # unknown
        self._set_headers(404)
        self.wfile.write(json.dumps({"error": "not found"}).encode())

    def do_GET(self):
        if self.path == "/devices/sync-members":
            members = [
                {"fingerprint_id": "1", "member_id": "m1", "name": "Test Member", "valid_until": "2026-12-31"},
                {"fingerprint_id": "2", "member_id": "m2", "name": "Alice", "valid_until": "2026-12-31"},
            ]
            self._set_headers(200)
            self.wfile.write(json.dumps({"members": members}).encode())
            return
        self._set_headers(404)
        self.wfile.write(json.dumps({"error": "not found"}).encode())

    def log_message(self, format, *args):
        logging.info("TestHTTP: %s", format % args)


def run_test_server(port=8001):
    server = ThreadingHTTPServer(("127.0.0.1", port), TestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("Starting mock cloud server on http://127.0.0.1:8001")
    server = run_test_server(8001)
    time.sleep(0.2)

    # Use a clean sqlite file for tests
    db_path = os.path.join(ROOT, "test_bridge.db")
    try:
        os.remove(db_path)
    except Exception:
        pass

    # Instantiate components
    device = DeviceConnector("127.0.0.1", 9999)
    device.start()
    time.sleep(0.1)
    print("Device connected (simulated):", device.connected)

    offline = OfflineHandler(db_path, "http://127.0.0.1:8001", "test-token", "dev1")
    offline.add_or_update_member("1", "m1", "Test Member", "2026-12-31", None)
    member = offline.get_member_by_fingerprint("1")
    print("Lookup member by fingerprint:", member)

    offline.log_attendance_cache("1", "2026-05-02T00:00:00Z", {"note": "initial"})
    unproc = offline.get_unprocessed_attendance()
    print("Unprocessed attendance (before):", unproc)

    door = DoorController(device, "redis://localhost:6379/0", "gym1")
    # Avoid a blocking 5s sleep in tests; use a fast unlock hook
    def fast_unlock():
        device.unlock()
        device.lock()
    door._do_unlock = fast_unlock

    listener = FingerprintListener(device, "http://127.0.0.1:8001", "test-token", "dev1", offline, door, poll_interval=0.1)

    # Simulate a scan event handled by the listener
    print("Handling simulated event for fingerprint '1' (should be granted)")
    listener._handle_event({"user_id": "1", "timestamp": "2026-05-02T01:00:00Z"})
    print("Event handled")

    # Add another attendance to test syncing
    offline.log_attendance_cache("2", "2026-05-02T02:00:00Z", {})
    print("Unprocessed attendance (before sync):", offline.get_unprocessed_attendance())
    sync_result = offline.sync_attendance_to_server()
    print("Sync attendance result:", sync_result)
    print("Unprocessed attendance (after sync):", offline.get_unprocessed_attendance())

    # Test member sync from cloud
    ms = offline.sync_members_from_cloud("/devices/sync-members")
    print("Members sync result:", ms)

    # Test local API routes using TestClient (no uvicorn process required)
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:
        print("Skipping LocalAPI tests: fastapi TestClient not available:", exc)
    else:
        api = LocalAPI(device, offline, port=5002)
        client = TestClient(api.app)
        r = client.get("/status")
        print("LocalAPI /status ->", r.json())
        r2 = client.post("/sync")
        print("LocalAPI /sync ->", r2.json())

    # Shutdown mock server and exit
    server.shutdown()
    print("Test harness finished successfully")


if __name__ == "__main__":
    main()
