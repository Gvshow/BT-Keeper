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
import struct
import wave
import threading
import logging
import tempfile
import subprocess
import ctypes
import winsound

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

    def play_silent(self):
        for _ in range(3):
            try:
                winsound.PlaySound("SystemDefault",
                                   winsound.SND_ALIAS | winsound.SND_ASYNC)
                return True
            except:
                pass
            try:
                tmp = os.path.join(tempfile.gettempdir(), f"_btk_{os.getpid()}.wav")
                with wave.open(tmp, 'w') as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(44100)
                    wf.writeframes(struct.pack('h' * 13230, *[0] * 13230))
                winsound.PlaySound(tmp, winsound.SND_FILENAME | winsound.SND_ASYNC)
                threading.Timer(0.5, lambda p=tmp: self._del(p)).start()
                return True
            except:
                pass
            try:
                subprocess.run(['powershell', '-Command', '[console]::beep(1000,50)'],
                               capture_output=True, timeout=2)
                return True
            except:
                pass
        return False

    @staticmethod
    def _del(p):
        try:
            if os.path.exists(p):
                os.remove(p)
        except:
            pass

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

    @classmethod
    def save(cls, data):
        try:
            with open(cls.FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            return True
        except:
            return False


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


def _native_input(title, prompt, default="30"):
    """PowerShell InputBox（Windows 原生），返回输入值或 None"""
    # PowerShell 调用 VB InputBox —— Windows 内置，无需额外安装
    ps_code = (
        f'Add-Type -AssemblyName Microsoft.VisualBasic; '
        f'$r = [Microsoft.VisualBasic.Interaction]::InputBox('
        f'"{prompt}", "{title}", "{default}"); '
        f'if ($r) {{Write-Output $r}}'
    )
    try:
        result = subprocess.run(
            ['powershell', '-NoProfile', '-Command', ps_code],
            capture_output=True, text=True, timeout=30
        )
        val = result.stdout.strip()
        return val if val else None
    except Exception as e:
        logger.error(f"InputBox 调用失败: {e}")
        return None


# ===================================================================
#  系统托盘应用
# ===================================================================

class TrayApp:
    def __init__(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except ImportError:
            _native_msg("错误", "缺少依赖：pip install pystray pillow")
            sys.exit(1)

        self.pystray = pystray
        self.Image = Image
        self.ImageDraw = ImageDraw

        self.cfg = Config.load()
        self.keeper = AudioKeeper(self.cfg.get('interval', 30))
        self.icon = None

    def _make_icon_img(self):
        w = h = 64
        img = self.Image.new('RGB', (w, h), 'white')
        d = self.ImageDraw.Draw(img)
        c = (0, 110, 220)
        d.polygon([(22, 15), (42, 10), (42, 50), (22, 45)], fill=c)
        d.arc([40, 10, 58, 50], -30, 30, fill=c, width=3)
        d.arc([46, 5, 66, 55], -30, 30, fill=c, width=3)
        sc = (0, 200, 0) if self.keeper.running else (200, 0, 0)
        d.ellipse([46, 40, 56, 50], fill=sc)
        return img

    # ---- 右键菜单回调 ----

    def _status(self, icon, item):
        autostart = "已启用" if Autostart.is_enabled() else "已禁用"
        txt = (f"蓝牙音响保持连接服务\n\n"
               f"运行状态: {'运行中' if self.keeper.running else '已停止'}\n"
               f"播放间隔: {self.keeper.interval} 秒\n"
               f"累计播放: {self.keeper.count} 次\n"
               f"开机自启: {autostart}\n\n"
               f"原理: 每隔{self.keeper.interval}秒播放一次\n"
               f"极短静音音频，让蓝牙音响保持活跃。")
        _native_msg("服务状态", txt)

    def _settings(self, icon, item):
        val = _native_input("设置播放间隔",
                           "请输入播放间隔（秒）:\n建议值：10~120",
                           str(self.keeper.interval))
        if val is None:
            return  # 用户点了取消
        try:
            interval = int(val)
            if interval < 5:
                _native_msg("提示", "间隔不能少于5秒")
                return
            self.keeper.interval = interval
            cfg = Config.load()
            cfg['interval'] = interval
            Config.save(cfg)
            _native_msg("成功", f"间隔已设为 {interval} 秒")
        except ValueError:
            _native_msg("错误", "请输入有效数字")

    def _toggle_autostart(self, icon, item):
        cur = Autostart.is_enabled()
        ok = Autostart.set_enabled(not cur)
        if ok:
            _native_msg("", f"开机自启已{'启用' if not cur else '禁用'}")
        else:
            _native_msg("错误", "操作失败")

    def _show_log(self, icon, item):
        path = get_log_file()
        if not os.path.exists(path):
            _native_msg("", "暂无日志")
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            # 用记事本打开日志（最简单可靠的方式）
            subprocess.Popen(['notepad.exe', path])
        except Exception as e:
            _native_msg("错误", f"打开日志失败: {e}")

    def _about(self, icon, item):
        _native_msg("关于",
            "蓝牙音响保持连接服务 v3.0\n\n"
            "用静音音频保持蓝牙连接不断开\n\n"
            "功能:\n"
            "· 每N秒播放静音音频\n"
            "· 保持蓝牙音响连接活跃\n"
            "· 支持开机自启\n"
            "· 系统托盘运行")

    def _quit(self, icon, item):
        logger.info("用户点击退出")
        self.keeper.stop()
        self.icon.stop()
        sys.exit(0)

    # ---- 主入口 ----

    def run(self):
        img = self._make_icon_img()
        menu = self.pystray.Menu(
            self.pystray.MenuItem("服务状态", self._status, default=True),
            self.pystray.Menu.SEPARATOR,
            self.pystray.MenuItem("设置间隔", self._settings),
            self.pystray.MenuItem("开机自启", self._toggle_autostart,
                checked=lambda _: Autostart.is_enabled()),
            self.pystray.Menu.SEPARATOR,
            self.pystray.MenuItem("查看日志", self._show_log),
            self.pystray.MenuItem("关于", self._about),
            self.pystray.Menu.SEPARATOR,
            self.pystray.MenuItem("退出", self._quit),
        )

        self.icon = self.pystray.Icon(
            'bt-audio-keeper', img, "蓝牙音响保持连接服务", menu)

        self.keeper.start()
        logger.info("应用程序已启动 → 系统托盘")

        try:
            self.icon.run()
        except KeyboardInterrupt:
            self._quit(None, None)


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