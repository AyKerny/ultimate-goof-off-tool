# -*- coding: utf-8 -*-
"""
摸鱼工具 (BossKey)
--------------------------------------------------
按下设定的键盘组合 / 鼠标侧键：
  - 切换虚拟桌面
  - 最小化全部窗口（可原样恢复）      <- 推荐
  - 彻底隐藏全部窗口（可恢复）
  - 回到桌面 (Win+D)
  - 温和关闭全部窗口（发 WM_CLOSE，程序会提示保存）
可选：伪装一个看起来在干活的 Excel 窗口。

作者：WorkBuddy  依赖：pynput / pystray / pillow
"""
import ctypes
import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import ttk

APP_NAME = "摸鱼工具"
APP_ID = "MoyuBossKey"
CONFIG_NAME = "config.json"

# ---------------------------------------------------------------- 基础路径

def app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def config_path():
    """优先放在程序目录（便携），不可写时退回 %APPDATA%"""
    p = os.path.join(app_dir(), CONFIG_NAME)
    try:
        with open(p, "a", encoding="utf-8"):
            pass
        return p
    except Exception:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_ID)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, CONFIG_NAME)


DEFAULT_CONFIG = {
    "hotkey": "ctrl+alt+q",
    "mouse": ["x1"],           # middle / x1 / x2
    "action": "minimize",      # switch / minimize / hide / desktop / close
    "restore_on_second": True,
    "switch_desktop_after": False,
    "camouflage": False,
    "notify": False,
    "start_minimized": False,
    "auto_start": False,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- Win32

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7
SW_RESTORE = 9
WM_CLOSE = 0x0010
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
VK_LWIN = 0x5B
VK_CONTROL = 0x11
VK_D = 0x44
VK_LEFT = 0x25
VK_RIGHT = 0x27
KEYEVENTF_KEYUP = 0x0002

user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT


def vk_to_char(vk):
    """虚拟键码 -> 不受修饰键影响的字符。Ctrl 按下时 pynput 只给 vk，没有 char。"""
    if vk is None:
        return None
    if 0x41 <= vk <= 0x5A:          # A-Z
        return chr(vk).lower()
    if 0x30 <= vk <= 0x39:          # 0-9
        return chr(vk)
    try:
        ch = user32.MapVirtualKeyW(vk, 2) & 0xFFFF   # MAPVK_VK_TO_CHAR
        if 32 < ch < 0xFFFD:
            c = chr(ch)
            return c.lower() if c.isalnum() else c
    except Exception:
        pass
    return None


SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "DV2ControlHost",
    "NotifyIconOverflowWindow", "ForegroundStaging", "MultitaskingViewFrame",
    "XamlExplorerHostIslandWindow", "Windows.UI.Core.CoreWindow",
    "ApplicationManager_DesktopShellWindow", "TaskListThumbnailWnd", "TopLevelWindowForOverflowXamlIsland",
    "tooltips_class32", "#32768", "MSCTFIME UI", "IME", "GDI+ Hook Window Class",
    "Chrome_SystemMessageWindow", "SysShadow", "Base_PowerMessageWindow",
}


