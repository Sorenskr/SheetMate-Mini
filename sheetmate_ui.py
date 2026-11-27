# -*- coding: utf-8 -*-
"""兼容层：统一转发到 Apple iOS 18 风格控制中心界面（ios_ui）。"""


def show_settings_window(app):
    from ios_ui import show_settings_window as _show
    _show(app)
