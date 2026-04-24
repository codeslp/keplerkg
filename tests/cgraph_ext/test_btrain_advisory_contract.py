"""Contract tests for the btrain ↔ cgraph advisory-state surface.

btrain's adapter is the sole writer for `.btrain/cgraph-advisory-state.jsonl`
(live) and `.btrain/logs/cgraph-advisories.jsonl` (append-only telemetry), and
normalizes a `cgraph` object into every lane event appended under
`.btrain/events/lane-*.jsonl`. cgraph owns the *shape* those files commit to
because the replay harness reads them as structured fields. These tests
validate that shape using the repo-local schemas and, when a live `.btrain/`
tree exists, against real artifacts so the contract fails loudly if it drifts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codegraphcontext_ext.io.schema_check import (
    SchemaValidationError,
    load_schema,
    schema_path,
    validate_payload,
    validate_schema,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BTRAIN_ROOT = REPO_ROOT / ".btrain"

STATE_SCHEMA = "cgraph-advisory-state.json"
LOG_SCHEMA = "cgraph-advisory-log.json"
EVENT_METADATA_SCHEMA = "adapter-event-metadata.json"


def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            stripped = line.strip()
            if not stripped:
                continue
            yield lineno, json.loads(stripped)


def _extract_cgraph_block(event: dict) -> dict | None:
    """Return the normalized cgraph metadata block from a lane event, if any."""
    for location in ("after", "details"):
        section = event.get(location)
        if isinstance(section, dict):
            candidate = section.get("cgraph")
            if isinstance(candidate, dict):
                return candidate
    # Some future writers may move cgraph to the top level; support that too.
    top_level = event.get("cgraph")
    return top_level if isinstance(top_level, dict) else None


# ---------------------------------------------------------------------------
# Schemas parse and declare the fields Phase B commits to
# ---------------------------------------------------------------------------


def test_advisory_state_schema_parses_and_has_expected_fields():
    schema = load_schema(STATE_SCHEMA)
    # The keys btrain's reconcileCgraphAdvisories writer commits to. Bumping
    # either side without the other silently breaks replay.
    assert set(schema["required"]) == {
        "lane",
        "kind",
        "context_hash",
        "first_seen",
        "last_surfaced",
        "resolved_at",
        "detail",
        "suggestion",
        "advisory_id",
    }
    assert schema.get("additionalProperties") is False


def test_advisory_log_schema_parses_and_pins_event_enum():
    schema = load_schema(LOG_SCHEMA)
    assert schema["properties"]["event"]["enum"] == ["surfaced", "resolved"]
    assert schema.get("additionalProperties") is False


def test_adapter_event_metadata_schema_exposes_advisory_arrays():
    schema = load_schema(EVENT_METADATA_SCHEMA)
    props = schema["properties"]
    for key in ("fresh_advisories", "resolved_advisories"):
        assert key in props, f"{key} missing from adapter-event-metadata schema"
        assert props[key]["type"] == "array"
        assert props[key]["items"] == {"$ref": "#/$defs/cgraphAdvisory"}
    assert "drift" in props
    assert "cgraphAdvisory" in schema["$defs"]


# ---------------------------------------------------------------------------
# Synthetic payloads exercise the contract positively and negatively
# ---------------------------------------------------------------------------

_ADVISORY_CORE = {
    "lane": "a",
    "kind": "lock_overlap",
    "context_hash": "abc123def456",
    "detail": "Lane a overlaps lane b on src/foo.py",
    "suggestion": "Split the lane or coordinate with lane b.",
    "advisory_id": "lock_overlap",
}


def _minimal_state_row() -> dict:
    return {
        **_ADVISORY_CORE,
        "first_seen": "2026-04-20T18:00:00.000Z",
        "last_surfaced": "2026-04-20T18:05:00.000Z",
        "resolved_at": None,
    }


def _minimal_log_row(event: str = "surfaced") -> dict:
    return {
        **_ADVISORY_CORE,
        "event": event,
        "recorded_at": "2026-04-20T18:00:00.000Z",
    }


def _minimal_cgraph_advisory_entry() -> dict:
    return {
        "kind": "lock_overlap",
        "level": "warn",
        "detail": "Overlap with lane b",
        "suggestion": "Split the lane",
        "rationale": "",
        "advisory_id": "lock_overlap",
        "context_hash": "abc123def456",
        "other_lane_ids": ["b"],
        "overlapping_node_ids": ["src/foo.py:42"],
        "changed_node_ids": [],
        "stale_file_paths": [],
        "truncated_node_ids": [],
        "handoff_id": "",
    }


def test_state_row_accepts_minimal_shape():
    validate_payload(STATE_SCHEMA, _minimal_state_row())


def test_state_row_accepts_resolved_at_string_for_forward_compat():
    row = _minimal_state_row()
    row["resolved_at"] = "2026-04-20T18:10:00.000Z"
    validate_payload(STATE_SCHEMA, row)


def test_state_row_rejects_extra_fields():
    row = _minimal_state_row()
    row["surprise"] = True
    with pytest.raises(SchemaValidationError, match="additional property 'surprise'"):
        validate_payload(STATE_SCHEMA, row)


def test_state_row_rejects_missing_required():
    row = _minimal_state_row()
    del row["context_hash"]
    with pytest.raises(SchemaValidationError, match="missing required property 'context_hash'"):
        validate_payload(STATE_SCHEMA, row)


def test_log_row_accepts_surfaced_and_resolved():
    validate_payload(LOG_SCHEMA, _minimal_log_row("surfaced"))
    validate_payload(LOG_SCHEMA, _minimal_log_row("resolved"))


def test_log_row_rejects_unknown_event_kind():
    row = _minimal_log_row()
    row["event"] = "promoted"
    with pytest.raises(SchemaValidationError, match="not in enum"):
        validate_payload(LOG_SCHEMA, row)


def test_adapter_event_metadata_accepts_fresh_and_resolved_advisories():
    validate_payload(
        EVENT_METADATA_SCHEMA,
        {
            "status": "ok",
            "graph_mode": "shared-working",
            "latency_ms": {"blast_radius": 1234},
            "fresh_advisories": [_minimal_cgraph_advisory_entry()],
            "resolved_advisories": [_minimal_cgraph_advisory_entry()],
        },
    )


def test_adapter_event_metadata_accepts_drift_block():
    validate_payload(
        EVENT_METADATA_SCHEMA,
        {
            "status": "ok",
            "graph_mode": "shared-working",
            "latency_ms": {"drift_check": 842},
            "drift": {
                "since": "2026-04-20T18:00:00.000Z",
                "drifted_nodes": 3,
                "neighbor_files": 5,
            },
        },
    )


def test_adapter_event_metadata_rejects_unknown_graph_mode():
    with pytest.raises(SchemaValidationError, match="not in enum"):
        validate_payload(
            EVENT_METADATA_SCHEMA,
            {"status": "ok", "graph_mode": "working/zz", "latency_ms": {}},
        )


def test_adapter_event_metadata_rejects_malformed_advisory_entry():
    with pytest.raises(SchemaValidationError):
        validate_payload(
            EVENT_METADATA_SCHEMA,
            {
                "status": "ok",
                "graph_mode": "shared-working",
                "latency_ms": {},
                "fresh_advisories": [{"kind": "lock_overlap"}],
            },
        )


# ---------------------------------------------------------------------------
# Live artifact validation — exercises the contract end-to-end against whatever
# btrain last wrote into this worktree. Skips cleanly when no .btrain exists
# (e.g. fresh clone in CI before any handoff has run).
# ---------------------------------------------------------------------------


def test_live_advisory_state_file_rows_conform_when_present():
    state_path = BTRAIN_ROOT / "cgraph-advisory-state.jsonl"
    if not state_path.exists() or state_path.stat().st_size == 0:
        pytest.skip(f"{state_path} missing or empty; no live advisories to validate")

    schema = load_schema(STATE_SCHEMA)
    for lineno, row in _iter_jsonl(state_path):
        try:
            validate_schema(schema, row)
        except SchemaValidationError as exc:
            raise AssertionError(
                f"{state_path}:{lineno} violates cgraph-advisory-state contract: {exc}"
            ) from exc


def test_live_telemetry_log_rows_conform_when_present():
    log_path = BTRAIN_ROOT / "logs" / "cgraph-advisories.jsonl"
    if not log_path.exists() or log_path.stat().st_size == 0:
        pytest.skip(f"{log_path} missing or empty; no telemetry rows to validate")

    schema = load_schema(LOG_SCHEMA)
    for lineno, row in _iter_jsonl(log_path):
        try:
            validate_schema(schema, row)
        except SchemaValidationError as exc:
            raise AssertionError(
                f"{log_path}:{lineno} violates cgraph-advisory-log contract: {exc}"
            ) from exc


def test_live_lane_events_cgraph_metadata_conforms_when_present():
    events_dir = BTRAIN_ROOT / "events"
    if not events_dir.is_dir():
        pytest.skip(f"{events_dir} not a directory; no lane events to validate")

    schema = load_schema(EVENT_METADATA_SCHEMA)
    seen_any = False
    for lane_file in sorted(events_dir.glob("lane-*.jsonl")):
        for lineno, event in _iter_jsonl(lane_file):
            cgraph_block = _extract_cgraph_block(event)
            if cgraph_block is None:
                continue
            seen_any = True
            try:
                validate_schema(schema, cgraph_block)
            except SchemaValidationError as exc:
                raise AssertionError(
                    f"{lane_file}:{lineno} cgraph metadata violates schema: {exc}"
                ) from exc

    if not seen_any:
        pytest.skip("no lane events carry a cgraph metadata block yet")


def test_live_artifact_layout_matches_spec_when_present():
    """Spec 005 §6: artifacts live at .btrain/artifacts/cgraph/{review-packets,audits}/lane-<id>/…"""
    root = BTRAIN_ROOT / "artifacts" / "cgraph"
    if not root.is_dir():
        pytest.skip(f"{root} not a directory; no artifacts written yet")

    for category in ("review-packets", "audits"):
        category_dir = root / category
        if not category_dir.is_dir():
            continue
        for child in category_dir.iterdir():
            assert child.is_dir(), f"{child} must be a lane-<id> directory"
            assert child.name.startswith("lane-"), (
                f"{child.name!r} does not match spec layout lane-<id>"
            )


# ---------------------------------------------------------------------------
# The two new schemas must be discoverable via schema_path() like every other
# repo-local schema so the rest of cgraph (manifest generator, replay harness)
# can rely on one uniform loader.
# ---------------------------------------------------------------------------


def test_new_schemas_are_reachable_via_schema_path():
    for name in (STATE_SCHEMA, LOG_SCHEMA):
        assert schema_path(name).is_file(), f"{name} missing from schemas/"
