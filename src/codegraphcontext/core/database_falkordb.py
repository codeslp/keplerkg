# src/codegraphcontext/core/database_falkordb.py
"""
This module provides a thread-safe singleton manager for the FalkorDB Lite database connection.
FalkorDB Lite is an embedded graph database that requires no external server setup.
"""

class FalkorDBUnavailableError(RuntimeError):
    """
    Raised when FalkorDB Lite is installed but cannot actually run in this
    environment (e.g. falkordb.so not found in a PyInstaller bundle,
    or GRAPH.QUERY not available). Callers should fall back to KùzuDB.
    """
import os
import sys
import subprocess
import time
import atexit
import threading
import re
from pathlib import Path
from typing import Optional, Tuple

from codegraphcontext.utils.debug_log import debug_log, info_logger, error_logger, warning_logger

# ---------------------------------------------------------------------------
# Compatibility patch: redis-py >= 5.x added OpenTelemetry error telemetry that
# accesses conn.port inside its error handler. UnixDomainSocketConnection never
# had a 'port' attribute, so any exception raised during a Unix-socket connection
# (e.g. the sentinel-detection INFO call inside FalkorDB.__init__) would produce
# a secondary AttributeError masking the real problem.
# Patching the class at import time costs nothing and fixes all call-sites.
# ---------------------------------------------------------------------------
try:
    from redis.connection import UnixDomainSocketConnection as _UDSC
    if not hasattr(_UDSC, 'port'):
        _UDSC.port = 0  # type: ignore[attr-defined]
except Exception:
    pass  # redis not installed or class structure changed — safe to ignore

