# Quickstart (Detailed Walkthrough)

KeplerKG is a powerful tool for translating your codebase into an exact knowledge graph. Here is an in-depth, step-by-step walkthrough on how to start using it today.

## 1. Prepare Your Repository

Before indexing, navigate into the directory of the codebase you want to analyze.
KeplerKG can index very large monorepos, but it's crucial to skip compiled assets, virtual environments, or logs.

Create a `.cgcignore` file in your root folder (it follows the same syntax as `.gitignore`).

```text
# Example .cgcignore
node_modules/
dist/
build/
.venv/
__pycache__/
*.min.js
```
[📄 Detailed .cgcignore configuration](../reference/cgcignore.md)

## 2. Index the Codebase

Run the following command to parse the codebase and store it in your local graph database (KùzuDB by default).

```bash
kkg index .
```

**What is happening during this step?**
1. **File Scanning:** KeplerKG traverses all files in your folder, obeying `.cgcignore`.
2. **Parser Matching:** It matches the file extension (e.g., `.py`, `.ts`, `.go`) to the bundled Tree-sitter parsers.
3. **AST Extraction:** It extracts Classes, Functions, Variables, Imports, and Exports.
4. **Relationship Mapping:** It wires up the edges (e.g., `Function A` CALLS `Function B`, `File X` IMPORTS `Module Y`).
5. **Database Commit:** It saves all these nodes and edges locally. 

A progress bar will tell you how many files have been successfully ingested.

## 3. Verify the Knowledge Graph

Once the indexing is complete, you should verify what was recorded in the database.

**List all indexed repositories:**
```bash
kkg list
```
*This will output the repository path, time of last index, and summary statistics (number of nodes/edges).*

**View Database Statistics:**
```bash
kkg stats
```
*This dumps a global overview of total Functions, Classes, and Files known by the database.*

## 4. Run Analytical Queries (CLI)

You don't need an AI to get value out of KeplerKG. You can ask structural questions directly from your terminal.

**Find where a function is called:**
```bash
kkg analyze callers authenticate_user
```

**Find everything a function calls (dependencies):**
```bash
kkg analyze calls authenticate_user
```

**Find complexity outliers (Refactoring targets):**
```bash
kkg analyze complexity --limit 10
```

**Show the Class Inheritance Hierarchy:**
```bash
kkg analyze tree DataController
```

## 5. Live File Watching

If you are actively developing and want the graph to stay up to date without manually running `kkg index .` every time you save a file:

```bash
kkg watch .
```

*This spins up a background process. Whenever a `.py` or `.ts` file changes, KeplerKG instantly re-parses just that file and updates the exact nodes in KùzuDB.*

## 6. Visualizing the Graph

Seeing your code structure can reveal architectural flaws, tangled imports, or monolithic classes.

```bash
kkg visualize
```

This command will start a local React application and print a web URL (`http://localhost...`). 
Opening it in your browser gives you a 3D/2D mapped interaction of your code!

---

## 7. Next Step: Integrate with AI

The true power of this graph is handing it to your LLMs.
If you use Cursor, Windsurf, or Claude Desktop:

1. **Configure the MCP Integration:**
   ```bash
   kkg mcp setup
   ```
2. **Start the MCP Server:** Run `kkg mcp start` from the terminal or let your MCP host launch it for you.
3. **Prompt the AI:** Simply ask the AI, *"I need to change how authentication tokens are validated. What functions are affected?"*
4. **Magic:** The AI will natively call the graph, precisely pinpointing dependencies without hallucination!

👉 **[MCP Integration Guide](../guides/mcp_guide.md)** for detailed instructions.
