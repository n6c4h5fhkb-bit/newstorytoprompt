from __future__ import annotations
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform"))

from storyforge.config import configuration
from storyforge.store import create_project, serialize
from storyforge.runner import Runner, import_episode, leaf_strings
from storyforge.llm import Client

FIXTURE = ROOT / "tests/fixtures/golden_project"
RESPONSES = json.loads((FIXTURE / "responses/responses.json").read_text(encoding="utf-8"))


def outfit_child():
    """A genuine appearance variant used only in parent-dependency tests."""
    asset = {"type":"character", "name":"云清禾", "variant":"换装", "parent":"char:云清禾",
        "what_changed":"灰白旧裙换为深蓝交领长裙，身份、发型与比例不变",
        "placeholder":"@云清禾_换装", "description":"云清禾的同一身份与身形，仅换为深蓝交领长裙；双腕空，不持道具"}
    brief = {"brief":"引用 @云清禾_母图，仅将灰白旧裙换为深蓝交领长裙；保留同一脸、发型、比例和设定表排版，双腕空，不添加道具。", "responses":[]}
    return asset, brief


def project(path: Path):
    store = create_project(path, {"name": path.name, "demo": True, "output": {"model_card": "seedance-2.0", "ratio": "16:9", "music": "none"},
        "models": {tier: {"provider": "replay", "model": "replay-fixture", "replay_dir": str(FIXTURE / "responses")} for tier in ("strong", "cheap", "reviewer")}})
    import_episode(store, (FIXTURE / "ep01.md").read_text(encoding="utf-8"),
        (FIXTURE / "bible.md").read_text(encoding="utf-8"), (FIXTURE / "ep00.md").read_text(encoding="utf-8"))
    store.write(".state/demo.json", {"demo": True}, json_data=True)
    return store


def approve_look(runner: Runner):
    first = runner.run()
    assert first["waiting"], first
    card = next(c for c in runner.cards.list() if c["kind"] == "checkpoint")
    runner.cards.answer(card["id"], "approve")


def patch(value, mutation):
    parent = value
    for key in mutation["path"][:-1]:
        parent = parent[key]
    key = mutation["path"][-1]
    if mutation.get("operation") == "remove":
        del parent[key]
    else:
        parent[key] = deepcopy(mutation["value"])


class ScriptedCodex:
    """An explicit fake transport, never used by production or quality metrics."""
    def __init__(self, case=None):
        self.case = case or {}
        self.counts, self.packets = Counter(), []

    def __call__(self, profile, packet, schema, *, stage, target):
        assert profile["model"] == "gpt-6-sol" and profile["effort"] == "high"
        key = packet.role + ":" + target
        self.counts[key] += 1
        self.packets.append((packet.role, target, deepcopy(packet.data), deepcopy(packet.repairs)))
        value = deepcopy(RESPONSES[key])
        if key == self.case.get("writer_key") and self.counts[key] <= self.case.get("bad_calls", 1):
            for mutation in self.case.get("mutations", []):
                patch(value, mutation)
        if packet.role == self.case.get("reviewer") and (not self.case.get("review_target") or target == self.case["review_target"]):
            evidence = self.case["finding"]["evidence"]
            if any(evidence in text for text in leaf_strings(packet.data)):
                value["findings"] = [deepcopy(self.case["finding"])]
        if packet.role == "unit_prompt":
            requested = {u["id"] for u in packet.data["units"]}
            value["prompts"] = [p for p in value["prompts"] if p["unit_id"] in requested]
        if "responses" in value and packet.repairs.get("findings"):
            value["responses"] = [{"finding_id": f["id"], "disposition": "fix", "reason": "Fixture supplies the corrected artifact"} for f in packet.repairs["findings"]]
        return {"text": serialize(value), "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 30},
                "error": None, "exit_code": 0, "tool_items": [], "complete": True}


def scripted_runner(store, case=None):
    config = configuration(store.root)
    config["models"] = configuration()["models"]
    transport = ScriptedCodex(case)
    runner = Runner(store, config=config, client=Client(store, config, codex_transport=transport))
    return runner, transport
