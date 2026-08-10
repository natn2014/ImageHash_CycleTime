/* Dashboard: run chart + histogram, live over SSE.
 *
 * Data flow: one /api/stats call renders everything; an SSE message on each
 * detected cycle triggers a refetch. Refetching (rather than appending client
 * side) keeps the control limits, histogram bins and the plotted points derived
 * from exactly the same rows — at 5-30 s cycles the cost is irrelevant.
 */
'use strict';

const CSS = getComputedStyle(document.documentElement);
const C = (name) => CSS.getPropertyValue(name).trim();

const COLOR = {
  series: C('--series-1'),
  critical: C('--critical'),
  warning: C('--warning'),
  good: C('--good'),
  text: C('--text-primary'),
  secondary: C('--text-secondary'),
  muted: C('--text-muted'),
  grid: C('--gridline'),
  base: C('--baseline'),
  surface: C('--surface-1'),
};

const FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif';
const LIMIT = 100;

Chart.defaults.font.family = FONT;
Chart.defaults.font.size = 10;
Chart.defaults.color = COLOR.muted;
Chart.defaults.animation.duration = 200;
Chart.defaults.maintainAspectRatio = false;

/* --------------------------------------------------------------- helpers */

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => (v === null || v === undefined ? '–' : Number(v).toFixed(d));

function localTime(iso) {
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? ''
    : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

/* ------------------------------------------------------- limit-line plugin
 * Draws the mean, the I-MR control limits and the target as reference rules
 * with direct labels at the right edge. Direct labels rather than a legend:
 * on a 7" panel a legend box would cost more space than the plot it explains.
 * Dashing is meaningful here — these ARE thresholds, unlike the gridlines,
 * which stay solid hairlines.
 */
const limitLines = {
  id: 'limitLines',
  afterDatasetsDraw(chart, _args, opts) {
    const lines = opts && opts.lines;
    if (!lines || !lines.length) return;
    const { ctx, chartArea: area, scales } = chart;
    const y = scales.y;
    if (!y) return;

    ctx.save();
    ctx.font = `600 9px ${FONT}`;
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'right';

    // Draw every rule first, then the labels. The target often sits within a
    // second of the mean, so their labels have to be de-collided as a group -
    // labelling each line as it is drawn would let one overwrite the other.
    const drawn = [];
    for (const line of lines) {
      if (line.value === null || line.value === undefined) continue;
      const py = y.getPixelForValue(line.value);
      if (py < area.top - 1 || py > area.bottom + 1) continue;  // off-scale

      ctx.beginPath();
      ctx.setLineDash(line.dash || []);
      ctx.strokeStyle = line.color;
      ctx.lineWidth = 1;
      ctx.globalAlpha = line.alpha === undefined ? 0.75 : line.alpha;
      ctx.moveTo(area.left, py);
      ctx.lineTo(area.right, py);
      ctx.stroke();
      drawn.push({ line, py });
    }

    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    const LABEL_H = 11;
    drawn.sort((a, b) => a.py - b.py);
    let lowest = -Infinity;   // bottom edge of the last label placed
    for (const { line, py } of drawn) {
      // Push down just enough to clear the previous label, then keep it inside
      // the plot so nothing is clipped by the panel edge.
      let ly = Math.max(py, lowest + LABEL_H);
      ly = Math.min(ly, area.bottom - 6);
      ly = Math.max(ly, area.top + 6);
      lowest = ly;

      const label = `${line.label} ${fmt(line.value)}`;
      const w = ctx.measureText(label).width;
      // Sit the label on the surface so the rule doesn't strike through it.
      ctx.fillStyle = COLOR.surface;
      ctx.fillRect(area.right - w - 6, ly - 5.5, w + 6, LABEL_H);
      ctx.fillStyle = line.color;
      ctx.fillText(label, area.right - 3, ly);
    }
    ctx.restore();
  },
};

Chart.register(limitLines);

/* ----------------------------------------------------------------- charts */

const tooltipStyle = {
  backgroundColor: COLOR.surface,
  borderColor: 'rgba(255,255,255,0.15)',
  borderWidth: 1,
  titleColor: COLOR.text,
  bodyColor: COLOR.secondary,
  titleFont: { size: 11, weight: '600' },
  bodyFont: { size: 11 },
  padding: 8,
  displayColors: false,
};

const runChart = new Chart($('runChart'), {
  type: 'line',
  data: {
    labels: [],
    datasets: [{
      label: 'Cycle time',
      data: [],
      borderColor: COLOR.series,
      borderWidth: 2,
      tension: 0,
      fill: false,
      pointRadius: 3,
      pointHoverRadius: 6,
      pointBackgroundColor: [],
      pointBorderColor: [],
      pointBorderWidth: 0,
    }],
  },
  options: {
    // Right padding leaves room for the limit labels drawn at the plot edge.
    layout: { padding: { top: 6, right: 8 } },
    // A crosshair-style hover: land anywhere on the column, not on the dot.
    interaction: { mode: 'index', intersect: false },
    scales: {
      x: {
        grid: { display: false },
        border: { color: COLOR.base },
        ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 7, font: { size: 9 } },
      },
      y: {
        grid: { color: COLOR.grid, drawTicks: false },
        border: { display: false },
        ticks: { padding: 4, font: { size: 9 }, callback: (v) => `${v}s` },
      },
    },
    plugins: {
      legend: { display: false },   // single series — the panel title names it
      tooltip: {
        ...tooltipStyle,
        callbacks: {
          title: (items) => items[0].label,
          label: (item) => {
            const p = item.raw;
            const lim = runChart.options.plugins.limitLines.lines;
            const ucl = lim[1] && lim[1].value;
            const lcl = lim[2] && lim[2].value;
            const flag = (ucl !== null && p > ucl) || (lcl !== null && p < lcl)
              ? '  ⚠ out of control' : '';
            return `${fmt(p, 2)} s${flag}`;
          },
        },
      },
      limitLines: { lines: [] },
    },
  },
});

