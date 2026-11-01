"""
ghostwriter_v5.py  -  developer-presence automation for VS Code

v5 = full review pass over v4. Fixes in this version:
  * Settings are pinned harder: window title format, per-language overrides
    ([python] / [markdown] / [plaintext]), Copilot completions off, secondary
    side bar hidden, Python terminal auto-activation off, shell integration off,
    final-newline / trailing-whitespace handling pinned.
  * settings.json is only rewritten when it changed (no needless reloads).
  * quick_open polls for the tab switch instead of one fixed sleep.
  * Problems panel can no longer leave the wrong file open before an edit
    (fix_step re-opens the target file afterwards).
  * run_in_terminal always runs a .py file (v4 could waste a turn on a .md/.txt).
  * Files are rotated back to their seed once they pass MAX_LINES (v4 grew forever).
  * .gitignore + initial commit so Source Control / git status look normal.
  * VS Code launch: keeps trying to bring the new window to the front, hides the
    console flash, and no longer depends on the window being foreground by luck.
  * Optional traceback logging (DEBUG).

pip install pyautogui pygetwindow pynput
Windows + US keyboard assumed. `code` on PATH.
F8 = pause/resume   F9 = quit   mouse top-left = emergency stop
Hands off the keyboard/mouse while it is working: any real input aborts the
current action and it waits IDLE_AFTER seconds before trying again.
"""

import json
import logging
import math
import random
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import pyautogui
import pygetwindow as gw
from pynput import keyboard, mouse

# ───────────────────────── CONFIG ─────────────────────────
BASE_DIR      = Path(r"C:\devtools")
FOLDER_NAME   = "mine-workspace"            # stable, so old windows are reused
ROOT          = BASE_DIR / FOLDER_NAME
PY            = "python"
CLEAR         = "cls"

ENABLE_CHAT   = False                       # agent mode may edit files behind our back
DEBUG         = False                       # True = print tracebacks on errors
MAX_LINES     = 300                         # rotate a file back to its seed beyond this

IDLE_AFTER    = 11
GAP_BETWEEN   = (5, 22)
LONG_BREAK_CHANCE = 0.09
LONG_BREAK    = (30, 110)

TYPING_SPEED  = 1.18
TYPO_CHANCE   = 0.016
MICRO_STOP    = 0.045

ZONE = (0.26, 0.80, 0.16, 0.58)             # editor-ish safe zone

PAUSE_KEY, QUIT_KEY = "<f8>", "<f9>"
LAUNCH_TIMEOUT = 45

# Keys that must behave identically in every language we touch.
_LANG_KEYS = {
    "editor.autoIndent": "none",
    "editor.autoClosingBrackets": "never",
    "editor.autoClosingQuotes": "never",
    "editor.formatOnType": False,
    "editor.formatOnSave": False,
    "editor.insertSpaces": True,
    "editor.tabSize": 4,
    "editor.detectIndentation": False,
    "editor.quickSuggestions": {"other": False, "comments": False, "strings": False},
    "editor.suggestOnTriggerCharacter": False,
    "editor.autoIndentOnPaste": False,
    "editor.formatOnPaste": False,
}

EDITOR_SETTINGS = {
    **_LANG_KEYS,
    "editor.acceptSuggestionOnEnter": "off",
    "editor.tabCompletion": "off",
    "editor.wordBasedSuggestions": "off",
    "editor.parameterHints.enabled": False,
    "editor.inlineSuggest.enabled": False,
    "editor.snippetSuggestions": "none",
    "editor.wordWrap": "off",
    "files.autoSave": "off",
    "files.eol": "\n",
    "files.trimTrailingWhitespace": False,
    "files.insertFinalNewline": False,
    "files.trimFinalNewlines": False,
    "git.enabled": True,
    "git.openRepositoryInParentFolders": "never",
    "git.autofetch": False,
    "python.analysis.autoImportCompletions": False,
    "python.terminal.activateEnvironment": False,   # no auto-typed activate script
    "terminal.integrated.confirmOnKill": "never",
    "terminal.integrated.confirmOnExit": "never",
    "terminal.integrated.shellIntegration.enabled": False,
    "github.copilot.enable": {"*": False},
    "workbench.startupEditor": "none",
    "workbench.tips.enabled": False,
    "workbench.secondarySideBar.defaultVisibility": "hidden",
    "chat.commandCenter.enabled": False,
    "window.confirmBeforeClose": "never",
    # title format is parsed by title_parts(); pin it so user settings can't break it
    "window.title": "${dirty}${activeEditorShort}${separator}${rootName}${separator}${appName}",
    "window.titleSeparator": " - ",
    "[python]": dict(_LANG_KEYS),
    "[markdown]": dict(_LANG_KEYS),
    "[plaintext]": dict(_LANG_KEYS),
}

