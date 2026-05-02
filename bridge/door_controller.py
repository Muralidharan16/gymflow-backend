import threading
import time
import json
import logging
from typing import Optional
import redis

logger = logging.getLogger("bridge.door")


class DoorController:
    def __init__(self, device_connector, redis_url: str, gym_id: str):
        self.device = device_connector
        self.redis_url = redis_url
        self.gym_id = gym_id
        self._stop_event = threading.Event()
        self._thread = None
        self._pubsub = None
        self._redis = None

    def start(self) -> None:
        t = threading.Thread(target=self._run_subscriber, daemon=True)
        t.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            if self._pubsub:
                self._pubsub.close()
        except Exception:
            pass

    def _connect_redis(self) -> Optional[redis.Redis]:
        try:
            r = redis.from_url(self.redis_url, decode_responses=True)
            # quick ping
            r.ping()
            return r
        except Exception:
            logger.warning("Redis not available at %s", self.redis_url)
            return None

    def _run_subscriber(self) -> None:
        channel_name = f"tenant:{self.gym_id}:door_control"
        while not self._stop_event.is_set():
            try:
                self._redis = self._connect_redis()
                if not self._redis:
                    time.sleep(5)
                    continue
                self._pubsub = self._redis.pubsub(ignore_subscribe_messages=True)
                self._pubsub.subscribe(channel_name)
                logger.info("Subscribed to Redis channel %s", channel_name)
                for message in self._pubsub.listen():
                    if self._stop_event.is_set():
                        break
                    try:
                        if message is None:
                            continue
                        data = message.get("data")
                        if isinstance(data, str):
                            payload = json.loads(data)
                        else:
                            payload = data
                        action = payload.get("action") if isinstance(payload, dict) else None
                        if action == "unlock" or payload == "unlock":
                            logger.info("Received unlock command via Redis: %s", payload)
                            self._do_unlock()
                    except Exception:
                        logger.exception("Error processing Redis message")
                # pubsub ended
            except Exception:
                logger.exception("Redis subscriber failed, reconnecting in 5s")
                time.sleep(5)

    def _do_unlock(self) -> None:
        try:
            ok = self.device.unlock()
            if not ok:
                logger.warning("Device unlock call reported failure")
            # keep door unlocked for 5 seconds then attempt to lock
            time.sleep(5)
            self.device.lock()
        except Exception:
            logger.exception("Unlock sequence failed")

    def trigger_unlock(self) -> bool:
        # programmatic trigger (e.g. from fingerprint listener)
        try:
            self._do_unlock()
            return True
        except Exception:
            return False
