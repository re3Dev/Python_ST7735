#!/usr/bin/env python3
# Cute robot eyes on dual ST7735 (degzero/Python_ST7735) — synchronized version
# Left eye on CE0, Right eye on CE1
# Draws 160x128 landscape, then rotates to 128x160 portrait for this driver.

import time, math, random, signal
from PIL import Image, ImageDraw, Image as PILImage
import ST7735 as TFT
import Adafruit_GPIO.SPI as SPI

# ---------------- Display config (same style as your other scripts) ----------------
BAUD = 16_000_000
LAND_W, LAND_H = 160, 128          # draw in landscape
PORTRAIT_W, PORTRAIT_H = 128, 160  # driver expects portrait
OFF_X, OFF_Y = 2, 1                # software offsets to hide the “L” border
FLIP_LEFT_180 = True               # keep your left panel flipped

LEFT  = TFT.ST7735(25, rst=23, spi=SPI.SpiDev(0, 0, max_speed_hz=BAUD)); LEFT.begin()
RIGHT = TFT.ST7735(24, rst=18, spi=SPI.SpiDev(0, 1, max_speed_hz=BAUD)); RIGHT.begin()

def _soft_offset(img):
    if OFF_X == 0 and OFF_Y == 0:
        return img
    pad = Image.new("RGB", (PORTRAIT_W + OFF_X, PORTRAIT_H + OFF_Y), (0, 0, 0))
    pad.paste(img, (OFF_X, OFF_Y))
    return pad.crop((0, 0, PORTRAIT_W, PORTRAIT_H))

def to_panel_frame(canvas_land, flip_180=False):
    frame = canvas_land.transpose(PILImage.ROTATE_270)  # landscape -> portrait
    frame = _soft_offset(frame)
    if flip_180:
        frame = frame.transpose(PILImage.ROTATE_180)
    return frame

# ---------------- Eye parameters ----------------
BG = (15, 18, 22)          # panel background
SCLERA = (245, 245, 245)   # eye white
IRIS = (80, 180, 255)      # iris ring
PUPIL = (20, 20, 20)       # pupil
LID = (15, 18, 22)         # eyelid color (same as BG so it looks like blinking)

FPS = 200
DT = 1.0 / FPS

# Eye geometry (per eye canvas 160x128)
EYE_CX, EYE_CY = LAND_W // 2, LAND_H // 2
EYE_R = 56                 # sclera radius
IRIS_R = 26
PUPIL_R = 12
# Max pupil travel inside sclera (keep a margin so it never touches edge)
PUPIL_MAX = EYE_R - IRIS_R - 6

# Blink behavior (shared between both eyes for sync)
MIN_BLINK_SEC = 2.0
MAX_BLINK_SEC = 6.0
BLINK_TIME = 0.12          # time to close or open
BLINK_HOLD = 0.06          # time lids stay fully closed

running = True
def _sigint(_sig, _frm):
    global running
    running = False
signal.signal(signal.SIGINT, _sigint)