# ───────────────────────── CORE FILES ─────────────────────────
CORE_FILES = [
    ("README.md", "md", "# toolkit\n\nSmall experimental project.\n"),
    ("src/main.py", "py", "# entry point\nprint('ready')\n"),
    ("src/utils.py", "py", "def helper(x):\n    return x * 2\n\n"),
    ("src/core.py", "py", "# core logic\n"),
    ("src/models.py", "py", "# data models\n"),
    ("tests/test_utils.py", "test", "# tests\n"),
    ("tests/test_core.py", "test", "# core tests\n"),
    ("notes/todo.txt", "txt", "TODO\n----\n- finish core helpers\n"),
    ("notes/ideas.md", "md", "# Ideas\n"),
]
SEEDS = {rel: seed for rel, _, seed in CORE_FILES}

KIND_WEIGHT = {"py": 8, "test": 2.5, "md": 1.2, "txt": 0.9}

NOUNS = ["user", "order", "item", "score", "token", "cache", "event", "session",
         "record", "metric", "batch", "stream", "key", "entry", "node", "payload"]
VERBS = ["compute", "parse", "filter", "merge", "count", "scan", "build", "load",
         "check", "clean", "split", "rank", "group", "fetch", "trim", "validate"]
COMMENTS = [
    "# TODO: tidy later", "# NOTE: edge case", "# quick fix", "# check this",
    "# FIXME", "# keep for now", "# slow path?", "# revisit", "# temporary"
]

CHAT_PROMPTS = [
    "explain this function briefly",
    "any obvious bugs here?",
    "suggest a cleaner way to write this",
    "what does this do?",
    "add a short docstring",
]

# ───────────────────────── SNIPPET GENERATORS ─────────────────────────
def _n(): return random.choice(NOUNS)
def _v(): return random.choice(VERBS)

def gen_clean():
    n, fn = _n(), f"{_v()}_{_n()}"
    return [
        f"def {fn}({n}s):",
        "    total = 0",
        f"    for {n} in {n}s:",
        f"        total += {n} * {random.randint(2, 6)}",
        "    return total",
        "",
        f"print({fn}(list(range({random.randint(4, 9)}))))"
    ]

def gen_bug_missing_return():
    n, fn = _n(), f"{_v()}_{_n()}"
    return [
        f"def {fn}({n}s):",
        "    total = 0",
        f"    for {n} in {n}s:",
        f"        total += {n}",
        # missing return on purpose
        "",
        f"print({fn}([1, 2, 3, 4]))"
    ]

def gen_bug_undefined():
    fn = f"{_v()}_items"                    # pick once, reuse
    return [
        f"def {fn}(values):",
        "    total = 0",
        "    for v in values:",
        "        total += item",            # undefined name on purpose
        "    return total",
        "",
        f"print({fn}([3, 5, 8]))"
    ]

def gen_bug_off_by_one():
    fn = f"{_v()}_range"
    return [
        f"def {fn}(n):",
        "    s = 0",
        "    for i in range(n + 1):",       # classic off-by-one
        "        s += i",
        "    return s",
        "",
        f"print({fn}(5))"
    ]

def gen_bug_type():
    fn = f"{_v()}_score"                    # pick once, reuse
    return [
        f"def {fn}(data):",
        "    return data['score'] + 10",
        "",
        f"print({fn}(42))"                  # blows up: expects a dict
    ]

def gen_test_ok():
    a, b = random.randint(2, 12), random.randint(2, 12)
    return [
        "def add(a, b):",
        "    return a + b",
        "",
        f"assert add({a}, {b}) == {a + b}",
        "print('ok')"
    ]

def gen_test_failing():
    return [
        "def multiply(a, b):",
        "    return a + b",                 # wrong on purpose
        "",
        "assert multiply(3, 4) == 12",
        "print('ok')"
    ]

def gen_md():
    return random.choice([
        ["## Notes", "", "- keep functions small", "- write the test first", ""],
        ["### Next steps", "", "1. clean the helpers", "2. add one more test", ""],
        ["> remove temporary prints before merging", ""],
        ["## Debugging notes", "", "- off-by-one in range loops", "- watch undefined names", ""],
    ])

