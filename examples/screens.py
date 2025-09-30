#!/usr/bin/env python3
# Dual ST7735 dashboard (degzero/Python_ST7735) + Moonraker @ 127.0.0.1
# Turns BOTH screens RED if Klipper is in error/shutdown (from /printer/info or webhooks.state).

import time, math, requests, traceback
from dataclasses import dataclass
from typing import Optional, Dict, Any
from PIL import Image, ImageDraw, ImageFont, Image as PILImage
import os, configparser
import ST7735 as TFT
import Adafruit_GPIO.SPI as SPI

# ----------------- CONFIG -----------------
MOONRAKER_URL = "http://127.0.0.1:7125"
SCREENS = [
    {"name": "T0", "tool": "extruder"},    # left
    {"name": "T1", "tool": "extruder1"},   # right
]
POLL_HZ = 5
HTTP_TIMEOUT = 1.2
BAUD = 16_000_000

LAND_W, LAND_H = 160, 128          # draw in landscape
PORTRAIT_W, PORTRAIT_H = 128, 160  # driver expects portrait
OFF_X, OFF_Y = 2, 1
FLIP_LEFT_180 = True
VARS_PATH = "/home/pi/printer_data/config/save_variables.cfg"

# ----------------- DISPLAY SETUP -----------------
LEFT  = TFT.ST7735(25, rst=23, spi=SPI.SpiDev(0, 0, max_speed_hz=BAUD)); LEFT.begin()
RIGHT = TFT.ST7735(24, rst=18, spi=SPI.SpiDev(0, 1, max_speed_hz=BAUD)); RIGHT.begin()

def load_font():
    try:
        return {
            "xl": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20),
            "lg": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16),
            "md": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14),
            "sm": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12),
            "xs": ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10),
        }
    except Exception:
        f = ImageFont.load_default()
        return {"xl": f, "lg": f, "md": f, "sm": f, "xs": f}
FONTS = load_font()

_last_vars_mtime = None
_last_active_tool = 0  # default to left

def get_active_tool(path: str = VARS_PATH) -> int:
    """
    Reads [Variables] active_tool from Klipper's save_variables.cfg.
    Returns 0 for left (T0) or 1 for right (T1). Falls back to last known / 0.
    """
    global _last_vars_mtime, _last_active_tool

    try:
        st = os.stat(path)
        mtime = st.st_mtime
        # Only re-read if file changed
        if _last_vars_mtime is not None and mtime == _last_vars_mtime:
            return _last_active_tool

        cfg = configparser.ConfigParser()
        # Klipper writes standard INI; allow no-value quirks just in case
        cfg.read(path)

        val = cfg.getint("Variables", "active_tool", fallback=_last_active_tool)
        if val not in (0, 1):
            val = _last_active_tool

        _last_active_tool = val
        _last_vars_mtime = mtime
        return val

    except Exception:
        # If anything goes wrong, keep using the last known value
        return _last_active_tool

# ----------------- DATA MODEL -----------------
@dataclass
class ExtruderData:
    tool: str
    temp: float
    target: float
    fan: Optional[float]
    status: str
    progress: float

# ----------------- RENDER HELPERS -----------------
def _soft_offset(img: Image) -> Image:
    if OFF_X == 0 and OFF_Y == 0: return img
    pad = Image.new("RGB", (PORTRAIT_W + OFF_X, PORTRAIT_H + OFF_Y), (0, 0, 0))
    pad.paste(img, (OFF_X, OFF_Y))
    return pad.crop((0, 0, PORTRAIT_W, PORTRAIT_H))

def to_panel_frame(canvas_land: Image, flip_180: bool = False) -> Image:
    frame = canvas_land.transpose(PILImage.ROTATE_270)
    frame = _soft_offset(frame)
    if flip_180:
        frame = frame.transpose(PILImage.ROTATE_180)
    return frame

def label(draw, xy, text, font, fill=(255,255,255)):
    draw.text(xy, text, font=font, fill=fill)

def bar(draw: ImageDraw.ImageDraw, x, y, w, h, pct, col=(120,255,120), bg=(40,40,40)):
    pct = max(0, min(100, float(pct)))
    draw.rectangle((x, y, x+w, y+h), fill=bg, outline=(200,200,200))
    fillw = int(w * pct / 100.0)
    if fillw > 0:
        draw.rectangle((x, y, x+fillw, y+h), fill=col)

def render_panel(name: str, data: ExtruderData, active: bool = False) -> Image:
    img = Image.new("RGB", (LAND_W, LAND_H), (0, 0, 0))
    d = ImageDraw.Draw(img)

    # Header
    title_state = "ACTIVE TOOL" if active else "STANDBY"
    label(d, (6, 4), f"{name} {title_state}", FONTS["lg"])

    # Temps: current / target (same size as old "Temp:" line)
    label(d, (10, 36), f"{int(data.temp)}/{int(data.target)}°C", FONTS["xl"])

    # Progress bar + labels
    bar_x, bar_y, bar_w, bar_h = 10, 92, LAND_W - 20, 16
    bar(d, bar_x, bar_y, bar_w, bar_h, data.progress)
    label(d, (bar_x + 4,  bar_y - 14), "Progress", FONTS["xs"], fill=(180, 180, 180))
    label(d, (bar_x + bar_w - 30, bar_y - 14), f"{int(data.progress)}%", FONTS["xs"], fill=(180, 180, 180))

    # Border (BGR tuple—yellow on your ST7735)
    if active:
        border_col = (0, 255, 255)  # bright yellow on BGR panels
        thickness = 4
    else:
        border_col = (80, 80, 80)   # normal gray
        thickness = 1

    for i in range(thickness):
        d.rectangle((0 + i, 0 + i, LAND_W - 1 - i, LAND_H - 1 - i), outline=border_col)

    return img