const histChart = new Chart($('histChart'), {
  type: 'bar',
  data: {
    labels: [],
    datasets: [{
      label: 'Count',
      data: [],
      backgroundColor: COLOR.series,
      // One series -> one colour for every bar. Shading by height would
      // double-encode the value the bar length already shows.
      borderRadius: 4,
      borderSkipped: 'bottom',   // round the data end, keep the baseline flat
      barPercentage: 0.94,
      categoryPercentage: 0.98,  // leaves the 2px surface gap between bars
    }],
  },
  options: {
    layout: { padding: { top: 4 } },
    scales: {
      x: {
        grid: { display: false },
        border: { color: COLOR.base },
        // No axis title: at 480px tall it collided with the tick labels, so
        // the unit lives in the panel note instead.
        ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: 9, font: { size: 9 } },
      },
      y: {
        beginAtZero: true,
        grid: { color: COLOR.grid, drawTicks: false },
        border: { display: false },
        ticks: { precision: 0, padding: 4, font: { size: 9 } },
      },
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        ...tooltipStyle,
        callbacks: {
          title: (items) => `${items[0].label} s and up`,
          label: (item) => `${item.raw} ${item.raw === 1 ? 'cycle' : 'cycles'}`,
        },
      },
    },
  },
});

/* ------------------------------------------------------------------ render */

let lastCycles = [];
let lastSummary = {};

