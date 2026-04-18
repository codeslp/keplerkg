/* cgraph-patch.js — simplified Projector for cgraph.
 *
 *   - Flip into 3D (Z axis) once PCA finishes.
 *   - Inject cgraph-patch.css into every Polymer shadow root we know about
 *     (document-level styles don't pierce shadow DOM on their own).
 *   - Toggle .cgraph-simple on <body> unless the URL has ?advanced=1.
 *   - Rebrand banner title.
 *   - Side-rail collapse toggles.
 *
 * Loaded from a <script defer> tag appended by the Python patcher to the
 * vendored Projector's index.html.  Fail-safe: if any selector is missing
 * (upstream rename), we log and keep running; the pane is still usable.
 */

(function () {
  'use strict';

  const CSS_HREF = 'cgraph-patch.css';
  const SIMPLE_DEFAULT = !(new URLSearchParams(location.search).get('advanced'));
  const BANNER_TITLE = 'Embedding Projector: Which Functions Are Similar to Each Other?';

  function log(...args) { console.log('[cgraph-patch]', ...args); }
  function warn(...args) { console.warn('[cgraph-patch]', ...args); }

  // --- shadow-root style injection ------------------------------------------

  let cssText = '';
  async function loadCss() {
    try {
      const r = await fetch(CSS_HREF, { cache: 'force-cache' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      cssText = await r.text();
    } catch (e) {
      warn('failed to load', CSS_HREF, e);
    }
  }

  function injectIntoShadow(el) {
    if (!el || !el.shadowRoot) return;
    if (el.shadowRoot.querySelector('style[data-cgraph]')) return;
    const style = document.createElement('style');
    style.setAttribute('data-cgraph', '1');
    style.textContent = cssText;
    el.shadowRoot.appendChild(style);
  }

  function walkAndInject(root) {
    if (!root) return;
    const host = root.host || root;
    if (host instanceof Element) injectIntoShadow(host);
    const all = (root.querySelectorAll ? root.querySelectorAll('*') : []);
    for (const el of all) {
      if (el.shadowRoot) {
        injectIntoShadow(el);
        walkAndInject(el.shadowRoot);
      }
    }
  }

  // --- polling helper for Polymer-ready elements ----------------------------

  function waitFor(predicate, timeoutMs = 10000, intervalMs = 100) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      (function tick() {
        let got;
        try { got = predicate(); } catch {}
        if (got) return resolve(got);
        if (Date.now() - start > timeoutMs) return reject(new Error('timeout'));
        setTimeout(tick, intervalMs);
      })();
    });
  }

  function deepQuery(root, selector) {
    if (!root) return null;
    const here = root.querySelector ? root.querySelector(selector) : null;
    if (here) return here;
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) {
      if (el.shadowRoot) {
        const found = deepQuery(el.shadowRoot, selector);
        if (found) return found;
      }
    }
    return null;
  }

  function deepQueryAll(root, selector, out = []) {
    if (!root) return out;
    if (root.querySelectorAll) {
      for (const el of root.querySelectorAll(selector)) out.push(el);
    }
    const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
    for (const el of all) {
      if (el.shadowRoot) deepQueryAll(el.shadowRoot, selector, out);
    }
    return out;
  }

  // --- the actual UI tweaks -------------------------------------------------

  async function forceThreeD() {
    try {
      const panel = await waitFor(
        () => deepQuery(document, 'vz-projector-projections-panel'),
        8000,
      );
      const zBox = deepQuery(panel, 'paper-checkbox');
      if (zBox && !zBox.checked) {
        zBox.click();
        log('3D (Z axis): on');
      } else if (zBox) {
        log('3D (Z axis): already on');
      }
    } catch {
      warn('z-axis checkbox not found (remaining 2D)');
    }
  }

  async function rebrandBanner() {
    try {
      const appbar = await waitFor(
        () => deepQuery(document, '#appbar'),
        5000,
      );
      const titleEl = appbar.querySelector(':scope > div:first-child');
      if (titleEl && titleEl.textContent.trim() === 'Embedding Projector') {
        titleEl.textContent = BANNER_TITLE;
        log('banner title rebranded');
      } else {
        warn('banner title element not in expected shape (unchanged)');
      }
    } catch {
      warn('banner not found (title unchanged)');
    }
  }

  // --- side-rail collapse toggles -----------------------------------------

  function injectRailToggle(pane, side) {
    if (!pane) return;
    if (pane.querySelector(':scope > .cgraph-rail-toggle')) return;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cgraph-rail-toggle';
    btn.dataset.side = side;
    const EXPANDED = side === 'left' ? '\u25C0' : '\u25B6';
    const COLLAPSED = side === 'left' ? '\u25B6' : '\u25C0';
    btn.textContent = EXPANDED;
    btn.setAttribute(
      'title',
      side === 'left' ? 'Collapse data panel' : 'Collapse inspector panel',
    );
    btn.addEventListener('click', () => {
      const collapsed = pane.classList.toggle('cgraph-rail-collapsed');
      btn.textContent = collapsed ? COLLAPSED : EXPANDED;
      btn.setAttribute(
        'title',
        collapsed
          ? (side === 'left' ? 'Expand data panel' : 'Expand inspector panel')
          : (side === 'left' ? 'Collapse data panel' : 'Collapse inspector panel'),
      );
    });
    pane.appendChild(btn);
  }

  async function installRailToggles() {
    try {
      const leftPane = await waitFor(
        () => deepQuery(document, '#left-pane'),
        5000,
      );
      injectRailToggle(leftPane, 'left');
    } catch {
      warn('#left-pane not found; left-rail toggle skipped');
    }
    try {
      const rightPane = await waitFor(
        () => deepQuery(document, '#right-pane'),
        5000,
      );
      injectRailToggle(rightPane, 'right');
    } catch {
      warn('#right-pane not found; right-rail toggle skipped');
    }
  }

  function applySimpleClass() {
    if (SIMPLE_DEFAULT) {
      document.body.classList.add('cgraph-simple');
      log('simple mode on (pass ?advanced=1 to disable)');
    } else {
      log('advanced mode (URL had ?advanced=1)');
    }
  }

  // --- orchestrate ----------------------------------------------------------

  async function run() {
    await loadCss();
    applySimpleClass();

    walkAndInject(document);
    for (const ms of [500, 1500, 3000]) {
      setTimeout(() => walkAndInject(document), ms);
    }

    await forceThreeD();
    await rebrandBanner();
    await installRailToggles();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
