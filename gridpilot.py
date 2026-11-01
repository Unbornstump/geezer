"""
gridwriter.py (v2)  -  spreadsheet-presence automation for GridCraft (Windows)

Drives GridCraft: fills the sheet with mathematical tables and formulas, edits
inputs so results recalculate, reviews formulas, selects ranges, scrolls, saves,
and now also opens / uses / closes dialog boxes (Find, Go To, Replace, Format)
with ordinary mouse movement and key presses.

What changed in v2
  * Typing is much slower and more uneven (target ~30-38 WPM measured, see the
    log line "typing ~ N wpm"). Keys are held for a realistic dwell time, Shift
    is held while a shifted key is pressed, and symbols / brackets cost extra.
  * Awareness: it knows when GridCraft is not in front, when a dialog is open
    that it did not open, and when you touched the mouse/keyboard. It stops at
    once, waits IDLE_AFTER seconds, closes stray dialogs the way a person would,
    re-syncs the cell cursor with Ctrl+Home and goes back to the exact row it
    was typing.
  * Dialogs: opens one with its shortcut (after hovering near the menu bar),
    moves the mouse around inside it, types into it, then closes it by Escape,
    Enter, or clicking the title-bar X. While a dialog is open nothing is typed
    into the grid.

pip install pyautogui pygetwindow pynput
Windows + US keyboard assumed.
F8 = pause/resume   F9 = quit   mouse top-left = emergency stop

ASSUMPTIONS ABOUT GRIDCRAFT (check on a first short test run):
  * it accepts a file path as a command-line argument
  * the window title contains APP_MATCH
  * Excel-style keys: arrows move the cell, typing starts editing, an arrow
    key commits the edit, Ctrl+Home = A1, Ctrl+A = select all, Ctrl+S = save,
    Ctrl+F / Ctrl+G / Ctrl+H / Ctrl+1 open Find / Go To / Replace / Format
  * dialogs are real Win32 windows. If GridCraft draws them inside its main
    window, set DIALOGS_IN_WINDOW = True (see below)
"""

import ctypes
import logging
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from pathlib import Path

import pyautogui
import pygetwindow as gw
from pynput import keyboard, mouse

IS_WIN = sys.platform == "win32"
if IS_WIN:
    from ctypes import wintypes

# ───────────────────────── CONFIG ─────────────────────────
WORK_DIR      = Path(r"C:\work")
APP_EXE       = None                        # e.g. r"C:\work\GridCraft\GridCraft.exe"; None = auto-find
CLI_EXE       = None                        # e.g. r"C:\work\gridcraft-cli.exe"; None = auto-find
APP_MATCH     = None                        # text in the window title; None = the workbook name
FILE_EXT      = ".xlsx"
WORKBOOK      = WORK_DIR / ("analysis_notes" + FILE_EXT)
APP_MATCH     = (APP_MATCH or WORKBOOK.stem).lower()

DEBUG         = False
MAX_BLOCKS    = 12
BOLD_HEADERS  = False
USE_F2        = True

IDLE_AFTER    = 11                          # seconds of no real input before it acts again
GAP_BETWEEN   = (6, 24)
LONG_BREAK_CHANCE = 0.09
LONG_BREAK    = (30, 110)

# typing. BASE_DELAY is the median time between key presses (seconds).
# 0.25 + symbol/shift penalties + pauses lands around 30-38 WPM.
# Raise TYPING_SPEED (e.g. 1.2) to be slower, lower it (0.9) to be faster.
BASE_DELAY    = 0.25
TYPING_SPEED  = 1.0
TYPO_CHANCE   = 0.025
MICRO_STOP    = 0.06
KEY_DWELL     = (0.045, 0.12)               # how long a key is held down

ZONE = (0.15, 0.85, 0.45, 0.85)

SLOT_COLS     = [0, 8, 16]
PAUSE_KEY, QUIT_KEY = "<f8>", "<f9>"
LAUNCH_TIMEOUT = 60

# dialogs
DIALOG_CHANCE_WEIGHT = 1.0
DIALOGS_IN_WINDOW = False       # True if GridCraft's dialogs are not separate windows
CLOSE_BY_CLICK    = True        # sometimes close a dialog by clicking the title-bar X
DIALOGS = {
    # keys: shortcut; text: what to type ("word", "cell" or None); enter: (lo, hi) Enter presses
    # inside the dialog; tabs: (lo, hi) Tab presses; accept: close with Enter instead of Escape
    "find":    {"keys": ("ctrl", "f"), "text": "word", "enter": (0, 2), "tabs": (0, 0), "accept": False, "w": 1.2},
    "goto":    {"keys": ("ctrl", "g"), "text": "cell", "enter": (0, 0), "tabs": (0, 0), "accept": True,  "w": 1.0},
    "replace": {"keys": ("ctrl", "h"), "text": "word", "enter": (0, 0), "tabs": (0, 2), "accept": False, "w": 0.6},
    "format":  {"keys": ("ctrl", "1"), "text": None,   "enter": (0, 0), "tabs": (1, 5), "accept": False, "w": 0.8},
}
FIND_WORDS = ["sum", "mean", "slope", "root", "error", "area", "det", "pi est", "irr", "payment"]

# navigation
# The Name Box is the "B14" box at the top-left of the sheet. Once its position is known, every
# table row starts with an absolute jump (click Name Box, type the address, Enter), so a dropped
# arrow key can never push a row into the wrong column.
# Calibrate: while the script runs, hover the mouse over the Name Box and press F7.
# Or set the pixel offset from the window's top-left corner by hand, e.g. (70, 38).
NAME_BOX_OFFSET = None
CALIBRATE_KEY = "<f7>"

