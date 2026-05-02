import json
import os
import threading
import logging
import time
import sys
import signal
from pathlib import Path

from device_connector import DeviceConnector
from offline_handler import OfflineHandler
from fingerprint_listener import FingerprintListener
from door_controller import DoorController
from local_api import LocalAPI

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None

logger = logging.getLogger("bridge.main")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def load_config(path: str = "config.json") -> dict:
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class BridgeApp:
    def __init__(self, config_path: str = "config.json") -> None:
        self.base = Path(__file__).parent
        self.config = load_config(config_path)
        self.device = DeviceConnector(self.config.get("device_ip"), int(self.config.get("device_port", 4370)), self.config.get("device_password", 0))
        self.offline = OfflineHandler(self.config.get("cache_db", "bridge_data.db"), self.config.get("server_url"), self.config.get("bridge_token"), self.config.get("device_id"))
        self.door = DoorController(self.device, self.config.get("redis_url"), self.config.get("gym_id"))
        self.listener = FingerprintListener(self.device, self.config.get("server_url"), self.config.get("bridge_token"), self.config.get("device_id"), self.offline, self.door, poll_interval=float(self.config.get("poll_interval_seconds", 1)))
        self.local_api = LocalAPI(self.device, self.offline, port=int(self.config.get("local_http_port", 5000)))
        self._icon = None
        self._stop = False

    def start(self) -> None:
        self.device.start()
        self.door.start()
        self.listener.start()
        self.local_api.start()
        # start periodic offline sync thread
        t = threading.Thread(target=self._periodic_sync, daemon=True)
        t.start()
        if pystray:
            t2 = threading.Thread(target=self._start_tray, daemon=True)
            t2.start()

    def _periodic_sync(self) -> None:
        interval = 15 * 60
        while not self._stop:
            try:
                if self._is_online():
                    logger.info("Syncing offline attendance to cloud")
                    res = self.offline.sync_attendance_to_server()
                    logger.info("Sync result: %s", res)
                    logger.info("Syncing members from cloud")
                    res2 = self.offline.sync_members_from_cloud(self.config.get("members_sync_endpoint", "/devices/sync-members"))
                    logger.info("Members sync: %s", res2)
            except Exception:
                logger.exception("Periodic sync failed")
            time.sleep(interval)

    def _is_online(self) -> bool:
        try:
            import requests

            r = requests.get(self.config.get("server_url"), timeout=4)
            return r.status_code < 500
        except Exception:
            return False

    def _create_image(self, color: tuple) -> Image:
        img = Image.new("RGB", (16, 16), color)
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 15, 15), fill=color)
        return img

    def _start_tray(self) -> None:
        green = (0, 192, 0)
        red = (192, 0, 0)
        icon = pystray.Icon("gymflow-bridge")
        icon.icon = self._create_image(red)
        icon.title = "GymFlow Bridge"

        def update_icon():
            while not self._stop:
                try:
                    ok = self.device.connected and self._is_online()
                    icon.icon = self._create_image(green if ok else red)
                except Exception:
                    pass
                time.sleep(2)

        def on_quit(icon, item):
            self.stop()
            icon.stop()

        icon.menu = pystray.Menu(pystray.MenuItem("Quit", on_quit))
        threading.Thread(target=update_icon, daemon=True).start()
        icon.run()

    def stop(self) -> None:
        self._stop = True
        self.listener.stop()
        self.device.stop()
        self.door.stop()

    def enable_autostart(self) -> None:
        try:
            if sys.platform != "win32":
                return
            import winreg

            exe = sys.executable
            if getattr(sys, "frozen", False):
                target = exe
            else:
                target = f"{exe} {str(Path(__file__).absolute())}"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Windows\\CurrentVersion\\Run", 0, winreg.KEY_ALL_ACCESS)
            winreg.SetValueEx(key, "GymFlowBridge", 0, winreg.REG_SZ, target)
            winreg.CloseKey(key)
        except Exception:
            logger.exception("Failed to enable autostart")


def main():
    cfg_path = "config.json"
    if not os.path.exists(cfg_path):
        # try working dir
        cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    app = BridgeApp(cfg_path)
    if app.config.get("auto_start") and sys.platform == "win32":
        try:
            app.enable_autostart()
        except Exception:
            logger.exception("autostart failed")
    def _signal_handler(sig, frame):
        logger.info("Shutting down bridge")
        app.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    app.start()
    # keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    main()
