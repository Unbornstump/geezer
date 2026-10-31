import json, logging, random, shutil, subprocess, sys, threading, time
from pathlib import Path
import pyautogui
import pygetwindow as gw
from pynput import keyboard, mouse   # pynput >= 1.7.7 (for the `injected` flag)

# ───────────── CONFIG ─────────────
BASE_DIR      = Path(r"C:\devtools")
FOLDER_NAME   = "mine-" + time.strftime("%Y%m%d-%H%M%S")
TARGET_FOLDER = BASE_DIR / FOLDER_NAME
FILES         = ["main.py", "utils.py", "temp.py"]
PY            = "python"        # use "py" if that's what works in your terminal
CLEAR         = "cls"           # PowerShell/cmd. Use "clear" for bash

IDLE_AFTER    = 9               # seconds of human inactivity before the bot takes over
GAP_BETWEEN   = (2, 12)         # random seconds between actions
MAX_LINES     = (20, 40)        # wipe a file once it holds this many typed lines

TYPING_SPEED  = 1.7             # bigger = SLOWER
TYPO_CHANCE   = 0.02
MICRO_STOP    = 0.07            # chance per character of a tiny hesitation
PAUSE_KEY, QUIT_KEY = "<f8>", "<f9>"
LAUNCH_TIMEOUT = 45

# Workspace settings written into the folder, so VS Code can't "help" while we type:
# no auto-indent, no auto-closing brackets/quotes, no autocomplete eating Enter.
EDITOR_SETTINGS = {
    "editor.autoIndent": "none",
    "editor.autoClosingBrackets": "never",
    "editor.autoClosingQuotes": "never",
    "editor.autoClosingOvertype": "never",
    "editor.autoSurround": "never",
    "editor.formatOnType": False,
    "editor.formatOnSave": False,
    "editor.quickSuggestions": {"other": False, "comments": False, "strings": False},
    "editor.suggestOnTriggerCharacter": False,
    "editor.acceptSuggestionOnEnter": "off",
    "editor.acceptSuggestionOnCommitCharacter": False,
    "editor.wordBasedSuggestions": "off",
    "editor.parameterHints.enabled": False,
    "editor.inlineSuggest.enabled": False,
    "editor.tabCompletion": "off",
    "files.autoSave": "off",
    "workbench.startupEditor": "none",
    "workbench.editor.enablePreview": False,
}

