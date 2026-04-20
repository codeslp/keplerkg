# 🚀 Quick Reference - Bundle Registry Commands

## 📋 **CLI Commands Cheat Sheet**

Examples below use `kkg`, the primary CLI entry point.

### **List Available Bundles**
```bash
kkg registry list                    # Show all bundles
kkg registry list --verbose          # Show with download URLs
kkg registry list -v                 # Short form
```

### **Search for Bundles**
```bash
kkg registry search flask            # Search by name
kkg registry search http             # Search by keyword
kkg registry search "web framework"  # Search phrase
```

### **Download Bundles**
```bash
kkg registry download flask          # Download to current dir
kkg registry download flask -o ./bundles  # Download to specific dir
kkg registry download flask --load   # Download and auto-load
kkg registry download flask -l       # Short form
```

### **Load Bundles (Auto-Download)**
```bash
kkg load flask                       # Auto-downloads if needed
kkg load httpx --clear               # Clear DB before loading
kkg load /path/to/bundle.cgc         # Load from local file
```

### **Request Custom Bundle**
```bash
kkg registry request https://github.com/encode/httpx
kkg registry request https://github.com/pallets/flask
```

---

## 🌐 **Self-Service Bundle Requests**

### **Run the GitHub Workflow**
- Visit: https://github.com/codeslp/keplerkg/actions/workflows/generate-bundle-on-demand.yml
- Click: "Run workflow"
- Enter: GitHub repository URL
- Wait: 5-10 minutes
- Download: When ready

---

## 📊 **Common Workflows**

### **Quick Start: Load a Bundle**
```bash
# One command - downloads and loads
kkg load flask
```

### **Browse Before Downloading**
```bash
# See what's available
kkg registry list

# Search for something specific
kkg registry search web

# Download and load
kkg load fastapi
```

### **Generate Custom Bundle**
```bash
# Request generation
kkg registry request https://github.com/psf/requests

# Wait 5-10 minutes, then:
kkg load requests
```

---

## 🔍 **Explore Loaded Bundles**

### **View Repository Info**
```bash
kkg list                             # List all loaded repos
kkg stats                            # Show database stats
kkg stats /path/to/repo              # Stats for specific repo
```

### **Query the Graph**
```bash
# Find all classes
kkg query "MATCH (c:Class) RETURN c.name LIMIT 20"

# Find all functions
kkg query "MATCH (f:Function) RETURN f.name LIMIT 20"

# Search for specific code
kkg find name Flask
kkg find name render_template
```

---

## 📦 **Available Bundles**

Current bundles in registry:
- **flask** - Lightweight web framework (316K)
- **httpx** - HTTP client (268K)
- **fastapi** - Modern API framework (796K)
- **requests** - HTTP library (224K)
- **StatWrap** - Statistics wrapper (380K)

*Use `kkg registry list` for latest*

---

## 🆘 **Help Commands**

```bash
kkg --help                           # General help
kkg registry --help                  # Registry commands help
kkg registry list --help             # Specific command help
kkg registry download --help
kkg registry search --help
kkg registry request --help
```

---

## 🎯 **Examples**

### **Example 1: Quick Load**
```bash
$ kkg load flask
Bundle 'flask' not found locally.
Attempting to download from registry...
✓ Downloaded successfully: flask-main-2579ce9.cgc
✅ Successfully imported flask-main-2579ce9.cgc
   Nodes: 4,534 | Edges: 9,218
```

### **Example 2: Search and Download**
```bash
$ kkg registry search http
Found 1 matching bundle(s)
┃ httpx │ encode/httpx │ main │ 268K ┃

$ kkg load httpx
✓ Downloaded and loaded successfully!
```

### **Example 3: Browse All**
```bash
$ kkg registry list
Available Bundles
┃ flask    │ pallets/flask   │ 316K ┃
┃ httpx    │ encode/httpx    │ 268K ┃
┃ fastapi  │ fastapi/fastapi │ 796K ┃
┃ requests │ psf/requests    │ 224K ┃
```

---

## 💡 **Tips**

1. **Auto-Download:** `kkg load` automatically downloads from registry if not found locally
2. **Search First:** Use `kkg registry search` to find bundles before downloading
3. **Verbose Mode:** Add `-v` to see download URLs
4. **Clear Database:** Use `--clear` flag to replace existing data
5. **Local Files:** `kkg load` works with both bundle names and file paths

---

## 🔗 **Resources**

- **Documentation:** `/docs/ON_DEMAND_BUNDLES.md`
- **CLI Guide:** `/CLI_REGISTRY_COMMANDS.md`
- **GitHub Actions:** https://github.com/codeslp/keplerkg/actions/workflows/generate-bundle-on-demand.yml
- **GitHub:** https://github.com/codeslp/keplerkg

---

## ✅ **Quick Checklist**

- [ ] List available bundles: `kkg registry list`
- [ ] Search for a bundle: `kkg registry search <query>`
- [ ] Download a bundle: `kkg load <name>`
- [ ] View loaded repos: `kkg list`
- [ ] Query the graph: `kkg query "<query>"`

---

**Save this file for quick reference!** 📌

All commands are ready to use. Just run them in your terminal! 🚀
