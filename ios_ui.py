# -*- coding: utf-8 -*-
"""暗号表助手·迷你版 (SheetMate-Mini) v1.0.0 轻量丝滑 Apple iOS 风格控制中心与调度后端。"""

import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

STEP_PROGRESS = {
    "正在打开浏览器…": 0.06,
    "正在打开WPS汇总表…": 0.16,
    "正在登录后台…": 0.18,
    "正在下载三份数据…": 0.32,
    "正在整理数据格式…": 0.45,
    "正在写入在线表格…": 0.60,
    "正在切换到数据汇总表…": 0.72,
    "正在等待汇总表刷新…": 0.76,
    "正在复制表格图片…": 0.84,
    "正在关闭后台浏览器…": 0.89,
    "正在发送到钉钉…": 0.95,
    "✅ 本轮跑完": 1.0,
}


def _find_app_shell_browser():
    """查找可作为独立 --app 原生感窗口启动的 Chromium 内核浏览器。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path, "edge_or_chrome"

    # 兜底尝试 Playwright 内置 Chromium
    pw_base = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    ))
    if pw_base.exists():
        for d in sorted(pw_base.glob("chromium-*"), reverse=True):
            exe = d / "chrome-win64" / "chrome.exe"
            if exe.exists():
                return str(exe), "chromium"
    return None, None


class _SheetMateController:
    """线程安全的配置、日志、任务执行与自动值守调度控制器。"""

    def __init__(self, app, glass, tk_root):
        self.app = app
        self.glass = glass
        self.tk_root = tk_root
        self._lock = threading.RLock()
        self._task_lock = threading.Lock()

        self.cfg = app.load_config()
        self.logs = []
        self._log_seq = 0

        self.job_running = False
        self.capture_running = False
        self.schedule_running = False
        self.stop_flag = False
        self.need_login_prompt = False

        self.status_level = "idle"  # idle | running | ok | err | stopped
        self.step_text = "空闲中，等待开始"
        self.progress = 0.0
        self.error_text = ""
        self.last_run_str = "—"
        self.next_run_str = "—"
        self.next_run_ts = 0.0
        self.last_heartbeat = time.time()
        self.close_requested_at = 0.0

        self.update_info = None

        app.set_gui_logger(self._on_log)
        app.set_step_callback(self._on_step)
        app.set_last_run_callback(self._on_last_run)
        app.set_login_prompt(self._on_login_prompt_show, self._on_login_prompt_close)

        threading.Thread(target=self._check_update_worker, daemon=True).start()

    def _check_update_worker(self):
        try:
            info = self.app.fetch_remote_version()
            if info and self.app._ver_newer(info[0], self.app.APP_VERSION):
                with self._lock:
                    self.update_info = {"version": info[0], "url": info[1]}
        except Exception:
            pass

    def _on_log(self, msg):
        level = "info"
        s = str(msg)
        if s.startswith("✅"):
            level = "ok"
        elif s.startswith("❌"):
            level = "err"
        elif s.startswith("⚠️") or "警告" in s or "未成功" in s or "未命中" in s:
            level = "warn"
        elif s.startswith("====") or s.startswith("📊") or s.startswith("📸"):
            level = "head"
        with self._lock:
            self._log_seq += 1
            self.logs.append({
                "id": self._log_seq,
                "time": time.strftime("%H:%M:%S"),
                "level": level,
                "text": s,
            })
            if len(self.logs) > 600:
                self.logs = self.logs[-500:]

    def _on_step(self, text):
        with self._lock:
            self.step_text = text
            prog = STEP_PROGRESS.get(text)
            if prog is not None:
                self.progress = prog
                if not self.glass.closed:
                    self.glass.update_step(text, prog)
            if text.startswith("✅"):
                self.status_level = "ok"
            elif text.startswith("❌"):
                self.status_level = "err"
            elif text.startswith("⏹"):
                self.status_level = "stopped"
            elif self.job_running or self.capture_running:
                self.status_level = "running"

    def _on_last_run(self):
        with self._lock:
            self.last_run_str = time.strftime("%m-%d %H:%M")

    def _on_login_prompt_show(self):
        with self._lock:
            self.need_login_prompt = True
        self._on_log("⏳ 请在弹出的浏览器中扫码登录 WPS，登录完成后将自动继续")

    def _on_login_prompt_close(self):
        with self._lock:
            self.need_login_prompt = False

    def get_config(self):
        with self._lock:
            return json.loads(json.dumps(self.cfg))

    def save_config(self, new_cfg):
        with self._lock:
            norm = self.app.normalize_config(new_cfg) if hasattr(self.app, "normalize_config") else self.app._deep_merge(self.app.DEFAULT_CONFIG, new_cfg)
            self.app.save_config(norm)
            self.cfg = self.app.load_config()
            return json.loads(json.dumps(self.cfg))

    def reset_config(self):
        import copy
        with self._lock:
            default = copy.deepcopy(self.app.DEFAULT_CONFIG)
            self.app.save_config(default)
            self.cfg = self.app.load_config()
            return json.loads(json.dumps(self.cfg))

    def clear_logs(self):
        with self._lock:
            self.logs.clear()

    def get_state(self, since=0):
        with self._lock:
            self.last_heartbeat = time.time()
            delta = [item for item in self.logs if item["id"] > since]
            return {
                "version": self.app.APP_VERSION,
                "job_running": self.job_running,
                "capture_running": self.capture_running,
                "busy": self.job_running or self.capture_running,
                "schedule_running": self.schedule_running,
                "need_login_prompt": self.need_login_prompt,
                "status_level": self.status_level,
                "step_text": self.step_text,
                "progress": self.progress,
                "error_text": self.error_text,
                "last_run_str": self.last_run_str,
                "next_run_str": self.next_run_str,
                "next_run_ts": self.next_run_ts,
                "server_now": time.time(),
                "log_seq": self._log_seq,
                "logs": delta,
                "update_info": self.update_info,
            }

    def start_run_now(self):
        with self._lock:
            if self.job_running or self.capture_running:
                return False, "当前已有任务正在运行，请稍候"
            if not self._task_lock.acquire(blocking=False):
                return False, "当前已有任务正在运行，请稍候"
            self.job_running = True
            self.error_text = ""
            self.status_level = "running"
            self.progress = 0.02
            self.step_text = "准备开始手动任务…"
        threading.Thread(target=self._run_once_worker, args=("手动", True), daemon=True).start()
        return True, "已开始运行"

    def start_capture_only(self):
        with self._lock:
            if self.job_running or self.capture_running:
                return False, "当前已有任务正在运行，请稍候"
            if not self._task_lock.acquire(blocking=False):
                return False, "当前已有任务正在运行，请稍候"
            self.capture_running = True
            self.error_text = ""
            self.status_level = "running"
            self.progress = 0.04
            self.step_text = "正在打开WPS汇总表…"
        threading.Thread(target=self._capture_only_worker, daemon=True).start()
        return True, "已开始仅截图发送"

    def _run_once_worker(self, round_name, owns_lock=True):
        self.glass.show()
        self.glass.update_step(f"正在跑第 {round_name} 轮…", 0.02)
        try:
            run_cfg = self.get_config()
            if isinstance(round_name, int) and not self.app.capture_allowed_now(run_cfg):
                run_cfg["enable_capture"] = False
                run_cfg["enable_dingtalk"] = False
                self.app.log("  当前不在截图发送时段，本轮只下载并更新表格")
                self._on_step("🕒 不在截图发送时段，仅更新表格")
            self.app.run_once(round_name, run_cfg)
            done_text = "✅ 手动任务已完成" if not isinstance(round_name, int) else f"✅ 第 {round_name} 轮已完成"
            with self._lock:
                self.status_level = "ok"
                self.step_text = done_text
                self.progress = 1.0
                self.error_text = ""
            self.glass.finish("✅ 本轮跑完")
            self.glass.close(1100)
        except Exception as exc:
            if "手动停止" in str(exc):
                self.app.log("⏹ 已手动停止")
                with self._lock:
                    self.status_level = "stopped"
                    self.step_text = "⏹ 已停止"
                    self.error_text = ""
                self.glass.fail("⏹ 已手动停止")
                self.glass.close(700)
            else:
                self.app.log_error(exc, f"执行失败-{round_name}")
                tip = self.app.human_error(exc)
                self.app.log(f"❌ 本轮失败：{tip}")
                with self._lock:
                    self.status_level = "err"
                    self.step_text = "❌ 运行失败，请看下方说明"
                    self.error_text = tip
                self.glass.fail("❌ " + tip)
                self.glass.close(1800)
        finally:
            with self._lock:
                self.job_running = False
                self.need_login_prompt = False
            if owns_lock:
                try:
                    self._task_lock.release()
                except Exception:
                    pass

    def _capture_only_worker(self):
        self.glass.show()
        self.glass.update_step("正在打开WPS汇总表…", 0.05)
        try:
            run_cfg = self.get_config()
            self.app.run_capture_only("截图", run_cfg)
            with self._lock:
                self.status_level = "ok"
                self.step_text = "✅ 截图发送完成"
                self.progress = 1.0
                self.error_text = ""
            self.glass.finish("✅ 截图发送完成")
            self.glass.close(1400)
        except Exception as exc:
            if "手动停止" in str(exc):
                self.app.log("⏹ 已手动停止")
                with self._lock:
                    self.status_level = "stopped"
                    self.step_text = "⏹ 已停止"
                    self.error_text = ""
                self.glass.fail("⏹ 已手动停止")
                self.glass.close(700)
            else:
                self.app.log_error(exc, "独立截图失败")
                tip = self.app.human_error(exc)
                self.app.log(f"❌ 独立截图失败：{tip}")
                with self._lock:
                    self.status_level = "err"
                    self.step_text = "❌ 截图发送失败"
                    self.error_text = tip
                self.glass.fail("❌ " + tip)
                self.glass.close(1800)
        finally:
            with self._lock:
                self.capture_running = False
                self.need_login_prompt = False
            try:
                self._task_lock.release()
            except Exception:
                pass

    def stop_current_task(self):
        self.app.request_stop()
        with self._lock:
            if self.schedule_running:
                self.stop_flag = True
        return True, "已发送停止指令"

    def toggle_schedule(self, enable=None):
        with self._lock:
            want = (not self.schedule_running) if enable is None else bool(enable)
            if not want:
                if self.schedule_running:
                    self.stop_flag = True
                    self.app.request_stop()
                    return True, "正在停止自动值守…", False
                return True, "自动值守未开启", False

            if self.schedule_running:
                return True, "自动值守已在运行", True
            if self.job_running or self.capture_running:
                return False, "请等当前任务结束后再开启自动值守", False

            cfg = self.get_config()
            day_gap = cfg["day"]["ih"] * 60 + cfg["day"]["im"]
            night_gap = cfg["night"]["ih"] * 60 + cfg["night"]["im"]
            if day_gap < 1 or (cfg.get("night_enabled", True) and night_gap < 1):
                return False, "白天或夜间的执行间隔不能为 0 分钟", False

            self.schedule_running = True
            self.stop_flag = False
            self.error_text = ""
        if cfg.get("prevent_sleep", True) and hasattr(self.app, "set_prevent_sleep"):
            self.app.set_prevent_sleep(True)
        threading.Thread(target=self._schedule_loop, daemon=True).start()
        return True, "已开启自动值守", True

    def _compute_next_slot_start(self, cfg, now_ts):
        """计算不在任何运行窗口内时，下一个有效窗口的精确开始时间戳。"""
        slots = [
            f"{cfg['day']['sh']:02d}:{cfg['day']['sm']:02d}",
        ]
        if cfg.get("night_enabled", True):
            slots.append(f"{cfg['night']['sh']:02d}:{cfg['night']['sm']:02d}")
        slot_ends = {
            slots[0]: f"{cfg['day']['eh']:02d}:{cfg['day']['em']:02d}",
        }
        if len(slots) > 1:
            slot_ends[slots[1]] = f"{cfg['night']['eh']:02d}:{cfg['night']['em']:02d}"

        day_mode = cfg.get("day_mode", "daily")
        # 逐分钟向前探查最多 4 天，精准兼容跨夜及仅工作日
        base_min = int(now_ts // 60) * 60
        for offset_min in range(1, 4 * 24 * 60):
            ts = base_min + offset_min * 60
            lt = time.localtime(ts)
            if day_mode == "workday" and lt.tm_wday >= 5:
                continue
            for st in slots:
                if self.app.in_slot(lt, st, slot_ends[st]):
                    return ts
        return 0.0

    def _schedule_loop(self):
        round_num = 1
        last_run_start = 0.0
        while not self.stop_flag:
            cfg = self.get_config()
            now_ts = time.time()
            now = time.localtime(now_ts)

            if cfg.get("day_mode", "daily") == "workday" and now.tm_wday >= 5:
                nxt = self._compute_next_slot_start(cfg, now_ts)
                with self._lock:
                    if not self.job_running and not self.capture_running:
                        self.status_level = "idle"
                        self.step_text = "今天是非工作日，自动值守休眠中"
                    self.next_run_ts = nxt
                    self.next_run_str = time.strftime("%m-%d %H:%M", time.localtime(nxt)) if nxt else "—"
                for _ in range(10):
                    if self.stop_flag:
                        break
                    time.sleep(1)
                continue

            day_start = f"{cfg['day']['sh']:02d}:{cfg['day']['sm']:02d}"
            day_end = f"{cfg['day']['eh']:02d}:{cfg['day']['em']:02d}"
            night_start = f"{cfg['night']['sh']:02d}:{cfg['night']['sm']:02d}"
            night_end = f"{cfg['night']['eh']:02d}:{cfg['night']['em']:02d}"

            slot = None
            if self.app.in_slot(now, day_start, day_end):
                slot = "day"
            elif cfg.get("night_enabled", True) and self.app.in_slot(now, night_start, night_end):
                slot = "night"

            if slot is None:
                nxt = self._compute_next_slot_start(cfg, now_ts)
                with self._lock:
                    if not self.job_running and not self.capture_running:
                        self.status_level = "idle"
                        self.step_text = "不在运行时段，静默待命中"
                    self.next_run_ts = nxt
                    self.next_run_str = time.strftime("%H:%M", time.localtime(nxt)) if nxt else "—"
                for _ in range(10):
                    if self.stop_flag:
                        break
                    time.sleep(1)
                continue

            gap_min = (cfg["day"]["ih"] * 60 + cfg["day"]["im"]) if slot == "day" else (cfg["night"]["ih"] * 60 + cfg["night"]["im"])
            interval_sec = max(60, gap_min * 60)
            since = now_ts - last_run_start

            if (last_run_start == 0.0 or since >= interval_sec) and not self.job_running and not self.capture_running:
                if self._task_lock.acquire(blocking=False):
                    last_run_start = time.time()
                    with self._lock:
                        self.job_running = True
                        self.status_level = "running"
                        self.progress = 0.02
                        self.error_text = ""
                        self.next_run_ts = last_run_start + interval_sec
                        self.next_run_str = time.strftime("%H:%M", time.localtime(self.next_run_ts))
                    self._run_once_worker(round_num, owns_lock=True)
                    round_num += 1
                    continue

            next_ts = (last_run_start + interval_sec) if last_run_start else now_ts
            with self._lock:
                if not self.job_running and not self.capture_running:
                    slot_label = "白天" if slot == "day" else "晚上"
                    self.status_level = "idle"
                    self.step_text = f"自动值守中（{slot_label}时段）"
                self.next_run_ts = next_ts
                self.next_run_str = time.strftime("%H:%M", time.localtime(next_ts))

            for _ in range(5):
                if self.stop_flag:
                    break
                time.sleep(1)

        if hasattr(self.app, "set_prevent_sleep"):
            self.app.set_prevent_sleep(False)
        with self._lock:
            self.schedule_running = False
            self.next_run_ts = 0.0
            self.next_run_str = "—"
            if not self.job_running and not self.capture_running:
                self.status_level = "stopped"
                self.step_text = "⏹ 自动值守已停止"


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
<title>📊暗号表助手·迷你版</title>
<style>
  :root {
    --bg: #F2F2F7;
    --nav: rgba(242, 242, 247, 0.96);
    --card: #FFFFFF;
    --card-soft: #F9F9FB;
    --sep: rgba(60, 60, 67, 0.12);
    --text: #000000;
    --text-2: #3C3C43;
    --sub: #8E8E93;
    --pill: rgba(120, 120, 128, 0.12);
    --pill-blue: rgba(0, 122, 255, 0.11);
    --switch-off: rgba(120, 120, 128, 0.24);
    --shadow: 0 2px 14px rgba(0, 0, 0, 0.045);
    --sheet-bg: #FFFFFF;
    --log-bg: #F6F7FA;
    --blue: #007AFF;
    --green: #34C759;
    --orange: #FF9500;
    --red: #FF3B30;
    --indigo: #5856D6;
    --spring: cubic-bezier(0.32, 0.72, 0, 1);
  }
  [data-theme="dark"] {
    --bg: #000000;
    --nav: rgba(18, 18, 20, 0.96);
    --card: #1C1C1E;
    --card-soft: #242426;
    --sep: rgba(255, 255, 255, 0.13);
    --text: #FFFFFF;
    --text-2: #EBEBF5;
    --sub: #8E8E93;
    --pill: rgba(120, 120, 128, 0.28);
    --pill-blue: rgba(10, 132, 255, 0.22);
    --switch-off: rgba(120, 120, 128, 0.36);
    --shadow: 0 4px 20px rgba(0, 0, 0, 0.45);
    --sheet-bg: #1C1C1E;
    --log-bg: #141416;
    --blue: #0A84FF;
    --green: #30D158;
    --orange: #FF9F0A;
    --red: #FF453A;
    --indigo: #5E5CE6;
  }
  * {
    box-sizing: border-box;
    -webkit-tap-highlight-color: transparent;
  }
  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", "PingFang SC", "Microsoft YaHei UI", "Segoe UI Emoji", "Apple Color Emoji", sans-serif;
    font-size: 14px;
    line-height: 1.4;
    -webkit-font-smoothing: antialiased;
    overflow-x: hidden;
    user-select: none;
  }
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: rgba(142, 142, 147, 0.35); border-radius: 99px; }

  /* 顶部毛玻璃导航栏 */
  .nav {
    position: sticky;
    top: 0;
    z-index: 40;
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 16px;
    background: var(--nav);
    border-bottom: 0.5px solid var(--sep);
  }
  .nav-left {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .app-icon {
    width: 34px;
    height: 34px;
    border-radius: 9px;
    box-shadow: 0 3px 10px rgba(0, 122, 255, 0.25);
    flex-shrink: 0;
  }
  .app-title-wrap {
    display: flex;
    flex-direction: column;
  }
  .app-title {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 16px;
    font-weight: 700;
    letter-spacing: -0.3px;
  }
  .ver-badge {
    font-size: 10px;
    font-weight: 600;
    padding: 1px 6px;
    border-radius: 99px;
    background: var(--pill-blue);
    color: var(--blue);
  }
  .app-sub {
    font-size: 11px;
    color: var(--sub);
  }
  .nav-actions {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .icon-pill-btn {
    position: relative;
    width: 32px;
    height: 32px;
    border: none;
    border-radius: 50%;
    background: var(--pill);
    color: var(--text);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    font-size: 15px;
    transition: transform 0.18s var(--spring), background 0.2s;
  }
  .icon-pill-btn:active { transform: scale(0.9); }
  .update-dot {
    position: absolute;
    top: 4px;
    right: 4px;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--red);
    display: none;
  }

  /* 主体容器 */
  .container {
    max-width: 540px;
    margin: 0 auto;
    padding: 12px 14px 36px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  /* 灵动岛实时活动卡片 */
  .live-card {
    background: var(--card);
    border-radius: 22px;
    padding: 14px 16px;
    box-shadow: var(--shadow);
    border: 0.5px solid var(--sep);
    transition: background 0.3s, border-color 0.3s, transform 0.25s var(--spring);
  }
  .live-card.running {
    background: linear-gradient(135deg, #161A23 0%, #1C2436 100%);
    color: #FFFFFF;
    border-color: rgba(10, 132, 255, 0.35);
  }
  .live-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
  }
  .live-status {
    display: flex;
    align-items: center;
    gap: 10px;
    min-width: 0;
    flex: 1;
  }
  .status-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--green);
    flex-shrink: 0;
    box-shadow: 0 0 0 3px rgba(52, 199, 89, 0.2);
  }
  .status-dot.err { background: var(--red); box-shadow: 0 0 0 3px rgba(255, 59, 48, 0.2); }
  .status-dot.stopped { background: var(--sub); box-shadow: none; }

  /* iOS 菊花加载器 */
  .ios-spinner {
    position: relative;
    width: 18px;
    height: 18px;
    flex-shrink: 0;
    display: none;
  }
  .live-card.running .ios-spinner { display: block; }
  .live-card.running .status-dot { display: none; }
  .ios-spinner div {
    position: absolute;
    left: 46%;
    top: 3%;
    width: 10%;
    height: 28%;
    background: #FFFFFF;
    border-radius: 50px;
    transform-origin: center 170%;
    animation: ios-spin 0.9s linear infinite;
  }
  @keyframes ios-spin {
    0% { opacity: 1; }
    100% { opacity: 0.15; }
  }
  .step-title {
    font-size: 15px;
    font-weight: 600;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .stop-btn {
    display: none;
    border: none;
    background: var(--red);
    color: #FFFFFF;
    font-size: 12px;
    font-weight: 600;
    padding: 5px 12px;
    border-radius: 99px;
    cursor: pointer;
    flex-shrink: 0;
    transition: transform 0.16s var(--spring);
  }
  .live-card.running .stop-btn { display: inline-flex; align-items: center; gap: 4px; }
  .stop-btn:active { transform: scale(0.92); }

  .progress-wrap {
    margin-top: 11px;
    display: none;
    align-items: center;
    gap: 10px;
  }
  .live-card.running .progress-wrap { display: flex; }
  .progress-track {
    flex: 1;
    height: 5px;
    background: rgba(255, 255, 255, 0.16);
    border-radius: 99px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    width: 0%;
    border-radius: 99px;
    background: linear-gradient(90deg, #0A84FF, #30D158);
    transition: width 0.35s var(--spring);
  }
  .progress-pct {
    font-size: 11px;
    font-weight: 600;
    color: rgba(255, 255, 255, 0.85);
    min-width: 32px;
    text-align: right;
  }

  .live-meta {
    margin-top: 10px;
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .meta-pill {
    flex: 1;
    min-width: 140px;
    background: var(--pill);
    border-radius: 10px;
    padding: 6px 10px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 11px;
  }
  .live-card.running .meta-pill {
    background: rgba(255, 255, 255, 0.1);
    color: rgba(255, 255, 255, 0.88);
  }
  .meta-pill span:first-child { color: var(--sub); }
  .live-card.running .meta-pill span:first-child { color: rgba(255, 255, 255, 0.6); }
  .meta-val { font-weight: 600; font-variant-numeric: tabular-nums; }

  .error-banner {
    display: none;
    margin-top: 10px;
    padding: 8px 11px;
    border-radius: 11px;
    background: rgba(255, 59, 48, 0.12);
    color: var(--red);
    font-size: 12px;
    font-weight: 500;
    word-break: break-word;
  }
  .login-banner {
    display: none;
    margin-top: 10px;
    padding: 8px 11px;
    border-radius: 11px;
    background: rgba(0, 122, 255, 0.18);
    color: #5AC8FA;
    font-size: 12px;
    font-weight: 600;
  }

  /* 控制中心三大触感按钮 */
  .cc-grid {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
  }
  .cc-btn {
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--text);
    border-radius: 18px;
    padding: 13px 10px 12px;
    box-shadow: var(--shadow);
    cursor: pointer;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 7px;
    text-align: left;
    transition: transform 0.18s var(--spring), background 0.25s, opacity 0.2s, box-shadow 0.25s;
  }
  .cc-btn:active:not(:disabled) { transform: scale(0.95); }
  .cc-btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .cc-ico {
    width: 34px;
    height: 34px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 15px;
    color: #FFFFFF;
  }
  .cc-ico.blue { background: var(--blue); }
  .cc-ico.orange { background: var(--orange); }
  .cc-ico.green { background: var(--green); }
  .cc-title { font-size: 13px; font-weight: 700; }
  .cc-sub { font-size: 10.5px; color: var(--sub); line-height: 1.2; }

  .cc-btn.schedule-on {
    background: linear-gradient(135deg, #28CD41 0%, #34C759 100%);
    color: #FFFFFF;
    border-color: transparent;
    box-shadow: 0 6px 18px rgba(52, 199, 89, 0.35);
  }
  .cc-btn.schedule-on .cc-ico {
    background: rgba(255, 255, 255, 0.25);
    animation: pulse-ring 2s infinite;
  }
  .cc-btn.schedule-on .cc-sub { color: rgba(255, 255, 255, 0.88); }
  @keyframes pulse-ring {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.07); }
  }

  /* iOS 分组列表 */
  .section-head {
    padding: 0 12px 4px;
    font-size: 12px;
    font-weight: 600;
    color: var(--sub);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .ios-group {
    background: var(--card);
    border-radius: 16px;
    box-shadow: var(--shadow);
    border: 0.5px solid var(--sep);
    overflow: hidden;
  }
  .ios-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 11px 14px;
    min-height: 46px;
    position: relative;
  }
  .ios-row + .ios-row::before {
    content: "";
    position: absolute;
    left: 14px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .row-col {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }
  .row-label {
    font-size: 14px;
    font-weight: 500;
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .row-desc {
    font-size: 11.5px;
    color: var(--sub);
  }

  /* 滑块分段控制器 */
  .segmented {
    position: relative;
    display: grid;
    grid-template-columns: 1fr 1fr;
    background: var(--pill);
    border-radius: 10px;
    padding: 2.5px;
    min-width: 176px;
  }
  .seg-thumb {
    position: absolute;
    top: 2.5px;
    bottom: 2.5px;
    left: 2.5px;
    width: calc(50% - 2.5px);
    background: var(--card);
    border-radius: 8px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
    transition: transform 0.25s var(--spring);
  }
  [data-theme="dark"] .seg-thumb { background: #3A3A3C; }
  .seg-btn {
    position: relative;
    z-index: 1;
    border: none;
    background: transparent;
    color: var(--text);
    font-size: 12.5px;
    font-weight: 600;
    padding: 5px 10px;
    cursor: pointer;
    text-align: center;
  }

  /* 苹果弹簧开关 51x31 */
  .ios-switch {
    position: relative;
    display: inline-block;
    width: 51px;
    height: 31px;
    flex-shrink: 0;
  }
  .ios-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }
  .ios-slider {
    position: absolute;
    inset: 0;
    cursor: pointer;
    background: var(--switch-off);
    border-radius: 31px;
    transition: background 0.25s var(--spring);
  }
  .ios-slider::before {
    content: "";
    position: absolute;
    height: 27px;
    width: 27px;
    left: 2px;
    top: 2px;
    background: #FFFFFF;
    border-radius: 50%;
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.2);
    transition: transform 0.26s var(--spring);
  }
  .ios-switch input:checked + .ios-slider { background: var(--green); }
  .ios-switch input:checked + .ios-slider::before { transform: translateX(20px); }

  /* 时间胶囊 & 间隔选择 */
  .slot-block {
    padding: 12px 14px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    position: relative;
  }
  .slot-block + .slot-block::before {
    content: "";
    position: absolute;
    left: 14px;
    right: 0;
    top: 0;
    height: 0.5px;
    background: var(--sep);
  }
  .slot-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .slot-body {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    transition: opacity 0.2s;
  }
  .slot-body.disabled {
    opacity: 0.42;
    pointer-events: none;
  }
  .time-range {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .time-pill {
    border: none;
    outline: none;
    background: var(--pill-blue);
    color: var(--blue);
    font-family: inherit;
    font-size: 13.5px;
    font-weight: 700;
    padding: 5px 9px;
    border-radius: 9px;
    cursor: pointer;
    font-variant-numeric: tabular-nums;
  }
  .gap-wrap {
    display: flex;
    align-items: center;
    gap: 4px;
    font-size: 12px;
    color: var(--sub);
  }
  .gap-select {
    border: none;
    outline: none;
    background: var(--pill);
    color: var(--text);
    font-family: inherit;
    font-size: 12.5px;
    font-weight: 600;
    padding: 5px 7px;
    border-radius: 8px;
    cursor: pointer;
  }
  .overlap-tip {
    display: none;
    padding: 8px 14px;
    background: rgba(255, 149, 0, 0.1);
    color: var(--orange);
    font-size: 11.5px;
    line-height: 1.4;
    border-top: 0.5px solid var(--sep);
  }

  /* 输入框 */
  .field-wrap {
    display: flex;
    align-items: center;
    gap: 6px;
    background: var(--pill);
    border-radius: 10px;
    padding: 6px 10px;
    width: 100%;
    margin-top: 6px;
  }
  .field-wrap input {
    border: none;
    outline: none;
    background: transparent;
    color: var(--text);
    font-family: inherit;
    font-size: 13.5px;
    width: 100%;
    user-select: text;
  }
  .clear-btn {
    border: none;
    background: rgba(142, 142, 147, 0.35);
    color: #FFFFFF;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    font-size: 11px;
    line-height: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    flex-shrink: 0;
  }
  .kw-preview {
    margin-top: 6px;
    font-size: 11.5px;
    color: var(--blue);
    background: var(--pill-blue);
    border-radius: 8px;
    padding: 5px 9px;
    line-height: 1.35;
  }
  .msg-preview {
    margin-top: 6px;
    font-size: 11.5px;
    color: var(--sub);
  }

  /* 手风琴折叠面板 */
  .accordion-body {
    max-height: 0;
    overflow: hidden;
    transition: max-height 0.32s var(--spring);
  }
  .accordion-body.open {
    max-height: 460px;
  }
  .chevron {
    font-size: 12px;
    color: var(--sub);
    transition: transform 0.25s var(--spring);
  }
  .chevron.open { transform: rotate(90deg); }

  /* 日志卡 */
  .log-tools {
    display: flex;
    gap: 6px;
  }
  .mini-pill-btn {
    border: none;
    background: var(--pill);
    color: var(--blue);
    font-size: 11.5px;
    font-weight: 600;
    padding: 3px 9px;
    border-radius: 99px;
    cursor: pointer;
  }
  .mini-pill-btn.active { background: var(--blue); color: #FFFFFF; }
  .log-box {
    background: var(--log-bg);
    padding: 10px 12px;
    height: 185px;
    overflow-y: auto;
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 11.5px;
    line-height: 1.5;
    user-select: text;
  }
  .log-line { word-break: break-all; margin-bottom: 2px; }
  .log-time { color: var(--sub); margin-right: 6px; }
  .log-ok { color: var(--green); }
  .log-err { color: var(--red); font-weight: 600; }
  .log-warn { color: var(--orange); }
  .log-head { color: var(--blue); font-weight: 600; }

  /* 恢复默认设置 */
  .reset-btn {
    width: 100%;
    border: 0.5px solid var(--sep);
    background: var(--card);
    color: var(--red);
    font-size: 14px;
    font-weight: 600;
    padding: 13px;
    border-radius: 16px;
    box-shadow: var(--shadow);
    cursor: pointer;
    transition: transform 0.16s var(--spring);
  }
  .reset-btn:active { transform: scale(0.97); }

  /* iOS 毛玻璃 Alert 弹窗 */
  .modal-mask {
    position: fixed;
    inset: 0;
    z-index: 90;
    background: rgba(0, 0, 0, 0.35);
    display: none;
    align-items: center;
    justify-content: center;
    padding: 24px;
  }
  .modal-mask.show { display: flex; }
  .ios-alert {
    width: 290px;
    background: var(--sheet-bg);
    border-radius: 16px;
    overflow: hidden;
    box-shadow: 0 20px 50px rgba(0, 0, 0, 0.25);
    border: 0.5px solid var(--sep);
    animation: pop-in 0.22s var(--spring);
  }
  @keyframes pop-in {
    from { opacity: 0; transform: scale(1.08); }
    to { opacity: 1; transform: scale(1); }
  }
  .alert-body {
    padding: 18px 16px 15px;
    text-align: center;
  }
  .alert-title { font-size: 16px; font-weight: 700; margin-bottom: 6px; }
  .alert-msg { font-size: 13px; color: var(--text-2); line-height: 1.4; }
  .alert-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    border-top: 0.5px solid var(--sep);
  }
  .alert-actions.single { grid-template-columns: 1fr; }
  .alert-btn {
    border: none;
    background: transparent;
    padding: 12px 8px;
    font-size: 15px;
    font-weight: 500;
    color: var(--blue);
    cursor: pointer;
  }
  .alert-btn + .alert-btn { border-left: 0.5px solid var(--sep); }
  .alert-btn.bold { font-weight: 700; }
  .alert-btn.danger { color: var(--red); font-weight: 700; }

  /* 顶部 Toast */
  .toast {
    position: fixed;
    top: 14px;
    left: 50%;
    transform: translate(-50%, -70px);
    z-index: 100;
    background: rgba(28, 28, 30, 0.92);
    color: #FFFFFF;
    padding: 8px 16px;
    border-radius: 99px;
    font-size: 12.5px;
    font-weight: 600;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    transition: transform 0.3s var(--spring), opacity 0.25s;
    opacity: 0;
    pointer-events: none;
  }
  .toast.show { transform: translate(-50%, 0); opacity: 1; }
</style>
</head>
<body>
  <div class="toast" id="toast">已保存</div>

  <header class="nav">
    <div class="nav-left">
      <svg class="app-icon" viewBox="0 0 64 64" fill="none">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="0" y2="64" gradientUnits="userSpaceOnUse">
            <stop offset="0%" stop-color="#007AFF"/>
            <stop offset="100%" stop-color="#FF9500"/>
          </linearGradient>
        </defs>
        <rect width="64" height="64" rx="16" fill="url(#g)"/>
        <rect x="14" y="34" width="9" height="16" rx="3" fill="white" fill-opacity="0.95"/>
        <rect x="27.5" y="24" width="9" height="26" rx="3" fill="white" fill-opacity="0.95"/>
        <rect x="41" y="14" width="9" height="36" rx="3" fill="white" fill-opacity="0.95"/>
      </svg>
      <div class="app-title-wrap">
        <div class="app-title">
          <span>暗号表助手·迷你版</span>
          <span class="ver-badge" id="verBadge">v1.0.0</span>
        </div>
        <div class="app-sub">轻量极速 · 零配置负担 · 独立并存</div>
      </div>
    </div>
    <div class="nav-actions">
      <button class="icon-pill-btn" id="updateBtn" title="检查更新" style="display:none;">
        <span>⬇</span><span class="update-dot" id="updateDot"></span>
      </button>
      <button class="icon-pill-btn" id="themeBtn" title="切换浅色/深色外观">🌓</button>
    </div>
  </header>

  <main class="container">
    <!-- 灵动岛实时活动卡 -->
    <section class="live-card" id="liveCard">
      <div class="live-top">
        <div class="live-status">
          <div class="status-dot" id="statusDot"></div>
          <div class="ios-spinner" id="iosSpinner">
            <div style="transform:rotate(0deg);animation-delay:-0.9s"></div>
            <div style="transform:rotate(30deg);animation-delay:-0.825s"></div>
            <div style="transform:rotate(60deg);animation-delay:-0.75s"></div>
            <div style="transform:rotate(90deg);animation-delay:-0.675s"></div>
            <div style="transform:rotate(120deg);animation-delay:-0.6s"></div>
            <div style="transform:rotate(150deg);animation-delay:-0.525s"></div>
            <div style="transform:rotate(180deg);animation-delay:-0.45s"></div>
            <div style="transform:rotate(210deg);animation-delay:-0.375s"></div>
            <div style="transform:rotate(240deg);animation-delay:-0.3s"></div>
            <div style="transform:rotate(270deg);animation-delay:-0.225s"></div>
            <div style="transform:rotate(300deg);animation-delay:-0.15s"></div>
            <div style="transform:rotate(330deg);animation-delay:-0.075s"></div>
          </div>
          <div class="step-title" id="stepText">空闲中，等待开始</div>
        </div>
        <button class="stop-btn" id="stopBtn">■ 停止</button>
      </div>
      <div class="progress-wrap">
        <div class="progress-track"><div class="progress-fill" id="progressFill"></div></div>
        <div class="progress-pct" id="progressPct">0%</div>
      </div>
      <div class="live-meta">
        <div class="meta-pill"><span>上次完成</span><span class="meta-val" id="lastRun">—</span></div>
        <div class="meta-pill"><span>下次自动执行</span><span class="meta-val" id="nextRun">—</span></div>
      </div>
      <div class="login-banner" id="loginBanner">⏳ 请在弹出的浏览器里扫码登录 WPS，完成后将自动继续…</div>
      <div class="error-banner" id="errorBanner"></div>
    </section>

    <!-- 控制中心三大触感按钮 -->
    <section class="cc-grid">
      <button class="cc-btn" id="btnNow">
        <div class="cc-ico blue">▶</div>
        <div class="cc-title">立即跑一次</div>
        <div class="cc-sub">下载 · 填表 · 发群</div>
      </button>
      <button class="cc-btn" id="btnCapture">
        <div class="cc-ico orange">▣</div>
        <div class="cc-title">仅截图发送</div>
        <div class="cc-sub">汇总表截图发钉钉</div>
      </button>
      <button class="cc-btn" id="btnSchedule">
        <div class="cc-ico green">◷</div>
        <div class="cc-title" id="schedTitle">自动值守</div>
        <div class="cc-sub" id="schedSub">点击开启定时计划</div>
      </button>
    </section>

    <!-- 分组1：自动值守计划 -->
    <div>
      <div class="section-head"><span>自动值守计划</span><span>改完立即生效</span></div>
      <div class="ios-group">
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">📅 运行日期</div>
            <div class="row-desc">仅限制自动值守，手动触发不受影响</div>
          </div>
          <div class="segmented" id="dayModeSeg">
            <div class="seg-thumb" id="dayModeThumb"></div>
            <button class="seg-btn" data-val="daily">每天运行</button>
            <button class="seg-btn" data-val="workday">仅工作日</button>
          </div>
        </div>

        <div class="slot-block">
          <div class="slot-head">
            <span class="row-label">☀️ 白天时段</span>
            <span class="row-desc" id="daySummary"></span>
          </div>
          <div class="slot-body">
            <div class="time-range">
              <input type="time" class="time-pill" id="dayStart" />
              <span style="color:var(--sub)">至</span>
              <input type="time" class="time-pill" id="dayEnd" />
            </div>
            <div class="gap-wrap">
              <span>每隔</span>
              <select class="gap-select" id="dayIh"></select>
              <span>时</span>
              <select class="gap-select" id="dayIm"></select>
              <span>分</span>
            </div>
          </div>
        </div>

        <div class="slot-block">
          <div class="slot-head">
            <span class="row-label">🌙 晚上时段</span>
            <label class="ios-switch"><input type="checkbox" id="nightEnabled" /><span class="ios-slider"></span></label>
          </div>
          <div class="slot-body" id="nightBody">
            <div class="time-range">
              <input type="time" class="time-pill" id="nightStart" />
              <span style="color:var(--sub)">至</span>
              <input type="time" class="time-pill" id="nightEnd" />
            </div>
            <div class="gap-wrap">
              <span>每隔</span>
              <select class="gap-select" id="nightIh"></select>
              <span>时</span>
              <select class="gap-select" id="nightIm"></select>
              <span>分</span>
            </div>
          </div>
        </div>
        <div class="overlap-tip" id="overlapTip">
          ⚠️ 白天与晚上时段存在重叠，系统会按统一的间隔节拍运行，交界处不会重复连跑。
        </div>
      </div>
    </div>

    <!-- 分组2：截图与钉钉投递 -->
    <div>
      <div class="section-head"><span>截图与钉钉投递</span></div>
      <div class="ios-group">
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">🖼 复制数据汇总图片</div>
            <div class="row-desc">写入在线表后选中 C6:AJ 并高清复制</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="enableCapture" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">💬 发送到钉钉会话</div>
            <div class="row-desc">自动检索目标群聊/会话并粘贴图片</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="enableDingtalk" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">🧹 发送前清空输入框</div>
            <div class="row-desc">粘贴前自动清空框内残留草稿，防止误发</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="dtClearBeforeSend" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">🔕 默认设为静音发送</div>
            <div class="row-desc">自动展开「发送」右侧菜单勾选「设为静音」</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="dtSilentSend" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">⚡ 粘贴后自动按 Enter 发送</div>
            <div class="row-desc">默认关闭：贴好图文并挂上「静音发送」后停在输入框待你确认</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="dtAutoSend" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row" style="flex-direction:column;align-items:stretch;">
          <div class="row-label">🎯 目标群聊 / 会话名称（多个用逗号隔开）</div>
          <div class="field-wrap">
            <input type="text" id="dtSearch" placeholder="例如：文件小助手 或 数据大群,汇报群" />
            <button class="clear-btn" id="clearSearchBtn" title="清空">✕</button>
          </div>
          <div class="kw-preview" id="kwPreview"></div>
        </div>
        <div class="ios-row" style="flex-direction:column;align-items:stretch;">
          <div class="row-label">🏷 消息时间后缀</div>
          <div class="field-wrap">
            <input type="text" id="dtSuffix" placeholder="例如：数据情况" />
          </div>
          <div class="msg-preview" id="msgPreview"></div>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">⏳ 稳态等待秒数（复制 / 贴前 / 发前）</div>
            <div class="row-desc">复制为图片后默认等 5 秒防切太快丢失</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center;">
            <input type="number" id="wpsPostCopyWaitSec" min="0" max="15" style="width:52px;padding:6px;border-radius:8px;border:1px solid var(--sep);background:var(--card-soft);color:var(--text);text-align:center;" title="复制为图片后等待秒数" />
            <input type="number" id="dtPasteDelaySec" min="0" max="15" style="width:52px;padding:6px;border-radius:8px;border:1px solid var(--sep);background:var(--card-soft);color:var(--text);text-align:center;" title="切入钉钉会话后粘贴前等待秒数" />
            <input type="number" id="dtSendWaitSec" min="0" max="20" style="width:52px;padding:6px;border-radius:8px;border:1px solid var(--sep);background:var(--card-soft);color:var(--text);text-align:center;" title="自动按Enter前等待秒数" />
          </div>
        </div>
      </div>
    </div>

    <!-- 分组2.5：轻量写表与系统保护 -->
    <div>
      <div class="section-head"><span>格位写入与系统保护</span></div>
      <div class="ios-group">
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">📅 自动填写 D3 查询日期</div>
            <div class="row-desc">写入日期并自动清空 D4/E4/D5</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="writeD3Date" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row" id="d3OffsetRow">
          <span class="row-label">D3 查询哪天</span>
          <div class="segmented" id="d3OffsetSeg">
            <div class="seg-thumb" id="d3OffsetThumb"></div>
            <button class="seg-btn" data-val="0">今天</button>
            <button class="seg-btn" data-val="-1">昨天</button>
            <button class="seg-btn" data-val="-2">前天</button>
          </div>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">🕒 贴表后在 R4 写更新时间</div>
            <div class="row-desc">完整记录三表粘贴完成的精确时刻</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="writeR4Time" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">🛡️ 值守期间防电脑休眠息屏</div>
            <div class="row-desc">避免锁屏导致自动任务和粘贴失效</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="preventSleep" /><span class="ios-slider"></span></label>
        </div>
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">📄 跑完自动弹窗打开 WPS</div>
            <div class="row-desc">默认关闭，保持后台静默不打扰工作</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="openWpsAfterRun" /><span class="ios-slider"></span></label>
        </div>
      </div>
    </div>

    <!-- 分组3：发群时段限制（手风琴） -->
    <div>
      <div class="section-head"><span>发群时段限制</span></div>
      <div class="ios-group">
        <div class="ios-row">
          <div class="row-col">
            <div class="row-label">🕒 仅在允许时段截图发群</div>
            <div class="row-desc">关闭时全天每次均发群；手动点击始终发群</div>
          </div>
          <label class="ios-switch"><input type="checkbox" id="cpEnabled" /><span class="ios-slider"></span></label>
        </div>
        <div class="accordion-body" id="cpAccordion">
          <div class="ios-row">
            <span class="row-label">适用日期配置</span>
            <div class="segmented" id="cpProfileSeg">
              <div class="seg-thumb" id="cpProfileThumb"></div>
              <button class="seg-btn" data-val="workday">工作日</button>
              <button class="seg-btn" data-val="weekend">非工作日</button>
            </div>
          </div>
          <div class="slot-block">
            <div class="slot-head">
              <span class="row-label">时段 1</span>
              <label class="ios-switch"><input type="checkbox" id="cpSlot1" /><span class="ios-slider"></span></label>
            </div>
            <div class="slot-body" id="cpSlot1Body">
              <div class="time-range">
                <input type="time" class="time-pill" id="cpStart1" />
                <span style="color:var(--sub)">至</span>
                <input type="time" class="time-pill" id="cpEnd1" />
              </div>
            </div>
          </div>
          <div class="slot-block">
            <div class="slot-head">
              <span class="row-label">时段 2</span>
              <label class="ios-switch"><input type="checkbox" id="cpSlot2" /><span class="ios-slider"></span></label>
            </div>
            <div class="slot-body" id="cpSlot2Body">
              <div class="time-range">
                <input type="time" class="time-pill" id="cpStart2" />
                <span style="color:var(--sub)">至</span>
                <input type="time" class="time-pill" id="cpEnd2" />
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 分组4：数据源与连接设置（手风琴） -->
    <div>
      <div class="section-head"><span>高级设置</span></div>
      <div class="ios-group">
        <div class="ios-row" id="connToggle" style="cursor:pointer;">
          <div class="row-col">
            <div class="row-label">🔗 数据源与后台账号</div>
            <div class="row-desc">修改 WPS 表格链接或后台登录凭据</div>
          </div>
          <span class="chevron" id="connChevron">❯</span>
        </div>
        <div class="accordion-body" id="connAccordion">
          <div class="ios-row" style="flex-direction:column;align-items:stretch;">
            <div class="row-label">WPS 在线表格链接</div>
            <div class="field-wrap"><input type="text" id="wpsUrl" placeholder="请粘贴您的金山文档在线表格链接，如 https://www.kdocs.cn/l/..." /></div>
          </div>
          <div class="ios-row" style="flex-direction:column;align-items:stretch;">
            <div class="row-label">Luya 后台账号</div>
            <div class="field-wrap"><input type="text" id="luyaUser" placeholder="请填写您的后台登录邮箱/账号" /></div>
          </div>
          <div class="ios-row" style="flex-direction:column;align-items:stretch;">
            <div class="row-label">Luya 后台密码</div>
            <div class="field-wrap">
              <input type="password" id="luyaPass" placeholder="请填写您的后台登录密码" />
              <button class="mini-pill-btn" id="togglePassBtn">显示</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 分组5：运行日志 -->
    <div>
      <div class="section-head">
        <span>运行日志</span>
        <div class="log-tools">
          <button class="mini-pill-btn active" id="autoScrollBtn">自动滚底</button>
          <button class="mini-pill-btn" id="copyLogBtn">复制</button>
          <button class="mini-pill-btn" id="clearLogBtn">清空</button>
        </div>
      </div>
      <div class="ios-group">
        <div class="log-box" id="logBox"></div>
      </div>
    </div>

    <button class="reset-btn" id="resetBtn">恢复默认设置</button>
  </main>

  <!-- iOS 毛玻璃模态弹窗 -->
  <div class="modal-mask" id="modalMask">
    <div class="ios-alert">
      <div class="alert-body">
        <div class="alert-title" id="alertTitle">提示</div>
        <div class="alert-msg" id="alertMsg"></div>
      </div>
      <div class="alert-actions" id="alertActions">
        <button class="alert-btn" id="alertCancel">取消</button>
        <button class="alert-btn bold" id="alertOk">确定</button>
      </div>
    </div>
  </div>

<script>
const TOKEN = __TOKEN_JSON__;
const $ = (id) => document.getElementById(id);

let cfg = {};
let cpProfile = "workday";
let autoScroll = true;
let lastLogSeq = 0;
let saveTimer = null;
let updateUrl = "";

function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove("show"), 1800);
}

async function api(path, method = "GET", body = null) {
  const opts = {
    method,
    headers: { "X-Token": TOKEN, "Content-Type": "application/json" }
  };
  if (body !== null) opts.body = JSON.stringify(body);
  const res = await fetch(path, opts);
  return await res.json();
}

function iosAlert(title, msg, { okText = "好", cancelText = null, danger = false } = {}) {
  return new Promise((resolve) => {
    $("alertTitle").textContent = title;
    $("alertMsg").textContent = msg;
    const actions = $("alertActions");
    const cancelBtn = $("alertCancel");
    const okBtn = $("alertOk");
    okBtn.textContent = okText;
    okBtn.className = "alert-btn " + (danger ? "danger" : "bold");
    if (cancelText) {
      actions.classList.remove("single");
      cancelBtn.style.display = "block";
      cancelBtn.textContent = cancelText;
    } else {
      actions.classList.add("single");
      cancelBtn.style.display = "none";
    }
    const mask = $("modalMask");
    mask.classList.add("show");
    const done = (v) => {
      mask.classList.remove("show");
      resolve(v);
    };
    okBtn.onclick = () => done(true);
    cancelBtn.onclick = () => done(false);
  });
}

function initSelectOptions() {
  for (const [hid, mid] of [["dayIh", "dayIm"], ["nightIh", "nightIm"]]) {
    const hSel = $(hid), mSel = $(mid);
    for (let i = 0; i <= 12; i++) {
      hSel.add(new Option(String(i).padStart(2, "0"), String(i)));
    }
    for (let i = 0; i < 60; i++) {
      mSel.add(new Option(String(i).padStart(2, "0"), String(i)));
    }
  }
}

function pad2(n) {
  return String(Number(n) || 0).padStart(2, "0");
}

function hmStr(h, m) {
  return `${pad2(h)}:${pad2(m)}`;
}

function parseHm(str, defH = 9, defM = 0) {
  const parts = String(str || "").split(":");
  if (parts.length !== 2) return [defH, defM];
  return [Math.max(0, Math.min(23, parseInt(parts[0], 10) || 0)),
          Math.max(0, Math.min(59, parseInt(parts[1], 10) || 0))];
}

function slotMinutes(s) {
  const [h, m] = parseHm(s);
  return h * 60 + m;
}

function slotsOverlap(s1, e1, s2, e2) {
  const toSegments = (a, b) => a <= b ? [[a, b]] : [[a, 1440], [0, b]];
  const segA = toSegments(slotMinutes(s1), slotMinutes(e1));
  const segB = toSegments(slotMinutes(s2), slotMinutes(e2));
  for (const [a1, a2] of segA) {
    for (const [b1, b2] of segB) {
      if (Math.max(a1, b1) < Math.min(a2, b2)) return true;
    }
  }
  return false;
}

function updateKeywordPreview() {
  const raw = ($("dtSearch").value || "").trim();
  if (!raw) {
    $("kwPreview").textContent = "将默认搜索：文件小助手";
    return;
  }
  const list = raw.split(/[\r\n,;，；]+/).map((s) => s.trim()).filter(Boolean);
  const tgts = [...new Set(list.length ? list : ["文件小助手"])];
  if (tgts.length > 1) {
    const segs = tgts.slice(0, 4).map((t) => {
      const toks = t.match(/[\u4e00-\u9fffA-Za-z0-9]+/g) || [];
      const best = [...toks].sort((a, b) => b.length - a.length)[0] || t;
      return `${t}(${best})`;
    });
    $("kwPreview").textContent = `已配置 ${tgts.length} 个群/联系人连发：${segs.join("、")}${tgts.length > 4 ? "…" : ""}`;
    return;
  }
  const tokens = tgts[0].match(/[\u4e00-\u9fffA-Za-z0-9]+/g) || [];
  if (tokens.length === 0) {
    $("kwPreview").textContent = `搜索词：${tgts[0]}`;
    return;
  }
  const ordered = [...tokens].sort((a, b) => b.length - a.length);
  const best = ordered.length >= 2 && `${ordered[0]} ${ordered[1]}`.length <= 14
    ? `${ordered[0]} ${ordered[1]}` : ordered[0];
  $("kwPreview").textContent = `智能检索策略：优先用纯文字词「${best}」精准搜群，自动绕开 Emoji 干扰`;
}

function updateMsgPreview() {
  const suffix = ($("dtSuffix").value || "").trim();
  const now = new Date();
  const stamp = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())} ${pad2(now.getHours())}:${pad2(now.getMinutes())}`;
  $("msgPreview").textContent = `消息预览：${stamp}${suffix ? " " + suffix : ""}`;
}

function updateSlotSummaries() {
  const ds = $("dayStart").value || "09:00";
  const de = $("dayEnd").value || "18:00";
  const dg = Number($("dayIh").value) * 60 + Number($("dayIm").value);
  $("daySummary").textContent = `${ds}–${de} · 每 ${dg} 分钟`;

  const nOn = $("nightEnabled").checked;
  $("nightBody").classList.toggle("disabled", !nOn);

  const ns = $("nightStart").value || "18:00";
  const ne = $("nightEnd").value || "23:00";
  $("overlapTip").style.display = (nOn && slotsOverlap(ds, de, ns, ne)) ? "block" : "none";
}

function applySegmented(segId, thumbId, value) {
  const seg = $(segId);
  const btns = [...seg.querySelectorAll(".seg-btn")];
  const idx = Math.max(0, btns.findIndex((b) => b.dataset.val === value));
  $(thumbId).style.transform = `translateX(${idx * 100}%)`;
}

function renderCaptureProfile() {
  const cp = (cfg.capture_plan && cfg.capture_plan[cpProfile]) || {};
  $("cpSlot1").checked = Boolean(cp.slot1);
  $("cpStart1").value = cp.start1 || "09:00";
  $("cpEnd1").value = cp.end1 || "18:00";
  $("cpSlot1Body").classList.toggle("disabled", !$("cpSlot1").checked);

  $("cpSlot2").checked = Boolean(cp.slot2);
  $("cpStart2").value = cp.start2 || "18:00";
  $("cpEnd2").value = cp.end2 || "22:00";
  $("cpSlot2Body").classList.toggle("disabled", !$("cpSlot2").checked);
  applySegmented("cpProfileSeg", "cpProfileThumb", cpProfile);
}

function renderConfig() {
  applySegmented("dayModeSeg", "dayModeThumb", cfg.day_mode || "daily");
  $("dayStart").value = hmStr(cfg.day.sh, cfg.day.sm);
  $("dayEnd").value = hmStr(cfg.day.eh, cfg.day.em);
  $("dayIh").value = String(cfg.day.ih);
  $("dayIm").value = String(cfg.day.im);

  $("nightEnabled").checked = cfg.night_enabled !== false;
  $("nightStart").value = hmStr(cfg.night.sh, cfg.night.sm);
  $("nightEnd").value = hmStr(cfg.night.eh, cfg.night.em);
  $("nightIh").value = String(cfg.night.ih);
  $("nightIm").value = String(cfg.night.im);

  $("enableCapture").checked = cfg.enable_capture !== false;
  $("enableDingtalk").checked = Boolean(cfg.enable_dingtalk);
  $("dtClearBeforeSend").checked = cfg.dt_clear_before_send !== false;
  $("dtSilentSend").checked = cfg.dt_silent_send !== false;
  $("dtAutoSend").checked = cfg.dt_auto_send === true;
  $("dtSearch").value = cfg.dt_search || "";
  $("dtSuffix").value = cfg.dt_suffix || "";

  const cpOn = Boolean(cfg.capture_plan && cfg.capture_plan.enabled);
  $("cpEnabled").checked = cpOn;
  $("cpAccordion").classList.toggle("open", cpOn);
  renderCaptureProfile();

  $("wpsPostCopyWaitSec").value = String(cfg.wps_post_copy_wait_sec ?? 5);
  $("dtPasteDelaySec").value = String(cfg.dt_paste_delay_sec ?? 2);
  $("dtSendWaitSec").value = String(cfg.dt_send_wait_sec ?? 5);
  $("writeD3Date").checked = Boolean(cfg.write_d3_date);
  $("d3OffsetRow").style.display = $("writeD3Date").checked ? "flex" : "none";
  applySegmented("d3OffsetSeg", "d3OffsetThumb", String(cfg.d3_day_offset ?? 0));
  $("writeR4Time").checked = cfg.write_r4_time !== false;
  $("preventSleep").checked = cfg.prevent_sleep !== false;
  $("openWpsAfterRun").checked = Boolean(cfg.open_wps_after_run);

  $("wpsUrl").value = cfg.wps_url || "";
  $("luyaUser").value = cfg.luya_user || "";
  $("luyaPass").value = cfg.luya_pass || "";

  updateSlotSummaries();
  updateKeywordPreview();
  updateMsgPreview();
}

function collectConfigFromDOM() {
  const [dsh, dsm] = parseHm($("dayStart").value, 9, 0);
  const [deh, dem] = parseHm($("dayEnd").value, 18, 0);
  const [nsh, nsm] = parseHm($("nightStart").value, 18, 0);
  const [neh, nem] = parseHm($("nightEnd").value, 23, 0);

  cfg.day = {
    sh: dsh, sm: dsm, eh: deh, em: dem,
    ih: Number($("dayIh").value), im: Number($("dayIm").value),
  };
  cfg.night = {
    sh: nsh, sm: nsm, eh: neh, em: nem,
    ih: Number($("nightIh").value), im: Number($("nightIm").value),
  };
  cfg.night_enabled = $("nightEnabled").checked;
  cfg.enable_capture = $("enableCapture").checked;
  cfg.enable_dingtalk = $("enableDingtalk").checked;
  cfg.dt_clear_before_send = $("dtClearBeforeSend").checked;
  cfg.dt_silent_send = $("dtSilentSend").checked;
  cfg.dt_auto_send = $("dtAutoSend").checked;
  cfg.dt_search = $("dtSearch").value.trim() || "文件小助手";
  cfg.dt_suffix = $("dtSuffix").value.trim();
  cfg.wps_post_copy_wait_sec = Math.max(0, Math.min(15, Number($("wpsPostCopyWaitSec").value || 5)));
  cfg.dt_paste_delay_sec = Math.max(0, Math.min(15, Number($("dtPasteDelaySec").value || 2)));
  cfg.dt_send_wait_sec = Math.max(0, Math.min(20, Number($("dtSendWaitSec").value || 5)));
  cfg.write_d3_date = $("writeD3Date").checked;
  cfg.write_r4_time = $("writeR4Time").checked;
  cfg.prevent_sleep = $("preventSleep").checked;
  cfg.open_wps_after_run = $("openWpsAfterRun").checked;
  cfg.wps_url = $("wpsUrl").value.trim();
  cfg.luya_user = $("luyaUser").value.trim();
  cfg.luya_pass = $("luyaPass").value;

  if (!cfg.capture_plan) cfg.capture_plan = { enabled: false, workday: {}, weekend: {} };
  cfg.capture_plan.enabled = $("cpEnabled").checked;
  cfg.capture_plan[cpProfile] = {
    slot1: $("cpSlot1").checked,
    start1: $("cpStart1").value || "09:00",
    end1: $("cpEnd1").value || "18:00",
    slot2: $("cpSlot2").checked,
    start2: $("cpStart2").value || "18:00",
    end2: $("cpEnd2").value || "22:00",
  };
  return cfg;
}

function scheduleSave(immediate = false) {
  updateSlotSummaries();
  updateKeywordPreview();
  updateMsgPreview();
  clearTimeout(saveTimer);
  const runSave = async () => {
    const data = collectConfigFromDOM();
    await api("/api/config", "POST", data);
    if (immediate) showToast("设置已保存");
  };
  if (immediate) {
    runSave();
  } else {
    saveTimer = setTimeout(runSave, 350);
  }
}

function formatCountdown(nextTs, serverNow) {
  if (!nextTs || nextTs <= 0) return "";
  const diff = Math.max(0, Math.round(nextTs - serverNow));
  if (diff <= 0) return "即将执行";
  const m = Math.floor(diff / 60);
  const s = diff % 60;
  return m > 0 ? `${m}分${s}秒后` : `${s}秒后`;
}

function appendLogLines(items) {
  if (!items || !items.length) return;
  const box = $("logBox");
  const frag = document.createDocumentFragment();
  for (const item of items) {
    const div = document.createElement("div");
    div.className = "log-line";
    const t = document.createElement("span");
    t.className = "log-time";
    t.textContent = `[${item.time}]`;
    const body = document.createElement("span");
    body.className = `log-${item.level}`;
    body.textContent = item.text;
    div.appendChild(t);
    div.appendChild(body);
    frag.appendChild(div);
  }
  box.appendChild(frag);
  while (box.children.length > 500) {
    box.removeChild(box.firstChild);
  }
  if (autoScroll) {
    box.scrollTop = box.scrollHeight;
  }
}

async function pollState() {
  try {
    const st = await api(`/api/state?since=${lastLogSeq}`);
    $("verBadge").textContent = `v${st.version}`;
    const card = $("liveCard");
    card.classList.toggle("running", Boolean(st.busy));

    const dot = $("statusDot");
    dot.className = "status-dot" + (st.status_level === "err" ? " err" : st.status_level === "stopped" ? " stopped" : "");

    $("stepText").textContent = st.step_text || "空闲中";
    const pct = Math.round((st.progress || 0) * 100);
    $("progressFill").style.width = `${pct}%`;
    $("progressPct").textContent = `${pct}%`;

    $("lastRun").textContent = st.last_run_str || "—";
    const cd = st.schedule_running ? formatCountdown(st.next_run_ts, st.server_now) : "";
    $("nextRun").textContent = st.next_run_str && st.next_run_str !== "—" && cd
      ? `${st.next_run_str} (${cd})` : (st.next_run_str || "—");

    $("loginBanner").style.display = st.need_login_prompt ? "block" : "none";
    const errEl = $("errorBanner");
    if (st.error_text) {
      errEl.textContent = "⚠️ " + st.error_text;
      errEl.style.display = "block";
    } else {
      errEl.style.display = "none";
    }

    $("btnNow").disabled = Boolean(st.busy);
    $("btnCapture").disabled = Boolean(st.busy);

    const schedBtn = $("btnSchedule");
    schedBtn.classList.toggle("schedule-on", Boolean(st.schedule_running));
    $("schedTitle").textContent = st.schedule_running ? "值守运行中" : "自动值守";
    $("schedSub").textContent = st.schedule_running ? "点击可停止定时计划" : "点击开启定时计划";

    if (st.log_seq < lastLogSeq) {
      $("logBox").innerHTML = "";
    }
    lastLogSeq = st.log_seq;
    appendLogLines(st.logs);

    if (st.update_info && st.update_info.url) {
      updateUrl = st.update_info.url;
      $("updateBtn").style.display = "flex";
      $("updateDot").style.display = "block";
      $("updateBtn").title = `发现新版 v${st.update_info.version}，点击下载`;
    }
  } catch (_err) {
    // 忽略瞬时刷新抖动
  }
}

function initTheme() {
  const saved = localStorage.getItem("sheetmate_mini_theme") || "light";
  document.documentElement.setAttribute("data-theme", saved);
  $("themeBtn").textContent = saved === "dark" ? "☀️" : "🌙";
  $("themeBtn").onclick = () => {
    const cur = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", cur);
    localStorage.setItem("sheetmate_mini_theme", cur);
    $("themeBtn").textContent = cur === "dark" ? "☀️" : "🌙";
  };
}

function bindEvents() {
  for (const btn of $("dayModeSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => {
      cfg.day_mode = btn.dataset.val;
      applySegmented("dayModeSeg", "dayModeThumb", cfg.day_mode);
      scheduleSave(true);
    };
  }
  for (const btn of $("cpProfileSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => {
      collectConfigFromDOM();
      cpProfile = btn.dataset.val;
      renderCaptureProfile();
    };
  }

  for (const btn of $("d3OffsetSeg").querySelectorAll(".seg-btn")) {
    btn.onclick = () => {
      cfg.d3_day_offset = Number(btn.dataset.val || 0);
      applySegmented("d3OffsetSeg", "d3OffsetThumb", String(cfg.d3_day_offset));
      scheduleSave(true);
    };
  }
  for (const id of [
    "dayStart", "dayEnd", "dayIh", "dayIm",
    "nightEnabled", "nightStart", "nightEnd", "nightIh", "nightIm",
    "enableCapture", "enableDingtalk", "dtClearBeforeSend", "dtSilentSend", "dtAutoSend",
    "wpsPostCopyWaitSec", "dtPasteDelaySec", "dtSendWaitSec",
    "writeD3Date", "writeR4Time", "preventSleep", "openWpsAfterRun",
    "cpSlot1", "cpStart1", "cpEnd1", "cpSlot2", "cpStart2", "cpEnd2"
  ]) {
    $(id).addEventListener("change", () => {
      if (id === "cpSlot1") $("cpSlot1Body").classList.toggle("disabled", !$("cpSlot1").checked);
      if (id === "cpSlot2") $("cpSlot2Body").classList.toggle("disabled", !$("cpSlot2").checked);
      if (id === "writeD3Date") $("d3OffsetRow").style.display = $("writeD3Date").checked ? "flex" : "none";
      scheduleSave(true);
    });
  }

  $("cpEnabled").addEventListener("change", () => {
    $("cpAccordion").classList.toggle("open", $("cpEnabled").checked);
    scheduleSave(true);
  });

  for (const id of ["dtSearch", "dtSuffix", "wpsUrl", "luyaUser", "luyaPass"]) {
    $(id).addEventListener("input", () => scheduleSave(false));
  }
  $("clearSearchBtn").onclick = () => {
    $("dtSearch").value = "";
    scheduleSave(true);
  };

  $("connToggle").onclick = () => {
    const acc = $("connAccordion");
    const open = !acc.classList.contains("open");
    acc.classList.toggle("open", open);
    $("connChevron").classList.toggle("open", open);
  };

  $("togglePassBtn").onclick = () => {
    const inp = $("luyaPass");
    const show = inp.type === "password";
    inp.type = show ? "text" : "password";
    $("togglePassBtn").textContent = show ? "隐藏" : "显示";
  };

  $("btnNow").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/run-now", "POST");
    showToast(r.message);
    pollState();
  };
  $("btnCapture").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/run-capture", "POST");
    showToast(r.message);
    pollState();
  };
  $("btnSchedule").onclick = async () => {
    await api("/api/config", "POST", collectConfigFromDOM());
    const r = await api("/api/schedule", "POST");
    if (!r.ok) {
      await iosAlert("无法开启计划", r.message);
      return;
    }
    showToast(r.message);
    pollState();
  };
  $("stopBtn").onclick = async () => {
    const r = await api("/api/stop", "POST");
    showToast(r.message);
    pollState();
  };

  $("autoScrollBtn").onclick = () => {
    autoScroll = !autoScroll;
    $("autoScrollBtn").classList.toggle("active", autoScroll);
  };
  $("copyLogBtn").onclick = async () => {
    try {
      await navigator.clipboard.writeText($("logBox").innerText || "");
      showToast("已复制运行日志");
    } catch (_e) {
      showToast("复制失败，请手动选中");
    }
  };
  $("clearLogBtn").onclick = async () => {
    await api("/api/clear-logs", "POST");
    $("logBox").innerHTML = "";
    lastLogSeq = 0;
    showToast("日志已清空");
  };

  $("resetBtn").onclick = async () => {
    const ok = await iosAlert(
      "恢复默认设置",
      "当前运行的时段、间隔与钉钉群设置将被重置为出厂默认值。",
      { okText: "恢复默认", cancelText: "取消", danger: true }
    );
    if (!ok) return;
    const res = await api("/api/reset", "POST");
    cfg = res.config;
    renderConfig();
    showToast("已恢复默认设置");
  };

  $("updateBtn").onclick = async () => {
    if (!updateUrl) return;
    const ok = await iosAlert("发现新版本", "检测到新版 暗号表助手·迷你版 (SheetMate-Mini)，是否前往 GitHub 下载？", {
      okText: "立即前往", cancelText: "稍后"
    });
    if (ok) window.open(updateUrl, "_blank");
  };

  window.addEventListener("pagehide", () => {
    try {
      navigator.sendBeacon(`/api/close?token=${encodeURIComponent(TOKEN)}`, "{}");
    } catch (_e) {}
  });
}

(async function bootstrap() {
  initTheme();
  initSelectOptions();
  bindEvents();
  const data = await api("/api/config");
  cfg = data.config;
  renderConfig();
  await pollState();
  setInterval(pollState, 900);
})();
</script>
</body>
</html>
"""