# Each snippet = list of lines, 4-space indents typed literally.
# All are valid and self-contained, so running the file never spews errors.
SNIPPETS = [
    ["import random", "", "def roll(sides=6):", "    return random.randint(1, sides)", "", "print(roll())"],
    ["def fib(n):", "    a, b = 0, 1", "    for _ in range(n):", "        a, b = b, a + b", "    return a", "", "print([fib(i) for i in range(10)])"],
    ["def factorial(n):", "    if n <= 1:", "        return 1", "    return n * factorial(n - 1)", "", "print(factorial(5))"],
    ["def is_prime(n):", "    if n < 2:", "        return False", "    for i in range(2, int(n ** 0.5) + 1):", "        if n % i == 0:", "            return False", "    return True", "", "print([n for n in range(30) if is_prime(n)])"],
    ["from collections import Counter", "", "text = 'the quick brown fox jumps over the lazy dog the end'", "counts = Counter(text.split())", "print(counts.most_common(3))"],
    ["def reverse(s):", "    return s[::-1]", "", "print(reverse('hello'))"],
    ["squares = [i ** 2 for i in range(20)]", "print(squares)"],
    ["names = ['amani', 'baraka', 'wanjiru']", "lengths = {n: len(n) for n in names}", "print(lengths)"],
    ["class Point:", "    def __init__(self, x, y):", "        self.x = x", "        self.y = y", "", "    def dist(self):", "        return (self.x ** 2 + self.y ** 2) ** 0.5", "", "p = Point(3, 4)", "print(p.dist())"],
    ["class Stack:", "    def __init__(self):", "        self.items = []", "", "    def push(self, item):", "        self.items.append(item)", "", "    def pop(self):", "        return self.items.pop()", "", "s = Stack()", "s.push(1)", "s.push(2)", "print(s.pop())"],
    ["for i in range(1, 16):", "    if i % 15 == 0:", "        print('FizzBuzz')", "    elif i % 3 == 0:", "        print('Fizz')", "    elif i % 5 == 0:", "        print('Buzz')", "    else:", "        print(i)"],
    ["from pathlib import Path", "", "lines = Path('main.py').read_text().splitlines()", "print(len(lines), 'lines')"],
    ["def c_to_f(c):", "    return c * 9 / 5 + 32", "", "for t in (0, 25, 100):", "    print(t, c_to_f(t))"],
    ["pairs = [('b', 2), ('a', 3), ('c', 1)]", "pairs.sort(key=lambda p: p[1])", "print(pairs)"],
    ["def gcd(a, b):", "    while b:", "        a, b = b, a % b", "    return a", "", "print(gcd(48, 18))"],
    ["import time", "", "def timed(fn):", "    def wrapper(*args):", "        start = time.time()", "        result = fn(*args)", "        print('took', round(time.time() - start, 4))", "        return result", "    return wrapper", "", "@timed", "def work(n):", "    return sum(range(n))", "", "print(work(100000))"],
    ["value = '42'", "try:", "    number = int(value)", "except ValueError:", "    print('not a number')", "else:", "    print(number + 1)"],
    ["from dataclasses import dataclass", "", "@dataclass", "class User:", "    name: str", "    age: int", "", "u = User('amani', 30)", "print(u)"],
    ["def countdown(n):", "    while n > 0:", "        yield n", "        n -= 1", "", "print(list(countdown(5)))"],
    ["fruits = ['mango', 'banana', 'pawpaw']", "for i, f in enumerate(fruits, start=1):", "    print(i, f)"],
    ["a = [1, 2, 3]", "b = ['x', 'y', 'z']", "print(list(zip(a, b)))"],
    ["def is_palindrome(s):", "    s = s.lower()", "    return s == s[::-1]", "", "print(is_palindrome('Level'))"],
    ["def flatten(nested):", "    out = []", "    for item in nested:", "        if isinstance(item, list):", "            out.extend(flatten(item))", "        else:", "            out.append(item)", "    return out", "", "print(flatten([1, [2, [3, 4]], 5]))"],
    ["def bubble_sort(arr):", "    n = len(arr)", "    for i in range(n):", "        for j in range(n - i - 1):", "            if arr[j] > arr[j + 1]:", "                arr[j], arr[j + 1] = arr[j + 1], arr[j]", "    return arr", "", "print(bubble_sort([5, 2, 9, 1, 7]))"],
    ["def binary_search(items, target):", "    lo, hi = 0, len(items) - 1", "    while lo <= hi:", "        mid = (lo + hi) // 2", "        if items[mid] == target:", "            return mid", "        if items[mid] < target:", "            lo = mid + 1", "        else:", "            hi = mid - 1", "    return -1", "", "print(binary_search([1, 3, 5, 7, 9], 7))"],
    ["import json", "", "data = {'name': 'demo', 'tags': ['a', 'b']}", "text = json.dumps(data, indent=2)", "print(text)"],
    ["from datetime import datetime", "", "now = datetime.now()", "print(now.strftime('%Y-%m-%d %H:%M'))"],
    ["matrix = [[1, 2, 3], [4, 5, 6]]", "transposed = [list(row) for row in zip(*matrix)]", "print(transposed)"],
    ["x, y = 1, 2", "x, y = y, x", "print(x, y)"],
    ["nums = [3, 8, 1, 9, 4]", "evens = list(filter(lambda n: n % 2 == 0, nums))", "doubled = list(map(lambda n: n * 2, nums))", "print(evens, doubled)"],
    ["def average(values):", "    if not values:", "        return 0", "    return sum(values) / len(values)", "", "print(average([4, 8, 15, 16, 23, 42]))"],
    ["name = 'dunia'", "print(f'habari, {name}!')"],
    ["# TODO: clean this up later", "# notes: keep helpers small"],
    ["print('pokea salamu kutoka wachawi wa kwenu')"],
    ["from collections import defaultdict", "", "groups = defaultdict(list)", "for word in ['apple', 'avocado', 'banana', 'blueberry', 'cherry']:", "    groups[word[0]].append(word)", "print(dict(groups))"],
    ["def clamp(n, lo, hi):", "    return max(lo, min(n, hi))", "", "print(clamp(15, 0, 10))"],
    ["import os", "", "print(os.getcwd())", "print(os.listdir('.'))"],
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.info

pyautogui.FAILSAFE = True    # mouse into the TOP-LEFT corner = emergency stop
pyautogui.PAUSE = 0

# ───────────── SHARED STATE ─────────────
running = threading.Event()  # set = go, cleared = paused
stop    = threading.Event()  # set = shut everything down
running.set()

last_input = time.time()     # last time a HUMAN touched mouse/keyboard
action_started = 0.0


class State:
    """The intern's notebook: where we are, so we can resume after interruptions."""
    file = FILES[0]
    pending = []             # lines of the current snippet not yet finished
    typed = 0                # chars already typed on the in-progress line
    dirty = False            # True if the last action didn't finish cleanly
    lines = {f: 0 for f in FILES}
    limit = {f: random.randint(*MAX_LINES) for f in FILES}


S = State()


class Interrupted(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# ───────────── LISTENER THREADS ─────────────
def touch(*args):
    global last_input
    injected = args[-1] if args and isinstance(args[-1], bool) else False
    if not injected:                 # ignore the bot's own synthetic input
        last_input = time.time()


def toggle():
    if running.is_set():
        running.clear(); log("paused")
    else:
        running.set();   log("resumed")


def quit_now():
    log("quit requested")
    stop.set()


listeners = [
    keyboard.GlobalHotKeys({PAUSE_KEY: toggle, QUIT_KEY: quit_now}),
    keyboard.Listener(on_press=touch),
    mouse.Listener(on_move=touch, on_click=touch, on_scroll=touch),
]
for l in listeners:
    l.daemon = True
    l.start()


# ───────────── WINDOW HELPERS ─────────────
def active_title():
    try:
        w = gw.getActiveWindow()
        return w.title if w else ""
    except Exception as e:
        log(f"active_title error: {e}")
        return ""


def is_vsc_title(t):
    t = t.lower()
    return "visual studio code" in t or "code - oss" in t


def in_vsc():
    """Focused window must be VS Code AND our own folder."""
    t = active_title()
    return FOLDER_NAME.lower() in t.lower() and is_vsc_title(t)


def vsc_windows():
    try:
        return [w for w in gw.getWindowsWithTitle(FOLDER_NAME) if is_vsc_title(w.title)]
    except Exception:
        return []


def bring_back():
    """You wandered off (YouTube etc.)? Pull our VS Code window back to the front."""
    if in_vsc():
        return True
    wins = vsc_windows()
    if not wins:
        log("our VS Code window is gone, reopening the same folder")
        return open_vscode()
    w = wins[0]
    log("bringing VS Code back to the front")
    for _ in range(3):
        try:
            if w.isMinimized:
                w.restore()
            w.activate()
        except Exception:
            pass                      # activate() often "errors" but works
        time.sleep(0.7)
        if in_vsc():
            return True
        try:                          # fallback: minimize + restore forces it forward
            w.minimize(); time.sleep(0.3); w.restore()
        except Exception:
            pass
        time.sleep(0.7)
        if in_vsc():
            return True
    return False


# ───────────── THE GUARD ─────────────
def guard(need_vsc=True):
    """Call before every input. Blocks while paused, aborts if we should stop."""
    while not running.wait(0.2):
        if stop.is_set():
            raise Interrupted("quit")
    if stop.is_set():
        raise Interrupted("quit")
    if last_input > action_started:
        raise Interrupted("human")
    if need_vsc and not in_vsc():
        raise Interrupted("focus")


def hit(*keys):
    guard()
    pyautogui.hotkey(*keys)


def nap(seconds):
    time.sleep(seconds)
    guard()


# ───────────── TYPING ─────────────
def printer(text, speed=1.0, track=False, typos=True):
    """Human-ish typing. track=True keeps S.typed accurate for cleanup."""
    for ch in text:
        guard()
        if typos and ch.isalpha() and random.random() < TYPO_CHANCE:
            wrong = random.choice([c for c in "abcdefghijklmnopqrstuvwxyz" if c != ch.lower()])
            pyautogui.write(wrong)
            time.sleep(random.uniform(0.15, 0.45))
            pyautogui.press("backspace")        # one unit, no guard in between

        pyautogui.write(ch)
        if track:
            S.typed += 1

        delay = random.uniform(0.10, 0.28) * speed * TYPING_SPEED
        if ch in " .,():=[]{}'\"":
            delay += random.uniform(0.05, 0.25)
        r = random.random()
        if r < MICRO_STOP:                       # tiny hesitation
            delay += random.uniform(0.3, 0.9)
        elif r < MICRO_STOP + 0.01:              # "thinking"
            delay += random.uniform(1.2, 2.5)
        time.sleep(delay)


def type_pending():
    """Type the unfinished snippet line by line. Safe to resume after interruption."""
    while S.pending:
        line = S.pending[0]
        body = line.lstrip(" ")
        lead = len(line) - len(body)
        if lead and S.typed == 0:
            guard()
            pyautogui.write(" " * lead, interval=0.04)
            S.typed += lead
        printer(body, track=True)
        nap(random.uniform(0.2, 0.7))
        hit("enter")
        S.pending.pop(0)
        S.typed = 0
        S.lines[S.file] += 1
        if random.random() < 0.25:
            nap(random.uniform(0.8, 2.5))        # "reading what I just wrote"


# ───────────── FOCUS CONTROL: editor vs terminal ─────────────
def focus_editor():
    hit("escape")
    hit("ctrl", "1")                             # focus editor group 1
    nap(0.3)


def focus_terminal():
    hit("escape")
    hit("ctrl", "shift", "p")
    nap(0.5)
    printer("Terminal: Focus Terminal", speed=0.25, typos=False)
    nap(0.4)
    hit("enter")
    nap(0.7)


def switch_to(name):
    """Cycle editor tabs until the active tab is `name`."""
    for _ in range(len(FILES) + 1):
        if name.lower() in active_title().lower():
            return
        hit("ctrl", "pagedown")
        nap(0.4)
    raise Interrupted("tab")


# ───────────── ACTIONS ─────────────
def jiggle():
    """Move the mouse around INSIDE the VS Code window."""
    guard()
    sw, sh = pyautogui.size()
    left, top, right, bottom = 5, 5, sw - 5, sh - 5
    try:
        w = gw.getActiveWindow()
        if w:
            left, top = max(w.left + 20, 5), max(w.top + 20, 5)
            right, bottom = min(w.right - 20, sw - 5), min(w.bottom - 20, sh - 5)
    except Exception:
        pass
    right, bottom = max(right, left + 1), max(bottom, top + 1)
    tween = random.choice([pyautogui.easeInOutQuad, pyautogui.easeOutQuad, pyautogui.easeInOutCubic])
    pyautogui.moveTo(random.randint(left, right), random.randint(top, bottom),
                     random.uniform(0.3, 1.0), tween)


def wipe_file():
    """Too much code piled up: select all, look at it for a sec, delete, start afresh."""
    log(f"wiping {S.file}")
    nap(random.uniform(1.0, 2.5))
    hit("ctrl", "a")
    nap(random.uniform(0.5, 1.2))
    hit("delete")
    nap(0.3)
    hit("ctrl", "s")
    S.lines[S.file] = 0
    S.limit[S.file] = random.randint(*MAX_LINES)


def code_step():
    fresh = not S.pending                        # False = resuming an interrupted snippet
    if fresh:
        if random.random() < 0.3:
            S.file = random.choice(FILES)
        S.pending = list(random.choice(SNIPPETS))
    focus_editor()
    switch_to(S.file)
    hit("ctrl", "end")
    if fresh and S.lines[S.file] > 0:
        hit("enter")                             # blank line between snippets
    type_pending()
    hit("ctrl", "s")
    if S.lines[S.file] >= S.limit[S.file]:
        wipe_file()


def terminal_step():
    focus_editor()
    hit("ctrl", "s")
    focus_terminal()
    cmd = random.choice([f"{PY} {S.file}"] * 4 + [f"{PY} --version", "dir", "pip --version"])
    printer(cmd, speed=0.8)
    nap(random.uniform(0.3, 1.0))
    hit("enter")
    nap(random.uniform(2.0, 5.0))                # "reading the output"
    if random.random() < 0.4:
        printer(CLEAR, speed=0.8)
        hit("enter")
        nap(0.5)
    focus_editor()


def browse_step():
    """Read through the code: move around, scroll, flip tabs. No typing."""
    focus_editor()
    for _ in range(random.randint(2, 5)):
        hit(random.choice(["up", "down", "pageup", "pagedown"]))
        nap(random.uniform(0.4, 1.2))
    guard()
    pyautogui.scroll(random.choice([-3, -1, 1, 3]))
    if random.random() < 0.4:
        hit("ctrl", "pagedown")
        nap(random.uniform(0.5, 1.5))


def heal():
    """We were cut off mid-action. Clean up the mess, keep the notebook."""
    log("healing after interruption")
    hit("escape")                                # closes palette, clears half-typed terminal command
    nap(0.3)
    focus_editor()
    if S.typed:                                  # half-typed line: backspace exactly that many chars
        switch_to(S.file)
        hit("ctrl", "end")
        pyautogui.press("backspace", presses=S.typed, interval=0.02)
        S.typed = 0


def run_one_action():
    global action_started
    action_started = time.time()
    try:
        if not bring_back():
            log("couldn't bring VS Code back, will retry")
            return
        if S.dirty:
            heal()
            S.dirty = False
        S.dirty = True
        for _ in range(random.randint(1, 2)):
            jiggle()
            time.sleep(random.uniform(0.25, 1.0))
        roll = random.random()
        if S.pending or roll < 0.55:             # unfinished snippet always continues first
            code_step()
        elif roll < 0.75:
            terminal_step()
        elif roll < 0.92:
            browse_step()
        else:
            jiggle()
        S.dirty = False
    except Interrupted as e:
        log(f"action aborted: {e.reason}")        # S.dirty stays True, so heal() runs next time


# ───────────── STARTUP ─────────────
def prepare_folder():
    TARGET_FOLDER.mkdir(parents=True, exist_ok=True)
    seeds = {
        "main.py": "# classified\nprint('...ready')\n",
        "utils.py": "def helper():\n    return 42\n",
        "temp.py": "# scratch\n",
    }
    for name, text in seeds.items():
        p = TARGET_FOLDER / name
        if not p.exists():                       # don't clobber work on a relaunch
            p.write_text(text)
    vs = TARGET_FOLDER / ".vscode"
    vs.mkdir(exist_ok=True)
    (vs / "settings.json").write_text(json.dumps(EDITOR_SETTINGS, indent=2))


def open_vscode():
    code = shutil.which("code")
    if not code:
        log("can't find 'code' on PATH. In VS Code: Ctrl+Shift+P > 'Shell Command: Install code in PATH'")
        return False
    prepare_folder()
    log(f"launching VS Code in {TARGET_FOLDER}")
    subprocess.Popen([code, "--disable-workspace-trust", "--new-window",
                      str(TARGET_FOLDER), *[str(TARGET_FOLDER / f) for f in FILES]])

    deadline = time.time() + LAUNCH_TIMEOUT
    while time.time() < deadline and not stop.is_set():
        wins = vsc_windows()
        if wins:
            try:
                wins[0].activate()
            except Exception:
                pass
            time.sleep(4)
            if in_vsc():
                log("VS Code ready")
                return True
        time.sleep(1)
    log("VS Code didn't show up in time")
    return False


def main():
    global last_input
    log("=" * 44)
    log(f"{PAUSE_KEY} pause/resume | {QUIT_KEY} quit | mouse to top-left corner = failsafe")
    log("=" * 44)

    if not open_vscode():
        sys.exit(1)

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
            next_at = time.time() + random.uniform(*GAP_BETWEEN)
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