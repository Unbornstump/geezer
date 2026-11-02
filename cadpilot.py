"""
cadwriter.py (v3)  -  drawing-presence automation for CADCraft (Windows)

Works on a CADCraft "project" from scratch, the way a person would:
  new drawing -> glance at Units / Drafting Settings -> create layers -> Save As (project name)
  -> sketch outlines -> add holes and centre lines -> offsets and dimensions -> review
  (zoom / pan / select) -> save -> next project (Ctrl+N, a brand-new drawing).

It stops the moment you touch the mouse/keyboard or another window comes forward, waits
IDLE_AFTER seconds, cancels any half-finished command, closes stray dialogs / side panels and
carries on at the exact step it was on.

pip install pyautogui pygetwindow pynput pillow
Windows + US keyboard assumed.
F8 = pause/resume   F9 = quit   mouse top-left = emergency stop

FIRST TIME:
  1. Maximise CADCraft on the MAIN monitor, close every side panel (Drafting Settings etc.).
  2. python cadwriter.py --calibrate     (7 quick points: the script reads where things are
                                          on YOUR screen; saved to cadwriter_calibration.json)
  3. python cadwriter.py --test          (fast PASS / FAIL self-check)
  4. python cadwriter.py

WHAT'S NEW IN v3 (fixes for dialogs / clicks)
  * CADCraft's dialogs (Drafting Settings, ...) are SIDE PANELS drawn inside the window, not Win32
    windows. v2 did not see them, "cancelled" with Escape (does not close them) and then clicked
    the drawing through the panel. v3 detects panels by screenshot, closes them with their X
    button (verified, retried, Escape fallback) and never clicks inside them (hover only), so no
    setting can be changed.
  * Every command is verified: the command line must visibly react after typing it, and the
    command prompt must react after the first click. Retries once; after repeated failures it
    PAUSES and tells you why (instead of typing / clicking blindly).
  * Clicks are more "real": a short wiggle on the spot and a longer button press.
  * --calibrate: drawing area, command line, menu bar, Layers row, panel X and the closed-panel
    reference are measured on your screen instead of guessed.
  * Project workflow with phases and layers (layer creation is verified; skipped if unsupported).
  * Never erases anything: every project is a new drawing (Ctrl+N) saved under its own name.
"""

import ctypes
import json
import logging
import math
import os
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

IS_WIN = sys.platform == "win32"
if IS_WIN:
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

import pyautogui
import pygetwindow as gw
from pynput import keyboard, mouse
try:
    from PIL import Image
except Exception:
    Image = None

# ───────────────────────── CONFIG ─────────────────────────
WORK_DIR      = Path(r"C:\work")
APP_EXE       = None                        # e.g. r"C:\Program Files\CADCraft\CADCraft.exe"; None = auto-find
APP_MATCH     = "cadcraft"                  # text in the window title
FILE_EXT      = ".dwg"
WORKBOOK      = WORK_DIR / ("session_" + time.strftime("%Y%m%d_%H%M") + FILE_EXT)   # fallback name
CALIB_FILE    = WORK_DIR / "cadwriter_calibration.json"
LOG_FILE      = WORK_DIR / "cadwriter.log"
DEBUG         = False

IDLE_AFTER    = 11
GAP_BETWEEN   = (8, 35)
LONG_BREAK_CHANCE = 0.09
LONG_BREAK    = (30, 110)

# typing
BASE_DELAY    = 0.40
TYPING_SPEED  = 1.0
WPM_MIN, WPM_MAX = 12, 20
TYPO_CHANCE   = 0.03
MICRO_STOP    = 0.08
KEY_DWELL     = (0.06, 0.15)

# layout, as fractions of the CADCraft window (l, r, t, b).  --calibrate overwrites these.
DRAW_AREA     = (0.21, 0.70, 0.21, 0.88)
COLS, ROWS    = 2, 2
PANEL_BOX     = (0.0, 0.19, 0.12, 0.95)    # the left strip a side panel (Drafting Settings...) covers
PANEL_CLOSE   = (0.191, 0.080)              # the panel's X button
PANEL_THRESH  = 0.25                        # fraction of the strip that must differ = "panel is open"
COMMAND_BOX   = (0.26, 0.64, 0.94, 1.0)     # the command line text at the bottom
LAYER_BOX     = (0.82, 0.995, 0.255, 0.305) # the "current layer" row of the Layers panel
PARK_SPOT     = (0.86, 0.45)                # empty spot used to park the mouse while taking screenshots
MENU_Y        = 0.092
MENU_X        = [0.017, 0.036, 0.059, 0.084, 0.115, 0.143, 0.169, 0.204, 0.244, 0.279, 0.309]
CENTER_BOX    = (0.36, 0.72, 0.22, 0.88)
X_BTN         = (24, 23)                    # title-bar X of a real Win32 dialog (px left of / below top-right)

def zone_from(da):
    return (da[0], da[1], da[2] + 0.06, da[3])
ZONE          = zone_from(DRAW_AREA)

USE_ALIASES   = True
UNDO_CHANCE   = 0.05
USE_LAYERS    = True
TOOLBAR_CLICK_CHANCE = 0.0                  # 0 = always the command line (reliable)
TOOL_ICONS    = {"line": (0.023, 0.281), "pline": (0.055, 0.281),
                 "circle": (0.087, 0.281), "rect": (0.119, 0.281)}

PAUSE_KEY, QUIT_KEY = "<f8>", "<f9>"
LAUNCH_TIMEOUT = 60
SPEED         = 1.0
NEW_DRAWING_IF_REUSED = True
PROBE_ON_START = True
MENU_PEEK     = True
VERIFY_DRAWING = True
CHANGE_MIN    = 0.004
DEAD_LIMIT    = 3                           # consecutive "nothing reacted" checks before pausing
DIALOG_CHANCE_WEIGHT = 1.0
CLOSE_BY_CLICK = True

# dialogs: opened by command; they are only LOOKED at (hover), never clicked inside.
DIALOGS = {
    "units":     {"alias": "un",   "w": 1.0},
    "drafting":  {"alias": "ds",   "w": 1.0},
    "dimstyle":  {"alias": "d",    "w": 0.8},
    "textstyle": {"alias": "st",   "w": 0.6},
    "options":   {"alias": "op",   "w": 0.6},
    "plot":      {"alias": "plot", "w": 0.8},
}

# ───────────────────────── LOGGING / SAFETY ─────────────────────────
_handlers = [logging.StreamHandler()]
try:
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    _handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
except Exception:
    pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S",
                    handlers=_handlers)
log = logging.info
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0

# ───────────────────────── WIN32 HELPERS ─────────────────────────
if IS_WIN:
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                    ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                    ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                    ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                    ("rcCaret", RECT)]

    user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]

def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def pid_of(h):
    try:
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return pid.value
    except Exception:
        return 0

def win_rect(h):
    if not IS_WIN or not h:
        return None
    try:
        rc = RECT()
        if user32.GetWindowRect(h, ctypes.byref(rc)):
            return rc.left, rc.top, rc.right, rc.bottom
    except Exception:
        pass
    return None

def menu_open(main=None):
    if not IS_WIN:
        return False
    try:
        fg = user32.GetForegroundWindow()
        if not fg:
            return False
        if main and pid_of(fg) != pid_of(main):
            return False
        tid = user32.GetWindowThreadProcessId(fg, None)
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if user32.GetGUIThreadInfo(tid, ctypes.byref(info)):
            return bool(info.flags & 0x4)
    except Exception:
        pass
    return False

# ───────────────────────── SHARED STATE ─────────────────────────
running = threading.Event()
stop = threading.Event()
running.set()

last_input = time.time()
action_started = 0.0
bot_ts = 0.0
USE_TIME_FILTER = False
listeners = []
PANEL_REF = None                # tiny picture of PANEL_BOX with NO panel open


class State:
    def __init__(self):
        self.fresh = True
        self.needs_heal = False
        self.rhythm = 1.0
        self.burst = 0
        self.recent = []
        self.in_dialog = False
        self.hwnd = 0
        self.hwnd_scan = 0.0
        self.chars = 0
        self.type_secs = 0.0
        self.drift = 1.0
        self.governor = 1.0
        self.cell_mult = 1.0
        self.saved = False          # has Save As been done for the current drawing?
        self.blank = True           # the current drawing is a blank one of ours (no Ctrl+N needed yet)
        self.project = None
        self.projects_done = 0
        self.slot = 0               # drawing slot used by px()
        self.view = {"zoom": 0, "zpos": None, "pan": (0, 0)}
        self.reused = False
        self.menu_ok = None
        self.menu_fail = 0
        self.checks = 0
        self.dead = 0               # blank-canvas checks in a row
        self.cmd_dead = 0           # commands the command line did not react to, in a row
        self.click_dead = 0         # clicks the prompt did not react to, in a row
        self.cmd_snap = None
        self.layers_ok = None       # None = unknown, True / False after the first try
        self.dialog_kind = None     # "window" | "panel" | None


S = State()