def gen_txt():
    return [random.choice([
        "- fix the off-by-one",
        "- add logging",
        "- check empty list case",
        "- rename helper if it grows",
        "- look at the failing test"
    ])]

BUG_GENERATORS = [gen_bug_missing_return, gen_bug_undefined, gen_bug_off_by_one, gen_bug_type]
CLEAN_GENERATORS = [gen_clean]

def make_snippet(kind, prefer_bug=False):
    if kind == "py":
        if prefer_bug or random.random() < 0.38:
            return random.choice(BUG_GENERATORS)()
        return random.choice(CLEAN_GENERATORS)()
    if kind == "test":
        if prefer_bug or random.random() < 0.4:
            return gen_test_failing()
        return gen_test_ok()
    if kind == "md":
        return gen_md()
    if kind == "txt":
        return gen_txt()
    return gen_clean()


# ───────────────────────── LOGGING / SAFETY ─────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.info
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0

# ───────────────────────── SHARED STATE ─────────────────────────
running = threading.Event()
stop = threading.Event()
running.set()

last_input = time.time()
action_started = 0.0
bot_ts = 0.0
USE_TIME_FILTER = False


class State:
    def __init__(self):
        self.files = {}
        self.model = {}
        self.cur = "src/main.py"
        self.snippet = []
        self.sidx = 0
        self.snip_file = None
        self.needs_heal = False
        self.term_open = False
        self.fails = 0
        self.expect = None
        self.rhythm = 1.0
        self.burst = 0
        self.mode = "writing"          # writing | debugging | idle
        self.bug_depth = 0
        self.last_bug_file = None
        self.snip_base = ""            # file text before the current snippet began


S = State()


class Interrupted(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ───────────────────────── INPUT PRIMITIVES ─────────────────────────
def mark():
    global bot_ts
    bot_ts = time.time()

def kb_write(s):
    mark()
    pyautogui.write(s)

def kb_press(k, n=1, iv=0):
    mark()
    pyautogui.press(k, presses=n, interval=iv)

def kb_hotkey(*k):
    mark()
    pyautogui.hotkey(*k)

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

listeners = [
    keyboard.GlobalHotKeys({PAUSE_KEY: toggle, QUIT_KEY: quit_now}),
    keyboard.Listener(on_press=on_press),
    mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll),
]
for _l in listeners:
    _l.daemon = True
    _l.start()


def self_test_injection():
    """If our own synthetic input is seen as 'human', fall back to a timing filter."""
    global USE_TIME_FILTER
    before = last_input
    pyautogui.moveRel(1, 0)
    pyautogui.moveRel(-1, 0)
    time.sleep(0.45)
    if last_input != before:
        USE_TIME_FILTER = True
        log("using timing filter for injected input")


# ───────────────────────── WINDOW HELPERS ─────────────────────────
def active_title():
    try:
        w = gw.getActiveWindow()
        return w.title if w else ""
    except Exception:
        return ""

def is_vsc_title(t):
    t = t.lower()
    return "visual studio code" in t or "code - oss" in t or "vscodium" in t

def in_vsc(t=None):
    t = active_title() if t is None else t
    return FOLDER_NAME.lower() in t.lower() and is_vsc_title(t)

def title_parts(t=None):
    """-> (active editor name, is_dirty). Relies on the pinned window.title."""
    t = active_title() if t is None else t
    first = t.split(" - ")[0].strip()
    return first.lstrip("●").strip(), first.startswith("●")

def vsc_windows():
    try:
        return [w for w in gw.getWindowsWithTitle(FOLDER_NAME) if is_vsc_title(w.title)]
    except Exception:
        return []

def force_activate(w):
    """Tap Alt first: Windows often refuses to hand over foreground otherwise."""
    try:
        if w.isMinimized:
            w.restore()
        kb_press("alt")
        w.activate()
    except Exception:
        pass

def bring_back():
    if in_vsc():
        return True
    wins = vsc_windows()
    if not wins:
        return open_vscode()
    w = wins[0]
    log("bringing existing VS Code window forward")
    for _ in range(3):
        force_activate(w)
        time.sleep(0.75)
        if in_vsc():
            return True
    return False


