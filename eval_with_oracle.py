import argparse
import os
import runpy
import sys


EVAL_SCRIPTS = {
    "exp": "eval_exp.py",
    "tpc": "eval_tpc.py",
}


def main():
    parser = argparse.ArgumentParser(
        description="Run an evaluation script while preserving oracle constraint fields."
    )
    parser.add_argument(
        "script",
        choices=EVAL_SCRIPTS,
        help="Evaluation script to run.",
    )
    parser.add_argument(
        "script_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to the selected evaluation script.",
    )
    args = parser.parse_args()

    script_path = EVAL_SCRIPTS[args.script]
    sys.argv = [script_path] + args.script_args

    original_parse_args = argparse.ArgumentParser.parse_args

    def parse_args_with_oracle(self, *parse_args, **parse_kwargs):
        namespace = original_parse_args(self, *parse_args, **parse_kwargs)
        if not hasattr(namespace, "oracle_translation"):
            namespace.oracle_translation = True
        return namespace

    argparse.ArgumentParser.parse_args = parse_args_with_oracle
    runpy.run_path(os.path.join(os.path.dirname(__file__), script_path), run_name="__main__")


if __name__ == "__main__":
    main()
