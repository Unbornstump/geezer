import random, time, threading,pyautogui
from pygetwindow as gw
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