class Interrupted(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def halt(lines):
    """Pause with an explanation (F8 resumes, F9 quits)."""
    log("=" * 60)
    for l in lines:
        log(l)
    log("Fix it, then press F8 to carry on (or F9 to quit).")
    log("=" * 60)
    running.clear()
    raise Interrupted("halt")


# ───────────────────────── INPUT PRIMITIVES ─────────────────────────
def mark():
    global bot_ts
    bot_ts = time.time()

def tap(k):
    mark()
    pyautogui.keyDown(k)
    try:
        time.sleep(random.uniform(*KEY_DWELL))
    finally:
        pyautogui.keyUp(k)

def kb_press(k, n=1, iv=0):
    for i in range(n):
        tap(k)
        if i < n - 1 and iv:
            time.sleep(iv)

def kb_hotkey(*k):
    mark()
    held = []
    try:
        for key in k:
            pyautogui.keyDown(key)
            held.append(key)
            time.sleep(random.uniform(0.03, 0.08))
        time.sleep(random.uniform(0.04, 0.09))
    finally:
        for key in reversed(held):
            pyautogui.keyUp(key)
            time.sleep(0.02)

def ms_down(b="left"):
    mark()
    pyautogui.mouseDown(button=b)

def ms_up(b="left"):
    mark()
    pyautogui.mouseUp(button=b)

def ms_scroll(n):
    mark()
    pyautogui.scroll(n)

def ms_move(x, y):
    mark()
    sw, sh = pyautogui.size()
    pyautogui.moveTo(min(max(int(x), 3), sw - 4), min(max(int(y), 3), sh - 4))

SHIFT_BASE = {'!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6', '&': '7', '*': '8',
              '(': '9', ')': '0', '_': '-', '+': '=', '{': '[', '}': ']', '|': '\\',
              ':': ';', '"': "'", '<': ',', '>': '.', '?': '/', '~': '`'}

def press_char(ch):
    mark()
    shift, key = False, ch
    if ch.isalpha() and ch.isupper():
        shift, key = True, ch.lower()
    elif ch in SHIFT_BASE:
        shift, key = True, SHIFT_BASE[ch]
    if key == " ":
        key = "space"
    if len(key) != 1 and key != "space":
        pyautogui.write(ch)
        return
    try:
        if shift:
            pyautogui.keyDown("shift")
            time.sleep(random.uniform(0.02, 0.06))
        pyautogui.keyDown(key)
        time.sleep(random.uniform(*KEY_DWELL))
        pyautogui.keyUp(key)
        if shift:
            time.sleep(random.uniform(0.01, 0.04))
    finally:
        if shift:
            pyautogui.keyUp("shift")


# ───────────────────────── HUMAN ACTIVITY ─────────────────────────
def _human():
    global last_input
    if USE_TIME_FILTER and time.time() - bot_ts < 0.35:
        return
    last_input = time.time()

def on_press(key, injected=False):
    if not injected:
        _human()

def on_move(x, y, injected=False):
    if not injected:
        _human()

def on_click(x, y, button, pressed, injected=False):
    if not injected:
        _human()

def on_scroll(x, y, dx, dy, injected=False):
    if not injected:
        _human()

def toggle():
    if running.is_set():
        running.clear()
        log("paused")
    else:
        running.set()
        log("resumed")

def quit_now():
    log("quit requested")
    stop.set()

def start_listeners():
    listeners.extend([
        keyboard.GlobalHotKeys({PAUSE_KEY: toggle, QUIT_KEY: quit_now}),
        keyboard.Listener(on_press=on_press),
        mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll),
    ])
    for l in listeners:
        l.daemon = True
        l.start()

def self_test_injection():
    global USE_TIME_FILTER
    before = last_input
    pyautogui.moveRel(1, 0)
    pyautogui.moveRel(-1, 0)
    time.sleep(0.45)
    if last_input != before:
        USE_TIME_FILTER = True
        log("using timing filter for injected input")


# ───────────────────────── WINDOW / DIALOG AWARENESS ─────────────────────────
def active_title():
    try:
        w = gw.getActiveWindow()
        return w.title if w else ""
    except Exception:
        return ""

def in_app(t=None):
    t = active_title() if t is None else t
    return APP_MATCH.lower() in t.lower()

def app_windows():
    try:
        return [w for w in gw.getWindowsWithTitle(APP_MATCH)
                if w.title and APP_MATCH.lower() in w.title.lower()]
    except Exception:
        return []

def main_hwnd():
    if not IS_WIN:
        return 0
    h = S.hwnd
    try:
        if h and user32.IsWindow(h) and user32.IsWindowVisible(h):
            return h
    except Exception:
        pass
    if time.time() - S.hwnd_scan < 1.0:
        return 0
    S.hwnd_scan = time.time()
    best, area = 0, -1
    for w in app_windows():
        try:
            a = w.width * w.height
            if a > area:
                best, area = int(w._hWnd), a
        except Exception:
            pass
    S.hwnd = best
    return best

def dialog_hwnd():
    """Handle of a REAL dialog / popup window of CADCraft open right now (or the main window if a menu is open)."""
    if not IS_WIN:
        return 0
    main = main_hwnd()
    if not main:
        return 0
    try:
        pop = user32.GetWindow(main, 6)
        if pop and pop != main and user32.IsWindowVisible(pop):
            return pop
        fg = user32.GetForegroundWindow()
        if fg and fg != main and user32.IsWindowVisible(fg) and pid_of(fg) == pid_of(main):
            return fg
        if menu_open(main):
            return main
    except Exception:
        pass
    return 0

def app_ok():
    if S.in_dialog:
        return bool(dialog_hwnd()) or in_app()
    return in_app() and not dialog_hwnd()

def force_activate(w):
    try:
        if w.isMinimized:
            w.restore()
        kb_press("alt")
        w.activate()
    except Exception:
        pass

def activate_main():
    h = main_hwnd()
    if IS_WIN and h:
        try:
            kb_press("alt")
            user32.SetForegroundWindow(h)
            return
        except Exception:
            pass
    wins = app_windows()
    if wins:
        force_activate(wins[0])

def bring_back():
    if dialog_hwnd():
        log("a dialog is open - closing it first")
        close_dialogs(accept=False)
    if app_ok():
        return True
    wins = app_windows()
    if not wins:
        return open_app()
    log("bringing existing CADCraft window forward")
    for _ in range(3):
        activate_main()
        time.sleep(0.75)
        if dialog_hwnd():
            close_dialogs(accept=False)
        if app_ok():
            return True
    return False


# ───────────────────────── GUARD ─────────────────────────
def guard(need_app=True):
    while not running.wait(0.2):
        if stop.is_set():
            raise Interrupted("quit")
    if stop.is_set():
        raise Interrupted("quit")
    if last_input > action_started:
        raise Interrupted("human")
    if need_app and not app_ok():
        if dialog_hwnd() and not S.in_dialog:
            raise Interrupted("dialog")
        raise Interrupted("focus")

def hit(*keys):
    guard()
    if len(keys) == 1:
        tap(keys[0])
        time.sleep(random.uniform(0.06, 0.16))
    else:
        kb_hotkey(*keys)

def nap(seconds):
    end = time.time() + seconds * SPEED
    while True:
        left = end - time.time()
        if left <= 0:
            break
        if left > 1.0 and random.random() < 0.05 and not S.in_dialog:
            wander()
            continue
        time.sleep(min(0.22, left))
        guard()

def quiet_nap(seconds):
    end = time.time() + seconds * SPEED
    while time.time() < end:
        time.sleep(min(0.2, max(0.0, end - time.time())))
        guard(False)

def think(lo, hi):
    d = random.uniform(lo, hi)
    if d > 1.6 and random.random() < 0.42:
        drift()
    nap(d)


# ───────────────────────── HUMAN-LIKE MOUSE ─────────────────────────
def _ease(t):
    return t * t * (3 - 2 * t)

def _leg(tx, ty, dur):
    sx, sy = pyautogui.position()
    dx, dy = tx - sx, ty - sy
    dist = math.hypot(dx, dy)
    if dist < 1:
        return
    nx, ny = -dy / dist, dx / dist
    bend = random.uniform(-0.16, 0.16) * dist
    c1 = (sx + dx * 0.28 + nx * bend, sy + dy * 0.28 + ny * bend)
    c2 = (sx + dx * 0.72 + nx * bend * 0.45, sy + dy * 0.72 + ny * bend * 0.45)
    steps = max(9, int(dur / 0.012))
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        u = 1 - t
        x = u**3 * sx + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * tx
        y = u**3 * sy + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * ty
        if i < steps:
            x += random.uniform(-0.55, 0.55)
            y += random.uniform(-0.55, 0.55)
        ms_move(round(x), round(y))
        if i % 5 == 0:
            guard(False)
        time.sleep(dur / steps)
    ms_move(round(tx), round(ty))

def human_move(tx, ty, speed=1.0):
    guard(False)
    sx, sy = pyautogui.position()
    dist = math.hypot(tx - sx, ty - sy)
    if dist < 3:
        return
    dur = (0.17 + 0.11 * math.log2(1 + dist / 55)) * random.uniform(0.9, 1.4) / speed * SPEED
    if dist > 260 and random.random() < 0.28:
        ux, uy = (tx - sx) / dist, (ty - sy) / dist
        o = random.uniform(9, 20)
        _leg(tx + ux * o, ty + uy * o, dur)
        time.sleep(random.uniform(0.04, 0.12))
        _leg(tx, ty, random.uniform(0.11, 0.2))
    else:
        _leg(tx, ty, dur)

def human_click(x, y, clicks=1, button="left"):
    """Move, settle, wiggle 1 px (so the app sees a fresh mouse-move on the spot), press for a real-looking time."""
    human_move(x, y)
    time.sleep(random.uniform(0.08, 0.22))
    if button == "left":
        ms_move(x + random.choice([-1, 1]), y)
        time.sleep(random.uniform(0.02, 0.05))
        ms_move(x, y)
        time.sleep(random.uniform(0.04, 0.09))
    for c in range(clicks):
        guard()
        ms_down(button)
        time.sleep(random.uniform(0.07, 0.14))
        ms_up(button)
        if c < clicks - 1:
            time.sleep(random.uniform(0.06, 0.13))

