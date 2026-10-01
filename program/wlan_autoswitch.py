#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WLAN 自动切换小工具 (wlan_autoswitch)
=====================================

用户输入一个目标 WLAN(SSID) 名称并保存，程序在后台轮询：
  1. 目标 WLAN 是否在扫描范围内；
  2. 当前是否已经连在目标 WLAN 上。
当"目标可见 且 未连接目标"时，自动断开当前无线连接，然后连接目标 WLAN。

特性
----
* 简单的 tkinter 操作窗口，可设置"保存并开始监控"；
* "开机自启动"可选两种方式：
    - 注册表 Run 项（无需管理员权限，但只能连接系统里已有配置的网络）；
    - 计划任务·以最高权限运行（首次连接新网络也能自动完成，开机不弹 UAC）；
* 目标 WLAN 名称保存在配置文件中，下次打开自动带出；
* 目标网络没有配置文件时，可用填写的密码生成，或复用当前网络的配置文件。

依赖：Windows 自带的 netsh / PowerShell，无需第三方库。
用法：
    python wlan_autoswitch.py            # 打开窗口
    python wlan_autoswitch.py --startup  # 开机自启动模式（最小化）
    python wlan_autoswitch.py --selftest # 无界面自检（打印当前无线状态）
"""

from __future__ import annotations

import ctypes
import json
import locale
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from pathlib import Path

APP_NAME = "WLAN自动切换"
APP_DIR_NAME = "wlan-autoswitch"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
IS_WINDOWS = os.name == "nt"

# 单实例占用本机端口；提权重启时必须先释放，否则新进程会误判"已有实例"而退出
SINGLE_INSTANCE_PORT = 51237

# 自动化测试/冒烟测试用：设置该环境变量后，任何"修改系统自启动"的操作都变成空操作，
# 避免测试真的往注册表/计划任务里写东西。
NO_AUTOSTART_ENV = "WLAN_AUTOSWITCH_NO_AUTOSTART"

# 自动化测试/冒烟测试用：把配置目录整个重定向到指定路径，
# 避免测试写到程序目录里那份"真实配置"（程序目录本来就是候选配置位置）。
CONFIG_DIR_ENV = "WLAN_AUTOSWITCH_CONFIG_DIR"


def autostart_writes_disabled() -> bool:
    return bool(os.environ.get(NO_AUTOSTART_ENV))

# 开机自启动的三种模式
AUTOSTART_OFF = "关闭"
AUTOSTART_RUN = "开机自启动（注册表）"
AUTOSTART_TASK = "开机自启动（计划任务·最高权限）"
AUTOSTART_MODES = (AUTOSTART_OFF, AUTOSTART_RUN, AUTOSTART_TASK)

# subprocess: 不弹出黑框
CREATE_NO_WINDOW = 0x08000000

# netsh 单次调用超时（秒）
NETSH_TIMEOUT = 20
# 扫描轮询间隔（秒）
POLL_INTERVAL = 5.0
# 连接失败/发起连接后的冷却时间（秒），避免疯狂重试
RETRY_COOLDOWN = 25.0


# ==========================================================================
# netsh 封装
# ==========================================================================
def netsh_encoding() -> str:
    """netsh 输出使用的编码。中文系统一般是 GBK/CP936。"""
    try:
        enc = locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        enc = "utf-8"
    if enc.lower() in ("ansi_x3.4-1968", "ascii", "us-ascii"):
        enc = "utf-8"
    return enc


ENCODING = netsh_encoding()

# 兼容中英文 netsh 输出的字段名
RE_OUTPUT_NAME = re.compile(r"^\s*(?:名称|Name)\s*:\s*(.+?)\s*$", re.I)
RE_STATE = re.compile(r"^\s*(?:状态|State)\s*:\s*(.+?)\s*$", re.I)
RE_SSID = re.compile(r"^\s*SSID\s*:\s*(.+?)\s*$", re.I)
RE_BSSID = re.compile(r"^\s*BSSID\s*:\s*(.+?)\s*$", re.I)
RE_NET_SSID = re.compile(r"^\s*SSID\s+\d+\s*:\s*(.+?)\s*$", re.I)
RE_PROFILE = re.compile(r"^\s*(?:配置文件|Profile)\s*:\s*(.+?)\s*$", re.I)
RE_SAVED_PROFILE = re.compile(
    r"^\s*(?:所有用户配置文件|当前用户配置文件|All User Profile|User Profile)\s*:\s*(.+?)\s*$",
    re.I,
)

# netsh 用这些字符串表示"没有"
_NA_VALUES = {"n/a", "不可用", "不支持", "none", ""}


def _clean(value: str) -> str:
    return "" if value.strip().lower() in _NA_VALUES else value.strip()


def decode_bytes(raw: bytes, preferred: str | None = None) -> str:
    """按候选编码依次尝试解码，最后兜底 utf-8/replace。

    有些系统上 netsh 输出既不是纯 GBK 也不是纯 UTF-8，必须容错。
    带 UTF-8 BOM 的输出优先按 UTF-8 解。
    """
    candidates: list[str] = []
    if raw.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    candidates.append(preferred or ENCODING)
    candidates += ["utf-8", "gbk", "cp936"]
    seen: list[str] = []
    for enc in candidates:
        if not enc or enc.lower() in seen:
            continue
        seen.append(enc.lower())
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def run_netsh(args: list[str]) -> tuple[int, str]:
    """执行 `netsh <args>`，返回 (返回码, 输出文本)。返回码 -1 表示超时。"""
    cmd = ["netsh"] + list(args)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=NETSH_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return -1, "[超时] netsh 超过 %d 秒未返回" % NETSH_TIMEOUT
    except FileNotFoundError:
        return -2, "[错误] 找不到 netsh，本工具需要在 Windows 上运行"
    raw = proc.stdout or b""
    if proc.stderr:
        raw += b"\n" + proc.stderr
    return proc.returncode, decode_bytes(raw)


# --------------------------------------------------------------------------
# 无线状态查询：优先用 PowerShell + WinRT（不需要管理员权限）
# --------------------------------------------------------------------------
WINRT_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
try {
    [void][Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]
    $profile = [Windows.Networking.Connectivity.NetworkInformation]::GetInternetConnectionProfile()
    if ($profile -eq $null) {
        Write-Output ('STATE|0|')
    } else {
        $name = $profile.ProfileName
        $isWlan = $profile.IsWlanConnectionProfile
        if ($isWlan) { Write-Output ('STATE|1|' + $name) }
        else { Write-Output ('STATE|2|' + $name) }
    }
} catch {
    Write-Output ('ERR|' + $_.Exception.Message)
}
"""


class WlanStatus:
    """一次状态查询的结果。"""

    __slots__ = ("available", "connected", "ssid", "source", "note", "profile")

    def __init__(self, available: bool, connected: bool, ssid: str, source: str,
                 note: str = "", profile: str = ""):
        self.available = available      # 无线网卡/服务是否可用
        self.connected = connected      # 是否已连上某个无线网络
        self.ssid = ssid                # 已连接的 WLAN 名称
        self.source = source            # "netsh" / "winrt"
        self.note = note                # 附加说明（例如需要管理员）
        self.profile = profile          # 当前连接的配置文件名称

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "WlanStatus(available=%r, connected=%r, ssid=%r, source=%r)" % (
            self.available,
            self.connected,
            self.ssid,
            self.source,
        )


def scratch_dir() -> Path:
    """可写的工作目录，用来放临时脚本/XML。"""
    candidates = [Path(tempfile.gettempdir())]
    candidates += candidate_config_dirs()
    for folder in candidates:
        if _is_writable_dir(folder):
            return folder
    return Path(tempfile.gettempdir())


