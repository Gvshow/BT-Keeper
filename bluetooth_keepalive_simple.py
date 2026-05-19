#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
蓝牙音响保持连接服务
每30秒播放静音音频，保持蓝牙音响连接不断开
Windows后台应用，系统托盘运行

重要：所有对话框使用 Windows 原生 API（ctypes/PowerShell），
     彻底避免 pystray 与 tkinter 的事件循环冲突。
"""

import sys
import os
import time
import json
import wave
import threading
import logging
import tempfile
import subprocess
import ctypes
import ctypes.wintypes as wintypes

# ---------- 路径 ----------
def get_app_path():
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))

def get_log_file():
    return os.path.join(get_app_path(), 'bluetooth_keepalive.log')

def get_config_file():
    return os.path.join(get_app_path(), 'config.json')


# ---------- 日志 ----------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(get_log_file(), encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


# ---------- 音频保持核心 ----------
class AudioKeeper:
    def __init__(self, interval=30):
        self.interval = interval
        self.running = False
        self.thread = None
        self.count = 0

    # ---------- 底层 waveOut API（绕过系统音效） ----------
    _winmm = None
    _waveout = None
    _silent_wave = None

    @classmethod
    def _ensure_winmm(cls):
        if cls._winmm is None:
            cls._winmm = ctypes.windll.winmm

    @classmethod
    def _open_waveout(cls):
        """打开波形输出设备，持续播放无声流"""
        cls._ensure_winmm()
        if cls._waveout is not None:
            return True
        fmt = (wintypes.WORD * 7)(1, 2, 44100, 176400, 4, 16, 0)  # PCM 16bit 44.1kHz stereo
        h = wintypes.HANDLE(0)
        ret = cls._winmm.waveOutOpen(
            ctypes.byref(h), 0, fmt, 0, 0, 0)
        if ret != 0:
            return False
        cls._waveout = h
        # 创建 0.5 秒静音缓冲
        buf = ctypes.create_string_buffer(b'\x00\x00\x00\x00' * 44100)
        cls._silent_wave = buf
        return True

    @classmethod
    def _cleanup_temp(cls, path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except:
            pass

    @classmethod
    def _play_silent_burst(cls):
        """一次性播放一小段静音（不依赖持续流）"""
        cls._ensure_winmm()
        tmp = os.path.join(tempfile.gettempdir(), f"_btk_{os.getpid()}_{threading.get_ident()}.wav")
        try:
            with wave.open(tmp, 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(44100)
                wf.writeframes(b'\x00\x00' * 6615)
            alias = f'btk{os.getpid()}_{threading.get_ident()}'
            cls._winmm.mciSendStringW(f'close {alias}', None, 0, None)
            cls._winmm.mciSendStringW(f'open "{tmp}" type waveaudio alias {alias}', None, 0, None)
            cls._winmm.mciSendStringW(f'play {alias} from 0', None, 0, None)
            threading.Timer(0.4, lambda p=tmp, a=alias: (
                cls._winmm.mciSendStringW(f'close {a}', None, 0, None),
                cls._cleanup_temp(p)
            )).start()
            return True
        except:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except:
                pass
            return False

    def _play_silent_fallback(self):
        """终极备用：PowerShell 无声播放（隐藏窗口）"""
        tmp = os.path.join(tempfile.gettempdir(), f"_btk_fb_{os.getpid()}.wav")
        try:
            with wave.open(tmp, 'w') as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(22050)
                wf.writeframes(b'\x00\x00' * 1103)
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0  # SW_HIDE
            subprocess.run(
                ['powershell', '-NoProfile', '-Command',
                 f'(New-Object Media.SoundPlayer "{tmp}").PlaySync(); Start-Sleep 0.1'],
                capture_output=True, timeout=5,
                startupinfo=si)
            os.remove(tmp)
            return True
        except:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except:
                pass
            return False

    def play_silent(self):
        """播放静音音频，保持蓝牙连接，绝不触发系统音效"""
        # 方式1: MCI 底层播放（绕过 winsound 系统音效）
        for _ in range(2):
            if self._play_silent_burst():
                return True
        # 方式2: PowerShell .NET SoundPlayer（绕开系统音效系统）
        return self._play_silent_fallback()

    def _loop(self):
        logger.info(f"服务启动，间隔 {self.interval} 秒")
        self.count = 0
        while self.running:
            try:
                ok = self.play_silent()
                self.count += 1
                if ok:
                    logger.info(f"第 {self.count} 次 · 播放成功")
                else:
                    logger.warning(f"第 {self.count} 次 · 所有方法均失败")
                for _ in range(self.interval * 2):
                    if not self.running:
                        return
                    time.sleep(0.5)
            except Exception as e:
                logger.error(f"循环异常: {e}")
                time.sleep(5)

    def start(self):
        if not self.running:
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            logger.info("服务已启动")
            return True
        return False

    def stop(self):
        self.running = False
        logger.info("服务已停止")
        return True


# ---------- 配置 ----------
class Config:
    FILE = get_config_file()

    @classmethod
    def load(cls):
        try:
            if os.path.exists(cls.FILE):
                with open(cls.FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except:
            pass
        return {'interval': 30}


# ---------- 开机自启 ----------
class Autostart:
    REG_PATH = r'Software\Microsoft\Windows\CurrentVersion\Run'
    REG_KEY = 'BluetoothAudioKeeper'

    @staticmethod
    def is_enabled():
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, Autostart.REG_PATH)
            winreg.QueryValueEx(k, Autostart.REG_KEY)
            winreg.CloseKey(k)
            return True
        except:
            return False

    @staticmethod
    def set_enabled(enabled):
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, Autostart.REG_PATH,
                               0, winreg.KEY_SET_VALUE)
            if enabled:
                exe = sys.executable
                if not getattr(sys, 'frozen', False):
                    exe = f'"{exe}" "{os.path.abspath(__file__)}"'
                winreg.SetValueEx(k, Autostart.REG_KEY, 0, winreg.REG_SZ, exe)
            else:
                try:
                    winreg.DeleteValue(k, Autostart.REG_KEY)
                except:
                    pass
            winreg.CloseKey(k)
            return True
        except:
            return False


# ===================================================================
#  Windows 原生对话框（彻底避免 tkinter 冲突）
# ===================================================================

def _native_msg(title, text):
    """Windows 原生消息框"""
    ctypes.windll.user32.MessageBoxW(0, text, title, 0)


# ===================================================================
#  Windows 原生系统托盘（使用 Shell_NotifyIconW，无需 pystray/Pillow）
# ===================================================================

# Windows API 常量
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_NULL = 0
WM_COMMAND = 0x0111
WM_DESTROY = 0x0002
WM_QUIT = 0x0012
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETVERSION = 4

NIF_MESSAGE = 1
NIF_ICON = 2
NIF_TIP = 4
NIF_SHOWTIP = 0x80
NIF_GUID = 0x20

NOTIFYICON_VERSION_4 = 4

MF_STRING = 0
MF_SEPARATOR = 0x0800
MF_CHECKED = 8
MF_UNCHECKED = 0
TPM_LEFTALIGN = 0
TPM_RIGHTBUTTON = 2
IDM_STATUS = 1001
IDM_AUTOSTART = 1002
IDM_LOG = 1003
IDM_ABOUT = 1004
IDM_EXIT = 1005

LR_DEFAULTCOLOR = 0

# Windows API 函数
kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
shell32 = ctypes.windll.shell32

# 补充类型
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_int,
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# 设置 Windows API 函数的参数类型（64位兼容）
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = wintypes.LPARAM
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL

class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", ctypes.c_byte * 16),
        ("hBalloonIcon", wintypes.HICON),
    ]

class ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HBITMAP),
        ("hbmColor", wintypes.HBITMAP),
    ]

class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]

class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
    ]

class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", ctypes.c_wchar_p),
        ("lpszClassName", ctypes.c_wchar_p),
    ]

# --- 图标绘制（GDI） ---

def _draw_icon(running):
    """用 GDI 绘制喇叭图标，返回 HICON"""
    w = h = 64
    # 创建 DIB section（32-bit BGRA）
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    hdc = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc)
    pixels_ptr = ctypes.POINTER(wintypes.DWORD)()
    hbmp = gdi32.CreateDIBSection(hdc, ctypes.byref(bmi), 0,
                                   ctypes.byref(pixels_ptr), None, 0)
    old_bmp = gdi32.SelectObject(hdc_mem, hbmp)

    # 直接操作像素：pixels[y * w + x] = BGRA
    buf_type = wintypes.DWORD * (w * h)
    pixels = buf_type.from_address(ctypes.addressof(pixels_ptr.contents))

    # 透明背景
    for i in range(w * h):
        pixels[i] = 0x00000000  # 全透明

    BLUE = 0xFF006EDC  # BGRA: Blue=0xDC, Green=0x6E, Red=0x00, Alpha=0xFF → RGB(0,110,220)
    DARK_BLUE = 0xFFB45000  # 深蓝 RGB(0,80,180)

    def setp(x, y, color):
        if 0 <= x < w and 0 <= y < h:
            pixels[y * w + x] = color

    def fill(x1, y1, x2, y2, color):
        for y in range(y1, y2 + 1):
            for x in range(x1, x2 + 1):
                setp(x, y, color)

    def fill_circle(cx, cy, r, color):
        for y in range(cy - r, cy + r + 1):
            for x in range(cx - r, cx + r + 1):
                dx, dy = x - cx, y - cy
                if dx * dx + dy * dy <= r * r:
                    setp(x, y, color)

    # 喇叭主体（左侧梯形）
    fill(22, 15, 28, 48, BLUE)

    # 喇叭左侧
    fill(16, 22, 22, 41, BLUE)

    # 喇叭右侧扩展
    fill(28, 18, 30, 45, BLUE)

    # 声波弧线（右侧弧形区域）
    for y in range(12, 52):
        for x in range(32, 48):
            dx, dy = x - 28, y - 32
            dist = (dx * dx + dy * dy) ** 0.5
            if 12 <= dist <= 16 and dy >= -14 and dy <= 14:
                setp(x, y, BLUE)

    # 外圈声波
    for y in range(8, 56):
        for x in range(36, 54):
            dx, dy = x - 28, y - 32
            dist = (dx * dx + dy * dy) ** 0.5
            if 20 <= dist <= 24 and dy >= -18 and dy <= 18:
                setp(x, y, BLUE)

    # 状态指示灯（右下角）
    STATUS_COLOR = 0xFF00D800 if running else 0xFF0000D8  # 绿/红
    fill_circle(50, 46, 7, STATUS_COLOR)

    gdi32.SelectObject(hdc_mem, old_bmp)

    # 创建单色 mask（全 0 = 完全不透明）
    row_size = (w + 31) // 32 * 4  # 单色位图每行对齐到 DWORD
    mask_data = (wintypes.BYTE * (row_size * h))()
    hbmp_mask = gdi32.CreateBitmap(w, h, 1, 1, mask_data)

    iconinfo = ICONINFO()
    iconinfo.fIcon = True
    iconinfo.hbmMask = hbmp_mask
    iconinfo.hbmColor = hbmp
    hicon = user32.CreateIconIndirect(ctypes.byref(iconinfo))

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteObject(hbmp_mask)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(None, hdc)
    return hicon


# --- 全局窗口过程 ---

_tray_app_instance = None

@WNDPROC
def _tray_wnd_proc(hwnd, msg, wparam, lparam):
    global _tray_app_instance
    if _tray_app_instance is not None:
        return _tray_app_instance._on_message(hwnd, msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# --- TrayApp ---

class TrayApp:
    def __init__(self):
        self.cfg = Config.load()
        self.keeper = AudioKeeper(self.cfg.get('interval', 30))
        self.hwnd = None
        self.hicon = None
        self.running = False

    def _on_message(self, hwnd, msg, wparam, lparam):
        if msg == WM_TRAYICON:
            # lparam 低16位是鼠标消息，高16位可能是 uID
            msg_id = lparam & 0xFFFF
            if msg_id == WM_RBUTTONUP:
                self._show_menu()
            elif msg_id == WM_LBUTTONUP or msg_id == WM_LBUTTONDBLCLK:
                self._status()
        elif msg == WM_COMMAND:
            cmd_id = wparam & 0xFFFF
            if cmd_id == IDM_STATUS:
                self._status()
            elif cmd_id == IDM_AUTOSTART:
                self._toggle_autostart()
            elif cmd_id == IDM_LOG:
                self._show_log()
            elif cmd_id == IDM_ABOUT:
                self._about()
            elif cmd_id == IDM_EXIT:
                self._quit()
        elif msg == WM_DESTROY:
            self._cleanup_tray()
            user32.PostQuitMessage(0)
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _create_window(self):
        """创建隐藏窗口"""
        hinst = kernel32.GetModuleHandleW(None)
        class_name = "BTKeeperTrayWClass"

        wc = WNDCLASSW()
        wc.lpfnWndProc = _tray_wnd_proc
        wc.hInstance = hinst
        wc.lpszClassName = class_name
        user32.RegisterClassW(ctypes.byref(wc))

        self.hwnd = user32.CreateWindowExW(
            0, class_name, "BT-Keeper",
            0, 0, 0, 0, 0,
            None, None, hinst, None)
        return self.hwnd is not None

    def _add_tray(self):
        """添加系统托盘图标"""
        self.hicon = _draw_icon(self.keeper.running)
        if not self.hicon:
            return False

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self.hicon
        nid.szTip = "蓝牙音响保持连接服务"

        ret = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        if not ret:
            return False

        # 设置版本
        nid.uVersion = NOTIFYICON_VERSION_4
        shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(nid))
        self.nid = nid
        return True

    def _update_icon(self):
        """更新托盘图标（运行状态改变时）"""
        new_icon = _draw_icon(self.keeper.running)
        if self.nid and new_icon:
            if self.hicon:
                user32.DestroyIcon(self.hicon)
            self.hicon = new_icon
            self.nid.hIcon = self.hicon
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self.nid))

    def _cleanup_tray(self):
        """清理托盘图标"""
        if self.nid:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self.nid))
            self.nid = None
        if self.hicon:
            user32.DestroyIcon(self.hicon)
            self.hicon = None

    def _show_menu(self):
        """显示右键弹出菜单"""
        hmenu = user32.CreatePopupMenu()
        user32.AppendMenuW(hmenu, MF_STRING, IDM_STATUS, "服务状态")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)

        flags = MF_STRING
        if Autostart.is_enabled():
            flags |= MF_CHECKED
        user32.AppendMenuW(hmenu, flags, IDM_AUTOSTART, "开机自启")

        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, IDM_LOG, "查看日志")
        user32.AppendMenuW(hmenu, MF_STRING, IDM_ABOUT, "关于")
        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, MF_STRING, IDM_EXIT, "退出")

        # 获取鼠标位置
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))

        user32.SetForegroundWindow(self.hwnd)
        user32.TrackPopupMenu(hmenu,
            TPM_LEFTALIGN | TPM_RIGHTBUTTON,
            pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)

    # ---- 菜单回调 ----

    def _status(self):
        autostart = "已启用" if Autostart.is_enabled() else "已禁用"
        txt = (f"蓝牙音响保持连接服务\n\n"
               f"运行状态: {'运行中' if self.keeper.running else '已停止'}\n"
               f"播放间隔: {self.keeper.interval} 秒\n"
               f"累计播放: {self.keeper.count} 次\n"
               f"开机自启: {autostart}\n\n"
               f"原理: 每隔{self.keeper.interval}秒播放一次\n"
               f"极短静音音频，让蓝牙音响保持活跃。")
        _native_msg("服务状态", txt)

    def _toggle_autostart(self):
        cur = Autostart.is_enabled()
        ok = Autostart.set_enabled(not cur)
        if ok:
            _native_msg("", f"开机自启已{'启用' if not cur else '禁用'}")
        else:
            _native_msg("错误", "操作失败")

    def _show_log(self):
        path = get_log_file()
        if not os.path.exists(path):
            _native_msg("", "暂无日志")
            return
        try:
            subprocess.Popen(['notepad.exe', path])
        except Exception as e:
            _native_msg("错误", f"打开日志失败: {e}")

    def _about(self):
        _native_msg("关于",
            "蓝牙音响保持连接服务 v3.0\n\n"
            "用静音音频保持蓝牙连接不断开\n\n"
            "功能:\n"
            "· 每N秒播放静音音频\n"
            "· 保持蓝牙音响连接活跃\n"
            "· 支持开机自启\n"
            "· 系统托盘运行")

    def _quit(self):
        logger.info("用户点击退出")
        self.keeper.stop()
        self._cleanup_tray()
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
        # 消息循环收到 WM_QUIT 时退出

    # ---- 主入口 ----

    def run(self):
        global _tray_app_instance
        _tray_app_instance = self

        if not self._create_window():
            _native_msg("错误", "创建窗口失败")
            return

        if not self._add_tray():
            _native_msg("错误", "创建托盘图标失败")
            return

        self.keeper.start()
        logger.info("应用程序已启动 → 系统托盘 (原生)")

        # 消息循环
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if not ret:
                break  # WM_QUIT
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        logger.info("应用程序退出")
        sys.exit(0)


def main():
    logger.info("=" * 50)
    logger.info("蓝牙音响保持连接服务 v3.0 启动")
    logger.info("=" * 50)
    try:
        TrayApp().run()
    except Exception as e:
        logger.exception("启动失败")
        _native_msg("错误", f"启动失败:\n{e}")
        sys.exit(1)


if __name__ == '__main__':
    main()