"""Background delivery of completed transcripts to a local Vectyr OS."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .logger import get_logger

logger = get_logger(__name__)


class VectyrSync:
    """Poll the transcript directory and reliably upload unseen files."""

    def __init__(self, transcripts_dir: Path, base_url: str, token: str = "", interval: float = 30.0):
        self.transcripts_dir = transcripts_dir
        self.endpoint = f"{base_url.rstrip('/')}/api/meetings/local-sync"
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        token_path = config_home / "meeting-notes" / "vectyr-sync-token"
        try:
            file_token = token_path.read_text().strip()
        except OSError:
            file_token = ""
        self.token = token or os.environ.get("VECTYR_MEETING_RECORDER_TOKEN", "") or file_token
        self.state_path = config_home / "meeting-notes" / "vectyr-sync.json"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="vectyr-meeting-sync", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _load_state(self) -> dict[str, str]:
        try:
            value = json.loads(self.state_path.read_text())
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def _save_state(self, state: dict[str, str]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2, sort_keys=True))
        os.chmod(temporary, 0o600)
        temporary.replace(self.state_path)

    @staticmethod
    def _version(path: Path) -> str:
        stat = path.stat()
        return f"{stat.st_size}:{stat.st_mtime_ns}"

    def _upload(self, path: Path) -> bool:
        body = json.dumps({"filename": path.name, "transcript": path.read_text()}).encode()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Vectyr-Linux-Meeting-Recorder/1.0",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                return 200 <= response.status < 300
        except HTTPError as error:
            detail = error.read(300).decode(errors="replace").strip()
            logger.warning("Vectyr sync rejected %s: HTTP %s %s", path.name, error.code, detail)
        except (URLError, TimeoutError, OSError) as error:
            logger.debug("Vectyr OS unavailable; %s remains queued: %s", path.name, error)
        return False

    def sync_once(self) -> None:
        state = self._load_state()
        changed = False
        for path in sorted(self.transcripts_dir.glob("*.txt")):
            try:
                version = self._version(path)
                if state.get(path.name) == version:
                    continue
                if self._upload(path):
                    state[path.name] = version
                    changed = True
                    logger.info("Synced transcript to Vectyr OS: %s", path.name)
            except OSError as error:
                logger.warning("Could not sync transcript %s: %s", path, error)
        if changed:
            self._save_state(state)

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sync_once()
            self._stop.wait(self.interval)