# ───────────────────────── LOGGING / SAFETY ─────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.info
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0

# ───────────────────────── WIN32 HELPERS ─────────────────────────
if IS_WIN:
    # Private copy of user32: setting argtypes on the shared ctypes.windll.user32
    # would break pygetwindow, which uses that same object with its own RECT type.
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
    """True while a menu (right-click menu, menu bar drop-down) of GridCraft is open."""
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

# ───────────────────────── CELL ADDRESS HELPERS ─────────────────────────
def col_name(c):
    s, c = "", c + 1
    while c:
        c, rem = divmod(c - 1, 26)
        s = chr(65 + rem) + s
    return s

def ad(r, c, ar=False, ac=False):
    """0-based (row, col) -> 'B$7' style address."""
    return f"{'$' if ac else ''}{col_name(c)}{'$' if ar else ''}{r + 1}"

def is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False

# ───────────────────────── TABLE GENERATORS ─────────────────────────
def blk_trig(r0, c0):
    step = random.choice([10, 15, 30])
    n = random.randint(7, 11)
    rows = [["deg", "rad", "sin", "cos", "tan", "s2+c2"]]
    for i in range(n):
        r = r0 + len(rows)
        d, b = ad(r, c0), ad(r, c0 + 1)
        rows.append([str(i * step), f"=RADIANS({d})", f"=ROUND(SIN({b}),4)",
                     f"=ROUND(COS({b}),4)", f"=ROUND(TAN({b}),4)",
                     f"=ROUND(SIN({b})^2+COS({b})^2,6)"])
    return rows, [], True

def blk_stats(r0, c0):
    n = random.randint(8, 12)
    slope = random.choice([1.5, 2.0, 2.4, 3.1, -1.8])
    icpt = random.randint(2, 20)
    xs = [round(random.uniform(1, 20), 1) for _ in range(n)]
    ys = [round(slope * x + icpt + random.gauss(0, 2.5), 1) for x in xs]
    rows = [["x_obs", "y_obs"]] + [[str(x), str(y)] for x, y in zip(xs, ys)]
    X = f"{ad(r0 + 1, c0, True, True)}:{ad(r0 + n, c0, True, True)}"
    Y = f"{ad(r0 + 1, c0 + 1, True, True)}:{ad(r0 + n, c0 + 1, True, True)}"
    rows.append([])
    summary = [("mean x", f"=AVERAGE({X})"), ("median y", f"=MEDIAN({Y})"),
               ("stdev x", f"=STDEV({X})"), ("var y", f"=VAR({Y})"),
               ("corr", f"=CORREL({X},{Y})"), ("slope", f"=SLOPE({Y},{X})"),
               ("intercept", f"=INTERCEPT({Y},{X})"), ("r squared", f"=RSQ({Y},{X})"),
               ("pred x=10", f"=FORECAST(10,{Y},{X})")]
    keep = sorted(random.sample(range(len(summary)), random.randint(5, 8)))
    for k in keep:
        rows.append(list(summary[k]))
    edits = [(1 + i, j) for i in range(n) for j in (0, 1)]
    return rows, edits, True

def blk_integral(r0, c0):
    kind = random.choice(["gauss", "sine", "square"])
    if kind == "gauss":
        b, fx, exact = 2.0, (lambda x: f"=EXP(-({x}^2))"), "=SQRT(PI())/2*ERF(2)"
    elif kind == "sine":
        b, fx, exact = 3.0, (lambda x: f"=SIN({x})"), "=1-COS(3)"
    else:
        b, fx, exact = 2.0, (lambda x: f"={x}^2"), "=2^3/3"
    h = 0.25
    n = int(b / h)
    rows = [["x", "f(x)", "trapezoid"]]
    for i in range(n + 1):
        r = r0 + len(rows)
        x, fc = ad(r, c0), ad(r, c0 + 1)
        row = [f"{i * h:g}", fx(x)]
        if i < n:
            row.append(f"=({fc}+{ad(r + 1, c0 + 1)})/2*({ad(r + 1, c0)}-{x})")
        rows.append(row)
    rows.append([])
    ar = r0 + len(rows)
    rows.append(["area", f"=SUM({ad(r0 + 1, c0 + 2)}:{ad(r0 + n, c0 + 2)})"])
    rows.append(["exact", exact])
    rows.append(["abs err", f"=ABS({ad(ar, c0 + 1)}-{ad(ar + 1, c0 + 1)})"])
    return rows, [], True

def blk_newton(r0, c0):
    a = random.choice([2, 3, 5, 7, 10, 11])
    x0 = random.choice(["1", "2", "1.5", "3"])
    rows = [["target a", str(a)], ["iter", "x_n", "f(x_n)", "err"]]
    A = ad(r0, c0 + 1, True, True)
    for i in range(7):
        r = r0 + len(rows)
        x, xp = ad(r, c0 + 1), ad(r - 1, c0 + 1)
        xv = x0 if i == 0 else f"={xp}-({xp}^2-{A})/(2*{xp})"
        rows.append([str(i), xv, f"={x}^2-{A}", f"=ABS({x}-SQRT({A}))"])
    return rows, [(0, 1)], False

