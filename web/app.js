/* 实验分析平台前端：原生 JS + 内联 SVG，无外部依赖。 */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const app = () => $("#app");

async function api(path, opts = {}) {
  if (opts.body) {
    opts.method = opts.method || "POST";
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(opts.body);
  }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.detail = data.detail;
    throw err;
  }
  return data;
}

// ---------------------------------------------------------------- 格式化
const fmtDay = (t) => new Date(t * 1000).toISOString().slice(5, 10);
const fmtTime = (t) => new Date(t * 1000).toLocaleString("zh-CN", { hour12: false });
const fmtPP = (x, d = 2) => (x == null ? "—" : (x * 100).toFixed(d) + "pp");
const fmtPct = (x, d = 2) => (x == null ? "—" : (x * 100).toFixed(d) + "%");
const fmtP = (p) => (p == null ? "—" : p < 1e-4 ? p.toExponential(1) : p.toFixed(4));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const REC_LABEL = {
  stop_efficacy: ["提前停（有效）", "ok"],
  stop_futility: ["终止（未检出效应）", "neutral"],
  continue: ["继续观察", "info"],
  rollback: ["止损回滚", "bad"],
  ship: ["全量发布", "ok"],
};
const DEC_LABEL = {
  stop_efficacy: "提前停（有效）", stop_futility: "终止（未检出效应）",
  continue: "继续观察", rollback: "止损回滚", ship: "全量发布",
};

// ---------------------------------------------------------------- SVG 图表
const NS = "http://www.w3.org/2000/svg";
function s(tag, attrs = {}, parent) {
  const el = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (parent) parent.appendChild(el);
  return el;
}

/* 通用折线图：series=[{name,color,ys,dash}], xs 为时间戳秒；
   hlines=[{y,color,dash,label}], vlines=[{x,color,label}], band={lo,hi,color} */
