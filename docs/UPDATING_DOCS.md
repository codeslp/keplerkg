# 📚 Documentation Update Guide

This guide explains how to update the KeplerKG documentation and landing pages.

## 🏗️ Documentation Structure

KeplerKG currently has **three** repo-managed web surfaces:

### 1. **MkDocs Documentation** (Published Docs)
- **Location**: `/docs/`
- **Build Output**: `/docs/site/`
- **Purpose**: Technical documentation, guides, API reference
- **Technology**: MkDocs with Material theme
- **Deployment**: `.github/workflows/deploy-root-docs.yml` builds `docs/site/` and publishes it to `gh-pages`

### 2. **Static Landing Page** (Standalone HTML)
- **Location**: `/site/`
- **Purpose**: Lightweight marketing/research landing page
- **Technology**: Static HTML/CSS/JS
- **Deployment**: Not auto-deployed by the current GitHub Actions workflow

### 3. **React Marketing App** (Alternate Frontend)
- **Location**: `/website/`
- **Purpose**: React/Vite landing page and experiments
- **Technology**: React + Vite + TypeScript
- **Deployment**: Not auto-deployed by the current GitHub Actions workflow

---

## 📝 Updating MkDocs Documentation

### Quick Start

```bash
cd docs
pip install mkdocs-material
mkdocs serve  # Preview at http://127.0.0.1:8000
```

### File Structure

```
docs/
├── mkdocs.yml           # Configuration & navigation
├── docs/                # Markdown content
│   ├── index.md
│   ├── getting-started/
│   │   └── installation.md
│   ├── cookbook.md
│   ├── deployment/      # Deployment guides
│   │   ├── README.md
│   │   ├── DOCKER_README.md
│   │   └── ...
│   └── ...
└── ...
```

**Note:** Deployment pages live under **`docs/docs/deployment/`**. They are linked from the **`Deployment`** section in **`mkdocs.yml`** `nav`; if you add a new deployment markdown file, remember to add a `nav` entry or it will not appear in the site sidebar.

### Adding New Pages

1. **Create a markdown file** in `docs/docs/`:
   ```bash
   touch docs/docs/my-new-page.md
   ```

2. **Add to navigation** in `docs/mkdocs.yml`:
   ```yaml
   nav:
     - My New Page: my-new-page.md
   ```

3. **Preview changes**:
   ```bash
   cd docs && mkdocs serve
   ```

### Building & Deploying

```bash
cd docs
mkdocs build --clean  # Generates static site in docs/site/
```

Pushes to `main` trigger `.github/workflows/deploy-root-docs.yml`, which publishes `docs/site/` to `gh-pages`. If you need a manual preview, run `mkdocs serve` locally instead of editing `docs/site/` by hand.

---

## 🎨 Updating Static Landing Page

### Quick Start

```bash
cd site
python3 -m http.server 8000
# Preview at http://127.0.0.1:8000
```

### Key Files to Edit

- **`site/index.html`** - Main landing page copy and layout
- **`site/img/`** - Images used by the landing page

### Deployment Notes

- There is **no repo-managed auto-deploy** for `site/` today.
- `scripts/sync-to-kkg.sh` copies `site/` into the sibling `keplerkg` repo working tree, but that does **not** update the live GitHub Pages branch by itself.
- To prepare the live landing page payload for `keplerkg/gh-pages`, run `bash scripts/publish-site-to-kkg-gh-pages.sh`.
- Research notes under `research/` are **not** auto-rendered into the landing page. If a note should appear publicly, add a condensed version to `site/index.html`.

---

## 🎨 Updating React Marketing App

### Quick Start

```bash
cd website
npm install
npm run dev  # Preview at http://localhost:5173
```

### Key Files to Edit

- **`src/pages/Index.tsx`** - Main landing page
- **`src/components/HeroSection.tsx`** - Hero banner
- **`src/components/FeaturesSection.tsx`** - Features list
- **`src/components/InstallationSection.tsx`** - Installation guide
- **`src/components/Footer.tsx`** - Footer links (just updated!)
- **`src/components/CookbookSection.tsx`** - Code examples

### Building for Production

```bash
cd website
npm run build  # Generates dist/ folder
```

### Deployment Notes

- There is **no repo-managed auto-deploy** for `website/` today.
- Treat it as a local/build artifact unless your branch also wires up an external host.

---

## ✅ Publishing Checklist

1. **For docs changes**:
   ```bash
   cd docs
   mkdocs serve
   ```
   Check navigation, links, and search locally.

2. **For `site/` changes**:
   ```bash
   cd site
   python3 -m http.server 8000
   ```
   Check the landing page in a browser.
   If the change should go live on the KeplerKG website, also run:
   ```bash
   cd ..
   bash scripts/publish-site-to-kkg-gh-pages.sh
   ```

3. **For `website/` changes**:
   ```bash
   cd website
   npm run build
   ```
   Confirm the React build still passes.

---

## 🔗 Useful Links

- **MkDocs Documentation**: https://www.mkdocs.org/
- **Material Theme**: https://squidfunk.github.io/mkdocs-material/
- **Docs Deploy Workflow**: `.github/workflows/deploy-root-docs.yml`

---

## 💡 Tips

- **MkDocs** uses relative paths from `docs/docs/` directory
- Use paths under `docs/docs/deployment/` for deployment markdown; link from other pages with relative paths (e.g. `deployment/README.md` from a page in `docs/docs/`).
- Do not hand-edit `docs/site/`; it is generated output from MkDocs.
- `site/` and `website/` both require separate hosting if you want a live deployment.
- TypeScript errors in `website/` are normal without `npm install`
