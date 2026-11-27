# -*- coding: utf-8 -*-
"""
暗号表助手·迷你版 (SheetMate-Mini) v1.0.0
- 白天/晚上时段、每天/工作日运行
- 计划运行时可手动立即跑
- 自动清理旧WPS标签页，最多保留2个
- 顶部置顶显示当前步骤/上次/下次抓取时间/报错步骤
- 可开关：WPS选区复制为图片、发送到钉钉；可改钉钉会话名与消息后缀，配置持久化
- 选区复制图片、钉钉唤起均已真机联调：键盘方向键选区→右键「复制为图片」，剪贴板实测含图
- 无黑窗口，步骤和报错都显示在弹窗里
- iOS 风格面板，数字支持鼠标滚轮调整
"""

import os
import sys
import time
import subprocess
import re
import io
import tempfile
import ctypes
from ctypes import wintypes
from pathlib import Path
from datetime import datetime
from PIL import Image

# 固定浏览器目录（必须在 import playwright 之前设置）
# 打包成 exe 后默认会指向每次清空的临时解压目录，这里强制用固定的用户目录
_browsers_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(_browsers_dir)

from playwright.sync_api import sync_playwright
import openpyxl
from tkinter import messagebox

# ==================== 配置 ====================
LUYA_USER = ""
LUYA_PASS = ""
WPS_URL = ""

DATA_URLS = [
    ("暗号数据", "https://bb.luyaapp.live/question/161"),
    ("注册数据", "https://bb.luyaapp.live/question/154?pkg=&device="),
    ("短链数据", "https://bb.luyaapp.live/question/155?pkg=&device="),
]

MAX_KEEP_WPS_TABS = 2
# ==============================================


