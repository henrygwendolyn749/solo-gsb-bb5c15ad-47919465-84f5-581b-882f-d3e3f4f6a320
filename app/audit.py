"""导入前数据质量审计。

在保留原始行号的原始 CSV 行上执行四类检查：

- 时间戳乱序（time_out_of_order）：当前行时间早于上一有效时间行；
- 重复时间（time_duplicate）：相邻有效时间行时间差为 0；
- 异常采样间隔（interval_abnormal）：间隔偏离全文件中位间隔（过大 / 过密）；
- 稳健离群点（outlier）：电压 / 电流 / 温度相对双侧局部邻域中位数的孤立尖峰
  （修正 z 分数 > 4.0，噪声尺度取 1.4826×邻域 MAD 与字段下限的较大者；
  充放电切换等阶跃工况只有一侧偏离，不会误判为离群）。

行号相邻（间隔 ≤ MERGE_GAP_LINES）的同类问题合并为质量区段，区段标明类型、
起止行、时间范围与受影响字段，并保留少量样例行作为证据。审计只读原始文本、
绝不抛异常，也不阻断可解析行的导入与循环分析。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import median
from typing import Optional

from .importer import match_log_columns, split_csv_rows, try_parse_time

# ---- 可调阈值 ----
GAP_FACTOR = 3.0            # 采样间隔 > 中位间隔 × 该倍数 → 间隔过大
DENSE_FACTOR = 3.0          # 采样间隔 < 中位间隔 ÷ 该倍数 → 间隔过密
MIN_INTERVAL_SAMPLES = 8    # 至少累积这么多有效间隔才评估采样间隔
OUTLIER_Z = 4.0             # 修正 z 分数阈值（稳健离群判定）
OUTLIER_WINDOW = 8          # 局部邻域每侧样本数
OUTLIER_MIN_SIDE = 3        # 每侧至少这么多有效邻域点才评估（文件首尾不评估）
MIN_OUTLIER_SAMPLES = 10    # 至少这么多有效残差才估计噪声尺度
MIN_SCALE = {"voltage": 0.05, "current": 0.05,
             "temperature": 0.5}     # 噪声尺度下限（防止 MAD=0 时误报）
MERGE_GAP_LINES = 3         # 同类问题行号间隔 ≤ 该值时合并为同一质量区段
EXAMPLE_ROWS = 3            # 每个区段保留的证据样例行数
MAX_SEGMENT_LINES = 100     # 每个区段记录的行号上限（超出仅计数）

ISSUE_LABELS = {
    "time_out_of_order": "时间戳乱序",
    "time_duplicate": "重复时间",
    "interval_abnormal": "采样间隔异常",
    "outlier": "数值离群",
}
FIELD_LABELS = {"time": "时间", "voltage": "电压",
                "current": "电流", "temperature": "温度"}
FIELD_ORDER = ["time", "voltage", "current", "temperature"]
GRADE_LABELS = {"good": "良好", "fair": "一般", "poor": "较差"}

_NUMERIC_FIELDS = ("voltage", "current", "temperature")


def _try_float(s: str) -> Optional[float]:
    try:
        v = float(s.strip())
    except (ValueError, AttributeError):
        return None
    return v if math.isfinite(v) else None


def _iso(t_abs: float) -> str:
    return datetime.fromtimestamp(t_abs, tz=timezone.utc).isoformat()


def audit_csv_text(text: str) -> dict:
    """审计原始 CSV 文本，返回 {"summary": {...}, "segments": [...]}。

    该函数绝不抛异常：审计失败时返回带 error 的摘要，导入流程不受影响。
    """
    try:
        return _audit(text)
    except Exception as exc:  # 审计是辅助环节，任何意外都不得阻断导入
        return {"summary": _empty_summary(f"审计过程异常：{exc}"),
                "segments": []}


def _empty_summary(error: str, total_rows: int = 0) -> dict:
    return {
        "total_rows": total_rows,
        "valid_time_rows": 0,
        "issue_rows": 0,
        "issue_count": 0,
        "segment_count": 0,
        "by_type": {t: {"label": label, "issues": 0, "segments": 0}
                    for t, label in ISSUE_LABELS.items()},
        "median_interval_s": None,
        "fields_checked": [],
        "quality_score": None,
        "quality_grade": None,
        "quality_label": None,
        "error": error,
    }


def _audit(text: str) -> dict:
    headers, data_rows = split_csv_rows(text)
    if not headers or not data_rows:
        return {"summary": _empty_summary("CSV 内容为空或缺少数据行"),
                "segments": []}
    cols = match_log_columns(headers)
    missing = [name for name, idx in cols.items() if idx is None]
    if missing:
        return {"summary": _empty_summary(f"缺少必需列: {', '.join(missing)}",
                                          len(data_rows)),
                "segments": []}

    # 1) 类型化提取（保留原始行号；无法解析的值记 None，仅不参与对应检查）
    records = []
    for line_no, row in data_rows:
        def cell(idx, _row=row):
            return _row[idx] if idx is not None and idx < len(_row) else ""
        parsed_t = try_parse_time(cell(cols["time"]))
        t_val = parsed_t[0] if parsed_t else None
        if t_val is not None and not math.isfinite(t_val):
            t_val, parsed_t = None, None
        records.append({
            "line": line_no,
            "t": t_val,
            "epoch": bool(parsed_t and parsed_t[1]),
            "raw_time": cell(cols["time"]).strip(),
            "voltage": _try_float(cell(cols["voltage"])),
            "current": _try_float(cell(cols["current"])),
            "temperature": _try_float(cell(cols["temperature"])),
        })

    first_valid = next((r for r in records if r["t"] is not None), None)
    t0 = first_valid["t"] if first_valid else None
    epoch_based = first_valid["epoch"] if first_valid else False

    issues: list[dict] = []

    def add_issue(rec, itype, fields, detail, values):
        issues.append({"line": rec["line"], "type": itype, "fields": fields,
                       "t_abs": rec["t"], "detail": detail, "values": values})

    # 2) 时间戳乱序 / 重复时间，并收集有效正向间隔
    prev: Optional[dict] = None
    intervals: list[tuple[dict, float, int]] = []  # (当前行, 间隔s, 上一有效行号)
    for rec in records:
        if rec["t"] is None:
            continue
        if prev is not None:
            dt = rec["t"] - prev["t"]
            if dt < -1e-9:
                add_issue(rec, "time_out_of_order", ["time"],
                          f"时间较上一有效行（第 {prev['line']} 行）回退 {-dt:.3f}s",
                          {"time": rec["raw_time"]})
            elif dt <= 1e-9:
                add_issue(rec, "time_duplicate", ["time"],
                          f"与上一有效行（第 {prev['line']} 行）时间戳相同",
                          {"time": rec["raw_time"]})
            else:
                intervals.append((rec, dt, prev["line"]))
        prev = rec

    # 3) 异常采样间隔（相对全文件中位间隔，过大 / 过密）
    med_interval = (median(dt for _, dt, _ in intervals)
                    if len(intervals) >= MIN_INTERVAL_SAMPLES else None)
    if med_interval and med_interval > 0:
        for rec, dt, prev_line in intervals:
            if dt > med_interval * GAP_FACTOR:
                add_issue(rec, "interval_abnormal", ["time"],
                          f"与上一有效行（第 {prev_line} 行）间隔 {dt:.3f}s，"
                          f"达中位间隔 {med_interval:.3f}s 的 "
                          f"{dt / med_interval:.1f} 倍",
                          {"interval_s": round(dt, 3)})
            elif dt < med_interval / DENSE_FACTOR:
                add_issue(rec, "interval_abnormal", ["time"],
                          f"与上一有效行（第 {prev_line} 行）间隔 {dt:.3f}s，"
                          f"不足中位间隔 {med_interval:.3f}s 的 "
                          f"1/{DENSE_FACTOR:.0f}",
                          {"interval_s": round(dt, 3)})

    # 4) 电压 / 电流 / 温度稳健离群点（双侧局部邻域中位数检验）
    for field in _NUMERIC_FIELDS:
        pts = [(r, r[field]) for r in records if r[field] is not None]
        for rec, x, ref, z in _local_outliers(pts, field):
            add_issue(rec, "outlier", [field],
                      f"{FIELD_LABELS[field]} {x:g} 偏离局部邻域中位数 {ref:g}"
                      f"（修正 z={z:.1f} > {OUTLIER_Z:g}）",
                      {field: x})

    segments = _merge_segments(issues, t0, epoch_based, med_interval)
    summary = _build_summary(records, issues, segments, med_interval)
    return {"summary": summary, "segments": segments}


def _local_outliers(pts, field):
    """双侧局部邻域中位数检验，返回 [(rec, 值, 邻域中位数, 修正z)]。

    飞行日志是多工况时间序列（静置 / 放电 / 充电的电流水准与噪声水平都不同，
    温度缓慢漂移），全局分布检验会把合法工况误判为离群，因此逐点检查：
    取左右各 OUTLIER_WINDOW 个邻域样本，仅当该点相对两侧中位数都显著偏离
    （修正 z 分数 > OUTLIER_Z）时判为孤立尖峰；阶跃式工况切换只有一侧偏离，
    不会误报。噪声尺度同样取局部值（邻域 MAD），适配各工况不同的噪声水平；
    持续超过邻域窗口的整段异常不属于"孤立尖峰"，不在本检查范围内。
    """
    n = len(pts)
    if n < MIN_OUTLIER_SAMPLES:
        return []
    flagged = []
    for k in range(n):
        left = [pts[j][1] for j in range(max(0, k - OUTLIER_WINDOW), k)]
        right = [pts[j][1] for j in range(k + 1,
                                          min(n, k + 1 + OUTLIER_WINDOW))]
        if len(left) < OUTLIER_MIN_SIDE or len(right) < OUTLIER_MIN_SIDE:
            continue
        ref = median(left + right)
        mad = median(abs(v - ref) for v in left + right)
        scale = max(1.4826 * mad, MIN_SCALE[field])
        thr = OUTLIER_Z * scale
        x = pts[k][1]
        dev = min(abs(x - median(left)), abs(x - median(right)))
        if dev > thr:
            flagged.append((pts[k][0], x, ref, dev / scale))
    return flagged


def _build_summary(records, issues, segments, med_interval) -> dict:
    total = len(records)
    issue_rows = len({i["line"] for i in issues})
    # 评分 = 100 − 问题行占比% − 每区段 3 分（多处分散问题比单处集中更值得关注）
    score = (max(0, round(100 - 100 * issue_rows / total - 3 * len(segments)))
             if total else None)
    if score is None:
        grade = None
    elif score >= 95:
        grade = "good"
    elif score >= 80:
        grade = "fair"
    else:
        grade = "poor"
    by_type = {}
    for itype, label in ISSUE_LABELS.items():
        segs = [s for s in segments if s["type"] == itype]
        by_type[itype] = {"label": label,
                          "issues": sum(s["issue_count"] for s in segs),
                          "segments": len(segs)}
    return {
        "total_rows": total,
        "valid_time_rows": sum(1 for r in records if r["t"] is not None),
        "issue_rows": issue_rows,
        "issue_count": len(issues),
        "segment_count": len(segments),
        "by_type": by_type,
        "median_interval_s": round(med_interval, 3) if med_interval else None,
        "fields_checked": list(_NUMERIC_FIELDS),
        "quality_score": score,
        "quality_grade": grade,
        "quality_label": GRADE_LABELS[grade] if grade else None,
        "error": "",
    }


def _merge_segments(issues, t0, epoch_based, med_interval) -> list[dict]:
    """按类型分组，行号相邻（≤ MERGE_GAP_LINES）的问题合并为质量区段。"""
    segments: list[dict] = []
    for itype in ISSUE_LABELS:
        group = sorted((i for i in issues if i["type"] == itype),
                       key=lambda i: i["line"])
        run: list[dict] = []
        last_line: Optional[int] = None
        for issue in group:
            if run and issue["line"] - last_line > MERGE_GAP_LINES:
                segments.append(_build_segment(run, itype, t0, epoch_based,
                                               med_interval))
                run = []
            run.append(issue)
            last_line = issue["line"]
        if run:
            segments.append(_build_segment(run, itype, t0, epoch_based,
                                           med_interval))
    segments.sort(key=lambda s: (s["start_line"], s["type"]))
    for n, seg in enumerate(segments, start=1):
        seg["id"] = f"seg-{n}"
    return segments


def _segment_detail(itype, run, fields, med_interval) -> str:
    n = len(run)
    if itype == "time_out_of_order":
        return f"{n} 行时间戳早于上一有效行，文件顺序与时间顺序不一致。"
    if itype == "time_duplicate":
        return f"{n} 行与上一有效行时间戳完全相同。"
    if itype == "interval_abnormal":
        med_txt = f"{med_interval:g}" if med_interval else "?"
        return f"{n} 处采样间隔偏离全文件中位间隔（{med_txt}s）。"
    names = "、".join(FIELD_LABELS[f] for f in fields)
    return (f"{n} 行{names}为稳健离群点（偏离双侧局部邻域中位数，"
            f"修正 z 分数 > {OUTLIER_Z:g}）。")


def _build_segment(run, itype, t0, epoch_based, med_interval) -> dict:
    lines = [i["line"] for i in run]
    fields = [f for f in FIELD_ORDER if any(f in i["fields"] for i in run)]
    ts = [i["t_abs"] for i in run if i["t_abs"] is not None]
    examples = []
    for i in run[:EXAMPLE_ROWS]:
        ex = {"line": i["line"], "detail": i["detail"], "values": i["values"]}
        if i["t_abs"] is not None and t0 is not None:
            ex["t"] = round(i["t_abs"] - t0, 3)
        if i["t_abs"] is not None and epoch_based:
            ex["time"] = _iso(i["t_abs"])
        examples.append(ex)
    return {
        "id": "",  # 合并排序后统一编号
        "type": itype,
        "type_label": ISSUE_LABELS[itype],
        "start_line": lines[0],
        "end_line": lines[-1],
        "issue_count": len(run),
        "lines": lines[:MAX_SEGMENT_LINES],
        "lines_truncated": len(lines) > MAX_SEGMENT_LINES,
        "fields": fields,
        "field_labels": [FIELD_LABELS[f] for f in fields],
        "start_t": round(min(ts) - t0, 3) if ts and t0 is not None else None,
        "end_t": round(max(ts) - t0, 3) if ts and t0 is not None else None,
        "start_time": _iso(min(ts)) if ts and epoch_based else None,
        "end_time": _iso(max(ts)) if ts and epoch_based else None,
        "detail": _segment_detail(itype, run, fields, med_interval),
        "examples": examples,
    }
