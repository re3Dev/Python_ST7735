#!/usr/bin/env python3
# Dual ST7735 dashboard pulling data from Moonraker
# - degzero/Python_ST7735 driver (confirmed working)
# - 0 = left (CE0), 1 = right (CE1)
# - Draw landscape (160x128), rotate to portrait (128x160) for driver
# - Left screen flipped 180 per your setup
# - Query Moonraker for: extruder/extruder1 temps/targets, print state, progress, fan

import time
import math
import socket
from dataclasses import dataclass
from typing import Optional, Dict, Any
import requests

from PIL import Image, ImageDraw, ImageFont, Image as PILImage
import ST7735 as TFT
import Adafruit_GPIO.SPI as SPI

# ----------------- CONFIG -----------------
# You can point both screens at the same Moonraker, or at two different ones.
# TOOL can be "extruder" for T0, "extruder1" for T1, etc. (match your Klipper tool names)
SCREENS = [
    {"name": "T0", "url": "http://192.168.1.50:7125", "tool": "extruder"},   # LEFT
    {"name": "T1", "url": "http://192.168.1.50:7125", "tool": "extruder1"},  # RIGHT
]
POLL_HZ = 5                 # how often to poll Moonraker
HTTP_TIMEOUT = 1.2          # seconds
# Small software offsets to remove the “L” border if needed
OFF_X, OFF_Y = 2, 1
# Flip the left screen 180°
FLIP_LEFT_180 = True
# SPI baud
BAUD = 16_000_000

# ----------------- DISPLAY SETUP -----------------
LAND_W, LAND_H = 160, 128          # draw in landscape
PORTRAIT_W, PORTRAIT_H = 128, 160  # driver expects portrait buffer

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
    fan: Optional[float]      # %
    status: str               # Klipper print_stats.state or derived
    progress: float           # %
    feed: Optional[float] = None
    flow: Optional[float] = None
    stale: bool = False       # True if data is old due to a fetch error

# ----------------- MOONRAKER CLIENT -----------------
class MoonrakerClient:
    def __init__(self, base_url: str, timeout: float = 1.2):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def query(self, tool: str) -> ExtruderData:
        # Build the query. We’ll ask for extruder + extruder1 regardless; we’ll pick what we need.
        # Also grab print_stats (state/progress) and fan.
        objects = [
            "print_stats",
            "display_status",
            "fan",
            "fan_generic fan",     # if you use fan_generic named "fan"
            "toolhead",
            "virtual_sdcard",
            "extruder",
            "extruder1",
        ]
        q = "&".join(obj.replace(" ", "=") if " " in obj else f"{obj}" for obj in objects)
        url = f"{self.base}/printer/objects/query?{q}"

        try:
            r = self.session.get(url, timeout=self.timeout)
            r.raise_for_status()
            res = r.json().get("result", {})
            objs: Dict[str, Any] = res.get("status", {})

            # Pick the tool block
            ext = objs.get(tool, {})
            # Fallback: if tool missing but "extruder" exists, use that
            if not ext and tool != "extruder":
                ext = objs.get("extruder", {})

            temp   = float(ext.get("temperature", float("nan")))
            target = float(ext.get("target", 0.0))

            # Progress/state
            ps = objs.get("print_stats", {})
            state = ps.get("state", "unknown").upper()
            # prefer print_stats.progress (0..1); fall back to display_status.progress (0..1)
            prog = ps.get("progress")
            if prog is None:
                ds = objs.get("display_status", {})
                prog = ds.get("progress", 0)
            progress_pct = float(prog) * 100.0 if prog is not None else 0.0

            # Fan: prefer "fan" object speed (0..1)
            fan_obj = objs.get("fan", {}) or objs.get("fan_generic fan", {})
            fan_pct = None
            if isinstance(fan_obj, dict) and "speed" in fan_obj and fan_obj["speed"] is not None:
                fan_pct = float(fan_obj["speed"]) * 100.0

            # Normalize status a bit
            nice = {
                "STANDBY": "IDLE",
                "READY": "IDLE",
                "PRINTING": "PRINTING",
                "PAUSED": "PAUSED",
                "CANCELLED": "CANCELLED",
                "COMPLETE": "COMPLETE",
            }
            status = nice.get(state, state)

            return ExtruderData(
                tool=tool.upper() if tool.startswith("extruder") else tool,
                temp=temp if not math.isnan(temp) else 0.0,
                target=target,
                fan=fan_pct,
                status=status,
                progress=progress_pct,
                stale=False,
            )

        except (requests.RequestException, ValueError, KeyError):
            # On error, surface a stale record so the UI keeps running.
            return ExtruderData(
                tool=tool.upper(),
                temp=0.0,
                target=0.0,
                fan=None,
                status="STALE",
                progress=0.0,
                stale=True,
            )

