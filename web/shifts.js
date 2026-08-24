/* Shifts tab: month calendar + rotation pattern.
 *
 * The calendar shows the *resolved* roster — rotation plus any overrides —
 * because that is what an operator needs to check. Overridden slots are marked
 * so exceptions stand out against the automatic pattern.
 */
'use strict';

const $ = (id) => document.getElementById(id);

let cursor = new Date();          // month being displayed
let selected = null;              // YYYY-MM-DD
let monthData = null;
let teams = [];
let starts = [];

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;

/* --------------------------------------------------------------- views */

function showView(which) {
  const cal = which === 'cal';
  $('viewCal').hidden = !cal;
  $('viewPat').hidden = cal;
  $('tabCal').classList.toggle('active', cal);
  $('tabPat').classList.toggle('active', !cal);
  if (!cal) loadPreview();
}

$('tabCal').addEventListener('click', () => showView('cal'));
$('tabPat').addEventListener('click', () => showView('pat'));

/* ------------------------------------------------------------ calendar */

async function loadMonth() {
  const y = cursor.getFullYear();
  const m = cursor.getMonth() + 1;
  const res = await fetch(`/api/shifts/month?year=${y}&month=${m}`);
  monthData = await res.json();
  teams = monthData.teams || [];
  starts = monthData.starts || [];
  $('monthLabel').textContent =
    cursor.toLocaleDateString([], { month: 'long', year: 'numeric' });
  renderGrid();
}

function renderGrid() {
  const grid = $('calGrid');
  const days = monthData.days || [];
  const todayIso = iso(new Date());

  const headers = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    .map((d) => `<div class="cal-head">${d}</div>`).join('');

  // Monday-first: getDay() is Sunday-based, so Sunday's 0 maps to 6.
  const first = new Date(`${days[0].date}T00:00:00`);
  const lead = (first.getDay() + 6) % 7;
  const blanks = Array.from({ length: lead },
    () => '<button class="cal-day" disabled></button>').join('');

  const cells = days.map((day) => {
    const chips = day.slots.map((s) => {
      const cls = ['cal-team'];
      if (s.overridden) cls.push('over');
      if (!s.team) cls.push('none');
      return `<span class="${cls.join(' ')}">${s.team || '–'}</span>`;
    }).join('');

    const cls = ['cal-day'];
    if (day.date === selected) cls.push('selected');
    if (day.date === todayIso) cls.push('today');
    const num = Number(day.date.slice(-2));
    return `<button class="${cls.join(' ')}" data-date="${day.date}">
        <span class="cal-num">${num}</span>
        <span class="cal-teams">${chips}</span>
      </button>`;
  }).join('');

  grid.innerHTML = headers + blanks + cells;
  grid.querySelectorAll('.cal-day[data-date]').forEach((el) => {
    el.addEventListener('click', () => selectDay(el.dataset.date));
  });
}

function dayFor(dateStr) {
  return (monthData.days || []).find((d) => d.date === dateStr);
}

function selectDay(dateStr) {
  selected = dateStr;
  renderGrid();
  renderEditor();
}

function renderEditor() {
  const day = dayFor(selected);
  if (!day) return;

  $('editDate').textContent = new Date(`${selected}T00:00:00`)
    .toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });

  // "—" is a real choice, not an absence: it records that nobody is on this
  // shift, which must not fall back to the rotation.
  const options = [...teams, null];

  $('editRows').innerHTML = day.slots.map((s) => {
    const buttons = options.map((t) => {
      const on = (t || null) === (s.team || null) ? ' on' : '';
      return `<button class="pick${on}" data-slot="${s.slot}" data-team="${t ?? ''}">${t ?? '—'}</button>`;
    }).join('');
    return `<div class="edit-row"><span class="edit-time">${s.start}</span>${buttons}</div>`;
  }).join('');

  $('editRows').querySelectorAll('.pick').forEach((el) => {
    el.addEventListener('click', () => setSlot(
      Number(el.dataset.slot), el.dataset.team || null,
    ));
  });

  const overridden = day.slots.some((s) => s.overridden);
  $('btnClearDay').disabled = !overridden;
  $('editNote').textContent = overridden
    ? 'Overridden — this day no longer follows the rotation.'
    : 'Following the automatic rotation.';
}

async function setSlot(slot, team) {
  const res = await fetch('/api/shifts/override', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ date: selected, slot, team }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return toast(err.detail || 'Could not save');
  }
  const updated = await res.json();
  dayFor(selected).slots = updated.slots;
  renderGrid();
  renderEditor();
  toast(team ? `Shift ${slot + 1} → Team ${team}` : `Shift ${slot + 1} → nobody`);
}

$('btnClearDay').addEventListener('click', async () => {
  if (!selected) return;
  const res = await fetch(`/api/shifts/override?date=${selected}`, { method: 'DELETE' });
  if (!res.ok) return toast('Could not clear');
  const updated = await res.json();
  dayFor(selected).slots = updated.slots;
  renderGrid();
  renderEditor();
  toast('Back to the rotation');
});

$('prevMonth').addEventListener('click', () => {
  cursor = new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1);
  loadMonth();
});
$('nextMonth').addEventListener('click', () => {
  cursor = new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1);
  loadMonth();
});