def app_dir():
    """兼容 exe 打包：返回程序所在目录（而非临时解压目录）"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def get_browser_user_data_dir():
    """若程序同目录已存在 browser_data 则继续沿用（保留已有登录态）；否则放在 LocalAppData 避免污染桌面。"""
    local_dir = app_dir() / "browser_data"
    if local_dir.exists():
        return local_dir
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SheetMateMini" / "browser_data"


BROWSER_USER_DATA = get_browser_user_data_dir()

_single_instance_mutex = None


def acquire_single_instance():
    """只允许运行一个实例；重复双击时直接恢复并置顶已有窗口。"""
    global _single_instance_mutex
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _single_instance_mutex = kernel32.CreateMutexW(
            None, False, "SheetMateMini_v1_Mutex")
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            user32 = ctypes.windll.user32
            for _ in range(12):
                all_wins = _enum_top_windows(target_classes=None, target_titles=None)
                windows = [
                    w for w in all_wins
                    if APP_NAME in (w[2] or "")
                    and w[1] in ("TkTopLevel", "Chrome_WidgetWin_1", "Chrome_WidgetWin_0")
                ]
                if not windows:
                    windows = [
                        w for w in all_wins
                        if APP_NAME in (w[2] or "") and w[1] != "ConsoleWindowClass"
                    ]
                if windows:
                    hwnd = windows[0][0]
                    current_tid = kernel32.GetCurrentThreadId()
                    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
                    attached = False
                    try:
                        if current_tid != target_tid:
                            attached = user32.AttachThreadInput(
                                current_tid, target_tid, True)
                        user32.ShowWindow(hwnd, 9)       # SW_RESTORE
                        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)
                        user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)
                        user32.BringWindowToTop(hwnd)
                        user32.SetForegroundWindow(hwnd)
                        user32.SetActiveWindow(hwnd)
                    finally:
                        if attached:
                            user32.AttachThreadInput(current_tid, target_tid, False)
                    break
                time.sleep(0.15)
            try:
                kernel32.CloseHandle(_single_instance_mutex)
            except Exception:
                pass
            return False
    except Exception:
        pass
    return True


def _expected_chromium_revision():
    """读取当前 playwright 期望的 chromium 版本号（revision）；读不到返回 None。"""
    try:
        import json
        from playwright._impl._driver import compute_driver_executable
        drv = compute_driver_executable()
        node = Path(drv[0] if isinstance(drv, (tuple, list)) else drv)
        for jf in (node.parent / "package" / "browsers.json", node.parent / "browsers.json"):
            if jf.exists():
                data = json.loads(jf.read_text(encoding="utf-8"))
                for b in data.get("browsers", []):
                    if b.get("name") == "chromium":
                        return b.get("revision")
                break
    except Exception:
        return None
    return None


def _chromium_installed():
    """按版本号精确核对当前 playwright 需要的 chromium 是否装好，
    避免本机留有旧版本目录就被误判为已装而跳过下载。"""
    try:
        base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
        rev = _expected_chromium_revision()
        if rev:
            return (base / f"chromium-{rev}" / "chrome-win64" / "chrome.exe").exists()
        # 读不到版本信息时回退：任一 chromium 目录下存在 chrome.exe 即视为已装
        for d in base.glob("chromium-*"):
            if (d / "chrome-win64" / "chrome.exe").exists():
                return True
    except Exception:
        return False
    return False


def _run_driver_install():
    """调用打包进程序的 playwright driver 下载 chromium 内核。"""
    try:
        from playwright._impl._driver import compute_driver_executable
        drv = compute_driver_executable()
        if isinstance(drv, (tuple, list)):
            cmd = [str(drv[0]), *[str(x) for x in drv[1:]], "install", "chromium"]
        else:
            cmd = [str(drv), "install", "chromium"]
        subprocess.run(cmd, check=False, timeout=300)
    except Exception:
        try:
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                           check=False, timeout=300)
        except Exception:
            pass


def ensure_browser_installed():
    """检测 chromium 内核是否已装，没装则自动安装（首次运行联网下载）。
    浏览器目录由 PLAYWRIGHT_BROWSERS_PATH 固定到用户目录，不随临时解压目录丢失。"""
    if _chromium_installed():
        return True
    _run_driver_install()
    return _chromium_installed()


def _launch_context(p, retried=False):
    """启动持久化浏览器上下文；若报内核缺失，先自动补装一次再重试。"""
    BROWSER_USER_DATA.mkdir(parents=True, exist_ok=True)
    try:
        return p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_USER_DATA),
            headless=False,
            viewport={"width": 1920, "height": 1080},
            args=["--start-maximized",
                  "--disable-blink-features=AutomationControlled",
                  "--no-sandbox"],
            accept_downloads=True,
            permissions=["clipboard-read", "clipboard-write"],
        )
    except Exception as e:
        msg = str(e)
        if not retried and "Executable doesn't exist" in msg:
            log("  浏览器内核版本不符，正在自动补装浏览器，请稍候…")
            ensure_browser_installed()
            return _launch_context(p, retried=True)
        raise

_gui_logger = None

APP_NAME = "暗号表助手·迷你版"
APP_VERSION = "1.0.0"
# 更新检查地址：独立迷你版 GitHub 仓库 version.json 的 raw 直链
UPDATE_CHECK_URL = "https://raw.githubusercontent.com/Sorenskr/SheetMate-Mini/main/version.json"
# 手动停止标志：运行中任何等待点检测到就中断本轮
_stop_requested = False


def request_stop():
    global _stop_requested
    _stop_requested = True


def reset_stop():
    global _stop_requested
    _stop_requested = False


def check_stop():
    if _stop_requested:
        raise RuntimeError("用户手动停止了任务")


def sleep_check(seconds):
    """可随时响应停止信号的分段休眠。"""
    end = time.time() + max(0.0, float(seconds))
    while True:
        check_stop()
        rem = end - time.time()
        if rem <= 0:
            break
        time.sleep(min(0.25, rem))


def fetch_remote_version():
    """联网取最新版本号与下载链接；未配置或失败返回 None。"""
    if not UPDATE_CHECK_URL:
        return None
    try:
        import urllib.request, json
        req = urllib.request.Request(UPDATE_CHECK_URL, headers={"User-Agent": f"SheetMate-Mini/{APP_VERSION}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8"))
        ver, url = data.get("version"), data.get("url")
        if ver and url:
            return ver, url
    except Exception:
        pass
    return None


def _ver_newer(remote, current):
    def parts(v):
        return [int(x) for x in str(v).split(".")]
    try:
        return parts(remote) > parts(current)
    except Exception:
        return False


def set_gui_logger(fn):
    global _gui_logger
    _gui_logger = fn


# 顶部状态区：当前步骤 / 上次抓取时间 / 下次抓取时间 / 报错步骤 回调
_step_callback = None
_last_run_callback = None


def set_step_callback(fn):
    global _step_callback
    _step_callback = fn


def set_step(text):
    if _step_callback:
        _step_callback(text)


def set_last_run_callback(fn):
    global _last_run_callback
    _last_run_callback = fn


def notify_last_run():
    if _last_run_callback:
        _last_run_callback()


_prompt_show = None
_prompt_close = None


def set_login_prompt(show_fn, close_fn):
    global _prompt_show, _prompt_close
    _prompt_show = show_fn
    _prompt_close = close_fn


def show_login_prompt():
    if _prompt_show:
        try:
            _prompt_show()
        except Exception:
            pass


def close_login_prompt():
    if _prompt_close:
        try:
            _prompt_close()
        except Exception:
            pass


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        if sys.stdout is not None:
            print(line)
    except Exception:
        pass
    # 落盘一份运行日志，方便排查“界面跑完但没发出去”这类静默问题
    try:
        p = app_dir() / "run.log"
        if not p.exists() or p.stat().st_size < 2_000_000:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        pass
    if _gui_logger:
        try:
            _gui_logger(msg)
        except Exception:
            pass


def log_error(exc=None, title=""):
    """将异常堆栈追加写入 error.log（单文件上限 2MB）。"""
    try:
        import traceback
        p = app_dir() / "error.log"
        mode = "w" if (p.exists() and p.stat().st_size >= 2_000_000) else "a"
        tb = traceback.format_exc()
        if (not tb or tb.strip() == "NoneType: None") and exc is not None:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with open(p, mode, encoding="utf-8") as f:
            head = f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}"
            if title:
                head += f" [{title}]"
            f.write(head + " =====\n")
            if tb:
                f.write(tb)
            elif exc is not None:
                f.write(repr(exc) + "\n")
    except Exception:
        pass


def human_error(e):
    """把技术报错翻译成人话"""
    s = str(e)
    low = s.lower()
    if "executable doesn't exist" in low or "chromium" in low:
        return "浏览器内核没找到：请联网后重新双击运行，让它自动下载浏览器；若还不行，把 error.log 发我"
    if "timeout" in low or "超时" in s:
        return "页面加载太慢，没等到按钮，可能是网络卡了，重试一次"
    if "download" in low:
        return "数据文件没下载下来，检查网络后重试"
    if "login" in low or "登录" in s:
        return "登录没成功，可能需要你在浏览器里手动扫码登录一次"
    if "browser" in low or "launch" in low:
        return "浏览器没能打开，可能被其他程序占用，重试一次"
    return f"运行中出了点问题：{s[:80]}"


def _no_window_flags():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def set_clipboard_windows(text):
    subprocess.run(
        ["clip"],
        input=text.encode("utf-16le"),
        check=False,
        creationflags=_no_window_flags(),
    )


def clear_clipboard_windows():
    """用 Win32 原生接口清空剪贴板，避免读到上一轮残留文本。"""
    if os.name != "nt":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = wintypes.BOOL
    for _ in range(5):
        if user32.OpenClipboard(None):
            try:
                user32.EmptyClipboard()
                return True
            finally:
                user32.CloseClipboard()
        time.sleep(0.12)
    return False


def get_clipboard_text_windows():
    """用 Win32 原生接口读取 Unicode 文字，避免 PowerShell 中文输出编码误判。"""
    if os.name != "nt":
        return ""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.GetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel32.GlobalUnlock.restype = wintypes.BOOL
    for _ in range(3):
        if not user32.OpenClipboard(None):
            time.sleep(0.15)
            continue
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    return ""


# ==================== 剪贴板图片（.NET 中转，避免图片被文本覆盖）====================
def _ps_clipboard(cmd, timeout=40):
    """运行带 WinForms + Drawing 引用的 PowerShell，返回 stdout 文本（无黑框）。"""
    full = ("Add-Type -AssemblyName System.Windows.Forms;"
             "Add-Type -AssemblyName System.Drawing;" + cmd)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command", full],
        capture_output=True, text=True, timeout=timeout,
        creationflags=_no_window_flags(),
    )
    return r.stdout.strip()


def clipboard_has_image():
    """原生查询图片格式，避免 .NET 剪贴板在浏览器未响应时每次卡 40 秒。"""
    if os.name == "nt":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
            user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
            # CF_BITMAP / CF_DIB / CF_DIBV5；WPS 至少会提供其中一种。
            return any(user32.IsClipboardFormatAvailable(fmt) for fmt in (2, 8, 17))
        except Exception:
            return False
    try:
        return _ps_clipboard(
            "[System.Windows.Forms.Clipboard]::ContainsImage()"
        ).lower().startswith("true")
    except Exception:
        return False


def get_clipboard_image_size():
    """读取剪贴板位图尺寸 (width, height)，无法读取返回 None。"""
    try:
        from PIL import ImageGrab
        obj = ImageGrab.grabclipboard()
        if isinstance(obj, Image.Image):
            return obj.size
    except Exception:
        pass
    return None


def save_clipboard_image(path):
    """把剪贴板里的图片（位图）保存成文件；只取位图，天然净化掉其他格式。"""
    out = _ps_clipboard(
        f"[System.Windows.Forms.Clipboard]::GetImage().Save('{path}')")
    return Path(path).exists()


def save_clipboard_image_if_exists(path):
    """剪贴板有图就保存到文件，一次 PowerShell 完成检查+保存。

    返回 'saved'（已保存）/ 'noimage'（剪贴板无图）/ 'error'（保存出错）。
    """
    out = _ps_clipboard(
        "if ([System.Windows.Forms.Clipboard]::ContainsImage()) {"
        f"[System.Windows.Forms.Clipboard]::GetImage().Save('{path}');"
        "Write-Output 'saved'} else {Write-Output 'noimage'}")
    if "saved" in out:
        return "saved"
    if "noimage" in out:
        return "noimage"
    return "error"


def set_clipboard_image_from_file(path):
    """把暂存图片以位图副本写回剪贴板；超大图像素等比压缩，避免内存失败。"""
    src = Path(path)
    use_path = src
    try:
        im = Image.open(src).convert("RGB")
        max_pixels = 14_000_000
        if im.width * im.height > max_pixels:
            scale = (max_pixels / (im.width * im.height)) ** 0.5
            new_size = (max(1, int(im.width * scale)), max(1, int(im.height * scale)))
            im = im.resize(new_size, Image.Resampling.LANCZOS)
            use_path = src.with_name(src.stem + "_send.png")
            im.save(use_path, "PNG", optimize=True)
        else:
            im.close()
    except Exception:
        use_path = src
    ps = (
        f"$img=[System.Drawing.Image]::FromFile('{use_path}');"
        "$bmp=New-Object System.Drawing.Bitmap($img);$ok=$false;"
        "for($i=0;$i -lt 6;$i++){try{[System.Windows.Forms.Clipboard]::SetImage($bmp);"
        "$ok=$true;break}catch{Start-Sleep -Milliseconds 250}};"
        "$img.Dispose();$bmp.Dispose();if($ok){Write-Output 'set_ok'}else{Write-Output 'set_fail'}"
    )
    out = _ps_clipboard(ps)
    return "set_ok" in out.lower()


# ==================== 配置持久化 ====================
import threading as _threading
from datetime import timedelta as _timedelta

CONFIG_PATH = app_dir() / "config.json"
_CONFIG_LOCK = _threading.RLock()

DEFAULT_CONFIG = {
    "day_mode": "daily",
    "day": {"sh": 9, "sm": 0, "eh": 18, "em": 0, "ih": 2, "im": 0},
    "night": {"sh": 18, "sm": 0, "eh": 23, "em": 0, "ih": 2, "im": 0},
    "night_enabled": True,       # 是否启用晚上时段自动执行
    "luya_user": LUYA_USER,      # 后台登录账号
    "luya_pass": LUYA_PASS,      # 后台登录密码
    "wps_url": WPS_URL,          # 金山文档在线表格地址
    "enable_capture": True,      # 跑完后在 WPS 选区复制为图片
    "enable_dingtalk": False,    # 复制后发到钉钉会话
    "dt_auto_send": False,       # 默认先不自动发；开启后才自动按 Enter 发送
    "dt_clear_before_send": True,  # 粘贴前先清空聊天输入框原有残留内容
    "dt_silent_send": True,      # 发送前自动勾选「设为静音」（静音发送）
    "dt_search": "文件小助手",    # 钉钉搜索会话名（支持逗号/分号/换行填多个群）
    "dt_suffix": "数据情况",      # 消息标题后缀：当前时间 + 后缀
    "wps_post_copy_wait_sec": 5, # 复制为图片后稳定等待秒数（默认 5 秒）
    "dt_paste_delay_sec": 2,     # 切入钉钉会话后粘贴前等待秒数
    "dt_send_wait_sec": 5,       # 贴图配文后自动按 Enter 发送前等待秒数
    "prevent_sleep": True,       # 运行与自动值守期间阻止电脑休眠息屏
    "open_wps_after_run": False, # 每轮跑完是否自动用系统程序打开表格
    "write_d3_date": False,      # 是否自动写入汇总表 D3 查询日期并清空 D4/E4/D5
    "d3_day_offset": 0,          # 0=今天, -1=昨天, -2=前天
    "write_r4_time": True,       # 全流程贴入三表后是否在 R4 写入精准更新时间
    # 独立截图发送时段：只约束自动计划，手动按钮始终可以立即发送。
    "capture_plan": {
        "enabled": False,
        "workday": {
            "slot1": True, "start1": "09:00", "end1": "18:00",
            "slot2": False, "start2": "18:00", "end2": "22:00",
        },
        "weekend": {
            "slot1": True, "start1": "09:00", "end1": "18:00",
            "slot2": False, "start2": "18:00", "end2": "22:00",
        },
    },
}


def _deep_merge(default, saved):
    """把保存的配置合并到默认配置上，缺的键补默认值。"""
    out = dict(default)
    for k, v in (saved or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _clamp_int(val, default, lo, hi):
    try:
        if isinstance(val, bool) or val in (None, ""):
            return default
        return max(lo, min(hi, int(round(float(val)))))
    except Exception:
        return default


def normalize_config(cfg):
    """校验并规范化配置项边界，防止损坏或极端数值。"""
    import copy
    out = _deep_merge(copy.deepcopy(DEFAULT_CONFIG), cfg if isinstance(cfg, dict) else {})
    if out.get("day_mode") not in ("daily", "workday"):
        out["day_mode"] = "daily"
    for key, def_slot in (("day", DEFAULT_CONFIG["day"]), ("night", DEFAULT_CONFIG["night"])):
        sl = out.get(key) if isinstance(out.get(key), dict) else {}
        out[key] = {
            "sh": _clamp_int(sl.get("sh"), def_slot["sh"], 0, 23),
            "sm": _clamp_int(sl.get("sm"), def_slot["sm"], 0, 59),
            "eh": _clamp_int(sl.get("eh"), def_slot["eh"], 0, 23),
            "em": _clamp_int(sl.get("em"), def_slot["em"], 0, 59),
            "ih": _clamp_int(sl.get("ih"), def_slot["ih"], 0, 12),
            "im": _clamp_int(sl.get("im"), def_slot["im"], 0, 59),
        }
    out["night_enabled"] = bool(out.get("night_enabled", True))
    out["luya_user"] = str(out.get("luya_user") or "").strip()
    out["luya_pass"] = "" if out.get("luya_pass") is None else str(out.get("luya_pass"))
    out["wps_url"] = str(out.get("wps_url") or "").strip()
    out["enable_capture"] = bool(out.get("enable_capture", True))
    out["enable_dingtalk"] = bool(out.get("enable_dingtalk", False))
    out["dt_auto_send"] = bool(out.get("dt_auto_send", False))
    out["dt_clear_before_send"] = bool(out.get("dt_clear_before_send", True))
    out["dt_silent_send"] = bool(out.get("dt_silent_send", True))
    out["dt_search"] = str(out.get("dt_search") or "文件小助手").strip() or "文件小助手"
    raw_suf = out.get("dt_suffix")
    out["dt_suffix"] = "" if raw_suf is None else str(raw_suf).strip()
    out["wps_post_copy_wait_sec"] = _clamp_int(out.get("wps_post_copy_wait_sec"), 5, 0, 15)
    out["dt_paste_delay_sec"] = _clamp_int(out.get("dt_paste_delay_sec"), 2, 0, 15)
    out["dt_send_wait_sec"] = _clamp_int(out.get("dt_send_wait_sec"), 5, 0, 20)
    out["prevent_sleep"] = bool(out.get("prevent_sleep", True))
    out["open_wps_after_run"] = bool(out.get("open_wps_after_run", False))
    out["write_d3_date"] = bool(out.get("write_d3_date", False))
    out["d3_day_offset"] = _clamp_int(out.get("d3_day_offset"), 0, -7, 0)
    out["write_r4_time"] = bool(out.get("write_r4_time", True))
    return out


def split_dt_targets(raw):
    """支持使用逗号、分号或换行拆分多个钉钉发送目标并去重保序。"""
    targets = []
    for part in re.split(r"[\r\n,;，；]+", str(raw or "")):
        s = part.strip()
        if s and s not in targets:
            targets.append(s)
    return targets or ["文件小助手"]


def set_prevent_sleep(enabled):
    """在 Windows 下阻止或恢复系统休眠/息屏（运行与自动值守期保护）。"""
    if os.name != "nt":
        return False
    try:
        flags = 0x80000003 if enabled else 0x80000000
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
        return True
    except Exception:
        return False


def load_config():
    with _CONFIG_LOCK:
        try:
            if CONFIG_PATH.exists():
                import json
                saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return normalize_config(saved if isinstance(saved, dict) else {})
        except Exception:
            pass
        return normalize_config({})


def save_config(cfg):
    import json
    with _CONFIG_LOCK:
        try:
            norm = normalize_config(cfg)
            payload = json.dumps(norm, ensure_ascii=False, indent=2)
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_p = tempfile.mkstemp(prefix="sm_cfg_", suffix=".tmp", dir=str(CONFIG_PATH.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp_p, str(CONFIG_PATH))
            finally:
                if os.path.exists(tmp_p):
                    try:
                        os.remove(tmp_p)
                    except Exception:
                        pass
            return norm
        except Exception:
            return normalize_config(cfg)


def parse_hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def in_slot(now, start, end):
    try:
        sh, sm = parse_hm(start)
        eh, em = parse_hm(end)
        cur = now.tm_hour * 60 + now.tm_min
        s = sh * 60 + sm
        e = eh * 60 + em
        if s <= e:
            return s <= cur < e
        return cur >= s or cur < e
    except Exception:
        return False


def capture_allowed_now(cfg, now=None):
    """自动计划是否允许此时截图发送；未启用独立计划时保持原行为。"""
    plan = (cfg or {}).get("capture_plan") or {}
    if not plan.get("enabled", False):
        return True
    now = now or time.localtime()
    profile = plan.get("weekend" if now.tm_wday >= 5 else "workday") or {}
    for i in (1, 2):
        if profile.get(f"slot{i}", False) and in_slot(
                now, profile.get(f"start{i}", "09:00"),
                profile.get(f"end{i}", "18:00")):
            return True
    return False


def cleanup_wps_tabs(context, keep=MAX_KEEP_WPS_TABS, wps_url=None):
    try:
        target = wps_url or WPS_URL
        wps_pages = [
            pg for pg in context.pages
            if target in (pg.url or "") or "kdocs.cn/l/" in (pg.url or "")
        ]
        if len(wps_pages) > keep:
            for pg in wps_pages[:-keep]:
                try:
                    pg.close()
                    log("  已关闭旧的表格页面")
                except Exception:
                    pass
    except Exception as e:
        log(f"  清理页面时跳过：{e}")


# 偶发网络抖动错误关键词，命中则自动等待重试，不直接中断
NETWORK_RETRY_ERRS = (
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NETWORK_IO_SUSPENDED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_NAME_NOT_RESOLVED",
    "Timeout",
    "net::ERR",
)


def goto_with_retry(page, url, wait_until="networkidle", timeout=60000, tries=3):
    """打开网页，遇到网络抖动/超时自动重试，最多 tries 次。"""
    last = None
    for i in range(tries):
        check_stop()
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout)
            return
        except Exception as e:
            last = e
            if i < tries - 1 and any(k in str(e) for k in NETWORK_RETRY_ERRS):
                wait = 3 + i * 3
                log(f"  网络抖动，{wait} 秒后自动重试（第 {i + 1}/{tries} 次）")
                sleep_check(wait)
            else:
                raise
    raise last


def download_xlsx(page, name, url, save_dir):
    check_stop()
    log(f"正在下载【{name}】...")
    save_dir.mkdir(exist_ok=True)
    icon_sels = [
        "[class*='download']", "[aria-label*='download']", "[title*='下载']",
        "button:has(.icon-download)", "svg[class*='download']",
    ]
    icon_combined = ", ".join(f"{s}:visible" for s in icon_sels)
    download_btn = ("button:has-text('Download'):visible, "
                    ".modal button:has-text('Download'):visible, "
                    "[role='dialog'] button:has-text('Download'):visible")

    for attempt in range(2):
        check_stop()
        # 页面有持续请求，networkidle 会白等；等表格真正渲染后再找下载入口。
        goto_with_retry(page, url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("table", state="visible", timeout=25000)
        except Exception:
            log("  表格仍在加载，继续等待下载入口…")
        if attempt:
            log("  刷新页面后重试下载…")

        log("  点右下角下载图标...")
        try:
            page.click(icon_combined, timeout=18000)
        except Exception:
            try:
                page.click("text=Download", timeout=3000)
            except Exception:
                log("  ⚠️ 没找到下载按钮")
                continue

        try:
            page.click("text=.xlsx", timeout=2000)
        except Exception:
            pass

        log("  点Download开始下载...")
        try:
            with page.expect_download(timeout=60000) as dl_info:
                page.click(download_btn, timeout=10000)
            dl = dl_info.value
            save_path = save_dir / f"{name}.xlsx"
            dl.save_as(str(save_path))
            log(f"✅ 【{name}】下载完成")
            return save_path
        except Exception:
            log(f"  【{name}】本次下载未成功")
    log(f"  【{name}】两次下载均未成功")
    return None


def xlsx_to_tsv(xlsx_path, tsv_path):
    wb = openpyxl.load_workbook(xlsx_path, read_only=True)
    ws = wb.active
    with open(tsv_path, "w", encoding="utf-8", newline="") as f:
        for row in ws.iter_rows(values_only=True):
            f.write("\t".join(str(c) if c is not None else "" for c in row) + "\n")
    wb.close()
    log(f"✅ 【{tsv_path.stem}】格式转换完成")


def switch_wps_sheet(page, name, tries=3):
    """可靠切换 WPS 工作表：has-text 子串匹配 + 标签栏滚动 + 选中态验证。"""
    item_sel = f".et-status-sheet-item:has-text('{name}')"
    for _ in range(tries):
        for _ in range(8):
            loc = page.locator(item_sel)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=5000)
                    break
            except Exception:
                pass
            try:
                page.locator(".bar-btn.next-btn").first.click(timeout=2000)
                time.sleep(0.6)
            except Exception:
                break
        try:
            page.wait_for_selector(
                f".et-status-sheet-item.selected:has-text('{name}')",
                timeout=8000)
            return True
        except Exception:
            time.sleep(1)
    return False


def paste_to_wps(page, sheet_name, tsv_path):
    check_stop()
    log(f"正在写入【{sheet_name}】...")
    if not switch_wps_sheet(page, sheet_name):
        raise RuntimeError(f"没找到【{sheet_name}】这个工作表")

    sleep_check(3)
    page.keyboard.press("Control+A")
    sleep_check(0.5)
    page.keyboard.press("Delete")
    sleep_check(1)

    with open(tsv_path, "r", encoding="utf-8") as f:
        content = f.read()
    set_clipboard_windows(content)
    sleep_check(0.5)
    page.keyboard.press("Control+V")
    sleep_check(3)
    log(f"✅ 【{sheet_name}】写入完成")


def check_wps_logged_in(page):
    check_selectors = ["text='fx'", "text='开始'", "text='插入'", "text='数据'",
                       "text='样式'", "text='合并'", "text='WPS AI'"]
    for sel in check_selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass
    for frame in page.frames:
        for sel in check_selectors:
            try:
                el = frame.query_selector(sel)
                if el and el.is_visible():
                    return True
            except Exception:
                pass
    return False


def click_in_all_frames(page, button_text, max_wait=20):
    for i in range(max_wait):
        time.sleep(1)
        for sel in [
            f"button:has-text('{button_text}')",
            f"text='{button_text}'",
            f"[role='dialog'] button:has-text('{button_text}')",
        ]:
            try:
                page.click(sel, timeout=800)
                log(f"  点了「{button_text}」")
                return True
            except Exception:
                pass
        for frame in page.frames:
            for sel in [f"button:has-text('{button_text}')", f"text='{button_text}'"]:
                try:
                    frame.click(sel, timeout=500)
                    log(f"  点了「{button_text}」")
                    return True
                except Exception:
                    pass
    return False


def wait_wps_login(page, max_wait=1200):
    """等待 WPS 登录成功：未登录时点「立即登录/确认登录」，出现扫码页则暂停等用户扫码。"""
    if check_wps_logged_in(page):
        return True
    log("  需要登录 WPS，正在找登录入口...")
    click_in_all_frames(page, "立即登录", max_wait=15)
    time.sleep(3)
    click_in_all_frames(page, "确认登录", max_wait=10)
    log("  ⏳ 请在浏览器里扫码登录，登录成功后会自动继续（最多等 20 分钟）")
    show_login_prompt()
    deadline = time.time() + max_wait
    try:
        while time.time() < deadline:
            check_stop()
            time.sleep(2)
            if check_wps_logged_in(page):
                log("  ✅ WPS 登录成功")
                return True
            click_in_all_frames(page, "立即登录", max_wait=2)
        return False
    finally:
        close_login_prompt()


def _col_letter_to_idx(letter):
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _main_canvas_box(page):
    """表格主体是面积最大的 canvas，返回其 bounding box。"""
    boxes = [c.bounding_box() for c in page.query_selector_all("canvas")]
    boxes = [b for b in boxes if b]
    return max(boxes, key=lambda b: b["width"] * b["height"])


def _goto_cell(page, addr):
    """名称框（input.edit-box）只支持定位单个单元格。"""
    eb = page.locator("input.edit-box").first
    eb.click(timeout=5000)
    page.keyboard.press("Control+A")
    eb.fill(addr)
    page.keyboard.press("Enter")
    time.sleep(0.45)


def _detect_last_row(page, start_cell="Q8"):
    """清空剪贴板后复制指定列，原生 Win32 轮询解析真实数据末行。"""
    try:
        clear_clipboard_windows()
        _goto_cell(page, start_cell)
        page.keyboard.press("Control+Shift+ArrowDown")
        time.sleep(0.4)
        page.keyboard.press("Control+C")
        text = ""
        deadline = time.time() + 2.5
        while time.time() < deadline:
            time.sleep(0.15)
            text = get_clipboard_text_windows()
            if text:
                break
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if not text:
            return None
        lines = text.splitlines()
        last = -1
        for i, line in enumerate(lines):
            if line.strip():
                last = i
        if last >= 0:
            start_row = int(re.search(r"\d+", start_cell).group())
            return start_row + last
    except Exception:
        return None
    return None


def _set_wps_zoom(page, percent):
    """通过右下角真实缩放控件设置比例，并回读确认。"""
    wanted = f"{percent}%"
    for _attempt in range(2):
        try:
            page.click("div.zoom-value", timeout=4000)
            time.sleep(0.25)
            zin = None
            for el in page.query_selector_all("input"):
                try:
                    if el.is_visible() and (el.input_value() or "").strip().isdigit():
                        zin = el
                        break
                except Exception:
                    pass
            if zin is not None:
                zin.fill(str(percent))
                page.keyboard.press("Enter")
            else:
                page.click(f".panel-item.only-text:text-is('{wanted}')", timeout=2500)
            time.sleep(0.6)
            actual = (page.locator("div.zoom-value").inner_text() or "").strip()
            if actual == wanted:
                log(f"  缩放比例已确认为 {wanted}")
                return
        except Exception:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
    raise RuntimeError(f"表格缩放没有设置到 {wanted}")


def _active_cell_rect(page, cell):
    """名称框定位单元格后，从 canvas 选中框读取其真实像素位置。"""
    _goto_cell(page, cell)
    box = _main_canvas_box(page)
    if box is None:
        raise RuntimeError("没有找到表格画布")
    canvases = []
    for c in page.query_selector_all("canvas"):
        try:
            r = c.bounding_box()
            if r:
                canvases.append((c, r))
        except Exception:
            pass
    if not canvases:
        raise RuntimeError("表格画布尚未准备好")
    target, _ = max(canvases, key=lambda it: it[1]["width"] * it[1]["height"])
    im = Image.open(io.BytesIO(target.screenshot())).convert("RGB")
    if im.height > 700:
        im = im.crop((0, 0, im.width, 700))
    pix = im.load()
    pts = []
    for y in range(im.height):
        for x in range(im.width):
            r, g, b = pix[x, y]
            if abs(r - 16) < 24 and abs(g - 153) < 30 and abs(b - 104) < 30:
                pts.append((x, y))
    if not pts:
        raise RuntimeError(f"没有识别到 {cell} 的选中框")
    if cell == "C6":
        comp = pts
    else:
        remaining = set(pts)
        comps = []
        while remaining:
            seed = remaining.pop()
            stack = [seed]
            comp_i = [seed]
            while stack:
                x, y = stack.pop()
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        q = (x + dx, y + dy)
                        if q in remaining:
                            remaining.remove(q)
                            stack.append(q)
                            comp_i.append(q)
            comps.append(comp_i)
        comp = max(comps, key=len)
    rect = (min(x for x, y in comp), min(y for x, y in comp),
            max(x for x, y in comp), max(y for x, y in comp))
    return box, rect


def _select_wps_range(page, last_row, use_mouse=True):
    """选中 C6:AJ{last_row}：优先 20% 缩放鼠标框选，未命中则自动键盘精确选区。"""
    if use_mouse:
        try:
            _set_wps_zoom(page, 20)
            box, sr = _active_cell_rect(page, "C6")
            _, er = _active_cell_rect(page, f"AJ{last_row}")
            box, sr = _active_cell_rect(page, "C6")
            sx = box["x"] + sr[0] + 5
            sy = box["y"] + sr[1] + max(2, (sr[3] - sr[1]) // 2)
            ex = box["x"] + (er[0] + er[2]) // 2
            ey = box["y"] + (er[1] + er[3]) // 2
            page.mouse.move(sx, sy)
            page.mouse.down()
            page.mouse.move(ex, ey, steps=35)
            time.sleep(0.25)
            selection = page.eval_on_selector("input.edit-box", "e=>e.value")
            page.mouse.up()
            m = re.search(r"(\d+)R\s*x\s*(\d+)C", selection or "")
            rows = int(m.group(1)) if m else 0
            cols = int(m.group(2)) if m else 0
            if rows != last_row - 5 or cols < 34:
                raise RuntimeError(f"框选回读为 {selection or '未知'}")
            log(f"  已用鼠标框选 C6:AJ{last_row}")
            return
        except Exception as e:
            try:
                page.mouse.up()
            except Exception:
                pass
            log(f"  鼠标框选未命中，改为键盘精确选区：{human_error(e)}")

    _goto_cell(page, "C6")
    for _ in range(max(0, last_row - 6)):
        page.keyboard.press("Shift+ArrowDown")
    for _ in range(_col_letter_to_idx("AJ") - _col_letter_to_idx("C")):
        page.keyboard.press("Shift+ArrowRight")
    time.sleep(0.35)
    log(f"  已选中区域 C6:AJ{last_row}")


def _read_wps_name_box(page):
    """安全回读 WPS 左上角名称框文本（如 C6:AJ24 或 A1），失败返回空串。"""
    try:
        val = page.eval_on_selector("input.edit-box", "e=>e.value")
        return str(val or "").strip()
    except Exception:
        return ""


def _find_c6_blue_banner_point(im, box):
    """识别 200% 缩放下第 6 行深蓝标题栏 (#364C74 = RGB(54,76,116)) 的横条区域，避开左上角 A1。"""
    if im is None or not box:
        return None
    w, h = im.size
    if w < 160 or h < 120:
        return None
    pix = im.load()
    y_start = max(48, int(h * 0.08))
    y_end = min(h - 20, int(h * 0.72))
    x_start = max(60, int(w * 0.08))
    x_end = min(w - 20, int(w * 0.96))
    band_rows = []
    for y in range(y_start, y_end, 2):
        row_xs = []
        for x in range(x_start, x_end, 3):
            r, g, b = pix[x, y]
            if abs(r - 54) <= 38 and abs(g - 76) <= 38 and abs(b - 116) <= 44 and b > r + 24:
                row_xs.append(x)
        if len(row_xs) >= 24:
            span = row_xs[-1] - row_xs[0]
            if span >= max(180, int(w * 0.25)):
                band_rows.append((y, row_xs[0], row_xs[-1], len(row_xs)))
    if not band_rows:
        return None
    groups = []
    cur = [band_rows[0]]
    for item in band_rows[1:]:
        if item[0] - cur[-1][0] <= 8:
            cur.append(item)
        else:
            groups.append(cur)
            cur = [item]
    groups.append(cur)
    best_grp = max(groups, key=lambda g: len(g) * max(x[2] - x[1] for x in g))
    mid_row = best_grp[len(best_grp) // 2]
    cy_local = mid_row[0]
    lx0 = min(x[1] for x in best_grp)
    lx1 = max(x[2] for x in best_grp)
    cx_local = max(lx0 + 80, min(lx1 - 40, int(lx0 + (lx1 - lx0) * 0.35)))
    rx = box["x"] + max(130, cx_local)
    ry = box["y"] + max(115, cy_local)
    return rx, ry


def _find_selection_right_click_point(page):
    """优先锁定 C6 深蓝标题栏 (#364C74)，否则在绿色选区内部计算安全右键坐标，严防误点 A1。"""
    canvases = []
    for c in page.query_selector_all("canvas"):
        try:
            r = c.bounding_box()
            if r and r["width"] > 100 and r["height"] > 100:
                canvases.append((c, r))
        except Exception:
            pass
    if not canvases:
        raise RuntimeError("没有找到表格画布")
    target, box = max(canvases, key=lambda it: it[1]["width"] * it[1]["height"])
    try:
        im = Image.open(io.BytesIO(target.screenshot())).convert("RGB")
        try:
            c6_pt = _find_c6_blue_banner_point(im, box)
            if c6_pt is not None:
                return c6_pt[0], c6_pt[1], True
            pix = im.load()
            min_x = im.width
            min_y = im.height
            max_x = -1
            max_y = -1
            for y in range(0, im.height, 2):
                for x in range(0, im.width, 2):
                    r, g, b = pix[x, y]
                    if abs(r - 16) < 28 and abs(g - 153) < 34 and abs(b - 104) < 34:
                        if x < min_x:
                            min_x = x
                        if x > max_x:
                            max_x = x
                        if y < min_y:
                            min_y = y
                        if y > max_y:
                            max_y = y
            if max_x - min_x >= 40 and max_y - min_y >= 14:
                rx = box["x"] + max(125, min(max_x - 16, min_x + max(36, (max_x - min_x) // 4)))
                ry = box["y"] + max(115, min(max_y - 10, min_y + max(14, min(32, (max_y - min_y) // 2))))
                return rx, ry, True
        finally:
            im.close()
    except Exception:
        pass
    rx = box["x"] + min(280, max(150, int(box["width"] * 0.24)))
    ry = box["y"] + min(210, max(120, int(box["height"] * 0.24)))
    return rx, ry, False


def _click_copy_as_image_menu(page):
    """兼容多个 WPS 右键菜单选择器并点击「复制为图片」。"""
    selectors = [
        ".ks-contextmenu :text('复制为图片')",
        "[class*='contextmenu'] :text('复制为图片')",
        "text='复制为图片'",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=3000)
                return True
        except Exception:
            pass
    # 最后一次再等最多 3 秒
    page.click(", ".join(selectors), timeout=3000)
    return True


def _verify_clipboard_image_size():
    """确认剪贴板图片宽高达标，避免误复制单格或空白区域。"""
    sz = get_clipboard_image_size()
    if sz is not None:
        return sz[0] >= 180 and sz[1] >= 50, sz
    tmp_check = app_dir() / "_sheetmate_check_tmp.png"
    try:
        if save_clipboard_image_if_exists(tmp_check) == "saved" and tmp_check.exists():
            with Image.open(tmp_check) as im:
                sz = im.size
            return sz[0] >= 180 and sz[1] >= 50, sz
    except Exception:
        pass
    finally:
        try:
            if tmp_check.exists():
                tmp_check.unlink()
        except Exception:
            pass
    return True, None


def _format_cn_date(day_offset=0):
    target_dt = datetime.now() + _timedelta(days=int(day_offset or 0))
    return f"{target_dt.year}年{target_dt.month}月{target_dt.day}日"


def _format_cn_timestamp():
    now_dt = datetime.now()
    return f"{now_dt.year}年{now_dt.month}月{now_dt.day}日{now_dt.hour:02d}:{now_dt.minute:02d}:{now_dt.second:02d}"


def _write_wps_cell_text(page, cell_addr, text_val):
    """在当前工作表中精准定位单元格并写入文本或清空。"""
    _goto_cell(page, cell_addr)
    sleep_check(0.18)
    page.keyboard.press("Delete")
    sleep_check(0.12)
    val = str(text_val or "")
    if val:
        set_clipboard_windows(val)
        sleep_check(0.15)
        page.keyboard.press("Control+v")
        sleep_check(0.22)
        page.keyboard.press("Enter")
        sleep_check(0.18)


def apply_mini_summary_cells(page, cfg, pasted_raw=True):
    """轻量汇总表格位写入：按设置写入 D3 日期（并清 D4/E4/D5），全流程粘贴后在 R4 写精准时间。"""
    c = cfg or {}
    if c.get("write_d3_date", False):
        date_str = _format_cn_date(c.get("d3_day_offset", 0))
        _write_wps_cell_text(page, "D3", date_str)
        for clr_cell in ("D4", "E4", "D5"):
            _write_wps_cell_text(page, clr_cell, "")
        log(f"  ✅ 已写入查询日期 D3 -> {date_str}（并清空 D4/E4/D5）")
    if pasted_raw and c.get("write_r4_time", True):
        ts_str = _format_cn_timestamp()
        _write_wps_cell_text(page, "R4", ts_str)
        log(f"  ✅ 已写入更新时间戳 R4 -> {ts_str}")


def capture_wps_region(page, cfg=None):
    """按 Q 列（失败则 D 列兜底）确定末行，框选 C6:AJ 并高清复制为图片。"""
    check_stop()
    log("  准备按 Q 列末行框选表格…")
    last_row = 8
    for _attempt in range(4):
        check_stop()
        candidate = _detect_last_row(page, "Q8")
        if candidate and candidate > 180:
            sleep_check(0.5)
            recheck = _detect_last_row(page, "Q8")
            if recheck and recheck > 8:
                candidate = recheck
        if candidate and candidate > last_row:
            last_row = candidate
        if last_row > 8:
            break
        sleep_check(1.2)
    if last_row <= 8:
        d_cand = _detect_last_row(page, "D8")
        if d_cand and d_cand > last_row and d_cand <= 300:
            last_row = d_cand
    log(f"  Q 列最后有文字的是第 {last_row} 行，目标 C6:AJ{last_row}")

    _select_wps_range(page, last_row, use_mouse=True)
    try:
        _set_wps_zoom(page, 200)
    except Exception:
        try:
            _set_wps_zoom(page, 150)
        except Exception as e_zoom:
            log(f"  高清缩放未生效，按当前比例继续：{human_error(e_zoom)}")

    clear_clipboard_windows()
    for copy_attempt in range(3):
        check_stop()
        if copy_attempt:
            log(f"  本次复制未拿到有效图片，自动重试第 {copy_attempt + 1} 次…")
            try:
                page.keyboard.press("Escape")
                time.sleep(0.25)
            except Exception:
                pass
            _select_wps_range(page, last_row, use_mouse=False)

        rx, ry, hit_green = _find_selection_right_click_point(page)
        if not hit_green and copy_attempt == 0:
            # 缩放后若选区已滚出可视区或丢失，用键盘重新选区使 C6 回到左上可视区域
            _select_wps_range(page, last_row, use_mouse=False)
            rx, ry, _ = _find_selection_right_click_point(page)

        page.mouse.move(rx, ry, steps=10)
        sleep_check(0.20)
        page.mouse.click(rx, ry, button="right")
        sleep_check(0.65)
        nb_after = _read_wps_name_box(page).upper()
        if nb_after == "A1" or (nb_after and ":" not in nb_after and "R" not in nb_after and "C" not in nb_after):
            log(f"  ⚠️ 检测到右键落点跑偏至「{nb_after}」，立即取消并重选…")
            try:
                page.keyboard.press("Escape")
                sleep_check(0.2)
            except Exception:
                pass
            _select_wps_range(page, last_row, use_mouse=False)
            continue
        try:
            _click_copy_as_image_menu(page)
        except Exception:
            continue
        log("  已点「复制为图片」，正在读取图片…")
        for _ in range(40):
            check_stop()
            if clipboard_has_image():
                ok_size, sz = _verify_clipboard_image_size()
                if ok_size:
                    wait_sec = _clamp_int((cfg or {}).get("wps_post_copy_wait_sec"), 5, 0, 15)
                    log(f"  图片已复制到剪贴板（尺寸 {sz}）")
                    if wait_sec > 0:
                        log(f"  ⏳ 复制为图片成功，稳态等待 {wait_sec} 秒后再切换粘贴…")
                        sleep_check(float(wait_sec))
                    return
                log(f"  检测到图片尺寸异常（{sz}），准备重新框选复制…")
                clear_clipboard_windows()
                break
            time.sleep(0.25)
    raise RuntimeError("复制为图片后仍未检测到有效图片，请确认选区及浏览器剪贴板权限后重试")


def _enum_top_windows(target_classes=None, target_titles=None):
    """用 Win32 EnumWindows 枚举可见顶层窗口，返回 (hwnd, class, title) 列表。

    相比 uiautomation 的 GetChildren（TreeWalker 逐个导航，会在某个窗口的
    UIA 提供者无响应时被卡住），EnumWindows 直接走窗口管理器 + GetClassName，
    毫秒级返回、不调用各窗口的 UIA 提供者，稳定不阻塞。
    target_classes / target_titles 为 None 表示不按该条件过滤。
    """
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    # 显式声明原型，避免 64 位窗口句柄被默认按 32 位 c_int 截断
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    results = []

    def _cb(hwnd, lparam):
        if user32.IsWindowVisible(hwnd):
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            tbuf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, tbuf, 256)
            cls, title = cbuf.value, tbuf.value
            if ((target_classes is None or cls in target_classes)
                    and (target_titles is None or title in target_titles)):
                results.append((hwnd, cls, title))
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return results


def _extract_search_keywords(search_name):
    """从含 Emoji 与符号的群名提取高命中率纯文字搜索词和校验分词。"""
    import unicodedata
    normalized = unicodedata.normalize("NFKC", search_name or "").strip()
    tokens = re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", normalized)
    candidates = []
    if tokens:
        by_len = sorted(enumerate(tokens), key=lambda x: (-len(x[1]), x[0]))
        ordered_tokens = [t for _, t in by_len]
        if len(ordered_tokens) >= 2:
            combo = f"{ordered_tokens[0]} {ordered_tokens[1]}"
            if len(combo) <= 14 and combo not in candidates:
                candidates.append(combo)
        if ordered_tokens[0] not in candidates:
            candidates.append(ordered_tokens[0])
        full_space = " ".join(tokens)
        if len(full_space) <= 20 and full_space not in candidates:
            candidates.append(full_space)
    if normalized and normalized not in candidates:
        candidates.append(normalized)
    return candidates, tokens


def _is_search_popup_open():
    """毫秒级检测是否存在可见且宽高均大于 150 的钉钉全局搜索弹窗（ToolSaveBits）。"""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    for h, c, _t in _enum_top_windows():
        if "ToolSaveBits" in c:
            rect = wintypes.RECT()
            if user32.GetWindowRect(h, ctypes.byref(rect)):
                if (rect.right - rect.left) > 150 and (rect.bottom - rect.top) > 150:
                    return True
            else:
                return True
    return False


def send_dingtalk_image(cfg):
    """把 WPS 复制的表格图片发到一个或多个指定钉钉会话，再发“时间+后缀”文字（支持闭静音免误点与稳态等待）。"""
    check_stop()
    target_list = split_dt_targets((cfg or {}).get("dt_search"))
    suffix = str((cfg or {}).get("dt_suffix") or "").strip()
    text_msg = time.strftime("%Y-%m-%d %H:%M") + (" " + suffix if suffix else "")

    try:
        import uiautomation as ua
    except Exception as e:
        raise RuntimeError("钉钉发送组件未安装") from e

    tmp_img = app_dir() / "_sheetmate_mini_tmp.png"
    try:
        exe_candidates = [
            r"D:\Program Files (x86)\DingDing\main\current_new\DingTalk.exe",
            r"C:\Program Files (x86)\DingDing\main\current_new\DingTalk.exe",
            r"C:\Program Files\DingDing\main\current_new\DingTalk.exe",
            os.path.expandvars(r"%LOCALAPPDATA%\DingDing\main\current_new\DingTalk.exe"),
        ]
        WIN_CLASSES = ("DtMainFrameView", "Qt51511QWindowIcon")
        WIN_TITLES = ("钉钉", "DingTalk")

        def _find():
            for hwnd, cls, title in _enum_top_windows(
                    target_classes=WIN_CLASSES, target_titles=None):
                ctl = ua.ControlFromHandle(hwnd)
                if ctl is not None:
                    return ctl
            for hwnd, cls, title in _enum_top_windows(
                    target_classes=None, target_titles=WIN_TITLES):
                ctl = ua.ControlFromHandle(hwnd)
                if ctl is not None:
                    return ctl
            return None

        def _fresh_ding():
            for _ in range(6):
                w = _find()
                if w is not None:
                    try:
                        rr = w.BoundingRectangle
                        if rr.right > rr.left and rr.bottom > rr.top:
                            return w
                    except Exception:
                        pass
                time.sleep(0.5)
            return None

        def _window_rect():
            w = _fresh_ding()
            if w is None:
                return None
            try:
                return w.BoundingRectangle
            except Exception:
                return None

        # 0) 搜索前先把 WPS 图片存成文件（关键：搜索全程禁止在剪贴板放图片）
        state0 = save_clipboard_image_if_exists(tmp_img)
        if state0 == "noimage":
            raise RuntimeError("剪贴板里没有图片，已停止钉钉发送")
        if state0 != "saved":
            raise RuntimeError("表格图片暂存失败，已停止钉钉发送")
        clear_clipboard_windows()
        log("  已暂存表格图片，开始搜索会话")

        # 1) 唤起钉钉（托盘时启动 exe），轮询等待窗口
        ding = _find()
        if ding is None:
            for exe in exe_candidates:
                if os.path.exists(exe):
                    subprocess.Popen(exe)
                    log("  钉钉窗口在托盘，已唤起，请稍候…")
                    break
            for _ in range(30):
                check_stop()
                time.sleep(1)
                ding = _find()
                if ding:
                    break
        if ding is None:
            raise RuntimeError("没找到钉钉窗口，请确认钉钉已登录后再试")

        # 2) 强力置前（AttachThreadInput 解除前台锁定）并最大化
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, wintypes.LPDWORD]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE,
                                       wintypes.DWORD, ctypes.c_ulonglong]
        user32.keybd_event.restype = None
        _KEYUP = 0x0002
        _VK_CTRL, _VK_SHIFT, _VK_F = 0x11, 0x10, 0x46

        def _force_front():
            win = _fresh_ding()
            if win is None:
                return
            try:
                hwnd = win.NativeWindowHandle
                user32.ShowWindow(hwnd, 9)
                cur = kernel32.GetCurrentThreadId()
                fg = user32.GetForegroundWindow()
                fg_thr = user32.GetWindowThreadProcessId(fg, None)
                user32.AttachThreadInput(cur, fg_thr, True)
                user32.SetForegroundWindow(hwnd)
                user32.BringWindowToTop(hwnd)
                user32.AttachThreadInput(cur, fg_thr, False)
                user32.ShowWindow(hwnd, 3)  # 最大化
            except Exception:
                try:
                    win.SetActive()
                except Exception:
                    pass

        def _paste_text(text, verify=False):
            if verify:
                ready = False
                for _ in range(6):
                    set_clipboard_windows(text)
                    time.sleep(0.2)
                    try:
                        if get_clipboard_text_windows().strip() == text.strip():
                            ready = True
                            break
                    except Exception:
                        pass
                    time.sleep(0.25)
                if not ready:
                    return False
            else:
                set_clipboard_windows(text)
            time.sleep(0.3)
            ua.SendKeys("{Ctrl}v", waitTime=0.3)
            return True

        def _type_unicode(text):
            class KEYBDINPUT(ctypes.Structure):
                _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                            ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("dwExtraInfo", ctypes.c_size_t))

            class INPUT(ctypes.Structure):
                _fields_ = (("type", wintypes.DWORD), ("ki", KEYBDINPUT),
                            ("padding", ctypes.c_byte * 8))

            events = []
            raw = text.encode("utf-16-le")
            for i in range(0, len(raw), 2):
                code = raw[i] | (raw[i + 1] << 8)
                events.append(INPUT(1, KEYBDINPUT(0, code, 0x0004, 0, 0)))
                events.append(INPUT(1, KEYBDINPUT(0, code, 0x0006, 0, 0)))
            if events:
                arr = (INPUT * len(events))(*events)
                sent = user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
                if sent != len(events):
                    raise RuntimeError("搜索词输入失败")
            time.sleep(0.3)

        def _link_box():
            tops = _enum_top_windows(
                target_classes=("im_chat::RichInputInsertLinkBox",),
                target_titles=None)
            if not tops:
                return None
            return ua.ControlFromHandle(tops[0][0])

        def _close_link_box():
            box = _link_box()
            if box is not None:
                try:
                    box.ButtonControl(ClassName="ddesign::IconButton").Click()
                    time.sleep(0.8)
                except Exception:
                    pass

        def _open_global_search():
            user32.keybd_event(_VK_CTRL, 0, 0, 0)
            user32.keybd_event(_VK_SHIFT, 0, 0, 0)
            user32.keybd_event(_VK_F, 0, 0, 0)
            time.sleep(0.12)
            user32.keybd_event(_VK_F, 0, _KEYUP, 0)
            user32.keybd_event(_VK_SHIFT, 0, _KEYUP, 0)
            user32.keybd_event(_VK_CTRL, 0, _KEYUP, 0)

        def _search_controls():
            out = []
            for h, c, t in _enum_top_windows():
                if "ToolSaveBits" in c:
                    ctl = ua.ControlFromHandle(h)
                    if ctl is not None:
                        out.append((h, ctl))
            return out

        def _search_doc():
            for h, ctl in _search_controls():
                try:
                    d = ctl.DocumentControl(searchDepth=12, Name="advancedSearch")
                    if d.Exists(0.2):
                        return ctl, d
                except Exception:
                    pass
            return None, None

        def _focus_search_input(popup_ctl, doc):
            # 1) 优先定位真实 EditControl
            try:
                ed = popup_ctl.EditControl(searchDepth=12)
                if ed.Exists(0.15):
                    er = ed.BoundingRectangle
                    if er.right - er.left > 40 and er.bottom - er.top > 12:
                        ua.Click(int((er.left + er.right) / 2), int((er.top + er.bottom) / 2), waitTime=0.15)
                        return
            except Exception:
                pass
            # 2) 找“综合”标签页位置，在其正上方点击输入区（兼容高分屏）
            doc_rect = doc.BoundingRectangle
            tab_rect = None
            try:
                def _find_tab(ctrl, depth=0):
                    nonlocal tab_rect
                    if tab_rect is not None or depth > 12:
                        return
                    for ch in ctrl.GetChildren():
                        try:
                            if ch.ControlTypeName == "TextControl" and (ch.Name or "").strip() == "综合":
                                tab_rect = ch.BoundingRectangle
                                return
                        except Exception:
                            pass
                        _find_tab(ch, depth + 1)
                _find_tab(doc, 0)
            except Exception:
                pass
            if tab_rect is not None and tab_rect.top - doc_rect.top > 30:
                fx = int(doc_rect.left + (doc_rect.right - doc_rect.left) * 0.35)
                fy = int(tab_rect.top - 28)
            else:
                fx = int(doc_rect.left + 160)
                fy = int(doc_rect.top + 32)
            ua.Click(fx, fy, waitTime=0.15)

        def _feature_item(doc, tgt_name, tgt_tokens):
            """解析搜索弹窗文档并精确返回命中的目标行控件矩形或控件。"""
            import unicodedata

            def norm(text):
                text = unicodedata.normalize("NFKC", text or "").casefold()
                return "".join(ch for ch in text if ch.isalnum())

            target = norm(tgt_name)
            norm_tokens = [norm(t) for t in tgt_tokens if norm(t)]
            found = []
            headings = []
            text_items = []

            def collect(ctrl, depth):
                if depth > 24:
                    return
                for k in ctrl.GetChildren():
                    try:
                        ct = k.ControlTypeName
                        if ct in ("TextControl", "ButtonControl", "ListItemControl", "HyperlinkControl"):
                            name = k.Name or ""
                            if name:
                                rect = k.BoundingRectangle
                                if rect.right > rect.left and rect.bottom > rect.top:
                                    text_items.append((k, name, rect))
                            normalized = norm(name)
                            if ct == "TextControl" and name in ("功能", "群组"):
                                headings.append((name, k.BoundingRectangle))
                            elif target and normalized.startswith(target):
                                found.append((k, k.BoundingRectangle))
                    except Exception:
                        pass
                    collect(k, depth + 1)

            documents = []
            for _h, ctl in _search_controls():
                try:
                    d = ctl.DocumentControl(searchDepth=12, Name="advancedSearch")
                    if d.Exists(0.05):
                        documents.append(d)
                except Exception:
                    pass
            if not documents:
                documents = [doc]
            for d in documents:
                collect(d, 0)

            # 按行聚合并拼接群名、emoji 与徽标文本
            if not found and (norm_tokens or len(target) >= 2):
                rows = []
                ordered = sorted(text_items,
                                 key=lambda x: ((x[2].top + x[2].bottom) / 2, x[2].left))
                for item in ordered:
                    r = item[2]
                    cy = (r.top + r.bottom) / 2
                    for row in rows:
                        if abs(cy - row[0]) <= 28:
                            row[1].append(item)
                            row[0] = (row[0] * (len(row[1]) - 1) + cy) / len(row[1])
                            break
                    else:
                        rows.append([cy, [item]])

                def _pick_row_target(row_items):
                    candidates = [
                        (k, r) for k, name, r in row_items
                        if norm(name) and not norm(name).isdigit()
                        and name not in ("功能", "群组", "内部群", "综合", "联系人", "聊天记录", "文档")
                    ]
                    if candidates:
                        return max(candidates, key=lambda x: x[1].right - x[1].left)
                    return None

                # 第一优先级：所有纯文字词段全命中
                for _cy, row_items in rows:
                    row_items.sort(key=lambda x: x[2].left)
                    combined = "".join(norm(name) for _k, name, _r in row_items)
                    if target and target in combined:
                        picked = _pick_row_target(row_items)
                        if picked:
                            found.append(picked)
                            break
                    if norm_tokens and all(tok in combined for tok in norm_tokens):
                        picked = _pick_row_target(row_items)
                        if picked:
                            found.append(picked)
                            break

                # 第二优先级：如果只有一个词段或钉钉截断群名，匹配核心词段
                if not found and norm_tokens:
                    main_tok = max(norm_tokens, key=len)
                    for _cy, row_items in rows:
                        row_items.sort(key=lambda x: x[2].left)
                        combined = "".join(norm(name) for _k, name, _r in row_items)
                        if len(main_tok) >= 2 and main_tok in combined:
                            picked = _pick_row_target(row_items)
                            if picked:
                                found.append(picked)
                                break

            if found:
                badged = [(k, r) for k, r in found
                          if any(label == "功能" and abs(h.top - r.top) < 34 and h.left >= r.left
                                 for label, h in headings)]
                if badged:
                    return min(badged, key=lambda x: x[1].top)
                below = [(k, r) for k, r in found
                         if any(0 < r.top - h.top < 280 and h.left <= r.left
                                for label, h in headings)]
                if below:
                    return min(below, key=lambda x: x[1].top)
                return min(found, key=lambda x: x[1].top)
            return None

        def _focus_chat_input(win, r):
            W = r.right - r.left
            H = r.bottom - r.top
            if win is not None:
                try:
                    target_rect = None

                    def _scan_input(ctrl, depth=0):
                        nonlocal target_rect
                        if target_rect is not None or depth > 12:
                            return
                        for ch in ctrl.GetChildren():
                            try:
                                ct = ch.ControlTypeName
                                rr = ch.BoundingRectangle
                                if (rr.right - rr.left > 120 and rr.bottom - rr.top > 28
                                        and rr.left >= r.left + W * 0.28
                                        and rr.top >= r.top + H * 0.62):
                                    nlow = (ch.Name or "").lower()
                                    if "搜索" not in nlow and "search" not in nlow:
                                        if ct in ("EditControl", "DocumentControl"):
                                            target_rect = rr
                                            return
                            except Exception:
                                pass
                            _scan_input(ch, depth + 1)

                    _scan_input(win, 0)
                    if target_rect is not None:
                        cx = int((target_rect.left + target_rect.right) / 2)
                        cy = int(min(target_rect.bottom - 18, max(target_rect.top + 18, (target_rect.top + target_rect.bottom) / 2)))
                        ua.Click(cx, cy, waitTime=0.4)
                        return
                except Exception:
                    pass
            in_x = int(r.left + W * 0.56)
            in_y = int(r.top + H * 0.80)
            ua.Click(in_x, in_y, waitTime=0.4)

        def _enable_dingtalk_silent_send(win, r):
            """确认未挂静音标签时，展开发送右侧下拉箭头并点击「设为静音」。"""
            W = r.right - r.left
            H = r.bottom - r.top
            # 1) 先检查输入框左下角是否已经挂有「静音发送」标签，避免重复切换
            if win is not None:
                try:
                    already = False

                    def _find_silent_badge(ctrl, depth=0):
                        nonlocal already
                        if already or depth > 16:
                            return
                        for ch in ctrl.GetChildren():
                            try:
                                rr = ch.BoundingRectangle
                                if rr.top >= r.top + H * 0.65 and "静音发送" in (ch.Name or ""):
                                    already = True
                                    return
                            except Exception:
                                pass
                            _find_silent_badge(ch, depth + 1)

                    _find_silent_badge(win, 0)
                    if already:
                        log("  当前已开启「静音发送」")
                        return True
                except Exception:
                    pass

            # 2) 定位右下角 [ 发送(S) | ▾ ] 组合按钮坐标（像素扫描优先，UIA / 比例兜底）
            pill_box = None
            try:
                from PIL import ImageGrab
                bx0 = max(0, int(r.right - W * 0.16))
                by0 = max(0, int(r.bottom - H * 0.12))
                bx1 = max(bx0 + 20, int(r.right - 6))
                by1 = max(by0 + 20, int(r.bottom - 10))
                shot = ImageGrab.grab(bbox=(bx0, by0, bx1, by1)).convert("RGB")
                pix = shot.load()
                sw, sh = shot.size
                xs, ys = [], []
                for y in range(sh):
                    for x in range(sw):
                        rc, gc, bc = pix[x, y]
                        is_green = gc > 85 and gc > rc + 18 and gc > bc + 8
                        is_blue = bc > 140 and bc > rc + 35 and bc > gc + 15
                        if is_green or is_blue:
                            xs.append(x)
                            ys.append(y)
                if xs and (max(xs) - min(xs) >= 36) and (max(ys) - min(ys) >= 12):
                    pill_box = (bx0 + min(xs), by0 + min(ys), bx0 + max(xs), by0 + max(ys))
            except Exception:
                pill_box = None

            if pill_box is None and win is not None:
                try:
                    found_btn = None

                    def _find_send_btn(ctrl, depth=0):
                        nonlocal found_btn
                        if found_btn is not None or depth > 16:
                            return
                        for ch in ctrl.GetChildren():
                            try:
                                rr = ch.BoundingRectangle
                                if (rr.left >= r.left + W * 0.78 and rr.top >= r.top + H * 0.85
                                        and rr.right - rr.left >= 36 and rr.bottom - rr.top >= 12
                                        and "发送" in (ch.Name or "")):
                                    found_btn = (rr.left, rr.top, rr.right, rr.bottom)
                                    return
                            except Exception:
                                pass
                            _find_send_btn(ch, depth + 1)

                    _find_send_btn(win, 0)
                    if found_btn is not None:
                        pill_box = found_btn
                except Exception:
                    pass

            if pill_box is None:
                pill_box = (
                    int(r.right - W * 0.070),
                    int(r.bottom - H * 0.063),
                    int(r.right - W * 0.018),
                    int(r.bottom - H * 0.036),
                )

            pill_left, pill_top, pill_right, pill_bottom = pill_box
            pill_w = max(40, pill_right - pill_left)
            pill_h = max(14, pill_bottom - pill_top)
            arrow_x = int(pill_right - max(7, pill_w * 0.14))
            arrow_y = int((pill_top + pill_bottom) / 2)

            ua.Click(arrow_x, arrow_y, waitTime=0.35)
            time.sleep(0.45)

            # 3) 优先从 UIA 顶层窗口及主窗子树查找「设为静音」/「取消静音」菜单项
            menu_action = None
            menu_rect = None
            try:
                roots = []
                for h, _c, _t in _enum_top_windows():
                    ctl = ua.ControlFromHandle(h)
                    if ctl is not None:
                        roots.append(ctl)
                if win is not None:
                    roots.append(win)

                def _scan_menu(ctrl, depth=0):
                    nonlocal menu_action, menu_rect
                    if menu_action is not None or depth > 14:
                        return
                    for ch in ctrl.GetChildren():
                        try:
                            nm = (ch.Name or "").strip()
                            if "取消静音" in nm:
                                menu_action = "already"
                                return
                            if "设为静音" in nm:
                                rr = ch.BoundingRectangle
                                if rr.right > rr.left and rr.bottom > rr.top:
                                    menu_action = "click"
                                    menu_rect = rr
                                    return
                        except Exception:
                            pass
                        _scan_menu(ch, depth + 1)

                for root_ctl in roots:
                    _scan_menu(root_ctl, 0)
                    if menu_action is not None:
                        break
            except Exception:
                pass

            if menu_action == "already":
                try:
                    ua.SendKeys("{Esc}", waitTime=0.2)
                except Exception:
                    pass
                log("  当前已开启「静音发送」")
                return True
            if menu_action == "click" and menu_rect is not None:
                ua.Click(int((menu_rect.left + menu_rect.right) / 2),
                         int((menu_rect.top + menu_rect.bottom) / 2), waitTime=0.35)
                log("  已选择「静音发送」")
                return True

            # 4) 自绘菜单未暴露 UIA 时按实测几何位置点击「设为静音」（位于按钮正上方第 2 项）
            menu_x = int(pill_right - pill_w * 0.65)
            menu_y = int(pill_top - pill_h * 1.9)
            ua.Click(menu_x, menu_y, waitTime=0.35)
            log("  已选择「静音发送」")
            return True

        def _is_focus_in_search_area(r):
            if _is_search_popup_open():
                return True, "全局搜索弹窗仍处于打开状态"
            try:
                fc = ua.GetFocusedControl()
                if fc is not None:
                    fname = (fc.Name or "").lower()
                    if "搜索" in fname or "search" in fname:
                        return True, f"焦点控件为「{fc.Name}」"
                    fr = fc.BoundingRectangle
                    H = max(1, r.bottom - r.top)
                    if (fr.bottom > fr.top and fr.right > fr.left
                            and fr.bottom < r.top + H * 0.35):
                        return True, "焦点位于钉钉窗口顶部搜索栏区域"
            except Exception:
                pass
            return False, ""

        def _ensure_not_in_search_box(r):
            bad, reason = _is_focus_in_search_area(r)
            if not bad:
                return
            try:
                ua.SendKeys("{Esc}", waitTime=0.25)
            except Exception:
                pass
            time.sleep(0.3)
            win_now = _fresh_ding()
            r_now = win_now.BoundingRectangle if win_now else r
            _focus_chat_input(win_now, r_now)
            time.sleep(0.35)
            bad2, reason2 = _is_focus_in_search_area(r_now)
            if bad2:
                raise RuntimeError(f"检测到焦点仍在钉钉搜索框（{reason2}），已安全拦截粘贴")

        paste_delay_sec = _clamp_int((cfg or {}).get("dt_paste_delay_sec"), 2, 0, 15)
        send_wait_sec = _clamp_int((cfg or {}).get("dt_send_wait_sec"), 5, 0, 20)

        def _send_one_target(idx, total, search_name):
            search_candidates, search_tokens = _extract_search_keywords(search_name)
            if total > 1:
                log(f"  → 目标 ({idx}/{total})：正在打开钉钉搜索「{search_name}」…")
            else:
                log(f"  正在打开钉钉，搜索「{search_name}」…")
            _force_front()
            sleep_check(0.55)
            chat_ready = False
            for _attempt in range(3):
                check_stop()
                if _link_box() is not None:
                    _close_link_box()
                _open_global_search()
                popup_ctl, doc = None, None
                for _ in range(20):
                    check_stop()
                    popup_ctl, doc = _search_doc()
                    if doc is not None:
                        break
                    time.sleep(0.45)
                if doc is None:
                    log("  搜索窗口没打开，正在重试…")
                    continue

                matched = None
                for kw_idx, kw in enumerate(search_candidates):
                    check_stop()
                    _focus_search_input(popup_ctl, doc)
                    time.sleep(0.18)
                    ua.SendKeys("{Ctrl}a", waitTime=0.1)
                    ua.SendKeys("{BackSpace}", waitTime=0.1)
                    time.sleep(0.1)
                    # 纯 BMP 字符走 SendInput；若末尾兜底词含 4 字节 Emoji 代理对则安全走剪贴板粘贴（此时无图片）
                    if any(ord(ch) > 0xFFFF for ch in kw):
                        _paste_text(kw, verify=False)
                    else:
                        _type_unicode(kw)

                    wait_loops = 12 if kw_idx < len(search_candidates) - 1 else 16
                    for _ in range(wait_loops):
                        check_stop()
                        matched = _feature_item(doc, search_name, search_tokens)
                        if matched is not None:
                            break
                        time.sleep(0.45)
                    if matched is not None:
                        break
                    log(f"  关键词「{kw}」未匹配，切换下一关键词重试…")

                if matched is None:
                    log("  没搜到目标，关闭搜索框后重试…")
                    ua.SendKeys("{Esc}", waitTime=0.25)
                    time.sleep(0.6)
                    continue

                item_ctl, item_rect = matched
                cx = int((item_rect.left + item_rect.right) / 2)
                cy = int((item_rect.top + item_rect.bottom) / 2)
                ua.Click(cx, cy, waitTime=0.3)

                # 严禁仅用 _search_doc() is None 判断！必须确认 ToolSaveBits 搜索窗完全关闭
                entered = False
                for _ in range(18):
                    check_stop()
                    time.sleep(0.35)
                    if not _is_search_popup_open():
                        time.sleep(0.2)
                        if not _is_search_popup_open():
                            entered = True
                            break
                if entered:
                    chat_ready = True
                    break
                log("  搜索弹窗尚未退出，关闭后重试…")
                ua.SendKeys("{Esc}", waitTime=0.25)
                time.sleep(0.5)

            if not chat_ready:
                raise RuntimeError("没能定位到钉钉会话，已停止发送")
            log("  已打开目标会话，准备粘贴图片")
            if paste_delay_sec > 0:
                log(f"  ⏳ 等待 {paste_delay_sec} 秒让钉钉会话就绪后再粘贴…")
                sleep_check(float(paste_delay_sec))

            # 4) 聚焦底部聊天输入框并先清空残留内容，再把图片写入剪贴板并粘贴
            ding_now = _fresh_ding()
            if ding_now is None:
                raise RuntimeError("找不到钉钉窗口，已停止粘贴")
            r = ding_now.BoundingRectangle
            _focus_chat_input(ding_now, r)
            time.sleep(0.35)
            _ensure_not_in_search_box(r)

            if cfg.get("dt_clear_before_send", True):
                ua.SendKeys("{Ctrl}a", waitTime=0.15)
                ua.SendKeys("{BackSpace}", waitTime=0.15)
                ua.SendKeys("{Delete}", waitTime=0.15)
                ua.SendKeys("{Ctrl}a", waitTime=0.1)
                ua.SendKeys("{BackSpace}", waitTime=0.2)
                log("  已清空发送框内原有内容")

            if not set_clipboard_image_from_file(tmp_img):
                raise RuntimeError("图片恢复失败，已停止粘贴")

            # 5) 粘贴图片前硬核查焦点，绝不把图片贴入顶部搜索框
            _ensure_not_in_search_box(r)
            ua.SendKeys("{Ctrl}v", waitTime=0.5)

            # 6) 弹窗兜底：检查“选择粘贴的形式”，出现就点「转为图片」
            converted = False
            for _ in range(12):
                time.sleep(0.25)
                tops = _enum_top_windows(
                    target_classes=("ddesign::ComplexModal",), target_titles=None)
                if tops:
                    modal = ua.ControlFromHandle(tops[0][0])
                    if modal is not None:
                        btn = modal.ButtonControl(searchDepth=12, Name="转为图片")
                        if btn.Exists(2):
                            btn.Click()
                            converted = True
                            time.sleep(0.6)
                    break
            if converted:
                log("  已自动点「转为图片」")
            else:
                log("  图片已直接粘贴（无需转换）")

            # 7) 粘贴文字前再次确认焦点在底部编辑区，最后再按设置决定静音与是否发出
            ding_now2 = _fresh_ding()
            if ding_now2 is None:
                raise RuntimeError("钉钉窗口异常，消息尚未发送，请检查后手动发送")
            r2 = ding_now2.BoundingRectangle
            _focus_chat_input(ding_now2, r2)
            time.sleep(0.25)
            ua.SendKeys("{End}", waitTime=0.15)
            _ensure_not_in_search_box(r2)
            if not _paste_text(text_msg, verify=True):
                raise RuntimeError("钉钉文字未就绪，已停止自动发送，请检查后手动按 Enter")
            time.sleep(0.5)

            if cfg.get("dt_silent_send", True):
                ding_now3 = _fresh_ding()
                if ding_now3 is not None:
                    _enable_dingtalk_silent_send(ding_now3, ding_now3.BoundingRectangle)
                    time.sleep(0.35)

            if cfg.get("dt_auto_send", False):
                if send_wait_sec > 0:
                    log(f"  ⏳ 图文已就绪，等待 {send_wait_sec} 秒后自动按 Enter 发送…")
                    sleep_check(float(send_wait_sec))
                ding_now4 = _fresh_ding()
                if ding_now4 is not None:
                    r4 = ding_now4.BoundingRectangle
                    _focus_chat_input(ding_now4, r4)
                    sleep_check(0.2)
                    _ensure_not_in_search_box(r4)
                ua.SendKeys("{Enter}", waitTime=0.4)
                silent_word = "静音" if cfg.get("dt_silent_send", True) else "常规"
                log(f"  ✅ 已{silent_word}发送到「{search_name}」：{text_msg}")
                sleep_check(0.45)
            else:
                silent_tip = "并设为「静音发送」" if cfg.get("dt_silent_send", True) else ""
                clear_tip = "已清空旧草稿、" if cfg.get("dt_clear_before_send", True) else ""
                log(f"  ✅ {clear_tip}贴好图片与「{text_msg}」{silent_tip}，按设置暂不发送，请检查后手动发送")
            return True

        for _t_idx, _t_name in enumerate(target_list, 1):
            check_stop()
            clear_clipboard_windows()
            _send_one_target(_t_idx, len(target_list), _t_name)
        return True
    except Exception as e:
        log(f"  钉钉操作失败：{human_error(e)}")
        raise
    finally:
        try:
            if tmp_img.exists():
                tmp_img.unlink()
            send_tmp = tmp_img.with_name(tmp_img.stem + "_send.png")
            if send_tmp.exists():
                send_tmp.unlink()
        except Exception:
            pass


def run_once(round_num, cfg=None):
    reset_stop()
    start_time = time.time()
    start_str = time.strftime("%Y-%m-%d %H:%M:%S")
    if cfg is None:
        cfg = load_config()
    cfg = normalize_config(cfg)
    luya_user = (cfg.get("luya_user") or LUYA_USER).strip()
    luya_pass = cfg.get("luya_pass") or LUYA_PASS
    wps_url = (cfg.get("wps_url") or WPS_URL).strip()
    if not wps_url:
        raise RuntimeError("请先在「高级设置 -> 数据源与后台账号」填写 WPS 在线表格链接后再运行")
    if not luya_user or not luya_pass:
        raise RuntimeError("请先在「高级设置 -> 数据源与后台账号」填写 Luya 后台账号与密码后再运行")
    failures = []
    if cfg.get("prevent_sleep", True):
        set_prevent_sleep(True)

    log("=" * 40)
    log(f"📊 开始第 {round_num} 轮，时间 {start_str}")
    log("=" * 40)

    temp_dir = Path.home() / "Desktop" / "数据日报"
    temp_dir.mkdir(exist_ok=True)
    BROWSER_USER_DATA.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        check_stop()
        set_step("正在打开浏览器…")
        log("正在打开浏览器...")
        context = _launch_context(p)

        try:
            cleanup_wps_tabs(context, wps_url=wps_url)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)

            check_stop()
            set_step("正在登录后台…")
            log("1/5 打开后台并登录...")
            goto_with_retry(page, "https://bb.luyaapp.live")
            sleep_check(2)

            need_login = False
            try:
                page.wait_for_selector("input[type='password']", timeout=3000)
                need_login = True
            except Exception:
                pass

            if need_login:
                check_stop()
                log("  检测到登录页，自动填写账号密码...")
                try:
                    pwd_sel = "input[type='password']"
                    page.wait_for_selector(pwd_sel, timeout=5000)
                    user_filled = False
                    try:
                        page.fill(f"{pwd_sel} >> xpath=preceding-sibling::input[1]", luya_user, timeout=2000)
                        user_filled = True
                    except Exception:
                        pass
                    if not user_filled:
                        for sel in ["input[type='email']", "input[type='text']:visible",
                                    "input[name='email']", "input[placeholder*='邮箱']",
                                    "input[placeholder*='账号']"]:
                            try:
                                page.fill(sel, luya_user, timeout=1000)
                                user_filled = True
                                break
                            except Exception:
                                continue
                    if not user_filled:
                        inputs = page.query_selector_all("input:visible")
                        for inp in inputs:
                            inp_type = inp.get_attribute("type") or "text"
                            if inp_type != "password":
                                inp.fill(luya_user)
                                user_filled = True
                                break
                    page.fill(pwd_sel, luya_pass, timeout=2000)
                    btn_clicked = False
                    for sel in ["button[type='submit']", "button:has-text('登录')", "input[type='submit']"]:
                        try:
                            page.click(sel, timeout=1500)
                            btn_clicked = True
                            break
                        except Exception:
                            continue
                    if not btn_clicked:
                        page.keyboard.press("Enter")
                    sleep_check(3)
                    log("  登录完成")
                except Exception as e:
                    log(f"  自动登录没成功：{human_error(e)}")
            else:
                log("  已经登录过，跳过")

            check_stop()
            set_step("正在下载三份数据…")
            log("2/5 下载三份数据...")
            xlsx_files = []
            for name, url in DATA_URLS:
                check_stop()
                dl_file = download_xlsx(page, name, url, temp_dir)
                if dl_file:
                    xlsx_files.append((name, dl_file))
                sleep_check(0.3)

            missing = [name for name, _ in DATA_URLS if name not in {n for n, _ in xlsx_files}]
            if missing:
                raise RuntimeError("这些数据没有下载成功：" + "、".join(missing))

            check_stop()
            set_step("正在整理数据格式…")
            log("3/5 整理数据格式...")
            tsv_files = []
            for name, xlsx_path in xlsx_files:
                check_stop()
                tsv_path = temp_dir / f"{name}.tsv"
                xlsx_to_tsv(xlsx_path, tsv_path)
                tsv_files.append((name, tsv_path))

            check_stop()
            set_step("正在写入在线表格…")
            log("4/5 写入在线表格...")
            cleanup_wps_tabs(context, wps_url=wps_url)
            goto_with_retry(page, wps_url)
            page.wait_for_selector("input.edit-box", timeout=20000)
            sleep_check(1)

            if check_wps_logged_in(page):
                log("  表格已登录，直接写入")
            else:
                if not wait_wps_login(page, max_wait=1200):
                    raise RuntimeError("WPS 登录超时：请在浏览器里扫码登录后再跑一次")

            sleep_check(1)
            log("  表格加载完成")

            for name, tsv_path in tsv_files:
                check_stop()
                paste_to_wps(page, name, tsv_path)
                sleep_check(0.5)

            check_stop()
            set_step("正在切换到数据汇总表…")
            log("5/5 切到数据汇总表...")
            if not switch_wps_sheet(page, "数据汇总"):
                raise Exception("切换到数据汇总表失败，请检查底部标签栏")
            set_step("正在等待汇总表刷新…")
            log("  等待 5 秒，让数据汇总表完成刷新…")
            sleep_check(5.0)

            apply_mini_summary_cells(page, cfg, pasted_raw=True)
            if cfg.get("write_d3_date") or cfg.get("write_r4_time", True):
                sleep_check(2.0)

            screenshot_path = temp_dir / "数据汇总.png"
            page.screenshot(path=str(screenshot_path))
            log("✅ 数据汇总表已准备好")

            # 按配置：在汇总表选区复制为图片（图片进剪贴板）
            capture_failed = False
            if cfg.get("enable_capture"):
                try:
                    check_stop()
                    set_step("正在复制表格图片…")
                    capture_wps_region(page, cfg=cfg)
                except Exception as e:
                    if "手动停止" in str(e):
                        raise
                    capture_failed = True
                    tip = human_error(e)
                    failures.append("复制表格图片失败：" + tip)
                    log(f"  复制图片没成功：{tip}")
                    log_error(e, "复制表格图片失败")

            cleanup_wps_tabs(context, wps_url=wps_url)
        finally:
            set_step("正在关闭后台浏览器…")
            log("关闭后台浏览器...")
            try:
                context.close()
            except Exception:
                pass

    # 复制失败时禁止发送旧剪贴板内容。
    if cfg.get("enable_dingtalk") and not capture_failed:
        try:
            check_stop()
            set_step("正在发送到钉钉…")
            send_dingtalk_image(cfg)
        except Exception as e:
            if "手动停止" in str(e):
                raise
            tip = human_error(e)
            failures.append("钉钉发送失败：" + tip)
            log(f"  钉钉发送没成功：{tip}")
            log_error(e, "钉钉发送失败")

    if failures:
        raise RuntimeError("；".join(failures))

    notify_last_run()
    end_time = time.time()
    duration = int(end_time - start_time)
    set_step("✅ 本轮跑完")
    log(f"✅ 本轮全部完成，用时 {duration} 秒")

    if not cfg.get("open_wps_after_run", False):
        return end_time
    log("正在打开数据汇总表给你看...")
    wps_paths = [
        r"C:\Program Files\Kingsoft\WPS Office\office6\wps.exe",
        r"C:\Program Files (x86)\Kingsoft\WPS Office\office6\wps.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Kingsoft\WPS Office\office6\wps.exe"),
    ]
    wps_found = False
    wps_path = None
    for wp in wps_paths:
        if os.path.exists(wp):
            wps_found = True
            wps_path = wp
            break
    if wps_found:
        log("检测到WPS，用WPS打开")
        try:
            subprocess.Popen([wps_path, wps_url])
        except Exception:
            import webbrowser
            webbrowser.open(wps_url)
    else:
        log("没装WPS，用浏览器打开")
        import webbrowser
        webbrowser.open(wps_url)

    return end_time


def run_capture_only(round_num="截图", cfg=None):
    """独立截图发送：不下载、不写入，只处理 WPS 汇总表和钉钉。"""
    reset_stop()
    start_time = time.time()
    if cfg is None:
        cfg = load_config()
    cfg = normalize_config(cfg)
    wps_url = (cfg.get("wps_url") or WPS_URL).strip()
    if not wps_url:
        raise RuntimeError("请先在「高级设置 -> 数据源与后台账号」填写 WPS 在线表格链接后再运行")
    if cfg.get("prevent_sleep", True):
        set_prevent_sleep(True)
    log("=" * 40)
    log(f"📸 开始独立截图发送，时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")
    BROWSER_USER_DATA.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        check_stop()
        set_step("正在打开WPS汇总表…")
        log("1/3 正在打开 WPS 数据汇总表…")
        context = _launch_context(p)
        try:
            cleanup_wps_tabs(context, wps_url=wps_url)
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(30000)
            goto_with_retry(page, wps_url)
            page.wait_for_selector("input.edit-box", timeout=20000)
            if check_wps_logged_in(page):
                log("  表格已登录，直接打开汇总表")
            elif not wait_wps_login(page, max_wait=1200):
                raise RuntimeError("WPS 登录超时，请扫码后再试")

            check_stop()
            if not switch_wps_sheet(page, "数据汇总"):
                raise RuntimeError("没找到【数据汇总】工作表")
            apply_mini_summary_cells(page, cfg, pasted_raw=False)
            set_step("正在等待汇总表刷新…")
            log("  等待 5 秒，让数据汇总表完成刷新…")
            sleep_check(5.0)
            check_stop()
            log("2/3 正在框选汇总表并复制为图片…")
            set_step("正在复制表格图片…")
            capture_wps_region(page, cfg=cfg)
        finally:
            try:
                context.close()
            except Exception:
                pass

    check_stop()
    log("3/3 正在粘贴到钉钉并发送…")
    set_step("正在发送到钉钉…")
    send_dingtalk_image(cfg)
    notify_last_run()
    end_time = time.time()
    set_step("✅ 本轮跑完")
    log(f"✅ 独立截图流程完成，用时 {int(end_time - start_time)} 秒")
    return end_time


def show_splash():
    """启动过渡窗：新拟态软质感 + 旋转加载，自检完成、走满后再进主界面。"""
    import threading
    import tkinter as tk
    import neu_ui as neu
    SPLASH_BASE = "#e3e8ef"
    win = tk.Tk()
    win.title(f"📊{APP_NAME}")
    win.geometry("420x288")
    win.resizable(False, False)
    try:
        win.eval("tk::PlaceWindow . center")
    except Exception:
        pass
    win.configure(bg=SPLASH_BASE)
    ctx = neu.NeuContext(SPLASH_BASE)

    icon = neu.NeuCard(win, ctx, 74, 74, radius=37, pad=12)
    icon.place(relx=0.5, y=28, anchor="n")
    tk.Label(icon.inner, text="📊", bg=SPLASH_BASE,
             font=("Microsoft YaHei UI", 32)).place(relx=0.5, rely=0.5, anchor="center")

    tk.Label(win, text=APP_NAME, bg=SPLASH_BASE, fg="#2d3138",
             font=("Microsoft YaHei UI", 20, "bold")).place(
        relx=0.5, y=140, anchor="n")

    neu.RingSpinner(win, ctx, size=42, color="#007aff").place(
        relx=0.5, y=186, anchor="n")

    tip = tk.Label(win, text="正在检查浏览器组件，请稍候…", bg=SPLASH_BASE,
                   fg="#7a8190", font=("Microsoft YaHei UI", 12))
    tip.place(relx=0.5, y=246, anchor="n")

    done = threading.Event()

    def _ensure():
        try:
            ensure_browser_installed()
        except Exception:
            pass
        done.set()

    def _watch():
        if done.is_set():
            tip.configure(text="准备完成，正在打开…")
            win.after(350, win.destroy)
            return
        win.after(150, _watch)

    threading.Thread(target=_ensure, daemon=True).start()
    win.after(200, _watch)
    win.after(60000, win.destroy)  # 兜底：自检再慢也进入主界面
    win.mainloop()


def _show_settings_window_legacy():
    import threading
    import tkinter as tk
    import neu_ui as neu

    root = tk.Tk()
    root.title(f"📊{APP_NAME}")
    root.geometry("720x860")
    root.minsize(720, 760)
    root.resizable(True, True)
    try:
        root.eval("tk::PlaceWindow . center")
    except Exception:
        pass

    # ===== 现代卡片主题：浅色窗口 + 白色内容卡 + 高对比文字 =====
    THEMES = {
        "light": {
            "label": "浅色", "base": "#ffffff", "window": "#f3f6fb",
            "text_main": "#172033", "text_sub": "#8490a5",
        },
        "dark": {
            "label": "深色", "base": "#252932", "window": "#171a21",
            "text_main": "#f1f4fa", "text_sub": "#929caf",
        },
        "warm": {
            "label": "暖色", "base": "#fffaf5", "window": "#f4ede6",
            "text_main": "#423326", "text_sub": "#a08a75",
        },
    }
    BLUE, ORANGE, GREEN, RED = "#2f6fed", "#ff8a34", "#24b47e", "#ef4b55"

    cur_theme = "light"
    TH = THEMES[cur_theme]
    ctx = neu.NeuContext(TH["base"], TH["window"])
    # 普通 tk 标签登记（主题切换时改色）
    lab_main, lab_sub = [], []

    root.configure(bg=TH["window"])
    cfg = load_config()

    def apply_theme(name):
        nonlocal TH, cur_theme
        old = TH
        cur_theme = name
        TH = THEMES[name]
        ctx.set_base(TH["base"])
        ctx.window_hex = TH["window"]
        root.configure(bg=TH["window"])
        ctx.redraw_all()
        accents = {BLUE.lower(), ORANGE.lower(), GREEN.lower(), RED.lower()}

        def _walk(w):
            # 内容卡与窗口背景分别映射，保留白色卡片层次。
            try:
                bg = w.cget("bg").lower()
                if bg == old["base"].lower():
                    w.configure(bg=TH["base"])
                elif bg == old["window"].lower():
                    w.configure(bg=TH["window"])
            except Exception:
                pass
            # 前景：强调色保持，主/次文字色映射到新主题
            try:
                fgl = w.cget("fg").lower()
                if fgl in accents:
                    pass
                elif fgl == old["text_main"].lower():
                    w.configure(fg=TH["text_main"])
                elif fgl == old["text_sub"].lower():
                    w.configure(fg=TH["text_sub"])
            except Exception:
                pass
            for c in w.winfo_children():
                _walk(c)
        _walk(root)
        for b in theme_btns.values():
            b.configure(bg=TH["window"])

    def _on_close():
        try:
            collect_and_save()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        os._exit(0)
    root.protocol("WM_DELETE_WINDOW", _on_close)

    schedule_running = False
    manual_trigger = False
    stop_flag = False
    capture_running = False

    # ===== 标题区 =====
    head_icon = neu.NeuCard(root, ctx, 52, 52, radius=18, pad=12, outer_bg=True)
    head_icon.place(x=38, y=24)
    tk.Label(head_icon.inner, text="📊", bg=TH["base"],
             font=("Microsoft YaHei UI", 26)).place(relx=0.5, rely=0.5, anchor="center")

    title_lab = tk.Label(root, text=APP_NAME, bg=TH["window"],
                         fg=TH["text_main"], font=("Microsoft YaHei UI", 24, "bold"))
    title_lab.place(x=112, y=30)
    lab_main.append(title_lab)

    # 主题切换（小圆点按钮）
    theme_btns = {}
    tx = 720 - 40

    def _pick_theme(name):
        apply_theme(name)
        for k, b in theme_btns.items():
            if k == name:
                b.configure(text=THEMES[k]["label"], fg=BLUE)
            else:
                b.configure(text=THEMES[k]["label"], fg=TH["text_sub"])

    for tname, t in THEMES.items():
        b = tk.Label(root, text=t["label"], bg=TH["window"], fg=TH["text_sub"],
                     font=("Microsoft YaHei UI", 12), cursor="hand2")
        b.place(x=tx - 60, y=38, anchor="ne")
        b.bind("<Button-1>", lambda e, n=tname: _pick_theme(n))
        theme_btns[tname] = b
        tx -= 64
    _pick_theme("light")

    sub = tk.Label(root, text="自动下载 · 自动填表 · 汇总图片一键送达",
                   bg=TH["window"], fg=TH["text_sub"],
                   font=("Microsoft YaHei UI", 13))
    sub.place(x=40, y=86)
    lab_sub.append(sub)
    ver = tk.Label(root, text=f"v{APP_VERSION}", bg=TH["window"], fg=TH["text_sub"],
                   font=("Microsoft YaHei UI", 11))
    ver.place(x=682, y=88, anchor="ne")
    lab_sub.append(ver)

    # ===== 固定操作按钮 =====
    btn_card = neu.NeuCard(root, ctx, 640, 92, radius=22, pad=14, outer_bg=True)
    btn_card.place(x=40, y=112)
    btn_now = neu.NeuButton(btn_card.inner, ctx, 180, 56, "立即跑一次",
                            radius=14, accent=BLUE, emoji="▶", font_size=14, filled=True)
    btn_now.place(x=10, y=18)
    btn_capture = neu.NeuButton(btn_card.inner, ctx, 180, 56, "仅截图发送",
                                radius=14, accent=ORANGE, emoji="▣", font_size=14, filled=True)
    btn_capture.place(x=218, y=18)
    btn_schedule = neu.NeuButton(btn_card.inner, ctx, 180, 56, "开始计划任务",
                                 radius=14, accent=GREEN, emoji="◷", font_size=14, filled=True)
    btn_schedule.place(x=426, y=18)

    # ===== 状态卡 =====
    status_card = neu.NeuCard(root, ctx, 640, 132, radius=22, pad=14, outer_bg=True)
    status_card.place(x=40, y=232)
    tk.Label(status_card.inner, text="📡 运行状态", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=10)

    run_spinner = neu.RingSpinner(status_card.inner, ctx, 44, color=ORANGE)
    run_spinner.place(x=20, y=48)
    # 空闲时隐藏 spinner，用静态圆点替代
    status_dot = tk.Label(status_card.inner, text="●", bg=TH["base"], fg=GREEN,
                          font=("Microsoft YaHei UI", 16))
    run_spinner.place_forget()
    status_dot.place(x=38, y=60)

    step_anim = neu.AnimatedText(status_card.inner, ctx, "✅ 空闲中，等待开始",
                                 color=GREEN, font_size=16, w=520, h=30, anchor="w")
    step_anim.place(x=80, y=46)
    last_anim = neu.AnimatedText(status_card.inner, ctx, "上次抓取：—",
                                 color=TH["text_sub"], font_size=12, bold=False,
                                 w=320, h=24, anchor="w")
    last_anim.place(x=80, y=88)
    next_anim = neu.AnimatedText(status_card.inner, ctx, "下次抓取：—",
                                 color=BLUE, font_size=12, bold=False,
                                 w=300, h=24, anchor="w")
    next_anim.place(x=320, y=88)
    err_lab = tk.Label(status_card.inner, text="", bg=TH["base"], fg=RED,
                       font=("Microsoft YaHei UI", 12, "bold"), anchor="w",
                       justify="left", wraplength=600)

    def _step_color(text):
        if text.startswith("✅"):
            return GREEN
        if text.startswith("❌") or text.startswith("⚠"):
            return RED
        if text.startswith("⏹"):
            return TH["text_sub"]
        return ORANGE

    # 桌面毛玻璃进度浮窗
    glass = neu.GlassProgress(root)
    STEP_PROGRESS = {
        "正在打开浏览器…": 0.06,
        "正在打开WPS汇总表…": 0.18,
        "正在登录后台…": 0.16,
        "正在下载三份数据…": 0.30,
        "正在整理数据格式…": 0.43,
        "正在写入在线表格…": 0.58,
        "正在切换到数据汇总表…": 0.72,
        "正在等待汇总表刷新…": 0.76,
        "正在复制表格图片…": 0.82,
        "正在关闭后台浏览器…": 0.88,
        "正在发送到钉钉…": 0.95,
        "✅ 本轮跑完": 1.0,
    }

    def _on_step(text):
        def _upd():
            step_anim.set(text, color=_step_color(text))
            prog = STEP_PROGRESS.get(text)
            if prog is not None and not glass.closed:
                glass.update_step(text, prog)
        root.after(0, _upd)

    def _on_last_run():
        def _upd():
            last_anim.set("上次抓取：" + time.strftime("%Y-%m-%d %H:%M"),
                          color=TH["text_sub"])
        root.after(0, _upd)

    set_step_callback(_on_step)
    set_last_run_callback(_on_last_run)

    # ===== 滚动区（纯 tk Canvas + 平滑缓动） =====
    scroll_cv = tk.Canvas(root, bg=TH["window"], highlightthickness=0, bd=0)
    scroll_cv.place(x=0, y=380, relwidth=1, relheight=1, height=-380)
    content = tk.Frame(scroll_cv, bg=TH["window"])
    scroll_cv.create_window(0, 0, anchor="nw", window=content)
    content.bind("<Configure>",
                 lambda e: scroll_cv.configure(scrollregion=scroll_cv.bbox("all")))

    smooth = {"target": None, "anim": None}

    def _frac():
        return scroll_cv.yview()[0]

    def _step_to_target():
        if smooth["target"] is None:
            smooth["anim"] = None
            return
        cur = _frac()
        tgt = smooth["target"]
        if abs(tgt - cur) < 0.002:
            scroll_cv.yview_moveto(tgt)
            smooth["target"] = smooth["anim"] = None
            return
        scroll_cv.yview_moveto(cur + (tgt - cur) * 0.22)
        smooth["anim"] = root.after(16, _step_to_target)

    def _on_wheel(e):
        try:
            bbox = scroll_cv.bbox("all")
            vis = scroll_cv.winfo_height()
            if bbox and vis > 0 and bbox[3] > vis:
                tick = max(0.04, (vis / bbox[3]) * 0.9)
            else:
                tick = 0.08
            direction = -1 if e.delta > 0 else 1
            smooth["target"] = max(0.0, min(1.0, _frac() + direction * tick))
            if smooth["anim"] is None:
                smooth["anim"] = root.after(16, _step_to_target)
        except Exception:
            pass
    root.bind_all("<MouseWheel>", _on_wheel)

    # ===== 卡片1：运行日期 =====
    c1 = neu.NeuCard(content, ctx, 640, 118, radius=22, pad=14, outer_bg=True)
    c1.pack(pady=(6, 14), padx=40)
    tk.Label(c1.inner, text="📅 运行日期", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)
    day_mode_var = tk.StringVar(value=cfg.get("day_mode", "daily"))
    seg = neu.NeuSegment(c1.inner, ctx,
                         [("每天都跑", "daily"), ("仅工作日", "workday")],
                         day_mode_var.get(), lambda v: day_mode_var.set(v), h=46)
    seg.place(x=18, y=50)

    # ===== 时段构建（步进器） =====
    def build_slot(parent, defaults, accent):
        rows = tk.Frame(parent, bg=TH["base"])
        rows.pack(fill="x", padx=18, pady=(46, 4))

        def make_row(label, lo, hi, d1, d2):
            r = tk.Frame(rows, bg=TH["base"])
            r.pack(fill="x", pady=4)
            tk.Label(r, text=label, bg=TH["base"], fg=TH["text_sub"], width=4, anchor="w",
                     font=("Microsoft YaHei UI", 13)).pack(side="left")
            s1 = neu.NeuStepper(r, ctx, lo[0], hi[0], d1, h=36)
            s1.pack(side="left", padx=(6, 2))
            tk.Label(r, text=":", bg=TH["base"], fg=TH["text_main"],
                     font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
            s2 = neu.NeuStepper(r, ctx, lo[1], hi[1], d2, h=36)
            s2.pack(side="left", padx=(2, 0))
            return s1, s2

        sh, sm = make_row("开始", (0, 0), (23, 59), defaults["sh"], defaults["sm"])
        eh, em = make_row("结束", (0, 0), (23, 59), defaults["eh"], defaults["em"])
        ih, im = make_row("每隔", (0, 0), (12, 59), defaults["ih"], defaults["im"])
        widgets = dict(sh=sh, sm=sm, eh=eh, em=em, ih=ih, im=im)
        preview = tk.Label(parent, text="", bg=TH["base"], fg=accent, anchor="w",
                           font=("Microsoft YaHei UI", 12))
        preview.pack(fill="x", padx=20, pady=(2, 12))

        def refresh(*_):
            hh, mm = ih.get(), im.get()
            gap = f"{hh}小时{mm}分钟" if hh > 0 else f"{mm}分钟"
            preview.configure(
                text=f"{sh.get():02d}:{sm.get():02d} ~ {eh.get():02d}:{em.get():02d} · 每{gap}一次")
        for w in widgets.values():
            w.command = refresh
        refresh()
        return widgets

    c2 = neu.NeuCard(content, ctx, 640, 272, radius=22, pad=14, outer_bg=True)
    c2.pack(pady=(0, 14), padx=40)
    tk.Label(c2.inner, text="☀️ 白天时段", bg=TH["base"], fg=ORANGE,
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)
    day_w = build_slot(c2.inner, dict(cfg["day"]), ORANGE)

    c3 = neu.NeuCard(content, ctx, 640, 272, radius=22, pad=14, outer_bg=True)
    c3.pack(pady=(0, 14), padx=40)
    tk.Label(c3.inner, text="🌙 晚上时段", bg=TH["base"], fg=BLUE,
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)
    night_w = build_slot(c3.inner, dict(cfg["night"]), BLUE)

    # ===== 发送设置 =====
    cs = neu.NeuCard(content, ctx, 640, 300, radius=22, pad=14, outer_bg=True)
    cs.pack(pady=(0, 14), padx=40)
    tk.Label(cs.inner, text="📤 发送设置", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=12)

    def switch_row(title, desc, default, accent, y):
        tk.Label(cs.inner, text=title, bg=TH["base"], fg=TH["text_main"],
                 font=("Microsoft YaHei UI", 14, "bold")).place(x=18, y=y)
        tk.Label(cs.inner, text=desc, bg=TH["base"], fg=TH["text_sub"],
                 font=("Microsoft YaHei UI", 11)).place(x=18, y=y + 24)
        sw = neu.NeuSwitch(cs.inner, ctx, accent_hex=accent, default=default)
        sw.place(x=540, y=y + 6)
        return sw

    capture_sw = switch_row("复制表格图片", "跑完后在汇总表选区复制为图片",
                            cfg.get("enable_capture", True), BLUE, 48)
    dt_sw = switch_row("发送到钉钉", "把图片发到下面填的钉钉会话",
                       cfg.get("enable_dingtalk", False), GREEN, 100)
    auto_send_sw = switch_row("自动点发送", "关掉则只贴好图片，由你手动点发送",
                              cfg.get("dt_auto_send", True), ORANGE, 152)

    dt_search_var = tk.StringVar(value=cfg.get("dt_search", "文件小助手"))
    dt_suffix_var = tk.StringVar(value=cfg.get("dt_suffix", "数据"))

    def entry_row(label, var, y):
        tk.Label(cs.inner, text=label, bg=TH["base"], fg=TH["text_sub"],
                 font=("Microsoft YaHei UI", 13)).place(x=18, y=y + 8)
        fld = neu.NeuField(cs.inner, ctx, 440, h=44, textvariable=var)
        fld.place(x=110, y=y - 4)
        return fld

    entry_row("钉钉会话", dt_search_var, 204)
    entry_row("消息后缀", dt_suffix_var, 250)

    # ===== 日志 =====
    c5 = neu.NeuCard(content, ctx, 640, 240, radius=22, pad=14, outer_bg=True)
    c5.pack(pady=(0, 14), padx=40)
    tk.Label(c5.inner, text="📋 运行日志", bg=TH["base"], fg=TH["text_main"],
             font=("Microsoft YaHei UI", 15, "bold")).place(x=18, y=10)
    log_inset = neu.NeuInset(c5.inner, ctx, 600, 112, radius=14, pad=10)
    log_inset.place(x=20, y=42)
    log_box = tk.Text(log_inset.inner, bd=0, relief=tk.FLAT, wrap="word",
                      font=("Microsoft YaHei UI", 12),
                      bg=TH["base"], fg=TH["text_main"], highlightthickness=0)
    log_box.pack(fill="both", expand=True, padx=6, pady=4)

    def append_log(msg):
        def _add():
            log_box.insert("end", msg + "\n")
            log_box.see("end")
        root.after(0, _add)
    set_gui_logger(append_log)

    # 日志卡内控制按钮
    btn_manual = neu.NeuButton(c5.inner, ctx, 290, 42, "立即跑一次",
                               radius=13, accent=BLUE, emoji="▶", font_size=13, filled=True)
    btn_manual.place(x=18, y=182)
    btn_stop = neu.NeuButton(c5.inner, ctx, 290, 42, "停止计划",
                             radius=13, accent=RED, emoji="■", font_size=13)
    btn_stop.place(x=324, y=182)

    tip = tk.Label(content, text="小提示：数字区域支持滚轮微调；计划运行时也可随时手动执行",
                   bg=TH["window"], fg=TH["text_sub"], wraplength=600, justify="left",
                   font=("Microsoft YaHei UI", 11))
    tip.pack(pady=(0, 6), padx=40, anchor="w")
    lab_sub.append(tip)

    btn_reset = neu.NeuButton(content, ctx, 640, 48, "恢复默认设置",
                              radius=15, accent=RED, emoji="↺", font_size=14,
                              outer_bg=True)
    btn_reset.pack(pady=(6, 24), padx=40)

    # ===== 扫码全屏窗 =====
    prompt_win = {"win": None}

    def _open_prompt():
        if prompt_win["win"] is not None:
            return
        top = tk.Toplevel(root)
        prompt_win["win"] = top
        sw, sh = top.winfo_screenwidth(), top.winfo_screenheight()
        top.overrideredirect(True)
        top.geometry(f"{sw}x{sh}+0+0")
        top.attributes("-topmost", True)
        try:
            top.attributes("-alpha", 0.62)
        except Exception:
            pass
        top.configure(bg="#101013")
        card = neu.NeuCard(top, ctx, 560, 360, radius=30, pad=16)
        card.place(relx=0.5, rely=0.5, anchor="center")
        tk.Label(card.inner, text="⏳", bg=ctx.base_hex,
                 font=("Microsoft YaHei UI", 70)).pack(pady=(40, 4))
        tk.Label(card.inner, text="请在浏览器里扫码登录", bg=ctx.base_hex, fg=BLUE,
                 font=("Microsoft YaHei UI", 28, "bold")).pack(pady=(4, 10))
        tk.Label(card.inner, text="登录成功后会自动继续（最多等待 20 分钟）",
                 bg=ctx.base_hex, fg=TH["text_main"], justify="center",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(pady=(0, 30))

    def _close_prompt():
        w = prompt_win["win"]
        if w is not None:
            try:
                w.destroy()
            except Exception:
                pass
            prompt_win["win"] = None

    set_login_prompt(lambda: root.after(0, _open_prompt),
                     lambda: root.after(0, _close_prompt))

    # ===== 业务逻辑 =====
    def do_run(round_num):
        nonlocal schedule_running, stop_flag
        global _stop_requested
        _stop_requested = False
        # 显示桌面毛玻璃浮窗（内部自动重置，线程安全）
        glass.show()
        try:
            err_lab.configure(text="")
            step_anim.set(f"🔄 正在跑第 {round_num} 轮...", color=ORANGE)
            glass.update_step(f"正在跑第 {round_num} 轮…", 0.02)
            root.update()
            run_cfg = dict(collect_and_save())
            if isinstance(round_num, int) and not capture_allowed_now(run_cfg):
                run_cfg["enable_capture"] = False
                run_cfg["enable_dingtalk"] = False
                log("  当前不在截图发送时段，本轮只下载并更新表格")
                step_anim.set("🕒 不在截图发送时段，仅更新表格", color=TH["text_sub"])
            end_time = run_once(round_num, run_cfg)
            done_text = ("✅ 手动任务已完成" if round_num == "手动"
                         else f"✅ 第 {round_num} 轮已完成")
            step_anim.set(done_text, color=GREEN)
            glass.finish("✅ 本轮跑完")
            glass.close(1100)
            return end_time
        except Exception as e:
            _close_prompt()
            if "手动停止" in str(e):
                log("⏹ 已手动停止")
                err_lab.configure(text="")
                step_anim.set("⏹ 已停止", color=TH["text_sub"])
                glass.fail("⏹ 已手动停止")
                glass.close(700)
                return time.time()
            tip = human_error(e)
            log(f"❌ {tip}")
            err_lab.configure(text="⚠️ 出错步骤：" + tip)
            step_anim.set("❌ 出错了，请看下面日志", color=RED)
            glass.fail("❌ " + tip)
            glass.close(1800)
            try:
                import traceback
                with open(app_dir() / "error.log", "a", encoding="utf-8") as f:
                    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
                    f.write(traceback.format_exc())
            except Exception:
                pass
            return time.time()

    def read_slot(w):
        sh, sm = w["sh"].get(), w["sm"].get()
        eh, em = w["eh"].get(), w["em"].get()
        ih, im = w["ih"].get(), w["im"].get()
        start = f"{sh:02d}:{sm:02d}"
        end = f"{eh:02d}:{em:02d}"
        interval = ih * 60 + im
        if interval < 1:
            raise ValueError
        return start, end, interval

    def _slot_dict(w):
        return dict(sh=w["sh"].get(), sm=w["sm"].get(),
                    eh=w["eh"].get(), em=w["em"].get(),
                    ih=w["ih"].get(), im=w["im"].get())

    def collect_and_save():
        try:
            cfg["day"] = _slot_dict(day_w)
            cfg["night"] = _slot_dict(night_w)
        except Exception:
            pass
        cfg["day_mode"] = day_mode_var.get()
        cfg["enable_capture"] = capture_sw.get()
        cfg["enable_dingtalk"] = dt_sw.get()
        cfg["dt_auto_send"] = auto_send_sw.get()
        cfg["dt_search"] = dt_search_var.get().strip() or "文件小助手"
        cfg["dt_suffix"] = dt_suffix_var.get().strip()
        save_config(cfg)
        return cfg

    def reset_defaults():
        ok = messagebox.askyesno(
            "恢复默认设置",
            "确定要把所有设置恢复成默认吗？\n\n你之前设置的运行日期、白天/晚上时段、间隔、钉钉会话和后缀都会被还原，此操作不可撤销。")
        if not ok:
            return
        import copy
        default = copy.deepcopy(DEFAULT_CONFIG)
        cfg.clear()
        cfg.update(default)
        seg.select(default["day_mode"])
        day_mode_var.set(default["day_mode"])
        for key in ("sh", "sm", "eh", "em", "ih", "im"):
            day_w[key].set(default["day"][key])
            night_w[key].set(default["night"][key])
        capture_sw.set(default["enable_capture"])
        dt_sw.set(default["enable_dingtalk"])
        auto_send_sw.set(default["dt_auto_send"])
        dt_search_var.set(default["dt_search"])
        dt_suffix_var.set(default["dt_suffix"])
        save_config(cfg)
        messagebox.showinfo("已恢复", "所有设置已恢复成默认。")

    btn_reset.command = reset_defaults

    # ===== 防抖自动保存 =====
    _auto_id = [None]

    def _do_autosave():
        _auto_id[0] = None
        try:
            collect_and_save()
        except Exception:
            pass

    def _autosave(*_):
        if _auto_id[0] is not None:
            try:
                root.after_cancel(_auto_id[0])
            except Exception:
                pass
        _auto_id[0] = root.after(500, _do_autosave)

    for _w in list(day_w.values()) + list(night_w.values()):
        _orig = _w.command

        def _wrap(orig):
            def _runner():
                if orig:
                    orig()
                _autosave()
            return _runner
        _w.command = _wrap(_orig)
    capture_sw.command = lambda on: _autosave()
    dt_sw.command = lambda on: _autosave()
    seg.command = lambda v: (day_mode_var.set(v), _autosave())
    dt_search_var.trace_add("write", _autosave)
    dt_suffix_var.trace_add("write", _autosave)

    def validate_config():
        try:
            ds, de, di = read_slot(day_w)
            ns, ne, ni = read_slot(night_w)
            return dict(day_mode=day_mode_var.get(),
                        day_start=ds, day_end=de, day_interval=di,
                        night_start=ns, night_end=ne, night_interval=ni)
        except Exception:
            messagebox.showerror("提示", "间隔不能是0，请把「每隔」的小时或分钟调大一点")
            return None

    def do_capture_only():
        global _active_end_time
        manual_start = time.time()
        _active_end_time = None
        _on_step("正在打开WPS汇总表…")
        _on_last_run()
        completed = False
        try:
            glass.show()
            end_time = run_capture_only("截图", collect_and_save())
            _active_end_time = end_time
            completed = True
            root.after(0, lambda: (
                step_anim.set("✅ 截图发送完成", color=GREEN),
                err_lab.configure(text="")))
        except CancelledError:
            root.after(0, lambda: (
                step_anim.set("⏹ 已停止", color=TH["text_sub"]),
                err_lab.configure(text="")))
        except Exception as e:
            log(f"❌ 独立截图失败: {e}")
            root.after(0, lambda: (
                step_anim.set("❌ 截图发送失败", color=RED),
                err_lab.configure(text=str(e))))
        finally:
            def _finish_ui():
                run_spinner.place_forget()
                status_dot.place(x=38, y=60)
                if completed:
                    glass.finish("✅ 截图发送完成")
                    glass.close(1400)
                else:
                    glass.close(1200)
            root.after(0, _finish_ui)

    def on_click_now():
        if capture_running:
            messagebox.showinfo("提示", "独立截图正在运行，请等它结束后再试")
            return
        btn_now.set_enabled(False)
        btn_capture.set_enabled(False)

        def task():
            try:
                do_run("手动")
            finally:
                root.after(0, lambda: (
                    btn_now.set_enabled(True), btn_capture.set_enabled(True)))
        threading.Thread(target=task, daemon=True).start()
    btn_now.command = on_click_now

    def on_click_capture():
        nonlocal capture_running
        if capture_running:
            return
        if schedule_running:
            messagebox.showinfo("提示", "计划任务正在运行，请先停止计划再单独截图")
            return
        capture_running = True
        btn_now.set_enabled(False)
        btn_capture.set_enabled(False)

        def task():
            nonlocal capture_running
            try:
                do_capture_only()
            finally:
                capture_running = False
                root.after(0, lambda: (
                    btn_now.set_enabled(True), btn_capture.set_enabled(True)))
        threading.Thread(target=task, daemon=True).start()
    btn_capture.command = on_click_capture

    def on_click_schedule():
        nonlocal schedule_running, stop_flag
        if capture_running:
            messagebox.showinfo("提示", "独立截图正在运行，请等它结束后再启动计划")
            return
        plan_cfg = validate_config()
        if not plan_cfg:
            return
        collect_and_save()
        confirm = messagebox.askyesno(
            "确认开始",
            f"运行方式：{'每天' if plan_cfg['day_mode']=='daily' else '仅工作日'}\n"
            f"白天 {plan_cfg['day_start']}~{plan_cfg['day_end']}，每{plan_cfg['day_interval']}分钟\n"
            f"晚上 {plan_cfg['night_start']}~{plan_cfg['night_end']}，每{plan_cfg['night_interval']}分钟\n\n开始吗？")
        if not confirm:
            return
        schedule_running = True
        stop_flag = False
        btn_schedule.text = "运行中..."
        btn_schedule.redraw()

        def schedule_task():
            nonlocal schedule_running, stop_flag, manual_trigger
            round_num = 1
            last_run = {"day": 0.0, "night": 0.0}
            while not stop_flag:
                now = time.localtime()
                if plan_cfg["day_mode"] == "workday" and now.tm_wday >= 5:
                    step_anim.set("🌙 今天是周末，不运行", color=TH["text_sub"])
                    next_anim.set("下次抓取：—", color=BLUE)
                    for _ in range(30):
                        if stop_flag:
                            break
                        time.sleep(1)
                    continue
                slot = None
                if in_slot(now, plan_cfg["day_start"], plan_cfg["day_end"]):
                    slot = "day"
                elif in_slot(now, plan_cfg["night_start"], plan_cfg["night_end"]):
                    slot = "night"
                if slot is None:
                    step_anim.set("🌙 不在运行时段，等待中", color=TH["text_sub"])
                    next_anim.set("下次抓取：—", color=BLUE)
                    for _ in range(30):
                        if stop_flag:
                            break
                        if manual_trigger:
                            manual_trigger = False
                            do_run("手动")
                        time.sleep(1)
                    continue
                interval = plan_cfg["day_interval"] if slot == "day" else plan_cfg["night_interval"]
                since = time.time() - last_run[slot]
                if manual_trigger:
                    manual_trigger = False
                    do_run("手动")
                    last_run[slot] = time.time()
                elif since >= interval * 60:
                    do_run(round_num)
                    round_num += 1
                    last_run[slot] = time.time()
                else:
                    left = int(interval * 60 - since)
                    nxt = time.strftime("%H:%M", time.localtime(time.time() + left))
                    step_anim.set(f"⏳ 在{('白天' if slot=='day' else '晚上')}时段，等待下一次",
                                  color=ORANGE)
                    next_anim.set(f"下次自动跑：{nxt}", color=BLUE)
                for _ in range(10):
                    if stop_flag:
                        break
                    time.sleep(1)
            schedule_running = False

            def _restore():
                btn_schedule.text = "开始计划任务"
                btn_schedule.redraw()
                step_anim.set("⏹ 计划任务已停止", color=TH["text_sub"])
                next_anim.set("下次抓取：—", color=BLUE)
            root.after(0, _restore)
        threading.Thread(target=schedule_task, daemon=True).start()
    btn_schedule.command = on_click_schedule

    def on_click_manual():
        nonlocal manual_trigger
        manual_trigger = True
        step_anim.set("⚡ 已触发，马上跑...", color=ORANGE)
    btn_manual.command = on_click_manual

    def on_click_stop():
        nonlocal stop_flag
        if messagebox.askyesno("确认", "要停止计划任务吗？"):
            stop_flag = True
            request_stop()
    btn_stop.command = on_click_stop

    def _check_update():
        info = fetch_remote_version()
        if info and _ver_newer(info[0], APP_VERSION):
            ver, url = info

            def ask():
                try:
                    if messagebox.askyesno("发现新版本", f"检测到新版本 v{ver}，是否前往下载？"):
                        import webbrowser
                        webbrowser.open(url)
                except Exception:
                    pass
            root.after(0, ask)
    threading.Thread(target=_check_update, daemon=True).start()

    root.mainloop()


def show_settings_window():
    from ios_ui import show_settings_window as show
    show(sys.modules[__name__])


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit(0)
    try:
        import threading
        threading.Thread(target=ensure_browser_installed, daemon=True).start()
        show_settings_window()
    except Exception as e:
        log_error(e, "启动失败")
        try:
            messagebox.showerror("出错了", human_error(e))
        except Exception:
            pass
