/* 无人机电池循环健康评估 —— 原生前端逻辑 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { batteries: [], currentId: null, currentLogId: null, logDetail: null };

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(msg, isError = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "show" + (isError ? " error" : "");
  setTimeout(() => { el.className = ""; }, 3200);
}

const fmt = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
const fmtDur = (s) => {
  if (s === null || s === undefined) return "—";
  const m = Math.floor(s / 60), sec = Math.round(s % 60);
  return `${m}分${String(sec).padStart(2, "0")}秒`;
};
const fmtTime = (iso) => iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }) : "—";

/* ---------------- 电池列表 ---------------- */

async function loadBatteries(keepSelection = false) {
  state.batteries = await api("/api/batteries");
  const ul = $("#battery-list");
  ul.innerHTML = "";
  for (const b of state.batteries) {
    const li = document.createElement("li");
    li.dataset.id = b.id;
    li.innerHTML = `<div class="b-name"></div>
      <div class="b-sub">额定 ${b.rated_capacity_ah}Ah · ${b.safe_voltage_min}~${b.safe_voltage_max}V</div>
      <div class="b-sub">日志 ${b.log_count} 份 · 放电循环 ${b.discharge_cycles} 个</div>`;
    li.querySelector(".b-name").textContent = b.name;
    if (Number(b.id) === Number(state.currentId)) li.classList.add("active");
    li.onclick = () => selectBattery(b.id);
    ul.appendChild(li);
  }
  if (!keepSelection && state.batteries.length && state.currentId === null) {
    selectBattery(state.batteries[0].id);
  }
  if (!state.batteries.length) {
    state.currentId = null;
    $("#battery-view").hidden = true;
    $("#empty-tip").hidden = false;
  }
}

$("#battery-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    name: f.name.value.trim(),
    rated_capacity_ah: parseFloat(f.rated_capacity_ah.value),
    safe_voltage_min: parseFloat(f.safe_voltage_min.value),
    safe_voltage_max: parseFloat(f.safe_voltage_max.value),
    notes: f.notes.value.trim(),
  };
  try {
    const b = await api("/api/batteries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    f.reset();
    state.currentId = b.id;
    await loadBatteries(true);
    selectBattery(b.id);
    toast("电池已创建");
  } catch (err) { toast(err.message, true); }
};

/* ---------------- 电池详情 ---------------- */

async function selectBattery(id) {
  state.currentId = id;
  state.currentLogId = null;
  state.logDetail = null;
  document.querySelectorAll("#battery-list li").forEach((li) =>
    li.classList.toggle("active", Number(li.dataset.id) === Number(id)));
  $("#empty-tip").hidden = true;
  $("#battery-view").hidden = false;
  $("#log-detail").hidden = true;
  try {
    const b = await api(`/api/batteries/${id}`);
    $("#b-name").textContent = b.name;
    $("#b-meta").textContent =
      `创建于 ${fmtTime(b.created_at)}${b.notes ? " · 备注：" + b.notes : ""}`;
    const f = $("#settings-form");
    f.rated_capacity_ah.value = b.rated_capacity_ah;
    f.safe_voltage_min.value = b.safe_voltage_min;
    f.safe_voltage_max.value = b.safe_voltage_max;
    await Promise.all([loadAssessment(), loadLogs()]);
  } catch (err) { toast(err.message, true); }
}