/* ------------------------------------------------------------- pattern */

/* The letters offered for a rota. Six is plenty — the server only ever keeps
 * one team per shift — and they fit one row on a 300px panel. Names already in
 * the config are added to the pool so a site using its own labels can still
 * reorder them without losing them. */
const TEAM_LETTERS = ['A', 'B', 'C', 'D', 'E', 'F'];

let patternTeams = [];   // the sequence being edited, one entry per shift
let teamSel = 0;         // box the next tapped letter fills

function setPatternTeams(list) {
  patternTeams = starts.map((_, i) => list[i]);
  // A short or duplicated list can only come from a hand-edited config; fill it
  // out here rather than letting the picker start in a state it cannot express.
  patternTeams.forEach((t, i) => {
    if (!t || patternTeams.indexOf(t) !== i) {
      patternTeams[i] = TEAM_LETTERS.find((l) => !patternTeams.includes(l));
    }
  });
  teamSel = 0;
  renderTeams();
}

function renderTeams() {
  const times = ['s0', 's1', 's2'].map((id) => $(id).value);

  $('teamSeq').innerHTML = patternTeams.map((t, i) => `
    <button type="button" class="team-slot${i === teamSel ? ' sel' : ''}" data-slot="${i}">
      <span class="team-slot-time">${times[i] || `Shift ${i + 1}`}</span>
      <span class="team-slot-name">${t || '–'}</span>
    </button>`).join('');

  const pool = [...new Set([...TEAM_LETTERS, ...patternTeams.filter(Boolean)])];
  $('teamPool').innerHTML = pool.map((label) => {
    const at = patternTeams.indexOf(label);
    // The shift number on an in-use letter says *where* it sits, so the pool is
    // readable without comparing fills by colour.
    const badge = at === -1 ? '' : `<span class="team-key-n">${at + 1}</span>`;
    return `<button type="button" class="team-key${at === -1 ? '' : ' used'}"
      data-team="${label}">${label}${badge}</button>`;
  }).join('');

  $('teamSeq').querySelectorAll('.team-slot').forEach((el) => {
    el.addEventListener('click', () => { teamSel = Number(el.dataset.slot); renderTeams(); });
  });
  $('teamPool').querySelectorAll('.team-key').forEach((el) => {
    el.addEventListener('click', () => pickTeam(el.dataset.team));
  });
}

function pickTeam(label) {
  const at = patternTeams.indexOf(label);
  if (at !== teamSel) {
    // Swapping rather than overwriting keeps every shift staffed by a distinct
    // team, which is the only shape the server accepts.
    if (at !== -1) patternTeams[at] = patternTeams[teamSel];
    patternTeams[teamSel] = label;
  }
  teamSel = (teamSel + 1) % patternTeams.length;
  renderTeams();
}

async function loadPattern() {
  const c = await fetch('/api/shifts/config').then((r) => r.json());
  starts = c.starts || [];
  teams = c.teams || [];
  ['s0', 's1', 's2'].forEach((id, i) => { $(id).value = starts[i] || ''; });
  $('rotDays').value = c.rotation_days;
  $('anchor').value = c.rotation_anchor;
  setPatternTeams(teams);
}

// Edited start times label the boxes, so the picker keeps showing which team
// works which clock hour.
['s0', 's1', 's2'].forEach((id) => $(id).addEventListener('change', renderTeams));

Keypad.attach($('rotDays'), { title: 'Rotate every (days)' });

async function loadPreview() {
  const data = await fetch('/api/shifts/preview?days=14').then((r) => r.json());
  const head = `<div class="preview-row head"><span>Date</span>` +
    starts.map((s) => `<span>${s}</span>`).join('') + '</div>';
  $('previewTable').innerHTML = head + (data.days || []).map((d) => {
    const label = new Date(`${d.date}T00:00:00`)
      .toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' });
    return `<div class="preview-row"><span>${label}</span>` +
      d.teams.map((t) => `<span>${t || '—'}</span>`).join('') + '</div>';
  }).join('');
}

$('btnSavePattern').addEventListener('click', async () => {
  const body = {
    starts: ['s0', 's1', 's2'].map((id) => $(id).value),
    teams: patternTeams.filter(Boolean),
    rotation_days: parseInt($('rotDays').value, 10),
    rotation_anchor: $('anchor').value,
  };
  const res = await fetch('/api/shifts/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) return toast('Could not save pattern');

  // Reload from the server: it repairs anything unusable rather than
  // rejecting it, so the fields must show what was actually stored.
  await loadPattern();
  await loadPreview();
  await loadMonth();
  toast('Pattern saved');
});

/* -------------------------------------------------------------- status */

async function loadNow() {
  try {
    const s = await fetch('/api/shifts/current').then((r) => r.json());
    $('nowChip').textContent = s.team
      ? `On now: Team ${s.team} (${s.slot_start})`
      : 'On now: nobody scheduled';
  } catch (_) {
    $('nowChip').textContent = 'offline';
  }
}

/* ---------------------------------------------------------------- boot */

(async function boot() {
  await loadPattern();
  await loadMonth();
  selectDay(iso(new Date()));
  loadNow();
  setInterval(loadNow, 30000);
})();
