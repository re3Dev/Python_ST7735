#!/usr/bin/env python3
# Dual ST7735 dashboard (degzero/Python_ST7735) + Moonraker @ 127.0.0.1
# - If ANY error occurs (timeout, bad JSON, missing fields, non-200), both screens go RED with the error.

import time, math, requests, traceback
from dataclasses import dataclass
from typing import Optional, Dict, Any
from PIL import Image, ImageDraw, ImageFont, Image as PILImage

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

# Display geometry & quirks
LAND_W, LAND_H = 160, 128          # draw in landscape
PORTRAIT_W, PORTRAIT_H = 128, 160  # driver expects portrait
OFF_X, OFF_Y = 2, 1                # adjust if you see the “L” border
FLIP_LEFT_180 = True               # your left panel orientation

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

# ----------------- DATA MODEL -----------------
@dataclass
class ExtruderData:
    tool: str
    temp: float
    target: float
    fan: Optional[float]
    status: str
    progress: float
    stale: bool = False

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

def render_panel(name: str, data: ExtruderData) -> Image:
    img = Image.new("RGB", (LAND_W, LAND_H), (0, 0, 0))
    d = ImageDraw.Draw(img)

    # Header: tool label + status
    label(d, (6, 4), f"{name}  {data.status}", FONTS["lg"])

    # Temps (text only)
    label(d, (10, 36), f"Temp: {int(data.temp)}°C",   FONTS["xl"])
    label(d, (10, 60), f"Target: {int(data.target)}°", FONTS["md"], fill=(180,180,180))

    # Progress
    bar_x, bar_y, bar_w, bar_h = 10, 92, LAND_W-20, 16
    bar(d, bar_x, bar_y, bar_w, bar_h, data.progress)
    label(d, (bar_x+4,  bar_y-14), "Progress",         FONTS["xs"], fill=(180,180,180))
    label(d, (bar_x+bar_w-30, bar_y-14), f"{int(data.progress)}%", FONTS["xs"], fill=(180,180,180))

    d.rectangle((0, 0, LAND_W-1, LAND_H-1), outline=(80,80,80))
    return img

# --------------- ERROR SCREEN ----------------
def render_error_screen(err_text: str) -> Image:
    """Solid red background with wrapped error text."""
    img = Image.new("RGB", (LAND_W, LAND_H), (180, 0, 0))
    d = ImageDraw.Draw(img)
    label(d, (6, 4), "ERROR", FONTS["lg"], fill=(255,255,255))

    # wrap message
    words = err_text.replace("\n", " ").split()
    lines, line = [], ""
    for w in words:
        test = f"{line} {w}".strip()
        if d.textlength(test, font=FONTS["xs"]) > LAND_W - 12:
            lines.append(line)
            line = w
        else:
            line = test
    if line: lines.append(line)

    y = 26
    for ln in lines[:7]:   # keep to screen
        label(d, (6, y), ln, FONTS["xs"], fill=(255,255,255))
        y += 12

    d.rectangle((0, 0, LAND_W-1, LAND_H-1), outline=(255,220,220))
    return img

def display_error_all(err_text: str):
    frame = to_panel_frame(render_error_screen(err_text), flip_180=FLIP_LEFT_180)
    LEFT.display(frame)
    RIGHT.display(to_panel_frame(render_error_screen(err_text), flip_180=False))

# ----------------- MOONRAKER -----------------
class MoonrakerClient:
    def __init__(self, base_url: str, timeout: float = 1.2):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.timeout = timeout

    def query(self, tool: str) -> ExtruderData:
        # Ask for a bunch of common objects; we'll pick what we need.
        objects = [
            "print_stats",
            "display_status",
            "fan",
            "extruder",
            "extruder1",
        ]
        q = "&".join(obj for obj in objects)
        url = f"{self.base}/printer/objects/query?{q}"

        r = self.s.get(url, timeout=self.timeout)            # may raise
        r.raise_for_status()
        js = r.json()                                        # may raise
        status: Dict[str, Any] = js["result"]["status"]      # may KeyError

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
            fan=None,                 # add fan parsing later if wanted
            status=state,
            progress=progress_pct,
            stale=False,
        )

# ----------------- MAIN -----------------
def main():
    period = 1.0 / POLL_HZ
    clients = [MoonrakerClient(MOONRAKER_URL, timeout=HTTP_TIMEOUT) for _ in SCREENS]
    last_err = None

    # seed with “INIT” screen
    display_error_all("Waiting for Moonraker...")

    while True:
        t0 = time.monotonic()
        try:
            # fetch both screens
            data = []
            for i, cfg in enumerate(SCREENS):
                data.append(clients[i].query(cfg["tool"]))

            # render normal panels
            left_land  = render_panel(SCREENS[0]["name"], data[0])
            right_land = render_panel(SCREENS[1]["name"], data[1])

            LEFT.display( to_panel_frame(left_land,  flip_180=FLIP_LEFT_180) )
            RIGHT.display( to_panel_frame(right_land, flip_180=False) )
            last_err = None

        except Exception as e:
            # Any error -> both screens red with message (truncated)
            msg = f"{e.__class__.__name__}: {str(e)}"
            # Optional: include last line of traceback for quick context
            tb_last = traceback.format_exc().strip().splitlines()[-1]
            if tb_last and tb_last not in msg:
                msg = f"{msg} | {tb_last}"
            # avoid spamming SPI if same error repeats
            if msg != last_err:
                display_error_all(msg[:220])
                last_err = msg

        # pace
        dt = time.monotonic() - t0
        if dt < period:
            time.sleep(period - dt)

if __name__ == "__main__":
    main()