$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    await api(`/api/batteries/${state.currentId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        rated_capacity_ah: parseFloat(f.rated_capacity_ah.value),
        safe_voltage_min: parseFloat(f.safe_voltage_min.value),
        safe_voltage_max: parseFloat(f.safe_voltage_max.value),
      }),
    });
    toast("设置已保存（对后续导入的日志生效）");
    loadBatteries(true);
  } catch (err) { toast(err.message, true); }
};

$("#btn-delete").onclick = async () => {
  if (!confirm("确定删除该电池及其全部日志与评估数据？")) return;
  try {
    await api(`/api/batteries/${state.currentId}`, { method: "DELETE" });
    state.currentId = null;
    await loadBatteries();
    toast("已删除");
  } catch (err) { toast(err.message, true); }
};

$("#btn-export").onclick = () => {
  window.open(`/api/batteries/${state.currentId}/export`, "_blank");
};

/* ---------------- 日志导入 ---------------- */

$("#btn-upload").onclick = async () => {
  const input = $("#csv-file");
  if (!input.files.length) { toast("请先选择 CSV 文件", true); return; }
  const fd = new FormData();
  fd.append("file", input.files[0]);
  $("#upload-result").textContent = "上传分析中…";
  try {
    const r = await api(`/api/batteries/${state.currentId}/logs`,
      { method: "POST", body: fd });
    const dis = r.cycles.filter((c) => c.kind === "discharge").length;
    const chg = r.cycles.length - dis;
    let msg =
      `导入成功：${r.log.row_count} 行样本，识别放电循环 ${dis} 个、充电循环 ${chg} 个` +
      (r.skipped_rows ? `，跳过无效行 ${r.skipped_rows} 行` : "") + "。";
    if (r.cells && r.cells.present) {
      if (r.cells.enabled) {
        msg += `识别单体电压列 ${r.cells.cell_count} 个，参与单体分析 ${r.cells.rows_with_cells} 行`;
        if (r.cells.rows_skipped) {
          const reasons = Object.entries(r.cells.skip_reasons)
            .map(([why, rec]) => `${why} ${rec.count} 行（如第 ${rec.rows.join("、")} 行）`)
            .join("；");
          msg += `；${r.cells.rows_skipped} 行跳过单体分析（${reasons}）`;
        }
        const imbalanced = r.cycles.filter((c) =>
          c.flags.some((f) => f.code === "CELL_IMBALANCE")).length;
        msg += imbalanced ? `；单体失衡告警循环 ${imbalanced} 个。` : "；未发现持续单体失衡。";
      } else {
        msg += `识别到单体列但未启用单体分析（${r.cells.disabled_reason}）。`;
      }
    }
    $("#upload-result").textContent = msg;
    input.value = "";
    await Promise.all([loadAssessment(), loadLogs(), loadBatteries(true)]);
  } catch (err) {
    $("#upload-result").textContent = "";
    toast(err.message, true);
  }
};

async function loadLogs() {
  const logs = await api(`/api/batteries/${state.currentId}/logs`);
  const tbody = $("#logs-table tbody");
  tbody.innerHTML = "";
  for (const l of logs) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${l.id}</td><td></td><td>${fmtTime(l.imported_at)}</td>
      <td>${fmtDur(l.duration_s)}</td><td>${l.row_count}</td>
      <td>${l.discharge_cycles} / ${l.charge_cycles}</td>
      <td><button class="danger" data-del="${l.id}">删除</button></td>`;
    tr.children[1].textContent = l.filename;
    tr.onclick = (e) => {
      if (e.target.dataset.del) return;
      selectLog(l.id, tr);
    };
    tr.querySelector("[data-del]").onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`删除日志 #${l.id}？`)) return;
      try {
        await api(`/api/logs/${l.id}`, { method: "DELETE" });
        if (state.currentLogId === l.id) {
          state.currentLogId = null;
          $("#log-detail").hidden = true;
        }
        await Promise.all([loadAssessment(), loadLogs(), loadBatteries(true)]);
        toast("日志已删除");
      } catch (err) { toast(err.message, true); }
    };
    tbody.appendChild(tr);
  }
  if (!logs.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">暂无日志，请上传 CSV。</td></tr>`;
  }
}

/* ---------------- 评估展示 ---------------- */

