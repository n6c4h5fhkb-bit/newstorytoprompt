from __future__ import annotations
from collections import Counter
import json
import math
import re

from storyforge import ROOT
from storyforge.config import load_yaml
from storyforge.checks.parsers import known_names, parse_bible, parse_script


def rules() -> dict:
    return load_yaml(ROOT / "skills" / "shared" / "check_rules.yaml")


def prompt_parts() -> dict:
    return load_yaml(ROOT / "skills" / "director" / "taste" / "prompt_parts.yaml")


def script_names(script, bible: dict) -> list[str]:
    errors = []
    known = known_names(bible)
    hint = (" — if this is an existing person, use exactly the bible name (" + "、".join(sorted(known - {"系统", "旁白"})) + "); "
            "a genuinely new person must be added through bible_additions with a voice")
    for scene in script.scenes:
        reported = set()
        for name in scene.cast + [l["who"] for l in scene.lines if "who" in l]:
            if name not in known and name not in reported:
                reported.add(name)
                errors.append(f"{scene.id}: unknown character {name}{hint}")
        if scene.location not in bible["Locations"]:
            errors.append(f"{scene.id}: unknown location {scene.location} — use a bible location ({'、'.join(sorted(bible['Locations']))}) or add it through bible_additions")
    return errors


def asset_rows(rows: list[dict], bible: dict) -> list[str]:
    errors, ids, placeholders = [], set(), set()
    for row in rows:
        for key in ("id", "type", "name", "placeholder", "description", "status"):
            if not row.get(key):
                errors.append(f"Asset missing {key}")
        if row.get("id") in ids:
            errors.append(f"Duplicate asset {row['id']}")
        if row.get("placeholder") in placeholders:
            errors.append(f"Duplicate placeholder {row['placeholder']}")
        ids.add(row.get("id"))
        placeholders.add(row.get("placeholder"))
        if row.get("type") not in rules()["asset_types"]:
            errors.append(f"Unknown asset type {row.get('type')}")
        if not re.fullmatch(r"@[\w\u4e00-\u9fff]+", row.get("placeholder", "")):
            errors.append(f"Invalid placeholder {row.get('placeholder')}")
        pattern = rules()["placeholder_patterns"].get(row.get("type"))
        if pattern and row.get("placeholder") != rules()["palette_placeholder"]:
            expected = pattern["child" if row.get("parent") else "master"]
            if not re.fullmatch(expected, row.get("placeholder", "")):
                errors.append(f"Placeholder {row.get('placeholder')} breaks the naming convention for {row.get('type')}{' variants' if row.get('parent') else ''}; use the form {pattern['example']}")
        if row.get("type") == "character" and row.get("name") not in known_names(bible):
            errors.append(f"Unknown character asset {row.get('name')}")
        if row.get("type") == "location" and row.get("name") not in bible["Locations"]:
            errors.append(f"Unknown location asset {row.get('name')}")
    by_id = {r.get("id"): r for r in rows}
    for row in rows:
        if row.get("parent"):
            parent = by_id.get(row["parent"])
            if not parent or parent.get("parent") or parent.get("name") != row.get("name") or parent.get("type") != row.get("type"):
                errors.append(f"{row['id']}: child must have one master of the same entity")
            if not row.get("what_changed"):
                errors.append(f"{row['id']}: child missing what_changed")
    return errors


