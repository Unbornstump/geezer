"""
cadwriter.py (v2)  -  drawing-presence automation for CADCraft (Windows)

Drives CADCraft like a person drafting: starts commands from the command line (or
sometimes by clicking the Draw tool icons), clicks points on the canvas one at a time,
builds up whole drawings (machined plate, gear, L-bracket, floor plan, truss, flange,
spirals, ellipses, staircase section) with holes, centre lines, offsets and dimensions,
looks around (zoom / pan and back), peeks at menus, opens and closes dialog boxes
(Units, Drafting Settings, Dimension Style, Options, Plot), saves, and every few
drawings clears the canvas with ERASE ALL and starts again.

Same awareness as gridwriter v2: it stops the moment you touch the mouse/keyboard or
another window comes to the front, waits IDLE_AFTER seconds, cancels any half-finished
command with Escape, closes stray dialogs, and goes back to the exact step it was on.

pip install pyautogui pygetwindow pynput pillow
Windows + US keyboard assumed.
F8 = pause/resume   F9 = quit   mouse top-left = emergency stop

FIRST RUN:  python cadwriter.py --test
  A fast self-check: draws a few shapes, opens a dialog, opens a menu, saves, and prints
  PASS / FAIL for every assumption below. Fix the FAILs, then run it normally.

WHAT'S NEW IN v2
  * It looks at the screen. After a few drawing steps it parks the mouse off the canvas and
    compares screenshots. If nothing appeared twice in a row (hidden command line, other
    aliases, wrong window) it PAUSES and says so, instead of typing blindly for hours.
  * It never touches your own work. If CADCraft was already open it starts a NEW drawing
    (Ctrl+N), and ERASE ALL only runs in a drawing it saved itself (after its own Save As);
    otherwise it stops and asks you to confirm with F8.
  * Dialogs: it works out whether they are separate windows or drawn inside the app, never
    presses Enter/Space inside one (so Plot can never actually plot; only the Save prompt is
    accepted), pokes at the Plot dialog's drop-downs and check boxes, then cancels.
    (Fixed: v1 aborted every dialog because it was not yet "inside the dialog" when the
    dialog popped up.)
  * Menu peeks are verified once (a menu must really open) and switched off if not, so a
    mis-aimed click can never land on a toolbar icon again and again.
  * DPI-aware, keeps the PC awake while running, writes cadwriter.log in WORK_DIR, releases
    every key and mouse button on exit, warns if CADCraft is not on the main monitor.

ASSUMPTIONS ABOUT CADCRAFT (check on a first short test run):
  * the window title contains APP_MATCH ("cadcraft")
  * AutoCAD-style command line: typing on the canvas goes to the command line and
    these aliases work: L PL C REC A EL POL DLI O M E U UN DS D ST OP PLOT
    (the command line must be visible - in AutoCAD-like apps Ctrl+9 toggles it)
  * Ctrl+S saves; the first save of "Drawing1" opens a Save As dialog with the
    file-name box focused
  * mouse wheel zooms, middle-button drag pans
  * dialogs are real Win32 windows. If CADCraft draws them inside its main window,
    set DIALOGS_IN_WINDOW = True
  * the window is maximised with the layout of the screenshot (tool sets left, canvas
    in the middle, ViewCube top-right of the canvas). If yours differs, change
    DRAW_AREA and TOOL_ICONS, or set TOOLBAR_CLICK_CHANCE = 0.
"""

import ctypes
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
    try:                                    # Win32 rectangles and pyautogui must agree on scaled displays
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
WORKBOOK      = WORK_DIR / ("session_" + time.strftime("%Y%m%d_%H%M") + FILE_EXT)   # new name every run: no overwrite prompt

DEBUG         = False

IDLE_AFTER    = 11                          # seconds of no real input before it acts again
GAP_BETWEEN   = (8, 35)
LONG_BREAK_CHANCE = 0.09
LONG_BREAK    = (30, 110)