def _wnd_text(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _wnd_class(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def collect_windows(exclude_pid=None):
    """收集当前桌面上可见的顶层窗口；默认排除本进程自己的窗口"""
    own = os.getpid() if exclude_pid is None else exclude_pid
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if _wnd_class(hwnd) in SHELL_CLASSES:
                return True
            ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex & WS_EX_TOOLWINDOW:
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == own:
                return True
            r = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            if (r.right - r.left) < 60 or (r.bottom - r.top) < 40:
                return True
            found.append({
                "hwnd": int(hwnd),
                "title": _wnd_text(hwnd),
                "cls": _wnd_class(hwnd),
                "iconic": bool(user32.IsIconic(hwnd)),
            })
        except Exception:
            pass
        return True

    user32.EnumWindows(_cb, 0)
    return found


def foreground():
    try:
        return int(user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------- 按键模拟

def _key(vk, up=False):
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP if up else 0, 0)


def _tap(vk):
    _key(vk)
    time.sleep(0.012)
    _key(vk, True)


def show_desktop():
    """Win+D"""
    _key(VK_LWIN)
    time.sleep(0.01)
    _tap(VK_D)
    time.sleep(0.01)
    _key(VK_LWIN, True)


def switch_desktop(step=1):
    """Ctrl+Win+Left/Right，切换虚拟桌面"""
    _key(VK_LWIN)
    _key(VK_CONTROL)
    time.sleep(0.012)
    _tap(VK_RIGHT if step > 0 else VK_LEFT)
    time.sleep(0.012)
    _key(VK_CONTROL, True)
    _key(VK_LWIN, True)


# ---------------------------------------------------------------- 摸鱼引擎

class Boss:
    def __init__(self, cfg, ui_queue):
        self.cfg = cfg
        self.ui = ui_queue
        self.hidden = []          # [(hwnd, was_iconic)]
        self.mode_used = None
        self.desktop_step = 0
        self.active = False
        self.prev_fg = 0
        self.fake = None
        self._lock = threading.Lock()

    # ---------- 动作
    def fire(self):
        if not self._lock.acquire(blocking=False):
            return
        try:
            action = self.cfg.get("action", "minimize")
            if action == "close":
                self.close_all()
            elif self.active and self.cfg.get("restore_on_second", True):
                self.restore()
            elif action == "switch":
                self.do_switch()
            elif action == "desktop":
                self.do_desktop()
            elif action == "hide":
                self.do_hide(minimize=False)
            else:
                self.do_hide(minimize=True)
        except Exception as exc:
            self.log("动作失败: %r" % (exc,))
        finally:
            self._lock.release()

    def do_switch(self):
        self.prev_fg = foreground()
        n = 1
        switch_desktop(1)
        self.desktop_step = n
        self.mode_used = "switch"
        self.active = True
        self.finish()

    def do_desktop(self):
        self.prev_fg = foreground()
        show_desktop()
        self.mode_used = "desktop"
        self.active = True
        self.finish()

    def do_hide(self, minimize=True):
        self.prev_fg = foreground()
        wins = [w for w in collect_windows()]
        done = []
        for w in wins:
            hwnd = w["hwnd"]
            if not user32.IsWindow(hwnd):
                continue
            if minimize:
                if w["iconic"]:
                    continue
                user32.ShowWindow(hwnd, SW_MINIMIZE)
            else:
                user32.ShowWindow(hwnd, SW_HIDE)
            done.append((hwnd, w["iconic"]))
        self.hidden = done
        self.mode_used = "minimize" if minimize else "hide"
        self.active = True
        if self.cfg.get("switch_desktop_after"):
            switch_desktop(1)
            self.desktop_step = 1
        if self.cfg.get("camouflage"):
            self.ui.put(("fake_window", True))
        self.finish(count=len(done))

    def close_all(self):
        n = 0
        for w in collect_windows():
            if not w["title"]:
                continue
            try:
                user32.PostMessageW(w["hwnd"], WM_CLOSE, 0, 0)
                n += 1
            except Exception:
                pass
        self.mode_used = "close"
        self.finish(count=n, toast="已温和关闭 %d 个窗口" % n)

    def restore(self):
        # 1) 先回到原来的桌面，窗口才会恢复在原来的桌面上
        if self.desktop_step:
            for _ in range(abs(self.desktop_step)):
                switch_desktop(-1 if self.desktop_step > 0 else 1)
            self.desktop_step = 0
        if self.mode_used == "desktop":
            show_desktop()
        if self.hidden:
            for hwnd, was_iconic in self.hidden:
                if not user32.IsWindow(hwnd):
                    continue
                user32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE if was_iconic else SW_RESTORE)
            self.hidden = []
            if self.prev_fg and user32.IsWindow(self.prev_fg):
                try:
                    user32.SetForegroundWindow(self.prev_fg)
                except Exception:
                    pass
        if self.cfg.get("camouflage"):
            self.ui.put(("fake_window", False))
        self.mode_used = None
        self.active = False
        self.finish(toast="已恢复桌面")

    def toggle_from_tray(self):
        self.fire()

    # ---------- 辅助
    def finish(self, count=0, toast=None):
        if toast is None:
            if self.active:
                toast = {
                    "switch": "已切到另一个桌面 · 再按一次切回",
                    "desktop": "已回到桌面 · 再按一次切回",
                    "minimize": "已最小化 %d 个窗口 · 再按一次恢复" % count,
                    "hide": "已隐藏 %d 个窗口 · 再按一次恢复" % count,
                }.get(self.mode_used, "已执行")
            else:
                toast = ""
        if toast:
            self.ui.put(("toast", toast))

    @staticmethod
    def log(msg):
        try:
            with open(os.path.join(app_dir(), "bosskey.log"), "a", encoding="utf-8") as f:
                f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass


# ---------------------------------------------------------------- 热键解析

MOD_KEYS = {"ctrl", "alt", "shift", "win"}
MOD_WORDS = {"ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
             "win": "win", "cmd": "win", "super": "win", "windows": "win"}
KEY_ALIAS = {
    "escape": "esc", "esc": "esc", "return": "enter", "enter": "enter",
    "space": "space", "tab": "tab", "backspace": "backspace", "delete": "delete",
    "insert": "insert", "home": "home", "end": "end", "prior": "pageup",
    "page_up": "pageup", "pageup": "pageup", "next": "pagedown",
    "page_down": "pagedown", "pagedown": "pagedown", "up": "up", "down": "down",
    "left": "left", "right": "right", "print": "printscreen",
    "scroll_lock": "scrolllock", "caps_lock": "capslock", "pause": "pause",
}


def norm_name(name):
    name = (name or "").strip().lower()
    return KEY_ALIAS.get(name, name)


# tkinter 的 keysym 与真实字符不同名，录制时需要翻译
TK_KEYSYM = {
    "minus": "-", "equal": "=", "bracketleft": "[", "bracketright": "]",
    "semicolon": ";", "apostrophe": "'", "comma": ",", "period": ".",
    "slash": "/", "backslash": "\\", "grave": "`", "quotedbl": "'",
}


def keysym_to_name(ks):
    low = ks.lower()
    if low in TK_KEYSYM:
        return TK_KEYSYM[low]
    if len(ks) == 1:
        return ks.lower()
    return norm_name(low)


def parse_hotkey(text):
    """'ctrl+alt+Q' -> ({'ctrl','alt'}, 'q')；无主键返回 (set(), None)"""
    if not text:
        return set(), None
    mods, main = set(), None
    for part in str(text).split("+"):
        part = part.strip().lower()
        if not part:
            continue
        if part in MOD_WORDS:
            mods.add(MOD_WORDS[part])
        else:
            main = norm_name(part)
    if mods and main is None:
        return set(), None
    return mods, main


def fmt_hotkey(mods, key):
    order = [m for m in ("ctrl", "alt", "shift", "win") if m in mods]
    shown = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win"}
    parts = [shown[m] for m in order]
    if key:
        parts.append(key.upper() if len(key) == 1 else key.capitalize())
    return "+".join(parts)


BUTTON_NAMES = {"middle": "鼠标中键", "x1": "侧键 X1（后退）", "x2": "侧键 X2（前进）",
                "b8": "拇指键 4", "b9": "拇指键 5"}


# ---------------------------------------------------------------- 全局监听

class HotkeyService:
    def __init__(self, boss, cfg):
        self.boss = boss
        self.cfg = cfg
        self.kb = None
        self.ms = None
        self.pressed_mods = set()
        self.pressed_keys = set()
        self.last_fire = 0.0
        self.recording = False
        self.enabled = False

    def start(self):
        from pynput import keyboard, mouse
        self._kb_mod = keyboard
        self.kb = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self.kb.daemon = True
        self.kb.start()
        self.ms = mouse.Listener(on_click=self._on_click)
        self.ms.daemon = True
        self.ms.start()
        self.enabled = True

    def stop(self):
        for lst in (self.kb, self.ms):
            try:
                if lst:
                    lst.stop()
            except Exception:
                pass
        self.enabled = False

    # --- 键盘
    def _kname(self, key):
        from pynput.keyboard import KeyCode
        if isinstance(key, KeyCode):
            c = vk_to_char(key.vk)
            if c:
                return c, False
            if key.char:
                return key.char.lower(), False
            if key.vk:
                return "vk%d" % key.vk, False
            return None, False
        try:
            name = key.name
        except Exception:
            return None, False
        if name in ("ctrl", "ctrl_l", "ctrl_r"):
            return "ctrl", True
        if name in ("alt", "alt_l", "alt_r", "alt_gr"):
            return "alt", True
        if name in ("shift", "shift_l", "shift_r"):
            return "shift", True
        if name in ("cmd", "cmd_l", "cmd_r"):
            return "win", True
        return norm_name(name), False

    def _on_press(self, key):
        name, is_mod = self._kname(key)
        if name is None:
            return
        if is_mod:
            self.pressed_mods.add(name)
            return
        if name in self.pressed_keys:
            return
        self.pressed_keys.add(name)
        if self.recording or not self.enabled:
            return
        mods, main = parse_hotkey(self.cfg.get("hotkey"))
        if not main or main != name:
            return
        if mods and not mods.issubset(self.pressed_mods):
            return
        now = time.time()
        if now - self.last_fire < 0.45:
            return
        self.last_fire = now
        threading.Thread(target=self.boss.fire, daemon=True).start()

    def _on_release(self, key):
        name, is_mod = self._kname(key)
        if name is None:
            return
        if is_mod:
            self.pressed_mods.discard(name)
        else:
            self.pressed_keys.discard(name)

    # --- 鼠标
    def _on_click(self, x, y, button, pressed):
        if not pressed or self.recording or not self.enabled:
            return
        try:
            bname = button.name  # left / right / middle / x1 / x2
        except Exception:
            return
        if bname not in (self.cfg.get("mouse") or []):
            return
        now = time.time()
        if now - self.last_fire < 0.45:
            return
        self.last_fire = now
        threading.Thread(target=self.boss.fire, daemon=True).start()


# ---------------------------------------------------------------- 伪装窗口

FAKE_ROWS = [
    ("华东大区", [1862.4, 2013.7, 2190.2, 6066.3, 12.4]),
    ("华南大区", [1340.8, 1288.5, 1452.1, 4081.4, 6.8]),
    ("华北大区", [1105.2, 1176.9, 1098.4, 3380.5, -2.1]),
    ("华中大区", [962.7, 1011.3, 1087.6, 3061.6, 9.2]),
    ("西南大区", [714.5, 702.8, 758.3, 2175.6, 4.4]),
    ("海外事业部", [438.1, 512.6, 604.9, 1555.6, 38.7]),
    ("线上渠道", [2245.9, 2410.2, 2593.8, 7249.9, 18.9]),
]
FAKE_HEAD = ["部门 / 渠道", "7月", "8月", "9月", "合计", "同比%"]


class FakeWorkWindow:
    """看起来像 Excel 的伪装窗口"""

    def __init__(self, root):
        self.top = tk.Toplevel(root)
        self.top.title("2026Q3经营分析.xlsx - Excel")
        self.top.geometry("%dx%d+0+0" % (self.top.winfo_screenwidth(), self.top.winfo_screenheight()))
        self.top.attributes("-topmost", False)
        self.top.configure(bg="#ffffff")
        try:
            self.top.state("zoomed")
        except Exception:
            pass
        self._build()
        # 保证盖在最前面：短暂置顶 + 抢焦点，之后放开
        try:
            self.top.deiconify()
            self.top.lift()
            self.top.attributes("-topmost", True)
            self.top.after(800, self._drop_topmost)
            self.top.focus_force()
        except Exception:
            pass

    def _drop_topmost(self):
        try:
            if self.top.winfo_exists():
                self.top.attributes("-topmost", False)
        except Exception:
            pass

    def _build(self):
        # ---- 标题条
        head = tk.Frame(self.top, bg="#217346", height=36)
        head.pack(fill="x")
        head.pack_propagate(False)
        tk.Label(head, text="  2026Q3经营分析.xlsx  -  Excel", bg="#217346", fg="#ffffff",
                 font=("Microsoft YaHei UI", 10)).pack(side="left")
        tk.Label(head, text="已保存    ", bg="#217346", fg="#cfe8d9",
                 font=("Microsoft YaHei UI", 9)).pack(side="right")

        # ---- 功能区
        bar = tk.Frame(self.top, bg="#f3f3f3", height=30)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        for i, t in enumerate(["开始", "插入", "页面布局", "公式", "数据", "审阅", "视图", "开发工具"]):
            tk.Label(bar, text=t, bg="#f3f3f3", fg="#217346" if i == 0 else "#3a3a3a",
                     font=("Microsoft YaHei UI", 9, "bold" if i == 0 else "normal")).pack(side="left", padx=13)
        tk.Frame(self.top, bg="#d6d6d6", height=1).pack(fill="x")

        # ---- 编辑栏
        fbar = tk.Frame(self.top, bg="#fafafa", height=28)
        fbar.pack(fill="x")
        fbar.pack_propagate(False)
        tk.Label(fbar, text=" B2 ", bg="#ffffff", fg="#222", relief="solid", bd=1,
                 font=("Consolas", 9)).pack(side="left", padx=(6, 10))
        tk.Label(fbar, text=" fx ", bg="#fafafa", fg="#666", font=("Consolas", 10)).pack(side="left")
        tk.Label(fbar, text="=SUM(B2:D2)", bg="#fafafa", fg="#1f1f1f",
                 font=("Consolas", 9)).pack(side="left")
        tk.Frame(self.top, bg="#d6d6d6", height=1).pack(fill="x")

        # ---- 底部工作表标签
        tab = tk.Frame(self.top, bg="#f3f3f3", height=28)
        tab.pack(fill="x", side="bottom")
        tab.pack_propagate(False)
        tk.Label(tab, text="  汇总  ", bg="#ffffff", fg="#217346",
                 font=("Microsoft YaHei UI", 9, "bold")).pack(side="left", padx=(8, 0))
        for name in ("明细", "预算对比", "部门口径"):
            tk.Label(tab, text="  %s  " % name, bg="#f3f3f3", fg="#555",
                     font=("Microsoft YaHei UI", 9)).pack(side="left")

        # ---- 表格区（真实尺寸要等窗口铺开后才知道，所以延迟绘制）
        self.canvas = tk.Canvas(self.top, bg="#ffffff", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.top.after(80, self._draw)

    def _draw(self):
        c = self.canvas
        try:
            if not c.winfo_exists():
                return
            c.delete("all")
        except Exception:
            return
        W = max(c.winfo_width(), 800)
        H = max(c.winfo_height(), 400)
        LEFT, TOPX, ROWH = 48, 22, 26
        widths = [220, 112, 112, 112, 120, 86]
        while LEFT + sum(widths) < W + 96:
            widths.append(96)

        def col_letter(i):
            s = ""
            i += 1
            while i:
                i, r = divmod(i - 1, 26)
                s = chr(65 + r) + s
            return s

        xs, x = [], LEFT
        for w in widths:
            x += w
            xs.append(x)
        grid_right = min(x, W)
        ys, y = [], TOPX
        while y < H:
            y += ROWH
            ys.append(y)

        # 空白网格线
        for xv in xs:
            c.create_line(xv, 0, xv, H, fill="#e0e0e0")
        for yv in ys:
            c.create_line(LEFT, yv, grid_right, yv, fill="#eaeaea")

        # 列标 & 行号
        px = LEFT
        for i, w in enumerate(widths):
            c.create_rectangle(px, 0, px + w, TOPX, fill="#f1f1f1", outline="#dcdcdc")
            c.create_text(px + w / 2, TOPX / 2, text=col_letter(i), fill="#555", font=("Consolas", 9))
            px += w
        for i in range(len(ys)):
            yy = TOPX + i * ROWH
            c.create_rectangle(0, yy, LEFT, yy + ROWH, fill="#f1f1f1", outline="#dcdcdc")
            c.create_text(LEFT / 2, yy + ROWH / 2, text=str(i + 1), fill="#555", font=("Consolas", 9))

        # 表头行（第 1 行）
        px = LEFT
        for i, w in enumerate(widths[:6]):
            c.create_rectangle(px, TOPX, px + w, TOPX + ROWH, fill="#e8f1ec", outline="#d8d8d8")
            c.create_text(px + w / 2, TOPX + ROWH / 2, text=FAKE_HEAD[i], fill="#1c4b33",
                          font=("Microsoft YaHei UI", 9, "bold"))
            px += w

        # 数据行
        for r, (name, vals) in enumerate(FAKE_ROWS):
            yy = TOPX + (r + 1) * ROWH
            px = LEFT
            for i, w in enumerate(widths[:6]):
                c.create_rectangle(px, yy, px + w, yy + ROWH, fill="#ffffff", outline="#e0e0e0")
                if i == 0:
                    c.create_text(px + 8, yy + ROWH / 2, text=name, anchor="w", fill="#222",
                                  font=("Microsoft YaHei UI", 9))
                else:
                    v = vals[i - 1]
                    txt = ("%.1f%%" % v) if i == 5 else ("{:,.1f}".format(v))
                    col = "#c00000" if (i == 5 and v < 0) else "#222"
                    c.create_text(px + w - 8, yy + ROWH / 2, text=txt, anchor="e", fill=col,
                                  font=("Microsoft YaHei UI", 9))
                px += w

        # 合计行
        yy = TOPX + 8 * ROWH
        px = LEFT
        total = [sum(r[1][i] for r in FAKE_ROWS) for i in range(4)]
        for i, w in enumerate(widths[:6]):
            c.create_rectangle(px, yy, px + w, yy + ROWH, fill="#e8f1ec", outline="#d0d0d0")
            if i == 0:
                c.create_text(px + 8, yy + ROWH / 2, text="合计", anchor="w", fill="#1c4b33",
                              font=("Microsoft YaHei UI", 9, "bold"))
            elif i == 5:
                c.create_text(px + w - 8, yy + ROWH / 2, text="11.8%", anchor="e", fill="#1c4b33",
                              font=("Microsoft YaHei UI", 9, "bold"))
            else:
                c.create_text(px + w - 8, yy + ROWH / 2, text="{:,.1f}".format(total[i - 1]), anchor="e",
                              fill="#1c4b33", font=("Microsoft YaHei UI", 9, "bold"))
            px += w

        # 选中的单元格（看着更像正在干活）
        c.create_rectangle(LEFT + widths[0], TOPX + ROWH, LEFT + widths[0] + widths[1], TOPX + 2 * ROWH,
                           outline="#217346", width=2)

    def destroy(self):
        try:
            self.top.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- 自启

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _launch_command():
    if getattr(sys, "frozen", False):
        return '"%s" --tray' % sys.executable
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pyw):
        exe = pyw
    return '"%s" "%s" --tray' % (exe, os.path.abspath(__file__))


def get_autostart():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, APP_ID)
            return bool(v)
    except Exception:
        return False