def blk_taylor(r0, c0):
    xv = random.choice(["0.5", "1", "1.5", "2", "-1"])
    fn = random.choice(["exp", "cos", "sin"])
    rows = [["x value", xv], ["n", "term", "partial", "error"]]
    X = ad(r0, c0 + 1, True, True)
    first = r0 + len(rows)
    for n in range(random.randint(6, 9)):
        r = r0 + len(rows)
        nn = ad(r, c0)
        if fn == "exp":
            term, truth = f"={X}^{nn}/FACT({nn})", f"EXP({X})"
        elif fn == "cos":
            term, truth = f"=(-1)^{nn}*{X}^(2*{nn})/FACT(2*{nn})", f"COS({X})"
        else:
            term, truth = f"=(-1)^{nn}*{X}^(2*{nn}+1)/FACT(2*{nn}+1)", f"SIN({X})"
        part = f"=SUM({ad(first, c0 + 1, True, False)}:{ad(r, c0 + 1)})"
        rows.append([str(n), term, part, f"=ABS({ad(r, c0 + 2)}-{truth})"])
    return rows, [(0, 1)], False

def blk_matrix(r0, c0):
    rows = [["c1", "c2", "c3", "row sum"]]
    for _ in range(3):
        r = r0 + len(rows)
        vs = [random.randint(-5, 9) for _ in range(3)]
        rows.append([str(v) for v in vs] + [f"=SUM({ad(r, c0)}:{ad(r, c0 + 2)})"])
    rows.append([f"=SUM({ad(r0 + 1, c0 + j)}:{ad(r0 + 3, c0 + j)})" for j in range(4)])
    M = f"{ad(r0 + 1, c0, True, True)}:{ad(r0 + 3, c0 + 2, True, True)}"
    rows.append([])
    rows.append(["det", f"=MDETERM({M})"])
    rows.append(["trace", f"={ad(r0 + 1, c0)}+{ad(r0 + 2, c0 + 1)}+{ad(r0 + 3, c0 + 2)}"])
    rows.append(["norm", f"=SQRT(SUMSQ({M}))"])
    rows.append(["max", f"=MAX({M})"])
    edits = [(1 + i, j) for i in range(3) for j in range(3)]
    return rows, edits, True

def blk_growth(r0, c0):
    a = random.choice([50, 120, 200])
    g = random.choice([0.12, 0.2, 0.35])
    n = random.randint(7, 10)
    rows = [["t", "value", "ln value"]]
    for t in range(n):
        r = r0 + len(rows)
        y = round(a * (1 + g) ** t * random.uniform(0.95, 1.05), 1)
        rows.append([str(t), str(y), f"=LN({ad(r, c0 + 1)})"])
    T = f"{ad(r0 + 1, c0, True, True)}:{ad(r0 + n, c0, True, True)}"
    L = f"{ad(r0 + 1, c0 + 2, True, True)}:{ad(r0 + n, c0 + 2, True, True)}"
    rows.append([])
    sr = r0 + len(rows)
    rows.append(["slope", f"=SLOPE({L},{T})"])
    rows.append(["growth rate", f"=EXP({ad(sr, c0 + 1)})-1"])
    rows.append(["start", f"=EXP(INTERCEPT({L},{T}))"])
    rows.append(["r squared", f"=RSQ({L},{T})"])
    edits = [(1 + i, 1) for i in range(n)]
    return rows, edits, True

def blk_finance(r0, c0):
    P = random.choice([150000, 250000, 320000])
    rate = random.choice([0.035, 0.045, 0.052])
    m = random.choice([180, 240, 300, 360])
    rows = [["principal", str(P)], ["annual rate", str(rate)], ["months", str(m)]]
    pr, rt, mo = (ad(r0 + i, c0 + 1, True, True) for i in range(3))
    rows.append(["payment", f"=PMT({rt}/12,{mo},-{pr})"])
    pay = ad(r0 + 3, c0 + 1, True, True)
    rows.append(["total paid", f"={pay}*{mo}"])
    rows.append(["interest", f"={ad(r0 + 4, c0 + 1)}-{pr}"])
    rows.append([])
    cf0 = random.choice([8000, 10000, 12000])
    first = r0 + len(rows)
    rows.append(["year 0", str(-cf0)])
    for y in range(1, 6):
        rows.append([f"year {y}", str(int(round(cf0 * random.uniform(0.22, 0.42), -1)))])
    last = r0 + len(rows) - 1
    rows.append(["npv 8pct", f"=NPV(0.08,{ad(first + 1, c0 + 1)}:{ad(last, c0 + 1)})+{ad(first, c0 + 1)}"])
    rows.append(["irr", f"=IRR({ad(first, c0 + 1)}:{ad(last, c0 + 1)})"])
    return rows, [(0, 1), (1, 1)], False

def blk_binom(r0, c0):
    n = random.choice([8, 10, 12])
    p = random.choice([0.2, 0.3, 0.45, 0.6])
    rows = [["trials n", str(n)], ["prob p", str(p)], ["k", "P(X=k)", "cum P", "check"]]
    N, Pp = ad(r0, c0 + 1, True, True), ad(r0 + 1, c0 + 1, True, True)
    first = r0 + len(rows)
    for k in range(n + 1):
        r = r0 + len(rows)
        kk = ad(r, c0)
        rows.append([str(k), f"=BINOMDIST({kk},{N},{Pp},FALSE)",
                     f"=SUM({ad(first, c0 + 1, True, False)}:{ad(r, c0 + 1)})",
                     f"=BINOMDIST({kk},{N},{Pp},TRUE)"])
    rows.append([])
    rows.append(["mean", f"={N}*{Pp}"])
    vr = r0 + len(rows)
    rows.append(["variance", f"={N}*{Pp}*(1-{Pp})"])
    rows.append(["std dev", f"=SQRT({ad(vr, c0 + 1)})"])
    return rows, [(1, 1)], False

def blk_numtheory(r0, c0):
    rows = [["n", "n!", "C(12,n)", "lngamma", "gcd", "mod 7"]]
    for n in range(1, random.randint(8, 11) + 1):
        r = r0 + len(rows)
        nn = ad(r, c0)
        rows.append([str(n), f"=FACT({nn})", f"=COMBIN(12,{nn})",
                     f"=ROUND(GAMMALN({nn}+1),4)", f"=GCD({nn},12)", f"=MOD({nn}^2,7)"])
    return rows, [], True

