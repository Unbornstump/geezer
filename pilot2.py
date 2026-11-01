"""
ghostwriter.py  -  an "intern" that keeps a VS Code project busy while you're away.

DESIGN RULE (this is what fixes the corruption bug):
    The file on DISK is the source of truth, never a counter in memory.
    - Lines are typed in small groups, then saved, then the disk is read back and
      compared with what we EXPECTED. Only then are they considered "done".
    - If anything goes wrong (you grabbed the mouse, focus was stolen, a key got
      dropped), unsaved edits are simply thrown away with "File: Revert File".
      No backspace counting, no guessing.

pip install pyautogui pygetwindow pynput        (pynput >= 1.7.7)
Assumes: Windows, US keyboard layout, `code` on PATH.
F8 = pause/resume   F9 = quit   mouse to TOP-LEFT corner = emergency stop
"""
import json, logging, math, random, shutil, subprocess, sys, threading, time
from pathlib import Path
import pyautogui
import pygetwindow as gw
from pynput import keyboard, mouse

# ───────────────────────── CONFIG ─────────────────────────
BASE_DIR      = Path(r"C:\devtools")
FOLDER_NAME   = "mine-" + time.strftime("%Y%m%d-%H%M%S")
ROOT          = BASE_DIR / FOLDER_NAME
PY            = "python"          # or "py"
CLEAR         = "cls"             # "clear" for bash

IDLE_AFTER    = 9                 # seconds of human inactivity before the bot acts
GAP_BETWEEN   = (2, 12)           # seconds between actions
LONG_BREAK_CHANCE = 0.06          # chance of a "went for coffee" pause after an action
LONG_BREAK    = (20, 75)
MAX_LINES     = (25, 70)          # per-file size before it gets tidied
MAX_FILES     = 60

TYPING_SPEED  = 1.3               # bigger = SLOWER
TYPO_CHANCE   = 0.025
MICRO_STOP    = 0.06              # per-char chance of a tiny hesitation
ZONE          = (0.30, 0.72, 0.22, 0.48)   # safe mouse area inside the VS Code window
                                           # (x0, x1, y0, y1 as fractions). Tune to your layout.
PAUSE_KEY, QUIT_KEY = "<f8>", "<f9>"
LAUNCH_TIMEOUT = 45

EDITOR_SETTINGS = {
    "editor.autoIndent": "none",
    "editor.autoClosingBrackets": "never",
    "editor.autoClosingQuotes": "never",
    "editor.autoClosingOvertype": "never",
    "editor.autoClosingComments": "never",
    "editor.autoSurround": "never",
    "editor.formatOnType": False,
    "editor.formatOnSave": False,
    "editor.formatOnPaste": False,
    "editor.quickSuggestions": {"other": False, "comments": False, "strings": False},
    "editor.suggestOnTriggerCharacter": False,
    "editor.acceptSuggestionOnEnter": "off",
    "editor.acceptSuggestionOnCommitCharacter": False,
    "editor.wordBasedSuggestions": "off",
    "editor.parameterHints.enabled": False,
    "editor.inlineSuggest.enabled": False,
    "editor.snippetSuggestions": "none",
    "editor.tabCompletion": "off",
    "editor.dragAndDrop": False,
    "editor.lightbulb.enabled": "off",
    "editor.insertSpaces": True,
    "editor.tabSize": 4,
    "editor.detectIndentation": False,
    "files.autoSave": "off",
    "files.eol": "\n",
    "files.insertFinalNewline": False,
    "files.trimTrailingWhitespace": False,
    "files.trimFinalNewlines": False,
    "files.hotExit": "off",
    "git.enabled": False,
    "python.analysis.autoImportCompletions": False,
    "terminal.integrated.confirmOnKill": "never",
    "terminal.integrated.showExitAlert": False,
    "window.confirmBeforeClose": "never",
    "window.title": "${dirty}${activeEditorShort}${separator}${rootName}${separator}${appName}",
    "workbench.startupEditor": "none",
    "workbench.tips.enabled": False,
    "workbench.editor.enablePreview": False,
    # the Python extension turns formatOnType back on for [python]; override it
    "[python]": {"editor.formatOnType": False, "editor.autoIndent": "none",
                 "editor.wordBasedSuggestions": "off"},
}

# ───────────────────────── PROJECT SKELETON ─────────────────────────
# (relative path, kind, seed text).  Basenames MUST be unique (we open files by name).
INITIAL_FILES = [
    ("README.md", "md", "# toolkit\n\nSmall scratch project for experiments.\n"),
    ("CHANGELOG.md", "md", "# Changelog\n"),
    ("requirements.txt", "req", "requests==2.32.3\n"),
    ("notes/todo.txt", "txt", "TODO\n----\n"),
    ("notes/ideas.md", "md", "# Ideas\n"),
    ("notes/journal.txt", "txt", "journal\n"),
    ("config/settings.yaml", "yaml", "app:\n  name: toolkit\n"),
    ("config/defaults.toml", "toml", "[defaults]\nretries = 3\n"),
    ("data/sample.csv", "csv", "id,name,score\n1,amani,88\n"),
    ("data/events.jsonl", "jsonl", '{"id": 0, "event": "boot", "ok": true}\n'),
    ("scripts/run_all.sh", "sh", "#!/bin/sh\n"),
    ("sql/schema.sql", "sql", "-- schema\n"),
    ("web/index.html", "html", "<!-- scratch page -->\n"),
    ("web/style.css", "css", "/* styles */\n"),
    ("web/app.js", "js", "// app\n"),
    ("src/main.py", "py", "# classified\nprint('...ready')\n"),
    ("src/utils.py", "py", "def helper():\n    return 42\n"),
    ("src/temp.py", "py", "# scratch\n"),
    ("src/helpers/strings.py", "py", "# string helpers\n"),
    ("src/helpers/mathx.py", "py", "# math helpers\n"),
    ("src/helpers/collectionsx.py", "py", "# collection helpers\n"),
    ("src/core/models.py", "py", "# models\n"),
    ("src/core/parser.py", "py", "# parser\n"),
    ("src/core/cache.py", "py", "# cache\n"),
    ("src/core/retry.py", "py", "# retry\n"),
    ("src/services/timing.py", "py", "# timing\n"),
    ("src/services/ioutil.py", "py", "# io\n"),
    ("tests/test_utils.py", "test", "# tests for utils\n"),
    ("tests/test_math.py", "test", "# tests for math\n"),
    ("tests/test_parser.py", "test", "# tests for parser\n"),
    ("tools/scratch1.py", "py", "# scratch 1\n"),
]
KIND_WEIGHT = {"py": 6, "test": 1.5, "md": 1, "txt": 1, "req": 0.2, "yaml": 0.5, "toml": 0.3,
               "csv": 0.5, "jsonl": 0.5, "sql": 0.6, "sh": 0.3, "html": 0.6, "css": 0.6, "js": 0.8}