function lineChart(el, { xs, series, hlines = [], vlines = [], band = null,
                         yFmt = (v) => v, logY = false, height = 240 }) {
  const W = 560, H = height, PL = 52, PR = 14, PT = 12, PB = 30;
  el.innerHTML = "";
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart" }, el);
  const allY = series.flatMap((sr) => sr.ys.filter((v) => v != null))
    .concat(hlines.map((h) => h.y));
  if (band) allY.push(...band.lo, ...band.hi);
  if (!allY.length) { el.textContent = "暂无数据"; return; }
  let yMin, yMax;
  if (logY) {
    const pos = allY.filter((v) => v > 0);
    yMin = Math.min(...pos, 1e-4); yMax = 1;
  } else {
    yMin = Math.min(...allY); yMax = Math.max(...allY);
    const pad = (yMax - yMin || Math.abs(yMax) || 1) * 0.12;
    yMin -= pad; yMax += pad;
  }
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const X = (t) => PL + (W - PL - PR) * ((t - xMin) / Math.max(xMax - xMin, 1));
  const Y = logY
    ? (v) => PT + (H - PT - PB) *
        (1 - (Math.log10(Math.max(v, 1e-6)) - Math.log10(yMin)) /
             (Math.log10(yMax) - Math.log10(yMin)))
    : (v) => PT + (H - PT - PB) * (1 - (v - yMin) / (yMax - yMin));

  // 网格与 y 轴刻度
  const ticks = [];
  if (logY) {
    for (let e = Math.ceil(Math.log10(yMin)); e <= 0; e++) ticks.push(Math.pow(10, e));
  } else {
    const span = yMax - yMin, step0 = Math.pow(10, Math.floor(Math.log10(span / 4)));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * step0)
      .find((st) => span / st <= 5) || step0 * 10;
    for (let v = Math.ceil(yMin / step) * step; v <= yMax; v += step)
      ticks.push(Math.abs(v) < step / 1e6 ? 0 : v);
  }
  for (const tv of ticks) {
    s("line", { x1: PL, x2: W - PR, y1: Y(tv), y2: Y(tv),
                stroke: tv === 0 ? "#9ca3af" : "#eee", "stroke-width": 1 }, svg);
    const t = s("text", { x: PL - 6, y: Y(tv) + 4, "text-anchor": "end",
                          "font-size": 10, fill: "#6b7280" }, svg);
    t.textContent = yFmt(tv);
  }
  // x 轴刻度（日期）
  const nT = Math.min(7, xs.length);
  for (let i = 0; i < nT; i++) {
    const xv = xs[Math.round(i * (xs.length - 1) / Math.max(nT - 1, 1))];
    const t = s("text", { x: X(xv), y: H - 8, "text-anchor": "middle",
                          "font-size": 10, fill: "#6b7280" }, svg);
    t.textContent = fmtDay(xv);
  }
  // 参考横线
  for (const h of hlines) {
    s("line", { x1: PL, x2: W - PR, y1: Y(h.y), y2: Y(h.y),
                stroke: h.color, "stroke-width": 1.2,
                "stroke-dasharray": h.dash || "none" }, svg);
    if (h.label) {
      const t = s("text", { x: W - PR - 2, y: Y(h.y) - 3, "text-anchor": "end",
                            "font-size": 9.5, fill: h.color }, svg);
      t.textContent = h.label;
    }
  }
  // 置信序列带
  if (band) {
    const pts = xs.map((x, i) => [x, band.lo[i], band.hi[i]])
      .filter(([, lo, hi]) => lo != null && hi != null);
    if (pts.length > 1) {
      const d = pts.map(([x, , hi], i) =>
        `${i ? "L" : "M"}${X(x)},${Y(hi)}`).join(" ") +
        pts.slice().reverse().map(([x, lo]) => `L${X(x)},${Y(lo)}`).join(" ") + "Z";
      s("path", { d, fill: band.color, stroke: "none" }, svg);
    }
  }
  // 折线
  for (const sr of series) {
    const d = xs.map((x, i) => [x, sr.ys[i]]).filter(([, y]) => y != null)
      .map(([x, y], i) => `${i ? "L" : "M"}${X(x)},${Y(y)}`).join(" ");
    if (d) s("path", { d, fill: "none", stroke: sr.color, "stroke-width": 1.8,
                       "stroke-dasharray": sr.dash || "none" }, svg);
  }
  // 垂直标记（分流变更等）
  for (const v of vlines) {
    if (v.x < xMin || v.x > xMax) continue;
    s("line", { x1: X(v.x), x2: X(v.x), y1: PT, y2: H - PB,
                stroke: v.color || "#d97706", "stroke-width": 1,
                "stroke-dasharray": "4 3" }, svg);
    const t = s("text", { x: X(v.x) + 3, y: PT + 10, "font-size": 9.5,
                          fill: v.color || "#d97706" }, svg);
    t.textContent = v.label || "";
  }
}

