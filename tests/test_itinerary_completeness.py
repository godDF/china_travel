import unittest

from chinatravel.agent.nesy_agent.day_completeness import (
    activity_fits_day_deadline,
    choose_next_activity_type,
    completion_errors,
    final_day_attraction_is_feasible,
    missing_day_items,
    required_day_items,
)


class ItineraryCompletenessTests(unittest.TestCase):
    def setUp(self):
        self.query = {"days": 3}
        self.poi_plan = {
            "go_transport": {"EndTime": "16:27"},
            "back_transport": {"BeginTime": "18:19"},
        }

    def test_requirements_match_arrival_middle_and_return_days(self):
        self.assertEqual(
            required_day_items(self.query, self.poi_plan, 0),
            {"dinner"},
        )
        self.assertEqual(
            required_day_items(self.query, self.poi_plan, 1),
            {"lunch", "dinner"},
        )
        self.assertEqual(
            required_day_items(self.query, self.poi_plan, 2),
            {"lunch", "attraction"},
        )

    def test_early_return_does_not_force_impossible_items(self):
        poi_plan = {
            "go_transport": {"EndTime": "16:27"},
            "back_transport": {"BeginTime": "10:00"},
        }
        self.assertEqual(
            required_day_items(self.query, poi_plan, 2), set()
        )

    def test_late_arrival_does_not_force_dinner(self):
        poi_plan = {
            "go_transport": {"EndTime": "20:00"},
            "back_transport": {"BeginTime": "18:19"},
        }
        self.assertNotIn(
            "dinner", required_day_items(self.query, poi_plan, 0)
        )

    def test_early_afternoon_return_still_requires_an_attraction(self):
        poi_plan = {
            "go_transport": {"EndTime": "16:27"},
            "back_transport": {"BeginTime": "13:19"},
        }
        self.assertEqual(
            required_day_items(self.query, poi_plan, 2),
            {"attraction"},
        )

    def test_last_day_activity_must_end_before_return_buffer(self):
        self.assertTrue(
            activity_fits_day_deadline(
                self.query, self.poi_plan, current_day=2, end_time="15:19"
            )
        )
        self.assertFalse(
            activity_fits_day_deadline(
                self.query, self.poi_plan, current_day=2, end_time="15:20"
            )
        )

    def test_meals_are_checked_per_day(self):
        plan = [
            {"day": 1, "activities": [{"type": "dinner"}]},
            {
                "day": 2,
                "activities": [{"type": "lunch"}, {"type": "dinner"}],
            },
            {"day": 3, "activities": [{"type": "breakfast"}]},
        ]
        self.assertEqual(
            missing_day_items(self.query, plan, self.poi_plan, current_day=2),
            {"lunch", "attraction"},
        )

    def test_completed_day_restores_terminal_eligibility(self):
        plan = [
            {"day": 1, "activities": [{"type": "dinner"}]},
            {"day": 2, "activities": []},
            {
                "day": 3,
                "activities": [
                    {"type": "breakfast"},
                    {"type": "attraction"},
                    {"type": "lunch"},
                ],
            },
        ]
        self.assertEqual(
            missing_day_items(self.query, plan, self.poi_plan, current_day=2),
            set(),
        )

    def test_final_validation_reports_incomplete_days(self):
        plan = [
            {"day": 1, "activities": []},
            {
                "day": 2,
                "activities": [{"type": "lunch"}, {"type": "dinner"}],
            },
            {"day": 3, "activities": [{"type": "lunch"}]},
        ]
        errors = completion_errors(self.query, plan, self.poi_plan)
        self.assertTrue(any("Day 1" in error and "dinner" in error for error in errors))
        self.assertTrue(
            any("Day 3" in error and "attraction" in error for error in errors)
        )

    def test_morning_attraction_is_followed_by_lunch(self):
        candidates = ["attraction", "lunch", "dinner"]
        self.assertEqual(
            choose_next_activity_type(
                candidates, {"lunch", "dinner"}, "08:30"
            ),
            "attraction",
        )
        self.assertEqual(
            choose_next_activity_type(
                candidates, {"lunch", "dinner"}, "10:20"
            ),
            "lunch",
        )

    def test_return_day_schedules_attraction_before_lunch(self):
        candidates = ["attraction", "lunch"]
        self.assertEqual(
            choose_next_activity_type(
                candidates, {"attraction", "lunch"}, "08:30"
            ),
            "attraction",
        )
        self.assertEqual(
            choose_next_activity_type(candidates, {"lunch"}, "10:30"),
            "lunch",
        )

    def test_dinner_is_not_scheduled_in_the_morning(self):
        candidates = ["attraction", "dinner"]
        self.assertEqual(
            choose_next_activity_type(candidates, {"dinner"}, "12:00"),
            "attraction",
        )
        self.assertEqual(
            choose_next_activity_type(candidates, {"dinner"}, "16:00"),
            "dinner",
        )

    def test_completed_day_chooses_terminal_activity(self):
        self.assertEqual(
            choose_next_activity_type(["hotel"], set(), "18:00"),
            "hotel",
        )
        self.assertEqual(
            choose_next_activity_type(
                ["back-intercity-transport"], set(), "13:00"
            ),
            "back-intercity-transport",
        )


class CheapestSearchPruningTests(unittest.TestCase):
    attractions = [
        {
            "name": "测试景点",
            "opentime": "09:00",
            "endtime": "17:00",
        }
    ]

    @staticmethod
    def arrival_estimator(hotel_name, attraction_name, start_time):
        return "09:10"

    def test_early_return_and_remote_hotel_are_pruned_before_dfs(self):
        query = {"days": 3, "target_city": "北京"}
        poi_plan = {
            "go_transport": {"EndTime": "16:36"},
            "back_transport": {"BeginTime": "13:19"},
            "accommodation": {"name": "机场酒店"},
        }
        self.assertFalse(
            final_day_attraction_is_feasible(
                query, poi_plan, self.attractions, self.arrival_estimator
            )
        )

    def test_later_return_keeps_the_same_hotel_combo_searchable(self):
        query = {"days": 3, "target_city": "北京"}
        poi_plan = {
            "go_transport": {"EndTime": "16:36"},
            "back_transport": {"BeginTime": "18:19"},
            "accommodation": {"name": "机场酒店"},
        }
        self.assertTrue(
            final_day_attraction_is_feasible(
                query, poi_plan, self.attractions, self.arrival_estimator
            )
        )


if __name__ == "__main__":
    unittest.main()
