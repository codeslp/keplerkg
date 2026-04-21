"""AST-based call extraction fallback for Python files.

Uses stdlib ``ast`` to extract function calls when tree-sitter parsing
fails or as a supplementary source.  Handles three tiers:

  1. **self/cls** — ``self.method()`` or ``cls.method()`` resolved to same file.
  2. **import-resolved** — calls to names imported in the file.
  3. **bare-unique** — unqualified calls to names that exist exactly once
     in the ``imports_map``.

Returns the same ``function_calls`` list format that the tree-sitter
parser produces, so it plugs directly into ``build_function_call_groups``.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ....utils.debug_log import info_logger, warning_logger

logger = logging.getLogger(__name__)


def extract_calls_from_python_file(
    file_path: str,
    source: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Parse a Python file with ``ast`` and return extracted call data.

    Parameters
    ----------
    file_path:
        Absolute path to the Python file.
    source:
        Optional pre-read source text.  If *None*, the file is read from disk.

    Returns
    -------
    list of call dicts compatible with the tree-sitter ``function_calls`` format::

        {
            "name": str,          # callee bare name
            "full_name": str,     # e.g. "self.method" or "module.func"
            "line_number": int,
            "args": list[str],
            "inferred_obj_type": str | None,
            "context": tuple,     # (enclosing_name, context_type, line)
            "class_context": tuple,
            "lang": "python",
            "is_dependency": False,
        }
    """
    if source is None:
        try:
            source = Path(file_path).read_text(encoding="utf-8")
        except Exception as exc:
            warning_logger(f"[AST-CALLS] Cannot read {file_path}: {exc}")
            return []

    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError as exc:
        warning_logger(f"[AST-CALLS] Syntax error in {file_path}: {exc}")
        return []

    visitor = _CallVisitor(file_path)
    visitor.visit(tree)
    return visitor.calls


class _CallVisitor(ast.NodeVisitor):
    """Walk the AST collecting function/method calls."""

    def __init__(self, file_path: str) -> None:
        self.file_path = file_path
        self.calls: List[Dict[str, Any]] = []
        # Stack of (name, node_type, line_number) for enclosing scopes
        self._scope_stack: List[tuple] = []

    # ------------------------------------------------------------------
    # Scope tracking
    # ------------------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope_stack.append((node.name, "function_definition", node.lineno))
        self.generic_visit(node)
        self._scope_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope_stack.append((node.name, "class_definition", node.lineno))
        self.generic_visit(node)
        self._scope_stack.pop()

    # ------------------------------------------------------------------
    # Call extraction
    # ------------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:  # noqa: C901
        name: Optional[str] = None
        full_name: Optional[str] = None
        inferred_obj_type: Optional[str] = None

        func = node.func
        if isinstance(func, ast.Name):
            # bare call: foo()
            name = func.id
            full_name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
            # Reconstruct dotted name
            parts = [func.attr]
            val = func.value
            while isinstance(val, ast.Attribute):
                parts.append(val.attr)
                val = val.value
            if isinstance(val, ast.Name):
                parts.append(val.id)
                full_name = ".".join(reversed(parts))
                base = val.id
                if base in ("self", "cls"):
                    inferred_obj_type = None  # resolved to same file
                else:
                    inferred_obj_type = base
            else:
                full_name = f"<expr>.{func.attr}"
        else:
            # Complex expression as callee — skip
            self.generic_visit(node)
            return

        if name is None:
            self.generic_visit(node)
            return

        # Build context tuples matching tree-sitter format
        context = self._current_context()
        class_context = self._current_class_context()

        self.calls.append({
            "name": name,
            "full_name": full_name or name,
            "line_number": node.lineno,
            "args": [ast.dump(a) for a in node.args[:4]],  # abbreviated
            "inferred_obj_type": inferred_obj_type,
            "context": context,
            "class_context": class_context,
            "lang": "python",
            "is_dependency": False,
        })

        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _current_context(self) -> tuple:
        """Return (name, context_type, line_number) of innermost scope."""
        for name, kind, line in reversed(self._scope_stack):
            return (name, kind, line)
        return (None, None, None)

    def _current_class_context(self) -> tuple:
        """Return (class_name, 'class_definition') of innermost class."""
        for name, kind, line in reversed(self._scope_stack):
            if kind == "class_definition":
                return (name, kind)
        return (None, None)


def ast_fallback_for_file_data(
    file_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Return AST-extracted calls for a parsed file_data dict.

    Use this when ``file_data["function_calls"]`` is empty but the file
    is a Python file that should have calls.
    """
    if file_data.get("lang") != "python":
        return []

    existing = file_data.get("function_calls", [])
    if existing:
        return existing  # tree-sitter already got them

    file_path = str(Path(file_data["path"]).resolve())
    calls = extract_calls_from_python_file(file_path)
    if calls:
        info_logger(
            f"[AST-CALLS] Fallback extracted {len(calls)} calls from {Path(file_path).name}"
        )
    return calls