def parse_winrt_output(text: str) -> WlanStatus | None:
    """解析 WINRT_SCRIPT 的输出。

    STATE|1|SSID -> 已连接某个无线网络
    STATE|2|名字 -> 连的是有线/其它网络
    STATE|0|     -> 完全没有网络
    ERR|...      -> 调用失败（例如被策略限制），返回 None 让调用方退回 netsh
    """
    for line in text.splitlines():
        line = line.strip().lstrip("\ufeff")
        if line.startswith("STATE|"):
            parts = line.split("|", 2)
            kind = parts[1] if len(parts) > 1 else "0"
            name = parts[2].strip() if len(parts) > 2 else ""
            return WlanStatus(
                available=True,
                connected=(kind == "1"),
                ssid=name if kind == "1" else "",
                source="winrt",
            )
        if line.startswith("ERR|"):
            return None
    return None


def write_once(path: Path, text: str, encoding: str = "utf-8") -> bool:
    """内容没变就不重写文件。

    反复"写一个文件 → 立刻删掉"会持续触发杀软实时扫描（实测 Defender 能到
    40%+ CPU）。所以脚本类文件改成写好一次、原地复用。
    """
    try:
        if path.exists() and path.read_text(encoding=encoding, errors="replace") == text:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return True
    except OSError:
        return False


def _winrt_status() -> WlanStatus | None:
    """用 PowerShell 调用 WinRT Connectivity API 查询当前无线连接。

    只在 netsh 读不到接口状态时才会走到这里（见 current_wlan_status），
    而且脚本文件写好一次后复用，不再每次写/删。
    """
    script = scratch_dir() / "wlan_autoswitch_winrt.ps1"
    if not write_once(script, WINRT_SCRIPT, encoding="utf-8-sig"):
        return None
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            timeout=30,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    return parse_winrt_output((proc.stdout or b"").decode("utf-8", errors="replace"))


def query_wlan_interfaces() -> WlanStatus:
    """用 netsh 查接口状态（首选路径）。

    netsh 只起一个进程、约 0.07 秒，而且**不写任何文件**；相比 PowerShell+WinRT
    不会触发杀软实时扫描，也不会反复创建 console host。
    唯一限制：非提升权限时可能返回 error 5，此时 available=False 并带上 note。
    """
    code, text = run_netsh(["wlan", "show", "interfaces"])
    lowered = text.lower()
    note = ""
    if "error 5" in lowered or "requires elevation" in lowered:
        note = "netsh 查询接口需要管理员权限"

    has_iface = any(
        marker in lowered or marker in text
        for marker in ("interface on the system", "个接口", "interface name", "接口名称")
    )
    ssid = ""
    state = ""
    profile_name = ""
    for line in text.splitlines():
        if RE_NET_SSID.match(line) or RE_BSSID.match(line):
            continue
        match = RE_SSID.match(line)
        if match:
            ssid = _clean(match.group(1))
            continue
        match = RE_STATE.match(line)
        if match:
            state = match.group(1).strip()
            continue
        match = RE_PROFILE.match(line)
        if match:
            profile_name = _clean(match.group(1))
            continue

    connected = bool(ssid) and state.lower() not in ("已断开连接", "disconnected")
    # 注意：error 5 时接口其实是存在的，只是读不到详情 —— 这种情况必须 still 算"可用"，
    # 否则会被误判成"没有无线网卡"。
    return WlanStatus(
        available=bool(has_iface or note or connected),
        connected=connected,
        ssid=ssid if connected else "",
        source="netsh",
        note="" if connected else note,
        profile=profile_name,
    )


# 状态查询策略：
#   "auto"   —— 先试 netsh（便宜、不产生文件），不行再退回 PowerShell
#   "netsh"  —— 已知 netsh 可用，之后不再尝试 PowerShell
#   "winrt"  —— 已知 netsh 不可用（非提升，error 5），只走 PowerShell 且降频
_STATUS_POLICY = {"mode": "auto", "last_winrt": 0.0, "last_netsh": None}
WINRT_MIN_INTERVAL = 30.0  # 退回 PowerShell 时，最少间隔多少秒才允许再起一个 powershell 进程


def reset_status_policy() -> None:
    _STATUS_POLICY["mode"] = "auto"
    _STATUS_POLICY["last_winrt"] = 0.0
    _STATUS_POLICY["last_netsh"] = None
    _STATUS_CACHE["value"] = None


def current_wlan_status(
    allow_powershell: bool = True,
    max_age: float = WINRT_MIN_INTERVAL,
) -> WlanStatus:
    """获取当前无线状态。默认策略：netsh 优先，必要时才动用 PowerShell。

    调用时机决定成本：目标网络不可见时，监控根本不会问"当前连的是谁"，
    于是完全不产生任何 PowerShell 进程；只有在需要判断要不要切换时才查，
    而且结果会缓存 max_age 秒，避免反复起进程。
    allow_powershell=False 时完全不起 PowerShell（供冒烟测试/自检使用）。
    """
    if _STATUS_POLICY["mode"] in ("auto", "netsh"):
        status = query_wlan_interfaces()
        if status.available or status.connected:
            _STATUS_POLICY["mode"] = "netsh"
            _STATUS_POLICY["last_netsh"] = status
            if status.ssid:
                _STATUS_CACHE["value"] = status
                return status
            # netsh 能连通但读不到 SSID（非提升权限的典型情况）：
            # 调用方不需要就别再起 PowerShell 了
            if not allow_powershell:
                return status
        else:
            _STATUS_POLICY["mode"] = "winrt"  # netsh 完全用不了，本会话改走 WinRT

    if not allow_powershell:
        return WlanStatus(False, False, "", "none", note="已禁用 PowerShell 查询")

    now = time.monotonic()
    cached = _STATUS_CACHE.get("value")
    if cached is not None and now - _STATUS_POLICY["last_winrt"] < max_age:
        return cached

    status = _winrt_status()
    _STATUS_POLICY["last_winrt"] = time.monotonic()
    if status is None:
        # PowerShell 也不成：保留 netsh 给出的更有用说明（例如"需要管理员权限"）
        fallback = _STATUS_POLICY.get("last_netsh")
        if fallback is not None:
            return fallback
        status = WlanStatus(False, False, "", "winrt", note="PowerShell 查询失败")
    _STATUS_CACHE["value"] = status
    return status


_STATUS_CACHE: dict = {"value": None}


def current_connected_ssid() -> str:
    """当前已连接的 WLAN 名称；未连接无线网络时返回空字符串。"""
    return current_wlan_status().ssid


def current_profile(allow_powershell: bool = True) -> str:
    """当前连接的配置文件名称。

    优先用已经查过的状态结果（netsh 查询里就带 Profile 字段），拿不到才单独查一次。
    """
    cached = _STATUS_CACHE.get("value")
    if cached is not None and cached.profile:
        return cached.profile
    return current_wlan_status(allow_powershell=allow_powershell).profile


def maybe_decode_hex_ssid(name: str) -> str:
    """有些驱动/netsh 组合会把非 ASCII 的 SSID 输出成 UTF-8 十六进制串。

    例如 "E8B081E79A84E5AEB6" 其实是 "我的家"。看起来像十六进制、又能
    解码成合法 UTF-8 的才转换，避免把正常名字改坏。
    """
    candidate = name.strip()
    if len(candidate) < 4 or len(candidate) % 2 != 0:
        return name
    if not re.fullmatch(r"[0-9A-Fa-f]+", candidate):
        return name
    try:
        decoded = bytes.fromhex(candidate).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return name
    if not decoded or "\ufffd" in decoded:
        return name
    # 至少包含一个非 ASCII 或空格才算真名字，避免把 "2024" 之类改坏
    if all(32 <= ord(ch) < 127 for ch in decoded):
        return name
    return decoded


def scan_visible_ssids() -> list[str]:
    """返回当前能扫描到的 WLAN 名称（去重、保持顺序）。"""
    code, text = run_netsh(["wlan", "show", "networks"])
    if code != 0:
        return []
    found: list[str] = []
    for line in text.splitlines():
        if RE_BSSID.match(line):  # "BSSID 1   : ..." 必须排除
            continue
        m = RE_NET_SSID.match(line)
        if m:
            ssid = maybe_decode_hex_ssid(m.group(1).strip())
            if ssid and ssid not in found:
                found.append(ssid)
    return found


