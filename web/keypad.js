/* On-screen numeric keypad for the kiosk panel.
 *
 * The 7" panel is finger-only: there is no keyboard attached and Chromium in
 * kiosk mode never raises a soft one, so a bare <input type="number"> cannot be
 * filled in at all — the spinner arrows are smaller than a fingertip and there
 * is nowhere to type. Fields wired to this open a full keypad instead.
 *
 *   Keypad.attach(input, { title: 'Rotate every (days)' });
 *
 * min / max / step are read off the input, so the range shown and the values
 * accepted stay in step with the markup rather than being restated here.
 */
'use strict';

const Keypad = (function () {

  // Labelled in words rather than with ⌫: the panel's font stack is whatever
  // the Pi ships, and a missing glyph on the only delete key is not a risk
  // worth taking for a symbol.
  const LAYOUT = [
    ['7', '7'], ['8', '8'], ['9', '9'],
    ['4', '4'], ['5', '5'], ['6', '6'],
    ['1', '1'], ['2', '2'], ['3', '3'],
    ['.', '.'], ['0', '0'], ['back', 'DEL'],
  ];
  const MAX_LEN = 8;

  let root = null;      // built on first use
  let els = {};
  let target = null;    // input being edited
  let buffer = '';
  // The first digit replaces the old value rather than appending to it: the
  // common edit here is "it should be 14, not 7", not "append a digit".
  let fresh = false;

  /* ------------------------------------------------------------ building */

  function build() {
    root = document.createElement('div');
    root.className = 'kp-backdrop';
    root.hidden = true;
    root.innerHTML = `
      <div class="kp" role="dialog" aria-modal="true" aria-label="Number pad">
        <div class="kp-title"></div>
        <div class="kp-display" aria-live="polite"></div>
        <div class="kp-hint"></div>
        <div class="kp-keys">${
          LAYOUT.map(([key, label]) =>
            `<button type="button" class="kp-key${key === 'back' ? ' wide-label' : ''}"
               data-key="${key}">${label}</button>`).join('')
        }</div>
        <div class="kp-actions">
          <button type="button" class="btn kp-cancel">Cancel</button>
          <button type="button" class="btn primary kp-ok">Set</button>
        </div>
      </div>`;
    document.body.appendChild(root);

    els = {
      title: root.querySelector('.kp-title'),
      display: root.querySelector('.kp-display'),
      hint: root.querySelector('.kp-hint'),
      ok: root.querySelector('.kp-ok'),
      dot: root.querySelector('.kp-key[data-key="."]'),
    };

    root.querySelectorAll('.kp-key').forEach((el) => {
      el.addEventListener('click', () => press(el.dataset.key));
    });
    els.ok.addEventListener('click', commit);
    root.querySelector('.kp-cancel').addEventListener('click', close);
    // Tapping outside the pad is the fastest way out on a touchscreen, and it
    // must not commit — a mistap should leave the field as it was.
    root.addEventListener('click', (e) => { if (e.target === root) close(); });

    // A physical keyboard is never present on the panel, but it is on a laptop
    // during setup and testing.
    document.addEventListener('keydown', (e) => {
      if (!target) return;
      if (e.key >= '0' && e.key <= '9') press(e.key);
      else if (e.key === '.') press('.');
      else if (e.key === 'Backspace') press('back');
      else if (e.key === 'Enter') commit();
      else if (e.key === 'Escape') close();
      else return;
      e.preventDefault();
    });
  }

  /* ------------------------------------------------------------- editing */

  function decimalsAllowed() {
    const step = target.getAttribute('step');
    if (step === 'any') return true;
    return !!step && !Number.isInteger(parseFloat(step));
  }

  function limits() {
    const min = target.min === '' ? -Infinity : parseFloat(target.min);
    const max = target.max === '' ? Infinity : parseFloat(target.max);
    return { min, max };
  }

  function value() {
    if (buffer === '' || buffer === '.') return NaN;
    return parseFloat(buffer);
  }

  function valid() {
    const v = value();
    const { min, max } = limits();
    return Number.isFinite(v) && v >= min && v <= max;
  }

  function press(key) {
    if (key === 'back') {
      buffer = fresh ? '' : buffer.slice(0, -1);
      fresh = false;
    } else if (key === '.') {
      if (!decimalsAllowed()) return;
      if (fresh) { buffer = '0'; fresh = false; }
      if (!buffer.includes('.')) buffer += buffer === '' ? '0.' : '.';
    } else {
      if (fresh) { buffer = ''; fresh = false; }
      if (buffer.length >= MAX_LEN) return;
      buffer = buffer === '0' ? key : buffer + key;
    }
    render();
  }

  function render() {
    const { min, max } = limits();
    const range = [
      Number.isFinite(min) ? min : null,
      Number.isFinite(max) ? max : null,
    ];
    els.display.textContent = buffer === '' ? '—' : buffer;
    els.display.classList.toggle('bad', buffer !== '' && !valid());
    els.hint.textContent = range[0] === null && range[1] === null ? ''
      : `${range[0] ?? '−∞'} – ${range[1] ?? '∞'}`;
    els.hint.classList.toggle('bad', buffer !== '' && !valid());
    els.ok.disabled = !valid();
    els.dot.disabled = !decimalsAllowed();
  }

  /* ------------------------------------------------------ open / close */

  function open(input, opts) {
    if (!root) build();
    target = input;
    buffer = input.value ?? '';
    fresh = buffer !== '';
    els.title.textContent = opts.title || labelFor(input) || 'Value';
    render();
    root.hidden = false;
  }

  function close() {
    target = null;
    if (root) root.hidden = true;
  }

  function commit() {
    if (!valid()) return;
    target.value = String(value());
    // Anything listening to the field — validation, live previews — should see
    // this exactly as if it had been typed.
    target.dispatchEvent(new Event('input', { bubbles: true }));
    target.dispatchEvent(new Event('change', { bubbles: true }));
    close();
  }

  function labelFor(input) {
    const el = input.id && document.querySelector(`label[for="${input.id}"]`);
    return el ? el.textContent.trim() : '';
  }

  /* --------------------------------------------------------------- api */

  function attach(input, opts = {}) {
    // readonly is what actually keeps Chromium from focusing a caret into the
    // field and, on a tablet, from raising its own keyboard over ours.
    input.readOnly = true;
    input.setAttribute('inputmode', 'none');
    input.classList.add('keypad-input');
    input.addEventListener('click', () => open(input, opts));
    return input;
  }

  return { attach, open, close };
})();
