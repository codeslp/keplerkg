# Complete CLI Command Reference

**All KeplerKG CLI Commands - Comprehensive List**

`kkg` is the primary CLI. `codegraphcontext` and the legacy `cgc` alias remain compatibility entry points when installed.

---

## 📋 **Table of Contents**

1. [Project Management](#1-project-management)
2. [Watching & Monitoring](#2-watching--monitoring)
3. [Code Analysis](#3-code-analysis)
4. [Discovery & Search](#4-discovery--search)
5. [Configuration & Setup](#5-configuration--setup)
6. [Bundle Management](#6-bundle-management)
7. [Bundle Registry](#7-bundle-registry)
8. [Utilities & Runtime](#8-utilities--runtime)
9. [Global Options](#global-options)
10. [Shortcuts](#shortcuts)

---

## 1. Project Management

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg index` | `[path]` `--force` | Index a repository. Default: current directory. Use `--force` to re-index. *(Alias: `kkg i`)* |
| `kkg list` | None | List all indexed repositories. *(Alias: `kkg ls`)* |
| `kkg delete` | `[path]` `--all` | Delete a repository from the graph. Use `--all` to wipe everything. *(Alias: `kkg rm`)* |
| `kkg stats` | `[path]` | Show indexing statistics for DB or specific repo. |
| `kkg clean` | None | Remove orphaned nodes and clean up the database. |
| `kkg add-package` | `<name> <lang>` | Manually add an external package node. |

---

## 2. Watching & Monitoring

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg watch` | `[path]` | Watch directory for changes and auto-reindex. *(Alias: `kkg w`)* |
| `kkg unwatch` | `<path>` | Stop watching a directory. |
| `kkg watching` | None | List all watched directories. |

---

## 3. Code Analysis

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg analyze calls` | `<func>` `--file` | Show outgoing calls: what does this function call? |
| `kkg analyze callers` | `<func>` `--file` | Show incoming calls: who calls this function? |
| `kkg analyze chain` | `<start> <end>` `--depth` | Find call path between two functions. Default depth: 5. |
| `kkg analyze deps` | `<module>` `--no-external` | Inspect dependencies (imports/importers) for a module. |
| `kkg analyze tree` | `<class>` `--file` | Visualize class inheritance hierarchy. |
| `kkg analyze complexity` | `[path]` `--threshold` `--limit` | List functions with high cyclomatic complexity. Default threshold: 10. |
| `kkg analyze dead-code` | `--exclude` | Find potentially unused functions (0 callers). |
| `kkg analyze overrides` | `<class>` `--file` | Show methods that override parent class methods. |
| `kkg analyze variable` | `<var_name>` `--file` | Analyze variable usage and assignments. |

---

## 4. Discovery & Search

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg find name` | `<name>` `--type` | Find code elements by exact name. |
| `kkg find pattern` | `<pattern>` `--case-sensitive` | Find elements using fuzzy substring matching. |
| `kkg find type` | `<type>` `--limit` | List all nodes of a specific type (function, class, module). |
| `kkg find variable` | `<name>` `--file` | Find variables by name across the codebase. |
| `kkg find content` | `<text>` `--case-sensitive` | Search for text content within code (docstrings, comments). |
| `kkg find decorator` | `<name>` | Find all functions/classes with a specific decorator. |
| `kkg find argument` | `<name>` | Find all functions that have a specific argument name. |

---

## 5. Configuration & Setup

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg mcp setup` | None | Configure IDE/MCP Client. Creates `mcp.json`. *(Alias: `kkg m`)* |
| `kkg mcp start` | None | Start the MCP Server (used by IDEs). |
| `kkg mcp tools` | None | List all available MCP tools. |
| `kkg neo4j setup` | None | Configure Neo4j database connection. *(Alias: `kkg n`)* |
| `kkg config show` | None | Display current configuration values. |
| `kkg config set` | `<key> <value>` | Set a configuration value. |
| `kkg config reset` | None | Reset configuration to defaults. |
| `kkg config db` | `<backend>` | Quick switch between `neo4j` and `falkordb`. |

---

## 6. Bundle Management

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg bundle export` | `<output.cgc>` `--repo` `--no-stats` | Export graph to portable .cgc bundle. *(Alias: `kkg export`)* |
| `kkg bundle import` | `<bundle.cgc>` `--clear` | Import a .cgc bundle into database. |
| `kkg bundle load` | `<name>` `--clear` | Load bundle (downloads from registry if needed). *(Alias: `kkg load`)* |

---

## 7. Bundle Registry

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg registry list` | `--verbose` `-v` `--unique` `-u` | List all available bundles in the registry. Use `--unique` to show only most recent version per package. |
| `kkg registry search` | `<query>` | Search for bundles by name/repo/description. |
| `kkg registry download` | `<name>` `--output` `-o` `--load` `-l` | Download bundle from registry. |
| `kkg registry request` | `<github-url>` `--wait` | Request on-demand bundle generation. |

---

## 8. Utilities & Runtime

| Command | Arguments | Description |
|---------|-----------|-------------|
| `kkg doctor` | None | Run system diagnostics (DB, dependencies, permissions). |
| `kkg visualize` | `[query]` | Generate link to Neo4j Browser. *(Alias: `kkg v`)* |
| `kkg query` | `<query>` | Execute raw Cypher query against DB. |
| `kkg help` | None | Show main help message with all commands. |
| `kkg version` | None | Show application version. |
| `kkg start` | None | **Deprecated**. Use `kkg mcp start` instead. |

---

## Global Options

These work with any command:

| Option | Short | Description |
|--------|-------|-------------|
| `--database` | `-db` | Override database backend (`falkordb` or `neo4j`). |
| `--visual` / `--viz` | `-V` | Show results as interactive graph visualization. |
| `--help` | `-h` | Show help for any command. |
| `--version` | `-v` | Show version (root level only). |

---

## Shortcuts

Quick aliases for common commands:

| Shortcut | Full Command | Description |
|----------|--------------|-------------|
| `kkg m` | `kkg mcp setup` | MCP client setup |
| `kkg n` | `kkg neo4j setup` | Neo4j database setup |
| `kkg i` | `kkg index` | Index repository |
| `kkg ls` | `kkg list` | List repositories |
| `kkg rm` | `kkg delete` | Delete repository |
| `kkg v` | `kkg visualize` | Visualize graph |
| `kkg w` | `kkg watch` | Watch directory |
| `kkg export` | `kkg bundle export` | Export bundle |
| `kkg load` | `kkg bundle load` | Load bundle |

---

## Quick Examples

### Basic Workflow
```bash
kkg index .                          # Index current directory
kkg list                             # List indexed repos
kkg find name MyFunction             # Find a function
kkg analyze callers MyFunction       # See who calls it
```

### Bundle Workflow
```bash
kkg bundle export my-project.cgc --repo .  # Export graph
kkg registry list                          # Browse bundles
kkg load flask                             # Download & load
kkg registry search web                    # Search bundles
```

### Advanced Analysis
```bash
kkg analyze complexity --threshold 15      # Find complex code
kkg analyze chain start end --depth 10     # Find call path
kkg analyze tree MyClass --visual          # Visualize in browser
```

### Configuration
```bash
kkg config show                      # View config
kkg config set DEFAULT_DATABASE neo4j  # Switch to Neo4j
kkg config db falkordb               # Quick switch to FalkorDB
kkg doctor                           # Check system health
```

---

## Command Count Summary

**Total Commands: 55**

- Project Management: 6 commands
- Watching & Monitoring: 3 commands
- Code Analysis: 9 commands (added 2 new)
- Discovery & Search: 7 commands (added 4 new)
- Configuration & Setup: 8 commands
- Bundle Management: 3 commands
- Bundle Registry: 4 commands
- Utilities & Runtime: 6 commands
- Global Options: 4 options
- Shortcuts: 9 aliases

---

**All commands documented!** ✅

**Newly Added Commands:**
- `kkg analyze overrides` - Show method overrides
- `kkg analyze variable` - Analyze variable usage
- `kkg find variable` - Find variables by name
- `kkg find content` - Search text in code
- `kkg find decorator` - Find by decorator
- `kkg find argument` - Find by argument name
- Hidden: `kkg cypher` (deprecated, use `kkg query`)
