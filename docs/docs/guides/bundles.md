# Using On-Demand Bundles

Don't index everything yourself. Use pre-built graphs for popular libraries.

## What is a Bundle?

A `.cgc` bundle is a snapshot of a graph. It allows you to "import" the knowledge of `flask`, `pandas`, or `react` without parsing it yourself.

## Registry (shipped)

The **bundle registry** is live: you can discover, search, and download bundles from the CLI without building graphs from source every time.

### CLI: list, search, download

```bash
# Everything available (add -v / --verbose for URLs)
kkg registry list

# Find bundles by name or keyword
kkg registry search react
kkg registry search "web framework"

# Download a bundle file (optionally --load / -l to import after download)
kkg registry download fastapi
kkg registry download fastapi -o ./bundles
```

`kkg load <name>` still **auto-downloads** from the registry when the bundle is not already local—see [QUICK_REFERENCE.md](../../QUICK_REFERENCE.md) for a full cheat sheet.

## How to use bundles

### 1. Search the registry

```bash
kkg registry search react
```

### 2. Load a bundle

```bash
kkg load react
```

*(This downloads on the order of megabytes instead of parsing tens of megabytes of source code.)*

### 3. Query it

Now your AI knows about the library's structure.

*"How does `useEffect` work internally in React?"* → The assistant can traverse the imported graph nodes (via MCP tools or `kkg query` / `kkg find` on the CLI).

## On-demand generation (GitHub Actions)

You can also generate a bundle **on demand** from the repository workflow: open **[Generate Bundle On-Demand](https://github.com/codeslp/keplerkg/actions/workflows/generate-bundle-on-demand.yml)**, click **Run workflow**, paste a GitHub repository URL, wait for the build to finish, then download the `.cgc` file and `kkg load` it locally.

## Requesting a bundle from the CLI

If a library is not listed yet, you can queue a request:

```bash
kkg registry request https://github.com/fastapi/fastapi
```

Build servers index the repo and publish it to the registry when ready (typically within minutes, depending on queue and repo size).