def blk_monte(r0, c0):
    rows = [["rx", "ry", "inside"]]
    n = random.randint(10, 15)
    for _ in range(n):
        r = r0 + len(rows)
        a, b = ad(r, c0), ad(r, c0 + 1)
        rows.append(["=RAND()", "=RAND()", f"=IF({a}^2+{b}^2<=1,1,0)"])
    ins = f"{ad(r0 + 1, c0 + 2, True, True)}:{ad(r0 + n, c0 + 2, True, True)}"
    rows.append([])
    rows.append(["pi est", f"=4*SUM({ins})/COUNT({ins})"])
    return rows, [], True

def blk_fib(r0, c0):
    rows = [["i", "fib", "ratio", "gold err"]]
    for i in range(random.randint(9, 12)):
        r = r0 + len(rows)
        f = "1" if i < 2 else f"={ad(r - 1, c0 + 1)}+{ad(r - 2, c0 + 1)}"
        ratio = "" if i < 1 else f"={ad(r, c0 + 1)}/{ad(r - 1, c0 + 1)}"
        err = "" if i < 1 else f"=ABS({ad(r, c0 + 2)}-(1+SQRT(5))/2)"
        rows.append([str(i + 1), f, ratio, err])
    return rows, [], True

def blk_quadratic(r0, c0):
    a = random.choice([1, 2, 3])
    r1, r2 = random.sample(range(-6, 7), 2)
    b, c = -a * (r1 + r2), a * r1 * r2
    rows = [["a", str(a)], ["b", str(b)], ["c", str(c)]]
    A, B, C = (ad(r0 + i, c0 + 1, True, True) for i in range(3))
    rows.append(["disc", f"={B}^2-4*{A}*{C}"])
    D = ad(r0 + 3, c0 + 1, True, True)
    rows.append(["root 1", f"=(-{B}+SQRT({D}))/(2*{A})"])
    rows.append(["root 2", f"=(-{B}-SQRT({D}))/(2*{A})"])
    for k, lab in ((4, "check 1"), (5, "check 2")):
        x = ad(r0 + k, c0 + 1)
        rows.append([lab, f"={A}*{x}^2+{B}*{x}+{C}"])
    return rows, [], False

BLOCKS = {
    "trig": (blk_trig, 1.0), "stats": (blk_stats, 1.4), "integral": (blk_integral, 1.1),
    "newton": (blk_newton, 1.0), "taylor": (blk_taylor, 1.0), "matrix": (blk_matrix, 1.0),
    "growth": (blk_growth, 0.9), "finance": (blk_finance, 0.9), "binom": (blk_binom, 1.0),
    "numtheory": (blk_numtheory, 0.8), "monte": (blk_monte, 0.7), "fib": (blk_fib, 0.8),
    "quadratic": (blk_quadratic, 0.8),
}

def tweak(old):
    """A plausible new value for an input cell, or None."""
    try:
        v = float(old)
    except ValueError:
        return None
    if "." in old:
        dec = len(old.split(".")[1])
        new = round(v * random.uniform(0.85, 1.15), dec)
        if new == v:
            new = round(v + 10 ** -dec, dec)
        return f"{new:.{dec}f}"
    iv = int(v)
    if abs(iv) >= 1000:
        new = int(round(iv * random.uniform(0.92, 1.08), -3))
        return str(new if new != iv else iv + 1000)
    new = iv + random.choice([-3, -2, -1, 1, 2, 3])
    if iv > 0 and new < 1:
        new = iv + random.randint(1, 3)
    if new == 0:
        new = iv + (1 if iv > 0 else -1)
    return str(new)

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
        self.known = False
        self.row = 0
        self.col = 0
        self.rhythm = 1.0
        self.burst = 0
        self.mode = "writing"
        self.block = None
        self.recent = []
        self.in_dialog = False         # True only while WE are handling a dialog
        self.hwnd = 0                  # cached main window handle
        self.hwnd_scan = 0.0
        self.chars = 0                 # typing statistics
        self.type_secs = 0.0
        self.reset_layout()

    def reset_layout(self):
        self.band_row = 0
        self.slot = 0
        self.band_h = 0
        self.blocks_done = 0
        self.inputs = {}
        self.formulas = []
        self.block = None


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
    """One key press with a realistic hold time (an instant press can be dropped by the app)."""
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
    """Press one character the way fingers do: hold Shift when needed, hold the key briefly."""
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

def calibrate_name_box():
    """Hover the mouse over the Name Box, press F7: remembers its position inside the window."""
    global NAME_BOX_OFFSET
    rc = win_rect(main_hwnd())
    if not rc:
        log("calibration failed: GridCraft window not found")
        return
    x, y = pyautogui.position()
    NAME_BOX_OFFSET = (x - rc[0], y - rc[1])
    log(f"Name Box offset saved: NAME_BOX_OFFSET = {NAME_BOX_OFFSET}  (put this in the config to keep it)")

