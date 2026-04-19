"""kkg advise: situational tip lookup for btrain integration.

Spec §4.2 — pure lookup, no graph queries.  Takes a ``situation`` key
and optional JSON context, returns a formatted suggestion with an
``advisory_id`` for telemetry correlation.

Timeout budget: 200ms (adapter-enforced; this command is fast by design).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import typer

from ..config import resolve_cgraph_config
from ..io.json_stdout import emit_json

COMMAND_NAME = "advise"
SCHEMA_FILE = "advise.json"
SUMMARY = "Advisory tip lookup: situational suggestions for btrain workflows."

# ---------------------------------------------------------------------------
# Tip table — static lookup keyed by situation
# ---------------------------------------------------------------------------

_TIP_TABLE: dict[str, dict[str, str]] = {
    "lock_overlap": {
        "suggestion": (
            "Run `kkg blast-radius --files {files} --lane {lane}` to see "
            "which transitive callers/callees overlap with other lanes."
        ),
        "rationale": (
            "git diff won't show you who calls the functions in your lock set; "
            "the graph does."
        ),
    },
    "drift": {
        "suggestion": (
            "Run `kkg drift-check --lane {lane}` to identify nodes that "
            "changed outside your lane's scope."
        ),
        "rationale": (
            "Upstream changes may have invalidated assumptions your lane "
            "depends on."
        ),
    },
    "packet_truncated": {
        "suggestion": (
            "The review packet exceeded the node cap. Consider narrowing "
            "the diff scope or increasing --max-nodes if the reviewer model "
            "can handle a larger context."
        ),
        "rationale": (
            "Truncated packets omit lower-priority nodes; the reviewer may "
            "miss cross-module impact."
        ),
    },
    "stale_index": {
        "suggestion": (
            "Run `kkg index` to rebuild the graph — some files have changed "
            "since the last indexing pass."
        ),
        "rationale": (
            "Stale graph data means blast-radius and review-packet may "
            "miss recently added callers or imports."
        ),
    },
    "untested_caller": {
        "suggestion": (
            "Functions {callers} call code you changed but have no test "
            "coverage. Consider adding tests before handoff."
        ),
        "rationale": (
            "Untested transitive callers are the #1 source of post-handoff "
            "regressions."
        ),
    },
    "audit_hard_violation": {
        "suggestion": (
            "Run `kkg audit --scope diff --require-hard-zero` to see which "
            "hard violations are blocking. Fix them before handoff."
        ),
        "rationale": (
            "Hard violations (circular imports, test imports in prod, "
            "compliance rules) block merges when enforcement is enabled."
        ),
    },
    "compliance_gap": {
        "suggestion": (
            "Run `kkg audit --profile soc2` to identify compliance gaps. "
            "Auth bypass, unlogged endpoints, and hardcoded secrets are "
            "the most common findings."
        ),
        "rationale": (
            "SOC 2 auditors need evidence that access control, monitoring, "
            "and data protection controls are in place."
        ),
    },
}


def _generate_advisory_id(situation: str, context: dict[str, Any]) -> str:
    """Generate a dedup-friendly advisory ID: ``adv_<hour>_<hash6>``."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    raw = situation + ":" + json.dumps(context, sort_keys=True)
    h = hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:6]
    return f"adv_{ts}_{h}"


def _format_tip(
    situation: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Look up the tip table and interpolate context values."""
    entry = _TIP_TABLE.get(situation)

    if entry is None:
        return {
            "situation": situation,
            "advisory_id": None,
            "suggestion": None,
            "rationale": f"Unknown situation '{situation}'; no cached analysis.",
        }

    # Interpolate context keys into suggestion/rationale (missing keys stay as {key})
    fmt = {k: str(v) for k, v in context.items()}
    suggestion = entry["suggestion"].format_map(_SafeDict(fmt))
    rationale = entry["rationale"].format_map(_SafeDict(fmt))

    return {
        "situation": situation,
        "advisory_id": _generate_advisory_id(situation, context),
        "suggestion": suggestion,
        "rationale": rationale,
    }


class _SafeDict(dict):
    """Dict subclass that returns ``{key}`` for missing format keys."""

    def __missing__(self, key: str) -> str:
        return f"{{{key}}}"


# ---------------------------------------------------------------------------
# Payload builder (pure, testable)
# ---------------------------------------------------------------------------

def build_advise_payload(
    situation: str,
    context: dict[str, Any] | None = None,
    lane: str | None = None,
) -> dict[str, Any]:
    """Build the advise response payload.

    Checks the cgraph config to see if advising is enabled for the given
    lane.  Returns a tip or a suppressed-advisory marker.
    """
    ctx = dict(context or {})
    if lane:
        ctx.setdefault("lane", lane)

    # Config check — is advise enabled for this lane?
    cfg = resolve_cgraph_config()
    if lane and lane in cfg.lanes:
        lane_cfg = cfg.lanes[lane]
        if lane_cfg.disable_advise:
            return {
                "situation": situation,
                "advisory_id": None,
                "suggestion": None,
                "rationale": f"Advise disabled for lane {lane} in project config.",
            }
        if lane_cfg.advise_on is not None and situation not in lane_cfg.advise_on:
            return {
                "situation": situation,
                "advisory_id": None,
                "suggestion": None,
                "rationale": (
                    f"Situation '{situation}' not in advise_on for lane {lane}."
                ),
            }
    elif cfg.advise_on and situation not in cfg.advise_on:
        return {
            "situation": situation,
            "advisory_id": None,
            "suggestion": None,
            "rationale": (
                f"Situation '{situation}' not in project-level advise_on."
            ),
        }

    return _format_tip(situation, ctx)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def advise_command(
    situation: str = typer.Argument(
        ...,
        help="Advisory situation key (e.g. lock_overlap, drift, packet_truncated).",
    ),
    context: Optional[str] = typer.Option(
        None,
        "--context",
        help="JSON object with situation-specific context (e.g. files, lane, callers).",
    ),
    lane: Optional[str] = typer.Option(
        None,
        "--lane",
        help="btrain lane id for per-lane config filtering.",
    ),
) -> None:
    """Look up a situational advisory tip for btrain workflows."""
    ctx: dict[str, Any] = {}
    if context:
        try:
            ctx = json.loads(context)
            if not isinstance(ctx, dict):
                raise typer.BadParameter("--context must be a JSON object")
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Invalid JSON in --context: {exc}") from exc

    payload = build_advise_payload(situation, ctx, lane)
    typer.echo(emit_json(payload))
    raise typer.Exit(code=0)
