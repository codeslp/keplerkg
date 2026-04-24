# CI/CD & Advanced Usage

Integrate CodeGraphContext into your automation pipelines.

## CI/CD Pipeline Integration

You can use CGC to block PRs that introduce "Dead Code" or excessive complexity.

**Example GitHub Action:**

```yaml
name: Code Quality
on: [pull_request]
jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install CGC
        run: pip install codegraphcontext
      - name: Use embedded DB in CI (recommended)
        # FalkorDB Lite is the default on Unix + Python 3.12+. Swap to
        # `cgc config db kuzudb` if the runner uses an older Python or
        # Windows, where KuzuDB is the portable embedded fallback.
        run: cgc config db falkordb
      - name: Index Code
        run: cgc index .
      - name: Check Complexity
        # Fail if any function is complexity > 20
        run: cgc analyze complexity --threshold 20 --fail-on-found
```

## CI without Docker

**FalkorDB Lite** and **KuzuDB** work in CI **without Docker** or external graph services—ideal for GitHub Actions and locked-down runners. **FalkorDB Lite** is the default on Unix runners with **Python 3.12+** and needs no extra configuration. Pin the backend explicitly with **`cgc config db falkordb`** (or **`cgc config db kuzudb`** on Windows / older Python) so behavior is predictable across environments.

## Large Scale Indexing

For repos with > 100,000 LOC:
1.  **Use Neo4j:** FalkorDB may run out of RAM.
2.  **Increase Memory:** `NEO4J_dbms_memory_heap_max_size=4G`.
3.  **Exclude Tests:** Add `tests/` to `.cgcignore`.