# typing (same model as gridwriter v2)
BASE_DELAY    = 0.40
TYPING_SPEED  = 1.0
WPM_MIN, WPM_MAX = 12, 20
TYPO_CHANCE   = 0.03
MICRO_STOP    = 0.08
KEY_DWELL     = (0.06, 0.15)

# canvas geometry, as fractions of the CADCraft window (left, right, top, bottom).
# Keeps clear of the ViewCube (top right), the tool sets (left) and the status area (bottom).
DRAW_AREA     = (0.19, 0.70, 0.21, 0.88)
ZONE          = (0.18, 0.70, 0.30, 0.85)    # where idle mouse wandering happens
COLS, ROWS    = 2, 2                        # each drawing gets one square slot of this grid
SLOTS         = COLS * ROWS

USE_ALIASES   = True                        # "l" instead of "line"
UNDO_CHANCE   = 0.05                        # now and then: finish a step, undo it with U, redo it

# Draw-panel icons, as fractions of the window (taken from the screenshot).
TOOLBAR_CLICK_CHANCE = 0.25                 # 0 = always use the command line
TOOL_ICONS = {"line": (0.023, 0.281), "pline": (0.055, 0.281),
              "circle": (0.087, 0.281), "rect": (0.119, 0.281)}

# menu bar titles (fractions of window width, y fraction) for "peeking" at menus
MENU_Y = 0.092
MENU_X = [0.017, 0.036, 0.059, 0.084, 0.115, 0.143, 0.169, 0.204, 0.244, 0.279, 0.309]

PAUSE_KEY, QUIT_KEY = "<f8>", "<f9>"
LAUNCH_TIMEOUT = 60

SPEED         = 1.0                         # pacing multiplier (--test lowers it)
NEW_DRAWING_IF_REUSED = True                # CADCraft already open -> Ctrl+N, never draw in your drawing
PROBE_ON_START = True                       # quick dialog / menu check before the first real action
MENU_PEEK     = True                        # glance at menu-bar menus now and then (verified once)
VERIFY_DRAWING = True                       # screenshot checks that drawing really appears
CHANGE_MIN    = 0.004                       # fraction of the canvas that must change after a few steps
CENTER_BOX    = (0.36, 0.72, 0.22, 0.88)    # where dialogs show up (fractions l, r, t, b of the window)
X_BTN         = (24, 23)                    # a dialog's title-bar X: pixels left of / below its top-right corner
LOG_FILE      = WORK_DIR / "cadwriter.log"

