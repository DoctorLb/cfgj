from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                            QPushButton, QLabel, QTextEdit, QLineEdit, QDoubleSpinBox)
from PyQt5.QtCore import pyqtSignal
import sys
import json
import threading
import time
import ctypes
import ctypes.wintypes
import subprocess
from pathlib import Path
from pynput import keyboard
from pynput.keyboard import Key
import pyautogui

try:  # 多分辨率识别需要 OpenCV
    import cv2
    import numpy as np
except ImportError:
    cv2 = None
    np = None


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False


APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent

GAME_PROCESS = "crossfire.exe"  # 游戏进程名
BUTTON_TEMPLATE_SIZE = (80, 20)  # 召唤Boss按钮模板尺寸
# 点击位置在游戏客户区中的相对位置（按 1024×768 客户区标定），
# 窗口移动或缩放后按当前客户区重新换算，不依赖绝对屏幕坐标。
DEFAULT_CARD_RATIO = (0.2695, 0.5013)  # 1星机械魔影卡片中心
DEFAULT_SUMMON_RATIO = (0.7676, 0.7409)  # “召唤Boss”按钮中心
POST_SUMMON_SECONDS = 5.0  # 召唤成功（面板关闭）后等待多久再按 E 开始下一轮
E_RETRY_ATTEMPTS = 30  # 单轮最多按 E 的次数，超过后自动停止循环
E_RETRY_WAIT = 1.0  # 每次按 E 后等待面板出现的时间（每秒按一次）
E_RETRY_GAP = 0.0  # 两次按 E 之间的额外间隔


