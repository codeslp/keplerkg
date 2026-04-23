# src/codegraphcontext/core/__init__.py
"""
Core database management module.

Supports KùzuDB, Neo4j, FalkorDB Lite, and remote FalkorDB backends.

Explicit backend selection (see ``get_database_manager``):
- ``CGC_RUNTIME_DB_TYPE`` — per-invocation override (CLI ``--database`` / MCP resolved context).
- ``DEFAULT_DATABASE`` — configured default from ``cgc config db …`` / KeplerKG ``.env``.

When neither is set, implicit selection:
- Remote FalkorDB if ``FALKORDB_HOST`` is set (explicit remote signal).
- Else **Unix**: FalkorDB Lite when Python 3.12+ and ``falkordblite`` work.
- Else KùzuDB if installed.
- Else Neo4j if credentials exist.
"""
import os
import platform
from typing import Union, Optional
import importlib.util

def _is_kuzudb_available() -> bool:
    """Check if KùzuDB is installed."""
    try:
        return importlib.util.find_spec("kuzu") is not None
    except ImportError:
        return False

def _is_falkordb_available() -> bool:
    """Check if FalkorDB Lite is installed (Unix only)."""
    if platform.system() == "Windows":
        return False

    import sys
    if sys.version_info < (3, 12):
        return False
    try:
        import redislite
        return hasattr(redislite, 'falkordb_client')
    except ImportError:
        return False

def _is_falkordb_remote_configured() -> bool:
    """Check if a remote FalkorDB host is configured."""
    return bool(os.getenv('FALKORDB_HOST'))

def _is_neo4j_configured() -> bool:
    """Check if Neo4j is configured with credentials."""
    return all([
        os.getenv('NEO4J_URI'),
        os.getenv('NEO4J_USERNAME'),
        os.getenv('NEO4J_PASSWORD')
    ])

def get_database_manager(db_path: Optional[str] = None) -> Union['DatabaseManager', 'FalkorDBManager', 'FalkorDBRemoteManager', 'KuzuDBManager']:
    """
    Factory function to get the appropriate database manager based on configuration.

    Selection logic:
    1. Runtime override: ``CGC_RUNTIME_DB_TYPE`` (CLI ``--database``, MCP context).
    2. Configured default: ``DEFAULT_DATABASE`` (``cgc config db …``, KeplerKG ``.env``).
    3. Implicit: ``FALKORDB_HOST`` → remote FalkorDB; else Unix-only
       FalkorDB Lite; then KùzuDB; Neo4j if configured.
    """
    from codegraphcontext.utils.debug_log import info_logger

    db_type = os.getenv("CGC_RUNTIME_DB_TYPE") or os.getenv("DEFAULT_DATABASE")

    if db_type:
        db_type = db_type.lower()
        if db_type == 'kuzudb':
            if not _is_kuzudb_available():
                raise ValueError("Database set to 'kuzudb' but Kùzu is not installed.\nRun 'pip install kuzu'")
            from .database_kuzu import KuzuDBManager
            info_logger(f"Using KùzuDB (explicit) at {db_path or 'default path'}")
            return KuzuDBManager(db_path=db_path)

        elif db_type == 'falkordb':
            if not _is_falkordb_available():
                info_logger("FalkorDB Lite is not supported or not installed. Falling back to KùzuDB.")
                if _is_kuzudb_available():
                    from .database_kuzu import KuzuDBManager
                    return KuzuDBManager()
                raise ValueError("Database set to 'falkordb' but FalkorDB Lite is not installed or not supported on this OS.\nRun 'pip install falkordblite'")
            
            from .database_falkordb import FalkorDBManager, FalkorDBUnavailableError
            try:
                mgr = FalkorDBManager(db_path=db_path)
                info_logger(f"Using FalkorDB Lite (explicit) at {db_path or 'default path'}")
                return mgr
            except FalkorDBUnavailableError as falkor_err:
                info_logger(f"FalkorDB Lite not functional ({falkor_err}). Falling back to KùzuDB.")
                if _is_kuzudb_available():
                    from .database_kuzu import KuzuDBManager
                    return KuzuDBManager(db_path=db_path)
                raise

        elif db_type == 'falkordb-remote':
            if not _is_falkordb_remote_configured():
                raise ValueError(
                    "Database set to 'falkordb-remote' but FALKORDB_HOST is not set.\n"
                    "Set the FALKORDB_HOST environment variable to your remote FalkorDB host."
                )
            from .database_falkordb_remote import FalkorDBRemoteManager
            info_logger("Using remote FalkorDB (explicit)")
            return FalkorDBRemoteManager()

        elif db_type == 'neo4j':
            if not _is_neo4j_configured():
                raise ValueError("Database set to 'neo4j' but it is not configured.\nRun 'cgc neo4j setup' to configure Neo4j.")
            from .database import DatabaseManager
            info_logger("Using Neo4j Server (explicit)")
            return DatabaseManager()
        else:
            raise ValueError(f"Unknown database type: '{db_type}'. Use 'kuzudb', 'falkordb', 'falkordb-remote', or 'neo4j'.")

    # Implicit: remote FalkorDB when FALKORDB_HOST is set (explicit infra signal)
    if _is_falkordb_remote_configured():
        from .database_falkordb_remote import FalkorDBRemoteManager
        info_logger("Using remote FalkorDB (auto-detected via FALKORDB_HOST)")
        return FalkorDBRemoteManager()

    # Implicit: FalkorDB Lite is the preferred local backend.
    if _is_falkordb_available():
        from .database_falkordb import FalkorDBManager, FalkorDBUnavailableError
        try:
            mgr = FalkorDBManager(db_path=db_path)
            info_logger(f"Using FalkorDB Lite (default) at {db_path or 'default path'}")
            return mgr
        except FalkorDBUnavailableError as falkor_err:
            info_logger(
                f"FalkorDB Lite not functional in this environment ({falkor_err}). "
                "Continuing to the next backend."
            )

    # Implicit: KùzuDB remains available as a local fallback.
    if _is_kuzudb_available():
        from .database_kuzu import KuzuDBManager
        info_logger(f"Using KùzuDB (fallback) at {db_path or 'default path'}")
        return KuzuDBManager(db_path=db_path)

    # Implicit: Neo4j when configured
    if _is_neo4j_configured():
        from .database import DatabaseManager
        info_logger("Using Neo4j Server (auto-detected)")
        return DatabaseManager()

    error_msg = "No database backend available.\n"
    if platform.system() != "Windows":
        error_msg += "Recommended: Install FalkorDB Lite as the default local backend ('pip install falkordblite')\n"

    error_msg += "Alternative: Install KùzuDB ('pip install kuzu')\n"

    error_msg += "Alternative: Run 'cgc neo4j setup' to configure Neo4j."

    raise ValueError(error_msg)

# For backward compatibility, export managers
from .database import DatabaseManager
from .database_falkordb import FalkorDBManager
from .database_falkordb_remote import FalkorDBRemoteManager
from .database_kuzu import KuzuDBManager

__all__ = ['DatabaseManager', 'FalkorDBManager', 'FalkorDBRemoteManager', 'KuzuDBManager', 'get_database_manager']
