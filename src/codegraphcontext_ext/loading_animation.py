"""Orbiting knowledge-graph loading animation for KeplerKG.

Returns self-contained CSS + HTML + JS snippets that render a 3D
rotating constellation of graph nodes with traveling edge particles,
depth-based parallax, and a breathing central hub.  Designed to overlay
each dashboard pane while its content loads, then fade out smoothly.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# CSS — injected into the dashboard <style> block
# ---------------------------------------------------------------------------

LOADING_CSS = """
  /* ── KeplerKG loading overlay ─────────────────────────────────── */
  .kkg-loader {
    position: absolute; inset: 0; z-index: 50;
    display: flex; flex-direction: column;
    align-items: center; justify-content: center;
    background: #0d1117;
    transition: opacity 0.6s ease-out, visibility 0.6s;
  }
  .kkg-loader.fade-out {
    opacity: 0; visibility: hidden; pointer-events: none;
  }
  .kkg-loader canvas {
    width: min(420px, 60vw); height: min(420px, 60vw);
  }
  .kkg-loader__label {
    margin-top: 20px;
    font-family: "Antic Didone", "Antic Slab", Georgia, serif;
    font-size: 14px; color: #8b949e; letter-spacing: 0.15em;
    text-transform: uppercase;
    animation: kkg-pulse-text 2.4s ease-in-out infinite;
  }
  @keyframes kkg-pulse-text {
    0%, 100% { opacity: 0.5; }
    50%      { opacity: 1; }
  }
