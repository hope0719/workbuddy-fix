# macOS 整机卡顿根因分诊

> 适用于"整机卡、卡死、开机好好的几分钟后突然卡"这类系统级故障。
> 单个 App 自身卡顿 / 崩溃不在此列，请先确认是整机问题。

**核心原则：卡顿 ≠ CPU 忙。先定象限，再找元凶，最后判"一次性还是持续性"。**

## 为什么需要分诊

多数"电脑卡了"的排查都止步于打开活动监视器看 CPU 排序，然后得出"没有异常进程"的结论。这是错的，因为整机卡顿有四条完全独立的成因线，而 CPU 只是其中最不容易出问题的一条：

| 象限 | 主线 | 典型表现 | 活动监视器能否看出 |
|---|---|---|---|
| A | CPU 饱和 | 风扇狂转、单进程高占用 | 能 |
| B | 内存 / swap 颠簸 | 越来越卡、切换 App 转圈 | 部分（看不到 VM 内存） |
| C | 磁盘 I/O 打满 | 鼠标能动但点击无响应 | **看不出** |
| D | 热降频 | 持续高负载后突然变慢 | 看不出 |

B 与 C 是最高发的两类，也恰好是活动监视器最容易漏掉的两类。

---

## 第一步 · 固定快照（永不跳过）

一条命令拿全六项，**不要凭感觉挑着看**：

```bash
uptime                                        # 负载 / 核数
sysctl vm.swapusage                           # swap 总量与已用
vm_stat | head -14                            # 空闲页 / wired / 压缩器
iostat -d -c 2                                # 磁盘吞吐（两次采样）
pmset -g therm                                # 热降频
df -h /System/Volumes/Data                    # 真实数据卷空间
```

或直接跑脚本：

```bash
python3 macos/lag-snapshot.py
```

### 判读阈值

| 指标 | 危险阈值 | 指向 |
|---|---|---|
| load ÷ 核心数 | > 1.5 | CPU 或 I/O 饱和 |
| `Pages free` × 16 KB | < 1 GB | 内存枯竭 |
| `Pages wired down` × 16 KB | > 6 GB | 内核 / 虚拟机锁死内存 |
| swap used | > 2 GB 或 > 60% | 内存耗尽、开始颠簸 |
| compressor 占用 | > 2 GB | 正在压缩换页 |
| `iostat` MB/s | > 100 | 磁盘被打满 |
| `iostat` tps | > 5000 | 小文件狂读写 |
| `pmset -g therm` | 有 warning 行 | 热降频 |
| 数据卷可用 | < 10 GB | 空间不足 |

> **单位换算陷阱**：`vm_stat` 输出的是页数，Apple Silicon 页大小为 **16384 字节**。必须 X16 KB 后再判断，直接看整数页数会严重误判（`288687` 页听起来很多，实为 4.6 GB；`5564` 页听起来还行，实为 91 MB）。

---

## 第二步 · 四象限定位

### A 象限 · CPU 高

```bash
pgrep -fl "影刀|AutoClaw|Electron"
```

进程排行用 libproc（见固定坑 1），**CPU% 必须两轮采样**（固定坑 3）。

### B 象限 · 内存 / swap（最高发）

顺序不能反 —— **先看 wired，再看进程 RSS**：

```bash
vm_stat | grep "wired"          # 内核锁定内存
colima status 2>/dev/null       # 虚拟机是否在跑
docker ps 2>/dev/null | head    # 需先 export DOCKER_HOST
```

**判据：`wired` 很高（> 6 GB）但所有进程 RSS 加起来远低于它 → 元凶是虚拟机，不是应用。**

Virtualization.framework 的 guest 内存计入宿主 `wired`，**不进任何进程的 RSS**，这是最容易漏掉的一类。实测案例：某 VM 配置 `memory: 10240MiB`，宿主 16 GB 直接变 6 GB，`wired` 峰值 11.9 GB，而 3 个容器实际 RSS 仅 665 MB。

只有 wired 正常，才进进程排行找应用级内存大户。

### C 象限 · 磁盘 I/O

**首选证据是 macOS 自带的读写超额报告，不是猜**：

