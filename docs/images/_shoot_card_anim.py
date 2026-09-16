"""Record README card GIFs: circuit heating + section cooling, light/dark, no narrow column."""

from __future__ import annotations

import base64
import http.server
import io
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CHROME = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")
BG = (42, 45, 51)
KINDS = ("circuit-heating", "section-cooling")
FPS = 10
SECONDS = 2.4
SCALE = 1.5
MAX_W = 1400


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        pass


def serve(root: Path, port: int) -> http.server.ThreadingHTTPServer:
    handler = lambda *a, **k: _QuietHandler(*a, directory=str(root), **k)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class Cdp:
    def __init__(self, ws):
        self.ws = ws
        self._n = 0

    def call(self, method: str, **params):
        self._n += 1
        mid = self._n
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result") or {}


def wait_ready(cdp: Cdp, timeout: float = 12.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = cdp.call(
            "Runtime.evaluate",
            expression="document.documentElement.dataset.ready === '1'",
            returnByValue=True,
        )
        if r.get("result", {}).get("value") is True:
            return
        time.sleep(0.05)
    raise TimeoutError("card preview did not become ready")


def sheet_clip(cdp: Cdp) -> dict:
    r = cdp.call(
        "Runtime.evaluate",
        expression="""(() => {
          const el = document.getElementById('sheet');
          const b = el.getBoundingClientRect();
          return {x: b.x, y: b.y, width: b.width, height: b.height};
        })()""",
        returnByValue=True,
    )
    box = r["result"]["value"]
    return {
        "x": max(0, box["x"]),
        "y": max(0, box["y"]),
        "width": box["width"],
        "height": box["height"],
        "scale": SCALE,
    }


def grab(cdp: Cdp, clip: dict) -> Image.Image:
    raw = cdp.call("Page.captureScreenshot", format="png", clip=clip, fromSurface=True)
    im = Image.open(io.BytesIO(base64.b64decode(raw["data"]))).convert("RGBA")
    # Flatten onto the README slate so GIF has no checkerboard.
    out = Image.new("RGB", im.size, BG)
    out.paste(im, mask=im.split()[-1])
    return out


def fit(frames: list[Image.Image]) -> list[Image.Image]:
    w, h = frames[0].size
    if w <= MAX_W:
        return frames
    nh = int(h * MAX_W / w)
    return [fr.resize((MAX_W, nh), Image.Resampling.LANCZOS) for fr in frames]


def save_anim(frames: list[Image.Image], dest: Path) -> None:
    frames = fit(frames)
    dest = dest.with_suffix(".webp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        dest,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=int(1000 / FPS),
        loop=0,
        quality=72,
        method=6,
        minimize_size=True,
    )
    print(f"{dest}  {dest.stat().st_size / 1024:.0f} KB  {len(frames)} frames {frames[0].size}")


def record(cdp: Cdp, kind: str, http_port: int) -> Path:
    url = f"http://127.0.0.1:{http_port}/docs/images/_card_readme.html?kind={kind}"
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    cdp.call(
        "Emulation.setEmulatedMedia",
        features=[{"name": "prefers-reduced-motion", "value": "no-preference"}],
    )
    cdp.call("Page.navigate", url=url)
    wait_ready(cdp)
    time.sleep(0.25)
    clip = sheet_clip(cdp)
    n = int(round(FPS * SECONDS))
    step = 1.0 / FPS
    frames: list[Image.Image] = []
    t0 = time.perf_counter()
    for i in range(n):
        target = t0 + i * step
        delay = target - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        frames.append(grab(cdp, clip))
    dest = HERE / f"card-{kind}.webp"
    save_anim(frames, dest)
    return dest


def main() -> None:
    try:
        import websocket
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "websocket-client"])
        import websocket

    http_port = _free_port()
    dbg_port = _free_port()
    httpd = serve(ROOT, http_port)
    profile = tempfile.mkdtemp(prefix="card-shot-")
    chrome = subprocess.Popen(
        [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--no-first-run",
            f"--remote-debugging-port={dbg_port}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--window-size=1200,900",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        ws_url = None
        for _ in range(50):
            try:
                import urllib.request

                with urllib.request.urlopen(f"http://127.0.0.1:{dbg_port}/json/list", timeout=1) as r:
                    pages = [p for p in json.loads(r.read()) if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
                    if pages:
                        ws_url = pages[0]["webSocketDebuggerUrl"]
                        break
            except OSError:
                time.sleep(0.1)
        if not ws_url:
            raise SystemExit("Chrome DevTools did not come up")
        ws = websocket.create_connection(ws_url, timeout=30)
        cdp = Cdp(ws)
        for kind in KINDS:
            record(cdp, kind, http_port)
        ws.close()
    finally:
        chrome.terminate()
        chrome.wait(timeout=5)
        httpd.shutdown()


if __name__ == "__main__":
    main()