class FalkorDBManager:
    """
    Manages the FalkorDB Lite database connection as a singleton.
    Uses a subprocess to isolate the embedded database from the main process environment.
    """
    _instance = None
    _process = None
    _driver = None
    _graph = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        """Standard singleton pattern implementation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(FalkorDBManager, cls).__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[str] = None, socket_path: Optional[str] = None):
        """
        Initializes the manager with default database path or explicit overrides.
        The `_initialized` flag prevents re-initialization on subsequent calls.
        """
        # If we have an existing instance but are being asked to connect to a different path
        # we need to be careful — it's a singleton. For the new context system, we ensure
        # the singleton instance gets re-initialized if the path changes.
        if hasattr(self, '_initialized') and self.db_path == db_path:
            return

        self._initialized = False

        # Configuration priority:
        # 1. Environment variable (highest priority)
        # 2. Config manager (supports project-local .env)
        # 3. Default path (lowest priority)
        
        # Try to load from config manager
        try:
            from codegraphcontext.cli.config_manager import get_config_value
            config_db_path = get_config_value('FALKORDB_PATH')
            config_socket_path = get_config_value('FALKORDB_SOCKET_PATH')
        except Exception:
            # Config manager not available or error loading
            config_db_path = None
            config_socket_path = None
        
        # Database path with fallback chain (Explicit > Env > Config/Default)
        self.db_path = db_path or os.getenv(
            'FALKORDB_PATH',
            config_db_path or str(Path.home() / '.codegraphcontext' / 'global' / 'falkordb.db')
        )
        
        # Socket path with fallback chain
        if socket_path:
            self.socket_path = socket_path
        elif db_path:
            # If a custom DB path was given but no socket path, infer socket path automatically
            # near the custom database rather than putting it in the global directory.
            db_dir = Path(db_path).parent
            self.socket_path = str(db_dir / 'falkordb.sock')
        else:
            self.socket_path = os.getenv(
                'FALKORDB_SOCKET_PATH',
                config_socket_path or str(Path.home() / '.codegraphcontext' / 'global' / 'falkordb.sock')
            )
        
        self.graph_name = os.getenv('FALKORDB_GRAPH_NAME', 'codegraph')
        self._initialized = True
        
        # Register cleanup on exit
        atexit.register(self.shutdown)

    def get_driver(self):
        """
        Gets the FalkorDB connection, starting the subprocess if necessary.
        This method is thread-safe.

        Returns:
            A FalkorDB graph instance that mimics Neo4j driver interface.
        """
        import platform
        
        if platform.system() == "Windows":
            raise RuntimeError(
                "KeplerKG uses redislite/FalkorDB, which does not support Windows.\n"
                "Please run the project using WSL or Docker."
            )
        
        if self._driver is None:
            if sys.version_info < (3, 12):
                raise ValueError("FalkorDB Lite is not supported on Python < 3.12.")

            with self._lock:
                if self._driver is None:
                    # CRITICAL FIX: Prevent ~/.local/bin/falkordb.so from shadowing falkordb package
                    # When running via 'cgc' script installed in ~/.local/bin, sys.path[0] is that dir.
                    if sys.path and sys.path[0]:
                        potential_shadow = os.path.join(sys.path[0], 'falkordb.so')
                        if os.path.exists(potential_shadow):
                            info_logger("Detected 'falkordb.so' in sys.path[0]. Removing path to prevent import shadowing.")
                            sys.path.pop(0)

                    try:
                        self._ensure_server_running()
                        
                        # Use Official FalkorDB Client to connect to the socket
                        from falkordb import FalkorDB
                        
                        info_logger(f"Connecting to FalkorDB Lite at {self.socket_path}")
                        self._driver = FalkorDB(unix_socket_path=self.socket_path)
                        self._graph = self._driver.select_graph(self.graph_name)
                        
                        # Test the connection
                        try:
                            # Graph creation is lazy in some clients, force a query
                            self._graph.query("RETURN 1")
                            info_logger(f"FalkorDB Lite connection established successfully")
                            info_logger(f"Graph name: {self.graph_name}")
                        except Exception as e:
                            info_logger(f"Initial ping check: {e}")
                            
                    except ImportError as e:
                        error_logger(
                            "FalkorDB client is not installed. Install it with:\n"
                            "  pip install falkordblite"
                        )
                        raise ValueError("FalkorDB client missing.") from e
                    except Exception as e:
                        error_logger(f"Failed to initialize FalkorDB: {e}")
                        raise

        # Return a wrapper that provides Neo4j-like session interface
        return FalkorDBDriverWrapper(self._graph)

    def _ensure_server_running(self):
        """Starts the FalkorDB worker subprocess if not reachable."""
        import platform
        
        if platform.system() == "Windows":
            raise RuntimeError(
                "KeplerKG uses redislite/FalkorDB, which does not support Windows.\n"
                "Please run the project using WSL or Docker."
            )
        
        # 1. Try to connect first (maybe running from previous session or other process)
        if os.path.exists(self.socket_path):
            try:
                from falkordb import FalkorDB
                d = FalkorDB(unix_socket_path=self.socket_path)
                # Test not just connectivity (PING), but functionality (GRAPH.QUERY)
                # This ensures we don't connect to a "stale" process that doesn't have the module loaded
                test_graph = d.select_graph('__cgc_health_check')
                test_graph.query("RETURN 1")
                info_logger("Connected to existing (functional) FalkorDB Lite process.")
                return
            except Exception as e:
                # Stale socket, unresponsive, or "brainless" (unknown command GRAPH.QUERY)
                info_logger(f"Existing FalkorDB process at {self.socket_path} is stale or non-functional: {e}")
                info_logger("Cleaning up and attempting fresh start...")
                try:
                    os.remove(self.socket_path)
                except OSError:
                    pass

        # 2. Start Subprocess
        env = os.environ.copy()
        env['FALKORDB_PATH'] = self.db_path
        env['FALKORDB_SOCKET_PATH'] = self.socket_path
        
        # Determine python executable
        python_exe = sys.executable
        
        # We assume codegraphcontext is installed or in python path
        if getattr(sys, 'frozen', False):
            # In frozen mode, the executable is the bundle itself.
            # We tell the bundle to run the worker instead of the app via environment variable.
            env['CGC_RUN_FALKOR_WORKER'] = 'true'
            cmd = [python_exe]
        else:
            # If not frozen, sys.executable should be python.
            # But on some platforms (like PIP installs), it might be the 'cgc' entry point script.
            # We check if it looks like python, otherwise search the PATH.
            import shutil
            exe_name = os.path.basename(python_exe).lower()
            if not any(x in exe_name for x in ['python', 'py.exe', 'pypy']):
                python_exe = shutil.which('python3') or shutil.which('python') or sys.executable
            
            cmd = [python_exe, '-m', 'codegraphcontext.core.falkor_worker']
        
        info_logger("Starting FalkorDB Lite worker subprocess...")
        self._process = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        # 3. Wait for Readiness
        start_time = time.time()
        timeout = 20 # seconds
        
        while time.time() - start_time < timeout:
            if os.path.exists(self.socket_path):
                # Socket created!
                # Give it a tiny sleep to ensure listening
                time.sleep(0.2)
                return
            
            # Check if process died
            if self._process.poll() is not None:
                out, err = self._process.communicate()
                returncode = self._process.returncode
                
                # Any non-zero exit code during startup means this backend is toast
                # Raise FalkorDBUnavailableError to trigger the automatic KùzuDB fallback
                raise FalkorDBUnavailableError(
                    f"FalkorDB Lite worker failed to start (Exit Code {returncode}).\n"
                    f"STDOUT: {out.decode().strip()}\n"
                    f"STDERR: {err.decode().strip()}"
                )
            
            time.sleep(0.5)
            
        raise RuntimeError("Timed out waiting for FalkorDB Lite to start.")

    def close_driver(self):
        """Closes the connection."""
        if self._driver is not None:
            info_logger("Closing FalkorDB Lite connection")
            self._driver = None
            self._graph = None

    def shutdown(self):
        """Kills the subprocess on exit."""
        if self._process:
            if self._process.poll() is None:
                info_logger("Stopping FalkorDB subprocess...")
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
    
    def is_connected(self) -> bool:
        """Checks if the database connection is currently active."""
        if self._graph is None:
            return False
        try:
            self._graph.query("RETURN 1")
            return True
        except Exception:
            return False
    
    def get_backend_type(self) -> str:
        """Returns the database backend type."""
        return 'falkordb'

    def get_conn(self):
        """Return a Kuzu-compatible ``conn`` over the FalkorDB graph.

        The shim exposes ``execute(query, parameters=...)`` returning an
        iterable with ``has_next``/``get_next``, matching what the rest of
        the codegraph extension expects from KuzuDB. Callers that want the
        Neo4j-style session should keep using ``get_driver().session()``.
        """
        self.get_driver()
        if self._graph is None:
            raise RuntimeError("FalkorDB graph is not initialized")
        return FalkorDBKuzuCompatConnection(self._graph)


    @staticmethod
    def validate_config(db_path: str = None) -> Tuple[bool, Optional[str]]:
        """
        Validates FalkorDB configuration parameters.
        
        Returns:
            Tuple[bool, Optional[str]]: (is_valid, error_message)
        """
        if db_path:
            db_dir = Path(db_path).parent
            if not os.access(db_dir, os.W_OK) and db_dir.exists():
                return False, (
                    f"Cannot write to directory: {db_dir}\n"
                    "Please ensure you have write permissions."
                )
        return True, None

    @staticmethod
    def test_connection(db_path: str = None) -> Tuple[bool, Optional[str]]:
        """
        Tests the FalkorDB Lite connection availability.
        """
        try:
            if sys.version_info < (3, 12):
                return False, "FalkorDB Lite is not supported on Python < 3.12. Please upgrade or use Neo4j."

            import falkordb
            return True, None
        except ImportError:
            return False, (
                "FalkorDB client is not installed.\n"
                "Install it with: pip install falkordblite"
            )


class FalkorDBDriverWrapper:
    """
    Wrapper class to provide Neo4j driver-like interface for FalkorDB Lite.
    This allows existing code to work with minimal changes.
    """
    
    def __init__(self, graph):
        self.graph = graph
    
    def session(self):
        """Returns a session-like object for FalkorDB."""
        return FalkorDBSessionWrapper(self.graph)
    
    def close(self):
        """FalkorDB Lite doesn't need explicit close for sessions."""
        pass