# ───────────────────────── GUARD ─────────────────────────
def guard(need_vsc=True):
    while not running.wait(0.2):
        if stop.is_set():
            raise Interrupted("quit")
    if stop.is_set():
        raise Interrupted("quit")
    if last_input > action_started:
        raise Interrupted("human")
    if need_vsc:
        t = active_title()
        if not in_vsc(t):
            raise Interrupted("focus")
        if S.expect and title_parts(t)[0] != S.expect:
            raise Interrupted("tab")

def hit(*keys):
    guard()
    kb_hotkey(*keys)

def nap(seconds):
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0:
            break
        if left > 1.0 and random.random() < 0.05:
            wander()
            continue
        time.sleep(min(0.22, left))
        guard()

def think(lo, hi):
    d = random.uniform(lo, hi)
    if d > 1.6 and random.random() < 0.42:
        drift()
    nap(d)


# ───────────────────────── DISK = TRUTH ─────────────────────────
def read_disk(rel):
    try:
        return (ROOT / rel).read_bytes().decode("utf-8", "replace").replace("\r\n", "\n")
    except Exception:
        return ""

def write_disk(rel, text):
    p = ROOT / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))

def as_text(lines):
    return "".join(l + "\n" for l in lines)

def register_file(rel, kind, seed):
    if not (ROOT / rel).exists():
        write_disk(rel, seed)
    S.files[rel] = kind
    S.model[rel] = read_disk(rel)

def verify_models():
    for rel in list(S.files):
        d = read_disk(rel)
        if d == S.model[rel]:
            continue
        log(f"{rel} changed on disk – adopting")
        S.model[rel] = d
        if S.snip_file == rel:
            resumed = False
            for k in range(len(S.snippet)):          # k < len → still unfinished
                if d == S.snip_base + as_text(S.snippet[:k]):
                    S.sidx, resumed = k, True
                    break
            if not resumed:
                S.snippet, S.snip_file, S.sidx = [], None, 0

def rotate_big_files():
    """Reset any file that grew past MAX_LINES. The editor is clean at this point
    (heal ran), so VS Code simply reloads the file from disk."""
    rotated = False
    for rel in list(S.files):
        if S.model[rel].count("\n") > MAX_LINES:
            log(f"{rel} is large – resetting to seed")
            write_disk(rel, SEEDS[rel])
            S.model[rel] = SEEDS[rel]
            if S.snip_file == rel:
                S.snippet, S.snip_file, S.sidx = [], None, 0
            rotated = True
    return rotated

def repair_broken_files():
    """Undo accidental syntax/indentation damage (not the deliberate runtime bugs)."""
    fixed = False
    for rel, kind in S.files.items():
        if kind not in ("py", "test"):
            continue
        if rel == S.snip_file and S.snippet:         # mid-snippet: it will be completed
            continue
        text = S.model[rel]
        try:
            compile(text, rel, "exec")
            continue
        except (SyntaxError, ValueError):
            pass
        lines = text.split("\n")
        good = None
        for end in range(len(lines) - 1, 0, -1):
            cand = "\n".join(lines[:end]).rstrip("\n") + "\n"
            try:
                compile(cand, rel, "exec")
            except (SyntaxError, ValueError):
                continue
            good = cand
            break
        good = good or SEEDS[rel]
        log(f"{rel} had accidental syntax damage – trimming back")
        write_disk(rel, good)
        S.model[rel] = good
        fixed = True
    return fixed

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
    dur = (0.17 + 0.11 * math.log2(1 + dist / 55)) * random.uniform(0.85, 1.28) / speed
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
    try:
        w = gw.getActiveWindow()
        if w and is_vsc_title(w.title):
            l, t, r, b = max(w.left, 0), max(w.top, 0), min(w.right, sw), min(w.bottom, sh)
    except Exception:
        pass
    W, H = r - l, b - t
    return l + ZONE[0] * W, l + ZONE[1] * W, t + ZONE[2] * H, t + ZONE[3] * H

def zone_point(bias_left=True):
    x0, x1, y0, y1 = zone_rect()
    if bias_left:
        x = random.triangular(x0, x1, x0 + 0.2 * (x1 - x0))
    else:
        x = random.uniform(x0, x1)
    return x, random.uniform(y0, y1)

def drift():
    x, y = pyautogui.position()
    x0, x1, y0, y1 = zone_rect()
    human_move(
        min(max(x + random.randint(-38, 38), x0), x1),
        min(max(y + random.randint(-22, 22), y0), y1),
        0.55
    )

