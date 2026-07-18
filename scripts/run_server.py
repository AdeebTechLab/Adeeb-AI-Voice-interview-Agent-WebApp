"""Reliable Adeeb server launcher.

This starts Uvicorn through its Python API instead of its command-line parser.
That avoids Windows PowerShell/CMD argument parsing differences across PCs.
"""
from __future__ import annotations

import os
import platform
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOG_FILE = LOG_DIR / "server.log"


class Tee:
    """Write console output to both the terminal and the server log."""

    def __init__(self, *streams: TextIO) -> None:
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            try:
                stream.write(data)
                stream.flush()
            except Exception:
                pass
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)() for stream in self.streams)


def _prepare_console() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main() -> int:
    _prepare_console()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    with LOG_FILE.open("a", encoding="utf-8", errors="replace", buffering=1) as log:
        sys.stdout = Tee(sys.__stdout__, log)  # type: ignore[assignment]
        sys.stderr = Tee(sys.__stderr__, log)  # type: ignore[assignment]

        print()
        print("=" * 68)
        print(f"Adeeb AI Meeting Agent server start: {datetime.now().isoformat(timespec='seconds')}")
        print(f"Project: {ROOT}")
        print(f"Python: {sys.version.split()[0]} ({platform.architecture()[0]})")
        print("=" * 68)

        try:
            import uvicorn
            from app import app

            print(f"Uvicorn: {getattr(uvicorn, '__version__', 'unknown')}")
            print("Listening on http://0.0.0.0:8000")
            print("Press Ctrl+C to stop Adeeb.")
            print()

            config = uvicorn.Config(
                app=app,
                host="0.0.0.0",
                port=8000,
                proxy_headers=True,
                forwarded_allow_ips="*",
                log_level="info",
                access_log=True,
            )
            server = uvicorn.Server(config)
            server.run()
            return 0 if server.started else 1
        except KeyboardInterrupt:
            print("\nAdeeb server stopped by the user.")
            return 0
        except Exception:
            print("\nFATAL: Adeeb could not start. Full error:")
            traceback.print_exc()
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