// ---------------------------------------------------------------- 列表视图
async function viewList() {
  const { experiments } = await api("/api/experiments");
  const rows = experiments.map((e) => {
    const r = e.latest_run;
    const dec = e.confirmed_decision;
    return `<tr>
      <td><a href="#/exp/${esc(e.id)}"><b>${esc(e.name)}</b></a><br>
        <span class="muted small mono">${esc(e.id)}</span></td>
      <td><span class="badge ${e.status}">${e.status}</span></td>
      <td class="num">${r ? fmtPP(r.current.theta) : "—"}</td>
      <td class="num">${r ? fmtP(r.msprt.p_always_valid) : "—"}</td>
      <td>${r ? `<span class="badge ${REC_LABEL[r.recommendation][1]}">
          ${REC_LABEL[r.recommendation][0]}</span>` : "—"}</td>
      <td>${dec ? `<span class="badge ok">${DEC_LABEL[dec.decision]}</span>
          <span class="muted small">#${dec.decision_id}</span>` : "<span class=muted>—</span>"}</td>
    </tr>`;
  }).join("");
  app().innerHTML = `<div class="card">
    <h2>实验列表</h2>
    <table><thead><tr>
      <th>实验</th><th>状态</th><th class="num">效应 θ̂</th>
      <th class="num">always-valid p</th><th>当前建议</th><th>已确认决策</th>
    </tr></thead><tbody>${rows || "<tr><td colspan=6 class=muted>暂无实验</td></tr>"}
    </tbody></table></div>
    ${methodExplainer()}`;
}

// ---------------------------------------------------------------- 详情视图
async function viewDetail(expId, notice = "") {
  const [exp, runsResp, decsResp, metricsResp] = await Promise.all([
    api(`/api/experiments/${expId}`),
    api(`/api/experiments/${expId}/analyses`),
    api(`/api/experiments/${expId}/decisions`),
    api("/api/metrics"),
  ]);
  const runs = runsResp.runs;
  const latest = runs[0] || null;
  const full = latest ? await api(`/api/analyses/${latest.run_id}`) : null;
  const metricId = latest ? latest.metric_id : "purchase";
  const series = await api(`/api/experiments/${expId}/series?metric=${metricId}`);
  const metricVersions = metricsResp.metrics.filter((m) => m.metric_id === metricId);
  const cur = latest ? latest.current : {};

  const el = app();
  el.innerHTML = `
    <div class="card">
      <h2>${esc(exp.name)}
        <span class="badge ${exp.status}">${exp.status}</span></h2>
      <div class="chips">
        <span class="chip">α=<b>${exp.alpha}</b></span>
        <span class="chip">MDE=<b>${fmtPP(exp.mde_abs, 1)}</b></span>
        <span class="chip">τ=<b>${exp.tau}</b></span>
        <span class="chip">组序贯=<b>${exp.spending.toUpperCase()}</b></span>
        <span class="chip">计划=<b>${exp.planned_days}天 × ${exp.planned_daily_n}/组</b></span>
        <span class="chip">数据水位 seq=<b>${exp.watermark}</b></span>
        <span class="chip">指标 <b>${esc(metricId)}</b> 当前版本
          <b>v${metricVersions.length ? metricVersions[metricVersions.length - 1].version : "?"}</b></span>
      </div>
      <div class="muted small">观察窗：${fmtTime(exp.start_time)} 起，不随分流变更重置；
        分流 epoch：${exp.epochs.length
          ? exp.epochs.map((ep) => `${fmtDay(ep.event_time)} 改为
            ${Object.entries(ep.weights).map(([k, v]) => `${k}:${(v * 100).toFixed(0)}%`)
              .join("/")}`).join("；")
          : "（未变更，始终 50/50 或初始权重）"}</div>
      ${notice}
      <div class="actions">
        <button class="btn primary" id="btn-run">立即分析</button>
        <button class="btn" id="btn-decide">记录决策</button>
        <button class="btn" id="btn-alloc">变更分流</button>
        <button class="btn" id="btn-metric">指标换版</button>
      </div>
      <div id="action-slot"></div>
      <div id="kpis"></div>
    </div>
    <div class="grid2">
      <div class="card"><div class="chart-title">累计转化率（按当前水位重算，含迟到/撤销）</div>
        <div class="chart-note">垂直虚线 = 分流变更；迟到与撤销事件已计入</div>
        <div id="ch-rate"></div><div class="legend" id="lg-rate"></div></div>
      <div class="card"><div class="chart-title">效应 θ̂ 与置信序列（漏斗）</div>
        <div class="chart-note">蓝色带 = 95% 置信序列；穿出 0 线即 always-valid 显著</div>
        <div id="ch-funnel"></div><div class="legend" id="lg-funnel"></div></div>
    </div>
    <div class="grid2">
      <div class="card"><div class="chart-title">always-valid p 值链（每次分析一次检查）</div>
        <div class="chart-note">对数轴；触及 α=${exp.alpha} 线即可停。指标换版会重启链</div>
        <div id="ch-p"></div></div>
      <div class="card"><div class="chart-title">迟到与撤销</div><div id="late-panel"></div></div>
    </div>
    <div class="card"><h2>分析运行记录（每次结论绑定数据水位 / 指标版本 / 方法版本）</h2>
      <div id="runs-table"></div></div>
    <div class="card"><h2>决策日志</h2><div id="decisions"></div></div>
    ${methodExplainer()}`;

  $("#btn-run").onclick = () => runAnalysis(expId, metricId);
  $("#btn-decide").onclick = () => decideForm(expId, metricId, runs);
  $("#btn-alloc").onclick = () => allocForm(expId);
  $("#btn-metric").onclick = () => metricForm(expId, metricId, metricVersions);

  renderKpis(exp, latest);
  if (full) renderCharts(exp, full, series, runs);
  renderLate(latest);
  renderRuns(runs);
  renderDecisions(decsResp);
}

function renderKpis(exp, run) {
  if (!run) { $("#kpis").innerHTML = "<p class=muted>尚未分析，点击「立即分析」。</p>"; return; }
  const c = run.current, m = run.msprt, g = run.gsd;
  const rec = REC_LABEL[run.recommendation];
  $("#kpis").innerHTML = `<div class="kpis">
    <div class="kpi"><div class="k">效应 θ̂（处理-对照）</div>
      <div class="v ${c.theta > 0 ? "pos" : c.theta < 0 ? "neg" : ""}">${fmtPP(c.theta)}</div>
      <div class="s">相对提升 ${c.p_c ? fmtPct(c.theta / c.p_c, 1) : "—"}</div></div>
    <div class="kpi"><div class="k">95% 置信序列</div>
      <div class="v small mono" style="font-size:13.5px">[${fmtPP(c.cs_lo)}, ${fmtPP(c.cs_hi)}]</div>
      <div class="s">任意时刻同时有效</div></div>
    <div class="kpi"><div class="k">always-valid p</div>
      <div class="v ${m.p_always_valid <= exp.alpha ? "pos" : ""}">${fmtP(m.p_always_valid)}</div>
      <div class="s">${m.chain_checks} 次检查 · α=${exp.alpha}</div></div>
    <div class="kpi"><div class="k">样本（对照/处理）</div>
      <div class="v" style="font-size:15px">${c.n_c} / ${c.n_t}</div>
      <div class="s">转化 ${c.x_c} / ${c.x_t}</div></div>
    <div class="kpi"><div class="k">信息量 t（组序贯）</div>
      <div class="v">${(g.t * 100).toFixed(0)}%</div>
      <div class="s">本次边界 u=${g.u_k ? g.u_k.toFixed(2) : "—"}（${g.spending}）</div></div>
    <div class="kpi"><div class="k">当前建议</div>
      <div class="v" style="font-size:15px"><span class="badge ${rec[1]}">${rec[0]}</span></div>
      <div class="s">run #${run.run_id} · seq=${run.as_of_seq}</div></div>
  </div>
  <p class="muted small">${esc(run.recommendation_reason)}</p>`;
}

function renderCharts(exp, full, seriesResp, runs) {
  const epochs = (seriesResp.epochs || []).map((e) => ({
    x: e.event_time, label: "分流变更", color: "#d97706" }));
  // 1) 转化率
  const days = seriesResp.days.filter((d) => (d.n.control || 0) > 0);
  if (days.length) {
    const xs = days.map((d) => d.time);
    const rate = (v) => days.map((d) =>
      d.n[v] ? d.x[v] / d.n[v] : null);
    lineChart($("#ch-rate"), { xs, vlines: epochs, yFmt: (v) => (v * 100).toFixed(1) + "%",
      series: [
        { name: "对照", color: "#6b7280", ys: rate("control") },
        { name: "处理", color: "#2563eb", ys: rate("treatment") },
      ] });
    $("#lg-rate").innerHTML =
      `<span><span class="sw" style="background:#6b7280"></span>对照</span>
       <span><span class="sw" style="background:#2563eb"></span>处理</span>`;
  }
  // 2) 漏斗：θ̂ 与置信序列（重放轨迹）
  const traj = full.result.trajectory.filter((t) => t.theta != null);
  if (traj.length) {
    const xs = traj.map((t) => t.time);
    lineChart($("#ch-funnel"), {
      xs, vlines: epochs, yFmt: (v) => (v * 100).toFixed(1),
      hlines: [{ y: 0, color: "#9ca3af" },
               { y: exp.mde_abs, color: "#d97706", dash: "3 3", label: "+MDE" },
               { y: -exp.mde_abs, color: "#d97706", dash: "3 3", label: "-MDE" }],
      band: { lo: traj.map((t) => t.cs_lo), hi: traj.map((t) => t.cs_hi),
              color: "rgba(37,99,235,.13)" },
      series: [{ name: "θ̂", color: "#2563eb", ys: traj.map((t) => t.theta) }],
    });
    $("#lg-funnel").innerHTML =
      `<span><span class="sw" style="background:#2563eb"></span>θ̂（重放轨迹，含迟到数据）</span>
       <span><span class="sw" style="background:rgba(37,99,235,.25)"></span>95% 置信序列</span>`;
  }
  // 3) always-valid p 链（按 run 顺序，当前指标版本）
  const latestVer = runs.length ? runs[0].metric_version : 1;
  const chain = runs.filter((r) => r.metric_version === latestVer)
    .slice().reverse();
  if (chain.length) {
    const xs = chain.map((r) => r.as_of_time);
    lineChart($("#ch-p"), { xs, logY: true, vlines: epochs,
      yFmt: (v) => v.toExponential(0),
      hlines: [{ y: exp.alpha, color: "#b91c1c", dash: "4 3", label: `α=${exp.alpha}` }],
      series: [{ name: "p_av", color: "#2563eb",
                 ys: chain.map((r) => Math.max(r.msprt.p_always_valid, 1e-6)) }] });
  }
}

function renderLate(run) {
  if (!run) { $("#late-panel").innerHTML = "<p class=muted>—</p>"; return; }
  const l = run.late_data, snc = run.since_previous_run || {};
  const byDay = Object.entries(l.late_by_event_day || {})
    .map(([d, n]) => `<tr><td class=mono>${d}</td><td class=num>${n}</td></tr>`).join("");
  $("#late-panel").innerHTML = `
    <p>水位内迟到事件（到达晚于发生 >1h）：<b>${l.late_events}</b>；
       被撤销事件：<b>${l.revoked_events}</b>（tombstone，重算时剔除）</p>
    <p class="muted">自上一次 run 以来新增：曝光 ${snc.exposure || 0} ·
       转化 ${snc.conversion || 0}（其中迟到 ${snc.late_arrivals || 0}）·
       撤销 ${snc.revoke || 0} · 分流变更 ${snc.allocation_change || 0}</p>
    ${byDay ? `<h3>迟到事件按发生日分布</h3>
      <table><thead><tr><th>发生日</th><th class=num>迟到件数</th></tr></thead>
      <tbody>${byDay}</tbody></table>` : ""}`;
}

function renderRuns(runs) {
  const rows = runs.map((r) => {
    const c = r.current, rec = REC_LABEL[r.recommendation];
    return `<tr>
      <td class=num>#${r.run_id}</td>
      <td class=mono>${fmtTime(r.created_at)}</td>
      <td class=num>${r.as_of_seq}</td>
      <td class=mono>${esc(r.metric_id)}@v${r.metric_version}</td>
      <td class="mono small">${esc(r.method_version)}</td>
      <td class=num>${fmtPP(c.theta)}</td>
      <td class=num>${fmtP(r.msprt.p_always_valid)}</td>
      <td class=num>${(r.gsd.t * 100).toFixed(0)}% / ${r.gsd.u_k ? r.gsd.u_k.toFixed(2) : "—"}</td>
      <td><span class="badge ${rec[1]}">${rec[0]}</span></td></tr>`;
  }).join("");
  $("#runs-table").innerHTML = `<table><thead><tr>
    <th class=num>run</th><th>时间</th><th class=num>水位 seq</th><th>指标@版本</th>
    <th>方法版本</th><th class=num>θ̂</th><th class=num>p_av</th>
    <th class=num>信息量/边界</th><th>建议</th></tr></thead>
    <tbody>${rows || "<tr><td colspan=9 class=muted>暂无</td></tr>"}</tbody></table>`;
}

function renderDecisions({ decisions, rejected_attempts }) {
  const items = [];
  for (const d of decisions) {
    items.push(`<li class="${d.status}">
      <span class="badge ${d.status === "confirmed" ? "ok" : "neutral"}">
        ${d.status === "confirmed" ? "已确认" : "已被取代"}</span>
      <b>${DEC_LABEL[d.decision]}</b>
      <span class="muted small">#${d.decision_id} · ${fmtTime(d.created_at)} ·
        ${esc(d.decided_by || "—")}</span><br>
      <span class="small">绑定 run #${d.run_id}（水位 seq=${d.run_as_of_seq} ·
        指标 v${d.metric_version} · ${esc(d.method_version)}）</span><br>
      <span class="muted small">${esc(d.rationale)}</span></li>`);
  }
  for (const a of rejected_attempts) {
    items.push(`<li class="rejected">
      <span class="badge bad">已拒绝</span> <b>${DEC_LABEL[a.decision] || a.decision}</b>
      <span class="muted small">${fmtTime(a.created_at)}</span><br>
      <span class="small">run #${a.run_id} · 原因 ${esc(a.reason)}</span><br>
      <span class="muted small">${esc(a.detail)}</span></li>`);
  }
  $("#decisions").innerHTML = items.length
    ? `<ul class="timeline">${items.join("")}</ul>`
    : "<p class=muted>暂无决策记录。</p>";
}

// ---------------------------------------------------------------- 操作表单
function slot(html) { $("#action-slot").innerHTML = html; }

async function runAnalysis(expId, metricId) {
  try {
    const run = await api(`/api/experiments/${expId}/analyses`,
                          { body: { metric_id: metricId } });
    viewDetail(expId, `<div class="ok-box">分析完成：run #${run.run_id}，
      水位 seq=${run.as_of_seq}，指标 v${run.metric_version}，方法
      ${esc(run.method_version)}。建议：${REC_LABEL[run.recommendation][0]}。</div>`);
  } catch (e) { slot(`<div class="error-box">${esc(e.message)}</div>`); }
}

function decideForm(expId, metricId, runs) {
  const opts = runs.slice(0, 12).map((r) =>
    `<option value="${r.run_id}">run #${r.run_id} · seq=${r.as_of_seq} ·
     θ̂=${fmtPP(r.current.theta)} · p_av=${fmtP(r.msprt.p_always_valid)} ·
     ${REC_LABEL[r.recommendation][0]}</option>`).join("");
  slot(`<form class="inline" id="f-dec">
    <label>依据的分析 run<select name="run_id">${opts}</select></label>
    <label>决策<select name="decision">
      ${Object.entries(DEC_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}
    </select></label>
    <label>理由<input name="rationale" size="34" placeholder="决策依据（必填）" required></label>
    <label>决策人<input name="decided_by" size="10" placeholder="工号/邮箱"></label>
    <button class="btn primary">确认决策</button>
    <span class="muted small">旧水位线的 run 确认会被拒绝（409）并留痕</span>
  </form>`);
  $("#f-dec").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    try {
      await api(`/api/experiments/${expId}/decisions`, { body: {
        metric_id: metricId, run_id: Number(f.get("run_id")),
        decision: f.get("decision"), rationale: f.get("rationale"),
        decided_by: f.get("decided_by") } });
      viewDetail(expId, `<div class="ok-box">决策已确认并写入日志。</div>`);
    } catch (e) {
      slot(`<div class="error-box">确认被拒绝（${e.status || ""}）：${esc(e.message)}</div>`);
    }
  };
}

function allocForm(expId) {
  slot(`<form class="inline" id="f-al">
    <label>对照权重<input name="control" type="number" step="0.05" min="0" max="1" value="0.5"></label>
    <label>处理权重<input name="treatment" type="number" step="0.05" min="0" max="1" value="0.5"></label>
    <label>备注<input name="note" size="30" placeholder="变更原因"></label>
    <button class="btn primary">记录分流变更</button>
    <span class="muted small">只记录 epoch；观察窗不会重置，历史数据继续参与分析</span>
  </form>`);
  $("#f-al").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    try {
      await api(`/api/experiments/${expId}/allocations`, { body: {
        weights: { control: Number(f.get("control")),
                   treatment: Number(f.get("treatment")) },
        note: f.get("note") } });
      viewDetail(expId, `<div class="ok-box">分流变更已记录为新 epoch（观察窗不变）。</div>`);
    } catch (e) { slot(`<div class="error-box">${esc(e.message)}</div>`); }
  };
}

function metricForm(expId, metricId, versions) {
  const latest = versions.length ? versions[versions.length - 1] : null;
  slot(`<form class="inline" id="f-m">
    <label>新定义（JSON）<textarea name="definition">${esc(
      JSON.stringify(latest ? latest.definition : {}, null, 2))}</textarea></label>
    <label>换版说明<input name="note" size="30" placeholder="例如：剔除小额测试单"></label>
    <button class="btn primary">发布 ${esc(metricId)} 新版本</button>
    <span class="muted small">换版后 always-valid 链按新版本重启；旧 run 仍绑定旧版本</span>
  </form>`);
  $("#f-m").onsubmit = async (ev) => {
    ev.preventDefault();
    const f = new FormData(ev.target);
    let def;
    try { def = JSON.parse(f.get("definition")); }
    catch { slot(`<div class="error-box">JSON 解析失败</div>`); return; }
    try {
      const r = await api(`/api/metrics/${metricId}/versions`,
                          { body: { definition: def, note: f.get("note") } });
      viewDetail(expId, `<div class="ok-box">指标 ${esc(metricId)} 已发布 v${r.version}。</div>`);
    } catch (e) { slot(`<div class="error-box">${esc(e.message)}</div>`); }
  };
}

// ---------------------------------------------------------------- 方法说明
function methodExplainer() {
  return `<div class="card"><details class="method">
    <summary>统计方法说明：为什么不能每天用固定样本检验？敏感性与决策延迟如何权衡？</summary>
    <h4>问题：反复查看会放大误报</h4>
    <p>固定样本量的 z 检验只在「预先定好样本量、只看一次」时保证 α=5% 的误报率。
       每天看一次、显著就停，14 天内误报率会膨胀到约 20%~30%（本平台测试里有蒙特卡洛验证）。
       因此本平台不用裸的固定样本 p 值做每日决策。</p>
    <h4>主方法：mSPRT（always-valid）</h4>
    <p>混合序贯概率比检验：H₁ 取效应 θ ~ N(0, τ²) 的混合先验，似然比 Λ 在零假设下是
       非负鞅，<code>P(任意时刻 1/Λ ≤ α) ≤ α</code>。所以每天看、想停就停，总体误报率仍 ≤ α。
       配套的<b>置信序列</b>在任意时刻同时覆盖真效应（概率 ≥ 1-α），图上表现为漏斗形边界。
       链按「实验 × 指标 × 指标版本」维护：每次分析是一次检查；指标换版 = 新假设，链重启。</p>
    <h4>备选：组序贯（Lan-DeMets α 消耗）</h4>
    <p>若合规要求「预定检查计划」，用 O'Brien-Fleming / Pocock 消耗函数：
       每次检查只花掉信息比例对应的一小部分 α。OBF 早期边界很高（很难早停），
       把 α 留到后期；Pocock 各次边界接近水平，早停更容易但终点检验更保守。</p>
    <h4>敏感性 vs 决策延迟</h4>
    <p>τ 取 MDE 量级时对 MDE 大小的效应最敏感：真实效应 ≥ MDE 时通常信息过半即可停；
       真实效应远小于 MDE 时会观察到计划终点。τ 调小 → 对小效应更敏感但边界更宽、
       大效应时停得更晚；OBF → 早期不敏感、终点敏感性高；Pocock → 反之。
       提前停的代价是「效应估计向上偏」（赢家魔咒），置信序列给出的区间比点估计更诚实。</p>
    <h4>数据语义</h4>
    <p>每次 run 绑定数据水位 seq、指标版本、方法版本；迟到与撤销事件随水位推进自动参与重算；
       分流变更只记 epoch、不重置观察窗；已确认决策只能被更新水位的新决策取代，
       迟到的旧任务确认会被拒绝并留痕。结论措辞一律保守：「未检出 ≥MDE 的效应」不等于「无效应」。</p>
  </details></div>`;
}

// ---------------------------------------------------------------- 路由
function route() {
  const h = location.hash || "#/";
  const m = h.match(/^#\/exp\/([\w-]+)/);
  if (m) viewDetail(m[1]).catch((e) => { app().innerHTML =
    `<div class="error-box">${esc(e.message)}</div>`; });
  else viewList().catch((e) => { app().innerHTML =
    `<div class="error-box">${esc(e.message)}</div>`; });
}
window.addEventListener("hashchange", route);
route();