async function loadAssessment() {
  const a = await api(`/api/batteries/${state.currentId}/assessment`);
  const card = $("#assessment-card");
  if (!a.summary.total_cycles) { card.hidden = true; return; }
  card.hidden = false;
  const s = a.summary;
  const cards = [
    ["健康等级", s.health_label, s.health_grade],
    ["SOH（最佳容量/额定）", s.soh_percent !== null ? fmt(s.soh_percent, 1) + "%" : "—", ""],
    ["最佳放电容量", fmt(s.best_discharge_ah) + " Ah", ""],
    ["近期平均容量", fmt(s.recent_avg_ah) + " Ah", ""],
    ["较最佳值衰减", fmt(s.fade_percent, 1) + "%", ""],
    ["每循环衰减", fmt(s.fade_per_cycle_percent, 2) + "%", ""],
    ["平均内阻", s.avg_ir_mohm !== null ? fmt(s.avg_ir_mohm, 1) + " mΩ" : "—", ""],
    ["历史最高温度", s.max_temp_c !== null ? fmt(s.max_temp_c, 1) + " °C" : "—", ""],
    ["单体最大压差", s.cell_worst_diff_v !== null
      ? fmt(s.cell_worst_diff_v, 3) + " V"
      + (s.cell_imbalance_cycles ? `（${s.cell_imbalance_cycles} 段失衡）` : "")
      : "—", s.cell_imbalance_cycles ? "fair" : ""],
    ["放电 / 充电循环", `${s.discharge_cycles} / ${s.charge_cycles}`, ""],
  ];
  $("#summary-cards").innerHTML = cards.map(([k, v, cls]) =>
    `<div class="metric"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`
  ).join("");
  $("#summary-text").textContent = a.summary_text;

  const sevLabel = { critical: "严重", warning: "警告", info: "提示" };
  $("#flag-count").textContent = `共 ${a.flags.length} 条`;
  $("#flag-list").innerHTML = a.flags.length
    ? a.flags.map((f) => `<li class="${f.severity}">
        <span class="badge">${sevLabel[f.severity] || f.severity}</span>${f.message}
        <span class="evidence">依据：${f.evidence} · 日志 #${f.log_id} 循环 #${f.cycle_seq}${f.start_iso ? " · " + fmtTime(f.start_iso) : ""}</span>
      </li>`).join("")
    : `<li class="info"><span class="badge">正常</span>未发现异常标记。</li>`;

  drawTrend(a.trend, a.battery.rated_capacity_ah);
}

/* ---------------- 日志详情与图表 ---------------- */

async function selectLog(logId, rowEl) {
  state.currentLogId = logId;
  document.querySelectorAll("#logs-table tbody tr").forEach((tr) =>
    tr.classList.toggle("selected", tr === rowEl));
  try {
    const d = await api(`/api/logs/${logId}`);
    state.logDetail = d;
    $("#log-detail").hidden = false;
    $("#log-title").textContent =
      `日志 #${logId}：${d.log.filename}（${d.log.row_count} 行样本）`;
    renderCyclesTable(d.cycles);
    drawLogChart(d.samples, d.cycles);
    $("#log-detail").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (err) { toast(err.message, true); }
}

function renderCyclesTable(cycles) {
  const tbody = $("#cycles-table tbody");
  tbody.innerHTML = "";
  for (const c of cycles) {
    const tr = document.createElement("tr");
    const kindTag = c.kind === "discharge"
      ? `<span class="tag discharge">放电</span>`
      : `<span class="tag charge">充电</span>`;
    const stateTag = c.closed
      ? `<span class="tag closed">闭合</span>`
      : `<span class="tag open">未闭合</span>`;
    const flagTxt = c.flags.length
      ? ` ⚠ ${c.flags.map((f) => f.code).join(", ")}` : "";
    const cellTxt = c.cell_count
      ? `${c.cell_count} 节 · ${fmt(c.cell_max_diff_v, 3)}V` +
        (c.cell_sustain_s > 0 ? ` · ${fmt(c.cell_sustain_s, 0)}s` : "")
      : "—";
    tr.innerHTML = `<td>${c.seq}</td><td>${kindTag}</td>
      <td>${c.start_iso ? fmtTime(c.start_iso) : fmt(c.start_t, 0) + "s"}</td>
      <td>${fmtDur(c.duration_s)}</td><td>${fmt(c.capacity_ah, 3)}</td>
      <td>${fmt(c.energy_wh, 1)}</td><td>${fmt(c.avg_current_a, 2)}</td>
      <td>${fmt(c.min_voltage)} ~ ${fmt(c.max_voltage)}</td>
      <td>${fmt(c.max_temp_c, 1)}</td><td>${fmt(c.temp_rise_c, 1)}</td>
      <td>${c.ir_mohm !== null ? fmt(c.ir_mohm, 1) : "—"}</td>
      <td>${cellTxt}</td>
      <td>${stateTag}${flagTxt}</td>`;
    tbody.appendChild(tr);
  }
  if (!cycles.length) {
    tbody.innerHTML = `<tr><td colspan="13" class="muted">未识别到完整充放电循环。</td></tr>`;
  }
}

/* ---------------- Canvas 绘图 ---------------- */

function setupCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = Number(canvas.getAttribute("height"));
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  return [ctx, w, h];
}

