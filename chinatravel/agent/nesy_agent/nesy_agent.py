import sys
import os
import time
import argparse
import pandas as pd
import json
import numpy as np
from numbers import Number

sys.path.append("./../../../")
project_root_path = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

if project_root_path not in sys.path:
    sys.path.insert(0, project_root_path)


from agent.base import AbstractAgent, BaseAgent
from agent.nesy_agent.utils import (
    time_compare_if_earlier_equal,
    calc_cost_from_itinerary_wo_intercity,
    add_time_delta,
    TimeOutError,
)

# from chinatravel.eval.utils import load_json_file, validate_json, save_json_file
from chinatravel.data.load_datasets import load_json_file, save_json_file
from chinatravel.agent.utils import Logger
from chinatravel.symbol_verification.commonsense_constraint import (
    func_commonsense_constraints,
)
from chinatravel.symbol_verification.hard_constraint import (
    get_symbolic_concepts,
    evaluate_constraints,
    evaluate_constraints_py,
)
from chinatravel.symbol_verification.preference import evaluate_preference_py

from chinatravel.symbol_verification.concept_func import *
from chinatravel.agent.nesy_agent.nl2sl_hybrid import nl2sl_reflect
from copy import deepcopy


class NesyAgent(BaseAgent):
    # def __init__(
    #     self,
    #     env,
    #     backbone_llm,
    #     method="NeSy",
    #     cache_dir="cache/",
    #     max_time=None,
    #     debug=True,
    #     search_width=None,
    # ):

    def __init__(self, **kwargs):
        super().__init__(name="LLMNeSy", **kwargs)

        self.max_steps = kwargs.get('max_steps', 0)

        self.debug = kwargs.get("debug", False)

        self.memory = {}

        self.TIME_CUT = kwargs.get("time_cut", 20)

        self._bt_count = 0          # backtrack counter for throttled logging
        self._bt_last_report = 0    # last reported backtrack count

        # Top-N truncation for outer loops (go × back × hotel)
        self.top_go = kwargs.get("top_go", 5)
        self.top_back = kwargs.get("top_back", 5)
        self.top_hotel = kwargs.get("top_hotel", 10)

        # Multi-plan support
        self.max_plans = kwargs.get("max_plans", 3)
        # Keep a wider candidate pool: the first three valid plans are not
        # necessarily the three closest to the user's budget.
        self.max_candidates = kwargs.get("max_candidates", 30)
        self.collected_plans = []

        # Failure diagnostics (populated during search, read by caller)
        self.failure_stats = {}
        self.min_intercity_hotel_cost = float('inf')
        self.min_cost_detail = {}  # cheapest go/back/hotel names & prices

        cache_dir = kwargs.get("cache_dir", "cache/")
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        self.cache_dir = cache_dir

        self.least_plan_schema, self.least_plan_comm = None, None
        self.method = kwargs["method"]

        print("cache dir:", self.cache_dir)
        if not os.path.exists(
            os.path.join(self.cache_dir, self.method + "_" + self.backbone_llm.name)
        ):
            os.makedirs(
                os.path.join(self.cache_dir, self.method + "_" + self.backbone_llm.name)
            )
        self.search_width = kwargs.get("search_width", None)
        self.inner_transport_width = kwargs.get("inner_transport_width", None)
        self.poi_candidate_width = kwargs.get("poi_candidate_width", None)

        self.preference_search = False
        self.prompt_upd = True

    def reset(self):
        pass

    def _bt_log(self, msg, always_show=False):
        """Throttled backtrack logging.
        In debug mode: print every message.
        In non-debug mode: only print every 100th backtrack, plus always_show messages.
        """
        self._bt_count += 1
        if self.debug:
            print(msg)
        elif always_show:
            print(f"[回溯 #{self._bt_count}] {msg}")
        elif self._bt_count - self._bt_last_report >= 100:
            print(f"[回溯] 已尝试 {self._bt_count} 个方案，正在继续搜索...")
            self._bt_last_report = self._bt_count

    def _save_partial_plan(self, query, plan):
        """Save the deepest partial plan as fallback for timeout recovery.
        Tracks the plan with most activities across all days."""
        total_acts = sum(len(d.get("activities", [])) for d in plan)
        best_acts = getattr(self, '_best_partial_acts', 0)
        if total_acts > best_acts:
            self._best_partial_acts = total_acts
            # Keep diagnostics separate from least_plan_schema. The latter is
            # reserved for a complete itinerary that reached validation; using
            # a partial DFS snapshot there caused a 3-day request to be returned
            # as a successful 2-day answer after timeout.
            self.best_partial_plan = {
                "people_number": query["people_number"],
                "start_city": query["start_city"],
                "target_city": query["target_city"],
                "itinerary": deepcopy(plan),
            }

    def _dfs_elapsed(self):
        """Pure DFS compute time, excluding LLM response waiting."""
        started_at = getattr(self, "dfs_started_at", self.time_before_search)
        llm_at_start = getattr(self, "dfs_llm_inference_start", 0)
        llm_during_dfs = max(0, self.llm_inference_time_count - llm_at_start)
        return max(0, time.time() - started_at - llm_during_dfs)

    def _dfs_timed_out(self):
        """Apply the configured deadline to traversal, not API waiting."""
        return self._dfs_elapsed() >= self.TIME_CUT

    def _plan_signature(self, plan):
        """Generate a dedup key from a plan's core POI structure.
        Only considers key activities: transport IDs, hotel name, POI names.
        Inner-city transport details are intentionally ignored."""
        parts = []
        for day_data in plan:
            for act in day_data.get("activities", []):
                t = act.get("type", "")
                if t == "train":
                    parts.append(f"train:{act.get('TrainID','')}")
                elif t == "airplane":
                    parts.append(f"flight:{act.get('FlightID','')}")
                elif t == "accommodation":
                    parts.append(f"hotel:{act.get('position','')}")
                elif t in ("attraction", "breakfast", "lunch", "dinner"):
                    parts.append(f"{t}:{act.get('position','')}")
                else:
                    parts.append(f"{t}:{act.get('position','')}")
        return "|".join(parts)


    @staticmethod
    def _plan_total_cost(plan):
        """Return the complete itinerary cost used for budget ranking."""
        total = 0
        for day_data in plan.get("itinerary", []):
            for activity in day_data.get("activities", []):
                value = activity.get("cost", 0)
                if value is None:
                    value = activity.get("price", 0)
                if isinstance(value, Number):
                    total += value
                for transport in activity.get("transports", []) or []:
                    transport_cost = transport.get("cost", 0)
                    if isinstance(transport_cost, Number):
                        total += transport_cost
        return total

    def _select_output_plans(self, plans):
        """Discard over-budget plans, then return the closest N from below."""
        ranked = []
        for candidate in plans:
            plan = deepcopy(candidate)
            plan["_total_cost"] = self._plan_total_cost(plan)
            if self.required_budget is None or plan["_total_cost"] <= self.required_budget:
                ranked.append(plan)

        ranked.sort(key=lambda item: item["_total_cost"], reverse=True)
        return ranked[:self.max_plans]

    def _budget_aware_ranking(self, preference_ranking, data, category, limit=None):
        """Mix preference candidates with prices near a category budget.

        Budget remains a hard upper bound, but a high budget should expose
        premium database entries to DFS instead of searching only the cheap
        LLM Top-N. Alternating the two rankings keeps user preferences visible.
        """
        preference = [int(index) for index in preference_ranking]
        all_indices = list(range(len(data)))
        for index in all_indices:
            if index not in preference:
                preference.append(index)

        budget = self.required_budget
        if budget is None:
            return preference[:limit] if limit is not None else preference

        days = max(1, int(self.query.get("days", 1)))
        if category == "hotel":
            target = budget * 0.35 / max(1, days - 1)
        elif category == "restaurant":
            target = budget * 0.40 / max(1, days * 2 - 1)
        else:  # attraction
            target = budget * 0.15 / max(1, days * 2)

        def price(index):
            try:
                return float(data.iloc[index]["price"])
            except (TypeError, ValueError, KeyError):
                return 0.0

        under_target = [index for index in all_indices if price(index) <= target]
        over_target = [index for index in all_indices if price(index) > target]
        under_target.sort(key=price, reverse=True)
        over_target.sort(key=price)
        price_ranking = under_target + over_target

        mixed = []
        for position in range(max(len(preference), len(price_ranking))):
            # Price-target candidate first, preference candidate second.
            for ranking in (price_ranking, preference):
                if position < len(ranking) and ranking[position] not in mixed:
                    mixed.append(ranking[position])
                    if limit is not None and len(mixed) >= limit:
                        return mixed
        return mixed

    @staticmethod
    def _prioritize_usable_return_times(ranking_back, back_info, query):
        """Put usable last-day departures before early-morning services.

        LLM rankings can contradict their own explanation and place 07:xx
        trains first. Since the outer search truncates to Top-N, that made a
        multi-day itinerary impossible even though afternoon services exist.
        Preserve all candidates, but prefer 12:00-20:30 departures closest to
        17:00 so the last day remains usable.
        """
        if query.get("days", 1) <= 1:
            return list(ranking_back)

        def minutes(value):
            try:
                hour, minute = map(int, str(value).split(":")[:2])
                return hour * 60 + minute
            except (TypeError, ValueError):
                return -1

        def key(item):
            original_position, row_index = item
            departure = minutes(back_info.iloc[row_index].get("BeginTime", ""))
            usable = 12 * 60 <= departure <= 20 * 60 + 30
            if usable:
                return (0, abs(departure - 17 * 60), original_position)
            return (1, original_position, 0)

        indexed = list(enumerate(ranking_back))
        indexed.sort(key=key)
        return [row_index for _, row_index in indexed]

    def translate_nl2sl(self, query, load_cache=False):

        llm_method = "translation_{}_reflect".format(self.backbone_llm.name)
        if not os.path.exists(os.path.join(self.cache_dir, llm_method)):
            os.makedirs(os.path.join(self.cache_dir, llm_method))

        file_path = os.path.join(
            self.cache_dir, llm_method, "{}.json".format(query["uid"])
        )

        print(file_path)

        if load_cache and os.path.exists(file_path):
            query = load_json_file(file_path)

        else:
            if self.lang == "en":
                from chinatravel.agent.nesy_agent.nl2sl_hybrid_en import nl2sl_reflect as nl2sl_reflect_en

                query = nl2sl_reflect_en(query, self.backbone_llm)
            else:
                query = nl2sl_reflect(query, self.backbone_llm, lang=self.lang)
            if "error" in query:
                query["hard_logic_py"] = {}
            save_json_file(query, file_path)

        return query

    def run(self, query, load_cache=False, oralce_translation=False, preference_search=False):

        self.preference_search = preference_search
        method_name = self.method + "_" + self.backbone_llm.name
        if oralce_translation:
            method_name = method_name + "_oracletranslation"
        if preference_search:
            method_name = method_name + "_preferencesearch"
        self.log_dir = os.path.join(self.cache_dir, method_name)
        if not os.path.exists(self.log_dir):
            os.makedirs(self.log_dir)

        sys.stdout = Logger(
            "{}/{}.log".format(
                self.log_dir, query["uid"]
            ),
            sys.stdout,
            self.debug,
        )
        sys.stderr = Logger(
            "{}/{}.error".format(
                self.log_dir, query["uid"]
            ),
            sys.stderr,
            self.debug,
        )


        self.backbone_llm.input_token_count = 0
        self.backbone_llm.output_token_count = 0
        self.backbone_llm.input_token_maxx = 0


        # natural language -> symoblic language -> plan

        if not oralce_translation:
            query = self.translate_nl2sl(query, load_cache=load_cache)


        succ, plan = self.symbolic_search(query)

        if succ:
            # Handle multi-plan output
            if "plans" in plan and isinstance(plan["plans"], list) and len(plan["plans"]) > 0:
                output_plans = self._select_output_plans(plan["plans"])
                if output_plans:
                    plan_out = {"plans": output_plans, "count": len(output_plans), "multi": True}
                else:
                    # DFS normally prunes these earlier; this final guard makes
                    # sure an over-budget candidate never reaches the user.
                    succ = False
                    plan_out = {}
            else:
                plan_out = plan
        else:
            if self.least_plan_logic is not None:
                plan_out = self.least_plan_logic

                if preference_search:
                    plan_out["preference_value"] = self.least_plan_logic_pvalue

                print("返回满足所有约束的最优方案: ", plan_out)
                succ = True

            elif self.least_plan_comm is not None:
                plan_out = self.least_plan_comm
                print(f"[超时恢复] 返回满足常识约束的部分方案 (回溯{self._bt_count}次)")
                succ = True  # Accept partial plan

            elif self.least_plan_schema is not None:
                plan_out = self.least_plan_schema
                print(f"[超时恢复] 返回基础结构的旅行方案 (回溯{self._bt_count}次)")
                succ = True  # Accept partial plan

            else:
                plan_out = {}

            plan_out["search_time_sec"] = self._dfs_elapsed()
            plan_out["llm_inference_time_sec"] = self.llm_inference_time_count
            if plan_out["search_time_sec"] > self.TIME_CUT:
                plan_out["time_out_flag"] = True


        plan_out["input_token_count"] = self.backbone_llm.input_token_count
        plan_out["output_token_count"] = self.backbone_llm.output_token_count
        plan_out["input_token_maxx"] = self.backbone_llm.input_token_maxx

        plan_out["llm_rec_count"] = self.llm_rec_count
        plan_out["llm_rec_format_error_count"] = self.llm_rec_format_error

        plan_out["search_nodes"] = self.search_nodes
        plan_out["backtrack_count"] = self.backtrack_count
        plan_out["constraints_validation_count"] = self.constraints_validation_count
        plan_out["commonsense_pass_count"] = self.commonsense_pass_count
        plan_out["logical_pass_count"] = self.logical_pass_count
        plan_out["all_constraints_pass"] = self.all_constraints_pass
        return succ, plan_out

    def constraints_validation(self, query, plan, poi_plan):

        self.constraints_validation_count += 1

        res_plan = {
            "people_number": query["people_number"],
            "start_city": query["start_city"],
            "target_city": query["target_city"],
            "itinerary": plan,
        }
        print("validate the plan [for query {}]: ".format(query["uid"]))
        print(res_plan)

        self.least_plan_schema = deepcopy(res_plan)

        bool_result = func_commonsense_constraints(query, res_plan, verbose=True)

        # if not bool_result:
        #     exit(0)

        if bool_result:
            self.commonsense_pass_count += 1

        try:
            extracted_vars = get_symbolic_concepts(query, res_plan, need_ood=False)

        except:
            extracted_vars = None

        print(extracted_vars)

        logical_result = evaluate_constraints_py(query["hard_logic_py"], res_plan, verbose=True)

        print(logical_result)

        logical_pass = True
        for idx, item in enumerate(logical_result):
            logical_pass = logical_pass and item

            if item:
                print(query["hard_logic_py"][idx], "passed!")
            else:

                print(query["hard_logic_py"][idx], "failed...")
        if bool_result and np.sum(logical_result) > self.least_plan_logical_pass:
            self.least_plan_comm = deepcopy(res_plan)
            self.least_plan_logical_pass = np.sum(logical_result)
        # if logical_result:
        #     print("Logical passed!")

        if logical_pass:
            self.logical_pass_count += 1

        bool_result = bool_result and logical_pass

        if bool_result:
            print("\n Pass! \n")
            self.all_constraints_pass += 1

            if self.least_plan_logic is None:
                self.least_plan_logic = res_plan

            if self.preference_search:
                # self.least_plan_logic = res_plan
                try:
                    if self.query["preference_opt"] == "maximize":

                        res = evaluate_preference_py([(self.query["preference_opt"], self.query["preference_concept"], self.query["preference_code"])], res_plan)[0]
                        print(self.query["preference_concept"], res)

                        # print(res, self.least_plan_logic_pvalue)
                        if res != -1 and res > self.least_plan_logic_pvalue:
                            print("preference value [{}]: {} -> {} \n update plan".format(self.query["preference_concept"], self.least_plan_logic_pvalue, res))
                            self.least_plan_logic_pvalue = res
                            self.least_plan_logic = deepcopy(res_plan)


                    elif self.query["preference_opt"] == "minimize":
                        res = evaluate_preference_py([(self.query["preference_opt"], self.query["preference_concept"], self.query["preference_code"])] , res_plan)[0]
                        print(self.query["preference_concept"], res)

                        # print(res, self.least_plan_logic_pvalue)
                        if res != -1 and res < self.least_plan_logic_pvalue:
                            print("preference value [{}]: {} -> {} \n update plan".format(self.query["preference_concept"], self.least_plan_logic_pvalue, res))
                            self.least_plan_logic_pvalue = res
                            self.least_plan_logic = deepcopy(res_plan)

                    else:
                        raise ValueError("Invalid preference_opt")
                    print(self.least_plan_logic)
                except Exception as e:
                    print(e)
                    print(self.query["preference_code"])
        else:
            print("\n Failed \n")

        # plan = res_plan

        # print(result)
        # exit(0)

        if self.preference_search:
            return False, plan

        if bool_result:
            res_plan["search_time_sec"] = self._dfs_elapsed()
            res_plan["llm_inference_time_sec"] = self.llm_inference_time_count
            # Multi-plan: dedup before collecting
            sig = self._plan_signature(plan)
            if sig in getattr(self, '_seen_signatures', set()):
                print(f"方案去重: 跳过重复方案")
                return False, plan
            self._seen_signatures.add(sig)
            self.collected_plans.append(deepcopy(res_plan))
            print(f"方案候选收集: {len(self.collected_plans)}/{self.max_candidates} (新方案)")
            if len(self.collected_plans) >= self.max_candidates:
                return True, res_plan  # Enough plans, stop search
            else:
                # Continue around this terminal branch. Nearby POI choices are
                # much cheaper to enumerate than restarting from the next
                # go/back/hotel combination for every additional plan.
                return False, plan
        else:
            return False, plan

    def add_intercity_transport(
        self, activities, intercity_info, innercity_transports=[], tickets=1
    ):
        activity_i = {
            "start_time": intercity_info["BeginTime"],
            "end_time": intercity_info["EndTime"],
            "start": intercity_info["From"],
            "end": intercity_info["To"],
            "price": intercity_info["Cost"],
            "cost": intercity_info["Cost"] * tickets,
            "tickets": tickets,
            "transports": innercity_transports,
        }
        if not pd.isna(intercity_info["TrainID"]):
            activity_i["TrainID"] = intercity_info["TrainID"]
            activity_i["type"] = "train"
        elif not pd.isna(intercity_info["FlightID"]):
            activity_i["FlightID"] = intercity_info["FlightID"]
            activity_i["type"] = "airplane"

        activities.append(activity_i)
        return activities

    def add_poi(
        self,
        activities,
        position,
        poi_type,
        price,
        cost,
        start_time,
        end_time,
        innercity_transports,
    ):
        activity_i = {
            "position": position,
            "type": poi_type,
            "price": price,
            "cost": cost,
            "start_time": start_time,
            "end_time": end_time,
            "transports": innercity_transports,
        }

        activities.append(activity_i)
        return activities

    def add_accommodation(
        self,
        current_plan,
        hotel_sel,
        current_day,
        arrived_time,
        required_rooms,
        transports_sel,
    ):

        current_plan[current_day]["activities"] = self.add_poi(
            activities=current_plan[current_day]["activities"],
            position=hotel_sel["name"],
            poi_type="accommodation",
            price=int(hotel_sel["price"]),
            cost=int(hotel_sel["price"]) * required_rooms,
            start_time=arrived_time,
            end_time="24:00",
            innercity_transports=transports_sel,
        )
        current_plan[current_day]["activities"][-1]["room_type"] = hotel_sel["numbed"]
        current_plan[current_day]["activities"][-1]["rooms"] = required_rooms

        return current_plan

    def add_restaurant(
        self, current_plan, poi_type, poi_sel, current_day, arrived_time, transports_sel
    ):

        # 开放时间
        opentime, endtime = (
            poi_sel["opentime"],
            poi_sel["endtime"],
        )

        # it is closed ...
        if time_compare_if_earlier_equal(endtime, arrived_time):
            raise Exception("Add POI error")
        if time_compare_if_earlier_equal(arrived_time, opentime):
            act_start_time = opentime
        else:
            act_start_time = arrived_time

        if poi_type == "lunch" and time_compare_if_earlier_equal(
            act_start_time, "11:00"
        ):
            act_start_time = "11:00"
        if poi_type == "lunch" and time_compare_if_earlier_equal(endtime, "11:00"):
            raise Exception("Add POI error")

        if poi_type == "dinner" and time_compare_if_earlier_equal(
            act_start_time, "17:00"
        ):
            act_start_time = "17:00"
        if poi_type == "dinner" and time_compare_if_earlier_equal(endtime, "17:00"):
            raise Exception("Add POI error")

        if poi_type == "lunch" and time_compare_if_earlier_equal(
            "13:00", act_start_time
        ):
            raise Exception("Add POI error")
        if poi_type == "dinner" and time_compare_if_earlier_equal(
            "20:00", act_start_time
        ):
            raise Exception("Add POI error")

        poi_time = 60
        act_end_time = add_time_delta(act_start_time, poi_time)
        if time_compare_if_earlier_equal(endtime, act_end_time):
            act_end_time = endtime

        tmp_plan = deepcopy(current_plan)
        tmp_plan[current_day]["activities"] = self.add_poi(
            activities=tmp_plan[current_day]["activities"],
            position=poi_sel["name"],
            poi_type=poi_type,
            price=int(poi_sel["price"]),
            cost=int(poi_sel["price"]) * self.query["people_number"],
            start_time=act_start_time,
            end_time=act_end_time,
            innercity_transports=transports_sel,
        )
        return tmp_plan

    def add_attraction(
        self, current_plan, poi_type, poi_sel, current_day, arrived_time, transports_sel
    ):

        # 开放时间
        opentime, endtime = (
            poi_sel["opentime"],
            poi_sel["endtime"],
        )

        # it is closed ...

        opentime, endtime = poi_sel["opentime"], poi_sel["endtime"]
        # it is closed ...
        if time_compare_if_earlier_equal(endtime, arrived_time):
            raise Exception("Add POI error")

        if time_compare_if_earlier_equal(arrived_time, opentime):
            act_start_time = opentime
        else:
            act_start_time = arrived_time

        poi_time = 90
        act_end_time = add_time_delta(act_start_time, poi_time)
        if time_compare_if_earlier_equal(endtime, act_end_time):
            act_end_time = endtime

        tmp_plan = deepcopy(current_plan)
        tmp_plan[current_day]["activities"] = self.add_poi(
            activities=tmp_plan[current_day]["activities"],
            position=poi_sel["name"],
            poi_type=poi_type,
            price=int(poi_sel["price"]),
            cost=int(poi_sel["price"]) * self.query["people_number"],
            start_time=act_start_time,
            end_time=act_end_time,
            innercity_transports=transports_sel,
        )
        tmp_plan[current_day]["activities"][-1]["tickets"] = self.query["people_number"]

        return tmp_plan

    def check_if_too_late(
        self, query, current_day, current_time, current_position, poi_plan
    ):

        if current_time != "" and time_compare_if_earlier_equal("23:00", current_time):
            self._bt_log("too late, after 23:00")
            return True

        if current_time != "" and current_day == query["days"] - 1:
            # We should go back in time ...
            transports_ranking = self.innercity_transports_ranking_from_query

            for transport_type_sel in transports_ranking:

                self.search_nodes += 1
                flag = True
                if "back_transport" in poi_plan:
                    transports_sel = self.collect_innercity_transport(
                        query["target_city"],
                        current_position,
                        poi_plan["back_transport"]["From"],
                        current_time,
                        transport_type_sel,
                    )
                    if not isinstance(transports_sel, list):
                        self.backtrack_count += 1
                        self._bt_log("inner-city transport error, backtrack...")
                        continue

                    if len(transports_sel) > 0:
                        arrived_time = transports_sel[-1]["end_time"]
                    else:
                        arrived_time = current_time

                    if not time_compare_if_earlier_equal(
                        poi_plan["back_transport"]["BeginTime"], arrived_time
                    ):
                        flag = False
                if flag:
                    self._bt_log(
                        "Can not go back source-city in time, current POI {}, station arrived time: {}".format(
                            current_position, arrived_time
                        )
                    )
                    return True

        elif current_time != "":
            if "accommodation" in poi_plan:
                hotel_sel = poi_plan["accommodation"]
                transports_ranking = self.innercity_transports_ranking_from_query

                for transport_type_sel in transports_ranking:
                    self.search_nodes += 1
                    flag = True
                    if "back_transport" in poi_plan:
                        transports_sel = self.collect_innercity_transport(
                            query["target_city"],
                            current_position,
                            hotel_sel["name"],
                            current_time,
                            transport_type_sel,
                        )
                        if not isinstance(transports_sel, list):
                            self.backtrack_count += 1
                            self._bt_log("inner-city transport error, backtrack...")
                            continue

                        flag = True

                        if len(transports_sel) > 0:
                            arrived_time = transports_sel[-1]["end_time"]
                        else:
                            arrived_time = current_time
                        if not time_compare_if_earlier_equal("24:00", arrived_time):
                            flag = False
                    if flag:
                        self._bt_log(
                            "Can not go back to hotel, current POI {}, hotel arrived time: {}".format(
                                current_position, arrived_time
                            )
                        )
                        return True

        return False

    def forward_check(self, query, plan, poi_plan, current_day, current_time, current_position):
        """
        前向检查：在继续搜索之前，快速判断当前状态是否还有可能找到可行解。
        如果不可能，立即返回 False 触发回溯，避免浪费搜索。

        检查维度：
        1. 预算：剩余预算是否还能覆盖最低成本的必做事项
        2. 时间：最后一天剩余时间是否足够完成必做事项 + 回程交通
        3. 天数：非最后一天且无酒店安排时，检查是否能赶上酒店

        返回 True 表示可能可行，False 表示一定不可行（直接回溯）。
        """
        # ---- 1. 统计当天还剩哪些必做事项 ----
        haved_lunch = any(
            a["type"] == "lunch" for d in plan for a in d["activities"]
        )
        haved_dinner = any(
            a["type"] == "dinner" for d in plan for a in d["activities"]
        )
        is_last_day = (current_day == query["days"] - 1)

        remaining_items = 0
        if not haved_lunch and current_time != "" and current_time != "00:00":
            remaining_items += 1  # 还没吃午饭
        if not haved_dinner and current_time != "" and current_time != "00:00":
            remaining_items += 1  # 还没吃晚饭
        if is_last_day and current_time != "" and "back_transport" in poi_plan:
            remaining_items += 1  # 还需要回程交通

        if remaining_items == 0:
            return True  # 没有必做事项了，不需要检查

        # ---- 2. 预算前向检查 ----
        if self.required_budget is not None:
            current_cost = 0
            for day_activities in plan:
                for activity in day_activities["activities"]:
                    if activity["type"] in ["breakfast", "lunch", "dinner", "attraction"]:
                        current_cost += activity.get("cost", 0)

            # 保守估算：每项至少 50 元（餐费）+ 每次市内交通至少 0 元（步行）
            MIN_COST_PER_ITEM = 30
            MIN_TRANSPORT_PER_ITEM = 0
            min_remaining_cost = remaining_items * (MIN_COST_PER_ITEM + MIN_TRANSPORT_PER_ITEM)

            if current_cost + self.intercity_with_hotel_cost + min_remaining_cost > self.required_budget:
                self.backtrack_count += 1
                self._bt_log(
                    "前向检查失败：预算不足 (已花{} + 城际酒店{} + 最低剩余{} > 预算{})".format(
                        current_cost, self.intercity_with_hotel_cost, min_remaining_cost, self.required_budget
                    )
                )
                return False

        # ---- 3. 时间前向检查（最后一天时间紧迫） ----
        if is_last_day and current_time != "":
            MIN_MINUTES_PER_ITEM = 60  # 每项至少 60 分钟（含交通）
            deadline = "21:00"  # 回程前需要完成的最后时间

            hour, minute = map(int, current_time.split(":"))
            current_minutes = hour * 60 + minute
            d_hour, d_minute = map(int, deadline.split(":"))
            deadline_minutes = d_hour * 60 + d_minute

            remaining_time = deadline_minutes - current_minutes
            needed_time = remaining_items * MIN_MINUTES_PER_ITEM

            if remaining_time < needed_time:
                self.backtrack_count += 1
                self._bt_log(
                    "前向检查失败：时间不足 (剩余{}分钟 < 需要{}分钟, {}/{}项)".format(
                        remaining_time, needed_time, remaining_items,
                        "午" if not haved_lunch else "" + "晚" if not haved_dinner else "" + "回程" if is_last_day else ""
                    )
                )
                return False

        return True

    def reranking_intercity_transport_go_with_constraints(
        self, ranking_go, go_info, query
    ):

        ### check constraints
        pass_num_list = np.zeros(len(go_info))

        for go_i in ranking_go:

            go_sel = go_info.iloc[go_i]
            tmp_plan = [{"day": 1, "activities": []}]
            tmp_plan[0]["activities"] = self.add_intercity_transport(
                tmp_plan[0]["activities"],
                go_sel,
                innercity_transports=[],
                tickets=self.query["people_number"],
            )

            res_plan = {
                "people_number": query["people_number"],
                "start_city": query["start_city"],
                "target_city": query["target_city"],
                "itinerary": tmp_plan,
            }
            # print("validate the plan [for query {}]: ".format(query["uid"]))
            # print(res_plan)

            logical_result = evaluate_constraints_py(query["hard_logic_py"], res_plan)

            # print(logical_result)

            pass_num_list[go_i] = np.sum(logical_result)

        pass_maxx = int(np.max(pass_num_list))

        # print(pass_num_list)
        # print(pass_maxx)

        reranking_list = []
        if pass_maxx > 0:
            for p_i in range(pass_maxx, -1, -1):
                for idx in ranking_go:
                    if pass_num_list[idx] == p_i:
                        reranking_list.append(idx)
        else:
            reranking_list = ranking_go

        # print(reranking_list)
        # exit(0)
        return reranking_list

    def reranking_intercity_transport_back_with_constraints(
        self, ranking_back, back_info, query, go_sel
    ):

        ### check constraints
        pass_num_list = np.zeros(len(back_info))

        for back_i in ranking_back:

            back_sel = back_info.iloc[back_i]
            tmp_plan = [{"day": 1, "activities": []}]
            tmp_plan[0]["activities"] = self.add_intercity_transport(
                tmp_plan[0]["activities"],
                go_sel,
                innercity_transports=[],
                tickets=self.query["people_number"],
            )
            if query["days"] > 1:
                for dayy in range(1, query["days"]):
                    tmp_plan.append({"day": dayy + 1, "activities": []})
            tmp_plan[-1]["activities"] = self.add_intercity_transport(
                tmp_plan[-1]["activities"],
                back_sel,
                innercity_transports=[],
                tickets=self.query["people_number"],
            )

            res_plan = {
                "people_number": query["people_number"],
                "start_city": query["start_city"],
                "target_city": query["target_city"],
                "itinerary": tmp_plan,
            }
            # print("validate the plan [for query {}]: ".format(query["uid"]))
            # print(res_plan)

            logical_result = evaluate_constraints_py(query["hard_logic_py"], res_plan)

            # print(logical_result)

            pass_num_list[back_i] = np.sum(logical_result)

        pass_maxx = int(np.max(pass_num_list))

        # print(pass_num_list)
        # print(pass_maxx)

        reranking_list = []
        if pass_maxx > 0:
            for p_i in range(pass_maxx, -1, -1):
                for idx in ranking_back:
                    if pass_num_list[idx] == p_i:
                        reranking_list.append(idx)
        else:
            reranking_list = ranking_back

        # print(reranking_list)
        # exit(0)
        return reranking_list

    def reranking_hotel_with_constraints(
        self, ranking_hotel, hotel_info, query, query_room_number
    ):

        pass_num_list = np.zeros(len(hotel_info))
        ### check constraints

        for idx in range(len(hotel_info)):
            hotel_sel = hotel_info.iloc[idx]

            if query_room_number == None:
                room_type = hotel_sel["numbed"]
                required_rooms = int((query["people_number"] - 1) / room_type) + 1
            else:
                required_rooms = query_room_number

            plan = []
            for dayy in range(query["days"] - 1):
                plan.append({"day": dayy + 1, "activities": []})
                plan = self.add_accommodation(
                    current_plan=plan,
                    hotel_sel=hotel_sel,
                    current_day=dayy,
                    arrived_time="20:00",
                    required_rooms=required_rooms,
                    transports_sel=[],
                )

            res_plan = {
                "people_number": query["people_number"],
                "start_city": query["start_city"],
                "target_city": query["target_city"],
                "itinerary": plan,
            }
            # print("validate the plan [for query {}]: ".format(query["uid"]))
            # print(res_plan)

            logical_result = evaluate_constraints_py(query["hard_logic_py"], res_plan)

            # print(logical_result)

            pass_num_list[idx] = np.sum(logical_result)

        pass_maxx = int(np.max(pass_num_list))

        # print(pass_num_list)
        # print(pass_maxx)

        reranking_list = []
        if pass_maxx > 0:
            for p_i in range(pass_maxx, -1, -1):
                for idx in ranking_hotel:
                    if pass_num_list[idx] == p_i:
                        reranking_list.append(idx)
        else:
            reranking_list = ranking_hotel

        # for r_i in reranking_list[:10]:
        #     print(hotel_info.iloc[r_i])
        #     print(pass_num_list[r_i])

        # print(reranking_list)
        # exit(0)
        return reranking_list

    def reranking_restaurants_with_constraints(
        self,
        plan,
        poi_type,
        current_day,
        current_time,
        current_position,
        rest_info,
        query,
        ranking_restaurants,
    ):

        candidate_indices = [int(index) for index in ranking_restaurants]
        if self.poi_candidate_width is not None:
            candidate_indices = candidate_indices[:self.poi_candidate_width]
        pass_scores = {}
        ### check constraints

        for idx in candidate_indices:
            if self._dfs_timed_out():
                raise TimeOutError
            poi_sel = rest_info.iloc[idx]
            self.search_nodes += 1
            if current_position == poi_sel["name"]:
                transports_sel = []
                arrived_time = current_time
            else:

                transports_sel = self.collect_innercity_transport(
                    query["target_city"],
                    current_position,
                    poi_sel["name"],
                    current_time,
                    "taxi",
                )
                if not isinstance(transports_sel, list):
                    self.backtrack_count += 1
                    self._bt_log("inner-city transport error, backtrack...")
                    continue

                if len(transports_sel) == 0:
                    arrived_time = current_time
                else:
                    arrived_time = transports_sel[-1]["end_time"]


            try:
                tmp_plan = self.add_restaurant(
                    plan, poi_type, poi_sel, current_day, arrived_time, transports_sel
                )
                res_plan = {
                    "people_number": query["people_number"],
                    "start_city": query["start_city"],
                    "target_city": query["target_city"],
                    "itinerary": tmp_plan,
                }
                logical_result = evaluate_constraints_py(
                    query["hard_logic_py"], res_plan
                )
                pass_scores[idx] = np.sum(logical_result)
            except:
                pass_scores[idx] = 0

            # print(logical_result)
            # pass_num_list.append(np.sum(logical_result))

        pass_maxx = max(pass_scores.values(), default=0)

        # print(pass_num_list)
        # print(pass_maxx)

        reranking_list = []
        if pass_maxx > 0:
            for p_i in range(pass_maxx, -1, -1):
                for idx in candidate_indices:
                    if pass_scores.get(idx, 0) == p_i:
                        reranking_list.append(idx)
        else:
            reranking_list = candidate_indices

        # print(reranking_list)
        # exit(0)

        # for r_i in ranking_restaurants[:10]:
        #     print(rest_info.iloc[r_i])

        # print("re-ranking ---")
        # for r_i in reranking_list[:10]:
        #     print(rest_info.iloc[r_i])

        return reranking_list

    def reranking_attractions_with_constraints(
        self,
        plan,
        poi_type,
        current_day,
        current_time,
        current_position,
        attr_info,
        query,
        ranking_attractions,
    ):

        candidate_indices = [int(index) for index in ranking_attractions]
        if self.poi_candidate_width is not None:
            candidate_indices = candidate_indices[:self.poi_candidate_width]
        pass_scores = {}
        ### check constraints

        for idx in candidate_indices:
            if self._dfs_timed_out():
                raise TimeOutError
            poi_sel = attr_info.iloc[idx]
            self.search_nodes += 1
            if poi_sel["name"] == current_position:
                transports_sel = []
                arrived_time = current_time
            else:
                transports_sel = self.collect_innercity_transport(
                    query["target_city"],
                    current_position,
                    poi_sel["name"],
                    current_time,
                    "taxi",
                )
                if not isinstance(transports_sel, list):
                    self.backtrack_count += 1
                    self._bt_log("inner-city transport error, backtrack...")
                    continue

                if len(transports_sel) == 0:
                    arrived_time = current_time
                else:
                    arrived_time = transports_sel[-1]["end_time"]


            try:
                tmp_plan = self.add_attraction(
                    plan, poi_type, poi_sel, current_day, arrived_time, transports_sel
                )
                res_plan = {
                    "people_number": query["people_number"],
                    "start_city": query["start_city"],
                    "target_city": query["target_city"],
                    "itinerary": tmp_plan,
                }
                logical_result = evaluate_constraints_py(
                    query["hard_logic_py"], res_plan
                )
                pass_scores[idx] = np.sum(logical_result)
            except:
                pass_scores[idx] = 0
        pass_maxx = max(pass_scores.values(), default=0)

        # print(pass_num_list)
        # print(pass_maxx)

        reranking_list = []
        if pass_maxx > 0:
            for p_i in range(pass_maxx, -1, -1):
                for idx in candidate_indices:
                    if pass_scores.get(idx, 0) == p_i:
                        reranking_list.append(idx)
        else:
            reranking_list = candidate_indices

        # print(reranking_list)
        # exit(0)
        return reranking_list

    def dfs_poi(
        self, query, poi_plan, plan, current_time, current_position, current_day=0
    ):

        self.search_nodes += 1

        # Fast exit: skip remaining search within current combo
        # when a unique plan has been collected and we need to try a different combo
        if getattr(self, '_skip_to_next_combo', False):
            return False, plan

        # Save partial plan snapshot for timeout recovery
        self._save_partial_plan(query, plan)

        if self._dfs_timed_out():

            raise TimeOutError

        if self.check_if_too_late(
            query, current_day, current_time, current_position, poi_plan
        ):
            self.backtrack_count += 1
            self._bt_log("The current time is too late to go hotel or back-transport, backtrack...")
            return False, plan

        if self.required_budget != None:
            total_cost = 0
            for day_activities in plan:
                for activity in day_activities["activities"]:
                    if activity["type"] in [
                        "breakfast",
                        "lunch",
                        "dinner",
                        "attraction",
                    ]:
                        total_cost += activity["cost"]

            if total_cost + self.intercity_with_hotel_cost > self.required_budget:
                self.backtrack_count += 1
                self._bt_log("budget exceeded, backtrack...")
                return False, plan

            # 前向检查：提前发现不可行状态，避免深入搜索后才发现失败
            if not self.forward_check(query, plan, poi_plan, current_day, current_time, current_position):
                return False, plan

        # intercity_transport - go
        if current_day == 0 and current_time == "":
            plan = [{"day": current_day + 1, "activities": []}]
            plan[current_day]["activities"] = self.add_intercity_transport(
                plan[current_day]["activities"],
                poi_plan["go_transport"],
                innercity_transports=[],
                tickets=self.query["people_number"],
            )
            new_time = poi_plan["go_transport"]["EndTime"]
            new_position = poi_plan["go_transport"]["To"]
            success, plan = self.dfs_poi(
                query, poi_plan, plan, new_time, new_position, current_day
            )
            if success:
                return True, plan
            else:
                self.backtrack_count += 1
                self._bt_log("No solution for the given Go Transport, backtrack...")
                return False, plan

        # breakfast
        if current_time == "00:00":

            if len(plan) < current_day + 1:
                plan.append({"day": current_day + 1, "activities": []})

            self.search_nodes += 1
            plan = self.select_and_add_breakfast(
                plan, poi_plan, current_day, current_time, current_position
            )

            new_time = plan[current_day]["activities"][-1]["end_time"]
            new_position = current_position
            success, plan = self.dfs_poi(
                query, poi_plan, plan, new_time, new_position, current_day
            )
            if success:
                return True, plan

            plan[current_day]["activities"].pop()

            candidates_type = []
            if current_day == query["days"] - 1 and current_time != "":
                candidates_type.append("back-intercity-transport")
            else:

                self.backtrack_count += 1
                self._bt_log("No solution for the given Breakfast, backtrack...")

                return False, plan

        else:
            haved_lunch_today, haved_dinner_today = False, False

            for act_i in plan[current_day]["activities"]:
                if act_i["type"] == "lunch":
                    haved_lunch_today = True
                if act_i["type"] == "dinner":
                    haved_dinner_today = True

            candidates_type = ["attraction"]
            if not haved_lunch_today:
                candidates_type.append("lunch")
            if not haved_dinner_today:
                candidates_type.append("dinner")
            if ("accommodation" in poi_plan) and (current_day < query["days"] - 1):
                candidates_type.append("hotel")
            if current_day == query["days"] - 1 and current_time != "":
                candidates_type.append("back-intercity-transport")

        print("candidates_type: ", candidates_type)

        while len(candidates_type) > 0:

            poi_type, candidates_type = self.select_next_poi_type(
                candidates_type,
                plan,
                poi_plan,
                current_day,
                current_time,
                current_position,
            )

            print(
                "POI planning, day {} {}, {}, next-poi type: {}".format(
                    current_day, current_time, current_position, poi_type
                )
            )

            if poi_type == "back-intercity-transport":

                if len(plan) < current_day + 1:
                    plan.append({"day": current_day + 1, "activities": []})

                # transports_ranking = self.ranking_innercity_transport(current_position, poi_plan["back_transport"]["From"], current_day, current_time)
                transports_ranking = self.innercity_transports_ranking_from_query
                for trans_type_sel in transports_ranking:
                    self.search_nodes += 1
                    transports_sel = self.collect_innercity_transport(
                        query["target_city"],
                        current_position,
                        poi_plan["back_transport"]["From"],
                        current_time,
                        trans_type_sel,
                    )
                    if not isinstance(transports_sel, list):
                        self.backtrack_count += 1
                        self._bt_log("inner-city transport error, backtrack...")
                        continue

                    plan[current_day]["activities"] = self.add_intercity_transport(
                        plan[current_day]["activities"],
                        poi_plan["back_transport"],
                        innercity_transports=transports_sel,
                        tickets=self.query["people_number"],
                    )

                    res_bool, res_plan = self.constraints_validation(
                        query, plan, poi_plan
                    )

                    if res_bool:
                        return True, res_plan
                    else:
                        plan[current_day]["activities"].pop()
                        self.backtrack_count += 1
                        self._bt_log("Back-transport, but constraints_validation failed, backtrack...")
                        return False, plan
            elif poi_type == "hotel":

                hotel_sel = poi_plan["accommodation"]

                # transports_ranking = self.ranking_innercity_transport(current_position, hotel_sel["name"], current_day, current_time)
                transports_ranking = self.innercity_transports_ranking_from_query

                for trans_type_sel in transports_ranking:
                    self.search_nodes += 1
                    if hotel_sel["name"] == current_position:
                        transports_sel = []
                        arrived_time = current_time
                    else:
                        transports_sel = self.collect_innercity_transport(
                            query["target_city"],
                            current_position,
                            hotel_sel["name"],
                            current_time,
                            trans_type_sel,
                        )
                        if not isinstance(transports_sel, list):
                            self.backtrack_count += 1
                            self._bt_log("inner-city transport error, backtrack...")
                            continue

                        if len(transports_sel) == 0:
                            arrived_time = current_time
                        else:
                            arrived_time = transports_sel[-1]["end_time"]

                    plan = self.add_accommodation(
                        current_plan=plan,
                        hotel_sel=hotel_sel,
                        current_day=current_day,
                        arrived_time=arrived_time,
                        required_rooms=self.required_rooms,
                        transports_sel=transports_sel,
                    )

                    new_time = "00:00"
                    new_position = hotel_sel["name"]

                    success, plan = self.dfs_poi(
                        query, poi_plan, plan, new_time, new_position, current_day + 1
                    )

                    if success:
                        return True, plan

                    self.backtrack_count += 1
                    self._bt_log("Fail with the given accommodation activity, backtrack...")

                    plan[current_day]["activities"].pop()
            elif poi_type in ["lunch", "dinner", "attraction"]:

                if poi_type in ["lunch", "dinner"]:

                    # print(poi_info["restaurants"])
                    ranking_idx = self.ranking_restaurants(
                        plan,
                        poi_plan,
                        current_day,
                        current_time,
                        current_position,
                        self.intercity_with_hotel_cost,
                    )
                    ranking_idx = self.reranking_restaurants_with_constraints(
                        plan,
                        poi_type,
                        current_day,
                        current_time,
                        current_position,
                        self.memory["restaurants"],
                        query,
                        ranking_idx,
                    )

                    for sea_i, r_i in enumerate(ranking_idx):

                        # Check timeout in restaurant loop
                        if self._dfs_timed_out():
                            raise TimeOutError

                        if self.search_width != None and sea_i >= self.search_width:
                            print(
                                "Out of search_width [{}], break".format(
                                    self.search_width
                                )
                            )
                            break

                        res_idx = r_i

                        if not (res_idx in self.restaurants_visiting):

                            if res_idx < 0 or res_idx >= len(
                                self.memory["restaurants"]
                            ):
                                print("index error: ", res_idx, len(self.memory["restaurants"]))

                            poi_sel = self.memory["restaurants"].iloc[res_idx]

                            # transports_ranking = self.ranking_innercity_transport(current_position, poi_sel["name"], current_day, current_time)
                            transports_ranking = (
                                self.innercity_transports_ranking_from_query
                            )

                            for trans_type_sel in transports_ranking:
                                self.search_nodes += 1
                                transports_sel = self.collect_innercity_transport(
                                    query["target_city"],
                                    current_position,
                                    poi_sel["name"],
                                    current_time,
                                    trans_type_sel,
                                )
                                if not isinstance(transports_sel, list):
                                    self.backtrack_count += 1
                                    self._bt_log("inner-city transport error, backtrack...")
                                    continue

                                if len(transports_sel) == 0:
                                    arrived_time = current_time
                                else:
                                    arrived_time = transports_sel[-1]["end_time"]


                                try:
                                    plan = self.add_restaurant(
                                        plan,
                                        poi_type,
                                        poi_sel,
                                        current_day,
                                        arrived_time,
                                        transports_sel,
                                    )
                                except:
                                    self.backtrack_count += 1
                                    self._bt_log("add_restaurant failed, backtrack...")
                                    continue

                                new_time = plan[current_day]["activities"][-1][
                                    "end_time"
                                ]
                                new_position = poi_sel["name"]
                                self.restaurants_visiting.append(res_idx)
                                self.food_type_visiting.append(poi_sel["cuisine"])
                                success, plan = self.dfs_poi(
                                    query,
                                    poi_plan,
                                    plan,
                                    new_time,
                                    new_position,
                                    current_day,
                                )
                                if success:
                                    return True, plan

                                self.backtrack_count += 1
                                self._bt_log("add_restaurant failed, backtrack...")

                                plan[current_day]["activities"].pop()
                                self.restaurants_visiting.pop()
                                self.food_type_visiting.pop()

                                # print("res {} fail...".format(poi_sel["name"]))

                elif poi_type == "attraction":
                    ranking_idx = self.ranking_attractions(
                        plan,
                        poi_plan,
                        current_day,
                        current_time,
                        current_position,
                        self.intercity_with_hotel_cost,
                    )

                    ranking_idx = self.reranking_attractions_with_constraints(
                        plan,
                        poi_type,
                        current_day,
                        current_time,
                        current_position,
                        self.memory["attractions"],
                        query,
                        ranking_idx,
                    )

                    for sea_i, r_i in enumerate(ranking_idx):

                        # Check timeout in attraction loop
                        if self._dfs_timed_out():
                            raise TimeOutError

                        if self.search_width != None and sea_i >= self.search_width:
                            print(
                                "Out of search_width [{}], break".format(
                                    self.search_width
                                )
                            )
                            break
                        self.search_nodes += 1
                        attr_idx = r_i
                        if not (attr_idx in self.attractions_visiting):

                            if attr_idx < 0 or attr_idx >= len(
                                self.memory["attractions"]
                            ):
                                print(attr_idx, len(self.memory["attractions"]))

                            poi_sel = self.memory["attractions"].iloc[attr_idx]
                            # print(current_position, poi_sel["name"])

                            # transports_ranking = self.ranking_innercity_transport(current_position, poi_sel["name"], current_day, current_time)
                            transports_ranking = (
                                self.innercity_transports_ranking_from_query
                            )
                            for trans_type_sel in transports_ranking:
                                self.search_nodes += 1
                                transports_sel = self.collect_innercity_transport(
                                    query["target_city"],
                                    current_position,
                                    poi_sel["name"],
                                    current_time,
                                    trans_type_sel,
                                )
                                if not isinstance(transports_sel, list):
                                    self.backtrack_count += 1
                                    self._bt_log("inner-city transport error, backtrack...")
                                    continue
                                if len(transports_sel) == 0:
                                    arrived_time = current_time
                                else:
                                    arrived_time = transports_sel[-1]["end_time"]

                                opentime, endtime = (
                                    poi_sel["opentime"],
                                    poi_sel["endtime"],
                                )
                                # too late
                                if time_compare_if_earlier_equal("21:00", arrived_time):
                                    self.backtrack_count += 1
                                    self._bt_log("The current time is too late...")
                                    continue

                                # it is closed ...
                                if time_compare_if_earlier_equal(endtime, arrived_time):
                                    self.backtrack_count += 1
                                    self._bt_log("The attraction is closed now...")
                                    continue

                                if time_compare_if_earlier_equal(
                                    arrived_time, opentime
                                ):
                                    act_start_time = opentime
                                else:
                                    act_start_time = arrived_time

                                poi_time = self.select_poi_time(
                                    plan,
                                    poi_plan,
                                    current_day,
                                    act_start_time,
                                    poi_sel["name"],
                                    poi_type,
                                    recommended_visit_time=poi_sel["recommendmintime"]
                                    * 60,
                                )
                                act_end_time = add_time_delta(act_start_time, poi_time)
                                if time_compare_if_earlier_equal(endtime, act_end_time):
                                    act_end_time = endtime

                                plan[current_day]["activities"] = self.add_poi(
                                    activities=plan[current_day]["activities"],
                                    position=poi_sel["name"],
                                    poi_type=poi_type,
                                    price=int(poi_sel["price"]),
                                    cost=int(poi_sel["price"])
                                    * self.query["people_number"],
                                    start_time=act_start_time,
                                    end_time=act_end_time,
                                    innercity_transports=transports_sel,
                                )
                                plan[current_day]["activities"][-1]["tickets"] = (
                                    self.query["people_number"]
                                )

                                new_time = act_end_time
                                new_position = poi_sel["name"]

                                self.attractions_visiting.append(attr_idx)
                                self.spot_type_visiting.append(poi_sel["type"])
                                self.attraction_names_visiting.append(poi_sel["name"])

                                success, plan = self.dfs_poi(
                                    query,
                                    poi_plan,
                                    plan,
                                    new_time,
                                    new_position,
                                    current_day,
                                )

                                if success:
                                    return True, plan

                                self.backtrack_count += 1
                                self._bt_log("add_attraction failed, backtrack...")

                                plan[current_day]["activities"].pop()
                                self.attractions_visiting.pop()
                                self.spot_type_visiting.pop()
                                self.attraction_names_visiting.pop()

                # The last event in a day: hotel or go-back

                if current_day == query["days"] - 1:

                    # go back

                    if len(plan) < current_day + 1:
                        plan.append({"day": current_day + 1, "activities": []})
                    self.search_nodes += 1
                    # transports_ranking = self.ranking_innercity_transport(current_position, poi_plan["back_transport"]["From"], current_day, current_time)
                    transports_ranking = self.innercity_transports_ranking_from_query
                    for trans_type_sel in transports_ranking:
                        self.search_nodes += 1
                        transports_sel = self.collect_innercity_transport(
                            query["target_city"],
                            current_position,
                            poi_plan["back_transport"]["From"],
                            current_time,
                            trans_type_sel,
                        )
                        if not isinstance(transports_sel, list):
                            self.backtrack_count += 1
                            self._bt_log("inner-city transport error, backtrack...")
                            continue

                        plan[current_day]["activities"] = self.add_intercity_transport(
                            plan[current_day]["activities"],
                            poi_plan["back_transport"],
                            innercity_transports=transports_sel,
                            tickets=self.query["people_number"],
                        )

                        res_bool, res_plan = self.constraints_validation(
                            query, plan, poi_plan
                        )

                        if res_bool:
                            return True, res_plan
                        else:
                            plan[current_day]["activities"].pop()

                            self.backtrack_count += 1

                            self._bt_log(
                                "Back-transport, but constraints_validation failed, backtrack..."
                            )
                            # If a unique plan was collected, stop trying other transport types
                            if getattr(self, '_skip_to_next_combo', False):
                                return False, plan

                elif self.query["days"] > 1:
                    # go to hotel
                    hotel_sel = poi_plan["accommodation"]
                    self.search_nodes += 1
                    # transports_ranking = self.ranking_innercity_transport(current_position, hotel_sel["name"], current_day, current_time)
                    transports_ranking = self.innercity_transports_ranking_from_query
                    for trans_type_sel in transports_ranking:
                        self.search_nodes += 1
                        transports_sel = self.collect_innercity_transport(
                            query["target_city"],
                            current_position,
                            hotel_sel["name"],
                            current_time,
                            trans_type_sel,
                        )
                        if not isinstance(transports_sel, list):
                            self.backtrack_count += 1
                            self._bt_log("inner-city transport error, backtrack...")
                            continue

                        if len(transports_sel) == 0:
                            arrived_time = current_time
                        else:
                            arrived_time = transports_sel[-1]["end_time"]

                        plan = self.add_accommodation(
                            current_plan=plan,
                            hotel_sel=hotel_sel,
                            current_day=current_day,
                            arrived_time=arrived_time,
                            required_rooms=self.required_rooms,
                            transports_sel=transports_sel,
                        )

                        new_time = "00:00"
                        new_position = hotel_sel["name"]

                        success, plan = self.dfs_poi(
                            query,
                            poi_plan,
                            plan,
                            new_time,
                            new_position,
                            current_day + 1,
                        )

                        if success:
                            return True, plan
                        else:
                            self.backtrack_count += 1
                            self._bt_log("Try the go back hotel, failed, backtrack...")

                            plan[current_day]["activities"].pop()

                            # If a unique plan was collected, stop trying other transport types
                            if getattr(self, '_skip_to_next_combo', False):
                                return False, plan
            else:
                # raise Exception("Not Implemented.")
                print("incorrect poi type: {}".format(poi_type))
                continue

            candidates_type.remove(poi_type)
            self._bt_log("try another poi type, backtrack...")

        return False, plan

    def generate_plan_with_search(self, query):

        source_city = query["start_city"]
        target_city = query["target_city"]

        print(source_city, "->", target_city)

        train_go = self.collect_intercity_transport(source_city, target_city, "train")
        train_back = self.collect_intercity_transport(target_city, source_city, "train")

        flight_go = self.collect_intercity_transport(
            source_city, target_city, "airplane"
        )
        flight_back = self.collect_intercity_transport(
            target_city, source_city, "airplane"
        )

        # print(train_go)
        # print(train_back)
        # print(flight_go)
        # print(flight_back)

        flight_go_num = 0 if flight_go is None else flight_go.shape[0]
        train_go_num = 0 if train_go is None else train_go.shape[0]
        flight_back_num = 0 if flight_back is None else flight_back.shape[0]
        train_back_num = 0 if train_back is None else train_back.shape[0]

        go_info = pd.concat([train_go, flight_go], axis=0)
        back_info = pd.concat([train_back, flight_back], axis=0)

        if self.debug:
            print(
                "from {} to {}: {} flights, {} trains".format(
                    source_city, target_city, flight_go_num, train_go_num
                )
            )
            print(
                "from {} to {}: {} flights, {} trains".format(
                    target_city, source_city, flight_back_num, train_back_num
                )
            )

            print(go_info.head())
            print(back_info.head())

        self.time_before_search = time.time()
        self.llm_inference_time_count = 0

        # Reset backtrack counter for this search
        self._bt_count = 0
        self._bt_last_report = 0

        # Reset failure diagnostics for this search
        self.failure_stats = {
            "budget_blocked": 0,
            "room_type_mismatch": 0,
            "room_number_mismatch": 0,
            "back_earlier_than_go": 0,
            "dfs_timeout": False,
            "dfs_no_solution": 0,
        }
        self.min_intercity_hotel_cost = float('inf')
        self.min_cost_detail = {}
        self._best_partial_acts = 0
        self.best_partial_plan = None
        self.collected_plans = []
        self._seen_signatures = set()
        self._skip_to_next_combo = False
        self._inner_transport_cache = {}

        # reset the cache before searching
        poi_plan = {}
        self.restaurants_visiting = []
        self.attractions_visiting = []
        self.food_type_visiting = []
        self.spot_type_visiting = []
        self.attraction_names_visiting = []
        self.restaurant_names_visiting = []
        self.ranking_attractions_flag = False
        self.ranking_restaurants_flag = False

        self.llm_rec_format_error = 0
        self.llm_rec_count = 0
        self.search_nodes = 0
        self.backtrack_count = 0

        self.constraints_validation_count = 0
        self.commonsense_pass_count = 0
        self.logical_pass_count = 0
        self.all_constraints_pass = 0

        self.least_plan_schema, self.least_plan_comm, self.least_plan_logic = None, None, None
        self.least_plan_logical_pass = -1

        ranking_go = self.ranking_intercity_transport_go(go_info, query)
        ranking_go = self.reranking_intercity_transport_go_with_constraints(
            ranking_go, go_info, query
        )

        ranking_hotel = self.ranking_hotel(self.memory["accommodations"], query)
        query_room_number, query_room_type = self.decide_rooms(query)
        self.required_budget = self.extract_budget(query)

        ranking_hotel = self.reranking_hotel_with_constraints(
            ranking_hotel, self.memory["accommodations"], query, query_room_number
        )
        ranking_hotel = self._budget_aware_ranking(
            ranking_hotel, self.memory["accommodations"], "hotel"
        )

        self.innercity_transports_ranking_from_query = self.ranking_innercity_transport_from_query(query)
        if self.inner_transport_width is not None:
            self.innercity_transports_ranking_from_query = (
                self.innercity_transports_ranking_from_query[:self.inner_transport_width]
            )

        # Preprocessing above includes LLM ranking calls. Start the requested
        # 35-second DFS budget only when traversal is actually about to begin,
        # and remember the LLM counter so calls made inside DFS can be excluded.
        self.dfs_started_at = time.time()
        self.dfs_llm_inference_start = self.llm_inference_time_count

        print(f"开始 DFS 回溯搜索: {query['start_city']} -> {query['target_city']}, {query['days']}天, 预算{self.required_budget or '不限'}")
        print(f"搜索范围: 去程Top{self.top_go} × 回程Top{self.top_back} × 酒店Top{self.top_hotel}")
        ranking_go = ranking_go[:self.top_go]
        ranking_hotel = ranking_hotel[:self.top_hotel]
        for go_i in ranking_go:
            go_info_i = go_info.iloc[go_i]
            poi_plan["go_transport"] = go_info_i
            self.search_nodes += 1

            ranking_back = self.ranking_intercity_transport_back(
                back_info, query, go_info_i
            )

            ranking_back = self.reranking_intercity_transport_back_with_constraints(
                ranking_back, back_info, query, go_info_i
            )
            ranking_back = self._prioritize_usable_return_times(
                ranking_back, back_info, query
            )
            ranking_back = ranking_back[:self.top_back]

            for back_i in ranking_back:
                back_info_i = back_info.iloc[back_i]
                poi_plan["back_transport"] = back_info_i
                self.search_nodes += 1

                if query["days"] > 1:
                    for hotel_i in ranking_hotel:
                        poi_plan["accommodation"] = self.memory["accommodations"].iloc[hotel_i]
                        room_type = poi_plan["accommodation"]["numbed"]
                        self.search_nodes += 1

                        required_rooms = (int((query["people_number"] - 1) / room_type) + 1)

                        if query_room_type != None and query_room_type != room_type:
                            self.backtrack_count += 1
                            self._bt_log("room_type not match, backtrack...")
                            self.failure_stats["room_type_mismatch"] += 1
                            continue

                        if query_room_number != None:
                            required_rooms = query_room_number

                        if query_room_number != None and query_room_type != None:
                            pass
                        else:
                            if (
                                room_type * required_rooms >= query["people_number"]
                            ) and (
                                room_type * required_rooms < query["people_number"] + room_type
                            ):
                                pass
                            else:
                                if query_room_number != None and room_type == 2:
                                    pass
                                else:
                                    self.backtrack_count += 1
                                    self._bt_log("room_number * room_type not match, backtrack...")
                                    self.failure_stats["room_number_mismatch"] += 1
                                continue
                        self.required_rooms = required_rooms

                        self.intercity_with_hotel_cost = (
                            poi_plan["go_transport"]["Cost"]
                            + poi_plan["back_transport"]["Cost"]
                        ) * query["people_number"] + poi_plan["accommodation"][
                            "price"
                        ] * required_rooms * (
                            query["days"] - 1
                        )
                        # Track minimum cost for diagnostics
                        if self.intercity_with_hotel_cost < self.min_intercity_hotel_cost:
                            self.min_intercity_hotel_cost = self.intercity_with_hotel_cost
                            go_t = poi_plan["go_transport"]
                            back_t = poi_plan["back_transport"]
                            hotel_t = poi_plan["accommodation"]
                            self.min_cost_detail = {
                                "go_type": "高铁" if not pd.isna(go_t.get("TrainID", None)) else "航班",
                                "go_id": str(go_t.get("TrainID", go_t.get("FlightID", ""))),
                                "go_from": str(go_t.get("From", "")),
                                "go_to": str(go_t.get("To", "")),
                                "go_time": f"{go_t.get('BeginTime', '')}→{go_t.get('EndTime', '')}",
                                "go_cost": float(go_t["Cost"]) * query["people_number"],
                                "back_type": "高铁" if not pd.isna(back_t.get("TrainID", None)) else "航班",
                                "back_id": str(back_t.get("TrainID", back_t.get("FlightID", ""))),
                                "back_from": str(back_t.get("From", "")),
                                "back_to": str(back_t.get("To", "")),
                                "back_time": f"{back_t.get('BeginTime', '')}→{back_t.get('EndTime', '')}",
                                "back_cost": float(back_t["Cost"]) * query["people_number"],
                                "hotel_name": str(hotel_t.get("name", "")),
                                "hotel_price": float(hotel_t["price"]),
                                "hotel_rooms": required_rooms,
                                "hotel_nights": query["days"] - 1,
                                "hotel_total": float(hotel_t["price"]) * required_rooms * (query["days"] - 1),
                                "total": self.intercity_with_hotel_cost,
                            }
                        if (
                            self.required_budget != None
                            and self.required_budget - self.intercity_with_hotel_cost
                            <= self.query["people_number"]
                            * (self.query["days"] - 1)
                            * 100
                        ):
                            self.backtrack_count += 1
                            self.failure_stats["budget_blocked"] += 1
                            self._bt_log("required_budget - intercity_with_hotel_cost <= 100 * people_number * (days-1), backtrack...")
                            continue

                        print("search: ...")
                        self._skip_to_next_combo = False
                        try:
                            success, plan = self.dfs_poi(
                                query,
                                poi_plan,
                                plan=[],
                                current_time="",
                                current_position="",
                            )
                        except TimeOutError as e:
                            print("TimeOutError")
                            print(f"[回溯] 超时，共搜索 {self._bt_count} 个方案。尝试返回当前最优部分方案...")
                            self.failure_stats["dfs_timeout"] = True
                            if len(self.collected_plans) > 0:
                                print(f"[超时恢复] 返回已收集的 {len(self.collected_plans)} 个方案")
                                return True, {"plans": self.collected_plans, "count": len(self.collected_plans)}
                            return False, {"error_info": "TimeOutError", "failure_stats": self.failure_stats}

                        if success:
                            if len(self.collected_plans) >= self.max_candidates:
                                print(f"已收集足够方案: {len(self.collected_plans)}")
                                return True, {"plans": self.collected_plans, "count": len(self.collected_plans)}
                            # Not enough unique plans yet, skip to next combo
                            print(f"继续扩充候选池 ({len(self.collected_plans)}/{self.max_candidates})...")
                            self._skip_to_next_combo = False
                            continue
                        else:
                            self._skip_to_next_combo = False  # Reset for next combo
                            if self._dfs_timed_out():
                                print("Searching TIME OUT !!!")
                                print(f"[回溯] 超时，共搜索 {self._bt_count} 个方案。尝试返回当前最优部分方案...")
                                if len(self.collected_plans) > 0:
                                    print(f"[超时恢复] 返回已收集的 {len(self.collected_plans)} 个方案")
                                    return True, {"plans": self.collected_plans, "count": len(self.collected_plans)}
                                return False, {"error_info": "TimeOutError", "failure_stats": self.failure_stats}

                            self.backtrack_count += 1
                            self.failure_stats["dfs_no_solution"] += 1
                            self._bt_log("search failed given the intercity-transport and hotels, backtrack...")

                else:
                    if time_compare_if_earlier_equal(
                        poi_plan["back_transport"]["BeginTime"],
                        poi_plan["go_transport"]["EndTime"],
                    ):
                        self.backtrack_count += 1
                        self.failure_stats["back_earlier_than_go"] += 1
                        self._bt_log("back_transport BeginTime earlier than go_transport EndTime, backtrack...")
                        continue

                    self.intercity_with_hotel_cost = (
                        poi_plan["go_transport"]["Cost"]
                        + poi_plan["back_transport"]["Cost"]
                    ) * query["people_number"]
                    if self.intercity_with_hotel_cost < self.min_intercity_hotel_cost:
                        self.min_intercity_hotel_cost = self.intercity_with_hotel_cost
                        go_t = poi_plan["go_transport"]
                        back_t = poi_plan["back_transport"]
                        self.min_cost_detail = {
                            "go_type": "高铁" if not pd.isna(go_t.get("TrainID", None)) else "航班",
                            "go_id": str(go_t.get("TrainID", go_t.get("FlightID", ""))),
                            "go_from": str(go_t.get("From", "")),
                            "go_to": str(go_t.get("To", "")),
                            "go_time": f"{go_t.get('BeginTime', '')}→{go_t.get('EndTime', '')}",
                            "go_cost": float(go_t["Cost"]) * query["people_number"],
                            "back_type": "高铁" if not pd.isna(back_t.get("TrainID", None)) else "航班",
                            "back_id": str(back_t.get("TrainID", back_t.get("FlightID", ""))),
                            "back_from": str(back_t.get("From", "")),
                            "back_to": str(back_t.get("To", "")),
                            "back_time": f"{back_t.get('BeginTime', '')}→{back_t.get('EndTime', '')}",
                            "back_cost": float(back_t["Cost"]) * query["people_number"],
                            "hotel_name": "无需住宿（1日游）",
                            "hotel_price": 0,
                            "hotel_rooms": 0,
                            "hotel_nights": 0,
                            "hotel_total": 0,
                            "total": self.intercity_with_hotel_cost,
                        }
                    print("search: ...")
                    self._skip_to_next_combo = False
                    try:
                        success, plan = self.dfs_poi(
                            query,
                            poi_plan,
                            plan=[],
                            current_time="",
                            current_position="",
                        )
                    except TimeOutError as e:
                        print("TimeOutError")
                        print(f"[回溯] 超时，共搜索 {self._bt_count} 个方案。尝试返回当前最优部分方案...")
                        if len(self.collected_plans) > 0:
                            print(f"[超时恢复] 返回已收集的 {len(self.collected_plans)} 个方案")
                            return True, {"plans": self.collected_plans, "count": len(self.collected_plans)}
                        return False, {"error_info": "TimeOutError", "failure_stats": self.failure_stats}

                    if success:
                        if len(self.collected_plans) >= self.max_candidates:
                            print(f"已收集足够方案: {len(self.collected_plans)}")
                            return True, {"plans": self.collected_plans, "count": len(self.collected_plans)}
                        # Not enough unique plans yet, skip to next combo
                        print(f"继续扩充候选池 ({len(self.collected_plans)}/{self.max_candidates})...")
                        self._skip_to_next_combo = False
                        continue
                    else:
                        self._skip_to_next_combo = False  # Reset for next combo
                        if self._dfs_timed_out():
                            print("Searching TIME OUT !!!")
                            print(f"[回溯] 超时，共搜索 {self._bt_count} 个方案。尝试返回当前最优部分方案...")
                            if len(self.collected_plans) > 0:
                                print(f"[超时恢复] 返回已收集的 {len(self.collected_plans)} 个方案")
                                return True, {"plans": self.collected_plans, "count": len(self.collected_plans)}
                            return False, {"error_info": "TimeOutError", "failure_stats": self.failure_stats}

                        self.backtrack_count += 1
                        self.failure_stats["dfs_no_solution"] += 1
                        self._bt_log("search failed given the intercity-transport and hotels, backtrack...")


        print(f"DFS 搜索完成: 共搜索 {self.search_nodes} 个节点, 回溯 {self.backtrack_count} 次, 耗时 {self._dfs_elapsed():.1f}秒, 组合上限 {self.top_go}×{self.top_back}×{self.top_hotel}")
        if len(self.collected_plans) > 0:
            print(f"收集到 {len(self.collected_plans)} 个方案")
            return True, {"plans": self.collected_plans, "count": len(self.collected_plans)}
        print(f"失败诊断: 预算不足 {self.failure_stats['budget_blocked']}次 | 房间类型不匹配 {self.failure_stats['room_type_mismatch']}次 | 回程早于去程 {self.failure_stats['back_earlier_than_go']}次 | 最低交通+住宿 ¥{self.min_intercity_hotel_cost:.0f}")
        return False, {"error_info": "No solution found.", "failure_stats": self.failure_stats, "min_intercity_hotel_cost": self.min_intercity_hotel_cost}

    def symbolic_search(self, symoblic_query):

        # print(symoblic_query)

        if (symoblic_query["target_city"] in self.env.support_cities) and (
            symoblic_query["start_city"] in self.env.support_cities
        ):
            pass
        else:
            return False, {"error_info": f"Unsupported cities {symoblic_query['start_city']} -> {symoblic_query['target_city']}."}

        if self.preference_search:
            # print(symoblic_query["preference_py"])

            preference_py = symoblic_query["preference_py"][0]
            index = preference_py.find("\n")

            concept = preference_py[:index]
            code = preference_py[index + 1 :]

            # print(concept, code)

            symoblic_query["preference_opt"] = concept.split(" ")[0]
            symoblic_query["preference_concept"] = concept.split(" ")[1]
            symoblic_query["preference_code"] = code
            print(symoblic_query["preference_opt"], "\n", symoblic_query["preference_concept"], "\n", symoblic_query["preference_code"])

            if symoblic_query["preference_opt"] == "maximize":
                self.least_plan_logic_pvalue = -19260817
            elif symoblic_query["preference_opt"] == "minimize":
                self.least_plan_logic_pvalue = 19260817
            else:
                raise ValueError("preference_opt must be maximize or minimize")



        self.memory["accommodations"] = self.collect_poi_info_all(
            symoblic_query["target_city"], "accommodation"
        )
        self.memory["attractions"] = self.collect_poi_info_all(
            symoblic_query["target_city"], "attraction"
        )
        self.memory["restaurants"] = self.collect_poi_info_all(
            symoblic_query["target_city"], "restaurant"
        )

        # print(symoblic_query)



        self.query = symoblic_query

        success, plan = self.generate_plan_with_search(symoblic_query)

        print(success, plan)

        return success, plan

    def collect_innercity_transport(self, city, start, end, start_time, trans_type):

        cache_key = (city, start, end, start_time, trans_type, self.query["people_number"])
        cache = getattr(self, "_inner_transport_cache", None)
        if cache is not None and cache_key in cache:
            return deepcopy(cache[cache_key])

        call_str = (
            'goto("{city}", "{start}", "{end}", "{start_time}", "{trans_type}")'.format(
                city=city,
                start=start,
                end=end,
                start_time=start_time,
                trans_type=trans_type,
            )
        )

        # print(call_str)
        if start == end:
            return []
        info = self.env(call_str)["data"]

        # print(info)

        if not isinstance(info, list):
            result = "No solution"
            if cache is not None:
                cache[cache_key] = result
            return result

        if len(info) == 3:
            info[1]["price"] = info[1]["cost"]
            info[1]["tickets"] = self.query["people_number"]
            info[1]["cost"] = info[1]["price"] * info[1]["tickets"]

            info[0]["price"] = info[0]["cost"]
            info[2]["price"] = info[2]["cost"]
        elif info[0]["mode"] == "taxi":
            info[0]["price"] = info[0]["cost"]
            info[0]["cars"] = int((self.query["people_number"] - 1) / 4) + 1
            info[0]["cost"] = info[0]["price"] * info[0]["cars"]
        elif info[0]["mode"] == "walk":
            info[0]["price"] = info[0]["cost"]

        if cache is not None:
            cache[cache_key] = deepcopy(info)
        return info

    def collect_intercity_transport(self, source_city, target_city, trans_type):

        info_return = self.env(
            "intercity_transport_select('{source_city}', '{target_city}', '{trans_type}')".format(
                source_city=source_city, target_city=target_city, trans_type=trans_type
            )
        )
        if not info_return["success"]:
            return pd.DataFrame([])
        trans_info = info_return["data"]
        # print(poi_info)
        while True:
            info_i = self.env("next_page()")["data"]
            if len(info_i) == 0:
                break
            else:
                trans_info = pd.concat([trans_info, info_i], axis=0, ignore_index=True)
        # print(poi_info)
        return trans_info

    def collect_poi_info_all(self, city, poi_type):

        if poi_type == "accommodation":
            func_name = "accommodations_select"
        elif poi_type == "attraction":
            func_name = "attractions_select"
        elif poi_type == "restaurant":
            func_name = "restaurants_select"
        else:
            raise NotImplementedError

        poi_info = self.env(
            "{func}('{city}', 'name', lambda x: True)".format(func=func_name, city=city)
        )["data"]
        # print(poi_info)
        while True:
            info_i = self.env("next_page()")["data"]
            if len(info_i) == 0:
                break
            else:
                poi_info = pd.concat([poi_info, info_i], axis=0, ignore_index=True)

        # print(poi_info)
        return poi_info


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description="argparse testing")
    parser.add_argument(
        "--splits",
        "-l",
        type=str,
        default="easy",
        choices=["easy", "medium", "human"],
        help="query subset",
    )
    parser.add_argument("--index", "-i", type=int, default=None, help="query index")
    parser.add_argument(
        "--start", "-s", type=int, default=None, help="start query index"
    )
    parser.add_argument(
        "--oracle_translation",
        action="store_true",
        help="Set this flag to enable oracle translation.",
    )
    args = parser.parse_args()

    from evaluation.test import load_query
    from agent.llms import Deepseek
    from environment.world_env import WorldEnv

    env = WorldEnv()

    query_index, query_data = load_query(args)

    # print(query_index, query_data)
    print(len(query_index), "samples")

    agent = NesyAgent(env=env, backbone_llm=Deepseek())

    if args.index is not None:
        query_index = [query_index[args.index]]

    for i, data_idx in enumerate(query_index):

        symbolic_input = query_data[data_idx]
        print(symbolic_input)

        agent.symbolic_search(symbolic_input)
