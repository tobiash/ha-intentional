"""Replay captured rules/states to measure steady-state engine CPU costs.

Run with PYTHONPATH=src python tools/benchmark_lifecycle.py rules.json states.json.
Inputs are an Intentional rules-document response and a HA /api/states response.
No requests or device actions are performed. This measures evaluation and two
lifecycle snapshots, not a complete HA tick (selectors and time context omitted).
"""

import argparse
import json
import time
from pathlib import Path

from intentional.engine import Engine
from intentional.yaml_loader import load_rules_from_string


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("rules", type=Path)
    parser.add_argument("states", type=Path)
    args = parser.parse_args()
    engine = Engine(clock_fn=lambda: 1_000_000)
    engine.load_rules(load_rules_from_string(json.loads(args.rules.read_text())["contents"]))
    for state in json.loads(args.states.read_text()):
        engine.update_state(state["entity_id"], state["state"])
        for key, value in state["attributes"].items():
            engine.update_state(state["entity_id"], value, field=key)
    engine.evaluate_all()

    def cycle() -> None:
        engine.evaluate_all()
        engine.export_lifecycle_records()
        engine.export_lifecycle_records()

    for name, operation in (
        ("evaluation", engine.evaluate_all),
        ("snapshot", engine.export_lifecycle_records),
        ("evaluation + two snapshots", cycle),
    ):
        started = time.process_time()
        for _ in range(500):
            operation()
        print(f"{name}: {(time.process_time() - started) * 2:.3f} ms/call")


if __name__ == "__main__":
    main()