function drawLogChart(samples, cycles) {
  const canvas = $("#log-chart");
  const [ctx, W, H] = setupCanvas(canvas);
  let cellPresent = false, cellMaxN = 0;
  for (const p of samples) {
    if (p[4]) { cellPresent = true; cellMaxN = Math.max(cellMaxN, p[4].length); }
  }
  const padL = 46, padR = cellPresent ? 44 : 10, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  if (!samples.length) return;

  const t0 = samples[0][0], t1 = samples[samples.length - 1][0];
  const span = Math.max(t1 - t0, 1e-6);
  const x = (t) => padL + ((t - t0) / span) * iw;

  const series = [
    { name: "电压 V", idx: 1, color: "#4da3ff" },
    { name: "电流 A", idx: 2, color: "#f0b429" },
    { name: "温度 °C", idx: 3, color: "#ef5350" },
  ];
  for (const s of series) {
    let mn = Infinity, mx = -Infinity;
    for (const p of samples) {
      const v = p[s.idx];
      if (v < mn) mn = v;
      if (v > mx) mx = v;
    }
    if (mx - mn < 1e-6) { mx += 0.5; mn -= 0.5; }
    s.min = mn; s.max = mx;
    s.y = (v) => padT + ih - ((v - mn) / (mx - mn)) * ih;
  }

  // 循环区间底色
  for (const c of cycles) {
    ctx.fillStyle = c.kind === "discharge"
      ? "rgba(77,163,255,0.08)" : "rgba(52,201,142,0.08)";
    ctx.fillRect(x(c.start_t), padT, Math.max(x(c.end_t) - x(c.start_t), 1), ih);
  }
  // 单体失衡持续超差区间高亮（红色竖带）
  let hasAlarmBand = false;
  for (const c of cycles) {
    for (const iv of (c.cell_intervals || [])) {
      hasAlarmBand = true;
      ctx.fillStyle = "rgba(239,83,80,0.18)";
      ctx.fillRect(x(iv.start_t), padT,
        Math.max(x(iv.end_t) - x(iv.start_t), 1), ih);
    }
  }
  // 网格与电压刻度
  ctx.strokeStyle = "#2a3550";
  ctx.fillStyle = "#8a97b5";
  ctx.font = "10px monospace";
  ctx.lineWidth = 1;
  const vS = series[0];
  for (let g = 0; g <= 4; g++) {
    const gy = padT + (ih * g) / 4;
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
    const vv = vS.max - ((vS.max - vS.min) * g) / 4;
    ctx.fillText(vv.toFixed(2) + "V", 4, gy + 3);
  }
  // 时间刻度
  for (let g = 0; g <= 6; g++) {
    const tt = t0 + (span * g) / 6;
    const mm = Math.floor(tt / 60), ss = Math.round(tt % 60);
    ctx.fillText(`${mm}:${String(ss).padStart(2, "0")}`, x(tt) - 12, H - 8);
  }
  // 曲线
  for (const s of series) {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    samples.forEach((p, k) => {
      const px = x(p[0]), py = s.y(p[s.idx]);
      k === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
    });
    ctx.stroke();
  }

  // 单体电压曲线（共用一套单体电压刻度；缺值行断开，不跨点连线）
  const CELL_COLORS = ["#b388ff", "#80cbc4", "#ffb74d", "#f06292",
    "#aed581", "#4dd0e1", "#ff8a65", "#9fa8da", "#c5e1a5", "#ffd54f",
    "#ce93d8", "#81d4fa"];
  const cellN = cellMaxN;
  let cellRange = null;
  if (cellN) {
    let mn = Infinity, mx = -Infinity;
    for (const p of samples) {
      if (!p[4]) continue;
      for (const cv of p[4]) {
        if (cv < mn) mn = cv;
        if (cv > mx) mx = cv;
      }
    }
    if (mx - mn < 1e-6) { mx += 0.5; mn -= 0.5; }
    const pad = (mx - mn) * 0.15;
    mn -= pad; mx += pad;
    const yCell = (cv) => padT + ih - ((cv - mn) / (mx - mn)) * ih;
    // 右轴：单体电压刻度
    ctx.fillStyle = "#b388ff";
    for (let g = 0; g <= 4; g++) {
      const gy = padT + (ih * g) / 4;
      const cv = mx - ((mx - mn) * g) / 4;
      ctx.fillText(cv.toFixed(2) + "V", W - padR + 4, gy + 3);
    }
    ctx.fillStyle = "#8a97b5";
    for (let k = 0; k < cellN; k++) {
      ctx.strokeStyle = CELL_COLORS[k % CELL_COLORS.length];
      ctx.lineWidth = 0.9;
      ctx.beginPath();
      let pen = false;
      for (const p of samples) {
        if (!p[4] || p[4].length !== cellN || p[4][k] === null) { pen = false; continue; }
        const px = x(p[0]), py = yCell(p[4][k]);
        pen ? ctx.lineTo(px, py) : ctx.moveTo(px, py);
        pen = true;
      }
      ctx.stroke();
    }
    cellRange = { min: mn, max: mx, n: cellN };
  }
  $("#chart-legend").innerHTML =
    series.map((s) =>
      `<span><span class="dot" style="background:${s.color}"></span>${s.name}
       （${s.min.toFixed(1)} ~ ${s.max.toFixed(1)}）</span>`).join("") +
    `<span>底色：<span class="dot" style="background:rgba(77,163,255,.4)"></span>放电循环
     <span class="dot" style="background:rgba(52,201,142,.4)"></span>充电循环</span>` +
    (hasAlarmBand
      ? `<span><span class="dot" style="background:rgba(239,83,80,.55)"></span>单体压差持续超差区间（&gt;0.10V 且 ≥5s）</span>`
      : "") +
    (cellRange
      ? `<span>单体电压 ${cellRange.n} 节（${cellRange.min.toFixed(2)} ~ ${cellRange.max.toFixed(2)}V，刻度见右轴）：` +
        Array.from({ length: cellRange.n }, (_, k) =>
          `<span class="dot" style="background:${CELL_COLORS[k % CELL_COLORS.length]}"></span>#${k + 1}`).join(" ") +
        "</span>"
      : "");
}

