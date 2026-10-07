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


def rhythm_errors(beats: list[dict], script, rules: dict) -> list[str]:
    """Mechanical shape of the beat sheet: a rising ladder of pressure, a burst, then payoff and a closing hook."""
    errors = []
    scenes = {s.id for s in script.scenes}
    if len(beats) < rules["min_beats"]:
        errors.append(f"Beat sheet has {len(beats)} beats; at least {rules['min_beats']} are needed to show the cadence")
    for beat in beats:
        if beat["scene"] not in scenes:
            errors.append(f"Beat refers to unknown scene {beat['scene']}")
    times = [b["at_seconds"] for b in beats]
    if times != sorted(times):
        errors.append("Beat times must not go backwards")
    if not beats:
        return errors
    if beats[-1]["kind"] != "hook":
        errors.append("The last beat must be the closing hook")
    bursts = [i for i, b in enumerate(beats) if b["kind"] == "burst"]
    if not bursts:
        errors.append("The episode needs a burst beat: one public, irreversible act that flips the balance of power")
    else:
        top = max(bursts, key=lambda i: (beats[i]["intensity"], -i))
        pressure = [b for b in beats[:top] if b["kind"] in ("press", "counter")]
        if len(pressure) < rules["min_pressure_before_burst"]:
            errors.append(f"Only {len(pressure)} pressure beats before the main burst; build at least {rules['min_pressure_before_burst']}, each heavier than the last")
        elif max(b["intensity"] for b in pressure) <= min(b["intensity"] for b in pressure):
            errors.append("The pressure beats before the burst must rise in intensity")
        if beats[top]["intensity"] < max((b["intensity"] for b in pressure), default=0):
            errors.append("The main burst must be at least as intense as the pressure that precedes it")
        if top >= len(beats) - 2:
            errors.append("Show the payoff or consequence after the burst before the hook")
    return errors


def rhythm_warnings(beats: list[dict], rules: dict, target: str) -> list[dict]:
    result = []
    if beats and (beats[0]["at_seconds"] > rules["first_conflict_by_seconds"] or beats[0]["kind"] not in ("press", "counter", "burst")):
        result.append({"target": target, "kind": "rhythm", "message": f"The first conflict beat should land within {rules['first_conflict_by_seconds']}s"})
    for before, after in zip(beats, beats[1:]):
        if after["at_seconds"] - before["at_seconds"] > rules["max_gap_seconds"]:
            result.append({"target": target, "kind": "rhythm", "message": f"{after['at_seconds'] - before['at_seconds']:g}s between beats at {before['at_seconds']:g}s and {after['at_seconds']:g}s; keep an emotional move every {rules['max_gap_seconds']}s"})
    return result


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
    from storyforge.checks import rules
    errors += rhythm_errors(value["beats"], script, rules()["rhythm"])
    return errors


def script_seconds(script_text, config) -> float:
    """Planning estimate from the script: dialogue at the speech rate plus a fixed allowance per action or sound line."""
    parsed = parse_script(script_text)
    dialogue = sum(len(l["line"]) for s in parsed.scenes for l in s.lines if "who" in l)
    other = sum(1 for s in parsed.scenes for l in s.lines if "who" not in l)
    return round(dialogue / config["speech_rate_chars_per_sec"] + other * config.get("action_seconds", 1.5))


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
            high = overlap >= config["warnings"].get("source_overlap_high", 0.4)
            result.append({"target":target,"kind":"source_overlap","level":"high" if high else "notice","message":f"Dialogue/source overlap {overlap:.1%}"+
                ("，高重合：多数台词照搬了原文" if high else "")+"; a differentiation signal, not an originality verdict"})
    return result