# dialogs: opened by typing a command; "tabs" = Tab presses inside; closed by Escape or the title-bar X
DIALOG_CHANCE_WEIGHT = 1.0
DIALOGS_IN_WINDOW = False
CLOSE_BY_CLICK    = True
DIALOGS = {
    "units":     {"alias": "un",   "tabs": (0, 3), "w": 1.0},
    "drafting":  {"alias": "ds",   "tabs": (0, 4), "w": 1.0},
    "dimstyle":  {"alias": "d",    "tabs": (0, 4), "w": 0.8},
    "textstyle": {"alias": "st",   "tabs": (0, 3), "w": 0.6},
    "options":   {"alias": "op",   "tabs": (0, 5), "w": 0.6},
    "plot":      {"alias": "plot", "tabs": (0, 4), "w": 0.8,
                  # Click targets inside the Plot dialog, as fractions of its window (measured on a screenshot).
                  # "list" = drop-down (looked at, then closed with ONE Escape), "tick" = check box / radio.
                  # Nothing here ever presses OK or Preview, and the dialog is always cancelled.
                  "pokes": [(0.36, 0.12, "list"), (0.46, 0.20, "list"), (0.46, 0.27, "list"),
                            (0.25, 0.33, "tick"), (0.40, 0.33, "tick"), (0.35, 0.39, "list"),
                            (0.25, 0.45, "tick"), (0.25, 0.51, "tick"), (0.25, 0.76, "tick"),
                            (0.25, 0.82, "tick"), (0.46, 0.88, "list")]},
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
    """True while a menu (menu bar drop-down, right-click menu) of CADCraft is open."""
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
            return bool(info.flags & 0x4)          # GUI_INMENUMODE
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
        self.saved = False             # has the first Save As been done?
        self.scene = None              # the drawing in progress
        self.next_slot = 0
        self.scenes_done = 0
        self.view = {"zoom": 0, "zpos": None, "pan": (0, 0)}
        self.reused = False            # attached to a CADCraft that was already open
        self.menu_ok = None            # None = not verified yet; True / False after the first menu peek
        self.checks = 0                # drawing checks done so far
        self.dead = 0                  # consecutive checks where nothing appeared
        self.erase_warned = False      # the user was asked before erasing a drawing we did not save


S = State()


class Interrupted(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


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
    """Handle of a dialog / popup that belongs to CADCraft and is open right now, else 0."""
    if not IS_WIN:
        return 0
    main = main_hwnd()
    if not main:
        return 0
    try:
        pop = user32.GetWindow(main, 6)              # GW_ENABLEDPOPUP
        if pop and pop != main and user32.IsWindowVisible(pop):
            return pop
        fg = user32.GetForegroundWindow()
        if fg and fg != main and user32.IsWindowVisible(fg) and pid_of(fg) == pid_of(main):
            return fg
        if menu_open(main):
            return main                              # sentinel: a menu is open
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
        if dialog_hwnd():                         # the Alt tap can leave the menu bar in menu mode
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
    human_move(x, y)
    time.sleep(random.uniform(0.05, 0.2))
    for c in range(clicks):
        guard()
        ms_down(button)
        time.sleep(random.uniform(0.035, 0.09))
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
    """Window-relative fraction -> screen pixel."""
    rc = win_rect(main_hwnd())
    if not rc:
        sw, sh = pyautogui.size()
        return fx * sw, fy * sh
    return rc[0] + fx * (rc[2] - rc[0]), rc[1] + fy * (rc[3] - rc[1])

# ── EYES: tiny screenshot comparisons, so the script notices what really happened ──
def park_mouse():
    """Look away from the canvas: the crosshair and tooltips would otherwise show up in screenshots."""
    human_move(*win_frac(random.uniform(0.04, 0.10), random.uniform(0.50, 0.60)), speed=0.8)
    quiet_nap(random.uniform(0.25, 0.6))

def grab(box, size=(48, 32)):
    """Tiny grey-scale picture of part of the CADCraft window (box = fractions l, r, t, b), or None."""
    if Image is None:
        return None
    try:
        sw, sh = pyautogui.size()
        l, t, r, b = win_rect(main_hwnd()) or (0, 0, sw, sh)
        W, H = r - l, b - t
        x0, y0 = max(int(l + box[0] * W), 0), max(int(t + box[2] * H), 0)
        x1, y1 = min(int(l + box[1] * W), sw), min(int(t + box[3] * H), sh)
        if x1 - x0 < 20 or y1 - y0 < 20:
            return None
        im = pyautogui.screenshot(region=(x0, y0, x1 - x0, y1 - y0)).convert("L")
        return im.resize(size, getattr(Image, "Resampling", Image).BOX).tobytes()
    except Exception:
        return None

def changed(a, b, cell=3):
    """Fraction of cells that differ between two grab()s, or None if either is missing."""
    if not a or not b or len(a) != len(b):
        return None
    return sum(1 for x, y in zip(a, b) if abs(x - y) > cell) / len(a)

def dialog_up(before=None):
    """A dialog is open: a real window, or (in-window mode) the middle of the window changed."""
    if dialog_hwnd():
        return True
    if DIALOGS_IN_WINDOW and before:
        d = changed(before, grab(CENTER_BOX))
        return d is not None and d > 0.12
    return False

def look_canvas():
    park_mouse()
    return grab(DRAW_AREA, (64, 40))

def verify_drawing(before):
    """After a few steps something must have appeared on the canvas. Two blank checks in a row -> pause."""
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
        log("=" * 56)
        log("Nothing is being drawn. Likely causes: the command line is hidden or uses other aliases,")
        log("DRAW_AREA is wrong, or CADCraft is not the window being driven.")
        log("Fix it, then press F8 to carry on (or F9 to quit).")
        log("=" * 56)
        S.dead = 0
        running.clear()
        raise Interrupted("verify")
    return False

def alive(h):
    try:
        return bool(h) and bool(user32.IsWindow(h)) and bool(user32.IsWindowVisible(h))
    except Exception:
        return False

def release_all():
    """Never leave a key or a mouse button held down."""
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
    """Stop Windows from sleeping or blanking the screen while this runs (undone on exit)."""
    if not IS_WIN:
        return
    try:
        flags = 0x80000000 | ((0x1 | 0x2) if on else 0)   # ES_CONTINUOUS | SYSTEM_REQUIRED | DISPLAY_REQUIRED
        ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
    except Exception:
        pass


def drift():
    x, y = pyautogui.position()
    x0, x1, y0, y1 = zone_rect()
    human_move(
        min(max(x + random.randint(-38, 38), x0), x1),
        min(max(y + random.randint(-22, 22), y0), y1),
        0.55
    )

def wander():
    """Mouse-only wandering. No wheel here: scrolling would zoom the drawing."""
    if random.random() < 0.65:
        drift()
    else:
        human_move(*zone_point(bias_left=random.random() < 0.5),
                   speed=random.uniform(0.5, 1.1))

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
    """Square slot i of the grid: (left, top, side). Equal axes so circles stay round."""
    l, t, r, b = draw_area_px()
    cw, ch = (r - l) / COLS, (b - t) / ROWS
    s = min(cw, ch) * 0.9
    col, row = i % COLS, i // COLS
    cx, cy = l + (col + 0.5) * cw, t + (row + 0.5) * ch
    return cx - s / 2, cy - s / 2, s

def px(x, y):
    """Unit-square drawing coordinates (0..1) of the current drawing -> screen pixels."""
    sx, sy, s = slot_square(S.scene["slot"])
    return sx + x * s, sy + y * s

def polar(cx, cy, r, ang):
    return cx + r * math.cos(ang), cy + r * math.sin(ang)


# ───────────────────────── DRAWINGS (lists of steps in unit coordinates) ─────────────────────────
# step kinds: line, pline, rect, circle, arc, ellipse, polygon, dim, offset, move

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
    st = [("rect", (0.06, 0.14), (0.94, 0.86)),
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
    return st

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

SCENES = {
    "plate": (sc_plate, 1.3), "gear": (sc_gear, 1.0), "bracket": (sc_bracket, 1.1),
    "floorplan": (sc_floorplan, 1.0), "truss": (sc_truss, 0.9), "flange": (sc_flange, 1.0),
    "spiral": (sc_spiral, 0.7), "orbit": (sc_orbit, 0.7), "stairs": (sc_stairs, 0.8),
}


# ───────────────────────── COMMANDS AND STEPS ─────────────────────────
ALIAS = {"line": ("line", "l"), "pline": ("pline", "pl"), "circle": ("circle", "c"),
         "rect": ("rectang", "rec"), "arc": ("arc", "a"), "ellipse": ("ellipse", "el"),
         "polygon": ("polygon", "pol"), "dim": ("dimlinear", "dli"), "offset": ("offset", "o"),
         "move": ("move", "m"), "erase": ("erase", "e"), "undo": ("undo", "u")}

def start_cmd(kind):
    full, short = ALIAS[kind]
    if kind in TOOL_ICONS and random.random() < TOOLBAR_CLICK_CHANCE:
        x, y = win_frac(*TOOL_ICONS[kind])
        human_click(x + random.uniform(-3, 3), y + random.uniform(-3, 3))
        nap(random.uniform(0.5, 1.1))
        return
    say(short if USE_ALIASES else full)

def click_at(x, y):
    X, Y = px(x, y)
    human_move(X, Y, speed=random.uniform(0.55, 1.1))
    if random.random() < 0.35:
        think(0.25, 1.0)
    human_click(X, Y)
    nap(random.uniform(0.2, 0.8))

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
    elif k == "arc":
        start_cmd("arc")
        for p in st[1:4]:
            click_at(*p)
    elif k == "ellipse":
        start_cmd("ellipse")
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
        say("t")                                  # "through point" option: no distance needed
        click_at(*st[1])
        click_at(*st[2])
        hit("enter")
    elif k == "move":
        start_cmd("move")
        click_at(-0.03, -0.03)                    # window-select the whole drawing
        click_at(1.03, 1.03)
        hit("enter")
        nap(random.uniform(0.3, 0.8))
        click_at(0.5, 0.5)
        click_at(0.5 + st[1][0], 0.5 + st[1][1])
    nap(random.uniform(0.4, 1.1))

def undo_redo(st):
    """Looks at the result, undoes it with U, then does it again."""
    think(1.0, 2.5)
    say("u")
    think(0.6, 1.6)
    do_step(st)


# ───────────────────────── DRAWING FLOW ─────────────────────────
def reset_drawing():
    if not S.saved and not S.erase_warned:
        S.erase_warned = True
        log("=" * 56)
        log("The canvas is full and needs clearing, but this drawing was not created and saved by me")
        log("(no Save As has happened), so it could contain your own work. ERASE ALL has NOT been sent.")
        log("Press F8 to confirm that clearing it is fine, or F9 to quit.")
        log("=" * 56)
        running.clear()
        raise Interrupted("unsafe")
    log("clearing the canvas")
    start_cmd("erase")
    say("all")
    hit("enter")
    nap(0.8)
    S.next_slot = 0
    S.scene = None
    save()


def new_scene():
    if S.next_slot >= SLOTS:
        reset_drawing()
    names = [n for n in SCENES if n not in S.recent[-3:]] or list(SCENES)
    name = random.choices(names, [SCENES[n][1] for n in names])[0]
    steps = SCENES[name][0]()
    if random.random() < 0.25:
        steps.append(("move", (random.choice([-1, 1]) * 0.03, random.choice([-1, 1]) * 0.02)))
    S.recent.append(name)
    S.scene = {"name": name, "steps": steps, "i": 0, "slot": S.next_slot}
    S.next_slot += 1
    log(f"new drawing: {name} (slot {S.scene['slot'] + 1}/{SLOTS}, {len(steps)} steps)")

def work_scene():
    if not S.scene:
        new_scene()
    sc = S.scene
    check = VERIFY_DRAWING and (S.checks < 3 or random.random() < 0.25)
    before = look_canvas() if check else None
    todo = random.randint(2, 4)
    done = 0
    while sc["i"] < len(sc["steps"]) and done < todo:
        st = sc["steps"][sc["i"]]
        do_step(st)
        if st[0] != "move" and random.random() < UNDO_CHANCE:
            undo_redo(st)
        sc["i"] += 1
        done += 1
        nap(random.uniform(0.4, 1.3))
        if random.random() < 0.14:
            think(1.0, 3.2)
        if random.random() < 0.08:
            save()
    if check and before is not None:
        verify_drawing(before)
    if sc["i"] >= len(sc["steps"]):
        S.scene = None
        S.scenes_done += 1
        log(f"finished '{sc['name']}' ({S.scenes_done} so far)")
    save()
    w = typing_wpm()
    if w:
        log(f"typing ~ {w:.0f} wpm (while typing, pauses excluded)")


# ───────────────────────── SAVE / CLEANUP / VIEW ─────────────────────────
def save():
    before = grab(CENTER_BOX) if DIALOGS_IN_WINDOW else None
    hit("ctrl", "s")
    quiet_nap(random.uniform(0.8, 1.4))
    if not dialog_up(before):
        return
    prev = S.in_dialog
    S.in_dialog = True
    try:
        if not S.saved:
            log("first save: Save As dialog")
            quiet_nap(random.uniform(0.4, 0.9))
            hit("ctrl", "a")
            nap(0.3)
            type_text(str(WORKBOOK), typos=False)
            nap(random.uniform(0.4, 0.9))
            hit("enter")
            quiet_nap(random.uniform(0.8, 1.5))
            S.saved = True
        if dialog_up(before):                 # e.g. a confirmation prompt: the ONLY place Enter is accepted
            log("save opened a prompt")
            close_dialogs(accept=True, assume_open=(DIALOGS_IN_WINDOW and not dialog_hwnd()))
    finally:
        S.in_dialog = prev


def heal():
    log("healing after interruption")
    if dialog_hwnd():
        close_dialogs(accept=False)
    for _ in range(3):
        hit("escape")                         # cancels whatever command was half-way
        nap(0.2)

def focus_canvas():
    hit("escape")
    nap(0.15)
    human_click(*zone_point())
    nap(0.25)
    hit("escape")
    nap(0.15)
    hit("escape")

def restore_view():
    """If a previous zoom / pan was cut short, put the view back the way it was."""
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
    x, y = rect_point(rc, 0.01, 0.32, 0.075, 0.105)
    human_move(x, y)
    think(0.3, 1.1)

def wait_dialog(timeout):
    end = time.time() + timeout
    while time.time() < end:
        h = dialog_hwnd()
        if h:
            return h
        quiet_nap(0.15)
    return 0

def close_dialogs(accept=False, assume_open=False):
    """Close whatever dialog / menu is open, with mouse movement and a key press or a click on the X."""
    prev = S.in_dialog
    S.in_dialog = True
    try:
        for attempt in range(4):
            h = dialog_hwnd()
            if not h and not (assume_open and attempt == 0):
                break
            is_menu = bool(h) and h == main_hwnd()
            rc = None if is_menu else (win_rect(h) if h else win_rect(main_hwnd()))
            quiet_nap(random.uniform(0.3, 0.9))
            if rc:
                human_move(*rect_point(rc, 0.25, 0.85, 0.3, 0.9))
                quiet_nap(random.uniform(0.3, 1.0))
            if is_menu:
                kb_press("escape")
            elif attempt == 0:
                if CLOSE_BY_CLICK and rc and h and not accept and random.random() < 0.45:
                    human_click(rc[2] - X_BTN[0], rc[1] + X_BTN[1])
                else:
                    kb_press("enter" if accept else "escape")
            elif attempt == 1:
                kb_press("escape")
            elif attempt == 2 and h and h != main_hwnd() and IS_WIN:
                user32.PostMessageW(h, 0x0010, 0, 0)          # WM_CLOSE as a last resort
            quiet_nap(random.uniform(0.5, 0.9))
            if assume_open and not h:
                break
        if not app_ok():
            activate_main()
            quiet_nap(0.6)
    finally:
        S.in_dialog = prev

def act_dialog():
    name = random.choices(list(DIALOGS), [DIALOGS[n].get("w", 1.0) for n in DIALOGS])[0]
    spec = DIALOGS[name]
    log(f"dialog: {name}")
    think(0.5, 1.6)
    hover_menu_bar()
    nap(random.uniform(0.15, 0.5))
    hwnd = 0
    try:
        S.in_dialog = True          # BEFORE the command: the dialog pops up while say() is still napping
        say(spec["alias"])
        hwnd = wait_dialog(2.5)
        if not hwnd and not DIALOGS_IN_WINDOW:
            log("no dialog window detected - cancelling")
            kb_press("escape")
            return
        quiet_nap(random.uniform(0.5, 1.2))
        rc = win_rect(hwnd) if hwnd else win_rect(main_hwnd())
        if rc:
            human_move(*rect_point(rc, 0.2, 0.8, 0.25, 0.75))
            think(0.6, 1.8)
        for _ in range(random.randint(*spec["tabs"])):
            hit("tab")
            time.sleep(random.uniform(0.3, 0.8))
            if rc and random.random() < 0.3:
                human_move(*rect_point(rc, 0.1, 0.9, 0.2, 0.9), speed=0.8)
        pokes = spec.get("pokes")
        if pokes and hwnd:
            for fx, fy, kind in random.sample(pokes, min(len(pokes), random.randint(1, 3))):
                if not alive(hwnd):                    # the dialog is gone: never click on the canvas by mistake
                    break
                rc = win_rect(hwnd)
                if not rc:
                    break
                x = rc[0] + fx * (rc[2] - rc[0]) + random.uniform(-4, 4)
                y = rc[1] + fy * (rc[3] - rc[1]) + random.uniform(-3, 3)
                human_click(x, y)
                quiet_nap(random.uniform(0.5, 1.2))
                if kind == "list":
                    human_move(x + random.uniform(-30, 30), y + random.uniform(30, 110), speed=0.8)
                    quiet_nap(random.uniform(0.5, 1.4))
                    kb_press("escape")                 # closes the list only; never Enter (it would pick an item)
                    quiet_nap(random.uniform(0.5, 0.9))
                else:
                    think(0.4, 1.0)
        if rc and random.random() < 0.6 and (not hwnd or alive(hwnd)):
            human_move(*rect_point(rc, 0.5, 0.95, 0.7, 0.95))
            think(0.4, 1.3)
        close_dialogs(accept=False, assume_open=(not hwnd and DIALOGS_IN_WINDOW))
    finally:
        S.in_dialog = False


# ───────────────────────── OTHER ACTIONS ─────────────────────────
def act_look_around():
    """Zoom in and back out, or pan and back, like checking a detail."""
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
    """Open a menu-bar menu, glance across a couple of menus, close it. Checked the first time."""
    if not MENU_PEEK or S.menu_ok is False:
        return mouse_idle()
    S.in_dialog = True
    try:
        i = random.randrange(len(MENU_X))
        below = (0.0, 0.34, 0.115, 0.55)               # where a drop-down appears
        before = grab(below, (40, 30)) if S.menu_ok is None else None
        human_click(*win_frac(MENU_X[i], MENU_Y))
        quiet_nap(random.uniform(0.5, 1.2))
        if S.menu_ok is None:
            d = changed(before, grab(below, (40, 30)))
            if d is not None:
                S.menu_ok = d > 0.04
                log("menu peek verified: a menu opens where I click" if S.menu_ok else
                    "menu peek: nothing opened where I clicked - switched off (check MENU_Y / MENU_X)")
        if S.menu_ok is not False:
            for _ in range(random.randint(0, 2)):
                j = min(max(i + random.choice([-1, 1]), 0), len(MENU_X) - 1)
                human_move(*win_frac(MENU_X[j], MENU_Y), speed=0.8)
                quiet_nap(random.uniform(0.4, 1.0))
            human_move(*win_frac(MENU_X[i] + random.uniform(-0.01, 0.02), MENU_Y + random.uniform(0.04, 0.2)), speed=0.7)
            quiet_nap(random.uniform(0.5, 1.4))
        hit("escape")                                   # closes the menu (or whatever a stray click opened)
        quiet_nap(random.uniform(0.3, 0.7))
        close_dialogs(accept=False)
    finally:
        S.in_dialog = False


def act_hover_panels():
    """Mouse over the Draw tools / Properties panel, as if choosing something."""
    fx0, fx1, fy0, fy1 = random.choice([(0.01, 0.14, 0.2, 0.4), (0.835, 0.98, 0.55, 0.95), (0.835, 0.98, 0.15, 0.35)])
    for _ in range(random.randint(1, 3)):
        human_move(*win_frac(random.uniform(fx0, fx1), random.uniform(fy0, fy1)))
        think(0.5, 1.6)
    human_move(*zone_point())

def act_select_window():
    """Window-select part of a finished drawing, look at it, deselect."""
    if S.next_slot == 0:
        return work_scene()
    slot = random.randrange(S.next_slot if not S.scene else max(S.next_slot - 1, 1))
    held = S.scene
    S.scene = {"slot": slot}
    try:
        click_at(-0.03, -0.03)
        click_at(random.uniform(0.5, 1.03), random.uniform(0.5, 1.03))
        think(1.2, 3.0)
    finally:
        S.scene = held
    hit("escape")
    nap(0.3)
    hit("escape")

def mouse_idle():
    for _ in range(random.randint(1, 4)):
        human_move(*zone_point())
        think(0.4, 1.4)


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
        restore_view()
        focus_canvas()
        S.fresh = False

        if random.random() < 0.5:
            human_move(*zone_point())
            time.sleep(random.uniform(0.15, 0.6))

        if S.scene and random.random() < 0.75:
            work_scene()
        else:
            r = random.random()
            dlg = 0.08 * DIALOG_CHANCE_WEIGHT
            if r < 0.50:
                work_scene()
            elif r < 0.58:
                act_look_around()
            elif r < 0.64:
                act_select_window()
            elif r < 0.64 + dlg:
                act_dialog()
            elif r < 0.78:
                act_peek_menu()
            elif r < 0.86:
                act_hover_panels()
            elif r < 0.95:
                mouse_idle()
            else:
                think(4.0, 11.0)

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
    rc = win_rect(main_hwnd())
    if not rc:
        return
    sw, sh = pyautogui.size()
    if rc[0] < -12 or rc[1] < -12 or rc[2] > sw + 12 or rc[3] > sh + 12:
        log(f"warning: the CADCraft window {rc} is not fully on the primary screen ({sw}x{sh}). "
            "Move it to the main monitor and maximise it: other monitors are not supported.")

def ensure_own_drawing():
    """If CADCraft was already open, work in a brand-new drawing so nothing of yours is ever touched."""
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
        quiet_nap(1.0)
    finally:
        S.in_dialog = False

def startup():
    global action_started
    action_started = time.time()
    try:
        if not bring_back():
            log("could not bring CADCraft forward")
            return False
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
    """Open Units, find out how dialogs show up (separate window / drawn inside the app), close it again."""
    global DIALOGS_IN_WINDOW
    park_mouse()
    before = grab(CENTER_BOX)
    S.in_dialog = True
    try:
        say("un")
        h = wait_dialog(2.5)
        mode = "window" if h else None
        if not h:
            quiet_nap(1.0)
            d = changed(before, grab(CENTER_BOX))
            if d is not None and d > 0.12:
                mode = "inside"
        if mode == "window":
            log("dialogs: separate windows - detection works")
            close_dialogs(accept=False)
        elif mode == "inside":
            DIALOGS_IN_WINDOW = True
            log("dialogs: drawn inside the CADCraft window - DIALOGS_IN_WINDOW switched on for this run")
            close_dialogs(accept=False, assume_open=True)
        else:
            log("dialogs: nothing opened after UN - the command line or its aliases may not work")
            hit("escape")
        return mode
    finally:
        S.in_dialog = False

def run_probes():
    """Quiet start-up checks of dialogs and menus (their results switch features on or off)."""
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
        new_scene()                                     # 1. drawing: command line, aliases, clicks
        sc = S.scene
        before = look_canvas()
        for st in sc["steps"][:4]:
            do_step(st)
            sc["i"] += 1
        d = changed(before, look_canvas())
        if d is None:
            log("INFO  could not compare screenshots (is Pillow installed?)")
        else:
            rep(d >= CHANGE_MIN, f"drawing: {d * 100:.1f}% of the canvas changed (needs {CHANGE_MIN * 100:.1f}%)")
        mode = probe_dialog()                           # 2. dialogs
        rep(mode is not None, f"dialogs: opened and closed ({mode or 'nothing opened'})")
        S.menu_ok = None                                # 3. menu bar
        act_peek_menu()
        rep(S.menu_ok is True, "menu bar: clicking a menu title opens a menu")
        save()                                          # 4. Save As
        rep(S.saved, "save: Save As dialog found and the file name typed")
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
    test = "--test" in sys.argv
    log("=" * 56)
    log(f"{PAUSE_KEY} pause/resume  |  {QUIT_KEY} quit  |  top-left = failsafe" + ("  |  TEST MODE" if test else ""))
    log("=" * 56)
    if not IS_WIN:
        log("warning: not Windows - dialog detection is disabled")
    if Image is None:
        log("warning: Pillow is missing (pip install pillow) - the screen checks are disabled")
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