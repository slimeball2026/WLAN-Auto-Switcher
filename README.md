# WLAN-Auto-Switcher
A tool used to auto switch WLAN
[README.md](https://github.com/user-attachments/files/32892271/README.md)

# WLAN 自动切换小工具
Writed code by deepseek


输入一个 WLAN 名称并保存，程序在后台持续扫描：**一旦发现该网络且当前没连它，就自动断开现有无线连接并连上去**。目标名称会保存到配置文件，下次打开自动带出。

注意！！！：不要点击“保存并开始监控”！它会持续创建进程，导致window防病毒程序持续运作，CPU占用会飙升导致极度卡顿。

Note!!!: Do not click "保存并开始监控"! It will continuously create processes, causing your antivirus software to run constantly, leading to a sharp increase in CPU usage and severe system lag.

---

## 0. 已经打包好的 exe（免 Python 环境）

`dist\WLAN自动切换\` 里是可直接运行的版本：

| 文件 | 说明 |
| --- | --- |
| `WLAN自动切换.exe` | 双击运行，**没有控制台黑框** |
| `_internal\` | 运行库（含 Python 3.14、Tcl/Tk 9），**必须与 exe 放在一起** |
| `使用说明.txt` | 给最终用户看的简短说明 |

`dist\WLAN自动切换_v1.zip`（约 11.9 MB）是打包好的压缩包，发给别人直接解压即可。

已实测验证：

* 双击启动 → 窗口标题 `WLAN自动切换`，界面元素齐全、ttk 原生样式正常（已截图确认）；
* 进程树下 `conhost.exe` 数量为 **0** → 确认没有控制台黑框；
* `--startup`（开机自启动模式）能正常最小化启动；
* 从 zip 解压出来的副本同样能独立运行，不依赖当前目录。

重新打包：

```powershell
# 需要先安装打包工具（装到用户目录，不需要管理员权限）
python -m pip install --user pyinstaller

# 在 WLAN-Auto-Switcher 目录下执行
powershell -ExecutionPolicy Bypass -File build-exe.ps1
```

打包要点（踩过的坑，同样写在 `build-exe.ps1` 的注释里）：

* 必须加 `--windowed`，否则会带一个控制台窗口；
* 必须用 `--onedir`：PyInstaller 6.22 + Python 3.14 下
  `--onefile --windowed` 会让 tkinter 起不来，窗口标题变成 `[Error]`；
* 入口用 `program\启动WLAN自动切换.pyw`，与"双击无黑框"的日常用法一致；
* 打包后 `__file__` 指向临时解包目录，所以所有要长期存活的路径
  （开机自启项、计划任务、提权重启）都改用 `sys.executable`，
  对应函数是 `launcher_exe()` / `entry_script()` / `app_dir()`。

---

## 1. 快速开始

```powershell
# 打开操作窗口
python wlan_autoswitch.py

# 或者双击（无黑框）
启动WLAN自动切换.pyw
```

窗口里：

1. 填「目标 WLAN 名称」（也可以点「扫描附近网络」，从列表里挑）；
2. 需要的话勾上「保存密码」并填密码（可选，见第 4 节）；
3. 选好开机自启动方式（见第 3 节）；
4. 点「保存并开始监控」。

状态栏会实时显示：`已连接 XXX` / `目标 XXX 不在范围内（当前：YYY）` / `正在切换到 XXX …`。

命令行参数：

| 参数 | 说明 |
| --- | --- |
| （无） | 打开窗口 |
| `--startup` | 开机自启动模式，窗口最小化 |
| `--selftest` | 不开窗口，打印当前无线状态，便于排查 |
| `--no-single-instance` | 允许多开（默认只允许一个实例） |

---

## 2. 工作原理

```
每 5 秒一轮：
  ├─ 扫描附近网络（netsh wlan show networks，约 0.07 秒，不起额外进程）
  │     └─ 目标不在范围内？   → 什么都不做（连"当前连的是谁"都不用问）
  ├─ 需要判断时（目标可见 / 可见性变化 / 每 30 秒核对一次）：
  │     └─ 查询当前连的是哪个 WLAN
  │           ├─ netsh wlan show interfaces（优先，不起 PowerShell）
  │           └─ 非提升权限下读不到 SSID 时 → PowerShell + WinRT（脚本复用、限频）
  ├─ 已经是目标网络？         → 什么都不做
  └─ 目标在范围内且未连接：
        1. 系统里没有目标网络的配置文件？→ 用密码新建，或复制当前网络的配置
        2. netsh wlan disconnect        （断开当前网络）
        3. netsh wlan connect name=目标 ssid=目标
```

细节：

* 连接失败后有 **25 秒冷却**，避免疯狂重试拖慢系统；
* 断开后等 1.5 秒再连接，给无线驱动一点时间；
* **尽量不起子进程**：每轮只做一次 `netsh wlan show networks` 扫描（约 0.07 秒、不写文件）；
  只有需要判断"当前连的是谁"时才查接口状态，并节流到 30 秒一次；
  查接口优先用 netsh，非提升权限下它读不到 SSID 时才动用 PowerShell+WinRT，
  且脚本文件写好一次原地复用（反复写删会触发杀软实时扫描，实测 Defender 能到 40%+ CPU）；
* 目标网络名字里的中文如果被某些驱动输出成十六进制串（例如 `E8B081E79A84E5AEB6`），会自动还原成 `谁的家`；
* 后台异常不会让监控线程退出，只会记一条日志。

---

## 3. 开机自启动的两种方式

界面上是一个下拉框，三选一：

| 方式 | 需要管理员权限 | 特点 |
| --- | --- | --- |
| **关闭** | 否 | 不设置任何自启动 |
| **注册表** | 否 | 写入 `HKCU\...\CurrentVersion\Run`。开机自动运行，但**以普通权限运行**，只能连接系统里已有配置的网络 |
| **计划任务·最高权限** | 创建时需要 | 注册一个「登录时触发、以最高权限运行」的计划任务。开机自动运行、**不弹 UAC**、首次连接新网络也能自动完成 —— **推荐** |

切换到「关闭」或另一种方式时，程序会顺手清理另一种方式的残留（注册表项 / 计划任务）。

计划任务的名字就是 `WLAN自动切换`，想手动删掉的话：任务计划程序里删，或执行

```powershell
schtasks /delete /tn "WLAN自动切换" /f
```

---

## 4. 关于权限（实测结论）

在本机（Windows 11 + Realtek RTL8852BE）实测：

| 操作 | 非管理员 | 说明 |
| --- | --- | --- |
| `netsh wlan disconnect` | ✅ 可用 | 断开当前连接 |
| `netsh wlan connect`（已有配置） | ✅ 可用 | 连接系统里已存在的网络 |
| `netsh wlan add profile` | ❌ 拒绝 | 提示 `You do not have the permission to add profile ...` |
| `netsh wlan show interfaces` | ❌ error 5 | 所以本工具改用 WinRT 查询状态 |

因此：

* **目标网络以前连过（系统里有配置文件）** → 普通权限就能全自动切换；
* **目标网络是全新的（系统里没有配置文件）** → 需要管理员权限，程序会在日志里提示，点窗口里的「以管理员身份重启」即可；之后系统里有了配置文件，就不再需要了。
* 想让全新网络也能开机全自动，就用「计划任务·最高权限」方式。

密码框的作用：系统里没有目标网络的配置文件时，用你填的密码现场生成一个。留空时会尝试**复用当前网络的配置文件**（两个网络密码相同的情况），失败则给出提示。

---

## 5. 文件说明

| 文件 | 作用 |
| --- | --- |
| `wlan_autoswitch.py` | 主程序（GUI + 监控 + netsh 封装） |
| `启动WLAN自动切换.pyw` | 双击启动用，无控制台窗口 |
| `test_wlan_autoswitch.py` | 离线单元测试（82 项，不碰真实网卡、不写注册表） |
| `smoke_gui.py` | GUI 冒烟测试：真实建一次窗口然后自动关闭 |
| `live_test.py` | **真实**切换测试（会短暂断网，需自己确认后运行） |
| `elevated_task_probe.py` | 需管理员权限：验证计划任务能创建/删除（会弹 UAC） |
| `config.json` | 配置：目标名称、是否记密码、自启动方式（正常运行时在 `%APPDATA%\wlan-autoswitch\`） |
| `diag.log` | 只在发生界面卡顿时生成，记录当时的线程栈与相关进程 |

### 界面卡顿了怎么办

程序内置看门狗：主线程连续 5 秒没有响应，就会把现场（各线程调用栈 + 相关子进程清单）
写进 `diag.log`，并在窗口日志里提示路径。把这个文件发出来即可定位。

关闭窗口请用右上角的 ×（会走正常保存流程）。脚本化调用时可以执行
`root._dsh_on_close()` 优雅关闭：停监控、停看门狗、取消挂起的定时回调。

运行测试：

```powershell
python -m unittest test_wlan_autoswitch     # 单元测试
python smoke_gui.py                         # 窗口能否正常创建
python live_test.py 目标SSID                # 真实切换（会短暂断网）
```

测试用的临时目录默认在 `%TEMP%\wlan-autoswitch-tests`，可用环境变量 `WLAN_TEST_TMP` 改。

> 目录里可能留着一个空的 `.test-tmp\`：那是早期版本的测试助手用系统 API 建的、带受限权限的空目录，
> 当时的清理被环境拦住了。它不影响程序运行，删不掉时按提示改用 `%APPDATA%` 下的配置文件即可。

---

## 6. 常见问题

**Q：窗口关了还会自动切换吗？**
不会。关闭窗口 = 退出程序（配置会保存）。要长期后台运行就配好开机自启动，或者让窗口最小化挂着。

**Q：为什么状态显示"无线网卡不可用"？**
无线服务（WlanSvc）没启动、没有无线网卡，或者查询被策略拦住了。先 `python wlan_autoswitch.py --selftest` 看详细输出。

**Q：怎么改回默认、彻底卸载？**
把自启动方式切到「关闭」，删掉 `%APPDATA%\wlan-autoswitch\` 目录即可。计划任务按第 3 节删。

**Q：支持有线网络吗？**
不支持。监控和切换都只针对 WLAN（无线）接口。

---

## 7. 修复记录（2026-09）

实测反馈三个问题，处理如下：

1. **「程序启动后自动开始监控」下次打开不勾选，还反过来把设置写成 false**
   根因：保存时拿「启用自动切换（后台监控）」这个**本次会话状态**当成了偏好设置，
   关窗口时它正好是 false，于是把偏好覆盖掉了。现在两者彻底分离：
   偏好只由「程序启动后自动开始监控（记住该选项）」这一项决定。
   回归测试：`MonitorOnStartTests`。

2. **点「保存并开始监控」后界面未响应、大量进程、CPU 100%（Defender 43%）**
   根因确认：轮询每 5 秒调用一次 `current_wlan_status()`，它每次都
   **新建一个 `powershell.exe`（连带 console host）+ 新写一个临时 .ps1 再删掉**。
   文件反复创建/删除正是实时防护的扫描对象，于是 Antimalware Service Executable
   顶到 40%+，任务管理器里就是"反复创建又删除的 PowerShell 和命令行窗口进程"。
   实测数据：

   | 方式 | 5 次查询耗时 | 每次是否起进程 | 是否写文件 |
   | --- | --- | --- | --- |
   | PowerShell + WinRT（旧） | 2.41s | 是（powershell + conhost） | 是，且写完就删 |
   | `netsh wlan show networks` | 0.35s | 否 | 否 |

   修复（三处）：
   * **netsh 优先**：`netsh wlan show interfaces` 本来就带 SSID/Profile 字段，
     能读到就完全不用 PowerShell（实测非提升环境下它报 error 5，见下方"必要性"）；
   * **查询节流**：目标不可见时根本不需要知道"当前连的是谁"，于是不起任何进程；
     需要时也限制到 30 秒一次，结果带缓存；
   * **脚本复用**：PowerShell 脚本写好一次原地复用，不再每次写删。

   修复后实测：**状态查询 12 次/分 → 2.4 次/分，且不再有"写文件→删文件"**。

   ### 为什么不能直接删掉 PowerShell

   不是没必要的代码，而是**在非提升权限下唯一能读到"当前连的是哪个 WLAN"的手段**：
   `netsh wlan show interfaces` 在非管理员下直接返回
   `功能 WlanQueryInterface 返回错误 5：请求的操作需要提升`，而
   `netsh wlan show networks` 只给"附近有哪些网络"，不给"现在连着谁"。
   没有它就无法判断"是否已连接目标"，会在已连接时反复断开重连。
   所以处理方式是把它的调用频率降到最低，而不是删除：

   * 提升运行时（例如计划任务方式）：`netsh` 能读 SSID，**全程零 PowerShell**；
   * 非提升运行时：只在"目标网络出现 / 可见性变化 / 需要核对"时调用，约 2 次/分钟，
     且脚本文件复用、不写删。
   另有一个纯 Python 的替代（ctypes 调 WinRT）可以彻底去掉 PowerShell，
   但那要手写 COM 接口，属于较大改动，暂时没做 —— 需要的话可以再上。

3. **「以管理员身份重启」UAC 过了但工具没有重启**
   根因确认：新进程启动时会去占用单实例锁，而旧实例还握着它，于是新进程认为
   "已有实例在运行"立刻退出 —— 表现就是 UAC 弹了、什么都没发生。
   现在改为显式**交接**：旧实例先在共享目录写 `handoff.json`（含一次性 token）→
   释放锁 → 拉起提权进程 → 新实例拿到锁后把 pid 写回同一文件 → 旧实例确认后才关闭。
   超时则认为启动失败，把锁抢回来继续运行并给出提示。
   实测：`ok=True`，交接标记确认新实例接管。回归测试：`HandoffTests`。

顺带改进：`is_elevated()` 改用 token 提升状态判断（原来 `IsUserAnAdmin()` 会在
"属于管理员组但未提升"时误判，导致按钮被错误禁用）；提权动作与 `ShellExecuteW`
都移出主线程，避免 UAC 对话框期间窗口假死。