# Composite-key → uid mapping. Must match KuzuSessionWrapper.uid_map so the
# same label-keyed MERGE writes the same uid on either backend; downstream
# consumers (embed, ANN, review-packet, bundle export) key off uid.
_UID_COMPOSITE_KEYS = {
    'Function': ['name', 'path', 'line_number'],
    'Class': ['name', 'path', 'line_number'],
    'Variable': ['name', 'path', 'line_number'],
    'Trait': ['name', 'path', 'line_number'],
    'Interface': ['name', 'path', 'line_number'],
    'Macro': ['name', 'path', 'line_number'],
    'Struct': ['name', 'path', 'line_number'],
    'Enum': ['name', 'path', 'line_number'],
    'Union': ['name', 'path', 'line_number'],
    'Annotation': ['name', 'path', 'line_number'],
    'Record': ['name', 'path', 'line_number'],
    'Property': ['name', 'path', 'line_number'],
    'Parameter': ['name', 'path', 'function_line_number'],
}

_UNWIND_RE = re.compile(r'UNWIND\s+\$(\w+)\s+AS\s+(\w+)')
_MERGE_PATTERN_RE = re.compile(r'MERGE\s+\((\w+):([^\s\{]+)\s*\{([^}]+)\}\)')
_UID_ALREADY_PRESENT_RE = re.compile(r'\buid\s*:')

