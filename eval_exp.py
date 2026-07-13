

# Patch SSL to avoid Windows certificate store corruption
import ssl as _ssl
_orig_create_default_context = _ssl.create_default_context
def _patched_create_default_context(*args, **kwargs):
    try:
        return _orig_create_default_context(*args, **kwargs)
    except _ssl.SSLError:
        import certifi
        context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(certifi.where())
        return context
_ssl.create_default_context = _patched_create_default_context

import sys as _sys
try:
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv()

import argparse

import numpy as np

import sys
import os
import json

project_root_path = os.path.dirname(os.path.abspath(__file__))
if project_root_path not in sys.path: sys.path.insert(0, project_root_path)


from chinatravel.data.load_datasets import load_query
from chinatravel.evaluation.utils import load_json_file, validate_json

from chinatravel.evaluation.schema_constraint import evaluate_schema_constraints
from chinatravel.evaluation.commonsense_constraint import evaluate_commonsense_constraints
from chinatravel.evaluation.hard_constraint import evaluate_hard_constraints, evaluate_hard_constraints_v2
from chinatravel.evaluation.preference import evaluate_preference, evaluate_preference_v2


METHOD_LIST = [
    "example" "act_Deepseek_zeroshot",
    "act_GPT4o_zeroshot",
    "react_Deepseek_zeroshot",
    "react_GPT4o_zeroshot",
    "react_GLM4Plus_zeroshot",
    "react_Deepseek_oneshot",
    "react_GPT4o_oneshot",
    "naive_ns_Deepseek",
    "naive_ns_GPT4o",
    "naive_ns_GLM4Plus",
]


def _method_has_en_suffix(method):
    base_method = method.split("_oracletranslation")[0].split("_oracle_translation")[0]
    return base_method.endswith("_en")


def load_result(args, query_index, verbose=False):

    def load_result_for_method(method):
        plans = {}
        for query_id in query_index:
            result_file = os.path.join(
                "results/", method, "{}.json".format(query_id)
            )

            try:
                if os.path.exists(result_file):
                    result = load_json_file(result_file)
                    plans[query_id] = result
                else:
                    plans[query_id] = {}
            except:
                plans[query_id] = {}
        return plans

    result = {}
    if args.method == "all":
        method_list = []
        for mi in METHOD_LIST:
            if mi != "example":
                method_list.append(mi)
    else:
        method_list = [args.method]

    for method in method_list:
        result[method] = load_result_for_method(method)

    if verbose:
        print(result)

    return method_list, result

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--splits", "-s", type=str, default="example")
    parser.add_argument(
        "--method", "-m", type=str, default="example"
    )  # , choices=METHOD_LIST)
    parser.add_argument("--preference", "-p", action="store_true", default=False)
    parser.add_argument("--oracle_translation", action="store_true", default=True, help="Load ground-truth constraints for evaluation (default: True)")
    parser.add_argument("--lang", "--locale", choices=["zh", "en"], default="zh")
    args = parser.parse_args()
    if args.lang == "en" and args.method != "all" and not _method_has_en_suffix(args.method):
        args.method += "_en"

    # print(args.splits)

    query_index, query_data = load_query(args)
    method_list, result_data = load_result(args, query_index)

    # print(result_data)



    schema_file_path = 'chinatravel/evaluation/output_schema.json'
    schema = load_json_file(schema_file_path)


    if not os.path.exists("eval_res/"):
        os.makedirs("eval_res/")
    if not os.path.exists("eval_res/splits_{}/".format(args.splits)):
        os.makedirs("eval_res/splits_{}/".format(args.splits))



    for method in method_list:

        print("method: ", method)

        plan_count = 0
        for plan in result_data[method]:
            if plan != {}:
                plan_count += 1
        print("There are {} results...".format(plan_count))


        print("Method: {}".format(method))

        if not os.path.exists("eval_res/splits_{}/{}/".format(args.splits, method)):
            os.makedirs("eval_res/splits_{}/{}/".format(args.splits, method))

        schema_rate, schema_result_agg, schema_pass_id = evaluate_schema_constraints(
            query_index, result_data[method], schema=schema
        )
        res_file = "eval_res/splits_{}/{}/schema.csv".format(args.splits, method)
        schema_result_agg.to_csv(res_file, index=False)
        print("save to {}".format(res_file))
        print("Schema Pass Rate:", schema_rate)

        macro_comm, micro_comm, common_result_agg, commonsense_pass_id = evaluate_commonsense_constraints(
            query_index, query_data, result_data[method], verbose=False, lang=args.lang
        )

        res_file = "eval_res/splits_{}/{}/commonsense.csv".format(args.splits, method)
        common_result_agg.to_csv(res_file, index=False)
        print("save to {}".format(res_file))

        print("Commonsense constraints:")
        print("micro accuracy: {}".format(micro_comm))
        print("macro accuracy: {}".format(macro_comm))


        # print("Logical constraints (flat version):")
        # macro_logi, micro_logi, logi_result_agg, logi_pass_id_flat = evaluate_hard_constraints(
        #     query_index, query_data, result_data[method], verbose=False
        # )

        # print("micro accuracy: {}".format(micro_logi))
        # print("macro accuracy: {}".format(macro_logi))

        # res_file = "eval_res/splits_{}/{}/logical.csv".format(args.splits, method)
        # logi_result_agg.to_csv(res_file, index=False)
        # print("save to {}".format(res_file))

        print("Logical constraints (python version):")
        macro_logi, micro_logi, conditional_macro_logi, conditional_micro_logi, logi_result_agg, logi_pass_id = evaluate_hard_constraints_v2(
            query_index, query_data, result_data[method], env_pass_id=commonsense_pass_id, verbose=False, lang=args.lang
        )


        print("micro accuracy: {}".format(micro_logi))
        print("macro accuracy: {}".format(macro_logi))

        print("conditional micro accuracy: {}".format(conditional_micro_logi))
        print("conditional macro accuracy: {}".format(conditional_macro_logi))


        print("Conditional LPR: {}".format(conditional_micro_logi))

        res_file = "eval_res/splits_{}/{}/logical_py.csv".format(args.splits, method)
        logi_result_agg.to_csv(res_file, index=False)
        print("save to {}".format(res_file))

        # record the index of the queries that pass the logical constraints
        logical_pass_info = logi_result_agg.iloc[:, 1:]
        id_list = logi_result_agg.iloc[:, 0].tolist()

        all_pass_id = list(set(schema_pass_id) & set(commonsense_pass_id) & set(logi_pass_id))



        print("All pass ratio: ", 1. * len(all_pass_id) / len(query_index) * 100)

        if args.preference:
            print("Preference:")
            result_agg = evaluate_preference_v2(
                query_index,
                query_data,
                result_data[method],
                list(set(commonsense_pass_id) & set(logi_pass_id)),
                lang=args.lang,
            )

            res_file = "eval_res/splits_{}/{}/preference.csv".format(
                args.splits, method
            )
            result_agg.to_csv(res_file, index=False)
            print("save to {}".format(res_file))