def zone_rect():
    sw, sh = pyautogui.size()
    l, t, r, b = 0, 0, sw, sh
    rc = win_rect(main_hwnd())
    if rc:
        l, t, r, b = max(rc[0], 0), max(rc[1], 0), min(rc[2], sw), min(rc[3], sh)
    else:
        try:
            w = gw.getActiveWindow()
            if w and in_app(w.title):
                l, t, r, b = max(w.left, 0), max(w.top, 0), min(w.right, sw), min(w.bottom, sh)
        except Exception:
            pass
    W, H = r - l, b - t
    return l + ZONE[0] * W, l + ZONE[1] * W, t + ZONE[2] * H, t + ZONE[3] * H

def zone_point(bias_left=True):
    x0, x1, y0, y1 = zone_rect()
    if bias_left:
        x = random.triangular(x0, x1, x0 + 0.25 * (x1 - x0))
    else:
        x = random.uniform(x0, x1)
    return x, random.uniform(y0, y1)

def rect_point(rc, fx0, fx1, fy0, fy1):
    l, t, r, b = rc
    W, H = r - l, b - t
    return random.uniform(l + fx0 * W, l + fx1 * W), random.uniform(t + fy0 * H, t + fy1 * H)

def win_frac(fx, fy):
    rc = win_rect(main_hwnd())
    if not rc:
        sw, sh = pyautogui.size()
        return fx * sw, fy * sh
    return rc[0] + fx * (rc[2] - rc[0]), rc[1] + fy * (rc[3] - rc[1])

# ── EYES ──
def park_mouse():
    """Move the mouse to an empty spot so the crosshair / tooltips don't pollute screenshots."""
    human_move(*win_frac(PARK_SPOT[0] + random.uniform(-0.03, 0.03), PARK_SPOT[1] + random.uniform(-0.05, 0.05)),
               speed=0.8)
    quiet_nap(random.uniform(0.25, 0.6))

def grab(box, size=(48, 32)):
    """Tiny grey picture of part of the CADCraft window (box = fractions l, r, t, b), or None."""
    if Image is None:
        return None
    try:
        sw, sh = pyautogui.size()
        l, t, r, b = win_rect(main_hwnd()) or (0, 0, sw, sh)
        W, H = r - l, b - t
        x0, y0 = max(int(l + box[0] * W), 0), max(int(t + box[2] * H), 0)
        x1, y1 = min(int(l + box[1] * W), sw), min(int(t + box[3] * H), sh)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        im = pyautogui.screenshot(region=(x0, y0, x1 - x0, y1 - y0)).convert("L")
        return im.resize(size, getattr(Image, "Resampling", Image).BOX).tobytes()
    except Exception:
        return None

def changed(a, b, cell=3):
    if not a or not b or len(a) != len(b):
        return None
    return sum(1 for x, y in zip(a, b) if abs(x - y) > cell) / len(a)

def panel_open():
    """True while a side panel (Drafting Settings, Units, ...) covers the left strip."""
    if PANEL_REF is None:
        return False
    d = changed(PANEL_REF, grab(PANEL_BOX), 4)
    return d is not None and d > PANEL_THRESH

def capture_panel_ref():
    global PANEL_REF
    PANEL_REF = grab(PANEL_BOX)

def panel_rect_px():
    sw, sh = pyautogui.size()
    l, t, r, b = win_rect(main_hwnd()) or (0, 0, sw, sh)
    W, H = r - l, b - t
    return l, t + PANEL_BOX[2] * H, l + (PANEL_BOX[1] + 0.012) * W, t + PANEL_BOX[3] * H

def close_panel():
    """Close an in-window side panel with its X (verified by screenshot), Escape as a fallback. Never clicks inside it."""
    prev = S.in_dialog
    S.in_dialog = True
    try:
        for attempt in range(4):
            if PANEL_REF is not None and not panel_open():
                return True
            quiet_nap(random.uniform(0.3, 0.8))
            if attempt % 2 == 0:
                x, y = win_frac(*PANEL_CLOSE)
                human_click(x + random.uniform(-3, 3), y + random.uniform(-3, 3))
            else:
                kb_press("escape")
            quiet_nap(random.uniform(0.7, 1.1))
            if PANEL_REF is None:
                return True
        if panel_open():
            halt(["A side panel is still open and I cannot close it.",
                  "Close it yourself, or run  python cadwriter.py --calibrate  to re-measure the X button."])
        return True
    finally:
        S.in_dialog = prev

def look_canvas():
    park_mouse()
    return grab(DRAW_AREA, (64, 40))

def verify_drawing(before):
    d = changed(before, look_canvas())
    if d is None:
        return True
    S.checks += 1
    if d >= CHANGE_MIN:
        S.dead = 0
        log(f"check: canvas changed ({d * 100:.1f}% of the picture)")
        return True
    S.dead += 1
    log(f"check: NOTHING appeared on the canvas ({S.dead}/2)")
    if S.dead >= 2:
        S.dead = 0
        halt(["Nothing is being drawn. Likely causes: a side panel covers the canvas, the command line",
              "is hidden / uses other aliases, DRAW_AREA is wrong (run --calibrate), or another window is driven.",
              "If CADCraft runs as Administrator, run this script as Administrator too."])
    return False

def alive(h):
    try:
        return bool(h) and bool(user32.IsWindow(h)) and bool(user32.IsWindowVisible(h))
    except Exception:
        return False

def release_all():
    for k in ("shift", "ctrl", "alt"):
        try:
            pyautogui.keyUp(k)
        except Exception:
            pass
    for b in ("left", "middle", "right"):
        try:
            pyautogui.mouseUp(button=b)
        except Exception:
            pass