# Kuzu's driver aliases standard Cypher ``type()`` to its internal ``label()``
# (see database_kuzu.py). FalkorDB implements the portable Cypher name
# ``labels()`` which returns a list; translate ``label(<ident>)`` projections
# to ``labels(<ident>)[0]`` in the compat shim so Kuzu-style read queries
# (hybrid/traverse, blast_radius, impact, execution_flow, review_packet)
# return node kinds instead of silently failing into empty result sets.
_KUZU_LABEL_FN_RE = re.compile(r'\blabel\((\w+)\)')


def _translate_kuzu_read_query(query: str) -> str:
    """Translate Kuzu-only read syntax to FalkorDB-compatible Cypher."""
    return _KUZU_LABEL_FN_RE.sub(r'labels(\1)[0]', query)


class FalkorDBSessionWrapper:
    """
    Wrapper class to provide Neo4j session-like interface for FalkorDB Lite.
    """

    def __init__(self, graph):
        self.graph = graph

    def run(self, query, **parameters):
        """
        Execute a Cypher query on FalkorDB.
        """
        # Inject synthetic uid into UNWIND+MERGE writes so uid-scoped labels
        # carry the same primary key contract as Kuzu. Must run before the
        # schema-query translation below so the SET/MERGE bodies we rewrite
        # here are still the ones FalkorDB will execute.
        query, parameters = self._inject_uid_for_unwind_merge(query, parameters)

        # Translate Neo4j schema queries to FalkorDB syntax
        query = self._translate_schema_query(query)

        try:
            result = self.graph.query(query, parameters)
            return FalkorDBResultWrapper(result)
        except Exception as e:
            # Ignore errors about existing constraints/indexes
            error_msg = str(e).lower()
            if "already exists" in error_msg or "already created" in error_msg:
                return FalkorDBResultWrapper(None)

            error_logger(f"FalkorDB query failed: {query[:100]}... Error: {e}")
            raise

    def _inject_uid_for_unwind_merge(self, query, parameters):
        """Mirror ``KuzuSessionWrapper``'s uid synthesis for UNWIND+MERGE writes.

        The indexer writes symbol nodes with
        ``UNWIND $batch AS row MERGE (n:Label {name: row.name, ...})``.
        Kuzu's driver wrapper mutates each batch item in place to add
        ``item['uid']`` and rewrites the MERGE to include
        ``uid: row.uid``. We do the same here so FalkorDB-backed
        indexing produces nodes with a stable uid that downstream tools
        (ANN, review-packet, bundle export) can key off of.

        Hot path: ``session.run`` fires once per MERGE batch during
        indexing, so the unaffected-query fast path below matters — we
        early-out via a literal substring check before any regex work.
        """
        if "UNWIND" not in query:
            return query, parameters

        unwind_m = _UNWIND_RE.search(query)
        if not unwind_m:
            return query, parameters

        batch_param = unwind_m.group(1)
        row_var = unwind_m.group(2)
        batch_data = parameters.get(batch_param)
        if not isinstance(batch_data, list) or not batch_data:
            return query, parameters

        for m in list(_MERGE_PATTERN_RE.finditer(query)):
            var_name, label_raw, props_str = m.groups()
            label = label_raw.strip('`')
            pk_parts = _UID_COMPOSITE_KEYS.get(label)
            if not pk_parts:
                continue
            if _UID_ALREADY_PRESENT_RE.search(props_str):
                continue

            # Resolve pk-part → (source, ref) ONCE per MERGE block; the
            # props string is shared across every batch item, so the
            # regex below would otherwise fire pk_parts × len(batch)
            # times on every write.
            resolution_ok = True
            resolved: list[tuple[str, str]] = []  # (source, ref_name)
            for part in pk_parts:
                row_ref = re.search(
                    rf'\b{part}\s*:\s*{re.escape(row_var)}\.(\w+)', props_str
                )
                if row_ref:
                    resolved.append(("row", row_ref.group(1)))
                    continue
                param_ref = re.search(rf'\b{part}\s*:\s*\$(\w+)', props_str)
                if param_ref:
                    resolved.append(("param", param_ref.group(1)))
                    continue
                resolution_ok = False
                break
            if not resolution_ok:
                continue

            all_ok = True
            for item in batch_data:
                uid_components = []
                for source, ref_name in resolved:
                    val = item.get(ref_name) if source == "row" else parameters.get(ref_name)
                    if val is None:
                        all_ok = False
                        break
                    uid_components.append(str(val))
                if not all_ok:
                    break
                item['uid'] = ''.join(uid_components)

            if all_ok:
                new_props = props_str.rstrip() + f", uid: {row_var}.uid"
                new_merge = f"MERGE ({var_name}:{label_raw} {{{new_props}}})"
                query = query.replace(m.group(0), new_merge, 1)

        return query, parameters

    def _translate_schema_query(self, query: str) -> str:
        """Translate Neo4j schema queries to FalkorDB/RedisGraph syntax."""
        q_upper = query.upper()
        
        # Handle Fulltext Indexes (Not supported in same syntax, skip for now)
        if "CREATE FULLTEXT INDEX" in q_upper:
            return "RETURN 1"
            
        # Handle Constraints
        if "CREATE CONSTRAINT" in q_upper:
            # Remove "IF NOT EXISTS"
            query = re.sub(r'\s+IF NOT EXISTS', '', query, flags=re.IGNORECASE)
            
            # Handle composite keys: (n.p1, n.p2) -> downgrade to INDEX
            if "," in query:
                match_node = re.search(r'FOR\s+(\([^)]+\))', query, flags=re.IGNORECASE)
                match_props = re.search(r'REQUIRE\s+(\([^)]+\))\s+IS UNIQUE', query, flags=re.IGNORECASE)
                
                if match_node and match_props:
                    return f"CREATE INDEX FOR {match_node.group(1)} ON {match_props.group(1)}"

            # Handle simple uniqueness: CREATE CONSTRAINT name FOR (n:Label) REQUIRE n.prop IS UNIQUE
            # TO: CREATE CONSTRAINT ON (n:Label) ASSERT n.prop IS UNIQUE
            
            # Remove constraint name
            query = re.sub(r'CREATE CONSTRAINT\s+\w+\s+', 'CREATE CONSTRAINT ', query, flags=re.IGNORECASE)
            query = re.sub(r'\s+FOR\s+', ' ON ', query, flags=re.IGNORECASE)
            query = re.sub(r'\s+REQUIRE\s+', ' ASSERT ', query, flags=re.IGNORECASE)
            
        # Handle Regular Indexes
        elif "CREATE INDEX" in q_upper:
            # Remove "IF NOT EXISTS"
            query = re.sub(r'\s+IF NOT EXISTS', '', query, flags=re.IGNORECASE)
            # Remove Index Name: CREATE INDEX name FOR -> CREATE INDEX FOR
            query = re.sub(r'CREATE INDEX\s+\w+\s+FOR', 'CREATE INDEX FOR', query, flags=re.IGNORECASE)
            
        return query
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class FalkorDBRecord(dict):
    """
    Dict wrapper that provides a .data() method for compatibility with Neo4j records.
    """
    def data(self):
        return self

