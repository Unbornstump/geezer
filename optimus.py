
import random, time, threading
import subprocess,os
import pyautogui
import pygetwindow as gw
from pynput import keyboard
from pathlib import Path

BASE_DIR = Path(r"C:\devtools")
FOLDER_NAME = "mine-" + time.strftime("%Y%m%d-%H%M%S")
TARGET_FOLDER = BASE_DIR / FOLDER_NAME
IDLE_AFTER = 9
PAUSE_KEY = "<f8>"


pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0

running = threading.Event()
running.set()

SNIPPETS = [
    "def process(data):\n return[X*2 for X in data]",
    "for i in range(10):\n print(i)",
    "import os, sys\n from pathlib import path",
    "result = sorted(data,key=lambda x: x[1])",
    "if __name__ == '__main__':\n main()",
    "try\n  value = int(input())\n except ValueError:\n  print('pueh pueh...')",
    "print('pokea salamu kutoka wachawi wa kwenu')\n",
    "x = [i**2 for i in range(20)] \nprint(x)\n",
]

def tog():
    running.clear() if running.is_set() else running.set()
    print('clearing the field' if running.is_set() else 'hot sun bitches')

keyboard.GlobalHotKeys({PAUSE_KEY : tog}).start()


def active_title():
    try:
        return gw.getWindowsWithTitle() or ""
    except Exception:
        return ""

def in_vsc():
    title = active_title().lower()
    return "visual studio code" in title or "code - oss" in title

def jiggle_dem_balls():
    w, h = pyautogui.size()
    x, y = pyautogui.position()
    tx = min(max(x+random.randint(-200, 200), 5), w -5)
    ty = min(max(y+random.randint(-150, 150), 5), h -5)
    tween = random.choice([pyautogui.easeInOutQuad,
                           pyautogui.easeOutQuad,
                           pyautogui.easeInOutCubic])
    pyautogui.moveTo(tx, ty, random.uniform(0.3, 1.0), tween)

def printer(text:str):
    for ch in text:
        if not running.is_set():
            return
        pyautogui.write(ch, interval=random.uniform(0.03, 0.12))

        
def bot_le_opener():
    print(f"forging: {TARGET_FOLDER} rn")
    TARGET_FOLDER.mkdir(parents=True, exist_ok=True)

    (TARGET_FOLDER / "main.py").write_text("#classified\n print('...ready')\n")
    (TARGET_FOLDER / "utils.py").write_text("def helper():\n  return 42\n")

    print('launching ...')
    subprocess.Popen(["code", str(TARGET_FOLDER)], shell=True)

    print('definition of patience...ngoja nduguu')
    for _ in range(30):
        time.sleep(1)
        if in_vsc():
            print('niice..ndo naingia')
            time.sleep(3)
            return True
    print('vsc failed to load in time..sawa')
    return False


def vscode_acts():
    if not in_vsc():
        return

    files = ["main.py", "utils.py", "temp.py"]
    chosen = random.choice(files)
    pyautogui.hotkey("ctrl", "p")
    time.sleep(0.4)
    printer(chosen)
    time.sleep(0.3)
    pyautogui.press("enter")
    time.sleep(0.5)

    for _ in range(random.randint(1, 3)):
        pyautogui.press(random.choice(["up", "down", "left", "right","end","home"]))
        time.sleep(random.uniform(0.1, 0.3))

    snippet = random.choice(SNIPPETS)
    printer(snippet)
    pyautogui.press("enter")
    time.sleep(random.uniform(0.2, 0.5))

    if random.random() < 0.3:
        pyautogui.hotkey("ctrl", "f5")
        time.sleep(random.uniform(1.8, 2.5))

        if random.random() < 0.5:
            pyautogui.hotkey("ctrl", "`")



def rada_safi():
    r = random.random()
    if r < 0.3:
        pyautogui.scroll(random.choice([-3, -1, 1, 3]))
    elif r < 0.5:
        pyautogui.press(random.choice(["shift", "ctrl", "alt"]))
    else:
        jiggle_dem_balls()



print('=' * 50)
print('beninging...')
print(f'target will be {TARGET_FOLDER}')
print(f'press {PAUSE_KEY} at your own risk')
print('=' * 50)

if not bot_le_opener():
    print('quit the job..im tired man')
    exit(1)

print('ready to go...')
last_pos, last_change = pyautogui.position(), time.time()

while True:
    time.sleep(random.uniform(1.0,2.5))

    if not running.is_set():
        continue

    pos= pyautogui.position()
    if pos != last_pos:
        last_pos, last_change = pos, time.time()
        continue

    if time.time() - last_change < IDLE_AFTER:
        continue

    for _ in range(random.randint(1,3)):
        jiggle_dem_balls()
        time.sleep(random.uniform(0.25,1.0))

    if in_vsc():
        vscode_acts()
    else:
        rada_safi()


    
    last_pos, last_change = pyautogui.position(), time.time() - IDLE_AFTER + random.randint(6,35)


print('forged steel')

