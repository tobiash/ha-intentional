#!/usr/bin/env python3
"""Serve the Intentional panel locally without a Home Assistant install.

This development harness loads the bundled web component, provides a mocked
``hass`` object, and backs the validation endpoints with the pure Python engine.
It is intentionally small and dependency-free so UI work can be exercised before
installing a new integration build on a real Home Assistant instance.
"""

from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from intentional.engine import Engine  # noqa: E402
from intentional.projection import simulation_step  # noqa: E402
from intentional.yaml_loader import RuleLoadError, load_rules_from_string  # noqa: E402

PANEL_PATH = REPO_ROOT / "custom_components" / "intentional" / "frontend" / "intentional-panel.js"

SAMPLE_DOCUMENT = """- id: living-room-presence
  enabled: true
  labels: [living-room, lighting]
  group: living-room-lighting
  profile: pass-through
  reason: Keep the sofa lamp on while the room is occupied
  while:
    binary_sensor.living_room_presence: "on"
    sensor.living_room_light:
      lt: 60
  hold:
    until:
      binary_sensor.living_room_presence: "off"
      for: 15m
  intent:
    light.sofa:
      state: "on"
      brightness_pct: 55
      apply:
        transition:
          assert: 2s
          change: 5s
          withdraw: 7s
"""


class HarnessState:
    def __init__(self, contents: str) -> None:
        self.contents = contents
        self.generation = "local-dev"
        self.history: list[dict[str, str]] = []