def storyboard(board: dict, script, bible: dict, assets: list[dict], card: dict, *, speech_rate: float | None = None) -> list[str]:
    errors = []
    if board.get("episode") != script.episode:
        errors.append("Storyboard episode does not match script")
    scenes = {s.id: s for s in script.scenes}
    known = known_names(bible)
    by_id = {a["id"]: a for a in assets}
    unit_ids, covered_scenes = set(), set()
    units = board.get("units", [])
    if not units:
        errors.append("Storyboard has no units")
    scene_order = {s.id: i for i, s in enumerate(script.scenes)}
    last_scene = -1
    for unit in units:
        uid = unit.get("id", "")
        if not re.fullmatch(rf"ep{script.episode:02d}_u\d{{2,}}", uid) or uid in unit_ids:
            errors.append(f"Invalid or duplicate unit ID {uid}")
        unit_ids.add(uid)
        scene = scenes.get(unit.get("scene"))
        if not scene:
            errors.append(f"{uid}: unknown scene {unit.get('scene')!r}; scene must be exactly one of the script's scene IDs: {', '.join(scenes)}")
            continue
        covered_scenes.add(scene.id)
        if scene_order[scene.id] < last_scene:
            errors.append(f"{uid}: scenes out of order")
        last_scene = scene_order[scene.id]
        seconds = unit.get("seconds", 0)
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or not 0 < seconds <= card["max_seconds"]:
            errors.append(f"{uid}: unit length outside model maximum")
        shots = unit.get("shots", [])
        if not shots:
            errors.append(f"{uid}: no shots")
        limit = card.get("max_shots_per_unit")
        if limit and len(shots) > limit:
            errors.append(f"{uid}: {len(shots)} shots in one unit; the model handles at most {limit}. Keep one core action per unit and move the rest into a following unit")
        shortest = card.get("min_shot_seconds")
        for shot in shots:
            if shortest and isinstance(shot.get("seconds"), (int, float)) and shot["seconds"] < shortest:
                errors.append(f"{uid} {shot.get('id')}: shot of {shot['seconds']}s is under the {shortest}s minimum; merge it into a neighbouring shot")
            if speech_rate and card.get("speech_overrun_limit"):
                chars = sum(len(l.get("line", "")) for l in shot.get("dialogue", []))
                if isinstance(shot.get("seconds"), (int, float)) and shot["seconds"] > 0 and chars / speech_rate > shot["seconds"] * card["speech_overrun_limit"]:
                    errors.append(f"{uid} {shot.get('id')}: {chars} characters of dialogue cannot be spoken in {shot['seconds']}s at {speech_rate} chars/s; lengthen the shot, shorten the line, or split the unit")
        if abs(sum(s.get("seconds", 0) for s in shots) - seconds) > 0.01:
            errors.append(f"{uid}: shot lengths do not sum to unit length")
        if len({s.get('id') for s in shots}) != len(shots):
            errors.append(f"{uid}: duplicate shot IDs")
        onscreen = {n for shot in shots for n in shot.get("on_screen", [])}
        offscreen = set(unit.get("offscreen", []))
        absence = unit.get("absent", {})
        if isinstance(absence, list):
            absence = {a["name"]: a["reason"] for a in absence}
        for name in onscreen | offscreen | set(absence):
            if name not in known:
                errors.append(f"{uid}: unknown character {name}" + (" — on_screen, offscreen and absent take character names such as 沈砚, not asset IDs; props go in assets" if ":" in name or "@" in name else ""))
            if name not in scene.cast:
                errors.append(f"{uid}: {name} not in scene cast")
        missing = set(scene.cast) - onscreen - offscreen - {n for n, reason in absence.items() if str(reason).strip()}
        if missing:
            errors.append(f"{uid}: missing cast coverage {', '.join(sorted(missing))}")
        if onscreen & set(absence) or offscreen & set(absence):
            errors.append(f"{uid}: character both present and absent")
        selected = []
        for asset_id in unit.get("assets", []):
            if asset_id not in by_id:
                errors.append(f"{uid}: unknown asset {asset_id}; request it from B2")
            else:
                selected.append(by_id[asset_id])
        roots = [a.get("parent") or a["id"] for a in selected]
        if len(roots) != len(set(roots)):
            errors.append(f"{uid}: more than one version of an entity")
        mapped_characters = {a["name"] for a in selected if a["type"] == "character"}
        # A speaker listed only under Voices (an unseen old voice, a system) is heard through its voice asset, never drawn.
        voice_only = set(bible.get("Voices", {})) - set(bible.get("Characters", {}))
        # Only people who appear in a shot need an image reference. Off-screen people are described in words unless the storyboarder lists them.
        lacking = onscreen - mapped_characters - voice_only
        if lacking:
            errors.append(f"{uid}: present cast missing from assets: {', '.join(sorted(lacking))} — add their char: asset IDs to this unit's assets (an on-screen person needs a reference)")
        if not any(a["type"] == "location" and a["name"] == scene.location for a in selected):
            errors.append(f"{uid}: location missing from assets")
        for shot in shots:
            for line in shot.get("dialogue", []):
                if line["who"] not in known:
                    errors.append(f"{uid}: unknown speaker {line['who']}")
                if line["kind"] not in rules()["dialogue_kinds"]:
                    errors.append(f"{uid}: unknown dialogue kind {line['kind']}")
                expected = {"系统": "system", "旁白": "narration"}.get(line["who"])
                if expected and line["kind"] != expected:
                    errors.append(f"{uid}: {line['who']} must be {expected}, never an in-scene voice")
                if line["kind"] == "speech" and line["who"] not in shot["on_screen"]:
                    errors.append(f"{uid}: lip-synced speaker is not on screen")
            for sfx in shot.get("sfx", []):
                if not 0 <= sfx["at"] <= shot["seconds"]:
                    errors.append(f"{uid}: sound time outside shot")
            for overlay in shot.get("overlays", []):
                if not 0 <= overlay["start"] < overlay["end"] <= seconds:
                    errors.append(f"{uid}: overlay time outside unit")
    for missing in set(scenes) - covered_scenes:
        errors.append(f"Missing scene {missing}")
    return errors