NO_BLANK = {"csv", "jsonl", "req"}      # row-style files: no blank line between snippets

NOUNS = ["user", "order", "item", "price", "score", "token", "queue", "cache", "event", "route",
         "session", "report", "tag", "node", "level", "packet", "record", "metric", "job", "count",
         "index", "buffer", "limit", "delta", "batch", "stream", "key", "entry"]
VERBS = ["compute", "parse", "filter", "merge", "count", "scan", "build", "load", "check", "clean",
         "split", "pick", "rank", "group", "sum", "fetch", "trim", "map"]
NAMES = ["amani", "baraka", "wanjiru", "otieno", "zuri", "kamau", "neema", "juma"]
COMMENTS = ["# TODO: tidy", "# NOTE: edge case", "# quick fix", "# check later", "# why does this work",
            "# FIXME", "# keep", "# revisit", "# slow?", "# ok for now"]


# ───────────────────────── SNIPPET LIBRARY ─────────────────────────
def _n(): return random.choice(NOUNS)
def _v(): return random.choice(VERBS)

def gen_func():
    n, fn = _n(), f"{_v()}_{_n()}"
    return [f"def {fn}({n}s):", "    total = 0", f"    for {n} in {n}s:",
            f"        total += {n} * {random.randint(2, 9)}", "    return total", "",
            f"print({fn}(list(range({random.randint(3, 12)}))))"]

def gen_filter():
    fn = f"{_v()}_{_n()}s"
    vals = ", ".join(str(random.randint(0, 12)) for _ in range(6))
    return [f"def {fn}(values, limit={random.randint(2, 8)}):", "    out = []", "    for v in values:",
            "        if v > limit:", "            out.append(v)", "    return out", "", f"print({fn}([{vals}]))"]

def gen_class():
    c, a = _n().capitalize(), _n()
    b = random.choice([x for x in NOUNS if x != a])
    return [f"class {c}:", f"    def __init__(self, {a}, {b}=0):", f"        self.{a} = {a}",
            f"        self.{b} = {b}", "", "    def total(self):", f"        return self.{a} + self.{b}", "",
            "    def __repr__(self):", f"        return f'{c}({{self.{a}!r}}, {{self.{b}!r}})'", "",
            f"print({c}({random.randint(1, 20)}, {random.randint(1, 20)}).total())"]

def gen_dictcomp():
    names = random.sample(NOUNS, 3)
    return [f"{names[0]}s = {names}", f"sizes = {{n: len(n) for n in {names[0]}s}}", "print(sizes)"]

def gen_generator():
    fn = f"iter_{_n()}s"
    return [f"def {fn}(n):", "    i = 0", "    while i < n:", f"        yield i * {random.randint(2, 5)}",
            "        i += 1", "", f"print(list({fn}({random.randint(4, 9)})))"]

def gen_try():
    return ["def safe_div(a, b):", "    try:", "        return a / b", "    except ZeroDivisionError:",
            "        return None", "", f"print(safe_div({random.randint(1, 50)}, {random.choice([0, 3, 7])}))"]

def gen_text():
    w = random.choice(["hello world", "python is fun", "type some code", "keep it simple"])
    return [f"def {_v()}_text(s):", "    words = s.split()", "    return ' '.join(w.capitalize() for w in words)",
            "", f"print({_v()}_text('{w}'))"][:3] + ["", f"print('{w}'.title())"]

def gen_consts():
    return [f"{_n().upper()}_LIMIT = {random.randint(10, 500)}", f"{_n().upper()}_NAME = '{_n()}'"]

def gen_loop():
    return [f"for i in range({random.randint(2, 6)}):", f"    print(i, i ** {random.randint(2, 3)})"]

def gen_test():
    fn, a, b = f"add_{_n()}", random.randint(1, 20), random.randint(1, 20)
    return [f"def {fn}(a, b):", "    return a + b", "", f"assert {fn}({a}, {b}) == {a + b}",
            f"assert {fn}(0, 0) == 0", "print('ok')"]

def gen_test2():
    vals = [random.randint(0, 30) for _ in range(5)]
    return [f"def biggest(xs):", "    return max(xs)", "", f"assert biggest({vals}) == {max(vals)}",
            f"assert biggest([{vals[0]}]) == {vals[0]}", "print('ok')"]