def saved_profile_names() -> list[str]:
    """系统里已存在的 WLAN 配置文件名称列表。"""
    code, text = run_netsh(["wlan", "show", "profiles"])
    if code != 0:
        return []
    names: list[str] = []
    for line in text.splitlines():
        m = RE_SAVED_PROFILE.match(line)
        if m:
            name = m.group(1).strip()
            if name and name not in names:
                names.append(name)
    return names


def has_profile(ssid: str) -> bool:
    return any(n.lower() == ssid.lower() for n in saved_profile_names())


def connect_to(ssid: str) -> tuple[bool, str]:
    code, text = run_netsh(["wlan", "connect", "name=%s" % ssid, "ssid=%s" % ssid])
    ok = code == 0 and "error" not in text.lower()
    return ok, text.strip()


def disconnect_current() -> tuple[bool, str]:
    code, text = run_netsh(["wlan", "disconnect"])
    return code == 0, text.strip()


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def build_profile_xml(ssid: str, password: str, auth: str = "WPA2PSK", cipher: str = "AES") -> str:
    """生成 WLAN 配置文件 XML（密码为明文 passPhrase，导入到当前用户下）。"""
    key_block = ""
    if password:
        key_block = (
            "\n                <sharedKey>"
            "\n                    <keyType>passPhrase</keyType>"
            "\n                    <protected>false</protected>"
            "\n                    <keyMaterial>%s</keyMaterial>"
            "\n                </sharedKey>" % _xml_escape(password)
        )
    name = _xml_escape(ssid)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<WLANProfile xmlns="http://www.microsoft.com/networking/WLAN/profile/v1">\n'
        "    <name>%s</name>\n"
        "    <SSIDConfig>\n"
        "        <SSID>\n"
        "            <name>%s</name>\n"
        "        </SSID>\n"
        "    </SSIDConfig>\n"
        "    <connectionType>ESS</connectionType>\n"
        "    <connectionMode>auto</connectionMode>\n"
        "    <MSM>\n"
        "        <security>\n"
        "            <authEncryption>\n"
        "                <authentication>%s</authentication>\n"
        "                <encryption>%s</encryption>\n"
        "                <useOneX>false</useOneX>\n"
        "            </authEncryption>%s\n"
        "        </security>\n"
        "    </MSM>\n"
        "</WLANProfile>\n" % (name, name, auth, cipher, key_block)
    )


def add_profile_file(path: Path) -> tuple[bool, str]:
    code, text = run_netsh(["wlan", "add", "profile", "filename=%s" % path, "user=current"])
    return code == 0, text.strip()


def add_profile_with_password(ssid: str, password: str) -> tuple[bool, str]:
    """用密码现场生成并导入一个 WLAN 配置文件。"""
    tmp = Path(tempfile.gettempdir()) / "wlan_autoswitch_new.xml"
    try:
        tmp.write_text(build_profile_xml(ssid, password), encoding="utf-8")
    except OSError as exc:
        return False, "无法写入临时配置文件: %s" % exc
    try:
        return add_profile_file(tmp)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def rewrite_profile_xml(xml: str, new_ssid: str) -> str:
    """把导出的配置文件 XML 中的名字/SSID 改成新的 SSID。"""
    escaped = _xml_escape(new_ssid)
    xml = re.sub(r"<name>.*?</name>", "<name>%s</name>" % escaped, xml, flags=re.S)
    xml = re.sub(r"<hex>.*?</hex>", "<hex>%s</hex>" % new_ssid.encode("utf-8").hex().upper(), xml, flags=re.S)
    # 有些配置文件带 <SSIDConfig><SSID><name> 之外的旧格式
    xml = re.sub(r"<SSID>.*?</SSID>", "<SSID><name>%s</name></SSID>" % escaped, xml, flags=re.S)
    return xml


def copy_profile_for(ssid: str, source_profile: str) -> tuple[bool, str]:
    """把已有配置文件（例如当前连接的网络）复制成目标 SSID 的配置文件。

    仅在目标网络还没有配置文件、并且两者密码相同时才有意义。
    """
    folder = Path(tempfile.gettempdir()) / "wlan_autoswitch_export"
    folder.mkdir(parents=True, exist_ok=True)
    filename = "src.xml"
    target = folder / filename

    code, text = run_netsh(
        [
            "wlan",
            "export",
            "profile",
            "name=%s" % source_profile,
            "key=clear",
            "folder=%s" % folder,
            "filename=%s" % filename,
        ]
    )
    if code != 0 or not target.exists():
        return False, "导出 %s 的配置文件失败：%s" % (source_profile, text.strip())

    try:
        xml = target.read_text(encoding="utf-8", errors="replace")
        new_xml = rewrite_profile_xml(xml, ssid)
        target.write_text(new_xml, encoding="utf-8")
        return add_profile_file(target)
    except OSError as exc:
        return False, "处理导出文件失败: %s" % exc
    finally:
        try:
            target.unlink()
        except OSError:
            pass


# ==========================================================================
# 配置持久化
# ==========================================================================
def _module_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def candidate_config_dirs() -> list[Path]:
    """配置文件候选目录，按优先级排列。

    先放 %APPDATA%（最规范），不可写时退回 %LOCALAPPDATA%，最后退回程序所在目录。
    测试可以用 WLAN_AUTOSWITCH_CONFIG_DIR 整体重定向。
    """
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return [Path(override)]

    dirs: list[Path] = []
    if IS_WINDOWS:
        roaming = os.environ.get("APPDATA")
        if roaming:
            dirs.append(Path(roaming) / APP_DIR_NAME)
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / APP_DIR_NAME)
    dirs.append(_module_dir())
    dirs.append(Path.home() / ("." + APP_DIR_NAME))
    out: list[Path] = []
    for d in dirs:
        if d not in out:
            out.append(d)
    return out


def _is_writable_dir(folder: Path) -> bool:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def config_dir() -> Path:
    for folder in candidate_config_dirs():
        if _is_writable_dir(folder):
            return folder
    return _module_dir()


def config_path() -> Path:
    """配置文件位置（config.json）。"""
    return config_dir() / "config.json"


DEFAULT_CONFIG = {
    "ssid": "",
    "password": "",
    "remember_password": False,
    "autostart_mode": AUTOSTART_OFF,
    "monitor_on_start": True,
}


def load_config(path: Path | None = None) -> dict:
    """读取配置。默认位置没有时，会依次尝试其它候选位置。"""
    cfg = dict(DEFAULT_CONFIG)
    candidates = [path] if path is not None else [config_path()] + [
        d / "config.json" for d in candidate_config_dirs()
    ]
    for p in candidates:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            # 兼容旧版本配置里的 autostart: true/false
            if "autostart_mode" not in data and "autostart" in data:
                data["autostart_mode"] = AUTOSTART_RUN if data["autostart"] else AUTOSTART_OFF
            for key in cfg:
                if key in data and data[key] is not None:
                    cfg[key] = data[key]
            break
    return cfg


def save_config(cfg: dict, path: Path | None = None) -> Path:
    """写入配置，返回实际写入的路径。默认路径不可写时自动回退。"""
    targets = [path] if path is not None else [config_path()] + [
        d / "config.json" for d in candidate_config_dirs()
    ]
    last_error: OSError | None = None
    for target in targets:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
            return target
        except (OSError, ValueError) as exc:  # 非法路径会抛 ValueError
            last_error = exc if isinstance(exc, OSError) else OSError(str(exc))
            continue
    raise last_error if last_error else OSError("无法写入配置文件")


