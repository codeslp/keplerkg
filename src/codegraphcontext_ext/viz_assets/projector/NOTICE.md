# TensorFlow Embedding Projector (standalone)

Files `index.html` and `favicon.png` in this directory are a vendored copy of
the TensorFlow Embedding Projector standalone build.

- **Upstream**: https://github.com/tensorflow/embedding-projector-standalone
- **Original license**: Apache License, Version 2.0
- **Copyright**: 2016-2017 The TensorFlow Authors

## Modifications

Three localized changes to `index.html` vs upstream, plus two companion
asset files (`cgraph-patch.css`, `cgraph-patch.js`) that are cgraph's own
work (not derived from upstream):

1. The attribute `projector-config-json-path`, changed from
   `"oss_data/oss_demo_projector_config.json"` to
   `"cgraph_data/projector_config.json"`, so that `cgc viz-projector` can
   write its tensor + metadata next to the served app without shipping any
   of the upstream demo datasets (word2vec, MNIST, iris, GNMT) — those
   totaled ~145 MB and are not included.

2. `hasWebGLSupport()` no longer gates on the bundled `weblas` matrix
   library (`typeof weblas !== "undefined"`).  That extra gate made the
   Projector throw "WebGL not enabled" on browsers where WebGL is actually
   available but `weblas` fails to attach to the global scope (common on
   modern Chrome).  `weblas` is only used for t-SNE acceleration; PCA +
   UMAP work without it via the bundled TensorFlow.js path.  The patched
   function also logs its decision to `console.log("[cgraph-patch] ...")`
   so any remaining failure is diagnosable.

3. Added `<link rel="stylesheet" href="cgraph-patch.css">` and
   `<script src="cgraph-patch.js" defer>` just before `<body>`.  Those
   two files (cgraph's own work, not derived from the Projector) apply:
     - a cgraph-dark palette via CSS custom properties,
     - a `.cgraph-simple` body class that hides advanced controls
       (Load/Publish/Download buttons, Edit-by, Tag-selection-as,
       Spherize, Checkpoint / Metadata paths, PCA component axis pickers,
       "PCA is approximate" warning, custom-projection tab);
       pass `?advanced=1` to the Projector URL to keep them visible,
     - auto-click of the night-mode toggle,
     - auto-enable of the Z axis so the view opens in 3D by default.

## License

A full copy of the Apache License 2.0 is available at
http://www.apache.org/licenses/LICENSE-2.0.

Distribution of this vendored copy within cgraph does not imply endorsement
by the TensorFlow project.  cgraph itself is MIT-licensed; the Apache 2.0
terms apply only to the two files in this directory.