def wander():
    """Aimless mouse movement, like a hand resting on the mouse."""
    r = random.random()
    if r < 0.55:
        drift()
    elif r < 0.85:
        human_move(*zone_point(bias_left=random.random() < 0.5),
                   speed=random.uniform(0.5, 1.1))
    elif not S.term_open:
        guard()
        ms_scroll(random.choice([-1, 1]) * random.randint(1, 2))
        time.sleep(random.uniform(0.1, 0.3))

def small_jitter():
    x, y = pyautogui.position()
    human_move(x + random.randint(-14, 14), y + random.randint(-10, 10), 0.8)

def ambient_move():
    """Mouse activity between actions, only while VS Code is in front."""
    global action_started
    action_started = time.time()
    try:
        if in_vsc():
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

def key_delay(prev, ch, speed):
    d = random.lognormvariate(math.log(0.125), 0.34) * TYPING_SPEED * speed * S.rhythm
    if prev == ch:
        d *= 0.8
    if ch.isupper() or ch in SHIFTED:
        d *= 1.2
    if ch in " .,;:":
        d += random.uniform(0.0, 0.06)
    if prev == " ":
        d += random.uniform(0.0, 0.08)
    if S.burst > 0:
        d *= 0.6
        S.burst -= 1
    elif random.random() < 0.065:
        S.burst = random.randint(4, 9)
    r = random.random()
    if r < MICRO_STOP:
        d += random.uniform(0.2, 0.65)
    elif r < MICRO_STOP + 0.006:
        d += random.uniform(1.0, 2.3)
    return d

def emit(ch, prev, speed):
    guard()
    kb_write(ch)
    time.sleep(key_delay(prev, ch, speed))

def backspaces(n):
    for _ in range(n):
        guard()
        kb_press("backspace")
        time.sleep(random.uniform(0.045, 0.11))

def type_text(text, typos=True, speed=1.0):
    i, prev = 0, ""
    while i < len(text):
        if i % 20 == 19 and random.random() < 0.12:
            small_jitter()
        ch = text[i]
        if typos and i > 0 and ch.isalpha() and random.random() < TYPO_CHANCE:
            kind = random.choices(["adjacent", "late", "swap"], [5, 3, 2])[0]
            if kind == "swap" and i + 1 < len(text) and text[i + 1].isalpha() and text[i + 1] != ch:
                emit(text[i + 1], prev, speed)
                emit(ch, text[i + 1], speed)
                time.sleep(random.uniform(0.16, 0.4))
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
                time.sleep(random.uniform(0.16, 0.5))
                backspaces(1 + extra)
        emit(ch, prev, speed)
        prev = ch
        i += 1

def indent(lead, rel):
    for i in range(lead):
        guard()
        kb_write(" ")
        time.sleep(random.uniform(0.02, 0.06))
        if i % 4 == 3:
            time.sleep(random.uniform(0.04, 0.12))

def type_line(rel, line):
    body = line.lstrip(" ")
    lead = len(line) - len(body)
    S.rhythm = min(max(S.rhythm * random.uniform(0.93, 1.07), 0.78), 1.25)
    if body.startswith(("def ", "class ")) and random.random() < 0.5:
        think(0.6, 2.0)
    if lead:
        indent(lead, rel)
    if body:
        type_text(body)
    nap(random.uniform(0.09, 0.35))
    hit("enter")
    if random.random() < 0.13:
        think(0.45, 1.6)


# ───────────────────────── EDITOR CONTROL ─────────────────────────
def focus_editor():
    """Escape any widget, then focus the first editor group."""
    hit("escape")
    hit("ctrl", "1")
    nap(0.22)

def palette(cmd, speed=0.52):
    hit("ctrl", "shift", "p")
    nap(random.uniform(0.3, 0.65))
    type_text(cmd, typos=False, speed=speed)
    nap(random.uniform(0.3, 0.7))
    hit("enter")
    nap(random.uniform(0.4, 0.8))

def quick_open(rel):
    S.expect = None                          # title legitimately changes here
    name = Path(rel).name
    hit("ctrl", "p")
    nap(random.uniform(0.3, 0.65))
    type_text(rel, typos=False, speed=0.52)
    nap(random.uniform(0.3, 0.7))
    hit("enter")
    nap(0.6)
    t0 = time.time()
    while title_parts()[0] != name and time.time() - t0 < 2.5:
        nap(0.2)
    if title_parts()[0] != name:
        raise Interrupted("tab")
    S.expect = name

