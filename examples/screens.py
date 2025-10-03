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
ERROR_FLASH_PERIOD = 0.5  # seconds for on/off flash when Klipper is in error

LAND_W, LAND_H = 160, 128          # draw in landscape
PORTRAIT_W, PORTRAIT_H = 128, 160  # driver expects portrait
OFF_X, OFF_Y = 2, 1
FLIP_LEFT_180 = True
VARS_PATH = "/home/pi/printer_data/config/save_variables.cfg"
FAN_PHASE = 0.0      # radians
BASE_RPS  = 1.6      # base rotations/sec when fan at 100% (tweak to taste)
MIN_RPS   = 0.5      # minimal visible spin at very low PWM (prevents “stutter”)
BED_W = 500.0
BED_H = 500.0
MM_PER_REV = 6.743      # tweak to your hardware
VEL_EMA_ALPHA = 0.35   # smoothing for e_vel (0=no smooth, 1=heavy smooth)

EXTRUDER_PHASE = [0.0, 0.0]   # persistent phase per screen
EXTRUDER_VEL_EMA = [0.0, 0.0] # smoothed e_vel per screen
EXTRUDER_SPIN_DIR = -1.0
_last_tick_time = None

# M117 message tracking
_m117_message = None
_m117_timestamp_mono = 0.0  # when we saw it, in time.monotonic() units
_last_seen_gcode_time = None  # optional: track Moonraker's gcode time to avoid reprocessing history
M117_CLEAR_TIMEOUT = 60.0
_cached_frames = [None, None]
_last_frame_data = [None, None]
_last_m117_message = None
_last_m117_timestamp = 0.0
_cap_cache = [None, None]  # per-panel cached (text, Image) for the bottom cap area

def build_cap_image(text: str, width: int, font) -> Image:
    """Create a small image for the bottom cap text, centered, with panel bg."""
    pad_y = 2
    h = font.size + pad_y * 2
    img = Image.new("RGB", (width, h), DARK_BG)
    d = ImageDraw.Draw(img)
    tw = int(d.textlength(text, font=font))
    x = (width - tw) // 2
    y = pad_y - 1
    d.text((x, y), text, font=font, fill=TEXT_SECONDARY)
    return img


# -------- THEME (Light Mode, BGR tuples) --------
# -------- THEME (Neutral Gray Mode, BGR tuples) --------
# -------- THEME (Dark-Grey Mode, BGR tuples) --------
def hex_to_bgr(hexstr: str) -> tuple[int,int,int]:
    hs = hexstr.lstrip("#")
    r = int(hs[0:2], 16); g = int(hs[2:4], 16); b = int(hs[4:6], 16)
    return (b, g, r)

# Dark greys (not black)
DARK_BG          = hex_to_bgr("#14171C")   # panel background (charcoal)
DARK_SURFACE     = hex_to_bgr("#1B1F26")   # slightly lighter surface (if needed)
DIVIDER_COLOR    = hex_to_bgr("#3B3F45")   # thin line
TEXT_PRIMARY     = hex_to_bgr("#FFFFFF")   # white
TEXT_SECONDARY   = hex_to_bgr("#C8CBD0")   # soft grey

# Brand accent
BRAND_YELLOW     = hex_to_bgr("#FFD400")
BRAND_YELLOW_DK  = hex_to_bgr("#E6BE00")

# Badges
BADGE_ACTIVE_BG  = BRAND_YELLOW
BADGE_IDLE_BG    = hex_to_bgr("#4A4F57")
BADGE_TEXT       = hex_to_bgr("#000000")   # black text on yellow/grey

# Progress bar
BAR_BG           = hex_to_bgr("#2A2F36")
BAR_OUTLINE      = hex_to_bgr("#6A6F77")
BAR_FILL_LOW     = hex_to_bgr("#78E178")   # readable lime on dark
BAR_FILL_MID     = hex_to_bgr("#47C947")
BAR_FILL_HIGH    = BRAND_YELLOW_DK         # tends toward yellow near 100%

# Borders
BORDER_ACTIVE    = BRAND_YELLOW            # thick bright yellow (BGR)
BORDER_IDLE      = hex_to_bgr("#51565E")   # thin neutral

# ----------------- DISPLAY SETUP -----------------
LEFT  = TFT.ST7735(25, rst=23, spi=SPI.SpiDev(0, 0, max_speed_hz=BAUD)); LEFT.begin()
RIGHT = TFT.ST7735(24, rst=18, spi=SPI.SpiDev(0, 1, max_speed_hz=BAUD)); RIGHT.begin()