def draw_eye_frame(w, h, t, phase, blink_amt):
    """
    Draw a single eye (160x128) with a wandering pupil.
    - t: time (sec)
    - phase: horizontal phase (set same for both to sync)
    - blink_amt: 0=open .. 1=fully closed
    """
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)

    # Sclera
    d.ellipse((EYE_CX - EYE_R, EYE_CY - EYE_R, EYE_CX + EYE_R, EYE_CY + EYE_R), fill=SCLERA)

    # Pupil target path (gentle Lissajous)
    tx = math.sin(t * 0.8 + phase) * 0.8
    ty = math.sin(t * 1.1 + phase * 1.2) * 0.6
    px = EYE_CX + int(PUPIL_MAX * tx)
    py = EYE_CY + int(PUPIL_MAX * ty)

    # Iris ring
    d.ellipse((px - IRIS_R, py - IRIS_R, px + IRIS_R, py + IRIS_R), fill=IRIS)
    # Pupil
    d.ellipse((px - PUPIL_R, py - PUPIL_R, px + PUPIL_R, py + PUPIL_R), fill=PUPIL)

    # Subtle specular highlight
    d.ellipse((px - PUPIL_R//2 - 6, py - PUPIL_R//2 - 6, px - PUPIL_R//2, py - PUPIL_R//2), fill=(255,255,255))

    # Eyelids (blink): draw two rectangles that meet in the middle as blink_amt goes to 1
    if blink_amt > 0.0:
        cover = int((h // 2) * blink_amt)
        d.rectangle((0, 0, w, (h // 2) - cover), fill=LID)                  # upper lid
        d.rectangle((0, (h // 2) + cover, w, h), fill=LID)                  # lower lid

    # Outer outline
    d.ellipse((EYE_CX - EYE_R, EYE_CY - EYE_R, EYE_CX + EYE_R, EYE_CY + EYE_R), outline=(40, 40, 50), width=2)

    return img

def eye_anim_loop():
    # One shared blink schedule for perfect sync
    next_blink = time.monotonic() + random.uniform(MIN_BLINK_SEC, MAX_BLINK_SEC)
    blink_state = ("idle", 0.0, 0.0)  # (phase, start_time, amt)
    t0 = time.monotonic()
    swap = False  # alternate display order to balance any tiny SPI lag

    def update_blink(now, next_blink, state):
        phase, t_start, amt = state
        if phase == "idle":
            if now >= next_blink:
                return ("closing", now, 0.0), now + random.uniform(MIN_BLINK_SEC, MAX_BLINK_SEC)
            return state, next_blink
        elif phase == "closing":
            prog = min(1.0, (now - t_start) / BLINK_TIME)
            if prog >= 1.0:
                return ("closed", now, 1.0), next_blink
            return ("closing", t_start, prog), next_blink
        elif phase == "closed":
            if now - t_start >= BLINK_HOLD:
                return ("opening", now, 1.0), next_blink
            return state, next_blink
        elif phase == "opening":
            prog = 1.0 - min(1.0, (now - t_start) / BLINK_TIME)
            if prog <= 0.0:
                return ("idle", 0.0, 0.0), next_blink
            return ("opening", t_start, prog), next_blink
        return state, next_blink

    while running:
        now = time.monotonic()
        t = now - t0

        # Shared blink
        blink_state, next_blink = update_blink(now, next_blink, blink_state)
        blink_amt = blink_state[2]

        # Same pupil motion phase for both
        phase = 0.0
        left_land  = draw_eye_frame(LAND_W, LAND_H, t, phase=phase, blink_amt=blink_amt)
        right_land = draw_eye_frame(LAND_W, LAND_H, t, phase=phase, blink_amt=blink_amt)

        # Convert to panel frames
        left_frame  = to_panel_frame(left_land,  flip_180=FLIP_LEFT_180)
        right_frame = to_panel_frame(right_land, flip_180=False)

        # Alternate push order each frame to avoid a constant lead
        if swap:
            RIGHT.display(right_frame)
            LEFT.display(left_frame)
        else:
            LEFT.display(left_frame)
            RIGHT.display(right_frame)
        swap = not swap

        time.sleep(DT)

def main():
    try:
        eye_anim_loop()
    finally:
        # On exit, quick “surprised blink” and calm stare
        for amt in [0.0, 0.4, 0.8, 1.0, 0.6, 0.2, 0.0]:
            t = time.monotonic()
            left_land  = draw_eye_frame(LAND_W, LAND_H, t, phase=0.0, blink_amt=amt)
            right_land = draw_eye_frame(LAND_W, LAND_H, t, phase=0.0, blink_amt=amt)
            LEFT.display( to_panel_frame(left_land,  flip_180=FLIP_LEFT_180) )
            RIGHT.display(to_panel_frame(right_land, flip_180=False))
            time.sleep(0.05)
        t = time.monotonic()
        left_land  = draw_eye_frame(LAND_W, LAND_H, t, phase=0.0, blink_amt=0.0)
        right_land = draw_eye_frame(LAND_W, LAND_H, t, phase=0.0, blink_amt=0.0)
        LEFT.display( to_panel_frame(left_land,  flip_180=FLIP_LEFT_180) )
        RIGHT.display(to_panel_frame(right_land, flip_180=False))

if __name__ == "__main__":
    main()