def open_file(rel):
    if title_parts()[0] != Path(rel).name:
        quick_open(rel)
    S.cur = rel
    S.expect = Path(rel).name

def reload_active():
    palette("File: Revert File")

def revert_until_clean():
    for _ in range(2):
        reload_active()
        nap(0.4)
        if not title_parts()[1]:
            return
    log("falling back to undo")
    focus_editor()
    for i in range(280):
        hit("ctrl", "z")
        if i % 10 == 9 and not title_parts()[1]:
            return
        time.sleep(0.016)
    raise Interrupted("cannot-clean")

def ensure_clean():
    if title_parts()[1]:
        log("discarding unexpected unsaved changes")
        revert_until_clean()

def commit(rel, expected):
    hit("ctrl", "s")
    t0 = time.time()
    while True:
        time.sleep(0.11)
        if not title_parts()[1]:
            break
        guard()
        if time.time() - t0 > 3.5:
            raise Interrupted("save-timeout")
    time.sleep(0.1)
    if read_disk(rel) == expected:
        S.model[rel] = expected
        S.fails = 0
        return
    log(f"MISMATCH on {rel} – restoring")
    S.fails += 1
    write_disk(rel, S.model[rel])
    if S.fails >= 3:
        S.snippet, S.snip_file, S.sidx, S.fails = [], None, 0, 0
    raise Interrupted("mismatch")

def heal():
    log("healing after interruption")
    for _ in range(2):
        hit("escape")
        nap(0.18)
    if S.term_open:
        palette("Terminal: Kill All Terminals")
        S.term_open = False
    focus_editor()
    S.expect = None
    if title_parts()[1]:
        revert_until_clean()


# ───────────────────────── HIGH-LEVEL ACTIONS ─────────────────────────
def pick_file(prefer_py=False):
    ok = (lambda r: S.files[r] in ("py", "test")) if prefer_py else (lambda r: True)
    if S.cur in S.files and ok(S.cur) and random.random() < 0.6:
        return S.cur
    cands = list(S.files)
    if prefer_py:
        cands = [r for r in cands if ok(r)] or cands
    weights = [KIND_WEIGHT.get(S.files[r], 1) for r in cands]
    return random.choices(cands, weights)[0]

def start_snippet(rel=None, prefer_bug=False):
    rel = rel or pick_file()
    kind = S.files[rel]
    lines = make_snippet(kind, prefer_bug=prefer_bug)
    text = S.model[rel]
    S.snip_base = text
    if text.strip() and not text.endswith("\n\n"):
        lines = [""] + lines
    S.snip_file, S.snippet, S.sidx = rel, lines, 0
    if prefer_bug and kind in ("py", "test"):
        S.mode = "debugging"
        S.bug_depth = random.randint(1, 3)
        S.last_bug_file = rel

def type_snippet(rel):
    k = S.sidx
    group = random.randint(1, 3)
    while k < len(S.snippet):
        type_line(rel, S.snippet[k])
        k += 1
        if k - S.sidx >= group or k == len(S.snippet):
            commit(rel, S.model[rel] + as_text(S.snippet[S.sidx:k]))
            S.sidx = k
            group = random.randint(1, 3)
        if k < len(S.snippet) and random.random() < 0.16:
            drift()

def code_step(force=None, prefer_bug=False):
    if not S.snippet:
        start_snippet(force, prefer_bug=prefer_bug)
    rel = S.snip_file
    open_file(rel)
    ensure_clean()
    focus_editor()                           # never type into a sidebar/panel
    if random.random() < 0.28:
        human_click(*zone_point())
        nap(0.25)
    hit("ctrl", "end")
    nap(0.18)
    type_snippet(rel)
    S.snippet, S.snip_file, S.sidx = [], None, 0

def run_in_terminal(rel=None, watch=True):
    if not rel or not rel.endswith(".py"):
        rel = pick_file(prefer_py=True)
    if not rel.endswith(".py"):
        return
    open_file(rel)
    ensure_clean()
    think(0.5, 1.6)
    S.term_open = True
    palette("Terminal: Create New Terminal")
    nap(random.uniform(1.5, 2.6))
    type_text(f"{PY} {rel}", speed=0.72)
    nap(0.35)
    hit("enter")
    if watch:
        think(2.8, 7.0)
    if random.random() < 0.5:
        type_text(CLEAR, typos=False, speed=0.65)
        hit("enter")
        nap(0.35)
    type_text("exit", typos=False, speed=0.65)
    hit("enter")
    nap(0.6)
    S.term_open = False
    focus_editor()

