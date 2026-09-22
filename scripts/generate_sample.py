#!/usr/bin/env python3
"""生成三份模拟飞行日志 CSV，用于演示与测试。

sample_flight_1.csv：正常飞行（静置 -> 放电 -> 静置 -> 充电 -> 静置），
    含 6 节单体电压，仅出现一次 0.16V 的瞬时尖峰（不足 5s，不告警）。
sample_flight_2.csv：老化电池，容量衰减、发热偏高，且日志在放电中截断（未闭合循环）；
    第 6 节单体持续偏弱（放电中段持续压差超差），其中夹杂 4s 采样断点与一行缺值。
sample_flight_3.csv：循环分析正常的飞行日志，但注入四类数据质量问题，
    用于演示导入前质量审计：时间戳乱序、重复时间、异常采样间隔、
    电压/电流/温度离群尖峰（质量问题不阻断循环分析）。

用法：python3 scripts/generate_sample.py  （输出到 sample_data/）
"""
import csv
import math
import os
import random
from datetime import datetime, timedelta, timezone

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "sample_data")
N_CELLS = 6

HEADER = ["time", "voltage", "current", "temperature",
          "cell1_voltage", "cell2_voltage", "cell3_voltage",
          "cell4_voltage", "cell5_voltage", "单体6电压"]


def ocv(soc: float) -> float:
    """简化开路电压曲线（电池包 6S 量级，V）。"""
    v = 19.6 + 5.4 * soc
    if soc < 0.15:                      # 低电量拐点
        v -= (0.15 - soc) * 12.0
    return v


def cell_voltages(pack_v: float, i: float, rng: random.Random,
                  weak: int, weak_mv: float, spike: bool) -> list:
    """把总压拆成 N_CELLS 节：指定编号单体偏弱 weak_mv（mV），其余微小不一致。"""
    offsets = [rng.gauss(0, 0.006) for _ in range(N_CELLS)]
    if weak > 0:
        # 偏弱单体承担全部固定偏置，保证平均后合计等于总压
        offsets[weak - 1] -= weak_mv / 1000.0
    if spike:
        offsets[weak - 1 if weak else 0] -= 0.16
    mean = sum(offsets) / N_CELLS
    base = pack_v / N_CELLS
    return [round(base + offsets[k] - mean, 4) for k in range(N_CELLS)]


def simulate(start: datetime, capacity_ah: float, ir_ohm: float,
             discharge_a: float, charge_a: float, ambient: float,
             heat_gain: float, cutoff_soc: float, unclosed_tail: bool,
             weak_cell: int, weak_mv: float, seed: int) -> list:
    """模拟一段飞行并返回行列表 [(时间戳, 电压, 电流, 温度, 单体)]。"""
    rng = random.Random(seed)
    rows = []
    t = 0.0
    soc = 1.0
    temp = ambient + 1.0

    def emit(i: float, dt: float = 1.0, weak_now_mv: float | None = None,
             spike: bool = False, missing_cell: bool = False) -> None:
        nonlocal t, soc, temp
        soc -= i * dt / 3600.0 / capacity_ah
        soc = min(max(soc, 0.0), 1.0)
        v = ocv(soc) - i * ir_ohm + rng.gauss(0, 0.02)
        temp += (i * i * ir_ohm * heat_gain - (temp - ambient) * 0.004) * dt
        cells = cell_voltages(v, i, rng, weak_cell,
                              weak_now_mv if weak_now_mv is not None else weak_mv,
                              spike)
        if missing_cell:
            cells[weak_cell - 1] = None  # 该行单体缺值 -> 跳过单体分析
        rows.append((start + timedelta(seconds=t),
                     round(v, 3), round(i + rng.gauss(0, 0.02), 3),
                     round(temp + rng.gauss(0, 0.1), 2), cells))
        t += dt

    def run(seconds: float, current_fn, cell_fn=None) -> None:
        elapsed = 0.0
        while elapsed < seconds:
            kw = cell_fn(elapsed) if cell_fn else {}
            emit(current_fn(elapsed), **kw)
            elapsed += kw.get("dt", 1.0)

    def run_until_soc(target: float, current_fn, discharging: bool,
                      cell_fn=None) -> None:
        guard = 0
        while guard < 20000:
            if discharging and soc <= target:
                break
            if not discharging and soc >= target:
                break
            kw = cell_fn(guard) if cell_fn else {}
            emit(current_fn(guard), **kw)
            guard += 1

    run(120, lambda _: 0.0)                                   # 起飞前置静置
    if weak_cell:
        # 老化电池：放电中段持续失衡；400s 处插入 4s 断点、一行缺值与一次短暂恢复
        def weak_discharge(e):
            if e == 400:
                return {"dt": 5.0}                            # 4s 采样断点（区间中断）
            if e == 500:
                return {"missing_cell": True}                 # 单体缺值（该行跳过）
            weak_now = weak_mv if not (620 <= e < 623) else 0
            return {"weak_now_mv": weak_now}                  # 3s 短暂恢复仍超 5s 总时长
        cell_fn = weak_discharge
    else:
        # 正常电池：放电 300s 处一次瞬时尖峰（单样本，不构成持续区间）
        cell_fn = lambda e: {"spike": e == 300}
    run_until_soc(cutoff_soc,                                 # 主放电段（放到截止电量）
                  lambda e: discharge_a + 6 * math.sin(e / 90.0)
                  + rng.uniform(-1, 1), discharging=True,
                  cell_fn=cell_fn)
    if unclosed_tail:
        return rows                                        # 放电中截断
    run(180, lambda _: 0.0)                                   # 静置（电压回升）
    run_until_soc(0.99, lambda _: -charge_a, discharging=False)  # 充电段
    run(120, lambda _: 0.0)                                   # 充满静置
    return rows