function render(payload) {
  const cycles = payload.cycles || [];
  const s = payload.summary || {};
  const h = payload.histogram || {};
  const status = payload.status || {};
  lastCycles = cycles;
  lastSummary = s;

  // Stoppages are excluded from the plotted series: one tea break would
  // rescale the y-axis and hide every bit of real variation. They stay
  // visible as a header chip and as rows in the table view.
  const plotted = cycles.filter((c) => !c.is_stoppage);

  const ucl = s.ucl === undefined ? null : s.ucl;
  const lcl = s.lcl === undefined ? null : s.lcl;

  const pointColors = plotted.map((c) =>
    (ucl !== null && c.cycle_s > ucl) || (lcl !== null && c.cycle_s < lcl)
      ? COLOR.critical
      : COLOR.series);

  // Out-of-control points also get a bigger dot, so the signal survives for a
  // colourblind reader and in a monochrome photo of the screen.
  const pointSizes = pointColors.map((c) => (c === COLOR.critical ? 5 : 3));

  runChart.data.labels = plotted.map((c) => localTime(c.ts_utc));
  runChart.data.datasets[0].data = plotted.map((c) => c.cycle_s);
  runChart.data.datasets[0].pointBackgroundColor = pointColors;
  runChart.data.datasets[0].pointRadius = pointSizes;
  runChart.options.plugins.limitLines.lines = [
    { label: 'x̄', value: s.mean, color: COLOR.secondary, dash: [] },
    { label: 'UCL', value: ucl, color: COLOR.critical, dash: [5, 4] },
    { label: 'LCL', value: lcl, color: COLOR.critical, dash: [5, 4] },
    { label: 'target', value: status.target_s || null, color: COLOR.good, dash: [2, 3] },
  ];
  runChart.update();

  histChart.data.labels = h.labels || [];
  histChart.data.datasets[0].data = h.counts || [];
  histChart.update();

  $('runEmpty').hidden = plotted.length > 0;
  $('histEmpty').hidden = (h.counts || []).length > 0;
  $('runNote').textContent = `last ${plotted.length} of ${status.total_rows || 0} cycles`;

  // ---- header
  const last = cycles.length ? cycles[cycles.length - 1] : null;
  $('vLast').textContent = last ? fmt(last.cycle_s, 1) : '–';
  $('vMean').textContent = fmt(s.mean, 1);
  $('vCv').textContent = fmt(s.cv_pct, 1);
  $('vCount').textContent = status.today_count ?? s.n ?? 0;

  // Chip text is kept terse: the topbar is close to full at 800px.
  const ooc = $('chipOoc');
  ooc.hidden = !s.out_of_control;
  if (s.out_of_control) {
    ooc.textContent = `⚠ ${s.out_of_control} outliers`;
    ooc.className = 'chip alert';
    ooc.title = 'Cycles beyond the ±3σ control limits';
  }

  const stop = $('chipStop');
  stop.hidden = !s.stoppages;
  if (s.stoppages) {
    stop.textContent = `${s.stoppages} stop${s.stoppages > 1 ? 's' : ''}`;
    stop.title = 'Gaps longer than the stoppage threshold — excluded from the chart and statistics';
  }

  renderStatus(status);
  if (!$('panelTable').hidden) renderTable();
}

function renderStatus(status) {
  const dot = $('dot');
  const text = $('stateText');
  if (!status.camera_connected) {
    dot.className = 'dot down';
    text.textContent = 'NO CAMERA';
  } else if (status.warming_up) {
    dot.className = 'dot warn';
    text.textContent = 'WARMING UP';
  } else if (status.detector_state === 'occupied') {
    dot.className = 'dot warn';
    text.textContent = 'PRODUCT';
  } else {
    dot.className = 'dot live';
    text.textContent = 'RUNNING';
  }
}

function renderTable() {
  const ucl = lastSummary.ucl;
  const lcl = lastSummary.lcl;
  const rows = lastCycles.slice().reverse().map((c) => {
    let cls = '';
    let label = 'ok';
    if (c.is_stoppage) {
      cls = 'stop';
      label = 'stoppage';
    } else if ((ucl != null && c.cycle_s > ucl) || (lcl != null && c.cycle_s < lcl)) {
      cls = 'ooc';
      label = 'out of control';
    }
    return `<tr><td>${c.id}</td><td>${localTime(c.ts_utc)}</td>` +
           `<td class="${cls}">${fmt(c.cycle_s, 2)}</td><td class="${cls}">${label}</td></tr>`;
  });
  $('tableBody').innerHTML = rows.join('') ||
    '<tr><td colspan="4" style="color:var(--text-muted)">No cycles recorded yet</td></tr>';
}

/* ------------------------------------------------------------------ fetch */

let inFlight = false;

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  try {
    const res = await fetch(`/api/stats?limit=${LIMIT}`);
    if (!res.ok) throw new Error(res.statusText);
    render(await res.json());
  } catch (err) {
    $('dot').className = 'dot down';
    $('stateText').textContent = 'OFFLINE';
  } finally {
    inFlight = false;
  }
}

/* -------------------------------------------------------------------- SSE */

function connectEvents() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    try {
      if (JSON.parse(ev.data).type === 'cycle') refresh();
    } catch (_) { /* keepalive comments never reach here */ }
  };
  // EventSource reconnects on its own; this only surfaces the state.
  es.onerror = () => { $('dot').className = 'dot down'; };
}

/* ------------------------------------------------------------------- view */

$('btnView').addEventListener('click', () => {
  const showTable = $('panelTable').hidden;
  $('panelTable').hidden = !showTable;
  $('panelRun').hidden = showTable;
  $('panelDist').hidden = showTable;
  $('btnView').textContent = showTable ? 'Charts' : 'Table';
  if (showTable) renderTable();
});

$('btnCsv').addEventListener('click', () => toast('Exporting CSV…'));

/* ------------------------------------------------------------------- boot */

refresh();
connectEvents();
// Slow poll as a safety net: keeps the camera/state indicator honest even
// while no products are passing and no SSE messages are arriving.
setInterval(refresh, 5000);
