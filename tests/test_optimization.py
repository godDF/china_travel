import unittest

from chinatravel.optimization import (
    DEFAULT_OPTIMIZATION_GOAL,
    MIN_TOTAL_COST,
    normalize_optimization_goal,
    rank_plans_by_total_cost,
    resolve_optimization_goal,
)
class OptimizationGoalTests(unittest.TestCase):
    def test_explicit_cheapest_request_selects_min_total_cost(self):
        self.assertEqual(
            resolve_optimization_goal("请给我总费用最低的方案"),
            MIN_TOTAL_COST,
        )

    def test_confirmation_keeps_selected_goal(self):
        self.assertEqual(
            resolve_optimization_goal("确认并开始规划", MIN_TOTAL_COST, DEFAULT_OPTIMIZATION_GOAL),
            MIN_TOTAL_COST,
        )

    def test_user_can_switch_back_to_normal(self):
        self.assertEqual(
            resolve_optimization_goal("不要最便宜了，改回正常方案", MIN_TOTAL_COST),
            DEFAULT_OPTIMIZATION_GOAL,
        )

    def test_unknown_value_falls_back_safely(self):
        self.assertEqual(normalize_optimization_goal("best_value"), DEFAULT_OPTIMIZATION_GOAL)

    def test_two_planning_branches_rank_in_opposite_directions(self):
        candidates = [
            {"_total_cost": 300},
            {"_total_cost": 100},
            {"_total_cost": 200},
        ]
        normal = rank_plans_by_total_cost(candidates, DEFAULT_OPTIMIZATION_GOAL, 3)
        self.assertEqual([plan["_total_cost"] for plan in normal], [300, 200, 100])

        cheapest = rank_plans_by_total_cost(candidates, MIN_TOTAL_COST, 3)
        self.assertEqual([plan["_total_cost"] for plan in cheapest], [100, 200, 300])


if __name__ == "__main__":
    unittest.main()