# ==========================================================================
# 开机自启动（HKCU\...\Run）
# ==========================================================================
def launcher_exe() -> str:
    """启动本程序用的可执行文件。

    打包成 exe 后就是它自己；否则优先用 pythonw.exe（无控制台黑框）。
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def entry_script() -> Path | None:
    """非打包模式下本程序的 .py 路径；打包后返回 None。

    ### 打包后 __file__ 指向 PyInstaller 的临时解包目录（onefile 模式还会被删掉），
    ### 因此任何"要长期存活"的路径（开机自启项、计划任务、提权重启）都必须用
    ### sys.executable，绝不能用 __file__。
    """
    if getattr(sys, "frozen", False):
        return None
    return Path(__file__).resolve()


def app_dir() -> Path:
    """程序所在目录（打包后 = exe 所在目录）。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def startup_command() -> str:
    """开机自启动要执行的命令行。"""
    launcher = launcher_exe()
    script = entry_script()
    if script is None:
        return '"%s" --startup' % launcher
    return '"%s" "%s" --startup' % (launcher, script)


def run_command(args: list[str], timeout: float | None = None) -> tuple[int, str]:
    """执行任意命令并返回 (返回码, 输出)。"""
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout or NETSH_TIMEOUT,
            creationflags=CREATE_NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return -1, str(exc)
    raw = (proc.stdout or b"") + (b"\n" + proc.stderr if proc.stderr else b"")
    return proc.returncode, decode_bytes(raw)


TASK_SCRIPT_PS1 = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding $false
try {
    $action = New-ScheduledTaskAction -Execute $env:DSH_TASK_EXE -Argument $env:DSH_TASK_ARGS -WorkingDirectory $env:DSH_TASK_CWD
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:DSH_TASK_USER
    $principal = New-ScheduledTaskPrincipal -UserId $env:DSH_TASK_USER -LogonType Interactive -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
    Register-ScheduledTask -TaskName $env:DSH_TASK_NAME -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    Write-Output 'OK'
} catch {
    Write-Output ('ERR|' + $_.Exception.Message)
}
"""


def _run_powershell_script(content: str, env_extra: dict) -> tuple[int, str]:
    """把一段 PowerShell 写到临时文件后执行（避免 -Command 的引号地狱）。"""
    script = scratch_dir() / "wlan_autoswitch_task.ps1"
    try:
        script.write_text(content, encoding="utf-8-sig")
    except OSError as exc:
        return -1, str(exc)
    env = dict(os.environ)
    env.update(env_extra)
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            timeout=60,
            creationflags=CREATE_NO_WINDOW,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return -1, str(exc)
    finally:
        try:
            script.unlink()
        except OSError:
            pass
    text = (proc.stdout or b"").decode("utf-8", errors="replace")
    if proc.stderr:
        text += "\n" + (proc.stderr or b"").decode("utf-8", errors="replace")
    return proc.returncode, text.strip()


def current_user_name() -> str:
    domain = os.environ.get("USERDOMAIN") or ""
    user = os.environ.get("USERNAME") or ""
    if domain and user:
        return "%s\\%s" % (domain, user)
    return user or "."


def is_scheduled_task_enabled() -> bool:
    """检查"以最高权限运行"的计划任务是否存在。"""
    if not IS_WINDOWS:
        return False
    code, text = run_command(["schtasks", "/query", "/tn", APP_NAME])
    return code == 0


def set_scheduled_task(enabled: bool) -> tuple[bool, str]:
    """用计划任务实现开机自启动（以最高权限运行，免 UAC 弹窗）。

    创建/删除任务需要管理员权限；普通权限下会返回明确错误。
    """
    if not IS_WINDOWS:
        return False, "仅支持 Windows"
    if autostart_writes_disabled():
        return True, "[测试模式] 已跳过注册表启动项写入"

    if enabled:
        if not is_admin():
            return False, "创建计划任务需要管理员权限，请先点“以管理员身份重启”"
        script = entry_script()
        exe = launcher_exe()
        task_args = "--startup" if script is None else '"%s" --startup' % script
        env_extra = {
            "DSH_TASK_NAME": APP_NAME,
            "DSH_TASK_EXE": exe,
            "DSH_TASK_ARGS": task_args,
            "DSH_TASK_CWD": str(app_dir()),
            "DSH_TASK_USER": current_user_name(),
        }
        code, text = _run_powershell_script(TASK_SCRIPT_PS1, env_extra)
        if "OK" in text:
            return True, "%s %s" % (exe, task_args)
        cleaned = text.replace("ERR|", "").strip() or "注册计划任务失败"
        return False, cleaned

    if not is_scheduled_task_enabled():
        return True, ""
    if not is_admin():
        return False, "删除计划任务需要管理员权限，请先点“以管理员身份重启”"
    code, text = run_command(["schtasks", "/delete", "/tn", APP_NAME, "/f"])
    if code == 0:
        return True, ""
    return False, text.strip() or "schtasks 删除任务失败"


def scheduled_task_info() -> str:
    if not is_scheduled_task_enabled():
        return ""
    code, text = run_command(["schtasks", "/query", "/tn", APP_NAME, "/fo", "list"])
    return text.strip() if code == 0 else ""


def is_autostart_enabled() -> bool:
    if not IS_WINDOWS:
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> tuple[bool, str]:
    """注册表 Run 项方式的开机自启动（不需要管理员权限）。"""
    if not IS_WINDOWS:
        return False, "仅支持 Windows"
    if autostart_writes_disabled():
        return True, "[测试模式] 已跳过注册表启动项写入"
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            if enabled:
                cmd = startup_command()
                winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                return True, cmd
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        return True, ""
    except OSError as exc:
        return False, str(exc)


AUTOSTART_OFF = "关闭"
AUTOSTART_RUN = "开机自启动（注册表）"
AUTOSTART_TASK = "开机自启动（计划任务·最高权限）"
AUTOSTART_MODES = (AUTOSTART_OFF, AUTOSTART_RUN, AUTOSTART_TASK)


def detect_autostart_mode() -> str:
    """当前实际生效的开机自启动方式。"""
    if is_scheduled_task_enabled():
        return AUTOSTART_TASK
    if is_autostart_enabled():
        return AUTOSTART_RUN
    return AUTOSTART_OFF


def apply_autostart_mode(mode: str) -> tuple[bool, str]:
    """把自启动方式设置为 mode，并清理另一种方式。"""
    messages: list[str] = []
    want_task = mode == AUTOSTART_TASK
    want_run = mode == AUTOSTART_RUN

    # 清理不需要的方式
    if not want_task and is_scheduled_task_enabled():
        ok, msg = set_scheduled_task(False)
        if not ok:
            return False, msg
        messages.append("已删除原计划任务")
    if not want_run and is_autostart_enabled():
        ok, msg = set_autostart(False)
        if not ok:
            return False, msg
        messages.append("已清除注册表启动项")

    if want_task and not is_scheduled_task_enabled():
        ok, msg = set_scheduled_task(True)
        if not ok:
            return False, msg
        messages.append("已创建计划任务：" + msg)
    if want_run and not is_autostart_enabled():
        ok, msg = set_autostart(True)
        if not ok:
            return False, msg
        messages.append("已写入注册表启动项：" + msg)

    return True, "；".join(messages)


# ==========================================================================
# 管理员权限
# ==========================================================================
def is_admin() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_elevated() -> bool:
    """进程当前是否真的处于提升（管理员）状态。

    比 IsUserAnAdmin() 可靠：后者在"属于管理员组但未提升"时也会返回真，
    会让"以管理员身份重启"按钮被错误禁用。无法判断时按"未提升"处理。
    """
    if not IS_WINDOWS:
        return False
    try:
        TOKEN_QUERY = 0x0008
        TokenElevation = 20
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        handle = ctypes.c_void_p()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(handle)
        ):
            return False
        try:
            size = ctypes.c_ulong(0)
            advapi32.GetTokenInformation(
                handle, TokenElevation, None, 0, ctypes.byref(size)
            )
            if not size.value:
                return False
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                handle, TokenElevation, buffer, size.value, ctypes.byref(size)
            ):
                return False
            return bool(ctypes.c_ulong.from_buffer(buffer).value)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def relaunch_as_admin(hard_timeout: float = 25.0) -> tuple[bool, str]:
    """以管理员身份重新启动自己。

    关键点：新进程启动时也会尝试占用单实例锁，所以必须**先释放锁**再拉起
    提权进程，否则新进程会以为"已有实例在运行"而立刻退出（曾经的 bug：
    UAC 过了，但工具并没有重启）。释放后通过 handoff 标记确认新实例真的
    接管了，没接管就把锁抢回来继续用。
    """
    global _single_lock
    if not IS_WINDOWS:
        return False, "仅支持 Windows"
    if is_elevated():
        return False, "当前已经是管理员"

    token = "%d-%d" % (os.getpid(), int(time.time() * 1000))
    write_handoff(token)

    exe = launcher_exe()          # 打包后即 exe 自身；否则 pythonw.exe
    script = entry_script()
    base_params = ('"%s"' % script) if script is not None else ""
    if "--startup" in sys.argv:
        base_params = ("%s --startup" % base_params).strip()
    params = ("%s --handoff %s" % (base_params, token)).strip()
    workdir = str(app_dir())

    held = _single_lock
    _single_lock = None
    if held is not None:
        try:
            held.close()
        except OSError:
            pass

    try:
        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, params, workdir, 1
        )
    except Exception as exc:
        clear_handoff()
        _reclaim_single_instance()
        return False, str(exc)

    if rc <= 32:
        # 常见：5 = 拒绝访问(用户点了"否")，31 = 无法启动关联程序
        clear_handoff()
        _reclaim_single_instance()
        return False, "UAC 被拒绝或启动失败（代码 %d）" % rc

    # 等新实例认领交接标记（它会自己写回 pid）
    deadline = time.monotonic() + max(0.0, hard_timeout)
    while time.monotonic() < deadline:
        time.sleep(0.25)
        data = read_handoff()
        if data.get("token") == token and int(data.get("pid") or 0) not in (0, os.getpid()):
            return True, "已以管理员身份重启（新实例 pid=%s）" % data.get("pid")

    clear_handoff()
    _reclaim_single_instance()
    return False, "提权进程没有启动成功（可能被 UAC、杀软或策略拦下）"


def single_instance_taken() -> bool:
    """单实例端口是否已被别的进程占用（=已有实例在跑）。"""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    try:
        return probe.connect_ex(("127.0.0.1", SINGLE_INSTANCE_PORT)) == 0
    finally:
        probe.close()


# --------------------------------------------------------------------------
# 提权重启的"交接标记"
#
# 新实例起来后第一件事就是占用单实例端口，所以判断它有没有真的启动，不能靠
# 旧实例去连端口（旧实例刚释放锁，很可能又自己抢回来，造成误判）。改成：
#   1. 旧实例释放锁之前，在共享目录写下 handoff.json（含新实例应有的一次性 token）
#   2. 新实例拿到锁之后，立刻把自己的 pid 写回同一个文件
#   3. 旧实例看到 pid 变了，才认账并退出；超时就说明没启动成功，自己把锁抢回来
# --------------------------------------------------------------------------
HANDOFF_NAME = "handoff.json"


def handoff_path() -> Path:
    try:
        return config_dir() / HANDOFF_NAME
    except OSError:
        return Path(tempfile.gettempdir()) / ("wlan-autoswitch-" + HANDOFF_NAME)


def write_handoff(token: str) -> None:
    try:
        handoff_path().write_text(
            json.dumps({"token": token, "pid": 0, "state": "requested"}), encoding="utf-8"
        )
    except OSError:
        pass


def claim_handoff(token: str) -> bool:
    """新实例启动时调用：如果本次启动是旧实例请求的提权重启，就认领它（返回 True）。"""
    if not token:
        return False
    path = handoff_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict) or data.get("token") != token:
        return False
    try:
        path.write_text(
            json.dumps({"token": token, "pid": os.getpid(), "state": "claimed"}),
            encoding="utf-8",
        )
    except OSError:
        return False
    return True


def read_handoff() -> dict:
    try:
        data = json.loads(handoff_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def clear_handoff() -> None:
    try:
        handoff_path().unlink()
    except OSError:
        pass


def _reclaim_single_instance() -> None:
    """把单实例锁抢回来（幂等）。"""
    global _single_lock
    if _single_lock is not None:
        return
    acquire_single_instance()


def setup_console_encoding() -> None:
    """控制台输出改成 UTF-8，避免中文在部分终端下报编码错误。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


