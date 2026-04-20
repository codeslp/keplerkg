# CLI: Indexing & Management

These commands are the foundation of KeplerKG. They allow you to add, remove, and monitor the code repositories in your graph.

## `kkg index`

Adds a code repository to the graph database. This is the first step for any project.

!!! info "Excluding Files (.cgcignore)"
    Want to skip specific files or folders? KeplerKG supports a `.cgcignore` file.
    **[📄 Read the .cgcignore Guide](cgcignore.md)**

**Usage:**
```bash
kkg index [path] [options]
```

**Common Options:**

*   `path`: The folder to index (default: current directory).
*   `--force`: Re-index from scratch, even if it looks unchanged.

**Example:**
```bash
# Index the current folder
$ kkg index .

# Index a specific project
$ kkg index /home/user/projects/backend-api
```

---

## `kkg list`

Shows all repositories currently stored in your graph database.

**Usage:**
```bash
kkg list
```

**Example Output:**
```text
Indexed Repositories:
1. /home/user/projects/backend-api (Nodes: 1205)
2. /home/user/projects/frontend-ui (Nodes: 850)
```

---

## `kkg watch`

Starts a real-time monitor. If you edit a file, the graph updates instantly.

!!! warning "Foreground Process"
    This command runs in the foreground. Open a new terminal tab to keep it running.

**Usage:**
```bash
kkg watch [path]
```

**Example:**
```bash
$ kkg watch .
[INFO] Watching /home/user/projects/backend-api for changes...
[INFO] Detected change in users/models.py. Re-indexing...
```

---

## `kkg delete`

Removes a repository from the database. This does *not* delete your actual files, only the graph index.

**Usage:**
```bash
kkg delete [path] [options]
```

**Common Options:**

*   `--all`: Dangerous. Wipes the entire database.

---

## `kkg bundle` Commands

Tools for managing portable graph snapshots (`.cgc` files).

### `kkg bundle export`
Save your graph to a file. Useful for sharing context with team members or loading into a production read-only instance.
```bash
kkg bundle export my-graph.cgc --repo /path/to/repo
```

### `kkg bundle load`
Download and install a popular library bundle from our registry.
*(Alias: `kkg load`)*

```bash
kkg load flask
```

### `kkg registry`
Search for available pre-indexed bundles in the cloud registry.
**Usage:** `kkg registry [query]`

```bash
# List top bundles
kkg registry

# Search for a specific package
kkg registry pandas
```