def close_panel():
    palette("View: Close Panel")

def open_problems_panel():
    """Ctrl+Shift+M – look at the list, close it, return focus to the editor.
    The active tab may legitimately change here, so tab-checking is off; callers
    that edit afterwards must call open_file() again."""
    S.expect = None
    hit("ctrl", "shift", "m")
    nap(random.uniform(0.8, 1.6))
    for _ in range(random.randint(1, 3)):
        hit("down")
        time.sleep(random.uniform(0.15, 0.4))
    think(0.8, 2.0)
    close_panel()
    focus_editor()

def go_to_line(n=None):
    """Jump to line n (1-based). Caller must have editor focus."""
    if n is None:
        total = max(1, len(S.model.get(S.cur, "").split("\n")))
        n = random.randint(1, total)
    hit("ctrl", "g")
    nap(0.35)
    type_text(str(n), typos=False, speed=0.6)
    hit("enter")
    nap(0.4)

def fix_step():
    """Pick the target line FIRST, jump to it, edit there, and compute the
    expected text from that same line."""
    rel = S.last_bug_file
    if not rel or S.files.get(rel) not in ("py", "test"):
        rel = pick_file(prefer_py=True)
    open_file(rel)
    ensure_clean()
    focus_editor()
    think(0.8, 2.2)

    lines = S.model[rel].split("\n")             # last element is "" (trailing newline)
    cands = [i for i, l in enumerate(lines) if l.strip() and "#" not in l]
    if not cands:
        S.bug_depth, S.mode = 0, "writing"
        return
    idx = random.choice(cands)

    if random.random() < 0.55:
        open_problems_panel()
        open_file(rel)                           # panel may have switched tabs
        focus_editor()

    go_to_line(idx + 1)
    hit("end")
    nap(0.2)
    note = "  " + random.choice(COMMENTS)
    type_text(note)
    lines[idx] += note
    commit(rel, "\n".join(lines))

    S.bug_depth = max(0, S.bug_depth - 1)
    if S.bug_depth <= 0:
        S.mode = "writing"
        if random.random() < 0.6:
            run_in_terminal(rel)

def search_workspace():
    hit("ctrl", "shift", "f")
    nap(0.55)
    term = random.choice(NOUNS + VERBS + ["TODO", "FIXME", "return"])
    type_text(term, typos=False, speed=0.6)
    nap(1.4)
    for _ in range(random.randint(0, 3)):
        hit("down")
        time.sleep(random.uniform(0.2, 0.5))
    focus_editor()

def source_control_peek():
    hit("ctrl", "shift", "g")
    nap(random.uniform(1.0, 2.2))
    for _ in range(random.randint(1, 3)):
        human_move(*zone_point(bias_left=True))
        think(0.4, 1.1)
    focus_editor()
    if random.random() < 0.45:
        S.term_open = True
        palette("Terminal: Create New Terminal")
        nap(1.4)
        type_text("git status", typos=False, speed=0.7)
        hit("enter")
        think(1.8, 3.5)
        type_text("exit", typos=False, speed=0.65)
        hit("enter")
        nap(0.5)
        S.term_open = False
        focus_editor()

def chat_interaction():
    """Opt-in (ENABLE_CHAT). Ctrl+Alt+I opens Chat in current VS Code builds."""
    hit("ctrl", "alt", "i")
    nap(random.uniform(1.0, 1.8))
    type_text(random.choice(CHAT_PROMPTS), typos=False, speed=0.65)
    nap(0.4)
    hit("enter")
    think(3.5, 7.0)
    focus_editor()

def navigate_and_read():
    rel = pick_file()
    open_file(rel)
    focus_editor()
    think(0.5, 1.4)
    for _ in range(random.randint(2, 5)):
        choice = random.choice(["scroll", "arrows", "click", "goline"])
        if choice == "scroll":
            human_move(*zone_point(False))
            for _ in range(random.randint(2, 5)):
                guard()
                ms_scroll(random.choice([-1, 1]) * random.randint(1, 3))
                time.sleep(random.uniform(0.07, 0.2))
        elif choice == "arrows":
            for _ in range(random.randint(3, 9)):
                hit(random.choice(["up", "down", "pageup", "pagedown"]))
                time.sleep(random.uniform(0.05, 0.13))
        elif choice == "goline":
            go_to_line()
        else:
            human_click(*zone_point())
        think(0.35, 1.1)

