#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无控制台窗口的启动入口（双击这个文件即可，不会闪黑框）。

等价于：pythonw wlan_autoswitch.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import wlan_autoswitch  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(wlan_autoswitch.main())