class _WinRect(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _WinPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _process_image_name(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


def find_game_window():
    """返回游戏主窗口句柄，找不到返回 None。优先可见、客户区最大的窗口。"""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    candidates = []

    @ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):  # 只接受真正显示中的游戏窗口
            return True
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if _process_image_name(pid.value).lower() != GAME_PROCESS:
            return True
        rect = _WinRect()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return True
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return True
        score = width * height
        if user32.IsIconic(hwnd):  # 最小化窗口优先级降低，由 focus_game_window 还原
            score *= 0.5
        candidates.append((score, hwnd))
        return True

    user32.EnumWindows(callback, 0)
    if candidates:
        return max(candidates)[1]
    return None


def game_client_rect():
    """返回游戏客户区 (left, top, width, height)；窗口不存在或已最小化时返回 None。"""
    hwnd = find_game_window()
    if not hwnd:
        return None
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        return None
    rect = _WinRect()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    origin = _WinPoint(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    return (origin.x, origin.y, width, height)


def focus_game_window():
    """把游戏窗口切到前台，避免按键和点击落到其它窗口。"""
    hwnd = find_game_window()
    if not hwnd:
        return False
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        foreground = user32.GetForegroundWindow()
        if foreground != hwnd:
            current_thread = user32.GetWindowThreadProcessId(foreground, None)
            target_thread = user32.GetWindowThreadProcessId(hwnd, None)
            attached = bool(current_thread and target_thread
                            and current_thread != target_thread
                            and user32.AttachThreadInput(current_thread, target_thread, True))
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(current_thread, target_thread, False)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def game_is_foreground():
    hwnd = find_game_window()
    if not hwnd:
        return False
    try:
        return ctypes.windll.user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


class MacroApp(QWidget):
    log_signal = pyqtSignal(str)
    capture_point_signal = pyqtSignal(str)
    f11_signal = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.Card_statistic = 0
        self.Card_statistic_ed = 0
        # 配置固定在脚本（或 exe）所在目录，避免从其它目录启动时读写到别处
        self.config_path = APP_DIR / "config.json"
        self.config = self.load_config()
        self.card_ratio = self.read_ratio("card_point_ratio", DEFAULT_CARD_RATIO)
        self.summon_ratio = self.read_ratio("summon_point_ratio", DEFAULT_SUMMON_RATIO)
        self.template_client_width = self.read_positive_int("button_template_client_width", 1024)
        self.custom_button_image = APP_DIR / "summon_button_custom.png"
        self._pressed_function_keys = set()
        self._consecutive_failures = 0
        self._e_attempts_exhausted = False
        self._image_status = {}
        try:
            wait_seconds = float(self.config.get("post_summon_seconds", POST_SUMMON_SECONDS))
            if not 0 <= wait_seconds <= 60:
                raise ValueError("召唤后等待无效")
            self.post_summon_seconds = wait_seconds
        except (TypeError, ValueError):
            self.post_summon_seconds = POST_SUMMON_SECONDS
        self.run_event = threading.Event()
        self._auto_thread = None
        self.init_ui()
        self.setup_signals()
        self.resize(560, 620)
        self.setWindowTitle("放卡挂机")

    def init_ui(self):
        layout = QVBoxLayout()
        button_layout = QHBoxLayout()
        btn_restart = QPushButton("重启脚本", self)
        btn_restart.clicked.connect(self.restart_program)
        btn_restart.setStyleSheet("background-color: #ffcc00;")
        btn_save = QPushButton("保存配置", self)
        btn_save.clicked.connect(self.save_config)
        button_layout.addWidget(btn_restart)
        button_layout.addWidget(btn_save)
        layout.addLayout(button_layout)

        stat_layout = QHBoxLayout()
        stat_layout.addWidget(QLabel("F11循环次数："))
        self.Statistics = QLineEdit(self)
        self.Statistics.setPlaceholderText("请输入放卡数量")
        self.Statistics.setText("1000")
        stat_layout.addWidget(self.Statistics)
        layout.addLayout(stat_layout)

        wait_layout = QHBoxLayout()
        wait_layout.addWidget(QLabel("召唤成功后等待（秒）："))
        self.post_summon_delay = QDoubleSpinBox(self)
        self.post_summon_delay.setRange(0, 60)
        self.post_summon_delay.setDecimals(1)
        self.post_summon_delay.setSingleStep(0.5)
        self.post_summon_delay.setValue(self.post_summon_seconds)
        wait_layout.addWidget(self.post_summon_delay)
        wait_layout.addStretch()
        layout.addLayout(wait_layout)

        self.custom_position_label = QLabel()
        self.custom_position_label.setWordWrap(True)
        capture_tip = QLabel("先手动按 E 打开面板：鼠标指向卡片中央按 F6，指向召唤Boss按钮中央按 F7（位置按游戏窗口比例保存）。")
        capture_tip.setWordWrap(True)
        layout.addWidget(capture_tip)
        layout.addWidget(self.custom_position_label)
        btn_reset_positions = QPushButton("清除记录的位置", self)
        btn_reset_positions.clicked.connect(self.reset_custom_positions)
        layout.addWidget(btn_reset_positions)
        self.update_custom_position_label()

        lbl_tip = QLabel(
            "•F11：面板未开先按 E（每秒 1 次，最多 30 次；30 次仍失败自动停止循环）→ 点卡片 → 点召唤Boss → 面板关闭后等待设定秒数（默认 5 秒）立即开始下一轮；"
            "失败自动跳过重试；再按 F11 停止。\n"
            "•F6 记录卡片中心，F7 记录“召唤Boss”按钮中心，取点需在面板打开且未自动运行时进行。\n"
            "启动前请关闭召唤面板，保持游戏在前台。")
        lbl_tip.setWordWrap(True)
        lbl_tip.setStyleSheet("color: #666;""font-family: 'SimSun';" "padding: 12px 0;")
        layout.addWidget(lbl_tip)

        layout.addWidget(QLabel("操作日志:"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        self.setLayout(layout)

    def Value_Get(self):
        try:
            value = int(self.Statistics.text())
        except ValueError:
            value = 0
        return value

    def restart_program(self):
        self._pressed_function_keys.clear()
        self.run_event.clear()
        self.log_signal.emit("[系统] 正在重新获取键盘监听权限...")
        if getattr(self, "key_listener", None) is not None:
            self.key_listener.stop()
        self.setup_signals()
        self.log_signal.emit("[系统] 键盘监听已重新获取，重启完成")

    def setup_signals(self):
        self.log_signal.connect(self.log.append)
        self.capture_point_signal.connect(self.capture_custom_position)
        self.f11_signal.connect(self.handle_f11)
        self.key_listener = keyboard.Listener(
            on_press=self.on_key_press,
            on_release=self.on_key_release
        )
        self.key_listener.start()

    def on_key_press(self, key):
        try:
            if key in (Key.f6, Key.f7, Key.f11):
                if key in self._pressed_function_keys:
                    return
                self._pressed_function_keys.add(key)
            if key == Key.f6:
                self.capture_point_signal.emit("card")
            elif key == Key.f7:
                self.capture_point_signal.emit("summon")
            elif key == Key.f11:
                self.f11_signal.emit()
        except Exception as e:
            self.log_signal.emit(f"[错误] 按键处理异常: {str(e)}")

    def on_key_release(self, key):
        self._pressed_function_keys.discard(key)

    def handle_f11(self):
        if self.run_event.is_set():
            self.stop_auto_cycle()
            return
        self.Card_statistic = self.Value_Get()
        self.Card_statistic_ed = 0
        self.start_auto_cycle()

    def start_auto_cycle(self):
        if self._auto_thread is not None and self._auto_thread.is_alive():
            self.log_signal.emit("[系统] 上一次循环正在结束，请稍后再按 F11")
            return
        if self.Card_statistic <= 0:
            self.log_signal.emit("[设置] F11循环次数必须大于 0")
            return
        if find_game_window() is None:
            self.log_signal.emit("[设置] 未找到游戏窗口，请先进入游戏再按 F11")
            return
        if not focus_game_window():
            self.log_signal.emit("[提示] 未能自动切回游戏窗口，请手动点一下游戏画面")
        time.sleep(0.3)
        if game_client_rect() is None:
            self.log_signal.emit("[设置] 游戏窗口已最小化或不可用，请还原窗口后再按 F11")
            return
        self.run_event.set()
        self._consecutive_failures = 0
        self.log_signal.emit("[系统] F11循环启动：E → 卡片 → 召唤Boss；再次按 F11 停止")
        self._auto_thread = threading.Thread(target=self.auto_cycle_worker, daemon=True)
        self._auto_thread.start()

    def stop_auto_cycle(self):
        self.run_event.clear()
        self.log_signal.emit("[系统] 自动循环停止")

    def auto_cycle_worker(self):
        while self.run_event.is_set():
            try:
                if self.Card_statistic_ed >= self.Card_statistic:
                    self.log_signal.emit("[系统] 已完成设定的循环次数")
                    self.stop_auto_cycle()
                    return
                if not self.execute_f11_cycle():
                    return
            except Exception as e:
                self.log_signal.emit(f"[错误] 循环异常: {str(e)}")
                self.stop_auto_cycle()

    def execute_f11_cycle(self):
        if not self.run_event.is_set():
            return False
        if not game_is_foreground():
            if not focus_game_window():
                self.log_signal.emit("[提示] 游戏窗口不在前台且切换失败，请手动点一下游戏画面")
            time.sleep(0.15)
        self._e_attempts_exhausted = False
        if not self.open_summon_panel():
            if self._e_attempts_exhausted:
                self.log_signal.emit(
                    f"[系统] 已按 E {E_RETRY_ATTEMPTS} 次仍未打开召唤面板，自动退出循环")
                self.stop_auto_cycle()
                return False
            self.note_failure("[识别] 多次按 E 仍未出现召唤面板，本轮跳过")
            return self.run_event.is_set()
        card = self.card_point()
        summon = self.summon_point()
        if card is None or summon is None:
            self.note_failure("[设置] 未找到游戏窗口，无法换算点击位置，本轮跳过")
            return self.run_event.is_set()
        self.safe_click(*card, "卡片")
        if not self.interruptible_sleep(0.3):
            return False
        if not self.summon_panel_visible():
            self.note_failure("[识别] 选卡后未找到召唤Boss按钮，本轮跳过")
            return self.run_event.is_set()
        self.safe_click(*summon, "召唤Boss")
        if not self.wait_for_panel_closed(3.0):
            if not self.run_event.is_set():
                return False
            self.note_failure("[识别] 已点击召唤Boss，但面板 3 秒内未关闭，本轮不计入")
            return True
        self._consecutive_failures = 0
        self.Card_statistic_ed += 1
        self.log_signal.emit(f"[操作] 已发送第 {self.Card_statistic_ed}/{self.Card_statistic} 轮：E → 卡片 → 召唤Boss，面板已关闭")
        self.log_signal.emit(f"[等待] 召唤成功，{self.post_summon_seconds:g} 秒后立即开始下一轮")
        return self.interruptible_sleep(self.post_summon_seconds)

    def open_summon_panel(self, attempts=E_RETRY_ATTEMPTS, wait=E_RETRY_WAIT, gap=E_RETRY_GAP):
        """确保召唤面板打开：每秒按一次 E，最多 attempts 次。"""
        self._e_attempts_exhausted = False
        if self.summon_panel_visible():
            return True
        for attempt in range(1, attempts + 1):
            if not self.run_event.is_set():
                return False
            pyautogui.press('e')
            self.log_signal.emit(f"[操作] 第 {attempt}/{attempts} 次按 E，等待召唤面板")
            if self.wait_for_panel(wait):
                return True
            if attempt < attempts and gap > 0:
                self.log_signal.emit(f"[识别] 面板未出现，{gap:g} 秒后再次按 E")
                if not self.interruptible_sleep(gap):
                    return False
        self._e_attempts_exhausted = True
        return False

    def note_failure(self, message):
        """失败不终止循环：记录次数、短暂等待后继续下一轮。"""
        self._consecutive_failures = getattr(self, "_consecutive_failures", 0) + 1
        self.log_signal.emit(f"{message}（连续失败 {self._consecutive_failures} 次，继续重试）")
        if self._consecutive_failures % 5 == 0:
            self.log_signal.emit("[提示] 已连续失败 5 轮，请检查游戏画面；按 F11 可随时停止")
            self.interruptible_sleep(3.0)
        else:
            self.interruptible_sleep(1.0)

    def interruptible_sleep(self, duration):
        deadline = time.monotonic() + duration
        while True:
            if not self.run_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(0.05, remaining))

    def wait_for_panel(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.run_event.is_set():
                return False
            if self.summon_panel_visible():
                return True
            time.sleep(0.1)
        self.log_signal.emit(f"[识别] {timeout:g} 秒内未识别到召唤面板，请检查游戏画面和模板")
        return False

    def wait_for_panel_closed(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.run_event.is_set():
                return False
            if not self.summon_panel_visible():
                return True
            time.sleep(0.1)
        return False

    def _ratio_to_screen(self, ratio):
        if ratio is None:
            return None
        rect = game_client_rect()
        if rect is None:
            return None
        left, top, width, height = rect
        return (int(round(left + ratio[0] * width)), int(round(top + ratio[1] * height)))

    def card_point(self):
        return self._ratio_to_screen(self.card_ratio)

    def summon_point(self):
        return self._ratio_to_screen(self.summon_ratio)

    def _template_scales(self, client_width, template_width):
        base = client_width / float(self.template_client_width or 1024)
        candidates = (base, base * 0.9, base * 1.1, 1.0, 1.25, 1.5, 1.75, 2.0, 0.75)
        scales, seen = [], set()
        for scale in candidates:
            if not 0.4 <= scale <= 3.0:
                continue
            key = round(scale, 2)
            if key in seen or template_width * scale < 12:
                continue
            seen.add(key)
            scales.append(scale)
        return scales

    def _best_button_score(self, screen, template, client_width):
        height, width = template.shape[:2]
        best, best_scale = 0.0, 1.0
        for scale in self._template_scales(client_width, width):
            size = (max(1, int(round(width * scale))), max(1, int(round(height * scale))))
            if size[0] > screen.shape[1] or size[1] > screen.shape[0]:
                continue
            resized = cv2.resize(template, size, interpolation=cv2.INTER_AREA)
            result = cv2.matchTemplate(screen, resized, cv2.TM_CCOEFF_NORMED)
            if not result.size:
                continue
            score = float(result.max())
            if score > best:
                best, best_scale = score, scale
        return best, best_scale

    def button_template_path(self):
        """优先用程序目录的模板（F7 可覆盖），打包成单文件 EXE 时回退到内置资源。"""
        if self.custom_button_image.is_file():
            return self.custom_button_image
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "summon_button_custom.png"
        return bundled if bundled.is_file() else None

    def _match_summon_button(self):
        """在召唤按钮附近多尺度查找按钮文字，窗口移动或分辨率变化后仍可识别。"""
        rect = game_client_rect()
        if rect is None:
            self.log_image_status("召唤面板", False, "未找到游戏窗口")
            return False
        template_path = self.button_template_path()
        if template_path is None:
            raise RuntimeError(f"按钮模板不存在: {self.custom_button_image}，请先按 F7 记录召唤Boss位置")
        template = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise RuntimeError(f"无法读取按钮模板: {template_path}")
        screen = cv2.cvtColor(np.array(pyautogui.screenshot(region=rect)), cv2.COLOR_RGB2GRAY)
        best, detail = 0.0, "未匹配召唤Boss按钮"
        point = self._ratio_to_screen(self.summon_ratio)
        if point is not None:
            # 只在按钮附近查找，避免把标题里的“召唤Boss”误判为按钮，也减少截图开销
            x = point[0] - rect[0]
            y = point[1] - rect[1]
            area = screen[max(0, y - 110):min(rect[3], y + 110),
                          max(0, x - 240):min(rect[2], x + 240)]
            best, scale = self._best_button_score(area, template, rect[2])
            detail = f"附近区域最高相似度 {best:.2f}（缩放 {scale:.2f}）"
        else:
            best, scale = self._best_button_score(screen, template, rect[2])
            detail = f"整屏最高相似度 {best:.2f}（缩放 {scale:.2f}）"
        if best >= 0.72:
            self.log_image_status("召唤面板", True, f"已匹配，{detail}")
            return True
        self.log_image_status("召唤面板", False, detail)
        return False

    def summon_panel_visible(self):
        if cv2 is None:
            raise RuntimeError("缺少 OpenCV，无法识别召唤面板；请安装 opencv-python")
        if self.button_template_path() is None:
            raise RuntimeError(f"按钮模板不存在: {self.custom_button_image}，请先按 F7 记录召唤Boss位置")
        return self._match_summon_button()

    def log_image_status(self, name, found, detail):
        now = time.monotonic()
        previous = self._image_status.get(name)
        if previous is None or previous[0] != found or now - previous[1] >= 10:
            self.log_signal.emit(f"[识别] {name}: {detail}")
            self._image_status[name] = (found, now)

    def read_ratio(self, key, default):
        value = self.config.get(key)
        if (isinstance(value, (list, tuple)) and len(value) == 2
                and all(isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
                        for v in value)):
            return (float(value[0]), float(value[1]))
        return default

    def read_positive_int(self, key, default):
        value = self.config.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
        return default

    def update_custom_position_label(self):
        rect = game_client_rect()
        if rect:
            window = f"游戏窗口 {rect[2]}×{rect[3]} @({rect[0]},{rect[1]})"
        elif find_game_window():
            window = "游戏窗口：已最小化，还原后可用"
        else:
            window = "游戏窗口：未找到（请先进入游戏）"
        card = self.card_point()
        summon = self.summon_point()
        self.custom_position_label.setText(
            f"{window}\n"
            f"卡片：比例({self.card_ratio[0]:.4f},{self.card_ratio[1]:.4f}) 当前屏幕 {card}\n"
            f"召唤Boss：比例({self.summon_ratio[0]:.4f},{self.summon_ratio[1]:.4f}) 当前屏幕 {summon}\n"
            "位置按游戏窗口比例保存，移动或缩放窗口后无需重新取点（界面布局不变时）。")

    def capture_custom_position(self, kind):
        if self.run_event.is_set():
            self.log_signal.emit("[设置] 请先按 F11 停止自动循环，再按 F6/F7 取点")
            return
        try:
            rect = game_client_rect()
            if rect is None:
                raise RuntimeError("未找到游戏窗口，请先进入游戏")
            x, y = map(int, pyautogui.position())
            left, top, width, height = rect
            if not (left <= x < left + width and top <= y < top + height):
                raise RuntimeError("请把鼠标放到游戏画面内再取点")
            ratio = (round((x - left) / float(width), 4), round((y - top) / float(height), 4))
            if kind == "card":
                self.card_ratio = ratio
                self.log_signal.emit(f"[设置] 已记录卡片位置 比例={ratio} 当前屏幕=({x},{y})")
            else:
                half_w, half_h = BUTTON_TEMPLATE_SIZE[0] // 2, BUTTON_TEMPLATE_SIZE[1] // 2
                if not (left <= x - half_w and x + half_w <= left + width
                        and top <= y - half_h and y + half_h <= top + height):
                    raise RuntimeError("召唤Boss按钮离游戏窗口边缘太近，请把鼠标放在按钮中央")
                button = pyautogui.screenshot(
                    region=(x - half_w, y - half_h, *BUTTON_TEMPLATE_SIZE))
                button.save(self.custom_button_image)
                self.summon_ratio = ratio
                self.template_client_width = width
                self.log_signal.emit(f"[设置] 已记录召唤Boss位置 比例={ratio} 当前屏幕=({x},{y}) 并保存按钮图片")
            self.update_custom_position_label()
            self.save_config()
        except Exception as e:
            self.log_signal.emit(f"[错误] 取点失败: {e}")

    def reset_custom_positions(self):
        if self.run_event.is_set():
            self.log_signal.emit("[设置] 请先按 F11 停止自动循环，再清除自定义位置")
            return
        self.card_ratio = DEFAULT_CARD_RATIO
        self.summon_ratio = DEFAULT_SUMMON_RATIO
        self.template_client_width = 1024
        self.update_custom_position_label()
        self.save_config()

    def safe_click(self, x, y, label):
        try:
            pyautogui.moveTo(x, y, duration=0.01)
            pyautogui.click()
            self.log_signal.emit(f"[操作] 已发送点击 {label}({x},{y})")
        except Exception as e:
            self.log_signal.emit(f"[错误] 点击失败: {str(e)}")
            raise

    def save_config(self):
        wait_seconds = self.post_summon_delay.value()
        self.config = {
            "post_summon_seconds": wait_seconds,
            "card_point_ratio": list(self.card_ratio),
            "summon_point_ratio": list(self.summon_ratio),
            "button_template_client_width": self.template_client_width
        }
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            self.post_summon_seconds = wait_seconds
            self.log_signal.emit("[系统] 配置已保存")
        except Exception as e:
            self.log_signal.emit(f"[错误] 保存失败: {str(e)}")

    def load_config(self):
        for encoding in ("utf-8-sig", "gbk"):
            try:
                with open(self.config_path, "r", encoding=encoding) as f:
                    config = json.load(f)
                return config if isinstance(config, dict) else {}
            except UnicodeError:
                continue
            except (OSError, ValueError):
                return {}
        return {}

    def closeEvent(self, event):
        self.run_event.clear()
        if getattr(self, "key_listener", None) is not None:
            self.key_listener.stop()
        event.accept()


if __name__ == "__main__":
    if not is_admin():
        # list2cmdline 会为含空格的路径正确加引号
        params = subprocess.list2cmdline(sys.argv)
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        sys.exit()
    app = QApplication(sys.argv)
    window = MacroApp()
    window.show()
    sys.exit(app.exec_())
