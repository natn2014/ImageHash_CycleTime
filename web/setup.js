/* ROI setup: drag a rectangle on the live view, save it to the detector.
 *
 * The MJPEG <img> renders at whatever size fits the pane, but the detector
 * works in camera pixels. Every coordinate is therefore converted through
 * `scale` on save, and back again on load, so a saved ROI lands on the same
 * belt whether it was drawn on the 7" panel or a laptop.
 */
'use strict';

const $ = (id) => document.getElementById(id);

const img = $('preview');
const canvas = $('roiCanvas');
const ctx = canvas.getContext('2d');

const CSS = getComputedStyle(document.documentElement);
const COLOR = {
  series: CSS.getPropertyValue('--series-1').trim(),
  critical: CSS.getPropertyValue('--critical').trim(),
  muted: CSS.getPropertyValue('--text-muted').trim(),
};

// ROI in camera pixels — the single source of truth on this page.
let roi = null;
let savedRoi = null;
let drag = null;

let toastTimer = null;
function toast(msg) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2200);
}

/* ------------------------------------------------------------ geometry */

function scale() {
  // Camera pixels per displayed pixel. Guard against a not-yet-loaded image.
  return img.naturalWidth && img.clientWidth ? img.naturalWidth / img.clientWidth : 1;
}

function syncCanvas() {
  // Match the canvas to the image's rendered box exactly, including its
  // offset inside the centred flex pane.
  canvas.width = img.clientWidth;
  canvas.height = img.clientHeight;
  canvas.style.width = `${img.clientWidth}px`;
  canvas.style.height = `${img.clientHeight}px`;
  canvas.style.left = `${img.offsetLeft}px`;
  canvas.style.top = `${img.offsetTop}px`;
  draw();
}

function draw() {
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!roi) return;

  const s = scale();
  const x = roi.x / s;
  const y = roi.y / s;
  const w = roi.w / s;
  const h = roi.h / s;

  // Dim everything outside the ROI so the trip-line reads at a glance.
  ctx.fillStyle = 'rgba(0,0,0,0.45)';
  ctx.fillRect(0, 0, canvas.width, y);
  ctx.fillRect(0, y + h, canvas.width, canvas.height - y - h);
  ctx.fillRect(0, y, x, h);
  ctx.fillRect(x + w, y, canvas.width - x - w, h);

  const unsaved = !savedRoi || savedRoi.x !== roi.x || savedRoi.y !== roi.y ||
                  savedRoi.w !== roi.w || savedRoi.h !== roi.h;
  ctx.strokeStyle = unsaved ? COLOR.critical : COLOR.series;
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, w, h);

  ctx.fillStyle = ctx.strokeStyle;
  ctx.font = '600 11px system-ui, -apple-system, "Segoe UI", sans-serif';
  const label = `${roi.w}x${roi.h}${unsaved ? '  (unsaved)' : ''}`;
  ctx.fillText(label, x, Math.max(12, y - 5));
}

function pointerPos(ev) {
  const rect = canvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(ev.clientX - rect.left, canvas.width)),
    y: Math.max(0, Math.min(ev.clientY - rect.top, canvas.height)),
  };
}

/* ---------------------------------------------------------------- drag */

canvas.addEventListener('pointerdown', (ev) => {
  canvas.setPointerCapture(ev.pointerId);
  drag = { start: pointerPos(ev) };
});

canvas.addEventListener('pointermove', (ev) => {
  if (!drag) return;
  const p = pointerPos(ev);
  const s = scale();
  // Normalise so dragging in any direction produces a positive-size box.
  const x = Math.min(drag.start.x, p.x);
  const y = Math.min(drag.start.y, p.y);
  const w = Math.abs(p.x - drag.start.x);
  const h = Math.abs(p.y - drag.start.y);
  roi = {
    x: Math.round(x * s), y: Math.round(y * s),
    w: Math.round(w * s), h: Math.round(h * s),
  };
  draw();
});

function endDrag() {
  if (!drag) return;
  drag = null;
  if (roi && (roi.w < 8 || roi.h < 8)) {
    // A tap rather than a drag — restore instead of leaving a useless sliver.
    roi = savedRoi ? { ...savedRoi } : null;
    draw();
    toast('Drag a box — that was too small');
  }
}

canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', endDrag);

/* ---------------------------------------------------------------- api */

async function loadRoi() {
  const res = await fetch('/api/roi');
  savedRoi = await res.json();
  roi = { ...savedRoi };
  draw();
}

async function loadDetector() {
  const d = await fetch('/api/detector').then((r) => r.json());
  $('enter').value = d.enter_ratio;
  $('exit').value = d.exit_ratio;
  $('thr').value = d.diff_threshold;
  $('dwell').value = d.min_present_s;
  $('markEnter').style.left = `${d.enter_ratio * 100}%`;
  $('markExit').style.left = `${d.exit_ratio * 100}%`;
}

$('btnSave').addEventListener('click', async () => {
  if (!roi) return toast('Draw a box first');
  const res = await fetch('/api/roi', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(roi),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    return toast(err.detail || 'Save failed');
  }
  // The server clamps to the frame, so adopt what it actually stored.
  savedRoi = await res.json();
  roi = { ...savedRoi };
  draw();
  toast('ROI saved — background relearning');
});

$('btnReset').addEventListener('click', () => {
  roi = savedRoi ? { ...savedRoi } : null;
  draw();
  toast('Reverted to saved ROI');
});

$('btnApply').addEventListener('click', async () => {
  const body = {
    enter_ratio: parseFloat($('enter').value),
    exit_ratio: parseFloat($('exit').value),
    diff_threshold: parseInt($('thr').value, 10),
    min_present_s: parseFloat($('dwell').value),
  };
  const res = await fetch('/api/detector', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) return toast('Could not apply thresholds');
  await loadDetector();   // reflect any values the server corrected
  toast('Thresholds applied and saved');
});

/* ------------------------------------------------------------- status */

async function pollStatus() {
  try {
    const s = await fetch('/api/status').then((r) => r.json());
    const pct = (s.occupancy || 0) * 100;
    $('meterFill').style.width = `${Math.min(100, pct)}%`;
    $('meterFill').style.background = s.detector_state === 'occupied' ? COLOR.critical : COLOR.series;
    $('readout').textContent =
      `${pct.toFixed(1)}%  ·  ${s.detector_state}  ·  ${s.detections} detected`;

    const dot = $('dot');
    if (!s.camera_connected) {
      dot.className = 'dot down';
      $('stateText').textContent = 'NO CAMERA';
    } else if (s.warming_up) {
      dot.className = 'dot warn';
      $('stateText').textContent = 'WARMING UP';
    } else {
      dot.className = 'dot live';
      $('stateText').textContent = 'LIVE';
    }
  } catch (_) {
    $('dot').className = 'dot down';
    $('stateText').textContent = 'OFFLINE';
  }
}

/* --------------------------------------------------------------- boot */

img.addEventListener('load', syncCanvas);
window.addEventListener('resize', syncCanvas);
// The MJPEG stream fires 'load' only on the first frame in some browsers, so
// nudge the canvas once the layout has settled regardless.
setTimeout(syncCanvas, 600);

loadRoi();
loadDetector();
pollStatus();
setInterval(pollStatus, 400);