def load_font():
    candidates = [
        ("/usr/share/fonts/truetype/lato/Lato-Bold.ttf", "/usr/share/fonts/truetype/lato/Lato-Regular.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ]
    for bold_path, reg_path in candidates:
        try:
            return {
                "xl": ImageFont.truetype(bold_path, 24),  # bigger temps
                "lg": ImageFont.truetype(bold_path, 18),  # bigger header
                "md": ImageFont.truetype(reg_path, 16),
                "sm": ImageFont.truetype(reg_path, 13),
                "xs": ImageFont.truetype(reg_path, 11),   # badge text a bit larger
            }
        except Exception:
            continue
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
    x: float = 0.0
    y: float = 0.0
    vel: float = 0.0
    e: float = 0.0
    e_vel: float = 0.0
    filename: Optional[str] = None
    m117_message: Optional[str] = None
    m117_timestamp: float = 0.0 

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

def label_shadow(d, xy, text, font, fill=(255,255,255), shadow=(0,0,0)):
    x, y = xy
    d.text((x+1, y+1), text, font=font, fill=shadow)
    d.text((x, y), text, font=font, fill=fill)

def pill(d: ImageDraw.ImageDraw, x, y, text, font, pad_x=6, pad_y=2,
         fg=(0,0,0), bg=(200,200,200)):
    tw = int(d.textlength(text, font=font))
    th = font.size
    w, h = tw + pad_x*2, th + pad_y*2
    r = h // 2
    _rr(d, x, y, x+w, y+h, r=r, fill=bg, outline=None)
    d.text((x+pad_x, y+pad_y-1), text, font=font, fill=fg)
    return w, h

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

def draw_h_rule(d, x1, x2, y, color):
    d.line((x1, y, x2, y), fill=color)

def draw_v_rule(d, x, y1, y2, color):
    d.line((x, y1, x, y2), fill=color)

def draw_header_gradient(d, x, y, w, h, top, bottom, steps=6):
    # Old-Pillow friendly "gradient": a few horizontal bands
    for i in range(steps):
        t = i / max(1, steps - 1)
        # simple lerp in BGR space
        col = tuple(int(top[c] * (1 - t) + bottom[c] * t) for c in range(3))
        y0 = y + int(i * h / steps)
        y1 = y + int((i + 1) * h / steps)
        d.rectangle((x, y0, x + w, y1), fill=col)

def draw_badge(d, x, y, w, h, text, bg, fg, border):
    d.rectangle((x, y, x + w, y + h), fill=bg, outline=border)
    tw = int(d.textlength(text, font=FONTS["xs"]))
    d.text((x + (w - tw)//2, y + 2), text, font=FONTS["xs"], fill=fg)

def temps_color(temp, target):
    # color hint for the big temp text (BGR tuples)
    if target <= 0:
        return (255, 220, 200)  # warm white when idle (BGR)
    diff = target - temp
    if abs(diff) <= 2:
        return (120, 255, 120)  # greenish when at target
    if diff > 2:
        return (80, 180, 255)   # bluish when still cold
    return (60, 170, 255) if temp < target else (70, 200, 255)

def draw_corner_notches(d, w, h, color, size=6):
    # small L-shaped marks in each corner (modern instrument look)
    s = size
    # TL
    d.line((0, 0, s, 0), fill=color); d.line((0, 0, 0, s), fill=color)
    # TR
    d.line((w-1-s, 0, w-1, 0), fill=color); d.line((w-1, 0, w-1, s), fill=color)
    # BL
    d.line((0, h-1, s, h-1), fill=color); d.line((0, h-1-s, 0, h-1), fill=color)
    # BR
    d.line((w-1-s, h-1, w-1, h-1), fill=color); d.line((w-1, h-1-s, w-1, h-1), fill=color)

def bar_rounded(d: ImageDraw.ImageDraw, x, y, w, h, pct,
                fg=(120,255,120), bg=(40,40,40), outline=(200,200,200)):
    _rr(d, x, y, x+w, y+h, r=h//2, fill=bg, outline=outline)
    fillw = max(0, int(w * max(0, min(100.0, float(pct))) / 100.0))
    if fillw > 0:
        _rr(d, x, y, x+fillw, y+h, r=h//2, fill=fg, outline=None)
        gloss_h = max(2, h//3)
        _rr(d, x+2, y+2, x+fillw-2, y+2+gloss_h, r=gloss_h//2, fill=(80,80,80), outline=None)

# ---- Fan drawing / animation ----
def fan_blade_polygon(cx, cy, r, angle_rad, thickness=0.42):
    """
    Returns a triangle-like blade polygon rotated by angle_rad.
    thickness ~0.3..0.5 looks nice; 0.42 default.
    """
    # base vector (pointing right), then rotate; we build a tapered blade
    tip = (r, 0)
    base1 = (r * (0.15),  r * thickness)
    base2 = (r * (0.15), -r * thickness)

    ca, sa = math.cos(angle_rad), math.sin(angle_rad)
    def rot(p):
        x, y = p
        return (cx + ca*x - sa*y, cy + sa*x + ca*y)
    return [rot(tip), rot(base1), rot(base2)]

def draw_fan_icon(d: ImageDraw.ImageDraw, x, y, size, angle_rad, on, theme_fg=(230,230,230), accent=(255,255,0)):
    """
    Draw a 3-blade fan centered at (x,y) with given 'size' (diameter-ish).
    angle_rad is the current rotation angle. 'on' toggles color/accent.
    """
    r = size // 2
    cx, cy = x, y
    # outer ring
    ring_col = (90, 95, 105)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ring_col)

    # blades
    blade_fill = accent if on else theme_fg
    for i in range(3):
        ang = angle_rad + i * (2*math.pi/3)
        poly = fan_blade_polygon(cx, cy, int(r*0.88), ang, thickness=0.38)
        d.polygon(poly, fill=blade_fill)

    # hub
    hub_r = max(2, int(r*0.22))
    hub_col = (40, 45, 50) if on else (70, 75, 85)
    d.ellipse((cx - hub_r, cy - hub_r, cx + hub_r, cy + hub_r), fill=hub_col, outline=None)

def draw_xy_orbit(d: ImageDraw.ImageDraw, x, y, size, pos_x, pos_y,
                  bed_w=BED_W, bed_h=BED_H,
                  track=(58,62,70), ticks=(80,85,95), dot=(255,215,0)):
    """
    A circular widget that maps (pos_x, pos_y) from bed coords to polar inside a ring.
    (x, y) = top-left of the square area; size = width = height
    """
    r  = size // 2
    cx = x + r
    cy = y + r

    # ring
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=track)
    inner = max(1, int(r * 0.72))
    d.ellipse((cx - inner, cy - inner, cx + inner, cy + inner), outline=track)

    # ticks (NESW + diagonals)
    for ang in range(0, 360, 45):
        a = math.radians(ang)
        r0 = int(r * 0.88)
        r1 = r
        x0 = cx + int(r0 * math.cos(a)); y0 = cy + int(r0 * math.sin(a))
        x1 = cx + int(r1 * math.cos(a)); y1 = cy + int(r1 * math.sin(a))
        d.line((x0, y0, x1, y1), fill=ticks)

    # normalize XY to [0,1] around center, then project to polar
    nx = clamp(pos_x / max(1e-6, bed_w), 0.0, 1.0) - 0.5
    ny = clamp(pos_y / max(1e-6, bed_h), 0.0, 1.0) - 0.5

    # angle from center and radius as distance from center
    theta = math.atan2(ny, nx)                    # [-pi, pi]
    rad   = (nx*nx + ny*ny) ** 0.5                # 0..~0.707
    rad   = clamp(rad / 0.7071, 0.0, 1.0)         # normalize to 0..1

    pr = int((r - 3) * 0.88 * rad)
    px = cx + int(pr * math.cos(theta))
    py = cy + int(pr * math.sin(theta))

    # trail tick (small), then dot
    d.ellipse((px - 1, py - 1, px + 1, py + 1), fill=ticks)
    d.ellipse((px - 2, py - 2, px + 2, py + 2), fill=dot)

def draw_extruder_orbit(d: ImageDraw.ImageDraw, x, y, size, phase, e_vel,
                        track=(58,62,70), dot=(255,215,0), trail=(100,105,115)):
    r  = size // 2
    cx = x + r
    cy = y + r

    # ring
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=track)
    ir = int(r * 0.72)
    d.ellipse((cx - ir, cy - ir, cx + ir, cy + ir), outline=track)

    # Dot on the outer track from phase
    pr = int((r - 3) * 0.88)
    px = cx + int(pr * math.cos(phase))
    py = cy + int(pr * math.sin(phase))

    # trail (length scales with |e_vel|)
    sweep = max(0.0, min(1.0, abs(e_vel))) * (math.pi / 4)  # up to 45°
    if sweep > 0.0:
        a0 = phase - math.copysign(sweep, e_vel)  # trail behind current direction
        steps = 7
        for i in range(steps):
            t = i / max(1, steps - 1)
            aa = a0 + t * math.copysign(sweep, e_vel)
            tx = cx + int(pr * math.cos(aa))
            ty = cy + int(pr * math.sin(aa))
            d.ellipse((tx - 1, ty - 1, tx + 1, ty + 1), fill=trail)

    # Dot
    d.ellipse((px - 2, py - 2, px + 2, py + 2), fill=dot)

def _mix(c1, c2, t):
    # linear blend between two BGR tuples
    return tuple(int(c1[i] * (1 - t) + c2[i] * t) for i in range(3))

def _progress_fill_color(pct):
    # low→mid→high gradient
    t = max(0.0, min(100.0, float(pct))) / 100.0
    if t < 0.5:
        # 0..50%: LOW -> MID
        return _mix(BAR_FILL_LOW, BAR_FILL_MID, t / 0.5)
    else:
        # 50..100%: MID -> HIGH
        return _mix(BAR_FILL_MID, BAR_FILL_HIGH, (t - 0.5) / 0.5)

def _rr(d, x0, y0, x1, y1, r, fill=None, outline=None):
    """
    Back-compat rounded rectangle for old Pillow.
    Draws fill first, then a 1px outline if provided.
    """
    w = max(0, x1 - x0); h = max(0, y1 - y0)
    r = max(0, min(r, w//2, h//2))

    # --- FILLED SHAPE ---
    if fill is not None:
        # center rects
        d.rectangle((x0 + r, y0,     x1 - r, y1), fill=fill)
        d.rectangle((x0,     y0 + r, x1,     y1 - r), fill=fill)
        # corners (pieslices as filled quarters)
        if r > 0:
            d.pieslice((x0,     y0,     x0+2*r, y0+2*r), 180, 270, fill=fill)  # TL
            d.pieslice((x1-2*r, y0,     x1,     y0+2*r), 270,   0, fill=fill)  # TR
            d.pieslice((x0,     y1-2*r, x0+2*r, y1),       90, 180, fill=fill)  # BL
            d.pieslice((x1-2*r, y1-2*r, x1,     y1),        0,  90, fill=fill)  # BR

    # --- OUTLINE ---
    if outline is not None:
        # edges
        d.line((x0+r, y0,   x1-r, y0),   fill=outline)
        d.line((x0+r, y1,   x1-r, y1),   fill=outline)
        d.line((x0,   y0+r, x0,   y1-r), fill=outline)
        d.line((x1,   y0+r, x1,   y1-r), fill=outline)
        # corner arcs
        if r > 0:
            # use arc with same bbox as above
            d.arc((x0,     y0,     x0+2*r, y0+2*r), 180, 270, fill=outline)
            d.arc((x1-2*r, y0,     x1,     y0+2*r), 270,   0, fill=outline)
            d.arc((x0,     y1-2*r, x0+2*r, y1),       90, 180, fill=outline)
            d.arc((x1-2*r, y1-2*r, x1,     y1),        0,  90, fill=outline)

def draw_progress_bar_modern(d: ImageDraw.ImageDraw, x, y, w, h, pct):
    pct = max(0.0, min(100.0, float(pct)))

    # Track
    _rr(d, x, y, x + w, y + h, r=h//2, fill=BAR_BG, outline=BAR_OUTLINE)

    # Fill
    fw = int(w * pct / 100.0)
    if fw > 0:
        fill_col = _progress_fill_color(pct)
        _rr(d, x, y, x + fw, y + h, r=h//2, fill=fill_col, outline=None)

        # glossy top band (simple rectangular gloss inside the filled area)
        gloss_h = max(2, h // 3)
        _rr(d, x + 2, y + 2, x + max(2, fw - 2), y + 2 + gloss_h,
            r=gloss_h//2, fill=(70, 72, 78), outline=None)

    # thin inner highlight line
    d.line((x+2, y+1, x+w-2, y+1), fill=(90,90,90))

    # centered % text
    label = f"{int(pct)}%"
    tw = int(d.textlength(label, font=FONTS["xs"]))
    d.text((x + (w - tw)//2, y + (h - FONTS["xs"].size)//2 - 1),
           label, font=FONTS["xs"], fill=TEXT_SECONDARY)

def draw_modern_m117_message(d: ImageDraw.ImageDraw, message: str, timestamp: float):
    """Draw a clean, static M117 message display"""
    
    # Simple modern card layout
    msg_area_x = 12
    msg_area_y = 42
    msg_area_w = LAND_W - 24
    msg_area_h = 44
    
    # Clean card background - single color, no effects
    card_bg = hex_to_bgr("#2A2F36")
    _rr(d, msg_area_x, msg_area_y, msg_area_x + msg_area_w, msg_area_y + msg_area_h, 
        r=4, fill=card_bg, outline=BRAND_YELLOW)
    
    # Small message indicator dot
    dot_x = msg_area_x + 8
    dot_y = msg_area_y + 8
    d.ellipse((dot_x, dot_y, dot_x + 4, dot_y + 4), fill=BRAND_YELLOW)
    
    # Word wrap the message simply
    words = message.split()
    lines = []
    current_line = ""
    max_width = msg_area_w - 24  # Leave space for padding
    
    for word in words:
        test_line = f"{current_line} {word}".strip()
        if d.textlength(test_line, font=FONTS["sm"]) <= max_width:
            current_line = test_line
        else:
            if current_line:
                lines.append(current_line)
                current_line = word
            else:
                # Truncate long words
                current_line = word[:15] + "..." if len(word) > 15 else word
    
    if current_line:
        lines.append(current_line)
    
    # Limit to 2 lines for stability
    lines = lines[:2]
    
    # Draw text cleanly
    text_x = dot_x + 10
    line_height = FONTS["sm"].size + 3
    start_y = msg_area_y + (msg_area_h - len(lines) * line_height) // 2
    
    for i, line in enumerate(lines):
        y_pos = start_y + i * line_height
        d.text((text_x, y_pos), line, font=FONTS["sm"], fill=TEXT_PRIMARY)

def ellipsize_middle(d: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if not text:
        return ""
    if d.textlength(text, font=font) <= max_w:
        return text
    # Try to preserve extension
    root, ext = os.path.splitext(text)
    left, right = 0, 0
    ellipsis = "…"
    # If removing entire root still too long, just hard trim
    if d.textlength(ellipsis + ext, font=font) > max_w:
        # fallback: trim whole thing
        s = text
        while s and d.textlength(s + ellipsis, font=font) > max_w:
            s = s[:-1]
        return s + ellipsis
    # Middle-chop the root
    while left + right < len(root):
        candidate = root[:max(1, left)] + ellipsis + root[-max(1, right):] + ext
        if d.textlength(candidate, font=font) <= max_w:
            left += 1
            right += 1
        else:
            # back off one step
            left = max(1, left - 1)
            right = max(1, right - 1)
            break
    candidate = root[:max(1, left)] + ellipsis + root[-max(1, right):] + ext
    # final safety pass
    while candidate and d.textlength(candidate, font=font) > max_w:
        # shave a bit more from the middle
        if left > right and left > 1:
            left -= 1
        elif right > 1:
            right -= 1
        else:
            break
        candidate = root[:max(1, left)] + ellipsis + root[-max(1, right):] + ext
    return candidate

def render_panel(name: str, data: ExtruderData, active: bool = False, extruder_phase: float = 0.0) -> Image:
    img = Image.new("RGB", (LAND_W, LAND_H), DARK_BG)
    d = ImageDraw.Draw(img)

        # --- Taller header band with subtle gradient ---
    header_h = 28
    grad_top    = hex_to_bgr("#232831")
    grad_bottom = hex_to_bgr("#1A1F26")
    draw_header_gradient(d, 0, 0, LAND_W, header_h, grad_top, grad_bottom, steps=5)

    # Title (tool name)
    title_x, title_y = 6, 6
    d.text((title_x, title_y), name, font=FONTS["lg"], fill=TEXT_PRIMARY)

    # Badge: "ACTIVE" / "STANDBY" (draw this first so we know where the fan can go)
    title_state = "ACTIVE" if active else "STANDBY"
    badge_w, badge_h = 74, 22
    badge_x, badge_y = LAND_W - badge_w - 6, 4
    badge_bg = BADGE_ACTIVE_BG if active else BADGE_IDLE_BG
    badge_border = BRAND_YELLOW if active else DIVIDER_COLOR
    draw_badge(d, badge_x, badge_y, badge_w, badge_h, title_state, badge_bg, BADGE_TEXT, badge_border)

    # Fan icon in header, on the right (just left of the badge)
    try:
        speed = float(data.fan or 0.0)
    except Exception:
        speed = 0.0
    fan_on   = speed > 0.01
    fan_size = 18

    # measure text width
    title_width = d.textlength(name, font=FONTS["lg"])
    title_right = title_x + title_width

    # center fan horizontally between tool name and badge
    space_start = title_right + 4
    space_end   = badge_x - 4
    fan_cx      = (space_start + space_end) // 2
    fan_cy      = header_h // 2

    draw_fan_icon(d, fan_cx, fan_cy, fan_size, FAN_PHASE, fan_on,
                  theme_fg=TEXT_SECONDARY, accent=BRAND_YELLOW)

    # Divider under header
    draw_h_rule(d, 6, LAND_W - 6, header_h, DIVIDER_COLOR)

    # --- Big temps (always) ---
    temp_text = f"{int(data.temp)}/{int(data.target)}°C"
    temp_col = temps_color(data.temp, data.target)  # subtle state color
    tw = int(d.textlength(temp_text, font=FONTS["xl"]))
    d.text(((LAND_W - tw)//2, 48), temp_text, font=FONTS["xl"], fill=temp_col)

    if active:
        orbit_size   = 36
        orbit_margin = 6
        orbit_x = orbit_margin
        orbit_y = LAND_H - orbit_margin - orbit_size
        draw_extruder_orbit(d, orbit_x, orbit_y, orbit_size, extruder_phase, EXTRUDER_SPIN_DIR * data.e_vel)
        # Live XY readout to the right of the wheel
        xy_font = FONTS["sm"]
        txt_x = orbit_x + orbit_size + 8
        txt_y = orbit_y + 2
        d.text((txt_x, txt_y),        f"X {data.x:.1f}", font=xy_font, fill=TEXT_SECONDARY)
        d.text((txt_x, txt_y + xy_font.size + 2), f"Y {data.y:.1f}", font=xy_font, fill=TEXT_SECONDARY)

    # Corner notches (subtle accent)
    draw_corner_notches(d, LAND_W, LAND_H, DIVIDER_COLOR, size=6)

    # --- Outer border (BGR) — thicker when active ---
    border_col = BORDER_ACTIVE if active else BORDER_IDLE
    thickness = 4 if active else 1
    for i in range(thickness):
        d.rectangle((i, i, LAND_W - 1 - i, LAND_H - 1 - i), outline=border_col)
    
    if not active:
        bar_h = 12
        margin = 8
        bar_w = LAND_W - margin*2
        bar_x = margin
        bar_y = LAND_H - margin - bar_h

        # Prefer showing a static M117 message in the cap area if it's present and fresh
        cap_font = FONTS["xs"]
        max_w = bar_w
        show_cap_text = None
        if data.m117_message:
            age = time.monotonic() - (data.m117_timestamp or 0.0)
            if age < M117_CLEAR_TIMEOUT:
                show_cap_text = data.m117_message

        if show_cap_text is None and data.filename:
            show_cap_text = data.filename

        if show_cap_text:
            cap_text = ellipsize_middle(d, show_cap_text, cap_font, max_w)
            # Use cached cap image per panel so pixels remain stable between frames
            try:
                panel_index = 0 if name == SCREENS[0]["name"] else 1
            except Exception:
                panel_index = 0
            if _cap_cache[panel_index] is None or _cap_cache[panel_index][0] != cap_text:
                cap_img = build_cap_image(cap_text, max_w, cap_font)
                _cap_cache[panel_index] = (cap_text, cap_img)
            else:
                cap_img = _cap_cache[panel_index][1]

            # Paste cap image centered above the bar
            cap_x = bar_x + (bar_w - cap_img.width) // 2
            cap_y = bar_y - cap_img.height - 3
            img.paste(cap_img, (cap_x, cap_y))

        draw_progress_bar_modern(d, bar_x, bar_y, bar_w, bar_h, data.progress)

    return img

# --------------- ERROR SCREENS ----------------
def render_error_screen(title: str, msg: str, bg_color: tuple[int,int,int] = (0, 0, 180)) -> Image:
    """
    Render an error screen. bg_color lets the caller choose the background so
    the caller can implement flashing by alternating the color.
    """
    img = Image.new("RGB", (LAND_W, LAND_H), bg_color)
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

    # border color: slightly lighter than background for visibility
    try:
        border_col = (min(255, bg_color[0] + 75), min(255, bg_color[1] + 75), min(255, bg_color[2] + 75))
    except Exception:
        border_col = (255,220,220)
    d.rectangle((0, 0, LAND_W-1, LAND_H-1), outline=border_col)
    return img

def display_error_all(title: str, msg: str, bg_color: tuple[int,int,int] = (0, 0, 180)):
    """Display the same error on both panels. bg_color allows flashing by toggling.
    """
    frameL = to_panel_frame(render_error_screen(title, msg, bg_color=bg_color), flip_180=FLIP_LEFT_180)
    frameR = to_panel_frame(render_error_screen(title, msg, bg_color=bg_color), flip_180=False)
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

    def get_recent_gcode_responses(self, count: int = 10) -> list:
        """Get recent G-code responses to look for M117 messages"""
        try:
            response = self._get(f"/server/gcode_store?count={count}")
            gcode_store = response.get("result", {}).get("gcode_store", [])
            return gcode_store[-count:] if gcode_store else []
        except Exception:
            return []
    
    def query_tool(self, tool: str) -> ExtruderData:
        # ask for print_stats/display_status, both extruders, fan, and motion_report
        q = "print_stats&display_status&extruder&extruder1&fan&toolhead&motion_report"
        js = self._get(f"/printer/objects/query?{q}")
        status: Dict[str, Any] = js["result"]["status"]

        ext = status.get(tool) or status.get("extruder", {})
        temp   = float(ext.get("temperature", 0.0))
        target = float(ext.get("target", 0.0))

        ps = status.get("print_stats", {})
        state = (ps.get("state") or "unknown").upper()

        raw_name = ps.get("filename") or ""
        try:
            fname = os.path.basename(raw_name) if raw_name else ""
        except Exception:
            fname = raw_name or ""


        prog = ps.get("progress")
        if prog is None:
            prog = status.get("display_status", {}).get("progress", 0.0)
        progress_pct = float(prog) * 100.0 if prog is not None else 0.0

        fan_speed = float((status.get("fan") or {}).get("speed", 0.0))  # 0.0..1.0

        th = status.get("toolhead", {}) or {}
        pos = th.get("position") or th.get("gcode_position")  # [x, y, z] or [x, y, z, e]
        if isinstance(pos, (list, tuple)) and len(pos) >= 2:
            x = float(pos[0]); y = float(pos[1])
        else:
            # fallback to motion_report if toolhead missing
            mr = status.get("motion_report", {}) or {}
            mpos = mr.get("live_position") or [0.0, 0.0, 0.0, 0.0]
            x = float(mpos[0] if len(mpos) > 0 else 0.0)
            y = float(mpos[1] if len(mpos) > 1 else 0.0)

        vel = float(th.get("velocity", 0.0))

        mr = status.get("motion_report", {}) or {}
        mpos = mr.get("live_position") or [0.0, 0.0, 0.0, 0.0]
        x = float(mpos[0] if len(mpos) > 0 else 0.0)
        y = float(mpos[1] if len(mpos) > 1 else 0.0)
        e = float(mpos[3] if len(mpos) > 3 else 0.0)

        e_vel = float(mr.get("live_extruder_velocity", 0.0)) 

        return ExtruderData(
            tool=tool.upper(),
            temp=temp,
            target=target,
            fan=fan_speed,
            status=state,
            progress=progress_pct,
            x=x, y=y, vel=vel,
            e=e, e_vel=e_vel,
            filename=fname,
            m117_message=_m117_message,
            m117_timestamp=_m117_timestamp_mono,
        )

def needs_redraw(panel_index: int, data: ExtruderData, active: bool, extruder_phase: float) -> bool:
    """Check if panel needs to be redrawn based on data changes"""
    global _last_frame_data
    
    # Create a simple hash of the current state
    # Exclude filename from the redraw state — we reserve the cap area for M117
    current_state = (
        data.temp, data.target, data.fan, data.status, data.progress,
        data.x, data.y, data.vel, data.e_vel,
        data.m117_message, data.m117_timestamp, active, int(extruder_phase * 100)
    )
    
    # Check if anything changed
    if _last_frame_data[panel_index] != current_state:
        _last_frame_data[panel_index] = current_state
        return True
    
    return False

def process_m117_messages(gcode_responses: list) -> bool:
    global _m117_message, _m117_timestamp_mono, _last_m117_message, _last_m117_timestamp, _cached_frames, _last_seen_gcode_time, _cap_cache

    now_mono = time.monotonic()
    changed = False

    # 1) expire based on monotonic
    if _m117_message and (now_mono - _m117_timestamp_mono) > M117_CLEAR_TIMEOUT:
        _m117_message = None
        _last_m117_message = None
        _last_m117_timestamp = now_mono
        # don't clear cached frames here; let needs_redraw drive re-rendering
        return True

    # 2) ignore historical repeats by only processing strictly-new gcode times
    newest_resp_time = _last_seen_gcode_time
    for resp in gcode_responses:
        msg = (resp.get("message") or "").strip()
        rtime = resp.get("time")  # Moonraker server time (monotonic since start)
        if rtime is None:
            continue
        if _last_seen_gcode_time is not None and rtime <= _last_seen_gcode_time:
            continue  # already processed this or older

        if msg.upper().startswith("M117"):
            parts = msg.split(" ", 1)
            display_msg = parts[1].strip() if len(parts) > 1 else ""
            # Empty -> clear
            if not display_msg:
                if _m117_message is not None:
                    _m117_message = None
                    _last_m117_message = None
                    _last_m117_timestamp = now_mono
                    _m117_timestamp_mono = 0.0
                    changed = True
                    # clear cap cache so the UI will rebuild the fallback filename cap
                    _cap_cache = [None, None]
            else:
                # Only update if truly different from the currently displayed message
                if display_msg != _m117_message:
                    _m117_message = display_msg
                    _last_m117_message = display_msg
                    _m117_timestamp_mono = now_mono
                    _last_m117_timestamp = now_mono
                    changed = True
                    # clear cap cache so new cap image will be built
                    _cap_cache = [None, None]

        if newest_resp_time is None or rtime > newest_resp_time:
            newest_resp_time = rtime

    if newest_resp_time is not None:
        _last_seen_gcode_time = newest_resp_time

    # If changed, let the main loop's needs_redraw detect and re-render next cycle.
    if changed:
        pass
    return changed

# ----------------- MAIN -----------------
def main():
    global _cached_frames, _last_frame_data  # Add missing global declarations
    period = 1.0 / POLL_HZ
    client = MoonrakerClient(MOONRAKER_URL, timeout=HTTP_TIMEOUT)
    last_err = None
    gcode_check_counter = 0  # Only check G-code every few cycles

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
                # Flashing: alternate between bright red and darker red every ERROR_FLASH_PERIOD
                now = time.monotonic()
                phase = int(now / ERROR_FLASH_PERIOD) % 2
                # Colors are BGR tuples (like the rest of the file). Red is (0,0,255).
                bright = (0, 0, 255)
                dim    = (0, 0, 100)
                bg = bright if phase == 0 else dim
                # Only update the display when the title/msg OR bg phase changes to reduce redraws
                if (title, msg, phase) != last_err:
                    display_error_all(title, msg, bg_color=bg)
                    last_err = (title, msg, phase)
            else:
                # 2) Normal dashboard: check G-code responses for M117 messages
                gcode_check_counter += 1
                if gcode_check_counter >= 5:  # Check every 5 cycles (every 1 second at 5Hz) - more frequent
                    gcode_responses = client.get_recent_gcode_responses(20)
                    message_changed = process_m117_messages(gcode_responses)
                    gcode_check_counter = 0
                    
                    # Debug: Print M117 status
                    if _m117_message:
                        print(f"DEBUG: M117 message active: '{_m117_message}'")
                    
                    # Clear cached frames if message changed
                    if message_changed:
                        print(f"DEBUG: M117 message changed, clearing cache")
                        _cached_frames = [None, None]
                
                data = []
                for cfg in SCREENS:
                    data.append(client.query_tool(cfg["tool"]))
                
                active = get_active_tool(VARS_PATH)

                try:
                    fan_speed = float(data[active].fan or 0.0)  # use active panel!
                except Exception:
                    fan_speed = 0.0

                if fan_speed > 0.01:
                    rps = MIN_RPS + (BASE_RPS - MIN_RPS) * clamp(fan_speed, 0.0, 1.0)
                else:
                    rps = 0.0

                global FAN_PHASE
                FAN_PHASE = (FAN_PHASE + (2*math.pi) * rps * period) % (2*math.pi)

                # --- Integrate extruder angle from velocity (accurate speed + direction)
                global _last_tick_time
                now = time.monotonic()
                if _last_tick_time is None:
                    dt = 1.0 / POLL_HZ
                else:
                    dt = max(0.0, min(0.25, now - _last_tick_time))  # clamp to avoid huge jumps
                _last_tick_time = now

                for i, ed in enumerate(data):
                    # Smooth e_vel a bit (helps jitter at low speeds)
                    EXTRUDER_VEL_EMA[i] = (1.0 - VEL_EMA_ALPHA) * EXTRUDER_VEL_EMA[i] + VEL_EMA_ALPHA * float(ed.e_vel or 0.0)
                    e_vel_s = EXTRUDER_VEL_EMA[i]  # mm/s (negative on retract)

                    # Convert to angular speed (rad/s) using hardware mm-per-rev
                    # ω = 2π * (mm/s) / (mm/rev)
                    omega = EXTRUDER_SPIN_DIR * (2.0 * math.pi) * (e_vel_s / max(1e-6, MM_PER_REV))

                    # Advance persistent phase and wrap
                    EXTRUDER_PHASE[i] = (EXTRUDER_PHASE[i] + omega * dt) % (2.0 * math.pi)

                # Render and display both panels each cycle (keeps UI consistent)
                left_land = render_panel(SCREENS[0]["name"], data[0], active=(active == 0), extruder_phase=EXTRUDER_PHASE[0])
                right_land = render_panel(SCREENS[1]["name"], data[1], active=(active == 1), extruder_phase=EXTRUDER_PHASE[1])

                LEFT.display(to_panel_frame(left_land, flip_180=FLIP_LEFT_180))
                RIGHT.display(to_panel_frame(right_land, flip_180=False))

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