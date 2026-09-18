"""Loading a discovery request from YAML."""
from __future__ import annotations

import pathlib
from typing import Any

import yaml

from .agent.loop import GoalSpec, OutputRequest
from .models.artifact import ParamSpec
from .models.enums import ParamType, Sensitivity


def load_goal(path: str | pathlib.Path) -> tuple[GoalSpec, list[str] | None]:
    """Return the goal and the outcome codes it declares (None = all)."""
    data = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    params = [
        ParamSpec(
            name=p["name"],
            type=ParamType(p.get("type", "string")),
            required=bool(p.get("required", True)),
            description=p.get("description", ""),
            sensitivity=Sensitivity(p.get("sensitivity", "internal")),
            pattern=p.get("pattern"),
            enum=p.get("enum"),
            minimum=p.get("minimum"),
            maximum=p.get("maximum"),
            example=p.get("example"),
        )
        for p in data.get("inputs") or []
    ]
    outputs = [
        OutputRequest(
            name=o["name"],
            description=o.get("description", ""),
            type=ParamType(o.get("type", "string")),
            sensitivity=Sensitivity(o.get("sensitivity", "internal")),
        )
        for o in data.get("outputs") or []
    ]
    goal = GoalSpec(
        goal=data["goal"].strip(),
        entry_url=data["entry_url"],
        capability_id=data["capability_id"],
        capability_name=data.get("name", data["capability_id"]),
        tenant=data.get("tenant", "default"),
        product=data.get("product", "meridian-core"),
        product_version=str(data.get("product_version", "unknown")),
        params=params,
        param_values={k: str(v) for k, v in (data.get("values") or {}).items()},
        secret_env={k: (v["env"] if isinstance(v, dict) else v)
                    for k, v in (data.get("secrets") or {}).items()},
        secret_descriptions={k: (v.get("description", "") if isinstance(v, dict) else "")
                             for k, v in (data.get("secrets") or {}).items()},
        outputs=outputs,
        description=(data.get("description") or "").strip(),
    )
    return goal, data.get("outcomes")


def dump_goal_summary(goal: GoalSpec) -> dict[str, Any]:
    return {
        "capability": goal.capability_id,
        "tenant": goal.tenant,
        "entry": goal.entry_url,
        "inputs": [p.name for p in goal.params],
        "outputs": [o.name for o in goal.outputs],
        "secrets": sorted(goal.secret_env),
    }
