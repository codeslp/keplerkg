# Comprehensive CLI Reference

This page lists **every single command** available in KeplerKG. Examples use the primary `kkg` CLI.

## Indexing & Management

| Command | Description | Full Details |
| :--- | :--- | :--- |
| **`kkg index`** | Adds a directory to the code graph. | [details](cli_indexing.md#kkg-index) |
| **`kkg list`** | Lists all indexed repositories. | [details](cli_indexing.md#kkg-list) |
| **`kkg delete`** | Removes a repository from the graph. | [details](cli_indexing.md#kkg-delete) |
| **`kkg watch`** | Monitors a directory for real-time updates. | [details](cli_indexing.md#kkg-watch) |
| **`kkg clean`** | Removes orphaned nodes from the DB. | - |
| **`kkg stats`** | Show node count statistics. | - |

## Code Analysis

| Command | Description | Full Details |
| :--- | :--- | :--- |
| **`kkg analyze callers`** | Show what functions call X. | [details](cli_analysis.md#analyze-callers) |
| **`kkg analyze calls`** | Show what functions X calls (callees). | [details](cli_analysis.md#analyze-calls) |
| **`kkg analyze chain`** | Show path between function A and B. | [details](cli_analysis.md#analyze-chain) |
| **`kkg analyze deps`** | Show imports/dependencies for a module. | [details](cli_analysis.md#analyze-deps) |
| **`kkg analyze tree`** | Show class inheritance hierarchy. | [details](cli_analysis.md#analyze-tree) |
| **`kkg analyze complexity`** | Find complex functions (Cyclomatic). | [details](cli_analysis.md#analyze-complexity) |
| **`kkg analyze dead-code`** | Find unused functions. | [details](cli_analysis.md#analyze-dead-code) |
| **`kkg analyze overrides`** | Find method overrides in subclasses. | [details](cli_analysis.md#analyze-overrides) |
| **`kkg analyze variable`** | Find variable usage across files. | [details](cli_analysis.md#analyze-variable) |

## Discovery & Search

| Command | Description | Full Details |
| :--- | :--- | :--- |
| **`kkg find name`** | Find element by exact name. | [details](cli_analysis.md#find-name) |
| **`kkg find pattern`** | Fuzzy search (substring). | [details](cli_analysis.md#find-pattern) |
| **`kkg find type`** | List all Class/Function nodes. | [details](cli_analysis.md#find-type) |
| **`kkg find variable`** | Find variables by name. | [details](cli_analysis.md#analyze-variable) |
| **`kkg find content`** | Full-text search in source code. | [details](cli_analysis.md#find-content) |
| **`kkg find decorator`** | Find functions with `@decorator`. | [details](cli_analysis.md#find-decorator) |
| **`kkg find argument`** | Find functions with specific arg. | [details](cli_analysis.md#find-argument) |

## System & Configuration

| Command | Description | Full Details |
| :--- | :--- | :--- |
| **`kkg doctor`** | Run system health check. | [details](cli_system.md#cgc-doctor) |
| **`kkg mcp setup`** | Configure AI clients. | [details](cli_system.md#cgc-mcp-setup) |
| **`kkg neo4j setup`** | Configure Neo4j database. | [details](cli_system.md#cgc-neo4j-setup) |
| **`kkg config`** | View or modify settings. | [details](configuration.md) |
| **`kkg bundle export`** | Save graph to `.cgc` file. | [details](cli_indexing.md#kkg-bundle-commands) |
| **`kkg bundle load`** | Load graph from file/registry. | [details](cli_indexing.md#kkg-bundle-commands) |
| **`kkg registry`** | Browse cloud bundles. | [details](cli_indexing.md#kkg-registry) |