def keep_awake(on=True):
    if not IS_WIN:
        return
    try:
        flags = 0x80000000 | ((0x1 | 0x2) if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
    except Exception:
        pass


def drift():
    x, y = pyautogui.position()
    x0, x1, y0, y1 = zone_rect()
    human_move(min(max(x + random.randint(-38, 38), x0), x1),
               min(max(y + random.randint(-22, 22), y0), y1), 0.55)

def wander():
    if random.random() < 0.65:
        drift()
    else:
        human_move(*zone_point(bias_left=random.random() < 0.5), speed=random.uniform(0.5, 1.1))

def small_jitter():
    x, y = pyautogui.position()
    human_move(x + random.randint(-14, 14), y + random.randint(-10, 10), 0.8)

def ambient_move():
    global action_started
    action_started = time.time()
    try:
        if app_ok():
            wander()
    except Interrupted:
        pass


# ───────────────────────── HUMAN-LIKE TYPING ─────────────────────────
_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
NEIGHBORS = {}
for _r, _row in enumerate(_ROWS):
    for _c, _ch in enumerate(_row):
        _nb = []
        if _c > 0:
            _nb.append(_row[_c - 1])
        if _c < len(_row) - 1:
            _nb.append(_row[_c + 1])
        if _r > 0:
            _up = _ROWS[_r - 1]
            _nb += [_up[min(_c, len(_up) - 1)], _up[min(_c + 1, len(_up) - 1)]]
        if _r < 2:
            _dn = _ROWS[_r + 1]
            _nb.append(_dn[min(_c, len(_dn) - 1)])
        NEIGHBORS[_ch] = [x for x in _nb if x != _ch]

def neighbor(ch):
    opts = NEIGHBORS.get(ch.lower())
    if not opts:
        return None
    w = random.choice(opts)
    return w.upper() if ch.isupper() else w

SYMBOLS = set("(),:;=^*/+-$.\\")
LEFT_KEYS = set("qwertasdfgzxcvb12345`")
COMMON_PAIRS = {"th","he","in","er","an","re","on","at","en","nd","ti","es","or","te",
                "of","ed","is","it","al","ar","st","to","nt","ng","se","ha","as","ou","io","le"}

def _hand(ch):
    return "L" if ch.lower() in LEFT_KEYS else "R"

def key_delay(prev, ch, speed):
    d = (random.lognormvariate(math.log(BASE_DELAY), 0.45)
         * TYPING_SPEED * speed * S.rhythm * S.drift * S.governor * S.cell_mult)
    if prev and prev.isalpha() and ch.isalpha():
        if prev == ch:
            d *= 0.9
        elif _hand(prev) != _hand(ch):
            d *= 0.93
        else:
            d *= 1.05
        if (prev + ch).lower() in COMMON_PAIRS:
            d *= 0.9
    if ch.isupper():
        d *= 1.35
    elif ch.isdigit():
        d *= 1.2
    if ch in "()[]{}":
        d += random.uniform(0.15, 0.55)
    elif ch in SYMBOLS:
        d += random.uniform(0.08, 0.40)
    if prev and prev in "(,=":
        d += random.uniform(0.1, 0.4)
    if prev == " ":
        d += random.uniform(0.04, 0.2)
    if S.burst > 0:
        d *= 0.9
        S.burst -= 1
    elif random.random() < 0.03:
        S.burst = random.randint(3, 5)
    r = random.random()
    if r < MICRO_STOP:
        d += random.uniform(0.3, 1.0)
    elif r < MICRO_STOP + 0.012:
        d += random.uniform(1.2, 3.5)
    return d

def emit(ch, prev, speed):
    guard()
    t0 = time.time()
    press_char(ch)
    spent = time.time() - t0
    time.sleep(max(0.03, key_delay(prev, ch, speed) - spent))

def backspaces(n):
    for _ in range(n):
        guard()
        kb_press("backspace")
        time.sleep(random.uniform(0.09, 0.2))

def type_text(text, typos=True, speed=1.0):
    t_start = time.time()
    i, prev = 0, ""
    while i < len(text):
        if i % 20 == 19 and random.random() < 0.12 and not S.in_dialog:
            small_jitter()
        ch = text[i]
        if typos and i > 0 and ch.isalpha() and random.random() < TYPO_CHANCE:
            kind = random.choices(["adjacent", "late", "swap"], [5, 3, 2])[0]
            if kind == "swap" and i + 1 < len(text) and text[i + 1].isalpha() and text[i + 1] != ch:
                emit(text[i + 1], prev, speed)
                emit(ch, text[i + 1], speed)
                time.sleep(random.uniform(0.25, 0.6))
                backspaces(2)
                emit(ch, prev, speed)
                emit(text[i + 1], ch, speed)
                prev = text[i + 1]
                i += 2
                continue
            wrong = neighbor(ch)
            if wrong:
                emit(wrong, prev, speed)
                extra = 0
                if kind == "late":
                    for c in text[i + 1:i + 1 + random.randint(1, 3)]:
                        emit(c, wrong, speed)
                        extra += 1
                time.sleep(random.uniform(0.25, 0.7))
                backspaces(1 + extra)
        emit(ch, prev, speed)
        prev = ch
        i += 1
    S.chars += len(text)
    S.type_secs += time.time() - t_start
    govern()

def typing_wpm():
    if S.type_secs < 8:
        return None
    return (S.chars / 5.0) / (S.type_secs / 60.0)

def govern():
    w = typing_wpm()
    if w and w > WPM_MAX:
        S.governor = min(S.governor * 1.10, 2.5)
    elif w and w < WPM_MIN and S.governor > 1.0:
        S.governor = max(S.governor / 1.03, 1.0)
    S.chars *= 0.96
    S.type_secs *= 0.96

def say(text):
    """Type something at the command line and press Enter."""
    S.rhythm = min(max(S.rhythm * random.uniform(0.94, 1.06), 0.85), 1.35)
    S.drift = min(max(S.drift + random.uniform(-0.03, 0.035), 0.85), 1.3)
    S.cell_mult = min(max(random.lognormvariate(0, 0.25), 0.8), 1.7)
    if random.random() < 0.2:
        think(0.4, 1.4)
    type_text(text)
    nap(random.uniform(0.2, 0.6))
    hit("enter")
    nap(random.uniform(0.4, 1.0))


# ───────────────────────── DRAWING GEOMETRY ─────────────────────────
def draw_area_px():
    sw, sh = pyautogui.size()
    l, t, r, b = 0, 0, sw, sh
    rc = win_rect(main_hwnd())
    if rc:
        l, t, r, b = max(rc[0], 0), max(rc[1], 0), min(rc[2], sw), min(rc[3], sh)
    W, H = r - l, b - t
    return l + DRAW_AREA[0] * W, t + DRAW_AREA[2] * H, l + DRAW_AREA[1] * W, t + DRAW_AREA[3] * H

def slot_square(i):
    l, t, r, b = draw_area_px()
    cw, ch = (r - l) / COLS, (b - t) / ROWS
    s = min(cw, ch) * 0.9
    col, row = i % COLS, i // COLS
    cx, cy = l + (col + 0.5) * cw, t + (row + 0.5) * ch
    return cx - s / 2, cy - s / 2, s

def px(x, y):
    sx, sy, s = slot_square(S.slot)
    return sx + x * s, sy + y * s

def polar(cx, cy, r, ang):
    return cx + r * math.cos(ang), cy + r * math.sin(ang)


# ───────────────────────── PARTS (lists of steps in unit coordinates) ─────────────────────────
def sc_plate():
    x0, x1 = 0.08, 0.92
    y0, y1 = random.uniform(0.2, 0.26), random.uniform(0.72, 0.78)
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    st = [("rect", (x0, y0), (x1, y1)),
          ("offset", (x0 + 0.22, y0), (x0 + 0.22, y0 + 0.04))]
    r = random.choice([0.035, 0.045, 0.055])
    holes = [(cx, cy) for cx in (x0 + 0.1, x1 - 0.1) for cy in (y0 + 0.1, y1 - 0.1)]
    random.shuffle(holes)
    st += [("circle", h, r) for h in holes]
    big = random.choice([0.14, 0.17])
    st += [("circle", (mx, my), big), ("circle", (mx, my), big * 0.55),
           ("line", [(mx, y0 - 0.06), (mx, y1 + 0.06)]),
           ("line", [(x0 - 0.04, my), (x1 + 0.04, my)]),
           ("dim", (x0, y1), (x1, y1), (mx, min(y1 + 0.1, 0.97))),
           ("dim", (x1, y0), (x1, y1), (min(x1 + 0.06, 0.99), my))]
    return st

def sc_gear():
    n = random.choice([8, 10, 12, 14, 16])
    c = (0.5, 0.5)
    return [("polygon", n, c, 0.42), ("circle", c, 0.3), ("circle", c, 0.14), ("circle", c, 0.07),
            ("rect", (0.475, 0.37), (0.525, 0.44)),
            ("line", [(0.5, 0.04), (0.5, 0.96)]), ("line", [(0.04, 0.5), (0.96, 0.5)]),
            ("dim", (0.43, 0.5), (0.57, 0.5), (0.5, 0.64))]

def sc_bracket():
    a = random.uniform(0.32, 0.4)
    b = random.uniform(0.55, 0.65)
    pts = [(0.1, 0.15), (a, 0.15), (a, b), (0.9, b), (0.9, 0.85), (0.1, 0.85)]
    st = [("pline", pts, True),
          ("offset", (0.1, 0.4), (0.145, 0.4))]
    r = random.choice([0.035, 0.045])
    st += [("circle", (0.1 + (a - 0.1) / 2, 0.3), r), ("circle", (0.1 + (a - 0.1) / 2, 0.7), r),
           ("circle", (0.75, (b + 0.85) / 2), r), ("circle", (0.5, (b + 0.85) / 2), r)]
    st += [("dim", (0.1, 0.85), (0.9, 0.85), (0.5, 0.96)),
           ("dim", (0.1, 0.15), (0.1, 0.85), (0.02, 0.5))]
    return st

def sc_floorplan():
    vx = random.uniform(0.4, 0.6)
    return [("rect", (0.06, 0.14), (0.94, 0.86)),
            ("offset", (0.3, 0.14), (0.3, 0.18)),
            ("line", [(vx, 0.18), (vx, 0.5)]),
            ("line", [(0.1, 0.55), (vx, 0.55)]),
            ("line", [(vx, 0.62), (vx, 0.82)]),
            ("arc", (vx, 0.62), (vx + 0.06, 0.585), (vx + 0.08, 0.53)),
            ("arc", (0.2, 0.55), (0.235, 0.6), (0.29, 0.62)),
            ("line", [(0.7, 0.18), (0.7, 0.3)]),
            ("line", [(0.7, 0.3), (0.9, 0.3)]),
            ("dim", (0.06, 0.86), (0.94, 0.86), (0.5, 0.96)),
            ("dim", (0.94, 0.14), (0.94, 0.86), (0.995, 0.5))]

def sc_truss():
    xs = [0.08 + i * 0.21 for i in range(5)]
    top = [0.7, 0.42, 0.3, 0.42, 0.7]
    st = [("line", [(xs[0], 0.7), (xs[4], 0.7)]),
          ("pline", [(xs[i], top[i]) for i in range(5)], False)]
    for i in (1, 2, 3):
        st.append(("line", [(xs[i], top[i]), (xs[i], 0.7)]))
    st += [("line", [(xs[1], top[1]), (xs[2], 0.7)]), ("line", [(xs[3], top[3]), (xs[2], 0.7)]),
           ("line", [(xs[0], 0.7), (xs[0], 0.82)]), ("line", [(xs[4], 0.7), (xs[4], 0.82)]),
           ("dim", (xs[0], 0.7), (xs[4], 0.7), (0.5, 0.93)),
           ("dim", (xs[2], 0.3), (xs[2], 0.7), (0.58, 0.5))]
    return st

def sc_flange():
    c = (0.5, 0.5)
    k = random.choice([6, 8])
    rb = 0.3
    st = [("circle", c, 0.43), ("circle", c, 0.36), ("circle", c, 0.15)]
    a0 = random.uniform(0, math.pi)
    for i in range(k):
        st.append(("circle", polar(*c, rb, a0 + i * 2 * math.pi / k), 0.035))
    st += [("line", [(0.5, 0.03), (0.5, 0.97)]), ("line", [(0.03, 0.5), (0.97, 0.5)]),
           ("dim", (0.07, 0.5), (0.93, 0.5), (0.5, 0.97))]
    return st

def sc_spiral():
    c = (0.5, 0.5)
    if random.random() < 0.5:
        n, turns = random.randint(22, 28), random.uniform(2.2, 2.8)
        pts = [polar(*c, 0.04 + 0.4 * i / (n - 1), i * turns * 2 * math.pi / (n - 1)) for i in range(n)]
    else:
        k, n = random.choice([2, 3, 4]), 38
        pts = [polar(*c, 0.42 * math.cos(k * th), th)
               for th in (i * 2 * math.pi / (n - 1) for i in range(n))]
    return [("pline", pts, False), ("circle", c, 0.02)]

def sc_orbit():
    c = (0.5, 0.5)
    st = []
    for a, b in ((0.43, 0.26), (0.31, 0.19), (0.19, 0.11)):
        st.append(("ellipse", (c[0] - a, c[1]), (c[0] + a, c[1]), (c[0], c[1] - b)))
    st += [("circle", c, 0.045), ("circle", (c[0] + 0.31, c[1]), 0.025),
           ("line", [(0.03, 0.5), (0.97, 0.5)]), ("line", [(0.5, 0.2), (0.5, 0.8)])]
    return st

def sc_stairs():
    n = random.randint(6, 9)
    rise, run = 0.55 / n, 0.7 / n
    pts = [(0.12, 0.8)]
    for i in range(n):
        x, y = pts[-1]
        pts.append((x, y - rise))
        pts.append((x + run, y - rise))
    pts.append((pts[-1][0], 0.8))
    return [("pline", pts, True),
            ("line", [(0.06, 0.8), (0.94, 0.8)]),
            ("dim", (0.12, 0.8), (0.12 + 0.7, 0.8), (0.5, 0.93)),
            ("dim", (0.12 + 0.7, 0.8), (0.12 + 0.7, 0.8 - 0.55), (0.97, 0.5))]

SCENES = {"plate": sc_plate, "gear": sc_gear, "bracket": sc_bracket, "floorplan": sc_floorplan,
          "truss": sc_truss, "flange": sc_flange, "spiral": sc_spiral, "orbit": sc_orbit,
          "stairs": sc_stairs}

# a project = a named set of parts (each in its own corner of the sheet)
PROJECTS = [
    ("Mounting_Plate",  ["plate", "flange"]),
    ("Gear_Housing",    ["gear", "flange", "orbit"]),
    ("Bracket_Set",     ["bracket", "plate"]),
    ("House_Plan",      ["floorplan", "stairs"]),
    ("Roof_Truss",      ["truss", "stairs"]),
    ("Cam_Study",       ["spiral", "orbit"]),
    ("Flange_Detail",   ["flange", "gear"]),
]
# drafting phases: what is drawn first, second ...
PHASE_OF = {"rect": 0, "pline": 0, "polygon": 0, "ellipse": 0,
            "arc": 1, "circle": 1, "line": 1,
            "offset": 2, "dim": 2, "move": 3}
PHASE_NAMES = ["sketching the outlines", "adding holes and centre lines", "offsets and dimensions", "final touches"]
PHASE_LAYER = {0: "OUTLINE", 1: "DETAILS", 2: "DIMS", 3: "OUTLINE"}
LAYER_NAMES = ["OUTLINE", "DETAILS", "DIMS"]


# ───────────────────────── COMMANDS AND STEPS ─────────────────────────
ALIAS = {"line": ("line", "l"), "pline": ("pline", "pl"), "circle": ("circle", "c"),
         "rect": ("rectang", "rec"), "arc": ("arc", "a"), "ellipse": ("ellipse", "el"),
         "polygon": ("polygon", "pol"), "dim": ("dimlinear", "dli"), "offset": ("offset", "o"),
         "move": ("move", "m"), "undo": ("undo", "u")}

def cmd_look():
    return grab(COMMAND_BOX, (96, 24))

def start_cmd(kind):
    """Start a drawing command and CHECK that the command line reacted (retry once, pause after repeated failures)."""
    full, short = ALIAS[kind]
    if kind in TOOL_ICONS and TOOLBAR_CLICK_CHANCE and random.random() < TOOLBAR_CLICK_CHANCE:
        x, y = win_frac(*TOOL_ICONS[kind])
        human_click(x + random.uniform(-3, 3), y + random.uniform(-3, 3))
        nap(random.uniform(0.5, 1.1))
        S.cmd_snap = cmd_look()
        return
    for attempt in (1, 2):
        before = cmd_look()
        say(short if USE_ALIASES else full)
        snap = cmd_look()
        d = changed(before, snap, 4)
        if d is None or d >= 0.01:
            S.cmd_dead = 0
            S.cmd_snap = snap
            return
        S.cmd_dead += 1
        log(f"warning: the command line did not react to '{short}' ({S.cmd_dead}/{DEAD_LIMIT})")
        if S.cmd_dead >= DEAD_LIMIT:
            S.cmd_dead = 0
            halt(["Typed commands are not reaching CADCraft's command line.",
                  "Check: command line visible? canvas has focus? (COMMAND_BOX right? run --calibrate)",
                  "If CADCraft runs as Administrator, run this script as Administrator too."])
        if attempt == 1:
            hit("escape")
            focus_canvas()
    S.cmd_snap = None

def click_at(x, y):
    X, Y = px(x, y)
    human_move(X, Y, speed=random.uniform(0.55, 1.1))
    if random.random() < 0.35:
        think(0.25, 1.0)
    human_click(X, Y)
    nap(random.uniform(0.3, 0.8))
    if S.cmd_snap is not None:                    # first click after a command: the prompt must change
        snap, S.cmd_snap = S.cmd_snap, None
        d = changed(snap, cmd_look(), 4)
        if d is not None:
            if d < 0.01:
                S.click_dead += 1
                log(f"warning: the click did not change the command prompt ({S.click_dead}/{DEAD_LIMIT})")
                if S.click_dead >= DEAD_LIMIT:
                    S.click_dead = 0
                    halt(["Clicks are not reaching the drawing canvas.",
                          "Likely: a side panel is covering it, DRAW_AREA is off (run --calibrate), or CADCraft",
                          "runs as Administrator (then run this script as Administrator too)."])
            else:
                S.click_dead = 0

def do_step(st):
    k = st[0]
    if k == "line":
        start_cmd("line")
        for p in st[1]:
            click_at(*p)
        hit("enter")
    elif k == "pline":
        start_cmd("pline")
        for p in st[1]:
            click_at(*p)
        if st[2]:
            say("c")
        else:
            hit("enter")
    elif k == "rect":
        start_cmd("rect")
        click_at(*st[1])
        click_at(*st[2])
    elif k == "circle":
        start_cmd("circle")
        cx, cy = st[1]
        click_at(cx, cy)
        click_at(*polar(cx, cy, st[2], random.uniform(0, 2 * math.pi)))
    elif k in ("arc", "ellipse"):
        start_cmd(k)
        for p in st[1:4]:
            click_at(*p)
    elif k == "polygon":
        start_cmd("polygon")
        say(str(st[1]))
        cx, cy = st[2]
        click_at(cx, cy)
        say("i")
        click_at(*polar(cx, cy, st[3], random.uniform(0, 2 * math.pi)))
    elif k == "dim":
        start_cmd("dim")
        click_at(*st[1])
        click_at(*st[2])
        click_at(*st[3])
    elif k == "offset":
        start_cmd("offset")
        say("t")
        click_at(*st[1])
        click_at(*st[2])
        hit("enter")
    elif k == "move":
        start_cmd("move")
        click_at(-0.03, -0.03)
        click_at(1.03, 1.03)
        hit("enter")
        nap(random.uniform(0.3, 0.8))
        click_at(0.5, 0.5)
        click_at(0.5 + st[1][0], 0.5 + st[1][1])
    S.cmd_snap = None
    nap(random.uniform(0.4, 1.1))

def undo_redo(st):
    think(1.0, 2.5)
    say("u")
    think(0.6, 1.6)
    do_step(st)


# ───────────────────────── PROJECT FLOW ─────────────────────────
def new_project():
    pool = [p for p in PROJECTS if p[0] not in S.recent[-3:]] or PROJECTS
    name, parts = random.choice(pool)
    S.recent.append(name)
    queue = []
    for si, part in enumerate(parts):
        for k, st in enumerate(SCENES[part]()):
            queue.append({"phase": PHASE_OF.get(st[0], 1), "slot": si, "part": part, "st": st, "k": k})
    queue.sort(key=lambda q: (q["phase"], q["slot"], q["k"]))
    if random.random() < 0.3:
        queue.append({"phase": 3, "slot": 0, "part": parts[0], "k": 99,
                      "st": ("move", (random.choice([-1, 1]) * 0.03, random.choice([-1, 1]) * 0.02))})
    stamp = time.strftime("%H%M%S")
    S.project = {"name": name, "title": name.replace("_", " "), "parts": parts, "queue": queue,
                 "i": 0, "phase": -1, "setup": 0,
                 "path": WORK_DIR / f"{name}_{stamp}{FILE_EXT}"}
    log(f"NEW PROJECT: {S.project['title']}  parts={parts}  steps={len(queue)}  file={S.project['path'].name}")

def new_blank_drawing():
    """A brand-new drawing for the project (Ctrl+N). The very first project uses the blank drawing we already have."""
    if S.blank:
        S.blank = False
        S.saved = False
        return
    S.in_dialog = True
    try:
        hit("escape")
        nap(0.3)
        hit("ctrl", "n")
        quiet_nap(random.uniform(1.4, 2.2))
        if dialog_hwnd():
            log("new-drawing dialog: accepting the default")
            close_dialogs(accept=True)
        elif panel_open():
            log("new-drawing panel: accepting the default")
            kb_press("enter")
            quiet_nap(1.0)
            if panel_open():
                close_panel()
        quiet_nap(1.0)
    finally:
        S.in_dialog = False
    S.saved = False

def layer_cmd(opt, name):
    """-LAYER  <option>  <name>  Enter   (m = make + set current, s = set current)."""
    try:
        say("-layer")
        say(opt)
        say(name)
        hit("enter")
        nap(0.4)
    finally:
        hit("escape")
        nap(0.2)
        hit("escape")

def make_layers():
    if not USE_LAYERS or S.layers_ok is False:
        return
    park_mouse()
    before = grab(LAYER_BOX, (48, 8))
    for nm in LAYER_NAMES:
        layer_cmd("m", nm)
    park_mouse()
    d = changed(before, grab(LAYER_BOX, (48, 8)), 4)
    if d is None:
        S.layers_ok = True
    elif d > 0.05:
        S.layers_ok = True
        log("layers: created (the current-layer row changed)")
    else:
        S.layers_ok = False
        log("layers: the current-layer row did not change - -LAYER is not supported here (or LAYER_BOX is off); "
            "layer steps switched off")
    if S.layers_ok:
        layer_cmd("s", PHASE_LAYER[0])

def set_layer(name):
    if USE_LAYERS and S.layers_ok:
        layer_cmd("s", name)

def project_setup(max_steps=2):
    """Start from scratch: new drawing, glance at settings, make layers, save under the project's name."""
    P = S.project
    steps = ["new", "units", "settings", "layers", "saveas"]
    n = 0
    while P["setup"] < len(steps) and n < max_steps:
        s = steps[P["setup"]]
        log(f"project '{P['title']}': setup - {s}")
        if s == "new":
            new_blank_drawing()
        elif s == "units":
            act_dialog("units")
        elif s == "settings":
            act_dialog("drafting")
        elif s == "layers":
            make_layers()
        elif s == "saveas":
            S.saved = False
            save()
        P["setup"] += 1
        n += 1
        think(1.0, 3.0)

def enter_phase(P, ph):
    if P["phase"] >= 0:
        save()
        if random.random() < 0.5:
            act_look_around()
    log(f"project '{P['title']}': {PHASE_NAMES[ph]}")
    set_layer(PHASE_LAYER[ph])
    P["phase"] = ph

def work_project():
    if not S.project:
        new_project()
    P = S.project
    if P["setup"] < 5:
        return project_setup()
    check = VERIFY_DRAWING and (S.checks < 3 or random.random() < 0.25)
    before = look_canvas() if check else None
    todo = random.randint(2, 4)
    done = 0
    while P["i"] < len(P["queue"]) and done < todo:
        it = P["queue"][P["i"]]
        if it["phase"] != P["phase"]:
            enter_phase(P, it["phase"])
        S.slot = it["slot"]
        do_step(it["st"])
        if it["st"][0] != "move" and random.random() < UNDO_CHANCE:
            undo_redo(it["st"])
        P["i"] += 1
        done += 1
        nap(random.uniform(0.4, 1.3))
        if random.random() < 0.14:
            think(1.0, 3.2)
        if random.random() < 0.08:
            save()
    if check and before is not None:
        verify_drawing(before)
    if P["i"] >= len(P["queue"]):
        think(1.0, 3.0)
        save()
        S.projects_done += 1
        log(f"PROJECT COMPLETE: {P['title']} ({S.projects_done} so far)")
        S.project = None
        S.blank = False
        return
    save()
    w = typing_wpm()
    if w:
        log(f"typing ~ {w:.0f} wpm (pauses excluded)")


# ───────────────────────── SAVE / VIEW ─────────────────────────
def dialog_up(before=None):
    if dialog_hwnd():
        return True
    if before:
        d = changed(before, grab(PANEL_BOX), 4)
        return d is not None and d > PANEL_THRESH
    return False

def save():
    before = grab(PANEL_BOX)
    hit("ctrl", "s")
    quiet_nap(random.uniform(0.8, 1.4))
    if not dialog_up(before):
        return
    path = S.project["path"] if S.project else WORKBOOK
    prev = S.in_dialog
    S.in_dialog = True
    try:
        if not S.saved:
            log(f"Save As: {Path(path).name}")
            quiet_nap(random.uniform(0.4, 0.9))
            hit("ctrl", "a")
            nap(0.3)
            type_text(str(path), typos=False)
            nap(random.uniform(0.4, 0.9))
            hit("enter")
            quiet_nap(random.uniform(0.8, 1.5))
            S.saved = True
        if dialog_hwnd():
            log("save opened a prompt")
            close_dialogs(accept=True)
        if panel_open():
            close_panel()
    finally:
        S.in_dialog = prev

def heal():
    log("healing after interruption")
    if dialog_hwnd():
        close_dialogs(accept=False)
    for _ in range(3):
        hit("escape")
        nap(0.2)
    if panel_open():
        close_panel()

def ensure_clean():
    """Before every action: no dialog window, no side panel."""
    if dialog_hwnd():
        close_dialogs(accept=False)
    if panel_open():
        log("a side panel is open - closing it")
        close_panel()

def focus_canvas():
    hit("escape")
    nap(0.15)
    human_click(*zone_point())
    nap(0.25)
    hit("escape")
    nap(0.15)
    hit("escape")

def restore_view():
    v = S.view
    if v["pan"] != (0, 0):
        cx, cy = win_frac(0.45, 0.55)
        dx, dy = v["pan"]
        human_move(cx, cy)
        _drag_middle(cx, cy, cx - dx, cy - dy)
        v["pan"] = (0, 0)
    if v["zoom"] and v["zpos"]:
        human_move(*v["zpos"])
        step = -1 if v["zoom"] > 0 else 1
        while v["zoom"]:
            guard()
            ms_scroll(step)
            v["zoom"] += step
            time.sleep(random.uniform(0.2, 0.45))

def _drag_middle(x0, y0, x1, y1):
    human_move(x0, y0)
    time.sleep(random.uniform(0.1, 0.3))
    guard()
    ms_down("middle")
    try:
        human_move(x1, y1, speed=0.6)
        time.sleep(random.uniform(0.1, 0.3))
    finally:
        pyautogui.mouseUp(button="middle")
        mark()


# ───────────────────────── DIALOG BOXES ─────────────────────────
def hover_menu_bar():
    rc = win_rect(main_hwnd())
    if not rc:
        return
    x, y = rect_point(rc, MENU_X[0], MENU_X[-1] + 0.01, MENU_Y - 0.012, MENU_Y + 0.012)
    human_move(x, y)
    think(0.3, 1.1)

def wait_dialog_kind(timeout, pre):
    """'window' (real Win32 dialog), 'panel' (side panel inside CADCraft) or None."""
    end = time.time() + timeout
    main = main_hwnd()
    while time.time() < end:
        h = dialog_hwnd()
        if h and h != main:
            return "window"
        d = changed(pre, grab(PANEL_BOX), 4)
        if d is not None and d > PANEL_THRESH:
            return "panel"
        quiet_nap(0.2)
    return None

def close_dialogs(accept=False):
    """Close real dialog windows / menus (side panels use close_panel)."""
    prev = S.in_dialog
    S.in_dialog = True
    try:
        for attempt in range(4):
            h = dialog_hwnd()
            if not h:
                break
            is_menu = h == main_hwnd()
            rc = None if is_menu else win_rect(h)
            quiet_nap(random.uniform(0.3, 0.9))
            if rc:
                human_move(*rect_point(rc, 0.25, 0.85, 0.3, 0.9))
                quiet_nap(random.uniform(0.3, 1.0))
            if is_menu:
                kb_press("escape")
            elif attempt == 0:
                if CLOSE_BY_CLICK and rc and not accept and random.random() < 0.45:
                    human_click(rc[2] - X_BTN[0], rc[1] + X_BTN[1])
                else:
                    kb_press("enter" if accept else "escape")
            elif attempt == 1:
                kb_press("escape")
            elif attempt == 2 and h != main_hwnd() and IS_WIN:
                user32.PostMessageW(h, 0x0010, 0, 0)
            quiet_nap(random.uniform(0.5, 0.9))
        if panel_open():
            close_panel()
        if not app_ok():
            activate_main()
            quiet_nap(0.6)
    finally:
        S.in_dialog = prev

def act_dialog(name=None):
    """Open a dialog / panel, look around it with the mouse (never click inside), close it."""
    if name is None:
        name = random.choices(list(DIALOGS), [DIALOGS[n].get("w", 1.0) for n in DIALOGS])[0]
    spec = DIALOGS[name]
    log(f"dialog: {name}")
    think(0.5, 1.6)
    hover_menu_bar()
    nap(random.uniform(0.15, 0.5))
    park_mouse()
    pre = grab(PANEL_BOX)
    try:
        S.in_dialog = True
        say(spec["alias"])
        kind = wait_dialog_kind(3.0, pre)
        if kind is None:
            log(f"'{spec['alias']}' opened nothing I can see - cancelling")
            kb_press("escape")
            return
        quiet_nap(random.uniform(0.8, 1.4))
        if kind == "window":
            h = dialog_hwnd()
            rc = win_rect(h) if h else None
        else:
            rc = panel_rect_px()
        if rc:
            for _ in range(random.randint(2, 4)):
                human_move(*rect_point(rc, 0.1, 0.9, 0.08, 0.95), speed=random.uniform(0.6, 1.0))
                think(0.6, 1.8)
        if kind == "window":
            close_dialogs(accept=False)
        else:
            close_panel()
    finally:
        S.in_dialog = False


# ───────────────────────── OTHER ACTIONS ─────────────────────────
def act_look_around():
    if random.random() < 0.55:
        x, y = zone_point(False)
        human_move(x, y)
        think(0.4, 1.2)
        n = random.randint(2, 4)
        S.view["zpos"] = (x, y)
        for _ in range(n):
            guard()
            ms_scroll(1)
            S.view["zoom"] += 1
            time.sleep(random.uniform(0.25, 0.55))
        think(1.5, 4.0)
        for _ in range(n):
            guard()
            ms_scroll(-1)
            S.view["zoom"] -= 1
            time.sleep(random.uniform(0.25, 0.55))
    else:
        ax, ay = win_frac(random.uniform(0.35, 0.55), random.uniform(0.45, 0.65))
        dx, dy = random.randint(-180, 180), random.randint(-110, 110)
        bx, by = ax + dx, ay + dy
        try:
            _drag_middle(ax, ay, bx, by)
        finally:
            px_, py_ = pyautogui.position()
            S.view["pan"] = (px_ - ax, py_ - ay)
        think(1.5, 3.5)
        _drag_middle(bx, by, ax, ay)
        S.view["pan"] = (0, 0)
    think(0.4, 1.0)

def act_peek_menu():
    """Open a menu-bar menu, glance across a couple of menus, close it. Switched off after 3 failed attempts."""
    if not MENU_PEEK or S.menu_ok is False:
        return mouse_idle()
    S.in_dialog = True
    try:
        i = random.randrange(len(MENU_X))
        below = (0.0, 0.40, MENU_Y + 0.02, MENU_Y + 0.45)
        before = grab(below, (40, 30)) if S.menu_ok is None else None
        human_click(*win_frac(MENU_X[i], MENU_Y))
        quiet_nap(random.uniform(0.5, 1.2))
        if S.menu_ok is None:
            d = changed(before, grab(below, (40, 30)))
            if d is not None:
                if d > 0.04:
                    S.menu_ok = True
                    log("menu peek verified: a menu opens where I click")
                else:
                    S.menu_fail += 1
                    log(f"menu peek: nothing opened ({S.menu_fail}/3)")
                    if S.menu_fail >= 3:
                        S.menu_ok = False
                        log("menu peek switched off (run --calibrate to measure the menu bar)")
        if S.menu_ok is True:
            for _ in range(random.randint(0, 2)):
                j = min(max(i + random.choice([-1, 1]), 0), len(MENU_X) - 1)
                human_move(*win_frac(MENU_X[j], MENU_Y), speed=0.8)
                quiet_nap(random.uniform(0.4, 1.0))
            human_move(*win_frac(MENU_X[i] + random.uniform(-0.01, 0.02), MENU_Y + random.uniform(0.04, 0.2)), speed=0.7)
            quiet_nap(random.uniform(0.5, 1.4))
        hit("escape")
        quiet_nap(random.uniform(0.3, 0.7))
        close_dialogs(accept=False)
    finally:
        S.in_dialog = False

def act_hover_panels():
    fx0, fx1, fy0, fy1 = random.choice([(0.01, 0.14, 0.2, 0.4), (0.835, 0.98, 0.55, 0.95), (0.835, 0.98, 0.15, 0.35)])
    for _ in range(random.randint(1, 3)):
        human_move(*win_frac(random.uniform(fx0, fx1), random.uniform(fy0, fy1)))
        think(0.5, 1.6)
    human_move(*zone_point())

def act_select_window():
    """Window-select one part of the project, look at it, deselect."""
    P = S.project
    if not P or P["setup"] < 5 or P["i"] == 0:
        return work_project()
    held = S.slot
    S.slot = random.randrange(len(P["parts"]))
    try:
        click_at(-0.03, -0.03)
        click_at(random.uniform(0.5, 1.03), random.uniform(0.5, 1.03))
        think(1.2, 3.0)
    finally:
        S.slot = held
    hit("escape")
    nap(0.3)
    hit("escape")

def mouse_idle():
    for _ in range(random.randint(1, 4)):
        human_move(*zone_point())
        think(0.4, 1.4)

def act_think():
    think(4.0, 11.0)


def run_one_action():
    global action_started
    action_started = time.time()
    try:
        if not bring_back():
            log("could not bring CADCraft forward")
            return
        if S.needs_heal:
            heal()
            S.needs_heal = False
        S.needs_heal = True
        ensure_clean()
        restore_view()
        focus_canvas()
        S.fresh = False
        if random.random() < 0.5:
            human_move(*zone_point())
            time.sleep(random.uniform(0.15, 0.6))

        if random.random() < 0.75:
            work_project()
        else:
            acts = [(act_look_around, 3.0), (act_select_window, 1.5), (act_dialog, 1.2 * DIALOG_CHANCE_WEIGHT),
                    (act_peek_menu, 1.5), (act_hover_panels, 1.2), (mouse_idle, 1.3), (act_think, 0.6)]
            random.choices([a for a, _ in acts], [w for _, w in acts])[0]()
        S.needs_heal = False
    except Interrupted as e:
        log(f"aborted: {e.reason}")
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        log(f"error: {e!r}")
        if DEBUG:
            traceback.print_exc()
    finally:
        S.in_dialog = False
        try:
            pyautogui.mouseUp(button="middle")
        except Exception:
            pass


# ───────────────────────── STARTUP ─────────────────────────
def find_app():
    if APP_EXE and Path(APP_EXE).exists():
        return str(APP_EXE)
    found = shutil.which("cadcraft") or shutil.which("CADCraft")
    if found:
        return found
    env = os.environ
    roots = [(WORK_DIR, 4),
             (Path(env.get("LOCALAPPDATA", "")) / "Programs", 3),
             (Path(env.get("ProgramFiles", "")), 2),
             (Path(env.get("ProgramFiles(x86)", "")), 2),
             (Path.home() / "Downloads", 3),
             (Path.home() / "Desktop", 3)]
    for root, depth in roots:
        if not str(root) or not root.exists():
            continue
        base = len(root.parts)
        for dirpath, dirs, files in os.walk(root):
            if len(Path(dirpath).parts) - base >= depth:
                dirs[:] = []
            for f in files:
                fl = f.lower()
                if fl.startswith("cadcraft") and fl.endswith(".exe") and "unins" not in fl and "update" not in fl:
                    return str(Path(dirpath) / f)
    return None

def open_app():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    S.hwnd = 0
    S.hwnd_scan = 0.0
    existing = app_windows()
    if existing:
        log("reusing already-open CADCraft window")
        S.reused = True
        force_activate(existing[0])
        time.sleep(1.4)
        if in_app():
            return True

    exe = find_app()
    if not exe:
        log("CADCraft.exe not found - set APP_EXE at the top of the script")
        return False

    log(f"launching {exe}")
    S.reused = False
    subprocess.Popen([exe], cwd=str(WORK_DIR))
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline and not stop.is_set():
        wins = app_windows()
        if wins:
            w = wins[0]
            force_activate(w)
            time.sleep(0.8)
            if in_app():
                try:
                    w.maximize()
                except Exception:
                    pass
                S.hwnd = 0
                S.hwnd_scan = 0.0
                log("CADCraft ready")
                time.sleep(3.0)
                return True
        time.sleep(1)
    log(f"no window with '{APP_MATCH}' in its title appeared. Open windows:")
    try:
        for t in gw.getAllTitles():
            if t.strip():
                log(f"   {t!r}")
    except Exception:
        pass
    log("-> set APP_MATCH to part of the real title (and APP_EXE if the exe wasn't found)")
    return False

def check_monitor():
    """The whole CADCraft window must be on the main screen (a window partly off-screen shifts every position)."""
    for attempt in (1, 2):
        rc = win_rect(main_hwnd())
        if not rc:
            return
        sw, sh = pyautogui.size()
        if not (rc[0] < -12 or rc[1] < -12 or rc[2] > sw + 12 or rc[3] > sh + 12):
            return
        if attempt == 1:
            log(f"the CADCraft window {rc} is not fully on the main screen ({sw}x{sh}) - maximising it")
            for w in app_windows():
                try:
                    w.maximize()
                except Exception:
                    pass
            time.sleep(1.2)
            S.hwnd = 0
            S.hwnd_scan = 0.0
    log("warning: CADCraft is still not fully on the main screen. Move it there and maximise it.")

def ensure_own_drawing():
    if not (S.reused and NEW_DRAWING_IF_REUSED):
        return
    log("CADCraft was already open: starting a NEW drawing (Ctrl+N) so your work is left alone")
    S.in_dialog = True
    try:
        hit("escape")
        nap(0.3)
        hit("ctrl", "n")
        quiet_nap(random.uniform(1.4, 2.2))
        if dialog_hwnd():
            log("template dialog: accepting the default")
            close_dialogs(accept=True)
        elif panel_open():
            kb_press("enter")
            quiet_nap(1.0)
            if panel_open():
                close_panel()
        quiet_nap(1.0)
    finally:
        S.in_dialog = False
    S.blank = True
    S.saved = False

def startup():
    global action_started
    action_started = time.time()
    try:
        if not bring_back():
            log("could not bring CADCraft forward")
            return False
        for _ in range(2):
            hit("escape")
            nap(0.2)
        if PANEL_REF is None:
            capture_panel_ref()
            log("panel reference taken from the current screen (assuming NO side panel is open right now)")
        ensure_clean()
        ensure_own_drawing()
        return True
    except Interrupted as e:
        log(f"start-up aborted: {e.reason}")
        return False
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        log(f"start-up error: {e!r}")
        return False

def probe_dialog():
    """Open Drafting Settings: is it a real window or an in-window side panel? Close it again."""
    park_mouse()
    pre = grab(PANEL_BOX)
    S.in_dialog = True
    try:
        say("ds")
        kind = wait_dialog_kind(3.0, pre)
        quiet_nap(0.8)
        S.dialog_kind = kind
        if kind == "window":
            log("dialogs: separate windows")
            close_dialogs(accept=False)
        elif kind == "panel":
            log("dialogs: in-window side panels - handled by screenshot + X button")
            close_panel()
            if panel_open():
                return None
        else:
            log("dialogs: nothing opened after DS - the command line or its aliases may not work")
            hit("escape")
        return kind
    finally:
        S.in_dialog = False

def run_probes():
    global action_started
    action_started = time.time()
    try:
        if not bring_back():
            return
        heal()
        focus_canvas()
        probe_dialog()
        quiet_nap(0.8)
        S.menu_ok = None
        act_peek_menu()
    except Interrupted as e:
        log(f"start-up checks aborted: {e.reason}")
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        log(f"start-up checks error: {e!r}")
    finally:
        S.in_dialog = False


# ───────────────────────── CALIBRATION ─────────────────────────
def load_calibration():
    global DRAW_AREA, ZONE, PANEL_BOX, PANEL_CLOSE, COMMAND_BOX, LAYER_BOX, MENU_Y, MENU_X, PANEL_REF
    try:
        d = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
    except Exception:
        log("no calibration file - using built-in layout guesses (run --calibrate for best results)")
        return False
    try:
        DRAW_AREA = tuple(d["draw_area"])
        ZONE = zone_from(DRAW_AREA)
        COMMAND_BOX = tuple(d["command_box"])
        PANEL_BOX = tuple(d["panel_box"])
        PANEL_CLOSE = tuple(d["panel_close"])
        LAYER_BOX = tuple(d["layer_box"])
        MENU_Y = d["menu_y"]
        MENU_X = list(d["menu_x"])
        if d.get("panel_ref"):
            PANEL_REF = bytes.fromhex(d["panel_ref"])
        log(f"calibration loaded from {CALIB_FILE.name}")
        return True
    except Exception as e:
        log(f"calibration file unusable ({e!r}) - using defaults")
        return False

def _point(label, secs=7):
    print(f"\n>>> {label}\n    (hold the mouse there - capturing in {secs} s)")
    for s in range(secs, 0, -1):
        print(f"    {s}...", end="\r", flush=True)
        time.sleep(1)
    x, y = pyautogui.position()
    rc = win_rect(main_hwnd()) or (0, 0) + tuple(pyautogui.size())
    fx, fy = (x - rc[0]) / (rc[2] - rc[0]), (y - rc[1]) / (rc[3] - rc[1])
    print(f"    got ({fx:.3f}, {fy:.3f})")
    return fx, fy

def calibrate():
    """Measure the layout on THIS screen. Nothing is clicked: you only hold the mouse still."""
    if not open_app():
        return
    force_activate(app_windows()[0])
    check_monitor()
    print("\nCALIBRATION - CADCraft must be maximised with NO side panel open. Do not click; just hold the mouse.")
    tl = _point("TOP-LEFT corner of the EMPTY drawing area (just inside the grid, right of any tool sets)")
    br = _point("BOTTOM-RIGHT corner of the drawing area (left of the ViewCube / Layers panel, above the command line)")
    cmd = _point("the MIDDLE of the command-line text at the bottom (where 'Command:' is shown)")
    fm = _point("the centre of the 'File' menu title (first menu)")
    hm = _point("the centre of the 'Help' menu title (last menu)")
    lay = _point("the current-layer row in the Layers panel (the row with the light bulb and the layer name)")
    print("\nNow OPEN Drafting Settings yourself (click in CADCraft, type DS and press Enter).")
    px_ = _point("the X (close) button of the Drafting Settings panel", secs=14)
    print("\nNow CLOSE the panel with that X. Leave NO panel open. Do not touch anything after closing.")
    time.sleep(8)
    global PANEL_BOX
    PANEL_BOX = (0.0, max(px_[0] - 0.004, 0.05), 0.12, 0.95)
    park_mouse()
    ref = grab(PANEL_BOX)
    n = 11
    menu_x = [fm[0] + (hm[0] - fm[0]) * i / (n - 1) for i in range(n)]
    data = {
        "draw_area": [round(tl[0], 4), round(br[0], 4), round(tl[1], 4), round(br[1], 4)],
        "command_box": [max(cmd[0] - 0.19, 0.0), min(cmd[0] + 0.19, 1.0), max(cmd[1] - 0.03, 0.0), min(cmd[1] + 0.03, 1.0)],
        "panel_box": list(PANEL_BOX),
        "panel_close": [round(px_[0], 4), round(px_[1], 4)],
        "layer_box": [max(lay[0] - 0.14, 0.0), min(lay[0] + 0.14, 1.0), lay[1] - 0.02, lay[1] + 0.02],
        "menu_y": round((fm[1] + hm[1]) / 2, 4),
        "menu_x": [round(v, 4) for v in menu_x],
        "panel_ref": ref.hex() if ref else "",
    }
    CALIB_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(f"\nSaved {CALIB_FILE}\nNext:  python cadwriter.py --test")


# ───────────────────────── SELF TEST ─────────────────────────
def self_check():
    """--test: a fast end-to-end check of every assumption; prints PASS / FAIL lines."""
    global action_started
    results = []

    def rep(ok, msg):
        results.append(bool(ok))
        log(("PASS  " if ok else "FAIL  ") + msg)

    action_started = time.time()
    try:
        heal()
        focus_canvas()
        kind = probe_dialog()
        rep(kind is not None, f"dialogs: Drafting Settings opened as '{kind}'")
        rep(not panel_open() and not dialog_hwnd(), "dialogs: closed again, nothing left open")
        new_project()
        P = S.project
        before = look_canvas()
        S.cmd_dead = S.click_dead = 0
        for it in P["queue"][:4]:
            S.slot = it["slot"]
            do_step(it["st"])
            P["i"] += 1
        d = changed(before, look_canvas())
        if d is None:
            log("INFO  could not compare screenshots (is Pillow installed?)")
        else:
            rep(d >= CHANGE_MIN, f"drawing: {d * 100:.1f}% of the canvas changed (needs {CHANGE_MIN * 100:.1f}%)")
        rep(S.cmd_dead == 0 and S.click_dead == 0, "commands and clicks: the command line / prompt reacted every time")
        make_layers()
        rep(S.layers_ok is not False, "layers: -LAYER works (the current-layer row changed)")
        S.menu_ok, S.menu_fail = None, 0
        act_peek_menu()
        rep(S.menu_ok is True, "menu bar: clicking a menu title opens a menu")
        S.saved = False
        save()
        rep(S.saved, "save: Save As dialog found and the project name typed")
    except Interrupted as e:
        log(f"test aborted: {e.reason} (touching the mouse or keyboard stops it)")
        return False
    finally:
        S.in_dialog = False
    ok = bool(results) and all(results)
    log("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED - see the FAIL lines above")
    return ok


def main():
    global last_input, SPEED, TYPING_SPEED, WPM_MAX, TYPO_CHANCE
    if "--calibrate" in sys.argv:
        calibrate()
        return
    test = "--test" in sys.argv
    log("=" * 56)
    log(f"{PAUSE_KEY} pause/resume  |  {QUIT_KEY} quit  |  top-left = failsafe" + ("  |  TEST MODE" if test else ""))
    log("=" * 56)
    if not IS_WIN:
        log("warning: not Windows - dialog detection is disabled")
    if Image is None:
        log("warning: Pillow is missing (pip install pillow) - the screen checks are disabled")
    if IS_WIN and not is_admin():
        log("note: if CADCraft runs as Administrator, Windows ignores this script's clicks and keys - "
            "start both the same way")
    load_calibration()
    if test:
        SPEED, TYPING_SPEED, WPM_MAX, TYPO_CHANCE = 0.35, 0.4, 999, 0.0

    keep_awake(True)
    start_listeners()
    try:
        if not open_app():
            return
        check_monitor()
        self_test_injection()
        last_input = time.time()
        log(f"hands off: starting in about {IDLE_AFTER} s (any mouse or key movement restarts the wait)")
        while not stop.is_set() and time.time() - last_input < IDLE_AFTER:
            time.sleep(0.5)
        if test:
            if startup():
                self_check()
            return
        startup()
        if PROBE_ON_START:
            run_probes()
        next_at = time.time() + IDLE_AFTER

        while not stop.is_set():
            time.sleep(random.uniform(1.3, 2.9))
            if not running.is_set():
                continue
            if time.time() - last_input < IDLE_AFTER or time.time() < next_at:
                if time.time() - last_input >= IDLE_AFTER and random.random() < 0.18:
                    ambient_move()
                continue
            run_one_action()
            gap = min(max(random.lognormvariate(math.log(15), 0.5), GAP_BETWEEN[0]), 70)
            if random.random() < LONG_BREAK_CHANCE:
                gap += random.uniform(*LONG_BREAK)
            next_at = time.time() + gap
    except pyautogui.FailSafeException:
        log("failsafe - stopping")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        release_all()
        keep_awake(False)
        for l in listeners:
            l.stop()
        log("session ended")


if __name__ == "__main__":
    main()