def set_autostart(on):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
            if on:
                winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_ID)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 托盘

def make_icon_image():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([3, 3, 60, 60], radius=15, fill=(47, 111, 237, 255))
    font = None
    for p in (r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
        if os.path.exists(p):
            try:
                font = ImageFont.truetype(p, 38)
                break
            except Exception:
                font = None
    if font is not None:
        d.text((32, 33), "摸", font=font, fill=(255, 255, 255, 255), anchor="mm")
    else:
        d.ellipse([16, 16, 48, 48], fill=(255, 255, 255, 255))
    return img


class Tray:
    def __init__(self, app):
        self.app = app
        self.icon = None

    def start(self):
        import pystray

        def item(text, fn, **kw):
            return pystray.MenuItem(text, lambda *_: self.app.post(fn), **kw)

        menu = pystray.Menu(
            pystray.MenuItem("打开设置", lambda *_: self.app.post(self.app.show_window), default=True),
            item("摸鱼 / 恢复", self.app.tray_fire),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启", lambda *_: self.app.post(self.app.toggle_autostart),
                             checked=lambda _i: get_autostart()),
            pystray.Menu.SEPARATOR,
            item("退出", self.app.quit),
        )
        self.icon = pystray.Icon(APP_ID, make_icon_image(), APP_NAME, menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def notify(self, msg):
        try:
            if self.icon:
                self.icon.notify(msg, APP_NAME)
        except Exception:
            pass

    def stop(self):
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass


# ---------------------------------------------------------------- 界面

C_BG = "#f4f5f7"
C_CARD = "#ffffff"
C_BORDER = "#e2e5ea"
C_TEXT = "#1f2329"
C_SUB = "#8a919c"
C_ACCENT = "#2f6fed"
C_OK = "#12b76a"
F_FONT = ("Microsoft YaHei UI", 9)
F_SMALL = ("Microsoft YaHei UI", 8)
F_H2 = ("Microsoft YaHei UI", 10, "bold")
F_TITLE = ("Microsoft YaHei UI", 15, "bold")

ACTION_ITEMS = [
    ("switch", "切换虚拟桌面", "整套桌面直接切走，最彻底；再按一次切回"),
    ("minimize", "最小化全部窗口", "逐个记录后最小化，再按一次原样恢复（推荐）"),
    ("hide", "彻底隐藏全部窗口", "窗口从任务栏消失；个别程序恢复后可能重绘异常"),
    ("desktop", "回到桌面（Win+D）", "系统原生显示桌面，再按一次回来"),
    ("close", "温和关闭全部窗口", "发关闭信号，程序会弹保存提示；不可恢复"),
]


class App:
    def __init__(self, start_hidden=False):
        self.cfg = load_config()
        self.ui = queue.Queue()
        self.boss = Boss(self.cfg, self.ui)
        self.svc = HotkeyService(self.boss, self.cfg)
        self.tray = Tray(self)
        self.recording = False
        self.rec_mods = set()
        self.rec_key = None
        self.status_job = None

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.configure(bg=C_BG)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._center(566, 690)
        self._styles()
        self._build()
        self._sync_widgets()

        self.svc.start()
        self.tray.start()
        self.root.after(120, self._poll)
        if not start_hidden and not self.cfg.get("start_minimized"):
            self.show_window()

    # ---------- 基础设施
    def _center(self, w, h):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry("%dx%d+%d+%d" % (w, h, max(0, (sw - w) // 2), max(0, (sh - h) // 3)))

    def _styles(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure("TFrame", background=C_BG)
        st.configure("Card.TFrame", background=C_CARD)
        st.configure("TRadiobutton", background=C_CARD, foreground=C_TEXT, font=F_FONT,
                     focuscolor=C_CARD, anchor="w", padding=(2, 3))
        st.map("TRadiobutton", background=[("active", C_CARD)])
        st.configure("TCheckbutton", background=C_CARD, foreground=C_TEXT, font=F_FONT,
                     focuscolor=C_CARD, padding=(2, 2))
        st.map("TCheckbutton", background=[("active", C_CARD)])
        st.configure("Accent.TButton", background=C_ACCENT, foreground="white",
                     borderwidth=0, focusthickness=0, font=("Microsoft YaHei UI", 9, "bold"), padding=(14, 6))
        st.map("Accent.TButton", background=[("active", "#2560d4"), ("disabled", "#a9c0f2")])
        st.configure("Ghost.TButton", background=C_CARD, foreground=C_TEXT, borderwidth=1,
                     font=F_FONT, padding=(12, 5))
        st.map("Ghost.TButton", background=[("active", "#f0f2f5")])

    def post(self, fn, *a):
        self.ui.put(("call", fn, a))

    def _poll(self):
        try:
            while True:
                msg = self.ui.get_nowait()
                kind = msg[0]
                if kind == "toast":
                    self.set_status(msg[1], C_OK)
                    if self.cfg.get("notify"):
                        self.tray.notify(msg[1])
                elif kind == "fake_window":
                    self._toggle_fake(msg[1])
                elif kind == "call":
                    try:
                        msg[1](*msg[2])
                    except Exception as exc:
                        Boss.log("ui call error %r" % (exc,))
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _toggle_fake(self, show):
        if show:
            if self.boss.fake is None:
                self.boss.fake = FakeWorkWindow(self.root)
        else:
            if self.boss.fake is not None:
                self.boss.fake.destroy()
                self.boss.fake = None

    # ---------- 界面搭建
    def _card(self, parent, pad=14):
        outer = tk.Frame(parent, bg=C_CARD, highlightbackground=C_BORDER, highlightthickness=1)
        outer.pack(fill="x", padx=18, pady=(0, 12))
        inner = tk.Frame(outer, bg=C_CARD)
        inner.pack(fill="x", padx=pad, pady=pad)
        return inner

    def _card_title(self, parent, text, hint=""):
        row = tk.Frame(parent, bg=C_CARD)
        row.pack(fill="x", pady=(0, 8))
        tk.Frame(row, bg=C_ACCENT, width=3, height=13).pack(side="left", padx=(0, 7))
        tk.Label(row, text=text, bg=C_CARD, fg=C_TEXT, font=F_H2).pack(side="left")
        if hint:
            tk.Label(row, text=hint, bg=C_CARD, fg=C_SUB, font=F_SMALL).pack(side="left", padx=(8, 0))

    def _build(self):
        root = self.root
        # 头部
        head = tk.Frame(root, bg=C_BG)
        head.pack(fill="x", padx=20, pady=(18, 12))
        tk.Label(head, text="摸鱼工具", bg=C_BG, fg=C_TEXT, font=F_TITLE).pack(side="left")
        tk.Label(head, text="  BossKey · 老板来了别慌", bg=C_BG, fg=C_SUB, font=F_SMALL).pack(side="left", pady=(6, 0))
        self.dot = tk.Label(head, text="● 监听中", bg=C_BG, fg=C_OK, font=F_SMALL)
        self.dot.pack(side="right", pady=(6, 0))

        # 卡片 1：触发方式
        c1 = self._card(root)
        self._card_title(c1, "触发方式", "键盘组合和鼠标按键都可以，任意一个触发")
        row = tk.Frame(c1, bg=C_CARD)
        row.pack(fill="x", pady=(0, 8))
        tk.Label(row, text="键盘组合", bg=C_CARD, fg=C_SUB, font=F_FONT, width=9, anchor="w").pack(side="left")
        self.hk_var = tk.StringVar()
        self.hk_entry = tk.Entry(row, textvariable=self.hk_var, state="readonly", width=18,
                                 readonlybackground="#f7f8fa", fg=C_TEXT, font=F_FONT,
                                 relief="flat", highlightthickness=1, highlightbackground=C_BORDER,
                                 highlightcolor=C_ACCENT)
        self.hk_entry.pack(side="left", ipady=4, padx=(0, 8))
        ttk.Button(row, text="录制", style="Ghost.TButton", width=6, command=self.record_hotkey).pack(side="left")
        ttk.Button(row, text="清除", style="Ghost.TButton", width=6, command=self.clear_hotkey).pack(side="left", padx=6)

        row2 = tk.Frame(c1, bg=C_CARD)
        row2.pack(fill="x")
        tk.Label(row2, text="鼠标按键", bg=C_CARD, fg=C_SUB, font=F_FONT, width=9, anchor="w").pack(side="left")
        self.mouse_vars = {}
        for key, label in (("middle", "中键"), ("x1", "侧键 X1"), ("x2", "侧键 X2")):
            v = tk.BooleanVar()
            self.mouse_vars[key] = v
            ttk.Checkbutton(row2, text=label, variable=v, style="TCheckbutton",
                            command=self._mouse_changed).pack(side="left", padx=(0, 14))

        # 卡片 2：动作
        c2 = self._card(root)
        self._card_title(c2, "按下之后做什么")
        self.act_var = tk.StringVar()
        for key, name, desc in ACTION_ITEMS:
            line = tk.Frame(c2, bg=C_CARD)
            line.pack(fill="x", pady=1)
            ttk.Radiobutton(line, text=name, value=key, variable=self.act_var, style="TRadiobutton",
                            command=self._action_changed).pack(side="left")
            tk.Label(line, text=desc, bg=C_CARD, fg=C_SUB, font=F_SMALL).pack(side="left", padx=(6, 0))
        self.warn = tk.Label(c2, text="", bg=C_CARD, fg="#d97706", font=F_SMALL, anchor="w")
        self.warn.pack(fill="x", pady=(6, 0))

        # 卡片 3：选项
        c3 = self._card(root)
        self._card_title(c3, "附加选项")
        self.opt_restore = tk.BooleanVar()
        self.opt_switch = tk.BooleanVar()
        self.opt_fake = tk.BooleanVar()
        self.opt_start = tk.BooleanVar()
        self.opt_boot = tk.BooleanVar()
        grid = tk.Frame(c3, bg=C_CARD)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        ttk.Checkbutton(grid, text="再按一次恢复", variable=self.opt_restore, style="TCheckbutton").grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(grid, text="隐藏后顺便切到下一个桌面", variable=self.opt_switch, style="TCheckbutton").grid(row=0, column=1, sticky="w")
        ttk.Checkbutton(grid, text="伪装一个 Excel 工作窗口", variable=self.opt_fake, style="TCheckbutton").grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(grid, text="开机自动启动（后台）", variable=self.opt_boot, style="TCheckbutton",
                        command=self._boot_changed).grid(row=1, column=1, sticky="w")
        ttk.Checkbutton(grid, text="启动后直接缩到托盘", variable=self.opt_start, style="TCheckbutton").grid(row=2, column=0, sticky="w")

        # 底部按钮
        foot = tk.Frame(root, bg=C_BG)
        foot.pack(fill="x", padx=20, pady=(2, 6))
        ttk.Button(foot, text="保存并生效", style="Accent.TButton", command=self.save).pack(side="left")
        ttk.Button(foot, text="立即试一下", style="Ghost.TButton", command=self.try_now).pack(side="left", padx=8)
        ttk.Button(foot, text="恢复桌面", style="Ghost.TButton", command=self.restore_now).pack(side="left")
        ttk.Button(foot, text="隐藏到托盘", style="Ghost.TButton", command=self.hide_window).pack(side="right")

        # 状态栏
        self.status = tk.Label(root, text="", bg=C_BG, fg=C_SUB, font=F_SMALL, anchor="w")
        self.status.pack(fill="x", padx=22, pady=(0, 12))

    # ---------- 状态同步
    def _sync_widgets(self):
        cfg = self.cfg
        mods, main = parse_hotkey(cfg.get("hotkey"))
        self.hk_var.set(fmt_hotkey(mods, main) if main else "（未设置）")
        for k, v in self.mouse_vars.items():
            v.set(k in (cfg.get("mouse") or []))
        self.act_var.set(cfg.get("action", "minimize"))
        self.opt_restore.set(bool(cfg.get("restore_on_second")))
        self.opt_switch.set(bool(cfg.get("switch_desktop_after")))
        self.opt_fake.set(bool(cfg.get("camouflage")))
        self.opt_start.set(bool(cfg.get("start_minimized")))
        self.opt_boot.set(get_autostart())
        self._action_changed()
        self._refresh_status()

    def _refresh_status(self, extra=None, color=None):
        mods, main = parse_hotkey(self.cfg.get("hotkey"))
        kb = fmt_hotkey(mods, main) if main else "关闭"
        mouse = " / ".join(BUTTON_NAMES.get(m, m).split("（")[0] for m in (self.cfg.get("mouse") or [])) or "关闭"
        txt = "监听中 · 键盘：%s · 鼠标：%s" % (kb, mouse)
        if extra:
            txt = extra
        self.status.configure(text=txt, fg=color or C_SUB)
        self.dot.configure(text="● 监听中", fg=C_OK)
        if self.status_job:
            try:
                self.root.after_cancel(self.status_job)
            except Exception:
                pass
        self.status_job = self.root.after(6000, lambda: self._refresh_status())

    def set_status(self, text, color=None):
        self._refresh_status(extra=text, color=color)

    # ---------- 交互
    def _action_changed(self):
        a = self.act_var.get()
        if a == "close":
            self.warn.configure(text="⚠ 「温和关闭」会真的关掉程序，未保存的内容可能丢失")
        elif a == "desktop":
            self.warn.configure(text="提示：Win+D 是系统行为，个别全屏程序 / 游戏可能不受影响")
        else:
            self.warn.configure(text="")

    def _mouse_changed(self):
        pass

    def _boot_changed(self):
        ok = set_autostart(self.opt_boot.get())
        if not ok:
            self.opt_boot.set(get_autostart())
            self.set_status("开机自启设置失败（可能被安全软件拦截）", "#d97706")
        else:
            self.set_status("开机自启已%s" % ("开启" if self.opt_boot.get() else "关闭"), C_OK)

    def record_hotkey(self):
        win = tk.Toplevel(self.root)
        win.title("录制快捷键")
        win.configure(bg=C_CARD)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        w, h = 340, 150
        x = self.root.winfo_rootx() + (self.root.winfo_width() - w) // 2
        y = self.root.winfo_rooty() + 160
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        tk.Label(win, text="请按下想要的组合键", bg=C_CARD, fg=C_TEXT, font=F_H2).pack(pady=(22, 6))
        show = tk.Label(win, text="等待按键…", bg=C_CARD, fg=C_ACCENT, font=("Consolas", 14))
        show.pack()
        tk.Label(win, text="建议 Ctrl/Alt + 字母；Esc 取消", bg=C_CARD, fg=C_SUB, font=F_SMALL).pack(pady=(10, 0))

        mods, key = set(), None
        self.svc.recording = True

        def paint():
            if key:
                show.configure(text=fmt_hotkey(mods, key))
            elif mods:
                show.configure(text="+".join(sorted(mods)) + " + ?")

        def on_press(e):
            nonlocal key
            ks = e.keysym
            if ks in ("Control_L", "Control_R"):
                mods.add("ctrl")
            elif ks in ("Shift_L", "Shift_R"):
                mods.add("shift")
            elif ks in ("Alt_L", "Alt_R"):
                mods.add("alt")
            elif ks in ("Super_L", "Super_R"):
                mods.add("win")
            elif ks == "Escape" and not mods:
                close()
                return
            else:
                key = keysym_to_name(ks)
            paint()

        def on_release(e):
            if e.keysym in ("Control_L", "Control_R"):
                mods.discard("ctrl")

        def close():
            self.svc.recording = False
            try:
                win.destroy()
            except Exception:
                pass

        def ok(_=None):
            if not key:
                show.configure(text="请再按一个主键", fg="#d97706")
                return
            self.cfg["hotkey"] = "+".join(sorted(mods) + [key]) if mods else key
            self.svc.pressed_mods.clear()
            close()
            self._sync_widgets()
            self.set_status("快捷键已设为 %s（记得保存生效）" % self.hk_var.get(), C_OK)

        win.bind("<KeyPress>", on_press)
        win.bind("<KeyRelease>", on_release)
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: close())
        win.protocol("WM_DELETE_WINDOW", close)
        win.after(60, lambda: win.focus_force())

    def clear_hotkey(self):
        self.cfg["hotkey"] = ""
        self.hk_var.set("（未设置）")
        self.set_status("键盘快捷键已清除，仅鼠标按键生效", C_OK)

    def try_now(self):
        self.save(silent=True)
        threading.Thread(target=self.boss.fire, daemon=True).start()

    def restore_now(self):
        threading.Thread(target=self.boss.restore, daemon=True).start()

    def tray_fire(self):
        if self.boss.active and self.cfg.get("restore_on_second", True):
            self.restore_now()
        else:
            threading.Thread(target=self.boss.fire, daemon=True).start()

    def toggle_autostart(self):
        self.opt_boot.set(not get_autostart())
        self._boot_changed()

    def save(self, silent=False):
        # cfg["hotkey"] 由录制/清除时写入，这里不重复解析显示串
        self.cfg["mouse"] = [k for k, v in self.mouse_vars.items() if v.get()]
        self.cfg["action"] = self.act_var.get() or "minimize"
        self.cfg["restore_on_second"] = bool(self.opt_restore.get())
        self.cfg["switch_desktop_after"] = bool(self.opt_switch.get())
        self.cfg["camouflage"] = bool(self.opt_fake.get())
        self.cfg["start_minimized"] = bool(self.opt_start.get())
        if not self.cfg["hotkey"] and not self.cfg["mouse"]:
            self.set_status("至少设置一个键盘组合或鼠标按键，否则按什么都没反应", "#d97706")
            return False
        ok = save_config(self.cfg)
        if ok:
            if not silent:
                self.set_status("设置已保存并立即生效", C_OK)
        else:
            self.set_status("配置保存失败，请检查目录权限", "#d97706")
        return ok

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.attributes("-topmost", True)
            self.root.after(300, lambda: self.root.attributes("-topmost", False))
        except Exception:
            pass
        self.root.focus_force()

    def hide_window(self):
        self.root.withdraw()
        if self.cfg.get("notify"):
            self.tray.notify("已缩到托盘，按快捷键即可摸鱼")

    def quit(self):
        try:
            self.svc.stop()
        except Exception:
            pass
        self.tray.stop()
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)


# ---------------------------------------------------------------- 入口

def single_instance():
    h = kernel32.CreateMutexW(None, False, "Local\\%s_Mutex" % APP_ID)
    if ctypes.get_last_error() == 183:
        try:
            user32.MessageBoxW(None, "摸鱼工具已经在运行啦，看右下角托盘图标。", APP_NAME, 0x40)
        except Exception:
            pass
        return None
    return h


def selftest():
    print("python:", sys.version.split()[0])
    print("config:", config_path())
    print("hotkey parse:", parse_hotkey("ctrl+alt+Q"), "| empty:", parse_hotkey(""))
    print("format:", fmt_hotkey({"ctrl", "alt"}, "q"))
    wins = collect_windows()
    print("visible windows:", len(wins))
    for w in wins[:8]:
        print("   -", (w["title"] or "(无标题)")[:40], "|", w["cls"], "| iconic", w["iconic"])
    make_icon_image()
    print("icon ok")
    print("autostart:", get_autostart())
    print("SELFTEST OK")


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    guard = single_instance()
    if guard is None:
        return
    app = App(start_hidden=("--tray" in sys.argv))
    app.root.mainloop()


if __name__ == "__main__":
    main()