# --------------- ERROR SCREENS ----------------
def render_error_screen(title: str, msg: str) -> Image:
    img = Image.new("RGB", (LAND_W, LAND_H), (180, 0, 0))
    d = ImageDraw.Draw(img)
    label(d, (6, 4), title, FONTS["lg"], fill=(255,255,255))

    # word-wrap
    words = (msg or "").replace("\n", " ").split()
    lines, line = [], ""
    for w in words:
        test = f"{line} {w}".strip()
        if d.textlength(test, font=FONTS["xs"]) > LAND_W - 12:
            lines.append(line); line = w
        else:
            line = test
    if line: lines.append(line)

    y = 26
    for ln in lines[:7]:
        label(d, (6, y), ln, FONTS["xs"], fill=(255,255,255))
        y += 12

    d.rectangle((0, 0, LAND_W-1, LAND_H-1), outline=(255,220,220))
    return img

def display_error_all(title: str, msg: str):
    frameL = to_panel_frame(render_error_screen(title, msg), flip_180=FLIP_LEFT_180)
    frameR = to_panel_frame(render_error_screen(title, msg), flip_180=False)
    LEFT.display(frameL)
    RIGHT.display(frameR)

# ----------------- MOONRAKER -----------------
class MoonrakerClient:
    def __init__(self, base_url: str, timeout: float = 1.2):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.timeout = timeout

    def _get(self, path: str) -> dict:
        r = self.s.get(f"{self.base}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def klippy_state(self) -> dict:
        """
        Returns dict with:
          state: 'ready' | 'printing' | 'shutdown' | 'error' | ...
          message: optional state_message from webhooks if present
        """
        # /printer/info is authoritative for Klippy state
        info = self._get("/printer/info")
        state = (info.get("result", {}).get("state") or "").lower()

        # Also peek at webhooks (sometimes has state_message for context)
        try:
            web = self._get("/printer/objects/query?webhooks")
            w = web.get("result", {}).get("status", {}).get("webhooks", {})
            w_state = (w.get("state") or "").lower()
            msg = w.get("state_message") or ""
            # prefer webhooks.state if it looks meaningful
            if w_state:
                state = w_state
        except Exception:
            msg = ""

        return {"state": state, "message": msg}

    def query_tool(self, tool: str) -> ExtruderData:
        # ask for print_stats/display_status and both extruders
        q = "print_stats&display_status&extruder&extruder1"
        js = self._get(f"/printer/objects/query?{q}")
        status: Dict[str, Any] = js["result"]["status"]

        ext = status.get(tool) or status.get("extruder", {})
        temp   = float(ext.get("temperature", 0.0))
        target = float(ext.get("target", 0.0))

        ps = status.get("print_stats", {})
        state = (ps.get("state") or "unknown").upper()

        prog = ps.get("progress")
        if prog is None:
            prog = status.get("display_status", {}).get("progress", 0.0)
        progress_pct = float(prog) * 100.0 if prog is not None else 0.0

        return ExtruderData(
            tool=tool.upper(),
            temp=temp,
            target=target,
            fan=None,
            status=state,
            progress=progress_pct,
        )

# ----------------- MAIN -----------------
def main():
    period = 1.0 / POLL_HZ
    client = MoonrakerClient(MOONRAKER_URL, timeout=HTTP_TIMEOUT)
    last_err = None

    display_error_all("INIT", "Waiting for Moonraker...")

    while True:
        t0 = time.monotonic()
        try:
            # 1) Check Klipper state first
            ks = client.klippy_state()
            kstate = ks.get("state", "")
            kmsg = ks.get("message", "")

            if kstate in ("error", "shutdown"):
                # Hard fail mode: both screens red with reason
                title = f"KLIPPER {kstate.upper()}"
                msg = kmsg or "Check printer console for details."
                if (title, msg) != last_err:
                    display_error_all(title, msg)
                    last_err = (title, msg)
            else:
                # 2) Normal dashboard: fetch both tools and render
                data = []
                for cfg in SCREENS:
                    data.append(client.query_tool(cfg["tool"]))


                active = get_active_tool(VARS_PATH)

                left_land  = render_panel(SCREENS[0]["name"], data[0], active=(active == 0))
                right_land = render_panel(SCREENS[1]["name"], data[1], active=(active == 1))

                LEFT.display( to_panel_frame(left_land,  flip_180=FLIP_LEFT_180) )
                RIGHT.display( to_panel_frame(right_land, flip_180=False) )

                last_err = None  # clear error

        except Exception as e:
            # Network/parse/etc → show generic RED error
            title = "MOONRAKER ERROR"
            msg = f"{e.__class__.__name__}: {str(e)}"
            tb_last = traceback.format_exc().strip().splitlines()[-1]
            if tb_last and tb_last not in msg:
                msg = f"{msg} | {tb_last}"
            if (title, msg) != last_err:
                display_error_all(title, msg[:220])
                last_err = (title, msg)

        # pace
        dt = time.monotonic() - t0
        if dt < period:
            time.sleep(period - dt)

if __name__ == "__main__":
    main()