# ----------------- RENDERING -----------------
def _soft_offset(img: Image) -> Image:
    if OFF_X == 0 and OFF_Y == 0:
        return img
    pad = Image.new("RGB", (PORTRAIT_W + OFF_X, PORTRAIT_H + OFF_Y), (0, 0, 0))
    pad.paste(img, (OFF_X, OFF_Y))
    return pad.crop((0, 0, PORTRAIT_W, PORTRAIT_H))

def to_panel_frame(canvas_land: Image, flip_180: bool = False) -> Image:
    frame = canvas_land.transpose(PILImage.ROTATE_270)  # landscape -> portrait
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

    # Header: tool label (from config) + status
    # Name is "T0"/"T1" from SCREENS; data.status from Moonraker
    label(d, (6, 4), f"{name}  {data.status}", FONTS["lg"])

    # Temps (text only)
    label(d, (10, 36), f"Temp: {int(data.temp)}°C",   FONTS["xl"])
    label(d, (10, 60), f"Target: {int(data.target)}°", FONTS["md"], fill=(180,180,180))

    # Progress
    bar_x, bar_y, bar_w, bar_h = 10, 92, LAND_W-20, 16
    bar(d, bar_x, bar_y, bar_w, bar_h, data.progress)
    label(d, (bar_x+4, bar_y-14), "Progress", FONTS["xs"], fill=(180,180,180))
    label(d, (bar_x+bar_w-30, bar_y-14), f"{int(data.progress)}%", FONTS["xs"], fill=(180,180,180))

    # Footer stats (show fan if present)
    stats = []
    if data.fan is not None:
        stats.append(f"Fan {int(data.fan)}%")
    # (You can add feed/flow later)
    if stats:
        label(d, (10, LAND_H-18), "  ".join(stats), FONTS["xs"], fill=(200,200,200))

    # Stale watermark (if last request failed)
    if data.stale:
        label(d, (LAND_W-52, 4), "STALE", FONTS["xs"], fill=(255,180,120))

    d.rectangle((0, 0, LAND_W-1, LAND_H-1), outline=(80,80,80))
    return img

# ----------------- MAIN LOOP -----------------
def main():
    # Prepare clients per screen (so you can point them at different printers)
    clients = [MoonrakerClient(cfg["url"], timeout=HTTP_TIMEOUT) for cfg in SCREENS]

    # simple pacing
    period = 1.0 / POLL_HZ
    last_data: list[ExtruderData] = [
        ExtruderData(tool=cfg["tool"], temp=0, target=0, fan=None, status="INIT", progress=0)
        for cfg in SCREENS
    ]

    while True:
        t0 = time.monotonic()

        # Fetch each screen’s data
        for i, cfg in enumerate(SCREENS):
            try:
                data = clients[i].query(cfg["tool"])
                last_data[i] = data
            except Exception:
                # keep previous but mark stale
                ld = last_data[i]
                ld.stale = True
                last_data[i] = ld

        # Render
        left_land  = render_panel(SCREENS[0]["name"], last_data[0])
        right_land = render_panel(SCREENS[1]["name"], last_data[1])

        LEFT.display( to_panel_frame(left_land,  flip_180=FLIP_LEFT_180) )
        RIGHT.display( to_panel_frame(right_land, flip_180=False) )

        dt = time.monotonic() - t0
        if dt < period:
            time.sleep(period - dt)

if __name__ == "__main__":
    main()