def mouse_idle():
    for _ in range(random.randint(1, 4)):
        human_move(*zone_point())
        think(0.35, 1.3)

def toggle_sidebar():
    hit("ctrl", "b")
    nap(random.uniform(0.6, 1.4))
    if random.random() < 0.7:
        hit("ctrl", "b")
        nap(0.4)
    focus_editor()


def run_one_action():
    global action_started
    action_started = time.time()
    S.expect = None
    try:
        if not bring_back():
            log("could not bring VS Code forward")
            return
        if S.needs_heal:
            heal()
            S.needs_heal = False
        verify_models()
        S.needs_heal = True
        changed = rotate_big_files()
        changed = repair_broken_files() or changed
        if changed:
            nap(1.6)                         # let VS Code reload the files
        focus_editor()

        if random.random() < 0.5:
            human_move(*zone_point())
            time.sleep(random.uniform(0.15, 0.6))

        if S.snippet:
            code_step()
        elif S.mode == "debugging" and S.bug_depth > 0:
            fix_step()
        else:
            r = random.random()
            if r < 0.32:
                code_step(prefer_bug=random.random() < 0.4)
            elif r < 0.44:
                run_in_terminal()
            elif r < 0.54:
                S.mode = "debugging"
                S.bug_depth = random.randint(1, 2)
                S.last_bug_file = pick_file(prefer_py=True)
                fix_step()
            elif r < 0.63:
                navigate_and_read()
            elif r < 0.70:
                search_workspace()
            elif r < 0.76:
                source_control_peek()
            elif r < 0.81:
                if ENABLE_CHAT:
                    chat_interaction()
                else:
                    navigate_and_read()
            elif r < 0.86:
                open_problems_panel()
            elif r < 0.91:
                mouse_idle()
            elif r < 0.95:
                toggle_sidebar()
            else:
                S.mode = "idle"
                think(4.0, 11.0)
                S.mode = "writing"

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
        S.expect = None


# ───────────────────────── STARTUP ─────────────────────────
def init_git():
    git = shutil.which("git")
    if not git or (ROOT / ".git").exists():
        return

    def g(*args):
        subprocess.run(
            [git, "-c", "user.name=dev", "-c", "user.email=dev@localhost", *args],
            cwd=ROOT, capture_output=True, timeout=20,
        )
    try:
        g("init")
        g("add", "-A")
        g("commit", "-m", "initial commit")
    except Exception as e:
        log(f"git setup skipped: {e!r}")

def prepare_folder():
    ROOT.mkdir(parents=True, exist_ok=True)
    for rel, kind, seed in CORE_FILES:
        register_file(rel, kind, seed)
    gi = ROOT / ".gitignore"
    if not gi.exists():
        gi.write_text("__pycache__/\n.vscode/\n")
    vs = ROOT / ".vscode"
    vs.mkdir(exist_ok=True)
    wanted = json.dumps(EDITOR_SETTINGS, indent=2)
    sf = vs / "settings.json"
    try:
        same = sf.read_text() == wanted
    except Exception:
        same = False
    if not same:                             # avoid needless settings reloads
        sf.write_text(wanted)
    init_git()

def open_vscode():
    prepare_folder()                         # always first, so S.files is never empty

    existing = vsc_windows()
    if existing:
        log("reusing already-open VS Code window")
        force_activate(existing[0])
        time.sleep(1.4)
        if in_vsc():
            return True

    code = shutil.which("code")
    if not code:
        log("'code' not found on PATH")
        return False

    log(f"launching VS Code → {ROOT}")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [code, "--disable-workspace-trust", "--new-window",
         str(ROOT), str(ROOT / "src" / "main.py")],
        creationflags=flags,
    )
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline and not stop.is_set():
        wins = vsc_windows()
        if wins:
            if in_vsc():
                log("VS Code ready")
                time.sleep(2.2)
                return True
            force_activate(wins[0])          # window exists but isn't foreground yet
        time.sleep(1)
    log("VS Code did not appear in time")
    return False

def main():
    global last_input
    log("=" * 56)
    log(f"{PAUSE_KEY} pause/resume  |  {QUIT_KEY} quit  |  top-left = failsafe")
    log("=" * 56)

    if not open_vscode():
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
        log("failsafe – stopping")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for l in listeners:
            l.stop()
        log("session ended")


if __name__ == "__main__":
    main()