# ==========================================================================
# 卡顿诊断
# ==========================================================================
_last_tick = time.monotonic()
_diag_lock = threading.Lock()


def mark_tick() -> None:
    """记录"主线程还活着"。UI 事件泵每次循环都会调用。"""
    global _last_tick
    _last_tick = time.monotonic()


def stall_seconds() -> float:
    return time.monotonic() - _last_tick


def diag_log_path() -> Path:
    return config_dir() / "diag.log"


def _append_diag(text: str) -> None:
    try:
        with diag_log_path().open("a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass


def collect_process_snapshot(budget: float = 4.0) -> list[str]:
    """列出当前相关的子进程，用于诊断"大量进程"。

    用 tasklist（不需要 WMI 权限），并在后台线程里跑 + 限时，避免诊断
    本身把主线程堵住。拿不到就如实写"无法枚举"，不抛异常。
    """
    result: dict = {}

    def work() -> None:
        code, text = run_command(
            ["tasklist", "/FO", "CSV", "/NH"], timeout=budget
        )
        result["code"] = code
        result["text"] = text

    worker = threading.Thread(target=work, name="ProcSnapshot", daemon=True)
    worker.start()
    worker.join(budget)

    if worker.is_alive():
        return ["[进程枚举超时，%.0f 秒未返回]" % budget]

    text = result.get("text", "")
    if result.get("code") != 0 or not text.strip():
        return ["[无法枚举进程] " + (text or "无输出")[:200]]

    wanted = ("powershell", "netsh", "schtasks", "python", "pythonw", "conhost")
    lines: list[str] = []
    counted: dict[str, int] = {}
    for raw in text.splitlines():
        parts = [p.strip('"') for p in raw.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0].strip('"').lower()
        if any(w in name for w in wanted):
            pid = parts[1].strip('"')
            counted[name] = counted.get(name, 0) + 1
            if len(lines) < 40:
                lines.append("  %s pid=%s" % (name, pid))
    summary = ["  %s × %d" % (k, v) for k, v in sorted(counted.items())]
    if sum(counted.values()) > 3:
        lines = ["[同类进程计数]"] + summary + lines
    return lines or ["[没有发现本工具的残留子进程]"]


def dump_stall_report(reason: str) -> None:
    """把卡顿现场写进 diag.log：线程栈 + 进程快照。"""
    import traceback as _tb

    with _diag_lock:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        parts = [
            "",
            "=" * 78,
            "[%s] 主线程卡顿 %.1fs（%s）" % (stamp, stall_seconds(), reason),
            "-" * 78,
            "线程列表：",
        ]
        for thread in threading.enumerate():
            parts.append("  %s (daemon=%s, alive=%s)" % (thread.name, thread.daemon, thread.is_alive()))
        parts.append("-" * 78)
        parts.append("相关子进程：")
        parts.extend("  " + line for line in collect_process_snapshot())
        parts.append("-" * 78)
        parts.append("线程调用栈：")
        frames = sys._current_frames()
        for thread in threading.enumerate():
            frame = frames.get(thread.ident)
            if frame is None:
                continue
            parts.append("  --- 线程 %s ---" % thread.name)
            parts.extend("    " + ln for ln in _tb.format_stack(frame))
        parts.append("=" * 78)
        _append_diag("\n".join(parts))


def start_watchdog(root, log_callback, threshold: float = 5.0,
                   check_interval_ms: int = 1000) -> dict:
    """自检看门狗：主线程超过 threshold 秒没动静就记录诊断信息。

    目的是抓"按下按钮后窗口未响应 + 进程暴涨"这类只在特定环境出现的问题：
    一旦再发生，config_dir()/diag.log 里会留下现场证据。
    返回一个 state 字典，把 stop 置真即可停掉（窗口关闭时必须停，
    否则销毁窗口后 after 回调会报 invalid command name）。
    """
    state = {"stop": False}

    def check() -> None:
        if state["stop"]:
            return
        stalled = stall_seconds()
        if stalled > threshold:
            try:
                log_callback("检测到界面卡顿 %.0f 秒，正在写入诊断日志…" % stalled)
            except Exception:
                pass
            dump_stall_report("看门狗")
            try:
                log_callback("诊断日志已写入：%s" % diag_log_path())
            except Exception:
                pass
            mark_tick()  # 避免连续刷屏
        try:
            root.after(check_interval_ms, check)
        except Exception:
            state["stop"] = True

    try:
        root.after(check_interval_ms, check)
    except Exception:
        state["stop"] = True
    return state


# ==========================================================================
# 单实例
# ==========================================================================
_single_lock = None


def acquire_single_instance(port: int = SINGLE_INSTANCE_PORT) -> bool:
    global _single_lock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        s.listen(1)
    except OSError:
        s.close()
        return False
    _single_lock = s
    return True


# ==========================================================================
# 动作层（可单独测试）
# ==========================================================================
def looks_like_permission_error(msg: str) -> bool:
    """判断 netsh 的输出是不是权限不足。"""
    lowered = msg.lower()
    return (
        "permission" in lowered
        or "access is denied" in lowered
        or "requires elevation" in lowered
        or "error 5" in lowered
        or "拒绝访问" in msg
        or "权限" in msg
        or "需要提升" in msg
    )


def ensure_profile(target: str, source_profile: str, password: str, log=None) -> tuple[bool, str]:
    """确保系统里存在目标网络的配置文件。"""
    if has_profile(target):
        return True, ""
    if password:
        if log:
            log("未找到 %s 的配置文件，使用填写的密码创建…" % target)
        ok, msg = add_profile_with_password(target, password)
        return (True, "") if ok else (False, msg)
    if source_profile and source_profile.lower() != target.lower():
        if log:
            log("未找到 %s 的配置文件，尝试复用 %s 的配置…" % (target, source_profile))
        return copy_profile_for(target, source_profile)
    return False, "系统中没有 %s 的配置文件，请填写密码或先手动连接一次" % target


def switch_to(target_ssid: str, connected_ssid: str, connected_profile: str = "",
              password: str = "", log=None) -> tuple[bool, str]:
    """完整的切换流程：补配置文件 -> 断开当前连接 -> 连接目标。

    参数名刻意避开 current_profile()/has_profile() 等模块级函数，防止遮蔽。
    """
    ok, msg = ensure_profile(target_ssid, connected_profile, password, log=log)
    if not ok:
        return False, msg

    if connected_ssid:
        ok, msg = disconnect_current()
        if log:
            log("已断开 %s" % connected_ssid if ok else "断开当前连接失败：%s" % msg)
        time.sleep(1.5)  # 给无线驱动一点时间

    ok, msg = connect_to(target_ssid)
    if ok and log:
        log("已发出连接请求：%s" % target_ssid)
    return ok, msg


# ==========================================================================
# 引擎：串起 GUI / 监控线程 / 配置
# ==========================================================================
class Engine:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg if cfg is not None else load_config()
        self.ssid = str(self.cfg.get("ssid") or "").strip()
        self.password = str(self.cfg.get("password") or "")
        self.remember_password = bool(self.cfg.get("remember_password", False))
        self.autostart_mode = str(self.cfg.get("autostart_mode") or AUTOSTART_OFF)
        if self.autostart_mode not in AUTOSTART_MODES:
            self.autostart_mode = AUTOSTART_OFF
        self.monitor_on_start = bool(self.cfg.get("monitor_on_start", True))

        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.monitor: "WlanMonitor | None" = None

    # --- 事件 ---
    def emit(self, kind: str, *payload) -> None:
        self.events.put((kind,) + payload)

    def log(self, msg: str) -> None:
        self.emit("log", "[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    def status(self, text: str, ok: bool | None = None) -> None:
        self.emit("status", text, ok)

    # --- 监控生命周期 ---
    def start_monitor(self) -> None:
        if self.monitor_alive():
            return
        if not self.ssid:
            self.status("请先填写并保存 WLAN 名称", ok=False)
            return
        self.monitor = WlanMonitor(self)
        self.monitor.start()
        self.emit("monitor", True)
        self.log("开始监控：%s" % self.ssid)

    def stop_monitor(self) -> None:
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
            self.emit("monitor", False)
            self.log("已停止监控")

    def monitor_alive(self) -> bool:
        return bool(self.monitor and self.monitor.is_alive())

    # --- 配置 ---
    def set_target(self, ssid: str, password: str) -> None:
        self.ssid = ssid.strip()
        self.password = password
        if self.monitor:
            self.monitor.poke()

    def persist(self) -> None:
        self.cfg.update(
            {
                "ssid": self.ssid,
                "password": self.password if self.remember_password else "",
                "remember_password": self.remember_password,
                "autostart_mode": self.autostart_mode,
                "monitor_on_start": self.monitor_on_start,
            }
        )
        try:
            path = save_config(self.cfg)
            self.emit("saved", str(path))
        except OSError as exc:
            self.emit("save_failed", str(exc))


class WlanMonitor(threading.Thread):
    """后台线程：轮询目标 WLAN 是否出现，并负责断开 / 连接。

    轮询策略（关键：尽量不起子进程）：
      * 每轮只做一次 `netsh wlan show networks` 扫描（约 0.07 秒、不写文件、
        不产生额外 console 进程）；
      * 只有"需要知道当前连的是哪个网络"时才去查接口状态，例如首次、
        目标可见性发生变化、刚切换过、以及每隔一段时间校正一次；
      * 查接口状态优先用 netsh（同样不起 PowerShell）；只有非提升权限下
        netsh 报 error 5 时才退回 PowerShell+WinRT，并且限频到 30 秒一次。
    """

    # 每隔多少轮强制核对一次真实连接状态（12 × 5 秒 = 1 分钟）
    RECONCILE_EVERY = 12
    # 两次"查当前连的是谁"之间至少间隔多少秒（目标可见、但还没连上时会用到）
    STATUS_MIN_INTERVAL = 30.0

    def __init__(self, engine: Engine) -> None:
        super().__init__(name="WlanMonitor", daemon=True)
        self.engine = engine
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._last_attempt = 0.0
        self._last_error = ""
        self._warned_no_nic = False
        # 轮询状态：避免每轮都去查接口
        self._target_was_visible: bool | None = None
        self._connected_to_target = False
        self._last_status: WlanStatus | None = None
        self._last_status_at = 0.0
        self._ticks = 0

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def poke(self) -> None:
        """配置变化后立即唤醒，并强制下一轮重新核对状态。"""
        self._target_was_visible = None
        self._last_status = None
        self._last_status_at = 0.0
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # 后台线程绝不因异常退出
                self.engine.log("监控异常：%r" % exc)
            self._wake.wait(POLL_INTERVAL)
            self._wake.clear()

    # ------------------------------------------------------------------
    def _tick(self) -> None:
        target = self.engine.ssid
        if not target:
            return
        self._ticks += 1
        target_lower = target.lower()

        # 1) 便宜的一步：扫描附近网络
        visible_names = scan_visible_ssids()
        visible = any(name.lower() == target_lower for name in visible_names)
        changed = visible != self._target_was_visible
        self._target_was_visible = visible

        # 2) 只在必要时查"当前连的是谁"（这一步在非提升环境要起 PowerShell）
        now = time.monotonic()
        stale = now - self._last_status_at >= self.STATUS_MIN_INTERVAL
        need_status = (
            self._last_status is None
            or changed
            or (not self._connected_to_target and stale)
            or self._ticks % self.RECONCILE_EVERY == 0
        )
        if need_status:
            status = current_wlan_status()
            self._last_status = status
            self._last_status_at = time.monotonic()
            self._connected_to_target = bool(
                status.connected and status.ssid.lower() == target_lower
            )
        status = self._last_status or WlanStatus(True, False, "", "none")
        connected = status.ssid

        # 3) 接口都读不到：只在异常时提示一次
        if not status.available and not status.connected:
            if not self._warned_no_nic:
                self.engine.log(
                    "读不到无线接口状态" + ("（%s）" % status.note if status.note else "")
                )
                self._warned_no_nic = True
            self.engine.status("无线网卡不可用", ok=False)
            return
        self._warned_no_nic = False

        # 4) 已经连上目标
        if self._connected_to_target:
            self.engine.status("已连接 %s" % target, ok=True)
            self._last_error = ""
            return

        if not visible:
            self.engine.status(
                "目标 %s 不在范围内（当前：%s）" % (target, connected or "未连接")
            )
            return

        now = time.monotonic()
        if now - self._last_attempt < RETRY_COOLDOWN:
            return
        self._last_attempt = now

        self.engine.log("发现 %s，开始切换（当前连接：%s）" % (target, connected or "无"))
        self.engine.status("正在切换到 %s …" % target)

        ok, msg = switch_to(
            target_ssid=target,
            connected_ssid=connected if status.connected else "",
            connected_profile=status.profile or current_profile(),
            password=self.engine.password,
            log=self.engine.log,
        )
        # 切换后强制重新核对真实状态，别靠猜测
        self._target_was_visible = None
        self._last_status = None
        self._last_status_at = 0.0
        if ok:
            self.engine.status("正在连接 %s …" % target)
        else:
            flat = msg.replace("\n", " ").strip()
            if flat != self._last_error:
                if looks_like_permission_error(flat):
                    self.engine.log(
                        "切换失败：%s。首次连接新网络需要管理员权限，请点窗口里的"
                        "“以管理员身份重启”，或先手动连接一次 %s。" % (flat, target)
                    )
                else:
                    self.engine.log("切换失败：%s" % flat)
                self._last_error = flat
            self.engine.status("连接 %s 失败" % target, ok=False)


# ==========================================================================
# GUI
# ==========================================================================
def build_gui(engine: Engine, start_minimized: bool = False):
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)

    pad = {"padx": 10, "pady": 4}
    frm = ttk.Frame(root, padding=12)
    frm.grid(row=0, column=0, sticky="nsew")

    ttk.Label(frm, text="目标 WLAN 名称：").grid(row=0, column=0, sticky="w", **pad)
    ssid_var = tk.StringVar(value=engine.ssid)
    ttk.Entry(frm, textvariable=ssid_var, width=34).grid(
        row=0, column=1, columnspan=2, sticky="we", **pad
    )

    ttk.Label(frm, text="密码（可留空）：").grid(row=1, column=0, sticky="w", **pad)
    pwd_var = tk.StringVar(value=engine.password)
    ttk.Entry(frm, textvariable=pwd_var, width=34, show="•").grid(
        row=1, column=1, columnspan=2, sticky="we", **pad
    )

    remember_var = tk.BooleanVar(value=engine.remember_password)
    ttk.Checkbutton(frm, text="保存密码（下次打开自动填写）", variable=remember_var).grid(
        row=2, column=1, columnspan=2, sticky="w", **pad
    )

    ttk.Separator(frm, orient="horizontal").grid(
        row=3, column=0, columnspan=3, sticky="we", pady=6
    )

    autostart_var = tk.StringVar(value=engine.autostart_mode)
    ttk.Label(frm, text="开机自启动：").grid(row=4, column=0, sticky="w", **pad)
    mode_box = ttk.Combobox(
        frm, textvariable=autostart_var, values=list(AUTOSTART_MODES), state="readonly", width=30
    )
    mode_box.grid(row=4, column=1, columnspan=2, sticky="w", **pad)

    ### 注意：monitor_on_start 是独立变量，不要从"启用自动切换"勾选框推断。
    ### 那个勾选框只表示"本次是否正在监控"，它的值会随本次会话变化，
    ### 如果拿它当偏好保存，下次打开就会被写成 false（曾经的 bug）。
    monitor_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(frm, text="启用自动切换（后台监控）", variable=monitor_var).grid(
        row=5, column=0, columnspan=2, sticky="w", **pad
    )

    start_monitor_var = tk.BooleanVar(value=engine.monitor_on_start)
    ttk.Checkbutton(
        frm, text="程序启动后自动开始监控（记住该选项）", variable=start_monitor_var
    ).grid(row=6, column=0, columnspan=3, sticky="w", **pad)

    btns = ttk.Frame(frm)
    btns.grid(row=7, column=0, columnspan=3, sticky="we", pady=(8, 4))
    save_btn = ttk.Button(btns, text="保存")
    save_btn.grid(row=0, column=0, padx=4)
    apply_btn = ttk.Button(btns, text="保存并开始监控")
    apply_btn.grid(row=0, column=1, padx=4)
    scan_btn = ttk.Button(btns, text="扫描附近网络")
    scan_btn.grid(row=0, column=2, padx=4)
    admin_btn = ttk.Button(btns, text="以管理员身份重启")
    admin_btn.grid(row=0, column=3, padx=4)
    # 只有在"确实已提升"时才禁用；判断不出来就别挡着用户
    if is_elevated():
        admin_btn.state(["disabled"])

    status_var = tk.StringVar(value="就绪")
    status_lbl = ttk.Label(frm, textvariable=status_var, foreground="#0a6", wraplength=430)
    status_lbl.grid(row=8, column=0, columnspan=3, sticky="w", **pad)

    log_box = tk.Text(frm, height=11, width=66, state="disabled", wrap="word")
    log_box.grid(row=9, column=0, columnspan=3, sticky="nsew", padx=10, pady=(0, 8))

    ttk.Label(
        frm,
        text="说明：程序持续扫描；发现目标 WLAN 且当前未连接它时，会自动断开现有连接并连接目标。"
             "首次连接新网络可能需要管理员权限，若失败请右键“以管理员身份运行”。",
        wraplength=440,
        foreground="#666",
    ).grid(row=10, column=0, columnspan=3, sticky="w", padx=10, pady=(0, 10))

    # 最新的日志行（最多 400 行）
    log_lines: deque[str] = deque(maxlen=400)

    def append_log(text: str) -> None:
        log_lines.append(text)
        log_box.configure(state="normal")
        log_box.insert("end", text + "\n")
        if len(log_lines) == 400:
            log_box.delete("1.0", "2.0")
        log_box.see("end")
        log_box.configure(state="disabled")

    def set_status(text: str, ok: bool | None = None) -> None:
        status_var.set(text)
        status_lbl.configure(foreground="#0a6" if ok is not False else "#c00")

    def show_scan(names: list[str]) -> None:
        if not names:
            set_status("没扫描到任何网络（或无线网卡不可用）", ok=False)
            return
        set_status("扫描完成，共 %d 个网络" % len(names), ok=True)
        append_log("附近网络：" + "，".join(names))
        if not ssid_var.get().strip():
            ssid_var.set(names[0])

    # --- 事件泵：引擎 -> GUI ---
    def pump() -> None:
        try:
            while True:
                kind, *payload = engine.events.get_nowait()
                if kind == "log":
                    append_log(payload[0])
                elif kind == "status":
                    set_status(payload[0], payload[1] if len(payload) > 1 else None)
                elif kind == "scan_result":
                    show_scan(payload[0])
                elif kind == "monitor":
                    if bool(payload[0]) != monitor_var.get():
                        monitor_var.set(bool(payload[0]))
                elif kind == "save_failed":
                    append_log("保存配置失败：%s" % payload[0])
                    set_status("保存配置失败（将以本次运行为准）", ok=False)
                elif kind == "admin_result":
                    ok_admin, msg_admin = bool(payload[0]), str(payload[1])
                    if ok_admin:
                        append_log("[权限] " + msg_admin + "，本窗口即将关闭")
                        set_status("已交给管理员实例，本窗口即将关闭…", ok=True)
                        root.after(600, on_close)
                    else:
                        append_log("[权限] " + msg_admin)
                        set_status("提权失败：" + msg_admin, ok=False)
        except queue.Empty:
            pass
        mark_tick()
        try:
            root.after(200, pump)
        except Exception:
            pass  # 窗口已销毁

    def stop_all_timers() -> None:
        """窗口销毁时调用：停掉看门狗并取消挂起的 after 回调。"""
        watchdog_state["stop"] = True
        try:
            pending = root.tk.call("after", "info")
            for item in root.tk.splitlist(pending):
                try:
                    root.after_cancel(item)
                except Exception:
                    pass
        except Exception:
            pass

    # --- 动作 ---
    def sync_from_form() -> None:
        engine.set_target(ssid_var.get(), pwd_var.get())
        engine.remember_password = bool(remember_var.get())
        engine.monitor_on_start = bool(start_monitor_var.get())


    def guarded(label: str, fn, *args) -> None:
        """执行界面动作：计时、捕获异常并写进诊断日志。

        这三个按钮曾经出现过"点一下就未响应"，所以任何一次阻塞或异常都要
        能在日志里看见，而不是悄悄把窗口冻住。
        """
        mark_tick()
        start = time.monotonic()
        try:
            fn(*args)
        except Exception:
            detail = traceback.format_exc()
            append_log("!! %s 出错：\n%s" % (label, detail))
            _append_diag("[%s] %s 异常：\n%s" % (time.strftime("%Y-%m-%d %H:%M:%S"), label, detail))
            set_status("%s 出错，详情见日志" % label, ok=False)
        finally:
            spent = time.monotonic() - start
            mark_tick()
            if spent > 1.0:
                append_log("（%s 耗时 %.1f 秒）" % (label, spent))
            if spent > 5.0:
                dump_stall_report("%s 阻塞主线程 %.1fs" % (label, spent))

    def apply_autostart() -> bool:
        """把界面上选择的自启动方式落到系统里。返回是否成功。"""
        want = autostart_var.get()
        if want == engine.autostart_mode and want == detect_autostart_mode():
            return True
        if want == detect_autostart_mode():
            engine.autostart_mode = want
            return True
        ok, msg = apply_autostart_mode(want)
        if ok:
            engine.autostart_mode = want
            append_log("[开机自启动] %s%s" % (want, ("：" + msg) if msg else ""))
            return True
        autostart_var.set(detect_autostart_mode())
        set_status("设置开机自启动失败：%s" % msg, ok=False)
        append_log("[开机自启动] 设置失败：%s" % msg)
        return False

    def do_save(log_msg: bool = True) -> bool:
        sync_from_form()
        if not engine.ssid:
            set_status("请先填写 WLAN 名称", ok=False)
            return False
        autostart_ok = apply_autostart()
        engine.persist()
        if log_msg:
            append_log("已保存目标 WLAN：%s" % engine.ssid)
            if autostart_ok:
                set_status("已保存", ok=True)
        return True

    def on_scan() -> None:
        set_status("正在扫描…")

        def work() -> None:
            try:
                engine.emit("scan_result", scan_visible_ssids())
            except Exception:
                engine.emit("scan_result", [])

        threading.Thread(target=work, daemon=True).start()

    def on_apply() -> None:
        if not do_save():
            return
        engine.stop_monitor()
        engine.start_monitor()

    def on_monitor_toggle() -> None:
        if monitor_var.get():
            if not engine.monitor_alive():
                if not do_save(log_msg=False):
                    monitor_var.set(False)
                    return
                engine.start_monitor()
        else:
            engine.stop_monitor()

    def on_admin() -> None:
        set_status("正在请求管理员权限…")
        # ShellExecuteW 会阻塞到 UAC 对话框结束，放到后台线程免得窗口假死
        def work() -> None:
            try:
                ok, msg = relaunch_as_admin()
            except Exception as exc:  # 后台线程绝不静默死掉
                ok, msg = False, "提权失败：%r" % exc
            engine.emit("admin_result", ok, msg)

        threading.Thread(target=work, daemon=True).start()

    # 每个动作都套上计时 + 异常捕获，避免"点一下就冻住"且查不到原因
    save_btn.configure(command=lambda: guarded("保存", do_save))
    apply_btn.configure(command=lambda: guarded("保存并开始监控", on_apply))
    scan_btn.configure(command=lambda: guarded("扫描附近网络", on_scan))
    admin_btn.configure(command=lambda: guarded("以管理员身份重启", on_admin))
    monitor_var.trace_add("write", lambda *_: guarded("切换监控状态", on_monitor_toggle))

    def on_close() -> None:
        try:
            sync_from_form()
            engine.persist()
        except Exception:
            pass
        engine.stop_monitor()
        stop_all_timers()
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", on_close)
    root._dsh_on_close = on_close  # 供自动化测试/脚本优雅关闭

    # --- 初始状态 ---
    if IS_WINDOWS:
        engine.autostart_mode = detect_autostart_mode()
        autostart_var.set(engine.autostart_mode)
        if not is_elevated():
            append_log("[权限] 当前未提升：可以连接系统里已有配置的网络；"
                       "首次连接新网络需要点“以管理员身份重启”。")
    append_log("配置文件：%s" % config_path())
    if engine.ssid:
        append_log("已载入上次保存的目标 WLAN：%s" % engine.ssid)
    else:
        append_log("请填写目标 WLAN 名称后点击“保存并开始监控”。")

    def maybe_autostart_monitor() -> None:
        if engine.monitor_on_start and engine.ssid:
            monitor_var.set(True)
    root.after(300, maybe_autostart_monitor)
    root.after(200, pump)
    # 卡顿看门狗：一旦界面再出现长时间无响应，现场会写进 diag.log
    watchdog_state = start_watchdog(root, append_log, threshold=5.0)

    if start_minimized:
        root.iconify()

    return root


# ==========================================================================
# 自检 / 入口
# ==========================================================================
def run_selftest() -> int:
    setup_console_encoding()
    print("本地编码:", ENCODING, "| 管理员:", is_admin())
    status = current_wlan_status()
    print("数据来源:", status.source, "| 网卡可用:", status.available,
          "| 已连接:", status.connected, "| 当前 SSID:", status.ssid or "无")
    if status.note:
        print("提示:", status.note)
    print("当前配置文件:", current_profile() or "无")
    print("可见网络:", scan_visible_ssids())
    print("已保存配置:", saved_profile_names())
    print("配置文件路径:", config_path())
    print("开机自启动方式:", detect_autostart_mode())
    print("注册表启动命令:", startup_command())
    print("计划任务存在:", is_scheduled_task_enabled())
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_console_encoding()
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in args:
        return run_selftest()

    if not IS_WINDOWS:
        print("本工具依赖 Windows 的 netsh，请在 Windows 上运行。")
        return 1

    start_minimized = "--startup" in args or "--minimized" in args

    # 先看这次启动是不是旧实例请求的"提权重启"
    handoff_token = ""
    if "--handoff" in args:
        index = args.index("--handoff")
        if index + 1 < len(args):
            handoff_token = args[index + 1]
    from_handoff = claim_handoff(handoff_token)

    if "--no-single-instance" in args:
        pass
    elif from_handoff:
        # 接手旧实例释放的锁；万一没抢到，也允许继续跑（旧实例此时已退出）
        if not acquire_single_instance():
            print("提权实例启动时端口被占用，仍继续运行。")
    elif not acquire_single_instance():
        print("已有实例正在运行，本次退出。")
        return 0

    engine = Engine()
    if from_handoff:
        engine.log("已作为管理员实例接管运行")
    root = build_gui(engine, start_minimized=start_minimized)
    try:
        root.mainloop()
    finally:
        engine.stop_monitor()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