def start_listeners():
    listeners.extend([
        keyboard.GlobalHotKeys({PAUSE_KEY: toggle, QUIT_KEY: quit_now, CALIBRATE_KEY: calibrate_name_box}),
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
    """Handle of GridCraft's main window (the biggest window that matches the title)."""
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
    """Handle of a dialog / popup that belongs to GridCraft and is open right now, else 0."""
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
            return main                              # sentinel: a menu is open, not a dialog window
    except Exception:
        pass
    return 0

def app_ok():
    """Is it safe to type / click right now?"""
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
    log("bringing existing GridCraft window forward")
    for _ in range(3):
        activate_main()
        time.sleep(0.75)
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
        time.sleep(random.uniform(0.06, 0.16))     # let the app finish handling the key
    else:
        kb_hotkey(*keys)

def nap(seconds):
    end = time.time() + seconds
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
    """Sleep but only watch for real human input (used while focus is changing)."""
    end = time.time() + seconds
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
    dur = (0.17 + 0.11 * math.log2(1 + dist / 55)) * random.uniform(0.9, 1.4) / speed
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

def drift():
    x, y = pyautogui.position()
    x0, x1, y0, y1 = zone_rect()
    human_move(
        min(max(x + random.randint(-38, 38), x0), x1),
        min(max(y + random.randint(-22, 22), y0), y1),
        0.55
    )

def wander():
    r = random.random()
    if r < 0.6:
        drift()
    elif r < 0.9:
        human_move(*zone_point(bias_left=random.random() < 0.5),
                   speed=random.uniform(0.5, 1.1))
    else:
        guard()
        ms_scroll(random.choice([-1, 1]) * random.randint(1, 2))
        time.sleep(random.uniform(0.1, 0.3))

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

SHIFTED = set('_(){}[]:"<>?+|~!@#$%^&*')
SYMBOLS = set("(),:;=^*/+-$.")

def key_delay(prev, ch, speed):
    """Target time between two key presses."""
    d = random.lognormvariate(math.log(BASE_DELAY), 0.36) * TYPING_SPEED * speed * S.rhythm
    if prev == ch:
        d *= 0.85
    if ch.isupper() or ch in SHIFTED:
        d *= 1.3
    elif ch.isdigit():
        d *= 1.12
    if ch in SYMBOLS:
        d += random.uniform(0.03, 0.17)        # symbols and brackets: look at the keyboard / think
    if prev in "(,=":
        d += random.uniform(0.0, 0.14)         # deciding what comes next
    if prev == " ":
        d += random.uniform(0.0, 0.1)
    if S.burst > 0:
        d *= 0.82
        S.burst -= 1
    elif random.random() < 0.04:
        S.burst = random.randint(3, 6)
    r = random.random()
    if r < MICRO_STOP:
        d += random.uniform(0.25, 0.8)
    elif r < MICRO_STOP + 0.008:
        d += random.uniform(1.0, 2.6)
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

def typing_wpm():
    if S.type_secs < 8:
        return None
    return (S.chars / 5.0) / (S.type_secs / 60.0)

def type_cell(text):
    S.rhythm = min(max(S.rhythm * random.uniform(0.94, 1.06), 0.85), 1.35)
    if text.startswith("="):
        if random.random() < 0.7:
            think(0.7, 2.6)
    elif random.random() < 0.15:
        think(0.3, 1.0)
    type_text(text)


# ───────────────────────── GRID NAVIGATION ─────────────────────────
def press_n(key, n):
    for i in range(n):
        guard()
        kb_press(key)
        time.sleep(random.uniform(0.12, 0.26))
        if i % 9 == 8:
            time.sleep(random.uniform(0.1, 0.35))

def home():
    hit("ctrl", "home")
    S.row = S.col = 0
    S.known = True
    nap(0.25)

def name_box_goto(r, c):
    """Click the Name Box, type the address, press Enter: absolute and immune to dropped keys."""
    rc = win_rect(main_hwnd())
    if not (NAME_BOX_OFFSET and rc):
        return False
    x = rc[0] + NAME_BOX_OFFSET[0] + random.uniform(-10, 10)
    y = rc[1] + NAME_BOX_OFFSET[1] + random.uniform(-3, 3)
    human_click(x, y)
    nap(random.uniform(0.3, 0.7))
    hit("ctrl", "a")
    nap(random.uniform(0.1, 0.3))
    type_text(ad(r, c), typos=False)
    nap(random.uniform(0.2, 0.5))
    hit("enter")
    nap(random.uniform(0.3, 0.7))
    S.row, S.col, S.known = r, c, True
    return True

def goto(r, c, absolute=False):
    """Move the cell cursor to 0-based (r, c). absolute=True prefers the Name Box jump."""
    if absolute and NAME_BOX_OFFSET and name_box_goto(r, c):
        return
    if not S.known:
        home()
    dr, dc = r - S.row, c - S.col
    far = abs(dr) + abs(dc)
    if far > 8 and (r + c) + 3 < far:
        home()
        dr, dc = r, c
    if dr:
        press_n("down" if dr > 0 else "up", abs(dr))
    if dc:
        press_n("right" if dc > 0 else "left", abs(dc))
    S.row, S.col = r, c
    nap(random.uniform(0.1, 0.35))

def focus_grid():
    """Cancel any edit, click into the grid so it has focus, jump to A1."""
    hit("escape")
    nap(0.15)
    human_click(*zone_point())
    nap(0.25)
    home()

def save():
    hit("ctrl", "s")
    quiet_nap(random.uniform(0.7, 1.3))
    if dialog_hwnd():                       # e.g. "keep this format?" - answer it like a person
        log("save opened a prompt")
        close_dialogs(accept=True)

def reset_sheet():
    log("clearing the sheet")
    hit("ctrl", "a")
    nap(0.5)
    hit("delete")
    nap(0.6)
    home()
    S.reset_layout()
    save()

def heal():
    log("healing after interruption")
    if dialog_hwnd():
        close_dialogs(accept=False)
    for _ in range(2):
        hit("escape")
        nap(0.2)
    S.known = False


# ───────────────────────── DIALOG BOXES ─────────────────────────
def hover_menu_bar():
    """Mouse drifts toward the menu / ribbon area before a shortcut, like looking for a command."""
    rc = win_rect(main_hwnd())
    if not rc:
        return
    x, y = rect_point(rc, 0.03, 0.45, 0.025, 0.09)
    human_move(x, y)
    think(0.3, 1.1)
    if random.random() < 0.5:
        human_move(*rect_point(rc, 0.03, 0.45, 0.025, 0.09), speed=0.7)
        think(0.2, 0.7)

def wait_dialog(timeout):
    end = time.time() + timeout
    while time.time() < end:
        h = dialog_hwnd()
        if h:
            return h
        quiet_nap(0.15)
    return 0

def close_dialogs(accept=False, assume_open=False):
    """Close whatever dialog is open, with mouse movement and a key press or a click on the X."""
    prev = S.in_dialog
    S.in_dialog = True
    try:
        for attempt in range(4):
            h = dialog_hwnd()
            if not h and not (assume_open and attempt == 0):
                break
            is_menu = bool(h) and h == main_hwnd()            # a menu, not a dialog window
            if is_menu:
                rc = None
            else:
                rc = win_rect(h) if h else win_rect(main_hwnd())
            quiet_nap(random.uniform(0.3, 0.9))
            if rc:
                human_move(*rect_point(rc, 0.25, 0.85, 0.3, 0.9))
                quiet_nap(random.uniform(0.3, 1.0))
            if is_menu:
                kb_press("escape")                            # Escape only; never Enter on a menu
            elif attempt == 0:
                if CLOSE_BY_CLICK and rc and h and not accept and random.random() < 0.45:
                    human_click(rc[2] - 24, rc[1] + 14)       # title-bar X
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
        S.known = False

def dialog_text(kind):
    if kind == "word":
        return random.choice(FIND_WORDS)
    if kind == "cell":
        if S.formulas:
            r, c = random.choice(S.formulas)
            return ad(r, c)
        return "A1"
    return None

def act_dialog():
    name = random.choices(list(DIALOGS), [DIALOGS[n].get("w", 1.0) for n in DIALOGS])[0]
    spec = DIALOGS[name]
    log(f"dialog: {name}")
    think(0.5, 1.6)
    hover_menu_bar()
    nap(random.uniform(0.15, 0.5))
    hit(*spec["keys"])
    S.known = False
    hwnd = 0
    try:
        S.in_dialog = True
        hwnd = wait_dialog(2.5)
        if not hwnd and not DIALOGS_IN_WINDOW:
            log("no dialog window detected - closing")
            kb_press("escape")
            return
        quiet_nap(random.uniform(0.5, 1.2))
        rc = win_rect(hwnd) if hwnd else win_rect(main_hwnd())
        if rc:                                         # look the dialog over
            human_move(*rect_point(rc, 0.2, 0.8, 0.25, 0.75))
            think(0.6, 1.8)
        text = dialog_text(spec["text"])
        if text:
            type_text(text)
            think(0.6, 1.6)
        for _ in range(random.randint(*spec["tabs"])):
            hit("tab")
            time.sleep(random.uniform(0.3, 0.8))
        for _ in range(random.randint(*spec["enter"])):
            hit("enter")
            think(0.8, 2.0)
        if rc and random.random() < 0.6:               # hover near where the buttons usually are
            human_move(*rect_point(rc, 0.5, 0.95, 0.7, 0.95))
            think(0.4, 1.3)
        close_dialogs(accept=spec["accept"], assume_open=(not hwnd and DIALOGS_IN_WINDOW))
    finally:
        S.in_dialog = False
        S.known = False


# ───────────────────────── TABLE TYPING ─────────────────────────
def peek_slot():
    return S.band_row, SLOT_COLS[S.slot]

def commit_slot(h):
    S.band_h = max(S.band_h, h)
    S.slot += 1
    if S.slot >= len(SLOT_COLS):
        S.slot = 0
        S.band_row += S.band_h + 2
        S.band_h = 0

def type_row(r, c0, row):
    """Type one row starting at (r, c0). Ends with the cursor at (r + 1, c0)."""
    cells = list(row)
    while cells and not cells[-1]:
        cells.pop()
    if not cells:
        hit("down")
        S.row, S.col = r + 1, c0
        return
    for j, text in enumerate(cells):
        last = j == len(cells) - 1
        if text:
            type_cell(text)
            if not text.startswith("=") and not is_number(text):
                guard()
                kb_press("delete")
            nap(random.uniform(0.15, 0.5))
        hit("down" if last else "right")
        if not last:
            time.sleep(random.uniform(0.12, 0.4))
    if len(cells) > 1:
        press_n("left", len(cells) - 1)
    S.row, S.col = r + 1, c0

def new_block():
    if S.blocks_done >= MAX_BLOCKS:
        reset_sheet()
        return False
    r0, c0 = peek_slot()
    names = [n for n in BLOCKS if n not in S.recent[-4:] and n not in DISABLED] or \
            [n for n in BLOCKS if n not in DISABLED]
    name = random.choices(names, [BLOCKS[n][1] for n in names])[0]
    rows, edits, has_header = BLOCKS[name][0](r0, c0)
    rows = [[apply_map(t) for t in row] for row in rows]
    commit_slot(len(rows))
    S.recent.append(name)
    S.block = {"name": name, "rows": rows, "edits": edits, "hdr": has_header,
               "r0": r0, "c0": c0, "i": 0}
    log(f"new table: {name} at {ad(r0, c0)}")
    return True

def finish_block():
    b = S.block
    for dr, dc in b["edits"]:
        S.inputs[(b["r0"] + dr, b["c0"] + dc)] = b["rows"][dr][dc]
    for dr, row in enumerate(b["rows"]):
        for dc, t in enumerate(row):
            if t.startswith("=") and len(S.formulas) < 400:
                S.formulas.append((b["r0"] + dr, b["c0"] + dc))
    S.blocks_done += 1
    S.block = None
    if BOLD_HEADERS and b["hdr"] and len(b["rows"][0]) > 1 and random.random() < 0.7:
        w = len(b["rows"][0])
        goto(b["r0"], b["c0"])
        S.known = False
        for _ in range(w - 1):
            hit("shift", "right")
            time.sleep(random.uniform(0.08, 0.2))
        nap(0.3)
        hit("ctrl", "b")
        nap(0.4)
        hit("down")

def work_block():
    if not S.block:
        if not new_block():
            return
    b = S.block
    rows = b["rows"]
    todo = random.randint(2, 4)
    done = 0
    while b["i"] < len(rows) and done < todo:
        r = b["r0"] + b["i"]
        goto(r, b["c0"], absolute=True)   # after any interruption this returns to the exact row
        type_row(r, b["c0"], rows[b["i"]])
        b["i"] += 1
        done += 1
        nap(random.uniform(0.4, 1.3))
        if random.random() < 0.14:
            think(1.0, 3.2)
        if random.random() < 0.1:
            save()
    if b["i"] >= len(rows):
        finish_block()
    save()
    w = typing_wpm()
    if w:
        log(f"typing ~ {w:.0f} wpm (while typing, pauses excluded)")


# ───────────────────────── OTHER ACTIONS ─────────────────────────
def act_edit_input():
    if not S.inputs:
        return work_block()
    (r, c), old = random.choice(list(S.inputs.items()))
    new = tweak(old)
    if new is None:
        return
    think(1.0, 3.0)
    goto(r, c)
    nap(0.3)
    type_cell(new)
    hit("down")
    S.row = r + 1
    S.inputs[(r, c)] = new
    think(1.5, 4.0)
    save()

def act_review():
    if not S.formulas:
        return work_block()
    for _ in range(random.randint(1, 3)):
        r, c = random.choice(S.formulas)
        goto(r, c)
        think(0.8, 2.2)
        if USE_F2 and random.random() < 0.6:
            hit("f2")
            think(1.0, 2.8)
            hit("escape")
            nap(0.3)

def act_select_range():
    cells = list(S.inputs) + S.formulas
    if not cells:
        return work_block()
    r, c = random.choice(cells)
    goto(r, c)
    S.known = False
    for _ in range(random.randint(1, 5)):
        hit("shift", "down")
        time.sleep(random.uniform(0.1, 0.3))
    for _ in range(random.randint(0, 3)):
        hit("shift", "right")
        time.sleep(random.uniform(0.1, 0.3))
    think(1.5, 3.5)
    hit(random.choice(["left", "up"]))

def act_navigate():
    think(0.4, 1.2)
    for _ in range(random.randint(2, 4)):
        choice = random.choice(["scroll", "arrows", "click", "jump"])
        if choice == "scroll":
            human_move(*zone_point(False))
            for _ in range(random.randint(2, 5)):
                guard()
                ms_scroll(random.choice([-1, 1]) * random.randint(1, 3))
                time.sleep(random.uniform(0.08, 0.22))
        elif choice == "arrows":
            S.known = False
            for _ in range(random.randint(3, 9)):
                hit(random.choice(["up", "down", "left", "right", "pageup", "pagedown"]))
                time.sleep(random.uniform(0.06, 0.16))
        elif choice == "click":
            S.known = False
            human_click(*zone_point())
        else:
            cells = list(S.inputs) + S.formulas
            if cells:
                goto(*random.choice(cells))
        think(0.4, 1.3)
    S.known = False

def mouse_idle():
    for _ in range(random.randint(1, 4)):
        human_move(*zone_point())
        think(0.4, 1.4)


def run_one_action():
    global action_started
    action_started = time.time()
    try:
        if not bring_back():
            log("could not bring GridCraft forward")
            return
        if S.needs_heal:
            heal()
            S.needs_heal = False
        S.needs_heal = True
        focus_grid()
        if S.fresh:
            reset_sheet()
            S.fresh = False

        if random.random() < 0.5:
            human_move(*zone_point())
            time.sleep(random.uniform(0.15, 0.6))

        if S.block and random.random() < 0.72:
            work_block()
        else:
            r = random.random()
            dlg = 0.10 * DIALOG_CHANCE_WEIGHT
            if r < 0.36:
                work_block()
            elif r < 0.48:
                act_edit_input()
            elif r < 0.58:
                act_review()
            elif r < 0.68:
                act_navigate()
            elif r < 0.76:
                act_select_range()
            elif r < 0.76 + dlg:
                act_dialog()
            elif r < 0.94:
                mouse_idle()
            else:
                S.mode = "idle"
                think(4.0, 11.0)
                S.mode = "writing"

        S.needs_heal = False
    except Interrupted as e:
        S.known = False
        log(f"aborted: {e.reason}")
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        S.known = False
        log(f"error: {e!r}")
        if DEBUG:
            traceback.print_exc()
    finally:
        S.in_dialog = False


# ───────────────────────── STARTUP ─────────────────────────
_XML = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

def make_empty_xlsx(path):
    parts = {
        "[Content_Types].xml": _XML + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
            '</Types>',
        "_rels/.rels": _XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml": _XML + '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": _XML + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
            '</Relationships>',
        "xl/styles.xml": _XML + '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
            '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
            '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
            '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
            '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/></cellXfs>'
            '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
            '</styleSheet>',
        "xl/sharedStrings.xml": _XML + '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" uniqueCount="0"/>',
        "xl/worksheets/sheet1.xml": _XML + '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>',
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)

def prepare_workbook():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if WORKBOOK.exists():
        if FILE_EXT.lower() != ".xlsx":
            return
        try:
            with zipfile.ZipFile(WORKBOOK) as z:
                if "xl/styles.xml" in z.namelist():
                    return
        except Exception:
            pass
        log("existing workbook is incomplete (no styles part) - rebuilding it")
    if FILE_EXT.lower() == ".xlsx":
        make_empty_xlsx(WORKBOOK)
    else:
        WORKBOOK.write_text("")
    log(f"created {WORKBOOK}")

# ───────────────────────── gridcraft-cli: FUNCTION CHECK ─────────────────────────
ALIASES = {"BINOMDIST": "BINOM.DIST", "NORMDIST": "NORM.DIST", "STDEV": "STDEV.S", "VAR": "VAR.S"}
FUNC_MAP = {}
DISABLED = set()

def find_cli():
    if CLI_EXE and Path(CLI_EXE).exists():
        return str(CLI_EXE)
    found = shutil.which("gridcraft-cli")
    if found:
        return found
    for root in (WORK_DIR, Path(os.environ.get("LOCALAPPDATA", "")) / "Programs",
                 Path(os.environ.get("ProgramFiles", ""))):
        if not str(root) or not root.exists():
            continue
        base = len(root.parts)
        for dirpath, dirs, files in os.walk(root):
            if len(Path(dirpath).parts) - base >= 4:
                dirs[:] = []
            for f in files:
                if f.lower().startswith("gridcraft-cli"):
                    return str(Path(dirpath) / f)
    return None

def _has_func(listing, name):
    return re.search(r"(?<![A-Z0-9.])" + re.escape(name) + r"(?![A-Z0-9.])", listing) is not None

def check_functions():
    cli = find_cli()
    if not cli:
        log("gridcraft-cli not found - skipping function check (set CLI_EXE to enable)")
        return
    try:
        out = subprocess.run([cli, "functions"], capture_output=True, text=True, timeout=30).stdout.upper()
    except Exception as e:
        log(f"function check skipped: {e!r}")
        return
    if len(out) < 200:
        log("function list looked empty - skipping function check")
        return
    used = {}
    for name, (fn, _) in BLOCKS.items():
        names = set()
        for _ in range(25):
            rows, _, _ = fn(0, 0)
            for row in rows:
                for cell in row:
                    if cell.startswith("="):
                        names.update(re.findall(r"([A-Z][A-Z0-9.]*)\(", cell))
        used[name] = names
    allnames = set().union(*used.values())
    missing = set()
    for fnm in sorted(allnames):
        if _has_func(out, fnm):
            continue
        alt = ALIASES.get(fnm)
        if alt and _has_func(out, alt):
            FUNC_MAP[fnm] = alt
            log(f"using {alt} instead of {fnm}")
        else:
            missing.add(fnm)
    if missing:
        log(f"not supported by GridCraft: {', '.join(sorted(missing))}")
    for name, names in used.items():
        if names & missing:
            DISABLED.add(name)
            log(f"table '{name}' disabled")
    log(f"function check done ({len(allnames) - len(missing)}/{len(allnames)} supported)")

def apply_map(text):
    for old, new in FUNC_MAP.items():
        text = re.sub(r"(?<![A-Z0-9.])" + re.escape(old) + r"\(", new + "(", text)
    return text

def find_app():
    if APP_EXE and Path(APP_EXE).exists():
        return str(APP_EXE)
    found = shutil.which("gridcraft") or shutil.which("GridCraft")
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
                if fl.startswith("gridcraft") and fl.endswith(".exe") and "cli" not in fl and "unins" not in fl and "update" not in fl:
                    return str(Path(dirpath) / f)
    return None

def open_app():
    prepare_workbook()
    S.hwnd = 0
    S.hwnd_scan = 0.0
    existing = app_windows()
    if existing:
        log("reusing already-open GridCraft window")
        force_activate(existing[0])
        time.sleep(1.4)
        if in_app():
            return True

    exe = find_app()
    if not exe:
        log("GridCraft.exe not found - set APP_EXE at the top of the script")
        return False

    log(f"launching {exe} -> {WORKBOOK}")
    subprocess.Popen([exe, str(WORKBOOK)], cwd=str(WORK_DIR))
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
                log("GridCraft ready")
                time.sleep(2.5)
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

def main():
    global last_input
    log("=" * 56)
    log(f"{PAUSE_KEY} pause/resume  |  {QUIT_KEY} quit  |  top-left = failsafe")
    log("=" * 56)
    if not IS_WIN:
        log("warning: not Windows - dialog detection is disabled")

    if not NAME_BOX_OFFSET:
        log("tip: hover the mouse over the Name Box (the 'B14' box) and press F7 once - rows then start with an exact jump")
    start_listeners()
    check_functions()
    if not open_app():
        sys.exit(1)

    self_test_injection()
    last_input = time.time()
    next_at = time.time() + IDLE_AFTER

    try:
        while not stop.is_set():
            time.sleep(random.uniform(1.3, 2.9))
            if not running.is_set():
                continue
            if time.time() - last_input < IDLE_AFTER or time.time() < next_at:
                if time.time() - last_input >= IDLE_AFTER and random.random() < 0.18:
                    ambient_move()
                continue
            run_one_action()
            gap = random.uniform(*GAP_BETWEEN)
            if random.random() < LONG_BREAK_CHANCE:
                gap += random.uniform(*LONG_BREAK)
            next_at = time.time() + gap
    except pyautogui.FailSafeException:
        log("failsafe - stopping")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for l in listeners:
            l.stop()
        log("session ended")


if __name__ == "__main__":
    main()