from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import threading
import uvicorn
import logging
from typing import Optional

logger = logging.getLogger("bridge.local_api")


class ConnectRequest(BaseModel):
    device_ip: str
    device_port: Optional[int] = 4370


class SyncResponse(BaseModel):
    ok: bool
    details: dict


class LocalAPI:
    def __init__(self, device_connector, offline_handler, port: int = 5000):
        self.device = device_connector
        self.offline = offline_handler
        self.port = port
        self.app = FastAPI()
        self._server_thread = None
        self._configure_routes()

    def _configure_routes(self) -> None:
        app = self.app

        @app.post("/connect")
        def connect(req: ConnectRequest):
            ok = self.device.test_connection(timeout=3)
            if ok:
                return {"ok": True}
            raise HTTPException(status_code=400, detail="Unable to reach device at provided IP/port")

        @app.get("/status")
        def status():
            return {
                "device_connected": bool(self.device.connected),
            }

        @app.post("/sync", response_model=SyncResponse)
        def sync():
            res = self.offline.sync_members_from_cloud("/devices/sync-members")
            return SyncResponse(ok=res.get("ok", False), details=res)

    def start(self) -> None:
        def _run():
            uvicorn.run(self.app, host="127.0.0.1", port=self.port, log_level="info")

        t = threading.Thread(target=_run, daemon=True)
        t.start()
