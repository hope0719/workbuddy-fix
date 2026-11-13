# 虚拟机静默吃掉内存 → swap 颠簸导致整机卡死

> 这是最容易被误判的一类卡顿：CPU 侧完全正常，进程列表里找不到元凶，但机器就是越来越卡直到卡死。

## 症状指纹

满足以下三条中的两条，基本可以锁定：

1. 整机卡顿，但 `top` / 活动监视器的 CPU 排序里**没有异常进程**（最高不到 30%）；
2. `vm_stat` 显示 `Pages wired down` 异常高（**> 6 GB**，16 GB 机器上尤为明显）；
3. `swap` 使用量持续增长（**12 GB 以上**），`iostat` 随机读写数千至一万 IOPS。

实测现场数据：

```
iostat    : 11819 IOPS / 417 MB/s
swap      : 12–13 GB 已用
Pages free: 78–207 MB
wired 峰值: 11.9 GB
load      : 7.67
```

## 根因

**Virtualization.framework 的 guest（虚拟机）内存计入宿主的 `wired` 内存，而 `wired` 内存不可被系统换出。**

这意味着：一台 16 GB 的机器，如果虚拟机配置了 `memory: 10240MiB`，宿主实际可用内存直接变成约 6 GB，且这部分内存连压缩都做不到。

关键反差：**虚拟机里 3 个容器的实际 RSS 只有 665 MB，却锁着 10 GB 额度。** 内存 balloon 会随 guest 使用量膨胀，一旦负载上来就吃满配置值。

`wired` 峰值 11.9 GB ≈ 虚拟机 10 GB + 系统自身 wired 1.9 GB，数字完全吻合。

## 为什么进程排行查不出来

因为**虚拟机内存不属于任何进程的 RSS**。宿主上的 `colima` / `qemu` / `VirtualMachine` 进程自身 RSS 很小，真正的内存占用在虚拟化框架层，只体现在 `wired` 里。

这是本文档存在的唯一理由：**先看 `wired`，再看 RSS。**

## 诊断

```bash
# 1. 确认 wired 是否异常（页数 × 16 KB）
vm_stat | grep "wired"

# 2. 确认是否有虚拟机在跑
colima status
docker ps                      # 需先 export DOCKER_HOST（见坑 2）

# 3. 确认 swap 是否已进入颠簸
sysctl vm.swapusage
iostat -d -c 2

# 4. 查虚拟机的内存配置
cat ~/.colima/_lima/colima/lima.yaml | grep -E "memory|cpus|vmType"
```

### 判定

| 条件 | 结论 |
|---|---|
| `wired` > 6 GB 且进程 RSS 合计远低于 `wired` | 虚拟化层占用，查虚拟机 |
| `wired` 正常但进程 RSS 高 | 应用级内存泄漏，查进程排行 |
| `wired` 高且无虚拟机在跑 | 内核扩展 / 驱动泄漏，另案处理 |

## 修复

把虚拟机内存降到物理内存的 **25%** 左右：

```bash
colima stop
colima start --memory 4 --cpus 4
```

- 容器数据在 Docker volume 里，`stop` / `start` 不丢数据，中断 1–2 分钟。
- 10 核机器上 `--cpus 6` 也偏高，建议一并降到 4。
- 保留 Docker 能力，同时释放约 6 GB 内存。

其他虚拟化产品同理：Docker Desktop 在 Settings → Resources 里调，UTM / Parallels / VMware 直接改虚拟机配置。

## 两个必踩的坑

### 坑 1：`limactl list` 的假阴性

```bash
limactl list        # 返回 No instance found
```

**这不代表实例不存在。** Colima 使用自己的 `LIMA_HOME`（`~/.colima/_lima`），而系统 `limactl` 默认只查 `~/.lima`（往往压根没这个目录）。

正确入口是 `colima status`，不是 `limactl list`。用后者得出的"VM 已删除、内存泄漏"结论是错的。

### 坑 2：`docker ps` 连不上不等于 daemon 挂了

Colima 的 socket 不在默认位置：

```bash
export DOCKER_HOST=unix://$HOME/.colima/default/docker.sock
docker ps
```

不设这个变量时 `docker ps` 报连接失败，容易误判成"Docker daemon 挂了"，进而去重启服务——白折腾。

## 预防

- 任何虚拟机的内存配置不要超过物理内存的 25%（16 GB 机器 → 4 GB）。
- 长期不用 Docker 时执行 `colima stop`，比留着空转划算。
- 排查卡顿的第一条命令永远是 `vm_stat | grep wired`，不是打开活动监视器。

## 一条方法论

报"孤儿进程"或"内存泄漏"前，必须先确认两件事：**该进程族的父进程链**，以及**它自己的专属管理入口**。用一个通用 CLI 的空结果当证据，是这类误判的常见起点（`limactl` vs `colima` 就是典型）。
