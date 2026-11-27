# 📊 暗号表助手·迷你版（SheetMate-Mini）v1.0.0

[![最新版本](https://img.shields.io/badge/Release-v1.0.0-0A84FF?style=flat-square&logo=github)](https://github.com/Sorenskr/SheetMate-Mini/releases/latest)
[![运行平台](https://img.shields.io/badge/Platform-Windows%2010%20%2F%2011%20x64-34C759?style=flat-square&logo=windows)](https://github.com/Sorenskr/SheetMate-Mini/releases/latest)

> **轻量化单机精简版**：基于经典 `v2.0.0` 极简架构深度加固——**免安装、单文件直跑、无云端账号/AI/手机远程模块**，与主版 [SheetMate](https://github.com/Sorenskr/SheetMate) 完全隔离互不干扰。

---

## 📥 直接下载（Windows 单文件免安装版）

- **单文件直链下载**：[**下载 SheetMateMini.exe (v1.0.0)**](https://github.com/Sorenskr/SheetMate-Mini/releases/latest/download/SheetMateMini.exe)
- **全部历史发行版**：[**GitHub Releases 页面**](https://github.com/Sorenskr/SheetMate-Mini/releases)

---

## ✨ 迷你版保留与强化了什么？

1. **轻量流畅 iOS 风格控制面板**：移除全部重负载毛玻璃滤镜，界面极简零卡顿；内置灵动岛悬浮进度条与独立冷启动防闪退保护。
2. **完整环境隔离 · 与主版可同时使用**：独立单实例互斥锁（`SheetMateMini_v1_Mutex`）、独立浏览器缓存目录（`%LOCALAPPDATA%\SheetMateMini`）与独立 `config.json`。
3. **WPS 高清复制防误点 A1 + 稳态等待**：放大 200% 后自动识别 **C6 深蓝标题栏（`#364C74`）** 并通过名称框核验防误点 A1；复制为图片后默认稳等 **5 秒**再粘贴到钉钉。
4. **钉钉多群连发 + 关闭静音零误点**：支持填写多个群聊（逗号/分号隔开）；关闭静音发送时完全跳过右下角静音下拉按钮，开启后支持发送前可调等待秒数自动发送。
5. **简洁格位写入 & 防休眠**：按需开启写入 `D3` 查询日期（今天/昨天/前天）并自动清空 `D4/E4/D5`，全流程贴表后支持自动写 `R4` 更新时间；值守与运行期间自动防止电脑休眠息屏。
6. **开箱即用不预存私密凭据**：默认不写入任何个人账号密码或私密表格 URL，首次使用在控制中心【高级设置】填写自己的表格链接与账号即可永久保存在本机。

---

## 🛠 源码运行与本地打包

```bash
pip install -r requirements.txt
python -m playwright install chromium
python auto_report.py

# 打包纯净单文件 EXE
pyinstaller SheetMateMini.spec --clean --noconfirm
```