function drawTrend(trend, ratedAh) {
  const canvas = $("#trend-chart");
  const [ctx, W, H] = setupCanvas(canvas);
  const padL = 46, padR = 10, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  ctx.clearRect(0, 0, W, H);
  if (!trend.length) return;

  const maxV = Math.max(...trend.map((p) => p.capacity_ah), ratedAh) * 1.15;
  const y = (v) => padT + ih - (v / maxV) * ih;
  const bw = Math.min(40, (iw / trend.length) * 0.6);

  ctx.strokeStyle = "#2a3550";
  ctx.fillStyle = "#8a97b5";
  ctx.font = "10px monospace";
  for (let g = 0; g <= 4; g++) {
    const gy = padT + (ih * g) / 4;
    ctx.beginPath(); ctx.moveTo(padL, gy); ctx.lineTo(W - padR, gy); ctx.stroke();
    ctx.fillText(((maxV * (4 - g)) / 4).toFixed(2), 4, gy + 3);
  }
  trend.forEach((p, k) => {
    const cx = padL + ((k + 0.5) / trend.length) * iw;
    ctx.fillStyle = "#4da3ff";
    ctx.fillRect(cx - bw / 2, y(p.capacity_ah), bw, ih - (y(p.capacity_ah) - padT));
    ctx.fillStyle = "#8a97b5";
    ctx.fillText(`#${p.n}`, cx - 8, H - 8);
    ctx.fillText(p.capacity_ah.toFixed(2), cx - 14, y(p.capacity_ah) - 4);
  });
  // 额定容量参考线
  ctx.strokeStyle = "#f0b429";
  ctx.setLineDash([5, 4]);
  ctx.beginPath(); ctx.moveTo(padL, y(ratedAh)); ctx.lineTo(W - padR, y(ratedAh)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#f0b429";
  ctx.fillText(`额定 ${ratedAh}Ah`, W - padR - 76, y(ratedAh) - 5);
}

/* ---------------- 启动 ---------------- */

window.addEventListener("resize", () => {
  if (state.logDetail && !$("#log-detail").hidden) {
    drawLogChart(state.logDetail.samples, state.logDetail.cycles);
  }
});
loadBatteries();