class Handler(BaseHTTPRequestHandler):
    state: HarnessState

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/":
            self._html(HTML)
            return
        if self.path == "/intentional-panel.js":
            self._javascript(PANEL_PATH.read_text())
            return
        if self.path == "/api/intentional/health":
            self._json({"status": "ok", "version": "local-dev", "rule_count": self._rule_count(), "active_intent_count": 0})
            return
        if self.path == "/api/intentional/rules/document":
            self._json(self._document_response())
            return
        if self.path == "/api/intentional/rules/history":
            self._json({"history": self.state.history})
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:  # noqa: N802
        if self.path != "/api/intentional/rules/document":
            self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)
            return
        data = self._read_json()
        contents = data.get("contents")
        if not isinstance(contents, str):
            self._json({"error": "Request body must include string `contents`"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            load_rules_from_string(contents)
        except RuleLoadError as err:
            self._json({"valid": False, "errors": [str(err)]}, HTTPStatus.BAD_REQUEST)
            return
        self.state.history.insert(0, {"generation": self.state.generation, "reason": "local save"})
        self.state.contents = contents
        self.state.generation = f"local-{len(self.state.history)}"
        self._json(self._document_response())

    def do_POST(self) -> None:  # noqa: N802
        data = self._read_json()
        if self.path == "/api/intentional/validate":
            self._validate(data)
            return
        if self.path == "/api/intentional/dry-run":
            self._dry_run(data)
            return
        if self.path == "/api/intentional/simulate":
            self._simulate(data)
            return
        if self.path == "/api/intentional/rules/rollback":
            self._json(self._document_response())
            return
        self._json({"error": "not_found"}, HTTPStatus.NOT_FOUND)

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"intentional-panel: {fmt % args}\n")

    def _validate(self, data: dict[str, Any]) -> None:
        contents = data.get("contents")
        if not isinstance(contents, str):
            self._json({"error": "Request body must include string `contents`"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            rules = load_rules_from_string(contents)
        except RuleLoadError as err:
            self._json({"valid": False, "errors": [str(err)]}, HTTPStatus.BAD_REQUEST)
            return
        self._json({
            "valid": True,
            "rule_count": len(rules),
            "normalized": [
                {"id": rule.id, "enabled": rule.enabled, "labels": list(rule.labels), "notes": rule.notes, "when": rule.when, "target": rule.target, "set": dict(rule.set), "cap": dict(rule.cap), "floor": dict(rule.floor), "offset": dict(rule.offset), "multiply": dict(rule.multiply), "effects": []}
                for rule in rules
            ],
            "warnings": [],
        })

    def _dry_run(self, data: dict[str, Any]) -> None:
        rules = self._load_request_rules(data)
        if rules is None:
            return
        engine = Engine(selector_resolver=lambda _selector: [])
        engine.load_rules(rules)
        for key, value in data.get("state_overrides", {}).items():
            if isinstance(key, str) and "." in key:
                entity_id, _sep, field = key.rpartition(".")
                engine.update_state(entity_id, value, field=field)
        engine.evaluate_all()
        resolved = []
        for target in engine.list_active_targets():
            result = engine.resolve(target)
            if result is not None:
                resolved.append({"target": target, "value": dict(result.value)})
        self._json({"valid": True, "active_targets": list(engine.list_active_targets()), "resolved_targets": resolved, "effects": [], "errors": []})

    def _simulate(self, data: dict[str, Any]) -> None:
        rules = self._load_request_rules(data)
        if rules is None:
            return
        timeline = data.get("timeline")
        if not isinstance(timeline, list):
            self._json({"error": "Request body must include list `timeline`"}, HTTPStatus.BAD_REQUEST)
            return
        engine = Engine(clock_fn=lambda: 0, selector_resolver=lambda _selector: [])
        engine.load_rules(rules)
        steps = []
        for index, step in enumerate(timeline):
            if not isinstance(step, dict):
                self._json({"error": f"timeline[{index}] must be a mapping"}, HTTPStatus.BAD_REQUEST)
                return
            if step.get("advance_ms", 0):
                engine.advance_clock(int(step["advance_ms"]))
            for key, value in step.get("states", {}).items():
                if isinstance(key, str) and "." in key:
                    entity_id, _sep, field = key.rpartition(".")
                    engine.update_state(entity_id, value, field=field)
            engine.evaluate_all()
            steps.append(simulation_step(engine, index=index))
        self._json({"valid": True, "steps": steps, "errors": []})

    def _load_request_rules(self, data: dict[str, Any]) -> list[Any] | None:
        contents = data.get("contents")
        if not isinstance(contents, str):
            self._json({"error": "Request body must include string `contents`"}, HTTPStatus.BAD_REQUEST)
            return None
        try:
            return load_rules_from_string(contents)
        except RuleLoadError as err:
            self._json({"valid": False, "errors": [str(err)]}, HTTPStatus.BAD_REQUEST)
            return None

    def _rule_count(self) -> int:
        try:
            return len(load_rules_from_string(self.state.contents))
        except RuleLoadError:
            return 0

    def _document_response(self) -> dict[str, Any]:
        return {
            "contents": self.state.contents,
            "size": len(self.state.contents.encode()),
            "generation": self.state.generation,
            "rule_count": self._rule_count(),
            "source": "local-harness",
        }

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def _html(self, body: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def _javascript(self, body: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def _json(self, body: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


HTML = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Intentional Panel Harness</title>
    <script src="/intentional-panel.js"></script>
    <style>
      :root {
        --primary-color: #03a9f4;
        --text-primary-color: #ffffff;
        --primary-text-color: #e6eaf2;
        --secondary-text-color: #9aa6b2;
        --primary-background-color: #111827;
        --secondary-background-color: #1f2937;
        --card-background-color: #172033;
        --divider-color: #334155;
        --error-color: #d32f2f;
        --success-color: #43a047;
        --warning-color: #ffa000;
        --ha-card-box-shadow: 0 10px 30px rgba(0, 0, 0, .28);
      }
      body { margin: 0; background: var(--primary-background-color); }
    </style>
  </head>
  <body>
    <intentional-panel></intentional-panel>
    <script>
      const panel = document.querySelector("intentional-panel");
      panel.hass = {
        states: {
          "binary_sensor.living_room_presence": { state: "on", attributes: {} },
          "binary_sensor.office_occupancy": { state: "off", attributes: {} },
          "sensor.living_room_light": { state: "40", attributes: {} },
          "light.sofa": { state: "off", attributes: { supported_color_modes: ["brightness", "color_temp"] } },
          "light.office": { state: "off", attributes: { supported_color_modes: ["brightness"] } },
          "schedule.living_room_evening": { state: "on", attributes: {} },
          "input_boolean.focus_mode": { state: "off", attributes: {} }
        },
        callApi(method, path, data) {
          return fetch(`/api/${path}`, {
            method,
            headers: { "Content-Type": "application/json" },
            body: data === undefined ? undefined : JSON.stringify(data),
          }).then(async (response) => {
            const body = await response.json();
            if (!response.ok) throw new Error(body.error || body.message || (body.errors || []).join("; ") || response.statusText);
            return body;
          });
        }
      };
    </script>
  </body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Intentional panel development harness.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    Handler.state = HarnessState(SAMPLE_DOCUMENT)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Serving Intentional panel harness at http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
