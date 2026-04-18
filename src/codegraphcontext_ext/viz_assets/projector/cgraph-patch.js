/* cgraph-patch.js — simplified Projector for cgraph.
 *
 *   - Force night mode on load so the scene matches cgraph dark.
 *   - Flip into 3D (Z axis) once PCA finishes.
 *   - Inject cgraph-patch.css into every Polymer shadow root we know about
 *     (document-level styles don't pierce shadow DOM on their own).
 *   - Toggle .cgraph-simple on <body> unless the URL has ?advanced=1.
 *
 * Loaded from a <script defer> tag appended by the Python patcher to the
 * vendored Projector's index.html.  Fail-safe: if any selector is missing
 * (upstream rename), we log and keep running; the pane is still usable.
 */

(function () {
  'use strict';

  const CSS_HREF = 'cgraph-patch.css';
  const SIMPLE_DEFAULT = !(new URLSearchParams(location.search).get('advanced'));

  function log(...args) { console.log('[cgraph-patch]', ...args); }
  function warn(...args) { console.warn('[cgraph-patch]', ...args); }

  // --- shadow-root style injection ------------------------------------------
  // Each Polymer component under vz-projector-app has its own shadowRoot and
  // ignores styles from the outer document.  Fetch our CSS once and clone it
  // into every shadow root we care about.

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
    // Walk into shadow roots looking for selector.
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

  async function forceNightMode() {
    // Night-mode toggle is a paper-icon-button with icon="image:brightness-2"
    // inside the projector's toolbar.  The iconset string is stable in the
    // bundled Polymer 2 build we vendor.
    try {
      const btn = await waitFor(
        () => deepQuery(document, 'paper-icon-button[icon="image:brightness-2"]'),
        5000,
      );
      // The Projector tracks night-mode state internally; a click flips it.
      // It starts "day" so one click = dark.
      btn.click();
      log('night mode: on');
    } catch {
      warn('night-mode toggle not found (UI unchanged)');
    }
  }

  async function forceThreeD() {
    // In the PCA projections panel there's a checkbox for the Z component.
    // Its id inside vz-projector-projections-panel is commonly #z-dropdown
    // area; simpler to hunt for the Component #3 checkbox.
    try {
      const panel = await waitFor(
        () => deepQuery(document, 'vz-projector-projections-panel'),
        8000,
      );
      // Checkbox is a paper-checkbox toggling z-axis enablement.
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

    // First injection pass immediately; retry at 500ms, 1.5s, 3s to catch
    // components that mount late.
    walkAndInject(document);
    for (const ms of [500, 1500, 3000]) {
      setTimeout(() => walkAndInject(document), ms);
    }

    await forceNightMode();
    await forceThreeD();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