```bash
ls -lt /Library/Logs/DiagnosticReports/*.diag | head
grep -E "^(Date/Time|Command|Event|Writes|Duration)" /Library/Logs/DiagnosticReports/<xxx>.diag
```

命中 `Event: disk writes` 即锁定写入方。再查它的堆栈：

```bash
grep -A 12 "Heaviest stack" /Library/Logs/DiagnosticReports/<xxx>.diag
```

- 堆栈落在 `write + 8` → 同步阻塞写；
- 报 `file backed memory dirtied` → **mmap 脏页**，文件大小不变（固定坑 7）。

**关键判读：`2147.48 MB` 是系统日写入配额上限，不是进程实际写入量。** 同日多个无关进程都会显示 2147.5x MB，它们只是各自撞线。**要比括号里的 `MB/s` 速率。** 实测排序：

| 速率 | 进程 | 性质 |
|---|---|---|
| 206.03 MB/s | `Autoupdate` | 系统更新下载解包 |
| 23.44 MB/s | `bsdtar` | 同上，解包阶段 |
| 17.66 MB/s | Chromium 浏览器 | GPU 着色器缓存重建 |
| 9.47 MB/s | `mds_stores` | Spotlight 索引重建 |
| 3.83 MB/s | `storagekitd` | 存储服务 |
| 1.97 MB/s | 通讯类 App | 数据库写入 |

常见元凶排序：系统自动更新 > Spotlight > 浏览器 GPU 缓存 > Time Machine > 同步工具。

### D 象限 · 热降频

`pmset -g therm` **无 warning 记录即可排除散热问题**，不必再查风扇或环境温度。

---

## 第三步 · 根因库

| # | 症状指纹 | 根因 | 处置 |
|---|---|---|---|
| 1 | wired > 10 GB、进程 RSS 都很小、swap 12 GB+ | **虚拟机默认分配过多内存**（Colima / Docker Desktop / UTM / Parallels） | 降到物理内存 25%：`colima stop && colima start --memory 4 --cpus 4` |
| 2 | 开机 3 分钟后才卡、`iostat` 数百 MB/s | **Spotlight 全量重建**（上次异常关机触发） | 等它跑完，勿强制关机 |
| 3 | 2 分钟内磁盘写成 2 GB、随即 App 版本号变了 | **应用自动更新 → GPU 着色器缓存重建** | 关自动更新；或删 `ShaderCache`/`GPUCache`/`Dawn*Cache` |
| 4 | 磁盘写入 200 MB/s 级、进程名 `Autoupdate` / `bsdtar` | **系统更新下载并解包** | 让它跑完；或关闭系统自动更新 |
| 5 | 画面正常但没声音 | 投屏虚拟声卡抢默认输出 | `SwitchAudioSource -s "<内置扬声器>"` |
| 6 | 亮屏一整晚、合盖不休眠、发烫 | RPA / 自动化类 App 持有 `NoDisplaySleepAssertion` | `pmset -g assertions` 定位后 `kill` |
| 7 | 待机耗电、CPU 不高 | 手机助手 / 投屏 / 旧代理工具残留自启 | 需 `sudo launchctl unload -w` |
| 8 | 内存缓慢上涨、无明显大进程 | 多个匿名 `Electron` 进程常驻（实测 7 个共 1080 MB） | 查路径确认归属后退出不用的 |
| 9 | 单 App 卡、快捷键失灵、系统不满载 | 该 App 崩溃后自动重启 | 查 Crash 目录 + `create_time()` |
| 10 | 内存逐日上涨、重启后缓解 | 长期不重启 + Electron 累积 | 定期重启比逐个体 kill 更有效 |

> 第 5–7 条不是典型"卡顿"，但共用同一排查入口，一并保留。

---

## 固定坑清单（12 条）

