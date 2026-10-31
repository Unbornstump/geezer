import random, time, threading
import pyautogui
import pygetwindow as gw
from pynput import keyboard

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0

IDLE_AFTER = 15
running = threading.Event()
running.set()

def toggle():
    running.clear() if running.is_set() else running.set()
    print("RUNNING" if running.is_set() else "PAUSED")

keyboard.GlobalHotKeys({"<f8>": toggle}).start()

def in_vscode():
    return "Visual Studio Code" in (gw.getActiveWindowTitle() or "")

def human_move():
    w, h = pyautogui.size()
    x, y = pyautogui.position()
    tx = min(max(x + random.randint(-200, 200), 5), w - 5)
    ty = min(max(y + random.randint(-150, 150), 5), h - 5)
    tween = random.choice([pyautogui.easeInOutQuad,
                           pyautogui.easeOutQuad,
                           pyautogui.easeInOutCubic])
    pyautogui.moveTo(tx, ty, random.uniform(0.3, 1.2), tween)

def small_actions():
    r = random.random()
    if r < 0.25:
        pyautogui.scroll(random.choice([-2, -1, 1, 2]))
    elif r < 0.45:
        pyautogui.press(random.choice(["shift", "ctrl", "scrolllock"]))

def vscode_actions():
    r = random.random()
    if r < 0.4:
        pyautogui.scroll(random.choice([-5, -3, 3, 5]))
    elif r < 0.6:
        pyautogui.press(random.choice(["up", "down", "pageup", "pagedown"]))
    elif r < 0.8:
        pyautogui.hotkey("ctrl", "tab")
    else:
        pyautogui.hotkey("ctrl", "b")
        time.sleep(random.uniform(0.5, 1.5))
        pyautogui.hotkey("ctrl", "b")

last_pos, last_change = pyautogui.position(), time.time()

while True:
    time.sleep(random.uniform(1, 3))
    pos = pyautogui.position()
    if pos != last_pos:
        last_pos, last_change = pos, time.time()
        continue
    if not running.is_set() or time.time() - last_change < IDLE_AFTER:
        continue

    for _ in range(random.randint(1, 3)):
        human_move()
        time.sleep(random.uniform(0.2, 1.5))

    if in_vscode():
        vscode_actions()
    else:
        small_actions()

    last_pos = pyautogui.position()
    last_change = time.time() - IDLE_AFTER + random.randint(10, 45)