PY_STATIC = [
    ["import random", "", "def roll(sides=6):", "    return random.randint(1, sides)", "", "print(roll())"],
    ["def fib(n):", "    a, b = 0, 1", "    for _ in range(n):", "        a, b = b, a + b", "    return a", "", "print([fib(i) for i in range(10)])"],
    ["def factorial(n):", "    if n <= 1:", "        return 1", "    return n * factorial(n - 1)", "", "print(factorial(5))"],
    ["def is_prime(n):", "    if n < 2:", "        return False", "    for i in range(2, int(n ** 0.5) + 1):", "        if n % i == 0:", "            return False", "    return True", "", "print([n for n in range(30) if is_prime(n)])"],
    ["from collections import Counter", "", "text = 'the quick brown fox jumps over the lazy dog the end'", "counts = Counter(text.split())", "print(counts.most_common(3))"],
    ["def reverse(s):", "    return s[::-1]", "", "print(reverse('hello'))"],
    ["squares = [i ** 2 for i in range(20)]", "print(squares)"],
    ["class Stack:", "    def __init__(self):", "        self.items = []", "", "    def push(self, item):", "        self.items.append(item)", "", "    def pop(self):", "        return self.items.pop()", "", "s = Stack()", "s.push(1)", "s.push(2)", "print(s.pop())"],
    ["for i in range(1, 16):", "    if i % 15 == 0:", "        print('FizzBuzz')", "    elif i % 3 == 0:", "        print('Fizz')", "    elif i % 5 == 0:", "        print('Buzz')", "    else:", "        print(i)"],
    ["from pathlib import Path", "", "lines = Path(__file__).read_text().splitlines()", "print(len(lines), 'lines')"],
    ["def c_to_f(c):", "    return c * 9 / 5 + 32", "", "for t in (0, 25, 100):", "    print(t, c_to_f(t))"],
    ["pairs = [('b', 2), ('a', 3), ('c', 1)]", "pairs.sort(key=lambda p: p[1])", "print(pairs)"],
    ["def gcd(a, b):", "    while b:", "        a, b = b, a % b", "    return a", "", "print(gcd(48, 18))"],
    ["import time", "", "def timed(fn):", "    def wrapper(*args):", "        start = time.time()", "        result = fn(*args)", "        print('took', round(time.time() - start, 4))", "        return result", "    return wrapper", "", "@timed", "def work(n):", "    return sum(range(n))", "", "print(work(100000))"],
    ["value = '42'", "try:", "    number = int(value)", "except ValueError:", "    print('not a number')", "else:", "    print(number + 1)"],
    ["from dataclasses import dataclass", "", "@dataclass", "class User:", "    name: str", "    age: int", "", "u = User('amani', 30)", "print(u)"],
    ["fruits = ['mango', 'banana', 'pawpaw']", "for i, f in enumerate(fruits, start=1):", "    print(i, f)"],
    ["a = [1, 2, 3]", "b = ['x', 'y', 'z']", "print(list(zip(a, b)))"],
    ["def flatten(nested):", "    out = []", "    for item in nested:", "        if isinstance(item, list):", "            out.extend(flatten(item))", "        else:", "            out.append(item)", "    return out", "", "print(flatten([1, [2, [3, 4]], 5]))"],
    ["def bubble_sort(arr):", "    n = len(arr)", "    for i in range(n):", "        for j in range(n - i - 1):", "            if arr[j] > arr[j + 1]:", "                arr[j], arr[j + 1] = arr[j + 1], arr[j]", "    return arr", "", "print(bubble_sort([5, 2, 9, 1, 7]))"],
    ["def binary_search(items, target):", "    lo, hi = 0, len(items) - 1", "    while lo <= hi:", "        mid = (lo + hi) // 2", "        if items[mid] == target:", "            return mid", "        if items[mid] < target:", "            lo = mid + 1", "        else:", "            hi = mid - 1", "    return -1", "", "print(binary_search([1, 3, 5, 7, 9], 7))"],
    ["import json", "", "data = {'name': 'demo', 'tags': ['a', 'b']}", "print(json.dumps(data, indent=2))"],
    ["from datetime import datetime", "", "print(datetime.now().strftime('%Y-%m-%d %H:%M'))"],
    ["matrix = [[1, 2, 3], [4, 5, 6]]", "print([list(row) for row in zip(*matrix)])"],
    ["nums = [3, 8, 1, 9, 4]", "print(list(filter(lambda n: n % 2 == 0, nums)), list(map(lambda n: n * 2, nums)))"],
    ["def average(values):", "    if not values:", "        return 0", "    return sum(values) / len(values)", "", "print(average([4, 8, 15, 16, 23, 42]))"],
    ["name = 'dunia'", "print(f'habari, {name}!')"],
    ["# TODO: clean this up later", "# notes: keep helpers small"],
    ["print('pokea salamu kutoka wachawi wa kwenu')"],
    ["from collections import defaultdict", "", "groups = defaultdict(list)", "for word in ['apple', 'avocado', 'banana', 'blueberry', 'cherry']:", "    groups[word[0]].append(word)", "print(dict(groups))"],
    ["def clamp(n, lo, hi):", "    return max(lo, min(n, hi))", "", "print(clamp(15, 0, 10))"],
    ["import os", "", "print(os.getcwd())"],
    ["from contextlib import contextmanager", "", "@contextmanager", "def section(title):", "    print('>>', title)", "    yield", "    print('<<', title)", "", "with section('demo'):", "    print('inside')"],
    ["import itertools", "", "print(list(itertools.combinations([1, 2, 3, 4], 2)))"],
    ["class Counter2:", "    count = 0", "", "    @classmethod", "    def bump(cls):", "        cls.count += 1", "        return cls.count", "", "print(Counter2.bump(), Counter2.bump())"],
]
PY_GENERATORS = [gen_func, gen_filter, gen_class, gen_dictcomp, gen_generator, gen_try, gen_text,
                 gen_consts, gen_loop]
TEST_GENERATORS = [gen_test, gen_test2]

def gen_csv():  return [f"{random.randint(2, 99)},{random.choice(NAMES)},{random.randint(40, 100)}"]
def gen_jsonl():
    return [json.dumps({"id": random.randint(1, 999), "event": random.choice(["login", "view", "click", "logout"]),
                        "ok": random.random() < 0.8})]