class FalkorDBResultWrapper:
    """
    Wrapper class to provide Neo4j result-like interface for FalkorDB results.
    """
    
    def __init__(self, result):
        self.result = result
        self._consumed = False
    
    def consume(self):
        """Mark result as consumed (for compatibility)."""
        self._consumed = True
        return self
    
    def single(self):
        """Return single result record as a FalkorDBRecord."""
        data = self.data()
        return data[0] if data else None
    
    def data(self):
        """Return all results as list of FalkorDBRecord objects."""
        if not hasattr(self.result, 'result_set'):
            return []
        
        # Convert result_set to list of dicts (wrapped in FalkorDBRecord)
        results = []
        if hasattr(self.result, 'header') and self.result.header:
            headers = self.result.header
            for row in self.result.result_set:
                row_dict = FalkorDBRecord()
                for i, header in enumerate(headers):
                    if i < len(row):
                        # FalkorDB headers are [column_type, column_name] pairs
                        # Extract the column name (index 1) and decode if bytes
                        if isinstance(header, (list, tuple)) and len(header) > 1:
                            header_name = header[1]
                            if isinstance(header_name, bytes):
                                header_name = header_name.decode('utf-8')
                        else:
                            header_name = str(header)
                        row_dict[header_name] = row[i]
                results.append(row_dict)
        elif hasattr(self.result, 'result_set'):
            # Fallback if no header
            for row in self.result.result_set:
                if isinstance(row, (list, tuple)) and len(row) == 1:
                    results.append(FalkorDBRecord({'value': row[0]}))
                else:
                    results.append(FalkorDBRecord({'value': row}))
        
        return results
    
    def __iter__(self):
        """Iterate over results as FalkorDBRecord objects."""
        return iter(self.data())