def inject_quality_issues(rows: list) -> list:
    """在正常飞行数据上注入四类数据质量问题（演示导入前质量审计）。

    仅改动时间戳与数值，行数不变，循环分析仍可正常进行。
    """
    rows = list(rows)
    # 1) 异常采样间隔：第 200 行之后整体后移 25s（制造一处 26s 大间隔）
    for k in range(201, len(rows)):
        ts, v, i, tp, cells = rows[k]
        rows[k] = (ts + timedelta(seconds=25), v, i, tp, cells)
    # 2) 重复时间：连续 3 行时间戳与上一行相同
    for k in (301, 302, 303):
        ts, v, i, tp, cells = rows[k]
        rows[k] = (rows[k - 1][0], v, i, tp, cells)
    # 3) 时间戳乱序：连续 4 行时间戳逆序（后 3 行逐行回退）
    block = rows[400:404]
    rows[400:404] = [(block[3 - k][0], *block[k][1:]) for k in range(4)]
    # 4) 稳健离群点：电压 / 电流 / 温度尖峰
    def bump(idx: int, dv: float = 0.0, di: float = 0.0, dtp: float = 0.0) -> None:
        ts, v, i, tp, cells = rows[idx]
        rows[idx] = (ts, round(v + dv, 3), round(i + di, 3),
                     round(tp + dtp, 2), cells)
    bump(500, dv=7.5)
    bump(501, dv=7.2)
    bump(520, di=60.0)
    bump(540, dtp=55.0)
    bump(541, dtp=52.0)
    return rows


def write_csv(path: str, rows) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for ts, v, i, tp, cells in rows:
            w.writerow([ts.isoformat(timespec="seconds"), v, i, tp, *cells])
    print(f"已生成 {path}（{len(rows)} 行，{N_CELLS} 节单体）")


def main() -> None:
    os.makedirs(OUT_DIR, exist_ok=True)
    base = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc)
    write_csv(os.path.join(OUT_DIR, "sample_flight_1.csv"),
              simulate(base, capacity_ah=2.2, ir_ohm=0.012, discharge_a=8.0,
                       charge_a=5.0, ambient=25.0, heat_gain=0.02,
                       cutoff_soc=0.15, unclosed_tail=False,
                       weak_cell=0, weak_mv=0, seed=7))
    write_csv(os.path.join(OUT_DIR, "sample_flight_2.csv"),
              simulate(base + timedelta(days=1),
                       capacity_ah=1.8, ir_ohm=0.02, discharge_a=8.5,
                       charge_a=5.0, ambient=28.0, heat_gain=0.05,
                       cutoff_soc=0.10, unclosed_tail=True,
                       weak_cell=6, weak_mv=130, seed=13))
    # 样例 3：循环分析正常，但注入四类数据质量问题（演示导入前质量审计）
    rows3 = simulate(base + timedelta(days=2),
                     capacity_ah=2.0, ir_ohm=0.012, discharge_a=8.0,
                     charge_a=5.0, ambient=25.0, heat_gain=0.02,
                     cutoff_soc=0.15, unclosed_tail=False,
                     weak_cell=0, weak_mv=0, seed=21)
    write_csv(os.path.join(OUT_DIR, "sample_flight_3.csv"),
              inject_quality_issues(rows3))


if __name__ == "__main__":
    main()