def references(mapping: list[dict], card: dict) -> list[str]:
    errors = []
    counts = Counter(r["category"] for r in mapping)
    if len({r["placeholder"] for r in mapping}) != len(mapping):
        errors.append("Duplicate mapped placeholder")
    for category, limit in card["refs"].items():
        if counts[category] > limit:
            counted = [r["placeholder"] for r in mapping if r["category"] == category]
            errors.append(f"Reference limit: {category} {counts[category]} > {limit}; the {counts[category]} counted are {', '.join(counted)} "
                          f"(the palette and the previous-frame reference are added by code and count too). Cut {counts[category] - limit}: add reference_exclusions "
                          "(with a reason) for the palette, a resting prop or a minor character who appears only briefly, or split the unit. Excluding something that is not in the list above changes nothing")
    if card.get("max_total_refs") and len(mapping) > card["max_total_refs"]:
        errors.append("Total reference limit exceeded")
    for reference in mapping:
        if reference.get("status") != "approved" or not reference.get("description", "").strip():
            errors.append(f"Asset description not approved: {reference['placeholder']}")
    return errors


def prompt_body(text: str, mapping: list[dict]) -> str:
    """The model-written part of a prompt: everything except the code-written reference manifest lines."""
    manifest = tuple(r["placeholder"] + "：" for r in mapping)
    return "\n".join(line for line in text.splitlines() if not line.startswith(manifest))


def prompt(text: str, mapping: list[dict]) -> list[str]:
    mentioned = set(re.findall(r"@[\w\u4e00-\u9fff]+", text))
    used = set(re.findall(r"@[\w\u4e00-\u9fff]+", prompt_body(text, mapping)))
    expected = {r["placeholder"] for r in mapping}
    errors = []
    if mentioned - expected:
        errors.append("Unmapped placeholders: " + ", ".join(sorted(mentioned - expected)))
    # The manifest is written by code, so a mapped reference only counts as used when the model's own text names it.
    if expected - used:
        errors.append("Mapped placeholders never used in the prompt text (use each in the scene, sound or palette wording): " + ", ".join(sorted(expected - used)))
    for term in rules()["jargon"]:
        if term in text:
            errors.append(f"Pipeline jargon: {term}")
    return errors


def warnings(board: dict, config: dict, prompts: dict[str, str] | None = None) -> list[dict]:
    result = []
    runtime = sum(u["seconds"] for u in board["units"]) / 60
    window = config["episode_minutes"]
    if not window["min"] <= runtime <= window["max"]:
        result.append({"kind": "episode_runtime", "target": f"ep{board['episode']:02d}", "message": f"Estimated runtime {runtime:.2f} minutes outside {window['min']}–{window['max']}"})
    for unit in board["units"]:
        for shot in unit["shots"]:
            chars = sum(len(l["line"]) for l in shot["dialogue"])
            if chars / config["speech_rate_chars_per_sec"] > shot["seconds"]:
                result.append({"kind": "speech_fit", "target": unit["id"], "message": f"{shot['id']}: dialogue may not fit"})
            if shot.get("overlays"):
                result.append({"kind": "readable_text", "target": unit["id"], "message": "Render blank panels; add the overlay text in editing"})
        text = (prompts or {}).get(unit["id"], "")
        if len(text) > config["warnings"]["prompt_chars"]:
            result.append({"kind": "prompt_length", "target": unit["id"], "message": "Prompt exceeds configured length warning"})
        for word in rules()["ambiguous_words"]:
            if word in text:
                result.append({"kind": "ambiguous_position", "target": unit["id"], "message": f"Ambiguous word: {word}"})
        section = rules()["negative_section"]
        shots = text.rsplit(section, 1)[0] if section in text else text
        first_shot = re.search(r"^镜头\d", shots, re.M)
        shots = shots[first_shot.start():] if first_shot else shots
        found = [m for m in rules()["negative_markers"] if m in shots]
        if found:
            result.append({"kind": "negatives_in_shots", "target": unit["id"], "message": "Keep negatives in the final section: " + ", ".join(found)})
    return result
