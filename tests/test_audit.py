"""数据质量审计与导入管线的单元测试。

可用 pytest 运行，也可直接执行：python3 tests/test_audit.py
（审计与导入模块均为纯标准库实现，无需安装第三方依赖。）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.analysis import detect_cycles  # noqa: E402
from app.audit import audit_csv_text  # noqa: E402
from app.importer import parse_flight_csv, split_csv_rows  # noqa: E402

HEADER = "time,voltage,current,temperature"


def make_csv(rows, header=HEADER):
    """rows: [(t, v, i, tp), ...] -> CSV 文本。"""
    lines = [header]
    lines += [",".join(map(str, r)) for r in rows]
    return "\n".join(lines) + "\n"


def normal_rows(n=200, i=8.0, t0=0.0, dt=1.0):
    """确定性正常数据：电压/电流/温度仅有微小周期性波动。"""
    rows = []
    for k in range(n):
        rows.append((t0 + k * dt,
                     round(25.0 + (0.01 if k % 2 else -0.01), 3),
                     round(i + (0.05 if k % 2 else -0.05), 3),
                     round(26.0 + (0.1 if k % 2 else -0.1), 2)))
    return rows


def issues_of(audit, itype):
    return [s for s in audit["segments"] if s["type"] == itype]


# ---------------------------------------------------------------- 基本审计

def test_clean_file_has_no_issues():
    audit = audit_csv_text(make_csv(normal_rows(200)))
    s = audit["summary"]
    assert s["error"] == ""
    assert s["issue_count"] == 0
    assert s["issue_rows"] == 0
    assert s["segment_count"] == 0
    assert s["quality_score"] == 100
    assert s["quality_grade"] == "good"
    assert s["median_interval_s"] == 1.0
    assert audit["segments"] == []
    # 四类检查均出现在 by_type 中（计数为 0）
    assert set(s["by_type"]) == {"time_out_of_order", "time_duplicate",
                                 "interval_abnormal", "outlier"}


def test_time_out_of_order_segment():
    rows = normal_rows(100)
    # 索引 50~55 共 6 行逆序 -> 后 5 行逐行回退，构成乱序
    block = rows[50:56]
    rows[50:56] = [(block[5 - k][0], *block[k][1:]) for k in range(6)]
    audit = audit_csv_text(make_csv(rows))
    segs = issues_of(audit, "time_out_of_order")
    assert len(segs) == 1
    seg = segs[0]
    assert seg["issue_count"] == 5
    assert seg["start_line"] == 53   # 索引 51 + 表头偏移 2
    assert seg["end_line"] == 57
    assert seg["fields"] == ["time"]
    assert seg["field_labels"] == ["时间"]
    # 时间范围：乱序行的相对时间
    assert seg["start_t"] == 50.0
    assert seg["end_t"] == 54.0
    assert seg["start_time"] is None  # 数值秒时间戳无墙钟时间
    assert len(seg["examples"]) == 3
    assert seg["examples"][0]["line"] == 53
    assert audit["summary"]["issue_rows"] >= 5


def test_duplicate_time_segment():
    rows = normal_rows(100)
    for k in (30, 31, 32):  # 连续 3 行与上一行时间相同
        t, v, i, tp = rows[k]
        rows[k] = (rows[29][0], v, i, tp)
    audit = audit_csv_text(make_csv(rows))
    segs = issues_of(audit, "time_duplicate")
    assert len(segs) == 1
    assert segs[0]["issue_count"] == 3
    assert segs[0]["start_line"] == 32
    assert segs[0]["end_line"] == 34
    assert segs[0]["lines"] == [32, 33, 34]


def test_interval_gap_and_dense():
    rows = normal_rows(200)
    # 大间隔：索引 60 起整体后移 30s
    for k in range(60, len(rows)):
        t, v, i, tp = rows[k]
        rows[k] = (t + 30.0, v, i, tp)
    # 过密：文件末尾索引 190~199 改为 0.1s 间隔（避免尾随间隔被并入）
    base = rows[189][0]
    for k in range(190, 200):
        t, v, i, tp = rows[k]
        rows[k] = (base + (k - 189) * 0.1, v, i, tp)
    audit = audit_csv_text(make_csv(rows))
    segs = issues_of(audit, "interval_abnormal")
    assert len(segs) == 2  # 大间隔与过密区段行号相距较远，不合并
    gap = [s for s in segs if s["start_line"] == 62]
    dense = [s for s in segs if s["start_line"] >= 192]
    assert len(gap) == 1 and gap[0]["issue_count"] == 1
    assert len(dense) == 1 and dense[0]["issue_count"] == 10
    assert audit["summary"]["median_interval_s"] == 1.0


def test_outlier_fields_and_merge():
    rows = normal_rows(200)
    # 相邻离群（行号间隔 ≤3）应合并为一个区段
    rows[100] = (rows[100][0], 33.0, rows[100][2], rows[100][3])  # 电压尖峰
    rows[103] = (rows[103][0], 32.5, rows[103][2], rows[103][3])  # 电压尖峰
    rows[104] = (rows[104][0], rows[104][1], 75.0, rows[104][3])  # 电流尖峰
    # 远处温度尖峰单独成段
    rows[150] = (rows[150][0], rows[150][1], rows[150][2], 95.0)
    audit = audit_csv_text(make_csv(rows))
    segs = issues_of(audit, "outlier")
    assert len(segs) == 2
    first, second = segs
    assert first["issue_count"] == 3
    assert first["start_line"] == 102 and first["end_line"] == 106
    # 受影响字段为并集且按固定顺序
    assert first["fields"] == ["voltage", "current"]
    assert first["field_labels"] == ["电压", "电流"]
    assert second["issue_count"] == 1
    assert second["fields"] == ["temperature"]
    # 证据样例保留原始行号与数值
    assert first["examples"][0]["values"]["voltage"] == 33.0


def test_normal_data_not_flagged_as_outlier():
    """微小波动不应触发离群（MAD 下限保护）。"""
    audit = audit_csv_text(make_csv(normal_rows(300)))
    assert audit["summary"]["by_type"]["outlier"]["issues"] == 0


def test_quality_score_and_grade():
    rows = normal_rows(100)
    for k in range(10, 20):  # 10 行逐行回退 -> 10 行乱序
        t, v, i, tp = rows[k]
        rows[k] = (9.0 - (k - 9), v, i, tp)
    audit = audit_csv_text(make_csv(rows))
    s = audit["summary"]
    assert s["issue_rows"] >= 10
    assert s["quality_score"] <= 90
    assert s["quality_grade"] in ("fair", "poor")


def test_iso_timestamp_segments_have_wall_clock():
    from datetime import datetime, timezone
    epoch0 = datetime(2026, 9, 20, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    rows = []
    for k in range(50):
        rows.append((epoch0 + k, 25.0, 8.0, 26.0))
    for k in (20, 21, 22):
        t, v, i, tp = rows[k]
        rows[k] = (t - 100, v, i, tp)
    audit = audit_csv_text(make_csv(rows))
    seg = issues_of(audit, "time_out_of_order")[0]
    assert seg["start_time"] is not None
    assert seg["start_time"].startswith("2026-09-20")
    assert seg["examples"][0]["time"].startswith("2026-09-20")


# ---------------------------------------------------------------- 健壮性

def test_audit_never_raises_on_garbage():
    for text in ("", "\n\n\n", "garbage without header",
                 "a,b,c\n1,2,3\n", "time,voltage\n1,2\n"):
        audit = audit_csv_text(text)
        assert audit["segments"] == []
        assert audit["summary"]["error"]
        assert audit["summary"]["quality_score"] is None


def test_unparseable_cells_are_skipped_not_flagged():
    rows = normal_rows(50)
    rows[25] = ("bad-time", "xx", "yy", "zz")
    audit = audit_csv_text(make_csv(rows))
    # 无法解析的行不参与检查，也不应让审计崩溃
    assert audit["summary"]["error"] == ""
    assert audit["summary"]["valid_time_rows"] == 49


def test_original_line_numbers_preserved_with_blank_lines():
    text = make_csv(normal_rows(30))
    lines = text.splitlines()
    # 在第 10 行（数据第 9 行）后插入空行
    lines.insert(10, "")
    lines.insert(20, "   ")
    text = "\n".join(lines) + "\n"
    headers, data_rows = split_csv_rows(text)
    assert headers == HEADER.split(",")
    # 空行不占位：第 12 个数据行的原始行号应 > 12
    assert data_rows[0][0] == 2
    assert data_rows[9][0] == 12   # 第 10 个数据行前面有一个空行
    assert data_rows[18][0] == 22  # 两个空行都已跳过
    # 审计行号与原始文件一致
    rows = normal_rows(30)
    rows[15] = (rows[15][0], 33.0, rows[15][2], rows[15][3])
    text2 = make_csv(rows)
    lines2 = text2.splitlines()
    lines2.insert(5, "")
    audit = audit_csv_text("\n".join(lines2) + "\n")
    seg = issues_of(audit, "outlier")[0]
    assert seg["start_line"] == 18  # 索引 15 + 表头 1 + 空行 1 + 1


# ---------------------------------------------------------------- 不阻断分析

def flight_rows_with_issues():
    """一段完整飞行（静置-放电-静置），叠加四类质量问题。"""
    rows = []
    t = 0.0
    for k in range(30):   # 静置
        rows.append((t, 25.0, 0.0, 26.0))
        t += 1.0
    for k in range(120):  # 放电
        rows.append((t, 24.0, 8.0, 30.0))
        t += 1.0
    for k in range(30):   # 静置
        rows.append((t, 24.8, 0.0, 27.0))
        t += 1.0
    # 乱序
    for k in range(50, 54):
        tt, v, i, tp = rows[k]
        rows[k] = (tt - 20.0, v, i, tp)
    # 重复
    for k in (70, 71):
        tt, v, i, tp = rows[k]
        rows[k] = (rows[69][0], v, i, tp)
    # 大间隔
    for k in range(90, len(rows)):
        tt, v, i, tp = rows[k]
        rows[k] = (tt + 15.0, v, i, tp)
    # 离群
    rows[100] = (rows[100][0], 33.0, rows[100][2], rows[100][3])
    rows[110] = (rows[110][0], rows[110][1], rows[110][2], 90.0)
    return rows


def test_quality_issues_do_not_block_cycle_analysis():
    text = make_csv(flight_rows_with_issues())
    audit = audit_csv_text(text)
    assert audit["summary"]["issue_count"] > 0
    assert audit["summary"]["segment_count"] >= 4
    # 可解析行仍正常导入并完成循环分析
    samples, meta = parse_flight_csv(text)
    assert len(samples) == 180
    cycles = detect_cycles(samples)
    discharge = [c for c in cycles if c.kind == "discharge"]
    assert len(discharge) == 1
    assert discharge[0].capacity_ah > 0.2  # 约 8A × 120s ≈ 0.27Ah


def test_audit_and_importer_share_line_numbers():
    """审计与导入的跳过行号基于同一套原始行号。"""
    text = make_csv(flight_rows_with_issues())
    audit = audit_csv_text(text)
    _, meta = parse_flight_csv(text)
    assert meta["skipped_rows"] == 0  # 所有行均可解析
    assert audit["summary"]["total_rows"] == 180


# ---------------------------------------------------------------- 数据库迁移

def test_db_migration_adds_audit_column():
    os.environ["BATTERY_DB"] = os.path.join(
        tempfile.mkdtemp(), "test_battery.db")
    import importlib
    import app.database as db
    importlib.reload(db)
    db.init_db()
    conn = db.get_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(logs)")}
        assert "audit" in cols
        # 再次初始化（模拟旧库升级路径）不报错
        db.init_db()
    finally:
        conn.close()


# ---------------------------------------------------------------- 直接运行

def _run_all():
    fns = [(name, fn) for name, fn in sorted(globals().items())
           if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(fns) - failed}/{len(fns)} 通过")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