POOLS = {
    "md": [["## Setup", "", "Install the deps, then run `python src/main.py`.", ""],
           ["## Notes", "", "- keep helpers small", "- write the test first", "- clean up before friday", ""],
           ["### Usage", "", "```", "python src/main.py", "```", ""],
           ["Plan for the week:", "", "1. tidy utils", "2. add a few tests", "3. update the docs", ""],
           ["> remember to rename temp.py once it grows", ""],
           ["## Ideas", "", "- small cli wrapper", "- cache the slow parts", ""]],
    "txt": [["- fix the parser edge case"], ["- add tests for retry"], ["- rename temp.py"],
            ["- look at caching again"], ["- update readme"], ["- ask about the data format"]],
    "req": [["rich==13.7.1"], ["pytest==8.2.0"], ["python-dateutil==2.9.0"], ["pyyaml==6.0.1"]],
    "yaml": [["debug: false"], ["retries: 3", "timeout: 30"], ["paths:", "  data: ./data", "  logs: ./logs"],
             ["features:", "  cache: true", "  verbose: false"]],
    "toml": [["[tool.demo]", 'name = "demo"', 'version = "0.1.0"'], ["[logging]", 'level = "INFO"'],
             ["[limits]", "max_items = 100", "max_depth = 4"]],
    "csv": [gen_csv], "jsonl": [gen_jsonl],
    "sql": [["SELECT id, name FROM users WHERE active = 1;"], ["CREATE TABLE tags (", "  id INTEGER PRIMARY KEY,", "  label TEXT", ");"],
            ["SELECT count(*) FROM events;"], ["CREATE INDEX idx_events_id ON events (id);"]],
    "sh": [["echo 'starting'"], ["python src/main.py"], ["echo 'done'"], ["ls -la"]],
    "html": [["<h1>toolkit</h1>"], ["<ul>", "  <li>one</li>", "  <li>two</li>", "</ul>"],
             ["<p>scratch notes go here</p>"], ["<button id=\"go\">Go</button>"]],
    "css": [["body {", "  margin: 0;", "  font-family: sans-serif;", "}"], [".card {", "  padding: 8px;", "  border: 1px solid #ccc;", "}"],
            ["h1 {", "  font-size: 24px;", "}"]],
    "js": [["function add(a, b) {", "  return a + b;", "}"], ["const items = [1, 2, 3];", "console.log(items.map(n => n * 2));"],
           ["document.title = 'toolkit';"], ["function clamp(n, lo, hi) {", "  return Math.max(lo, Math.min(n, hi));", "}"]],
}

def make_snippet(kind):
    if kind == "py":
        return list(random.choice(PY_STATIC)) if random.random() < 0.4 else random.choice(PY_GENERATORS)()
    if kind == "test":
        return random.choice(TEST_GENERATORS)()
    item = random.choice(POOLS[kind])
    return item() if callable(item) else list(item)


# ───────────────────────── LOGGING / SAFETY ─────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.info
pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0


# ───────────────────────── SHARED STATE ─────────────────────────
running = threading.Event()   # set = go
stop    = threading.Event()
running.set()

last_input = time.time()      # last time a HUMAN touched mouse/keyboard
action_started = 0.0
bot_ts = 0.0                  # last time WE sent input (fallback human-detection)
USE_TIME_FILTER = False


class State:
    def __init__(self):
        self.files = {}        # rel -> kind
        self.model = {}        # rel -> text we KNOW is saved on disk
        self.limit = {}
        self.cur = "src/main.py"
        self.snippet, self.sidx, self.snip_file = [], 0, None   # sidx = lines already SAVED
        self.needs_heal = False
        self.term_open = False
        self.fails = 0
        self.open_fails = {}
        self.force_reload = set()
        self.expect = None     # tab name that must be active while we type
        self.rhythm = 1.0
        self.burst = 0


S = State()