1. **`ps` / `top` / `lsof` / `launchctl list` 全部 EPERM**，提权也无效（setuid 二进制在非交互上下文被拒）。唯一可行通道是 Python ctypes 调 `/usr/lib/libproc.dylib`。**别在这上面反复试命令浪费时间。**
2. **libproc 结构体大小必须精确**：`proc_bsdinfo` 里 `pbi_comm` 是 `char[16]`、`pbi_name` 是 `char[32]`，写成 17/33 会让 sizeof 变 138，`proc_pidinfo` 静默返回不匹配、**无任何报错**。
3. **CPU% 必须两轮采样**：第一轮只做标记，间隔 2 秒后第二轮才拿得到真实值。且 `pti_total_user` / `pti_total_system` 是 **uint64、单位纳秒**，写成 int64 会算出 1212599600% 这种荒谬值。
4. **卡顿时绝对不要建议强制关机**。中断 Spotlight 索引会导致下次开机从零重建，白卡一遍。
5. **先看 wired 再看 RSS**。虚拟机内存不在任何进程的 RSS 里，只查进程排行会得出"没有元凶"的错误结论。
6. **磁盘写入暴涨先查 `.diag` 的 `disk writes` 事件**，这是系统自带监控，比 `fs_usage`（需 sudo）可靠且免授权。**但要比速率，不比总量**（见 C 象限）。
7. **mmap 缓存写入不会让文件变大**。实测 2.1 GB 写入，`find -size +50M` 一个都搜不到 —— 这是正常现象，不要据此否定"写入"结论。要 `stat` 看 mtime 间隔（常为整 60 秒一轮）。
8. **`iostat` 必须采两次**：第一次可能读到上一轮残留的高位，用第二次的值下结论。
9. **`pmset -g therm` 无记录 = 排除热降频**，不要再往散热上猜。
10. **沙箱注入的 `HTTP_PROXY` 会污染本机探测**，查本地端口 / 本地域名一律 `curl --noproxy '*'`。
11. **报"孤儿进程"前先确认父进程链与专属管理入口**。`limactl list` 查不到 Colima 实例是**假阴性** —— Colima 用自己的 `LIMA_HOME`（`~/.colima/_lima`）。同理 `docker ps` 需先 `export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock`。
12. **`df /` 只看密封系统卷**，真实数据在 `/System/Volumes/Data`，判空间不足必须看后者。

---

## 处置纪律

- **区分"一次性事件"与"持续性问题"**：这两类的结论完全不同，前者不需要任何处置。
  - 一次性：写入 / 索引在几分钟内自行结束 → 报告"已自愈，无需操作"；
  - 持续性：结束观察后指标仍高位 → 才进入处置。
- **不擅自 kill 用户可能正在使用的进程**。先诊断并说明，等确认。
- 改系统电源策略、卸载驱动、`sudo` 类操作，一律先征得同意。
- 处置后**复测同一组指标**，不靠"感觉快了"下结论。

## 报告模板

```
结论（一句话根因）
→ 实测数据表（指标 / 实测 / 阈值 / 判断）
→ 因果链（时间戳级）
→ 根因库编号
→ 是否需要处置 + 建议动作
```

时间戳对齐是这类问题最有说服力的部分：把 `uptime` 的开机时长、`.diag` 的 `Time Since Boot`、文件 mtime 三者对上，因果链自然闭合。

## 案例索引

| 日期 | 根因 | 关键数据 |
|---|---|---|
| 2026-07-16 | RPA 工具阻止显示器休眠 | 断言持续数十小时，发烫耗电 |
| 2026-07-29 | 手机助手 / 投屏 / 旧代理工具残留自启 | 4 进程仅 8 MB，属耗电不属内存 |
| 2026-11-13 上午 | 虚拟机吃 10 GB wired | IOPS 11819 / 417 MB/s，swap 12–13 GB |
| 2026-11-13 13:01 | 系统 Autoupdate 下载解包 | 写入 206 MB/s，全机最高 |
| 2026-11-13 晚 | Spotlight + 浏览器 GPU 缓存双重写入 | 609 MB/s，swap 6372 MB，空闲 67 MB |

## 一条反直觉的归因教训

2026-11-13 晚那次卡顿，最初的怀疑对象是"某浏览器占用内存太大"。实测后推翻：该 App 全部 16 个进程 RSS 合计仅 **525 MB**，排不进前五。它真正的异常是磁盘写入速率，而内存紧张的元凶是另外几个进程 + 长期不重启的累积。

**结论：卡顿现场的第一直觉，大概率是错的。先拿数据，再下结论。**
