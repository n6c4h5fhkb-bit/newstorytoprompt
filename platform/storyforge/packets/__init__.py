from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path

from storyforge import ROOT
from storyforge.config import SflError, load_yaml
from storyforge.store import Store


@dataclass
class Source:
    data: object
    bindings: list[dict]


@dataclass
class Packet:
    role: str
    instruction: str
    data: dict
    bindings: list[dict]
    repairs: dict


def file_source(store: Store, path: str, selector: dict | None = None) -> Source:
    with store.locked():
        binding = store.binding(path, selector)
        return Source(store.selected(binding), [binding])


def json_source(store: Store, path: str) -> Source:
    source = file_source(store,path)
    import json
    source.data = json.loads(source.data) if source.data is not None else None
    return source


def external_source(store: Store, path: Path, *, style_reference: bool = False) -> Source:
    binding = store.binding("external:" + str(path.resolve()), style_reference=style_reference)
    return Source(store.selected(binding), [binding])


def methodology_bindings(store: Store, roles: list[str]) -> list[dict]:
    table_path = ROOT / "platform/packets.yaml"
    policies = load_yaml(table_path)
    paths = {table_path, ROOT / "skills/shared/codex_call.md", ROOT / "skills/shared/model_output.md"}
    for role in roles:
        paths.add(ROOT / policies[role]["role_card"])
        paths.update(ROOT / path for path in policies[role].get("references", []))
    return [external_source(store, path, style_reference=True).bindings[0] for path in sorted(paths)]


def build(store: Store, role: str, sources: dict[str, Source], *, repairs: dict | None = None) -> Packet:
    table_path = ROOT / "platform" / "packets.yaml"
    policy = load_yaml(table_path)[role]
    forbidden = set(policy["must_not_see"])
    allowed = policy["inputs"]
    if forbidden & set(allowed):
        raise SflError(f"Invalid packet policy for {role}: forbidden input allowed")
    missing = set(allowed) - set(sources)
    if missing:
        raise SflError(f"Missing packet inputs for {role}: {sorted(missing)}")
    # Only the allowlist is copied; a caller cannot accidentally leak extra files.
    data, bindings = {}, []
    for name in allowed:
        data[name] = sources[name].data
        bindings.extend(sources[name].bindings)
    cards = [ROOT / "skills/shared/codex_call.md", ROOT / policy["role_card"], ROOT / "skills/shared/model_output.md"]
    cards += [ROOT / path for path in policy.get("references", [])]
    instruction = "\n\n".join(path.read_text(encoding="utf-8-sig") for path in cards)
    bindings += [external_source(store, path, style_reference=True).bindings[0] for path in cards]
    bindings += external_source(store, table_path, style_reference=True).bindings
    unique = {str((b["path"], b["selector"], b["style_reference"])): b for b in bindings}
    repairs = repairs or {}
    if set(repairs) - {"hard_errors", "findings", "challenge", "previous_output", "reference_limit_errors", "user_note", "retry_id"}:
        raise SflError("Unexpected repair context")
    if role == "reconstruction_blind" and set(repairs) & {"previous_output", "challenge", "user_note", "findings"}:
        raise SflError("Blind review cannot receive writer output, challenges or intent")
    if role == "viewer" and set(repairs) & {"previous_output", "user_note", "findings"}:
        raise SflError("Viewer cannot receive writer output or user intent")
    return Packet(role, instruction, data, list(unique.values()), repairs)