class Interrupted(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ───────────────────────── INPUT PRIMITIVES ─────────────────────────
def mark():
    global bot_ts
    bot_ts = time.time()

def kb_write(s):            mark(); pyautogui.write(s)
def kb_press(k, n=1, iv=0): mark(); pyautogui.press(k, presses=n, interval=iv)
def kb_hotkey(*k):          mark(); pyautogui.hotkey(*k)
def ms_down(b="left"):      mark(); pyautogui.mouseDown(button=b)
def ms_up(b="left"):        mark(); pyautogui.mouseUp(button=b)
def ms_scroll(n):           mark(); pyautogui.scroll(n)

def ms_move(x, y):
    mark()
    sw, sh = pyautogui.size()
    pyautogui.moveTo(min(max(int(x), 3), sw - 4), min(max(int(y), 3), sh - 4))


# ───────────────────────── HUMAN-ACTIVITY LISTENERS ─────────────────────────
def _human():
    global last_input
    if USE_TIME_FILTER and time.time() - bot_ts < 0.3:
        return
    last_input = time.time()

def on_press(key, injected=False):
    if not injected: _human()
def on_move(x, y, injected=False):
    if not injected: _human()
def on_click(x, y, button, pressed, injected=False):
    if not injected: _human()
def on_scroll(x, y, dx, dy, injected=False):
    if not injected: _human()

def toggle():
    if running.is_set(): running.clear(); log("paused")
    else:                running.set();   log("resumed")

def quit_now():
    log("quit requested"); stop.set()

listeners = [
    keyboard.GlobalHotKeys({PAUSE_KEY: toggle, QUIT_KEY: quit_now}),
    keyboard.Listener(on_press=on_press),
    mouse.Listener(on_move=on_move, on_click=on_click, on_scroll=on_scroll),
]
for _l in listeners:
    _l.daemon = True
    _l.start()


def self_test_injection():
    """Make sure our own synthetic input is NOT seen as human. Otherwise fall back to a timing filter."""
    global USE_TIME_FILTER
    before = last_input
    pyautogui.moveRel(1, 0); pyautogui.moveRel(-1, 0)
    time.sleep(0.4)
    if last_input != before:
        USE_TIME_FILTER = True
        log("injected-flag not reliable here, using timing filter instead")


# ───────────────────────── WINDOW HELPERS ─────────────────────────
def active_title():
    try:
        w = gw.getActiveWindow()
        return w.title if w else ""
    except Exception:
        return ""

def is_vsc_title(t):
    t = t.lower()
    return "visual studio code" in t or "code - oss" in t

def in_vsc(t=None):
    t = active_title() if t is None else t
    return FOLDER_NAME.lower() in t.lower() and is_vsc_title(t)

def title_parts(t=None):
    """-> (active tab name, has unsaved changes).  VS Code puts '● ' in the title when dirty."""
    t = active_title() if t is None else t
    first = t.split(" - ")[0].strip()
    return first.lstrip("●").strip(), first.startswith("●")

def vsc_windows():
    try:
        return [w for w in gw.getWindowsWithTitle(FOLDER_NAME) if is_vsc_title(w.title)]
    except Exception:
        return []

def bring_back():
    if in_vsc():
        return True
    wins = vsc_windows()
    if not wins:
        log("our VS Code window is gone, reopening")
        return open_vscode()
    w = wins[0]
    log("bringing VS Code back to the front")
    for _ in range(3):
        try:
            if w.isMinimized: w.restore()
            w.activate()
        except Exception:
            pass
        time.sleep(0.7)
        if in_vsc(): return True
        try:
            w.minimize(); time.sleep(0.3); w.restore()
        except Exception:
            pass
        time.sleep(0.7)
        if in_vsc(): return True
    return False


# ───────────────────────── THE GUARD ─────────────────────────
def guard(need_vsc=True):
    """Called before EVERY input. Blocks while paused; aborts on human input, lost focus, wrong tab."""
    while not running.wait(0.2):
        if stop.is_set(): raise Interrupted("quit")
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
    guard(); kb_hotkey(*keys)

def nap(seconds):
    end = time.time() + seconds
    while True:
        left = end - time.time()
        if left <= 0: break
        time.sleep(min(0.25, left))
        guard()

def think(lo, hi):
    d = random.uniform(lo, hi)
    if d > 1.5 and random.random() < 0.35:
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
    p.write_bytes(text.encode("utf-8"))       # bytes => no CRLF translation

def as_text(lines):
    return "".join(l + "\n" for l in lines)

def line_count(text):
    return text.count("\n")

def register_file(rel, kind, seed):
    if not (ROOT / rel).exists():
        write_disk(rel, seed)
    S.files[rel] = kind
    S.model[rel] = read_disk(rel)
    S.limit[rel] = random.randint(*MAX_LINES)

def verify_models():
    """You edited something yourself? Adopt the disk version; never fight it."""
    for rel in list(S.files):
        d = read_disk(rel)
        if d != S.model[rel]:
            log(f"{rel} changed on disk, adopting it")
            S.model[rel] = d
            if S.snip_file == rel:
                S.snippet, S.snip_file, S.sidx = [], None, 0


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
    bend = random.uniform(-0.22, 0.22) * dist
    c1 = (sx + dx * 0.30 + nx * bend, sy + dy * 0.30 + ny * bend)
    c2 = (sx + dx * 0.75 + nx * bend * 0.5, sy + dy * 0.75 + ny * bend * 0.5)
    steps = max(10, int(dur / 0.012))
    for i in range(1, steps + 1):
        t = _ease(i / steps)
        u = 1 - t
        x = u**3 * sx + 3 * u * u * t * c1[0] + 3 * u * t * t * c2[0] + t**3 * tx
        y = u**3 * sy + 3 * u * u * t * c1[1] + 3 * u * t * t * c2[1] + t**3 * ty
        if i < steps:
            x += random.uniform(-0.7, 0.7); y += random.uniform(-0.7, 0.7)
        ms_move(round(x), round(y))
        if i % 5 == 0:
            guard(False)
        time.sleep(dur / steps)
    ms_move(round(tx), round(ty))

def human_move(tx, ty, speed=1.0):
    guard(False)
    sx, sy = pyautogui.position()
    dist = math.hypot(tx - sx, ty - sy)
    if dist < 2:
        return
    dur = (0.20 + 0.14 * math.log2(1 + dist / 50)) * random.uniform(0.8, 1.35) / speed
    if dist > 300 and random.random() < 0.35:            # overshoot, then correct
        ux, uy = (tx - sx) / dist, (ty - sy) / dist
        o = random.uniform(8, 26)
        _leg(tx + ux * o, ty + uy * o, dur)
        time.sleep(random.uniform(0.04, 0.12))
        _leg(tx, ty, random.uniform(0.12, 0.25))
    else:
        _leg(tx, ty, dur)

def human_click(x, y, clicks=1, button="left"):
    human_move(x, y)
    time.sleep(random.uniform(0.05, 0.25))
    for c in range(clicks):
        guard()
        ms_down(button)
        time.sleep(random.uniform(0.04, 0.11))
        ms_up(button)
        if c < clicks - 1:
            time.sleep(random.uniform(0.06, 0.14))

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
    x = random.triangular(x0, x1, x0 + 0.25 * (x1 - x0)) if bias_left else random.uniform(x0, x1)
    return x, random.uniform(y0, y1)

def drift():
    """Tiny idle mouse wobble while 'thinking'."""
    x, y = pyautogui.position()
    x0, x1, y0, y1 = zone_rect()
    human_move(min(max(x + random.randint(-45, 45), x0), x1), min(max(y + random.randint(-30, 30), y0), y1), 0.6)

def mouse_hover():
    for _ in range(random.randint(2, 4)):
        human_move(*zone_point()); think(0.3, 1.6)

def mouse_read_line():
    x0, x1, y0, y1 = zone_rect()
    y = random.uniform(y0, y1)
    x = x0 + random.uniform(0, 0.1) * (x1 - x0)
    human_move(x, y)
    for _ in range(random.randint(3, 6)):
        x = min(x + random.uniform(40, 130), x1)
        human_move(x, y + random.uniform(-4, 4), 0.6)
        nap(random.uniform(0.05, 0.3))

def mouse_click_somewhere():
    human_click(*zone_point()); think(0.3, 1.0)

def mouse_double_click_word():
    human_click(*zone_point(), clicks=2); think(0.6, 1.6)
    human_click(*zone_point())                      # click away to deselect

def mouse_drag_select():
    x0, x1, y0, y1 = zone_rect()
    sx, sy = zone_point()
    ex, ey = min(sx + random.uniform(80, 320), x1), sy + random.choice([0, 0, 18, 36])
    human_move(sx, sy); time.sleep(random.uniform(0.08, 0.25))
    guard(); ms_down()
    try:
        time.sleep(random.uniform(0.05, 0.15))
        human_move(ex, ey, 0.7)
        time.sleep(random.uniform(0.4, 1.2))
    finally:
        ms_up()                                      # never leave the button stuck down
    think(0.5, 1.5)
    human_click(*zone_point())

def mouse_scroll():
    human_move(*zone_point(False))
    d = random.choice([-1, 1])
    for _ in range(random.randint(2, 6)):
        guard(); ms_scroll(d * random.choice([1, 2, 3])); time.sleep(random.uniform(0.06, 0.25))
    think(0.5, 1.5)
    if random.random() < 0.5:
        for _ in range(random.randint(1, 4)):
            guard(); ms_scroll(-d * random.choice([1, 2])); time.sleep(random.uniform(0.06, 0.25))

def mouse_context_peek():
    human_click(*zone_point(), button="right"); think(0.5, 1.2)
    hit("escape")

MOUSE_BEHAVIOURS = [(mouse_hover, 3), (mouse_read_line, 3), (mouse_click_somewhere, 3),
                    (mouse_double_click_word, 1.5), (mouse_drag_select, 1), (mouse_scroll, 3),
                    (mouse_context_peek, 0.6)]

def mouse_play():
    for _ in range(random.randint(1, 3)):
        fn = random.choices([f for f, _ in MOUSE_BEHAVIOURS], [w for _, w in MOUSE_BEHAVIOURS])[0]
        fn()
        think(0.3, 1.2)


# ───────────────────────── HUMAN-LIKE TYPING ─────────────────────────
_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
NEIGHBORS = {}
for _r, _row in enumerate(_ROWS):
    for _c, _ch in enumerate(_row):
        _nb = []
        if _c > 0: _nb.append(_row[_c - 1])
        if _c < len(_row) - 1: _nb.append(_row[_c + 1])
        if _r > 0:
            _up = _ROWS[_r - 1]; _nb += [_up[min(_c, len(_up) - 1)], _up[min(_c + 1, len(_up) - 1)]]
        if _r < 2:
            _dn = _ROWS[_r + 1]; _nb.append(_dn[min(_c, len(_dn) - 1)])
        NEIGHBORS[_ch] = [x for x in _nb if x != _ch]

def neighbor(ch):
    opts = NEIGHBORS.get(ch.lower())
    if not opts: return None
    w = random.choice(opts)
    return w.upper() if ch.isupper() else w

SHIFTED = set('_(){}[]:"<>?+|~!@#$%^&*')

def key_delay(prev, ch, speed):
    d = random.lognormvariate(math.log(0.14), 0.38) * TYPING_SPEED * speed * S.rhythm
    if prev == ch: d *= 0.85
    if ch.isupper() or ch in SHIFTED: d *= 1.25
    if ch in " .,;:": d += random.uniform(0.0, 0.08)
    if prev == " ": d += random.uniform(0.0, 0.10)
    if S.burst > 0:
        d *= 0.65; S.burst -= 1
    elif random.random() < 0.08:
        S.burst = random.randint(3, 9)               # a fast run of keys
    r = random.random()
    if r < MICRO_STOP:                d += random.uniform(0.25, 0.8)
    elif r < MICRO_STOP + 0.008:      d += random.uniform(1.2, 2.6)   # "thinking"
    return d

def emit(ch, prev, speed):
    guard(); kb_write(ch)
    time.sleep(key_delay(prev, ch, speed))

def backspaces(n):
    for _ in range(n):
        guard(); kb_press("backspace"); time.sleep(random.uniform(0.05, 0.13))

def type_text(text, typos=True, speed=1.0):
    i, prev = 0, ""
    while i < len(text):
        ch = text[i]
        if typos and i > 0 and ch.isalpha() and random.random() < TYPO_CHANCE:
            kind = random.choices(["adjacent", "late", "swap"], [5, 3, 2])[0]
            if kind == "swap" and i + 1 < len(text) and text[i + 1].isalpha() and text[i + 1] != ch:
                emit(text[i + 1], prev, speed); emit(ch, text[i + 1], speed)
                time.sleep(random.uniform(0.15, 0.45))
                backspaces(2)
                emit(ch, prev, speed); emit(text[i + 1], ch, speed)
                prev = text[i + 1]; i += 2
                continue
            wrong = neighbor(ch)
            if wrong:
                emit(wrong, prev, speed)
                extra = 0
                if kind == "late":                    # keep typing, notice a few chars later
                    for c in text[i + 1:i + 1 + random.randint(1, 3)]:
                        emit(c, wrong, speed); extra += 1
                time.sleep(random.uniform(0.15, 0.5))
                backspaces(1 + extra)
        emit(ch, prev, speed)
        prev = ch
        i += 1

def indent(lead, rel):
    if rel.endswith(".py") and lead % 4 == 0:
        for _ in range(lead // 4):
            guard(); kb_press("tab"); time.sleep(random.uniform(0.08, 0.22))
    else:
        for _ in range(lead):
            guard(); kb_write(" "); time.sleep(random.uniform(0.03, 0.08))

def type_line(rel, line):
    body = line.lstrip(" ")
    lead = len(line) - len(body)
    S.rhythm = min(max(S.rhythm * random.uniform(0.9, 1.1), 0.75), 1.35)
    if body.startswith(("def ", "class ")) and random.random() < 0.6:
        think(0.8, 2.5)
    if lead: indent(lead, rel)
    if body: type_text(body)
    nap(random.uniform(0.12, 0.5))
    hit("enter")
    if random.random() < 0.18:
        think(0.6, 2.2)


# ───────────────────────── EDITOR CONTROL ─────────────────────────
def focus_editor():
    hit("escape")
    hit("ctrl", "1")
    nap(0.3)

def palette(cmd, speed=0.55):
    hit("ctrl", "shift", "p"); nap(random.uniform(0.4, 0.8))
    type_text(cmd, typos=False, speed=speed)
    nap(random.uniform(0.4, 0.9))
    hit("enter"); nap(random.uniform(0.5, 0.9))

def quick_open(rel):
    S.expect = None
    hit("ctrl", "p"); nap(random.uniform(0.4, 0.8))
    type_text(rel, typos=False, speed=0.6)
    nap(random.uniform(0.4, 0.9))
    hit("enter"); nap(0.7)
    if title_parts()[0] != Path(rel).name:
        S.open_fails[rel] = S.open_fails.get(rel, 0) + 1
        raise Interrupted("tab")
    S.open_fails[rel] = 0

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
        nap(0.5)
        if not title_parts()[1]:
            return
    log("revert didn't clear it, undoing back to the saved state")
    focus_editor()
    for i in range(400):
        hit("ctrl", "z")
        if i % 10 == 9 and not title_parts()[1]:
            return
        time.sleep(0.02)
    raise Interrupted("cannot-clean")

def ensure_clean():
    if title_parts()[1]:
        log("unexpected unsaved changes, discarding them")
        revert_until_clean()

def maybe_click_editor(p=0.35):
    if random.random() < p:
        human_click(*zone_point()); nap(random.uniform(0.2, 0.6))

def commit(rel, expected):
    """Save, then PROVE the disk matches what we expected. Raises if not."""
    hit("ctrl", "s")
    t0 = time.time()
    while True:
        time.sleep(0.15)
        if not title_parts()[1]:
            break
        guard()
        if time.time() - t0 > 3:
            raise Interrupted("save-timeout")
    time.sleep(0.15)
    if read_disk(rel) == expected:
        S.model[rel] = expected
        S.fails = 0
        return
    log(f"MISMATCH in {rel}: disk != expected, restoring")
    S.fails += 1
    write_disk(rel, S.model[rel])               # put the last good version back
    S.force_reload.add(rel)                     # and make the editor reload it
    if S.fails >= 3:
        log("giving up on this snippet")
        S.snippet, S.snip_file, S.sidx, S.fails = [], None, 0, 0
    raise Interrupted("mismatch")

def heal():
    """We were cut off. Clean the editor back to the last SAVED state, nothing more."""
    log("healing after interruption")
    for _ in range(2):
        hit("escape"); nap(0.25)
    if S.term_open:
        palette("Terminal: Kill All Terminals")
        S.term_open = False
    focus_editor()
    S.expect = None
    if title_parts()[1]:
        log("discarding unsaved edits")
        revert_until_clean()
    for rel in list(S.force_reload):
        open_file(rel)
        reload_active()
        S.force_reload.discard(rel)
        S.expect = None


# ───────────────────────── ACTIONS ─────────────────────────
def pick_file():
    ok = lambda r: S.open_fails.get(r, 0) < 3
    if S.cur in S.files and ok(S.cur) and random.random() < 0.55:
        return S.cur
    cands = [r for r in S.files if ok(r)]
    return random.choices(cands, [KIND_WEIGHT.get(S.files[r], 1) for r in cands])[0]

def start_snippet(rel=None):
    rel = rel or pick_file()
    kind = S.files[rel]
    lines = make_snippet(kind)
    text = S.model[rel]
    if kind not in NO_BLANK and text.strip() and not text.endswith("\n\n"):
        lines = [""] + lines
    S.snip_file, S.snippet, S.sidx = rel, lines, 0

def flourish():
    """Between lines only: mouse drifts / scrolls / clicks. Caret is restored with ctrl+End."""
    r = random.random()
    if r < 0.12:
        drift()
    elif r < 0.18:
        human_click(*zone_point()); hit("ctrl", "end")
    elif r < 0.23:
        mouse_scroll(); hit("ctrl", "end")

def type_snippet(rel):
    k = S.sidx
    group = random.randint(1, 4)
    while k < len(S.snippet):
        type_line(rel, S.snippet[k])
        k += 1
        if k - S.sidx >= group or k == len(S.snippet):
            commit(rel, S.model[rel] + as_text(S.snippet[S.sidx:k]))
            S.sidx = k
            group = random.randint(1, 4)
        if k < len(S.snippet):
            flourish()

def tidy_file(rel):
    log(f"tidying {rel}")
    think(1.0, 2.5)
    lines = S.model[rel].splitlines()
    S.limit[rel] = random.randint(*MAX_LINES)
    if random.random() < 0.5 or len(lines) < 8:
        hit("ctrl", "a"); think(0.5, 1.2); hit("delete"); nap(0.3)
        commit(rel, "")
    else:
        k = len(lines) // 2
        hit("ctrl", "end")
        for _ in range(k):
            hit("shift", "up"); time.sleep(random.uniform(0.03, 0.08))
        think(0.5, 1.2); hit("delete"); nap(0.3)
        commit(rel, as_text(lines[:-k]))

def code_step(force=None):
    if not S.snippet:
        start_snippet(force)
    rel = S.snip_file
    open_file(rel)
    ensure_clean()
    maybe_click_editor()
    hit("ctrl", "end"); nap(0.2)
    type_snippet(rel)
    S.snippet, S.snip_file, S.sidx = [], None, 0
    if line_count(S.model[rel]) >= S.limit[rel]:
        tidy_file(rel)

def edit_comment_step():
    """Go back to an old line, add a trailing comment, save. Verified like everything else."""
    cands = [r for r, k in S.files.items() if k in ("py", "test") and S.open_fails.get(r, 0) < 3
             and not (S.snippet and S.snip_file == r)]
    random.shuffle(cands)
    rel, idx, lines = None, [], []
    for r in cands:
        lines = S.model[r].splitlines()
        idx = [i for i, l in enumerate(lines) if l.strip() and "#" not in l and not l.rstrip().endswith(("\\", ","))]
        if idx:
            rel = r; break
    if not rel:
        return code_step()
    i = random.choice(idx)
    open_file(rel); ensure_clean(); maybe_click_editor(0.2)
    hit("ctrl", "g"); nap(random.uniform(0.4, 0.8))
    type_text(str(i + 1), typos=False, speed=0.6); nap(0.3)
    hit("enter"); nap(0.4)
    hit("end"); nap(0.2)
    note = "  " + random.choice(COMMENTS)
    type_text(note)
    new = lines[:]
    new[i] += note
    commit(rel, as_text(new))

def key_browse():
    for _ in range(random.randint(2, 5)):
        k = random.choice(["up", "down", "pageup", "pagedown"])
        for _ in range(random.randint(1, 6)):
            hit(k); time.sleep(random.uniform(0.05, 0.15))
        nap(random.uniform(0.4, 1.2))

def navigate_read_step():
    cands = [r for r in S.files if S.open_fails.get(r, 0) < 3]
    open_file(random.choice(cands))
    think(0.5, 1.2)
    for _ in range(random.randint(2, 4)):
        random.choice([key_browse, mouse_scroll, mouse_read_line, mouse_click_somewhere])()
        think(0.3, 1.0)
    if random.random() < 0.3:
        hit("ctrl", "home"); think(0.5, 1.5)

def terminal_step():
    cands = [r for r, k in S.files.items() if k in ("py", "test") and S.model[r].strip()
             and S.open_fails.get(r, 0) < 3 and not (S.snippet and S.snip_file == r)]
    if not cands:
        return navigate_read_step()
    rel = random.choice(cands)
    open_file(rel); ensure_clean()
    think(0.5, 1.5)
    S.term_open = True
    palette("Terminal: Create New Terminal")     # always a FRESH terminal: focus is guaranteed
    nap(random.uniform(1.8, 3.0))
    cmd = random.choice([f"{PY} {rel}"] * 5 + [f"{PY} --version", "dir", "pip --version"])
    type_text(cmd, speed=0.8)
    nap(random.uniform(0.3, 1.0)); hit("enter")
    think(2.0, 5.0)
    if random.random() < 0.4:
        type_text(CLEAR, typos=False, speed=0.8); hit("enter"); nap(0.5)
    type_text("exit", typos=False, speed=0.8); hit("enter"); nap(0.8)
    S.term_open = False
    focus_editor()

def make_new_file_spec():
    dirs = ["src/helpers", "src/core", "src/services", "tests", "tools", "scripts", "notes"]
    used = {Path(r).name for r in S.files}
    for _ in range(40):
        d = random.choice(dirs)
        if d == "tests":
            rel, kind, seed = f"tests/test_{_n()}_{_v()}.py", "test", "# tests\n"
        elif d == "notes":
            rel, kind, seed = f"notes/{_n()}_{random.choice(['log', 'plan', 'draft'])}.md", "md", "# notes\n"
        else:
            rel = f"{d}/{_n()}_{random.choice(['util', 'tool', 'step', 'lib', 'kit'])}.py"
            kind, seed = "py", f"# {Path(rel).stem}\n"
        if Path(rel).name not in used:
            return rel, kind, seed
    return None

def new_file_step():
    spec = make_new_file_spec() if len(S.files) < MAX_FILES else None
    if not spec:
        return code_step()
    rel, kind, seed = spec
    log(f"new file {rel}")
    think(1.0, 2.5)
    register_file(rel, kind, seed)               # appears in the explorer, like "New File"
    S.expect = None
    nap(2.0)                                     # let VS Code's file index catch up
    code_step(force=rel)

def think_step():
    think(3.0, 9.0)
    if random.random() < 0.5:
        mouse_hover()

def run_one_action():
    global action_started
    action_started = time.time()
    S.expect = None
    try:
        if not bring_back():
            log("couldn't bring VS Code back, will retry")
            return
        if S.needs_heal:
            heal()
            S.needs_heal = False
        verify_models()
        S.needs_heal = True
        if random.random() < 0.7:
            human_move(*zone_point()); time.sleep(random.uniform(0.2, 0.8))
        if S.snippet:                              # finish what we started first
            code_step()
        else:
            r = random.random()
            if   r < 0.40: code_step()
            elif r < 0.48: edit_comment_step()
            elif r < 0.60: navigate_read_step()
            elif r < 0.70: terminal_step()
            elif r < 0.84: mouse_play()
            elif r < 0.90: new_file_step()
            else:          think_step()
        S.needs_heal = False
    except Interrupted as e:
        log(f"action aborted: {e.reason}")         # needs_heal stays True => next action heals first
    except pyautogui.FailSafeException:
        raise
    except Exception as e:
        log(f"action error: {e!r}")                # a bug must never kill the loop
    finally:
        S.expect = None


# ───────────────────────── STARTUP ─────────────────────────
def prepare_folder():
    ROOT.mkdir(parents=True, exist_ok=True)
    for rel, kind, seed in INITIAL_FILES:
        if rel in S.files:
            continue
        register_file(rel, kind, seed)
    vs = ROOT / ".vscode"
    vs.mkdir(exist_ok=True)
    (vs / "settings.json").write_text(json.dumps(EDITOR_SETTINGS, indent=2))

def open_vscode():
    code = shutil.which("code")
    if not code:
        log("can't find 'code' on PATH. In VS Code: Ctrl+Shift+P > 'Shell Command: Install code in PATH'")
        return False
    prepare_folder()
    log(f"launching VS Code in {ROOT}")
    first = [str(ROOT / r) for r in ("src/main.py", "src/utils.py", "src/temp.py")]
    subprocess.Popen([code, "--disable-workspace-trust", "--new-window", str(ROOT), *first])
    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline and not stop.is_set():
        wins = vsc_windows()
        if wins:
            try: wins[0].activate()
            except Exception: pass
            time.sleep(4)
            if in_vsc():
                log("VS Code ready")
                return True
        time.sleep(1)
    log("VS Code didn't show up in time")
    return False

def main():
    global last_input
    log("=" * 50)
    log(f"{PAUSE_KEY} pause/resume | {QUIT_KEY} quit | mouse to top-left corner = failsafe")
    log("=" * 50)
    if not open_vscode():
        sys.exit(1)
    self_test_injection()
    last_input = time.time()
    next_at = time.time() + IDLE_AFTER
    try:
        while not stop.is_set():
            time.sleep(random.uniform(1.0, 2.5))
            if not running.is_set():
                continue
            if time.time() - last_input < IDLE_AFTER or time.time() < next_at:
                continue
            run_one_action()
            gap = random.uniform(*GAP_BETWEEN)
            if random.random() < LONG_BREAK_CHANCE:
                gap += random.uniform(*LONG_BREAK)
            next_at = time.time() + gap
    except pyautogui.FailSafeException:
        log("failsafe triggered, stopping")
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for l in listeners:
            l.stop()
        log("forged steel")


if __name__ == "__main__":
    main()