def _make_handler(ctrl, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def _check_auth(self, parsed):
            hdr = self.headers.get("X-Token") or ""
            qs = parse_qs(parsed.query)
            qtok = (qs.get("token") or [""])[0]
            return hdr == token or qtok == token

        def _send_sec_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send_json(self, payload, status=HTTPStatus.OK):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self._send_sec_headers()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                return None, "bad_length"
            if length <= 0:
                return {}, None
            if length > 2 * 1024 * 1024:
                return None, "too_large"
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8")), None
            except Exception:
                return None, "bad_json"

        def do_GET(self):
            parsed = urlparse(self.path)
            if not self._check_auth(parsed):
                self._send_json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return

            if parsed.path == "/":
                body = HTML_TEMPLATE.replace("__TOKEN_JSON__", json.dumps(token)).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self._send_sec_headers()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            if parsed.path == "/api/state":
                qs = parse_qs(parsed.query)
                try:
                    since = int((qs.get("since") or ["0"])[0] or 0)
                except Exception:
                    since = 0
                self._send_json(ctrl.get_state(since=since))
                return

            if parsed.path == "/api/config":
                self._send_json({"ok": True, "config": ctrl.get_config()})
                return

            self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            parsed = urlparse(self.path)
            if not self._check_auth(parsed):
                self._send_json({"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return

            if parsed.path == "/api/config":
                body, r_err = self._read_json()
                if r_err == "too_large":
                    self._send_json({"ok": False, "message": "Payload Too Large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                if r_err or not isinstance(body, dict) or not body:
                    self._send_json({"ok": False, "message": "Invalid JSON config payload"}, HTTPStatus.BAD_REQUEST)
                    return
                saved = ctrl.save_config(body)
                self._send_json({"ok": True, "config": saved})
                return

            if parsed.path == "/api/run-now":
                ok, msg = ctrl.start_run_now()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/run-capture":
                ok, msg = ctrl.start_capture_only()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/schedule":
                body, r_err = self._read_json()
                if r_err == "too_large":
                    self._send_json({"ok": False, "message": "Payload Too Large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                enable = body.get("enable") if isinstance(body, dict) and "enable" in body else None
                ok, msg, running = ctrl.toggle_schedule(enable)
                self._send_json({"ok": ok, "message": msg, "schedule_running": running})
                return

            if parsed.path == "/api/stop":
                ok, msg = ctrl.stop_current_task()
                self._send_json({"ok": ok, "message": msg})
                return

            if parsed.path == "/api/reset":
                saved = ctrl.reset_config()
                self._send_json({"ok": True, "config": saved})
                return

            if parsed.path == "/api/clear-logs":
                ctrl.clear_logs()
                self._send_json({"ok": True})
                return

            if parsed.path == "/api/close":
                ctrl.close_requested_at = time.time()
                self._send_json({"ok": True})
                return

            self._send_json({"ok": False, "message": "Not Found"}, HTTPStatus.NOT_FOUND)

    return Handler


def start_ui_server(app, glass, tk_root):
    """启动本地 HTTP 控制面板服务，返回 (server, ctrl, url, token)。"""
    token = secrets.token_urlsafe(24)
    ctrl = _SheetMateController(app, glass, tk_root)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(ctrl, token))
    server.daemon_threads = True
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/?token={token}"
    return server, ctrl, url, token


def show_settings_window(app):
    """启动 iOS 18 风格原生感桌面控制中心 + 隐藏 Tk 驱动灵动岛浮窗。"""
    import tkinter as tk
    import neu_ui as neu

    tk_root = tk.Tk()
    tk_root.withdraw()
    glass = neu.GlassProgress(tk_root, title="暗号表助手·迷你版")

    server, ctrl, url, _token = start_ui_server(app, glass, tk_root)

    win_w, win_h = 500, 880
    sw, sh = tk_root.winfo_screenwidth(), tk_root.winfo_screenheight()
    wx = max(20, (sw - win_w) // 2)
    wy = max(20, (sh - win_h) // 2)

    shell_exe, kind = _find_app_shell_browser()
    proc = None
    launched_at = time.time()

    if shell_exe:
        shell_dir = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SheetMateMini" / "ui_shell"
        shell_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            shell_exe,
            f"--app={url}",
            f"--user-data-dir={shell_dir}",
            f"--window-size={win_w},{win_h}",
            f"--window-position={wx},{wy}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=msEdgeSidebarV2",
        ]
        try:
            proc = subprocess.Popen(cmd)
        except Exception:
            proc = None

    if proc is None:
        webbrowser.open(url)

    def _shutdown():
        try:
            ctrl.save_config(ctrl.get_config())
        except Exception:
            pass
        try:
            app.request_stop()
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            tk_root.destroy()
        except Exception:
            pass
        os._exit(0)

    def _watch_lifecycle():
        now = time.time()
        # 方式1：前端发出 pagehide 关闭信号且超过 1.6 秒无新心跳重连，说明窗口真的关掉了
        if ctrl.close_requested_at > 0 and (now - ctrl.close_requested_at) > 1.6 and (now - ctrl.last_heartbeat) > 1.4:
            _shutdown()
            return
        # 方式2：若独立 Edge 进程存在超过 3 秒后自行退出且最近 2.5 秒无前端心跳，则干净退出
        if proc is not None and (now - launched_at) > 12.0 and proc.poll() is not None and (now - ctrl.last_heartbeat) > 2.5:
            _shutdown()
            return
        tk_root.after(450, _watch_lifecycle)

    tk_root.after(1000, _watch_lifecycle)
    tk_root.mainloop()
