"""Shared, dependency-free helpers for ChinaTravel's two planning objectives."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any


DEFAULT_OPTIMIZATION_GOAL = "budget_fit"
MIN_TOTAL_COST = "min_total_cost"
VALID_OPTIMIZATION_GOALS = {DEFAULT_OPTIMIZATION_GOAL, MIN_TOTAL_COST}


_NORMAL_GOAL_PATTERNS = (
    r"(?:不要|不选|取消|关闭).{0,4}(?:最便宜|最低价|最省钱)",
    r"(?:改回|恢复|使用|选择).{0,4}(?:正常|默认).{0,3}方案",
    r"正常方案|默认方案|按比例分配|尽量接近预算|靠近预算|尽量用满预算",
)

_MIN_COST_PATTERNS = (
    r"最便宜|最低价|最省钱|花费最少|费用最低|总价最低|总费用最低",
    r"尽量少花钱|尽可能少花钱|能省则省|低成本方案|经济型方案",
)


def normalize_optimization_goal(value: object) -> str:
    """Return one of the two supported values, falling back safely."""
    return value if value in VALID_OPTIMIZATION_GOALS else DEFAULT_OPTIMIZATION_GOAL


def resolve_optimization_goal(
    latest_message: str,
    current_goal: object = DEFAULT_OPTIMIZATION_GOAL,
    llm_goal: object | None = None,
) -> str:
    """Resolve a goal while keeping explicit user corrections deterministic."""
    text = (latest_message or "").strip().lower()
    if any(re.search(pattern, text) for pattern in _NORMAL_GOAL_PATTERNS):
        return DEFAULT_OPTIMIZATION_GOAL
    if any(re.search(pattern, text) for pattern in _MIN_COST_PATTERNS):
        return MIN_TOTAL_COST

    current = normalize_optimization_goal(current_goal)
    if current == MIN_TOTAL_COST:
        return current
    return normalize_optimization_goal(llm_goal)


def optimization_goal_label(value: object) -> str:
    return "最便宜方案（总花费最低）" if value == MIN_TOTAL_COST else "正常方案（预算内尽量接近预算）"


def rank_plans_by_total_cost(
    plans: Iterable[dict[str, Any]],
    optimization_goal: object,
    max_plans: int,
) -> list[dict[str, Any]]:
    """Rank prepared plans without coupling the two objective branches."""
    goal = normalize_optimization_goal(optimization_goal)
    return sorted(
        plans,
        key=lambda item: item["_total_cost"],
        reverse=goal != MIN_TOTAL_COST,
    )[:max_plans]
