"""Pure helpers for deciding whether each trip day is complete."""


def time_to_minutes(value):
    if not isinstance(value, str) or ":" not in value:
        return None
    try:
        hour, minute = (int(part) for part in value.split(":", 1))
    except (TypeError, ValueError):
        return None
    return hour * 60 + minute


def choose_next_activity_type(candidates, missing_items, current_time):
    """Choose the next activity type without an LLM call.

    Meals and the mandatory last-day attraction are scheduling constraints, so
    asking an LLM to choose their order makes the DFS non-deterministic and can
    repeatedly miss a service window.  Attractions remain useful as fillers
    before the next meal window; once the day's required items are complete the
    caller supplies only the terminal activity (hotel/return transport).
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    candidate_set = set(candidates)
    missing = set(missing_items)
    current_minutes = time_to_minutes(current_time)

    if "lunch" in missing and "lunch" in candidate_set:
        if (
            current_minutes is not None
            and current_minutes < 10 * 60
            and "attraction" in candidate_set
        ):
            return "attraction"
        return "lunch"

    if "attraction" in missing and "attraction" in candidate_set:
        return "attraction"

    if "dinner" in missing and "dinner" in candidate_set:
        if (
            current_minutes is not None
            and current_minutes < 16 * 60
            and "attraction" in candidate_set
        ):
            return "attraction"
        return "dinner"

    for terminal in ("hotel", "back-intercity-transport"):
        if terminal in candidate_set:
            return terminal

    return candidates[0]


def day_activity_bounds(query, poi_plan, current_day):
    """Return the usable start/deadline (minutes) for a trip day."""
    days = int(query.get("days", 0) or 0)
    if days <= 0 or current_day < 0 or current_day >= days:
        return None, None

    if current_day == 0:
        start_time = poi_plan.get("go_transport", {}).get("EndTime")
    else:
        start_time = "08:30"

    start = time_to_minutes(start_time)
    if current_day == days - 1:
        return_time = poi_plan.get("back_transport", {}).get("BeginTime")
        return_start = time_to_minutes(return_time)
        deadline = None if return_start is None else return_start - 180
    else:
        deadline = 24 * 60
    return start, deadline


def required_day_items(query, poi_plan, current_day):
    """Compute hard day-completeness requirements from the usable window."""
    start, deadline = day_activity_bounds(query, poi_plan, current_day)
    if start is None or deadline is None or deadline <= start:
        return set()

    required = set()

    def meal_fits(window_start, latest_start):
        meal_start = max(start, window_start)
        return meal_start < latest_start and meal_start + 60 <= deadline

    if meal_fits(11 * 60, 13 * 60):
        required.add("lunch")
    if meal_fits(17 * 60, 20 * 60):
        required.add("dinner")

    days = int(query.get("days", 0) or 0)
    if current_day == days - 1 and deadline - start >= 90:
        required.add("attraction")

    return required


def missing_day_items(query, plan, poi_plan, current_day):
    required = required_day_items(query, poi_plan, current_day)
    if not required:
        return set()
    if current_day < 0 or current_day >= len(plan):
        return required
    present = {
        activity.get("type")
        for activity in plan[current_day].get("activities", [])
    }
    return required - present


def activity_fits_day_deadline(query, poi_plan, current_day, end_time):
    _, deadline = day_activity_bounds(query, poi_plan, current_day)
    end_minutes = time_to_minutes(end_time)
    return (
        deadline is not None
        and end_minutes is not None
        and end_minutes <= deadline
    )


def final_day_attraction_is_feasible(
    query, poi_plan, attractions, arrival_estimator
):
    """Return whether any full 90-minute attraction can fit on the last day.

    ``arrival_estimator`` receives ``(hotel_name, attraction_name, start_time)``
    and keeps routing/data access outside this pure scheduling helper.
    """
    days = int(query.get("days", 0) or 0)
    if days <= 1:
        return True

    final_day = days - 1
    if "attraction" not in required_day_items(query, poi_plan, final_day):
        return True

    _, deadline = day_activity_bounds(query, poi_plan, final_day)
    hotel_name = poi_plan.get("accommodation", {}).get("name", "")
    if deadline is None or not hotel_name:
        return False

    day_start = "08:30"
    for attraction in attractions:
        attraction_name = attraction.get("name", "")
        arrived = time_to_minutes(
            arrival_estimator(hotel_name, attraction_name, day_start)
        )
        opens = time_to_minutes(attraction.get("opentime"))
        closes = time_to_minutes(attraction.get("endtime"))
        if arrived is None or opens is None or closes is None:
            continue

        visit_start = max(arrived, opens)
        visit_end = visit_start + 90
        if visit_end <= closes and visit_end <= deadline:
            return True

    return False


def completion_errors(query, plan, poi_plan):
    errors = []
    days = int(query.get("days", 0) or 0)
    for current_day in range(days):
        missing = missing_day_items(query, plan, poi_plan, current_day)
        if missing:
            missing_text = ", ".join(sorted(missing))
            errors.append(
                f"Day {current_day + 1} missing required items: {missing_text}"
            )
    return errors