"""

# ---------------------------------------------------------------------------
# HTML — one overlay per pane; inserted inside each .pane div
# ---------------------------------------------------------------------------

def loader_html(pane_id: str, label: str = "Resolving graph\u2026") -> str:
    """Return an overlay div for a single pane."""
    canvas_id = f"kkg-loader-canvas-{pane_id}"
    return (
        f'<div class="kkg-loader" id="kkg-loader-{pane_id}">'
        f'<canvas id="{canvas_id}" width="840" height="840"></canvas>'
        f'<div class="kkg-loader__label">{label}</div>'
        f"</div>"
    )


# ---------------------------------------------------------------------------
# JS — the Canvas animation engine  (one IIFE, drives all loader canvases)
# ---------------------------------------------------------------------------

LOADING_JS = r"""
(function () {
  "use strict";

  /* ── palette ─────────────────────────────────────────────────── */
  var COLORS = [
    "#7ee787",   /* green  — contains */
    "#f778ba",   /* pink   — calls    */
    "#d2a8ff",   /* purple — inherits */
    "#58a6ff",   /* blue   — imports  */
    "#79c0ff",   /* light blue        */
  ];
  var EDGE_COLOR = "rgba(48,54,61,0.6)";
  var PARTICLE_COLORS = ["#7ee787","#58a6ff","#f778ba","#d2a8ff"];
  var BG = "#0d1117";
  var TAU = Math.PI * 2;

  /* ── seeded PRNG (same as BackgroundTree) ────────────────────── */
  function prng(seed) {
    var s = seed;
    return function () {
      s = (s * 1103515245 + 12345) & 0x7fffffff;
      return s / 0x7fffffff;
    };
  }

  /* ── build the constellation ─────────────────────────────────── */
  function buildGraph(rand) {
    var nodes = [];
    var edges = [];

    /* central hub */
    nodes.push({ x:0, y:0, z:0, r:4.5, color: COLORS[0], hub: true,
                  orbit: 0, phase: 0, speed: 0, tilt: 0, dist: 0 });

    /* 3 orbital shells */
    var shells = [
      { count: 7,  dist: 0.35, speed: 0.0004, tilt: 0.42, rMin: 2.0, rMax: 3.0 },
      { count: 9,  dist: 0.62, speed:-0.00025, tilt:-0.28, rMin: 1.6, rMax: 2.5 },
      { count: 11, dist: 0.92, speed: 0.00015, tilt: 0.55, rMin: 1.2, rMax: 2.0 },
    ];

    for (var si = 0; si < shells.length; si++) {
      var sh = shells[si];
      for (var ni = 0; ni < sh.count; ni++) {
        var phase = (TAU / sh.count) * ni + rand() * 0.5;
        var yOff  = (rand() - 0.5) * 0.18;
        nodes.push({
          x: 0, y: 0, z: 0,
          r: sh.rMin + rand() * (sh.rMax - sh.rMin),
          color: COLORS[Math.floor(rand() * COLORS.length)],
          hub: false,
          orbit: si + 1,
          phase: phase,
          speed: sh.speed * (0.8 + rand() * 0.4),
          tilt:  sh.tilt + (rand() - 0.5) * 0.12,
          dist:  sh.dist + (rand() - 0.5) * 0.06,
          yOff:  yOff,
        });
      }
    }

    /* 8 scattered field nodes */
    for (var fi = 0; fi < 8; fi++) {
      var theta = rand() * TAU;
      var phi   = Math.acos(2 * rand() - 1);
      var rd    = 0.3 + rand() * 0.85;
      nodes.push({
        x: rd * Math.sin(phi) * Math.cos(theta),
        y: rd * Math.sin(phi) * Math.sin(theta),
        z: rd * Math.cos(phi),
        r: 1.0 + rand() * 1.2,
        color: COLORS[Math.floor(rand() * COLORS.length)],
        hub: false, orbit: -1, phase: 0, speed: 0, tilt: 0, dist: 0,
        field: true,
      });
    }

    /* edges: hub ↔ shell-1, shell-1 ↔ shell-2, shell-2 ↔ shell-3, nearest field */
    var shellStart = [1, 1 + shells[0].count, 1 + shells[0].count + shells[1].count];
    var shellEnd   = [shellStart[1], shellStart[2], shellStart[2] + shells[2].count];

    /* hub → inner shell */
    for (var i = shellStart[0]; i < shellEnd[0]; i++) {
      edges.push({ a: 0, b: i });
    }
    /* inter-shell */
    for (var s = 0; s < shells.length - 1; s++) {
      for (var i = shellStart[s]; i < shellEnd[s]; i++) {
        var best = shellStart[s+1];
        var bestD = 1e9;
        for (var j = shellStart[s+1]; j < shellEnd[s+1]; j++) {
          var d = Math.abs(nodes[i].phase - nodes[j].phase);
          if (d < bestD) { bestD = d; best = j; }
        }
        edges.push({ a: i, b: best });
        /* second nearest with 40% chance */
        if (rand() < 0.4) {
          var second = shellStart[s+1];
          var secD = 1e9;
          for (var j = shellStart[s+1]; j < shellEnd[s+1]; j++) {
            if (j === best) continue;
            var d = Math.abs(nodes[i].phase - nodes[j].phase);
            if (d < secD) { secD = d; second = j; }
          }
          edges.push({ a: i, b: second });
        }
      }
    }
    /* field → nearest non-field */
    var fieldStart = shellEnd[2];
    for (var i = fieldStart; i < nodes.length; i++) {
      var best = 0, bestD = 1e9;
      for (var j = 0; j < fieldStart; j++) {
        var dx = nodes[i].x - nodes[j].x;
        var dy = nodes[i].y - nodes[j].y;
        var dz = nodes[i].z - nodes[j].z;
        var d  = dx*dx + dy*dy + dz*dz;
        if (d < bestD) { bestD = d; best = j; }
      }
      edges.push({ a: i, b: best });
    }

    /* dedup */
    var seen = {};
    var unique = [];
    for (var ei = 0; ei < edges.length; ei++) {
      var lo = Math.min(edges[ei].a, edges[ei].b);
      var hi = Math.max(edges[ei].a, edges[ei].b);
      var key = lo + "-" + hi;
      if (!seen[key]) { seen[key] = true; unique.push(edges[ei]); }
    }

    return { nodes: nodes, edges: unique };
  }

  /* ── particles that travel along edges ───────────────────────── */
  function spawnParticles(edges, rand) {
    var particles = [];
    for (var i = 0; i < edges.length; i++) {
      if (rand() < 0.55) {
        particles.push({
          edge: i,
          t: rand(),
          speed: 0.0003 + rand() * 0.0006,
          color: PARTICLE_COLORS[Math.floor(rand() * PARTICLE_COLORS.length)],
          size: 1.0 + rand() * 1.5,
          forward: rand() < 0.5,
        });
      }
    }
    return particles;
  }

  /* ── 3D → 2D projection with perspective ─────────────────────── */
  function project(node, cx, cy, scale, globalAngle) {
    var x = node.x, y = node.y, z = node.z;

    /* global Y-axis rotation */
    var cos = Math.cos(globalAngle), sin = Math.sin(globalAngle);
    var rx = x * cos - z * sin;
    var rz = x * sin + z * cos;
    x = rx; z = rz;

    /* gentle X tilt (15°) for depth feel */
    var tilt = 0.26;
    var ct = Math.cos(tilt), st = Math.sin(tilt);
    var ry = y * ct - z * st;
    rz      = y * st + z * ct;
    y = ry; z = rz;

    var depth = (z + 1.5) / 3.0;       /* 0..1 */
    var persp = 0.6 + depth * 0.55;
    return {
      sx: cx + x * scale * persp,
      sy: cy + y * scale * persp,
      depth: depth,
      radius: node.r * (0.4 + depth * 0.6),
    };
  }

  /* ── per-canvas animation loop ───────────────────────────────── */
  function initCanvas(canvas) {
    var ctx = canvas.getContext("2d");
    if (!ctx) return;

    var rand = prng(7919);   /* fixed seed for deterministic layout */
    var graph = buildGraph(rand);
    var particles = spawnParticles(graph.edges, prng(1337));
    var nodes = graph.nodes;
    var edges = graph.edges;
    var start = 0;
    var raf = null;

    function updateOrbits(now) {
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.orbit <= 0 && !n.field) continue;
        if (n.field) continue;  /* field nodes stay static in world space */

        var angle = n.phase + n.speed * now;
        var ct = Math.cos(n.tilt), st = Math.sin(n.tilt);
        var cx = Math.cos(angle) * n.dist;
        var cz = Math.sin(angle) * n.dist;
        n.x = cx;
        n.y = (n.yOff || 0) + cz * st;
        n.z = cz * ct;
      }
    }

    function hexToRgb(hex) {
      var v = parseInt(hex.slice(1), 16);
      return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
    }

    function render(now) {
      if (!start) start = now;
      var elapsed = now - start;

      var rect = canvas.parentElement.getBoundingClientRect();
      var size = Math.min(rect.width, rect.height, 420);
      var dpr  = Math.min(window.devicePixelRatio || 1, 2);
      var px   = Math.floor(size * dpr);
      if (canvas.width !== px || canvas.height !== px) {
        canvas.width = px; canvas.height = px;
        canvas.style.width  = size + "px";
        canvas.style.height = size + "px";
      }

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size, size);

      var cx = size / 2, cy = size / 2;
      var scale = size * 0.42;
      var globalAngle = elapsed * 0.00018;

      updateOrbits(elapsed);

      /* project all nodes */
      var proj = [];
      for (var i = 0; i < nodes.length; i++) {
        proj.push(project(nodes[i], cx, cy, scale, globalAngle));
      }

      /* ── draw orbital ring traces (subtle) ───────────────────── */
      for (var si = 0; si < 3; si++) {
        var dist = [0.35, 0.62, 0.92][si];
        var tilt = [0.42, -0.28, 0.55][si];
        ctx.strokeStyle = "rgba(48,54,61,0.18)";
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        for (var a = 0; a <= 64; a++) {
          var ang = (TAU / 64) * a;
          var ox = Math.cos(ang) * dist;
          var ct = Math.cos(tilt), st = Math.sin(tilt);
          var oz = Math.sin(ang) * dist;
          var oy = oz * st;
          oz = oz * ct;
          var p = project({ x: ox, y: oy, z: oz, r: 0 }, cx, cy, scale, globalAngle);
          if (a === 0) ctx.moveTo(p.sx, p.sy);
          else ctx.lineTo(p.sx, p.sy);
        }
        ctx.stroke();
      }

      /* ── draw edges ──────────────────────────────────────────── */
      ctx.lineCap = "round";
      for (var i = 0; i < edges.length; i++) {
        var a = proj[edges[i].a], b = proj[edges[i].b];
        var d = (a.depth + b.depth) / 2;
        ctx.strokeStyle = "rgba(48,54,61," + (0.15 + d * 0.35) + ")";
        ctx.lineWidth = 0.4 + d * 0.8;
        ctx.beginPath();
        ctx.moveTo(a.sx, a.sy);
        ctx.lineTo(b.sx, b.sy);
        ctx.stroke();
      }

      /* ── draw particles ──────────────────────────────────────── */
      for (var i = 0; i < particles.length; i++) {
        var p = particles[i];
        p.t += (p.forward ? p.speed : -p.speed) * 16.67;
        if (p.t > 1) p.t -= 1;
        if (p.t < 0) p.t += 1;

        var ea = proj[edges[p.edge].a];
        var eb = proj[edges[p.edge].b];
        var px = ea.sx + (eb.sx - ea.sx) * p.t;
        var py = ea.sy + (eb.sy - ea.sy) * p.t;
        var d  = ea.depth + (eb.depth - ea.depth) * p.t;
        var rgb = hexToRgb(p.color);

        ctx.shadowBlur = 6 + d * 6;
        ctx.shadowColor = "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + (0.4 + d * 0.4) + ")";
        ctx.fillStyle = "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + (0.5 + d * 0.5) + ")";
        ctx.beginPath();
        ctx.arc(px, py, p.size * (0.4 + d * 0.6), 0, TAU);
        ctx.fill();
        ctx.shadowBlur = 0;
      }

      /* ── draw nodes (sorted back-to-front) ───────────────────── */
      var order = [];
      for (var i = 0; i < proj.length; i++) order.push(i);
      order.sort(function (a, b) { return proj[a].depth - proj[b].depth; });

      for (var oi = 0; oi < order.length; oi++) {
        var idx = order[oi];
        var nd  = nodes[idx];
        var pr  = proj[idx];
        var d   = pr.depth;
        var rgb = hexToRgb(nd.color);

        var rad = pr.radius;
        /* hub breathing */
        if (nd.hub) {
          rad *= 1.0 + 0.18 * Math.sin(elapsed * 0.0015);
        }

        /* glow */
        ctx.shadowBlur = (nd.hub ? 18 : 8) + d * 10;
        ctx.shadowColor = "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + (0.25 + d * 0.3) + ")";

        /* filled circle */
        ctx.fillStyle = "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + (0.35 + d * 0.55) + ")";
        ctx.beginPath();
        ctx.arc(pr.sx, pr.sy, rad, 0, TAU);
        ctx.fill();

        /* bright core dot */
        ctx.fillStyle = "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + (0.7 + d * 0.3) + ")";
        ctx.beginPath();
        ctx.arc(pr.sx, pr.sy, rad * 0.45, 0, TAU);
        ctx.fill();

        /* corona ring */
        ctx.shadowBlur = 0;
        ctx.strokeStyle = "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + (0.15 + d * 0.2) + ")";
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.arc(pr.sx, pr.sy, rad + 1.2, 0, TAU);
        ctx.stroke();
      }

      raf = requestAnimationFrame(render);
    }

    raf = requestAnimationFrame(render);

    /* expose a stop handle so the overlay fade-out can kill the loop */
    canvas._kkgStop = function () {
      if (raf) { cancelAnimationFrame(raf); raf = null; }
    };
  }

  /* ── boot all loader canvases present in the DOM ─────────────── */
  var canvases = document.querySelectorAll(".kkg-loader canvas");
  for (var i = 0; i < canvases.length; i++) {
    initCanvas(canvases[i]);
  }

  /* ── fade-out helper: call window._kkgLoaded(paneId) ─────────── */
  window._kkgLoaded = function (paneId) {
    var overlay = document.getElementById("kkg-loader-" + paneId);
    if (!overlay) return;
    overlay.classList.add("fade-out");
    var cvs = overlay.querySelector("canvas");
    /* stop the RAF loop after the transition ends to save CPU */
    setTimeout(function () {
      if (cvs && cvs._kkgStop) cvs._kkgStop();
    }, 700);
  };
})();
"""
