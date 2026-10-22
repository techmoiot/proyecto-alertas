"""Notificaciones opcionales por Telegram (no bloquean el motor de alertas)."""

import json
import logging
import queue
import threading
import urllib.request

log = logging.getLogger("mkmonitor.notify")


class Notifier:
    def __init__(self, token: str = "", chat_id: str = ""):
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self._queue: queue.Queue[str] = queue.Queue(maxsize=200)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        self._ensure_thread()
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            log.warning("Cola de notificaciones llena; se descarta un mensaje")

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, name="notifier", daemon=True
                )
                self._thread.start()

    def _run(self) -> None:
        while True:
            text = self._queue.get()
            try:
                self._post(text)
            except Exception as exc:  # noqa: BLE001
                log.warning("No se pudo enviar a Telegram: %s", exc)

    def _post(self, text: str) -> None:
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        body = json.dumps({"chat_id": self.chat_id, "text": text}).encode()
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            resp.read()
