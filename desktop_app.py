import os
import subprocess
import sys
import time
import urllib.request

import webview

ROOT = os.path.dirname(os.path.abspath(__file__))
SERVER_URL = "http://127.0.0.1:8501"


def wait_for_server(url: str = SERVER_URL, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except Exception as exc:  # pragma: no cover - startup timing only
            last_error = exc
        time.sleep(0.25)
    raise RuntimeError(f"The app did not start in time: {last_error}")


def main() -> None:
    server = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "app.py")],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_server()
        window = webview.create_window(
            "Goodbuy",
            SERVER_URL,
            width=1400,
            height=980,
            resizable=True,
            min_size=(1000, 700),
        )
        webview.start()
        window.destroy()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


if __name__ == "__main__":
    main()
