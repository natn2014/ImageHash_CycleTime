/* 40" wall scoreboard.
 *
 * A dumb renderer: the server assembles /api/board with the ranking and the
 * date arithmetic already done, so the day, week and month panels cannot
 * disagree with each other or with the dashboard.
 *
 * Two update paths, deliberately different rates:
 *   - the live line is event-driven (SSE), so it changes the instant a product
 *     passes;
 *   - the aggregates refresh on a timer, because a wall board has no need to
 *     recompute a month average every few seconds.
 */
'use strict';

const $ = (id) => document.getElementById(id);

const AGGREGATE_MS = 30000;   // day/week/month refresh
// Stopwatch for the cycle in progress. Ticking at 10 Hz keeps the tenths
// moving, which is what makes the board look alive between products; set
// SINCE_DP to 0 for a whole-second counter instead.
const SINCE_TICK_MS = 100;
const SINCE_DP = 1;
// Two missed refreshes before the board admits it is stale. A frozen number
// that is silently believed is worse than an obvious "NO DATA".
const STALE_MS = AGGREGATE_MS * 2 + 5000;

let lastOk = Date.now();
let teams = [];

/* --------------------------------------------------------- live stopwatch */

// performance.now() at the moment the running cycle started. Monotonic, so a
// clock step on this Pi cannot send the counter backwards mid-shift; null
// means nothing has ever been detected and there is nothing to count.
let sinceRef = null;

function syncSince(sinceS) {
  sinceRef = sinceS === null || sinceS === undefined
    ? null
    : performance.now() - sinceS * 1000;
  drawSince();
}

function drawSince() {
  const el = $('liveSince');
  if (sinceRef === null) {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const elapsed = Math.max(0, (performance.now() - sinceRef) / 1000);
  $('liveSinceVal').textContent = elapsed.toFixed(SINCE_DP);
}

const fmt = (v, d = 1) => (v === null || v === undefined ? '—' : Number(v).toFixed(d));

function fmtDate(iso) {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return iso || '—';
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()}`;
}

/* ----------------------------------------------------------------- render */

function renderLive(data) {
  const live = data.live || {};
  const shift = data.shift || {};
  const el = $('live');

  const hasTeam = Boolean(shift.team);
  const hasCycle = live.cycle_s !== null && live.cycle_s !== undefined;

  if (!hasTeam) {
    // A deliberate "nobody on this shift" must not borrow the last team's name.
    $('liveText').textContent = 'NO TEAM SCHEDULED';
    el.classList.add('idle');
  } else if (!hasCycle) {
    $('liveText').textContent = `TEAM ${shift.team}: WAITING`;
    el.classList.add('idle');
  } else {
    $('liveText').innerHTML =
      `TEAM ${shift.team}: ${fmt(live.cycle_s)}<span class="live-unit"> sec</span>`;
    el.classList.remove('idle');
  }

  // Re-anchor the stopwatch on every poll rather than trusting it to have
  // free-run correctly since the last one.
  syncSince(live.since_s);

  $('targetVal').textContent = `${fmt(live.target_s)} sec`;

  const diffEl = $('diff');
  if (!hasCycle) {
    $('diffVal').textContent = '—';
    diffEl.classList.remove('over');
    return;
  }

  const diff = live.diff_s;
  const tol = live.tolerance_s ?? 0;
  // Sign and arrow carry the same information as the colour, so the state is
  // still readable in monochrome or by a colourblind operator.
  const arrow = diff > 0 ? '▲' : diff < 0 ? '▼' : '';
  const sign = diff > 0 ? '+' : '';
  $('diffVal').textContent = `${arrow} ${sign}${fmt(diff)} sec`;
  diffEl.classList.toggle('over', diff > tol);
}

function rankRow(row, currentTeam) {
  const empty = row.avg === null || row.avg === undefined;
  const cls = ['rank-row'];
  if (empty) cls.push('empty');
  if (row.team === currentTeam) cls.push('current');
  return `<div class="${cls.join(' ')}">
      <span class="rank-num">${row.rank ?? '–'}</span>
      <span class="rank-name">Team ${row.team}:</span>
      <span class="rank-value">${fmt(row.avg)}</span>
      <span class="rank-unit">${empty ? '' : 'sec'}</span>
    </div>`;
}

function renderDay(data) {
  $('dayDate').textContent = fmtDate(data.work_date);
  const current = (data.shift || {}).team;
  $('dayList').innerHTML = (data.day || []).map((r) => rankRow(r, current)).join('');
}

function renderMonth(data) {
  const month = data.month || {};
  $('monthLabel').textContent = month.label || '—';
  const current = (data.shift || {}).team;
  $('monthList').innerHTML = (month.rows || []).map((r) => rankRow(r, current)).join('');
}

function renderWeeks(data) {
  const weeks = data.weeks || [];

  // Best (lowest) value in each week column, highlighted so a glance shows who
  // led that week without re-sorting the rows.
  const bestPerWeek = weeks.map((w) => {
    const vals = Object.values(w.teams || {}).filter((v) => v !== null && v !== undefined);
    return vals.length ? Math.min(...vals) : null;
  });

  $('weekBody').innerHTML = teams.map((team) => {
    const cells = weeks.map((w, i) => {
      const v = (w.teams || {})[team];
      const has = v !== null && v !== undefined;
      const isBest = has && bestPerWeek[i] !== null && Math.abs(v - bestPerWeek[i]) < 1e-9;
      return `<td class="${has ? (isBest ? 'best' : '') : 'na'}">${fmt(v)}</td>`;
    }).join('');
    return `<tr><td class="team-name">Team ${team}:</td>${cells}</tr>`;
  }).join('');
}

function render(data) {
  $('station').textContent = data.station || '—';
  teams = data.teams || [];
  renderLive(data);
  renderDay(data);
  renderWeeks(data);
  renderMonth(data);
}

/* ------------------------------------------------------------------ fetch */

let inFlight = false;

async function refresh() {
  if (inFlight) return;
  inFlight = true;
  try {
    const res = await fetch('/api/board');
    if (!res.ok) throw new Error(res.statusText);
    render(await res.json());
    lastOk = Date.now();
    document.body.classList.remove('stale');
  } catch (_) {
    if (Date.now() - lastOk > STALE_MS) document.body.classList.add('stale');
  } finally {
    inFlight = false;
  }
}

/* -------------------------------------------------------------------- SSE */

function connectEvents() {
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    try {
      // A new cycle changes the live number and today's average, so the whole
      // payload is refetched rather than patched — it is one small request at
      // a 5-30 s cadence, and it keeps every panel mutually consistent.
      if (JSON.parse(ev.data).type !== 'cycle') return;
      // Zero the stopwatch on the event itself, not on the response: the
      // product has already passed, and waiting a round trip to show it would
      // leave the counter visibly overshooting on every cycle.
      syncSince(0);
      refresh();
    } catch (_) { /* keepalive comments never parse */ }
  };
  es.onerror = () => { /* EventSource reconnects itself; the timer covers us */ };
}

/* ------------------------------------------------------------------- boot */

refresh();
connectEvents();
setInterval(refresh, AGGREGATE_MS);
setInterval(drawSince, SINCE_TICK_MS);

// The date panel must roll over at midnight even on a line that has stopped
// and is sending no events.
setInterval(() => {
  const now = new Date();
  if (now.getHours() === 0 && now.getMinutes() === 0) refresh();
}, 60000);
