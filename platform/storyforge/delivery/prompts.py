"""Compose the fixed prompt structure from methodology text and model fragments."""
from string import Template
import re

from storyforge import ROOT
from storyforge.checks import rules
from storyforge.config import load_yaml


def style_document(value: dict) -> str:
    sections = [("美术定调提示词", "ART_PROMPT", value["art_prompt"]),
                ("每段视频共用风格", "STYLE_LOCK", value["style_lock"]),
                ("色卡生成提示词", "PALETTE", value["palette_description"])]
    return value["text"].rstrip() + "\n" + "".join(
        f"\n## {title}\n\n<!-- {marker}_BEGIN -->\n{text}\n<!-- {marker}_END -->\n"
        for title, marker, text in sections)


def style_parts(text: str) -> dict:
    result = {}
    for key, marker in (("art_prompt", "ART_PROMPT"), ("style_lock", "STYLE_LOCK"), ("palette_description", "PALETTE")):
        match = re.search(rf"<!-- {marker}_BEGIN -->\s*\n(.*?)\n<!-- {marker}_END -->", text, re.S)
        result[key] = match[1] if match else ""
    # Retain the prose before the generated sections, including legacy files.
    result["text"] = re.split(r"(?:\n## 美术定调提示词\s*\n|<!-- (?:ART_PROMPT|STYLE_LOCK)_BEGIN -->)", text, maxsplit=1)[0].strip()
    return result


def unit_label(unit: dict, units: list[dict]) -> str:
    scene = re.sub(r"^S(?=\d)", "SC", unit["scene"])
    number = next(i for i, u in enumerate((u for u in units if u["scene"] == unit["scene"]), 1) if u["id"] == unit["id"])
    return f"{scene}-U{number:02d}"


def render(fragments: dict, references: list[dict], style_lock: str, config: dict, unit: dict, *, label: str) -> str:
    parts = load_yaml(ROOT / "skills/director/taste/prompt_parts.yaml")
    lines = [Template(parts["reference_line"]).substitute(
        placeholder=r["placeholder"], name=r["name"], role=parts["roles"]["previous_frame" if r["asset_id"] == "external:previous_frame" else "palette" if r["placeholder"] == rules()["palette_placeholder"] else r["type"]], description=r.get("identity_notes") or r["description"], position=r["position"])
        for r in references]
    audio = parts["audio_none"] if config["output"]["music"] == "none" else Template(parts["audio_cue"]).substitute(music=config["output"]["music_placeholder"])
    return Template(parts["prompt"]).substitute(label=label, seconds=f"{unit['seconds']:g}", manifest="\n".join(lines), style_lock=style_lock,
        audio_line=audio, **{key: fragments[key] for key in ("performance", "environment", "spatial_anchors", "continuation", "shots", "negative")}) + "\n"
