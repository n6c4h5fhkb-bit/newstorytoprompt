"""Source-address and structural checks; creative judgments remain in skills/."""
from collections import Counter
from difflib import SequenceMatcher
import re

from storyforge.checks import script_names
from storyforge.checks.parsers import known_names, parse_bible, parse_script
from storyforge.config import SflError


def references(refs, index):
    return [f"Unknown source paragraph: {r}" for r in refs if r not in index["paragraphs"]]


def summary(value, chunk, index):
    errors = []
    if value["chunk_id"] != chunk["id"]:
        errors.append("Summary chunk_id must match the requested chunk")
    allowed = {p["ref"] for p in chunk["paragraphs"]}
    ids = [e["id"] for e in value["events"]]
    if len(ids) != len(set(ids)):
        errors.append("Duplicate event IDs")
    for entry in value["events"] + value["entities"]:
        errors += references(entry["source_refs"], index)
        if not set(entry["source_refs"]) <= allowed:
            errors.append("Summary references a paragraph outside its chunk")
    return errors


def breakdown(value, index):
    ids = [b["id"] for b in value["beats"]]
    errors = [] if len(ids) == len(set(ids)) else ["Duplicate beat IDs"]
    for beat in value["beats"]:
        errors += references(beat["source_refs"], index)
    return errors


def plan(value, timeline, index):
    errors, known = [], {b["id"] for b in timeline["beats"]}
    actions = {d["beat_id"]:d for d in value["decisions"]}
    if set(actions) != known or len(actions) != len(value["decisions"]):
        errors.append("Every source beat needs exactly one keep/cut/merge decision")
    included = Counter(b for ep in value["episodes"] for b in ep["beats"])
    for beat, decision in actions.items():
        expected = 1 if decision["action"] == "keep" else 0
        if included[beat] != expected:
            errors.append(f"{beat}: {decision['action']} needs {expected} episode assignments, got {included[beat]}")
        if decision["action"] == "merge":
            destination = actions.get(decision["merge_into"])
            if not destination or destination["action"] != "keep" or decision["merge_into"] == beat:
                errors.append(f"{beat}: merge_into must name a kept beat")
    if set(included) - known:
        errors.append("Episode contains an unknown beat")
    numbers = [e["number"] for e in value["episodes"]]
    if numbers != list(range(1, len(numbers)+1)):
        errors.append("Episode numbers must be consecutive and ordered from 1")
    by_id = {b["id"]:b for b in timeline["beats"]}
    episode_source_beats = {}
    for episode in value["episodes"]:
        errors += references(episode["source_refs"], index)
        source_beats = set(episode["beats"]) | {d["beat_id"] for d in actions.values() if d["action"] == "merge" and d["merge_into"] in episode["beats"]}
        episode_source_beats[episode["number"]] = source_beats
        required_refs = {r for b in source_beats if b in by_id for r in by_id[b]["source_refs"]}
        if not required_refs <= set(episode["source_refs"]):
            errors.append(f"ep{episode['number']:02d}: missing kept or merged beat source passages")
    keys = [h["key"] for h in value["hooks"]]
    if len(keys) != len(set(keys)) or value["selected_hook"] not in keys:
        errors.append("Opening hook choice must name a unique candidate")
    moment_ids = [m["id"] for m in value["moments"]]
    if len(moment_ids) != len(set(moment_ids)):
        errors.append("Duplicate moment IDs")
    if not any(m["key"] and m["episode"] == 1 for m in value["moments"]):
        errors.append("Opening needs a key moment")
    for moment in value["moments"]:
        errors += references(moment["source_refs"], index)
        if moment["episode"] not in numbers or not set(moment["beat_ids"]) <= known:
            errors.append(f"{moment['id']}: unknown episode or beat")
        elif not set(moment["beat_ids"]) <= episode_source_beats[moment["episode"]]:
            errors.append(f"{moment['id']}: moment beats must belong to its episode")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", moment["id"]):
            errors.append("Moment ID must be a safe filename identifier")
    for ep in value["episodes"]:
        if ep["major_turn"] and not any(m["key"] and m["episode"]==ep["number"] for m in value["moments"]):
            errors.append(f"ep{ep['number']:02d}: a major turning point needs a key moment")
    bible = parse_bible(value["bible"])
    characters = bible["Characters"]
    fields = ("name", "role", "look", "voice", "source name")
    if not characters or any(not isinstance(row, dict) or row.get("name") != name
        or any(not str(row.get(field, "")).strip() for field in fields) for name, row in characters.items()):
        errors.append("bible needs a Markdown table under '## Characters' with non-empty columns: name | role | look | voice | source name; prose is not a character row")
    for ep in value["episodes"]:
        missing_names = set(ep["characters"]) - known_names(bible)
        if missing_names:
            errors.append(f"ep{ep['number']:02d}: declare these character names in bible tables: {', '.join(sorted(missing_names))}")
        missing_locations = set(ep["locations"]) - set(bible["Locations"])
        if missing_locations:
            errors.append(f"ep{ep['number']:02d}: declare these locations in the bible '## Locations' name | look table: {', '.join(sorted(missing_locations))}")
    return errors


def episode(value, number, bible):
    try:
        script = parse_script(value["script"])
    except SflError as exc:
        return [str(exc)]
    errors = script_names(script, parse_bible(bible))
    if value["episode"] != number or script.episode != number:
        errors.append("Episode number must match the requested episode")
    if not value["ledger_out"].strip():
        errors.append("Ending ledger cannot be empty")
    return errors


def warnings(seconds, config, target, *, script="", passages=None):
    result = []
    lower, upper = (config["episode_minutes"][k] * 60 for k in ("min", "max"))
    if not lower <= seconds <= upper:
        result.append({"target":target,"kind":"runtime","message":f"Estimated runtime {seconds:g}s is outside {lower:g}–{upper:g}s"})
    if script and passages:
        parsed = parse_script(script)
        lines = [l["line"] for s in parsed.scenes for l in s.lines if "who" in l]
        dialogue = "".join(lines)
        source = "\n".join(p["text"] for p in passages)
        matched = sum(block.size for block in SequenceMatcher(None, dialogue, source, autojunk=False).get_matching_blocks())
        overlap = matched / len(dialogue) if dialogue else 0
        if overlap >= config["warnings"]["source_overlap"]:
            result.append({"target":target,"kind":"source_overlap","message":f"Dialogue/source overlap {overlap:.1%}; a differentiation signal, not an originality verdict"})
    return result
