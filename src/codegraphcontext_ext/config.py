"""Phase 3 cgraph config layer — reads [cgraph] from .btrain/project.toml.

Provides ``resolve_cgraph_config()`` which returns a ``CgraphConfig``
dataclass with typed fields and defaults.  The config feeds into:

- Preflight (db_path / model_cache → mount check)
- Advise (advise_on filtering, per-lane overrides)
- Future adapter (bin_path, timeout budgets)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class LaneConfig:
    """Per-lane overrides under ``[cgraph.lanes.<id>]``."""
    disable_advise: bool = False
    advise_on: Optional[list[str]] = None  # None = inherit project-level


@dataclass
class StandardsConfig:
    """Parsed ``[cgraph.standards]`` section."""
    profile: str = "default"  # "default" | "strict" | "soc2" | "minimal"
    categories: list[str] = field(default_factory=lambda: ["all"])
    overrides: dict[str, str] = field(default_factory=dict)  # rule_id → severity | "off"
    thresholds: dict[str, dict[str, int]] = field(default_factory=dict)  # rule_id → {warn: N, hard: M}
    hard_stop: list[str] = field(
        default_factory=lambda: ["CGQ-A01", "CGQ-A02", "CGQ-A03"],
    )


# Preset profiles — each defines category set + overrides + hard_stop
STANDARDS_PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "categories": ["all"],
        "hard_stop": ["CGQ-A01", "CGQ-A02", "CGQ-A03"],
    },
    "strict": {
        "categories": ["all"],
        "overrides": {
            "CGQ-A05": "blocker",  # god_class
            "CGQ-A06": "blocker",  # inappropriate_intimacy
            "CGQ-B01": "blocker",  # cyclomatic_complexity
        },
        "hard_stop": [
            "CGQ-A01", "CGQ-A02", "CGQ-A03",
            "CGQ-A05", "CGQ-A06", "CGQ-B01",
        ],
    },
    "soc2": {
        "categories": ["coupling", "compliance"],
        "overrides": {
            "CGQ-H01": "blocker",  # auth_bypass
            "CGQ-H03": "blocker",  # sensitive_data_unprotected
            "CGQ-H04": "blocker",  # hardcoded_secret
            "CGQ-H05": "blocker",  # admin_no_audit_trail
        },
        "hard_stop": [
            "CGQ-A01", "CGQ-A02", "CGQ-A03",
            "CGQ-H01", "CGQ-H03", "CGQ-H04", "CGQ-H05",
        ],
    },
    "minimal": {
        "categories": ["coupling"],
        "hard_stop": ["CGQ-A01", "CGQ-A02", "CGQ-A03"],
    },
}


@dataclass
class CgraphConfig:
    """Parsed ``[cgraph]`` section from ``.btrain/project.toml``."""
    enabled: bool = False
    bin_path: str = "kkg"
    source_checkout: Optional[Path] = None
    db_path: Optional[Path] = None
    model_cache: Optional[Path] = None
    advise_on: list[str] = field(
        default_factory=lambda: ["lock_overlap", "drift", "packet_truncated"],
    )
    advise_on_resolution: bool = False
    lanes: dict[str, LaneConfig] = field(default_factory=dict)
    standards: StandardsConfig = field(default_factory=StandardsConfig)


# ---------------------------------------------------------------------------
# TOML micro-parser (no external dependency, mirrors sync_check.py)
# ---------------------------------------------------------------------------

def _parse_toml_value(raw: str) -> str | bool | list[str]:
    """Parse a single TOML value — string, bool, or string array."""
    val = raw.strip()
    if val.lower() == "true":
        return True
    if val.lower() == "false":
        return False
    if val.startswith("["):
        # Simple string array: ["a", "b"]
        try:
            parsed = ast.literal_eval(val)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (ValueError, SyntaxError):
            return val
    if val and val[0] in {"'", '"'}:
        try:
            return str(ast.literal_eval(val))
        except (ValueError, SyntaxError):
            return val
    return val


def _parse_kv(line: str) -> tuple[str, str | bool | list[str]] | None:
    """Return (key, parsed_value) or None if line isn't a key=value pair."""
    key, sep, raw_val = line.partition("=")
    if not sep:
        return None
    return key.strip(), _parse_toml_value(raw_val)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_btrain_project_toml(start_dir: Optional[Path] = None) -> Optional[Path]:
    """Walk up from *start_dir* looking for ``.btrain/project.toml``."""
    current = (start_dir or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        toml = candidate / ".btrain" / "project.toml"
        if toml.is_file():
            return toml
    return None


def resolve_cgraph_config(
    project_toml: Optional[Path] = None,
) -> CgraphConfig:
    """Parse the ``[cgraph]`` block and return a typed config.

    If *project_toml* is None, searches upward from cwd.  Returns a
    default ``CgraphConfig`` if no file or no ``[cgraph]`` section found.
    """
    if project_toml is None:
        project_toml = find_btrain_project_toml()
    if project_toml is None or not project_toml.is_file():
        return CgraphConfig()

    text = project_toml.read_text(encoding="utf-8")
    return _parse_cgraph_section(text)


def _parse_cgraph_section(text: str) -> CgraphConfig:
    """Extract ``[cgraph]`` and ``[cgraph.lanes.*]`` from raw TOML text."""
    cfg = CgraphConfig()
    section: tuple[str, ...] = ()

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue

        # Section header
        if line.startswith("[") and line.endswith("]"):
            section = tuple(p.strip() for p in line[1:-1].split("."))
            continue

        kv = _parse_kv(line)
        if kv is None:
            continue
        key, val = kv

        if section == ("cgraph",):
            _apply_top_level(cfg, key, val)
        elif section == ("cgraph", "standards"):
            _apply_standards_level(cfg.standards, key, val)
        elif section == ("cgraph", "standards", "overrides"):
            # [cgraph.standards.overrides] — rule_id = severity
            if isinstance(val, str):
                cfg.standards.overrides[key] = val
        elif len(section) == 3 and section[:2] == ("cgraph", "lanes"):
            lane_id = section[2]
            if lane_id not in cfg.lanes:
                cfg.lanes[lane_id] = LaneConfig()
            _apply_lane_level(cfg.lanes[lane_id], key, val)

    # Apply preset profile if set
    _apply_preset(cfg.standards)

    return cfg


def _apply_top_level(
    cfg: CgraphConfig,
    key: str,
    val: str | bool | list[str],
) -> None:
    if key == "enabled" and isinstance(val, bool):
        cfg.enabled = val
    elif key == "bin_path" and isinstance(val, str):
        cfg.bin_path = val
    elif key == "source_checkout" and isinstance(val, str):
        cfg.source_checkout = Path(val).expanduser()
    elif key == "db_path" and isinstance(val, str):
        cfg.db_path = Path(val).expanduser()
    elif key == "model_cache" and isinstance(val, str):
        cfg.model_cache = Path(val).expanduser()
    elif key == "advise_on" and isinstance(val, list):
        cfg.advise_on = val
    elif key == "advise_on_resolution" and isinstance(val, bool):
        cfg.advise_on_resolution = val


def _apply_standards_level(
    std: StandardsConfig,
    key: str,
    val: str | bool | list[str],
) -> None:
    if key == "profile" and isinstance(val, str):
        std.profile = val
    elif key == "categories" and isinstance(val, list):
        std.categories = val
    elif key == "hard_stop" and isinstance(val, list):
        std.hard_stop = val


def _apply_preset(std: StandardsConfig) -> None:
    """Apply preset profile defaults — explicit config overrides preset values."""
    if std.profile not in STANDARDS_PRESETS:
        return
    preset = STANDARDS_PRESETS[std.profile]
    # Only apply preset values where user hasn't explicitly set them
    # (categories default is ["all"] — if unchanged, inherit from preset)
    if std.categories == ["all"] and "categories" in preset:
        std.categories = preset["categories"]
    # Merge preset overrides (user overrides win)
    if "overrides" in preset:
        for rule_id, severity in preset["overrides"].items():
            std.overrides.setdefault(rule_id, severity)
    # Merge preset hard_stop (union with user's)
    if "hard_stop" in preset:
        preset_stops = set(preset["hard_stop"])
        user_stops = set(std.hard_stop)
        std.hard_stop = sorted(preset_stops | user_stops)


def _apply_lane_level(
    lane: LaneConfig,
    key: str,
    val: str | bool | list[str],
) -> None:
    if key == "disable_advise" and isinstance(val, bool):
        lane.disable_advise = val
    elif key == "advise_on" and isinstance(val, list):
        lane.advise_on = val
