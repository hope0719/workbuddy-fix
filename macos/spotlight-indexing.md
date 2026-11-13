# Spotlight 索引重建 → 磁盘 I/O 打满导致整机假死

> 特征是「开机明明是好的，用了两三分钟后突然卡死」。这个时间差是它最好认的指纹。

## 症状指纹

1. **开机后 2–3 分钟才开始卡**，而不是一开机就卡；
2. 界面表现为**鼠标能动但点击无响应**（UI 线程被 I/O 阻塞，不是崩溃）；
3. `iostat` 显示数百 MB/s 的持续写入，`tps` 数千至一万以上；
4. `uptime` 的 5 分钟负载远高于 1 分钟（说明刚经历过尖峰，如 `4.77 / 13.55 / 12.47`）；
5. 磁盘空间充足、`pmset -g therm` 无热降频记录。

## 为什么是「延迟卡」

索引重建不是开机瞬间启动的。实测时间线：

```
开机
 └─ +103 秒   mds_stores 开始全量重建
     └─ 持续 221 秒，写入 2147.50 MB（9.7 MB/s，超日配额约 400 倍）
         └─ 期间用户已正常打开若干应用，两边争抢 I/O 与内存
             └─ swap 开始颠簸 → 整机卡死
```

前两分钟一切正常，是因为索引还在扫描阶段尚未大量落盘。等它开始写入，用户已经把应用都开起来了，两者叠加才爆。

## 根因：为什么会「全量重建」

Spotlight 正常情况下是增量索引，不会全量重建。以下情况会触发：

| 触发条件 | 证据位置 |
|---|---|
| **上次关机不干净**（进程无响应/强制关机） | `/Library/Logs/DiagnosticReports/*.shutdownStall` |
| 系统大版本升级后首次启动 | `system_profiler SPInstallHistoryDataType` |
| 手动执行过 `sudo mdutil -E /` | 操作历史 |
| 索引文件损坏 | `mdutil -s /` 状态异常 |

第一条最常见也最隐蔽。只要存在 `shutdown_stall` 记录，几乎必然导致下次开机重建索引。

## 诊断

```bash
# 1. 看是否存在索引进程的写入超额报告
ls -lt /Library/Logs/DiagnosticReports/*.diag | head
grep -E "^(Command|Event|Writes|Time Since Boot)" /Library/Logs/DiagnosticReports/mds_stores_*.diag

# 2. 看上次关机是否异常
ls -lt /Library/Logs/DiagnosticReports/*.shutdownStall

# 3. 看索引服务状态
mdutil -s /

# 4. 看实时 I/O
iostat -d -c 2
```

关键判读：`.diag` 里的 `Time Since Boot: 103s` 直接告诉你它是在开机后第几秒启动的，把它和 `uptime` 的已开机时长一对照，因果链立刻闭环。

## 处置

### 正确做法：等

索引重建通常 10–30 分钟跑完，之后 I/O 自然回落。实测恢复对照：

| 指标 | 卡顿时 | 恢复后 |
|---|---|---|
| 磁盘吞吐 | 609 MB/s | 3.45 MB/s |
| Swap 已用 | 6372 MB | 2975 MB |
| 空闲物理内存 | 67 MB | 约 4.5 GB |
| 内存压缩器 | 2.4 GB | 1.0 GB |

### 绝对不要做：强制关机

**这是本文档最重要的一条。** 强制关机会中断索引过程，下次开机从零重来一遍——等于白卡两次，而且第二次通常更糟（因为索引数据处于不一致状态）。

### 可选：临时抑制

如果急需用机：

```bash
sudo mdutil -i off /     # 临时关闭索引（需用户授权）
sudo mdutil -i on /      # 用完记得开回来
```

⚠️ 关闭期间 Spotlight 搜索、邮件索引、部分 App 的文件检索都会失效。不建议长期关闭。

需要主动重建时：

```bash
sudo mdutil -E /         # 注意：这会触发一次新的全量重建
```

## 三个判读陷阱

### 陷阱 1：`2147.48 MB` 不是真实写入量

它是**系统日写入配额上限**。同日 `mds_stores`、浏览器、通讯 App、`storagekitd`、`python3.12`、`ANECompilerService` 全都显示 2147.5x MB——它们只是各自撞线。

**要比括号里的 `MB/s` 速率。** 实测：系统自动更新 206 MB/s > `bsdtar` 23 MB/s > 浏览器 GPU 缓存 17.66 MB/s > `mds_stores` 9.47 MB/s。

### 陷阱 2：找不到对应大文件是正常的

索引写入走 mmap，报的是 `file backed memory dirtied`（脏页量），不是新增文件字节。所以 `find -size +50M` 搜不到东西，**不能据此否定"写入"结论**。判据是 `stat` 看 mtime 变化。

### 陷阱 3：别把 `mdutil -s` 的 "Indexing enabled" 当成"没在索引"

它只表示索引功能开启，不表示当前是否正在重建。真正的证据是 `.diag` 报告和 `iostat`。

## 与其他 I/O 源叠加

索引重建很少单独出现。实测同一开机窗口内三股写入叠加：

| 来源 | 速率 | 性质 |
|---|---|---|
| `mds_stores` | 9.47 MB/s | Spotlight 索引 |
| 浏览器 GPU 缓存 | 17.66 MB/s | 应用自动更新后 shader cache 失效重建 |
| `Autoupdate` | 206.03 MB/s | 系统更新下载解包 |

三者叠加把磁盘推到 609 MB/s。所以排查时**不要只盯一个进程**，要看 `.diag` 里同一时段的全部 `disk writes` 事件，按速率排序。

## 预防

- 尽量正常关机，避免 `shutdown_stall`。
- 应用和系统更新尽量手动触发，别让它在你开机干活时自动跑。
- 如果经常遇到，考虑把系统更新设为手动，并在更新后留出几分钟让它自己跑完缓存重建。
