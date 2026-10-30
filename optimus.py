from operator import sub
import random, time, threading
import subprocess,os
import pyautogui
import pygetwindow as gw
from pynput import keyboard
from pathlib import Path

BASE_DIR = Path(r"C:\devtools")
FOLDER_NAME = "activity-sim-" + time.strftime("%Y%m%d-%H%M%S")
TARGET_FOLDER = BASE_DIR / FOLDER_NAME
IDLE_AFTER = 10

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
    print('clearing the field' if running.is_set() else 'broken panga')

keyboard.GlobalHotKeys({PAUSE_KEY : tog}).start()


def active_tile():
    try:
        return gw.getWindowsWithTitle() or ""
    except Exception:
        return ""

def in_vsc():
    title = active_tile().lower()
    return "vscode" in title or "code" in title

def jiggle_dem_balls():
    w, h = pyautogui.size()
    x, y = pyautogui.position()
    tx = min(max(x+random.randint(-200, 200), 5), w -5)
    ty = min(max(y+random.randint(-150, 150), 5), h -5)
    tween = random.choice([pyautogui.easeInOutQuad,
                           pyautogui.easeOutQuad,
                           pyautogui.easeInOutCubic])
    pyautogui.moveTo(tx, ty, random.uniform(0.3, 1.2), tween)

def printer(text:str):
    for ch in text:
        if not running.is_set():
            return
        pyautogui.write(ch, interval=random.uniform(0.02, 0.08))

        
def bot_le_opener():
    print(f"forging: {TARGET_FOLDER} rn")
    TARGET_FOLDER.mkdir(parents=True, exist_ok=True)
    (TARGET_FOLDER / "main.py").write_text("#classified\n print('...ready')\n")
    (TARGET_FOLDER / "utils.py").write_text("def helper():\n  return 42\n")

    print('launching code')
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


    

    #py_files = list(TARGET_FOLDER.rglob("*.py"))
    #if not py_files:
        #pyautogui.hotkey("ctrl", "n")
        #time.sleep(0.7)
        #return

    #chosen = random.choice(py_files)
    #rel = chosen.relative_to(TARGET_FOLDER)
    #pyautogui.hotkey("ctrl", "p")
    #time.sleep(0.4)
    #printer(str(rel).replace("\\", "/"))
    #time.sleep(0.3)
    #pyautogui.press("enter")
    #time.sleep(0.8)


def vsc_edits():
    if not in_vsc():
        return
    if "untitled" not in active_tile() and TARGET.name not in active_tile():
        safe_open_py_file()

    for _ in range(random.randint(2,5)):
        pyautogui.press(random.choice(["up", "down", "left", "right"]))
        time.sleep(random.uniform(0.1, 0.35))

    snippet = random.choice(SNIPPETS)
    printer(snippet)
    pyautogui.press("escape")
    time.sleep(random.uniform(0.4, 0.9))

    if random.random() < 0.55:
        for _ in range(random.randint(8,35)):
            pyautogui.press("backspace")
            time.sleep(random.uniform(0.025, 0.07))



    if random.random() < 0.25:
        pyautogui.hotkey("ctrl", "f5")
        time.sleep(random.uniform(1.5, 3.5))

        if random.random() < 0.6:
            pyautogui.hotkey("ctrl", "'")

    if random.random() < 0.12 and "untitled" in active_tile():
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.2)
        pyautogui.press("delete")





def rada_safi():
    r = random.random()
    if r < 0.3:
        pyautogui.scroll(random.choice([-3, -1, 1, 3]))
    elif r < 0.5:
        pyautogui.press(random.choice(["shift", "ctrl", "alt"]))
    else:
        jiggle_dem_balls()


last_pos, last_change = pyautogui.position(), time.time()

while True:
    time.sleep(random.uniform(1.2,2.8))

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
        time.sleep(random.uniform(0.25,1.2))

    if in_vsc():
        vsc_edits()
    else:
        rada_safi()


    
    last_pos, last_change = pyautogui.position(), time.time() - IDLE_AFTER + random.randint(8,35)



    
