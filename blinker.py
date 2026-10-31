import random, time, threading,pyautogui
import pygetwindow as gw
from pynput import keyboard

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0

#seems threading will do
#make sure you check on how threads interact

IDLE_AFTER = 10
running = threading.Event()
running.set()

def tog():
    running.clear() if running.is_set() else running.set()
    print('checking thru the trash' if running.is_set() else 'trash smells')

keyboard.GlobalHotKeys({"<f8>": tog}).start()

def in_editor():
    return "Visual Studio Code" in (gw.getActiveWindowTitle() or "")

def jiggle_dem_balls():
    w, h = pyautogui.size()
    x, y = pyautogui.position()
    tx = min(max(x + random.randint(-200, 200), 5), w - 5)
    ty = min(max(y + random.randint(-150, 150), 5), h - 5)
    tween = random.choice([pyautogui.easeInOutQuad,
                           pyautogui.easeOutQuad,
                           pyautogui.easeInOutCubic])
    pyautogui.moveTo(tx, ty, random.uniform(0.3, 1.2), tween)

def tiny_actions():
    r = random.random()
    if r < 0.25:
        pyautogui.scroll(random.choice([-2, -1, 1, 2]))
    elif r < 0.45:
        pyautogui.press(random.choice(["shift", "ctrl", "scrolllock"]))


def editor_actions():
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
        jiggle_dem_balls()
        time.sleep(random.uniform(0.25, 1.2))

    if in_editor():
        editor_actions()
    else:
        tiny_actions()

    last_pos, last_change = pyautogui.position(), time.time()
