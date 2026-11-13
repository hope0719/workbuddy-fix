# 进程内存归因：为什么"看活动监视器"经常得出错误结论

> 这篇讲的是排查动作本身的失真来源。同样的数据，看错方式就会指向完全错误的元凶。

## 四个常见失真

### 失真 1：RSS 相加会重复计数

macOS 的 RSS（Resident Set Size）**包含共享内存**。Chromium/Electron 类应用有大量共享的 framework 页，多个子进程会各自把它们计入自己的 RSS。

实测：某机器所有进程 RSS 合计 **9.08 GB**，但物理内存只有 16 GB，`Pages active` 仅 5.56 GB。差出来的部分是重复计数。

**结论：RSS 合计只能用于横向比较谁大，不能当成"实际占用总量"。** 要算真实占用，看 `vm_stat` 的 `active + wired + compressor`。

### 失真 2：匿名进程名不能望文生义

打包 Electron 应用时如果没改可执行文件名，进程列表里就会显示成一堆同名的 `Electron`。此时**唯一的判断依据是 `proc_pidpath` 拿到的可执行路径**。

实测案例：某机器上有 **7 个进程都叫 `Electron`，合计 964 MB**，看起来像是一堆随手安装的小工具在偷偷常驻。查路径后真相是：

| 进程数 | 实际归属 | 合计 |
|---|---|---|
| 3 | `WorkBuddy.app` | 431 MB |
| 2 | `WorkBuddy AI.app` | 225 MB |
| 1 | `Trae CN.app` | 115 MB |
| 1 | 同上派生子进程 | 14 MB |

**全是正经的 AI 工具本体，一个可疑的小工具都没有。** 如果按名字判断，很可能会建议用户"清理这些不明 Electron 进程"，而实际上会关掉他正在用的主力软件。

> 这条教训的代价：差点把用户正在使用的助手进程当成垃圾清理掉。

### 失真 3：单进程视角漏掉多进程架构

Chromium / Electron / VS Code / 各类 AI 客户端都是多进程架构：主进程 + 若干 Renderer + GPU + Utility。单个子进程通常只有 50–200 MB，看起来都不构成威胁。

但一个应用的真实占用要**按应用合计**：

```
某浏览器 : 主进程 81 MB + 14 个 Helper 合计 444 MB = 525 MB
```

这个数字和"最大单进程 111 MB"给人的印象完全不同。

### 失真 4：采样瞬间值不等于持续占用

CPU% 和内存都是一次快照。一个只在特定操作时爆发的进程，可能在采样那一刻恰好是 0%。

**CPU 必须两轮采样**（间隔 2 秒求差），内存则要看趋势而非单点。

## 正确的归因流程

```
1. 先看 vm_stat 的 wired —— 排除虚拟化层（见 colima-memory.md）
2. 再看应用级合计（按可执行路径聚合，不按进程名）
3. 对可疑应用，看它的全部子进程合计，而不是最大那个
4. RSS 只用于排序，不用于求和
```

## 拿进程数据的代码

在受限环境里 `ps` / `top` / `lsof` 可能全部被拒（setuid 二进制在非交互上下文 EPERM）。用 libproc：

```python
import ctypes

lib = ctypes.CDLL("/usr/lib/libproc.dylib")

class TaskInfo(ctypes.Structure):
    _fields_ = [
        ("vs", ctypes.c_uint64), ("rss", ctypes.c_uint64),
        ("ut", ctypes.c_uint64), ("st", ctypes.c_uint64),
        # pti_total_user / pti_total_system：uint64，单位纳秒
        ("tu", ctypes.c_uint64), ("ts", ctypes.c_uint64),
        ("pol", ctypes.c_int32), ("flt", ctypes.c_int32), ("pi", ctypes.c_int32),
        ("cow", ctypes.c_int32), ("ms", ctypes.c_int32), ("mr", ctypes.c_int32),
        ("sm", ctypes.c_int32), ("su", ctypes.c_int32), ("csw", ctypes.c_int32),
        ("tn", ctypes.c_int32), ("nr", ctypes.c_int32), ("pr", ctypes.c_int32),
    ]

n = lib.proc_listallpids(None, 0)
buf = (ctypes.c_int * n)()
cnt = lib.proc_listallpids(buf, ctypes.sizeof(buf))

for i in range(cnt):
    pid = buf[i]
    ti = TaskInfo()
    if lib.proc_pidinfo(pid, 4, 0, ctypes.byref(ti), ctypes.sizeof(ti)) <= 0:
        continue
    name = ctypes.create_string_buffer(256)
    lib.proc_name(pid, name, 256)
    path = ctypes.create_string_buffer(4096)
    lib.proc_pidpath(pid, path, 4096)
    print(f"{ti.rss/1048576:8.0f} MB  {name.value.decode():<24} {path.value.decode()}")
```

### 两个必须注意的细节

1. **`pti_total_user` / `pti_total_system` 是 uint64、单位纳秒**。写成 `c_int64` 不会报错，但算出来的 CPU% 会是 `1212599600%` 这种荒谬值。
2. **要拿父进程**需要 `proc_pidinfo(pid, 3, ...)`（`PROC_PIDTBSDINFO`），结构体 sizeof **必须精确 136 字节**——其中 `pbi_comm` 是 `char[16]`、`pbi_name` 是 `char[32]`，写成 17/33 会让整个结构偏移 2 字节，函数**静默返回不匹配且无任何报错**。

## 判定口径

| 现象 | 结论 |
|---|---|
| wired 高、进程 RSS 都小 | 虚拟化层占用（另见 colima-memory.md） |
| 单个应用合计 > 1 GB 且持续增长 | 该应用内存泄漏，查版本/重装 |
| 多应用合计高但各自正常 | 常驻应用太多，属配置问题非故障 |
| 采样瞬间高、复测回落 | 正常波动，不是泄漏 |
