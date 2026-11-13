#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
macOS 整机卡顿固定快照
用法: python3 lag-snapshot.py
只读，不改任何系统状态。约 4 秒完成。
依赖: 仅标准库（libproc 走 ctypes，无需 psutil / 无需 root）
"""
import ctypes
import datetime
import glob
import os
import re
import subprocess
import sys

PG = 16384  # macOS Apple Silicon 页大小
GB = 1024 ** 3


def sh(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return p.stdout.strip()
    except Exception:
        return ""


def mark(ok, warn=False):
    return "🔴" if warn else ("⚠️" if not ok else "✅")


# ---------- 负载 ----------
def do_load():
    out = sh("uptime")
    m = re.search(r"load averages?:\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)", out)
    ncpu = int(sh("sysctl -n hw.ncpu") or 10)
    up = out.split(" up ")[1].split(",")[0] if " up " in out else "?"
    if not m:
        return None, ncpu, up
    la = [float(x) for x in m.groups()]
    return la, ncpu, up


# ---------- 内存 ----------
def do_mem():
    out = sh("vm_stat")
    d = {}
    for line in out.splitlines():
        mm = re.match(r'"?([A-Za-z][^:"]*)"?:\s+(\d+)', line)
        if mm:
            d[mm.group(1).strip()] = int(mm.group(2))
    total = int(sh("sysctl -n hw.memsize") or 0)
    g = lambda k: d.get(k, 0) * PG
    return {
        "total": total,
        "free": g("Pages free"),
        "wired": g("Pages wired down"),
        "active": g("Pages active"),
        "inactive": g("Pages inactive"),
        "compressor": g("Pages occupied by compressor"),
        "swapins": d.get("Swapins", 0),
    }


# ---------- Swap ----------
def do_swap():
    out = sh("sysctl vm.swapusage")
    m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M\s+free = ([\d.]+)M", out)
    if not m:
        return None
    return tuple(float(x) for x in m.groups())


# ---------- 磁盘 I/O（两次采样，取第二次） ----------
def do_io():
    out = sh("iostat -d -c 2", timeout=20)
    rows = [l.split() for l in out.splitlines() if re.match(r"^\s*[\d.]+\s+\d+\s+[\d.]+", l)]
    if not rows:
        return None
    last = rows[-1]
    try:
        return float(last[0]), int(last[1]), float(last[2])  # KB/t, tps, MB/s
    except Exception:
        return None


# ---------- 进程（libproc，无需 root） ----------
class TaskInfo(ctypes.Structure):
    _fields_ = [
        ("vs", ctypes.c_uint64), ("rss", ctypes.c_uint64),
        ("ut", ctypes.c_uint64), ("st", ctypes.c_uint64),
        ("tu", ctypes.c_uint64), ("ts", ctypes.c_uint64),  # pti_total_user/system 是 uint64，单位纳秒
        ("pol", ctypes.c_int32), ("flt", ctypes.c_int32), ("pi", ctypes.c_int32),
        ("cow", ctypes.c_int32), ("ms", ctypes.c_int32), ("mr", ctypes.c_int32),
        ("sm", ctypes.c_int32), ("su", ctypes.c_int32), ("csw", ctypes.c_int32),
        ("tn", ctypes.c_int32), ("nr", ctypes.c_int32), ("pr", ctypes.c_int32),
    ]


def proc_snapshot():
    try:
        lib = ctypes.CDLL("/usr/lib/libproc.dylib")
        n = lib.proc_listallpids(None, 0)
        if n <= 0:
            return {}
        buf = (ctypes.c_int * n)()
        cnt = lib.proc_listallpids(buf, ctypes.sizeof(buf))
        res = {}
        for i in range(cnt):
            pid = buf[i]
            ti = TaskInfo()
            if lib.proc_pidinfo(pid, 4, 0, ctypes.byref(ti), ctypes.sizeof(ti)) <= 0:
                continue
            nb = ctypes.create_string_buffer(256)
            lib.proc_name(pid, nb, 256)
            name = nb.value.decode("utf-8", "ignore") or f"pid{pid}"
            res[pid] = {"name": name, "rss": ti.rss, "cpu": ti.tu + ti.ts}
        return res
    except Exception:
        return {}


# ---------- 写入超额报告 ----------
def do_diag():
    hits = []
    for path in glob.glob("/Library/Logs/DiagnosticReports/*.diag"):
        try:
            if os.path.getmtime(path) < (datetime.datetime.now().timestamp() - 12 * 3600):
                continue
            with open(path, "r", errors="ignore") as f:
                head = f.read(4000)
            if "Event:" in head and "disk writes" in head:
                cmd = re.search(r"^Command:\s+(.+)$", head, re.M)
                w = re.search(r"^Writes:\s+(.+)$", head, re.M)
                t = re.search(r"^Date/Time:\s+(.+)$", head, re.M)
                wtext = w.group(1).strip() if w else "?"
                # 提取真实速率（诊断价值高于 2147MB 配额线本身）
                rate = 0.0
                rm = re.search(r"\(([\d.]+)\s*(MB|KB)\s+per second", wtext)
                if rm:
                    rate = float(rm.group(1)) / (1024 if rm.group(2) == "KB" else 1)
                hits.append((
                    rate,
                    (cmd.group(1).strip() if cmd else "?"),
                    wtext[:75],
                    (t.group(1).strip() if t else "?"),
                ))
        except Exception:
            continue
    hits.sort(reverse=True)  # 按写入速率排序，速率才是异常判据
    return hits


# ---------- 睡眠断言 ----------
def do_assertions():
    out = sh("pmset -g assertions")
    bad = []
    for line in out.splitlines():
        if re.search(r"PreventUserIdleDisplaySleep\s+1|NoDisplaySleepAssertion", line):
            bad.append(line.strip())
    return bad


def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"=== macOS 整机卡顿快照 · {now} ===\n")
    alerts = []

    la, ncpu, up = do_load()
    if la:
        ratio = la[0] / ncpu
        s = mark(ratio < 1.0, ratio > 1.5)
        print(f"【负载】{la[0]} / {la[1]} / {la[2]}   {ncpu} 核  饱和度 {ratio:.2f} {s}   已开机 {up}")
        if ratio > 1.5:
            alerts.append("负载饱和：CPU 或磁盘 I/O 被打满")

    mem = do_mem()
    if mem:
        free_gb = mem["free"] / GB
        wired_gb = mem["wired"] / GB
        comp_gb = mem["compressor"] / GB
        print(f"【内存】总 {mem['total']/GB:.1f} GB   空闲 {free_gb:.2f} GB {mark(free_gb > 1, free_gb < 0.3)}")
        print(f"        wired {wired_gb:.2f} GB {mark(wired_gb < 6, wired_gb > 6)}   "
              f"compressor {comp_gb:.2f} GB {mark(comp_gb < 2, comp_gb > 2)}   "
              f"active {mem['active']/GB:.2f} GB")
        if free_gb < 0.3:
            alerts.append("物理内存枯竭（空闲 < 300 MB）")
        if wired_gb > 6:
            alerts.append(f"wired 高达 {wired_gb:.1f} GB —— 优先怀疑虚拟机（Colima/Docker/UTM），不是应用")

    sw = do_swap()
    if sw:
        total, used, free = sw
        pct = used / total * 100 if total else 0
        print(f"【Swap】{total:.0f} MB 总 / {used:.0f} MB 已用 ({pct:.0f}%) {mark(used < 2048, used > 2048)}")
        if used > 2048:
            alerts.append(f"Swap 已用 {used:.0f} MB —— 内存在颠簸")

    io = do_io()
    if io:
        kbt, tps, mbs = io
        print(f"【磁盘】{mbs:.1f} MB/s   {tps} tps {mark(mbs < 100, mbs > 100)}")
        if mbs > 100 or tps > 5000:
            alerts.append(f"磁盘 I/O 异常：{mbs:.0f} MB/s / {tps} tps —— 查下方写入超额报告")

    th = sh("pmset -g therm")
    thermal = "No thermal warning" in th or not th
    print(f"【热】{'无热降频记录（可排除散热问题）' if thermal else th.splitlines()[0]} {mark(thermal)}")

    dfout = sh("df -h /System/Volumes/Data")
    m = re.search(r"(\d+)Gi\s+(\d+)%", dfout)
    if m:
        avail, cap = int(m.group(1)), m.group(2)
        print(f"【空间】数据卷可用 {avail} GB ({cap} 已用) {mark(avail > 10, avail <= 10)}")
        if avail <= 10:
            alerts.append("数据卷空间不足 10 GB")

    print("\n【进程 TOP 10】(按内存，CPU% 为 2 秒采样)")
    a = proc_snapshot()
    import time
    time.sleep(2)
    b = proc_snapshot()
    rows = []
    for pid, v in b.items():
        prev = a.get(pid)
        # cpu 字段为纳秒累计值，差值 / 1e9 / 采样秒数 × 100
        cpu = max(0.0, (v["cpu"] - prev["cpu"]) / 1e9 / 2.0 * 100) if prev else 0.0
        rows.append((v["rss"], cpu, v["name"], pid))
    rows.sort(reverse=True)
    tot = 0.0
    for rss, cpu, name, pid in rows[:10]:
        tot += rss
        print(f"  {rss/1024/1024:7.0f} MB  {cpu:5.1f}%  {name[:38]:<38} {pid}")
    allrss = sum(r[0] for r in rows) / GB
    print(f"  ── 全部进程 RSS 合计 {allrss:.2f} GB")

    dup = {}
    for rss, cpu, name, pid in rows:
        if name == "Electron":
            dup.setdefault("Electron", [0, 0])
            dup["Electron"][0] += 1
            dup["Electron"][1] += rss
    if dup.get("Electron"):
        c, sz = dup["Electron"]
        print(f"  ⚠️  匿名 Electron 进程 {c} 个，合计 {sz/1024/1024:.0f} MB（建议查归属）")

    print("\n【写入超额报告 · 近 12 小时 · 按速率排序】")
    print("  注：2147 MB 是系统日配额上限，撞线值不代表实际写入量；判异常看速率。")
    hits = do_diag()
    if not hits:
        print("  无（未触发系统 disk writes 限额）✅")
    else:
        for rate, cmd, w, t in hits[:6]:
            print(f"  {rate:7.2f} MB/s  {cmd:<26} {t}")
        if hits[0][0] > 5:
            alerts.append(f"写入速率最高 {hits[0][0]:.1f} MB/s（{hits[0][1]}）—— 用 MODULE.md『C 象限』查堆栈")

    print("\n【睡眠断言】")
    bad = do_assertions()
    if bad:
        for line in bad[:5]:
            print(f"  ⚠️  {line}")
        alerts.append("有进程阻止显示器休眠（影刀/AutoClaw 常见），会亮屏+发烫")
    else:
        print("  无异常断言 ✅")

    print("\n" + "=" * 52)
    if alerts:
        print("【诊断要点】")
        for i, x in enumerate(alerts, 1):
            print(f"  {i}. {x}")
    else:
        print("【诊断要点】各项指标正常，卡顿更可能来自单个 App 自身（见 MODULE.md D 象限）。")
    print("=" * 52)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