class FalkorDBKuzuCompatResult:
    """Kuzu-style result iterator backed by FalkorDB's ``result_set``.

    Kuzu callers iterate with ``while result.has_next(): result.get_next()``
    and index rows positionally. FalkorDB returns a ``QueryResult`` with a
    ``result_set`` of lists in the same order as the RETURN clause, so we
    can expose the Kuzu contract by stepping through that list directly.
    """

    def __init__(self, query_result):
        self._rows = list(getattr(query_result, "result_set", None) or [])
        self._idx = 0

    def has_next(self) -> bool:
        return self._idx < len(self._rows)

    def get_next(self):
        row = self._rows[self._idx]
        self._idx += 1
        return row


class FalkorDBKuzuCompatConnection:
    """Kuzu-style ``conn.execute(query, parameters=...)`` shim over FalkorDB.

    Lets code written against Kuzu's driver (``execute``/``has_next``/
    ``get_next``) run unchanged on FalkorDB so we don't have to rewrite
    every query site during the Spec 006 migration. Cypher queries that
    are portable between Kuzu and FalkorDB continue to work, and the
    shim auto-translates Kuzu's ``label(<ident>)`` node-kind projection
    to portable ``labels(<ident>)[0]`` on the way through. Callers that
    rely on Kuzu-only DDL (``ALTER TABLE``, ``CREATE HNSW INDEX``,
    ``CALL hnsw_search``) still need backend-specific branches.
    """

    def __init__(self, graph):
        self._graph = graph

    def execute(self, query: str, parameters=None):
        params = parameters or {}
        result = self._graph.query(_translate_kuzu_read_query(query), params)
        return FalkorDBKuzuCompatResult(result)
