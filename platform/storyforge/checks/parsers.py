from __future__ import annotations
from dataclasses import dataclass, field
import re

from storyforge.config import SflError


@dataclass
class Scene:
    id: str
    location: str
    cast: list[str] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)
    text: str = ""
    cast_notes: dict[str, str] = field(default_factory=dict)


@dataclass
class Screenplay:
    episode: int
    title: str
    hook: str
    cliffhanger: str
    scenes: list[Scene]


def names(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[、,，]", text) if s.strip()]


def cast_names(text: str):
    # Commas inside a parenthesized staging note do not separate people.
    parts = re.split(r"[、,，](?![^（）()]*[）)])", text)
    cast, notes = [], {}
    for part in parts:
        match = re.fullmatch(r"\s*([^（）()]+?)(?:（([^（）()]+)）|\(([^（）()]+)\))?\s*", part)
        if not match:
            raise SflError("Cast entry needs a name and optional parenthesized staging note")
        name = match[1].strip()
        cast.append(name)
        if match[2] or match[3]:
            notes[name] = (match[2] or match[3]).strip()
    return cast, notes


def complete_voice_cast(text: str, bible: dict):
    """Declare already registered voice-only sources without inventing people."""
    voices = set(bible.get("Voices", {})) - set(bible.get("Characters", {})) - set(bible.get("Extras", {})) - {"系统", "旁白"}
    rows = text.splitlines(keepends=True)
    additions, start = [], None
    def finish(end):
        if start is None:
            return
        cast_index = next((i for i in range(start, end) if rows[i].strip().startswith(("出场：", "出场:"))), None)
        if cast_index is None:
            return
        cast, _ = cast_names(rows[cast_index].strip()[3:])
        missing = []
        for raw in rows[start:end]:
            match = re.fullmatch(r"([^：:（）()]+)[（(]画外(?:[、,，][^（）()]+)?[）)][：:]\s*(.+)", raw.strip())
            if match and match[1].strip() in voices and match[1].strip() not in cast + missing:
                missing.append(match[1].strip())
        if missing:
            ending = "\r\n" if rows[cast_index].endswith("\r\n") else "\n" if rows[cast_index].endswith("\n") else ""
            rows[cast_index] = rows[cast_index].rstrip("\r\n") + "、" + "、".join(missing) + ending
            additions.append({"scene":rows[start].strip().split()[1], "voice_sources":missing})
    for i, row in enumerate(rows):
        if row.strip().startswith("## S"):
            finish(i)
            start = i
    finish(len(rows))
    return "".join(rows), additions


def parse_script(text: str) -> Screenplay:
    episode, title, hook, cliffhanger = None, "", "", ""
    scenes: list[Scene] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = re.fullmatch(r"# EP(\d+)\s+(.+)", line)
        if match:
            if episode is not None:
                raise SflError("Multiple episode headers")
            episode, title = int(match[1]), match[2]
            continue
        if line.startswith("hook:"):
            hook = line[5:].strip()
            continue
        if line.startswith("cliffhanger:"):
            cliffhanger = line[12:].strip()
            continue
        match = re.fullmatch(r"## (S\d+)\s+(.+?)\s*·\s*(.+?)\s*·\s*(内|外)", line)
        if match:
            scenes.append(Scene(match[1], match[2].strip(), text=line + "\n"))
            continue
        if not scenes:
            raise SflError(f"Line {number}: expected episode header or scene")
        scene = scenes[-1]
        scene.text += raw + "\n"
        if line.startswith("出场：") or line.startswith("出场:"):
            if scene.cast:
                raise SflError(f"Line {number}: duplicate cast line")
            scene.cast, scene.cast_notes = cast_names(line[3:])
        elif line.startswith("△"):
            scene.lines.append({"kind": "action", "line": line[1:].strip()})
        elif line.startswith("【音效】"):
            scene.lines.append({"kind": "sfx", "line": line[4:].strip()})
        elif line.startswith("【环境】"):
            scene.lines.append({"kind": "ambience", "line": line[4:].strip()})
        else:
            match = re.fullmatch(r"([^：:（）()]+)(?:[（(]([^（）()]+)[）)])?[：:]\s*(.+)", line)
            if not match:
                raise SflError(f"Line {number}: unrecognized screenplay line: {line}")
            who = match[1].strip()
            modifiers = names(match[2]) if match[2] else []
            if modifiers and (modifiers[0] not in ("画外", "心声") or any(m in ("画外", "心声") for m in modifiers[1:])):
                raise SflError(f"Line {number}: use one speech kind, followed by optional staging notes")
            kind = {"画外": "offscreen", "心声": "thought"}.get(modifiers[0] if modifiers else None, "speech")
            if who in ("系统", "旁白"):
                if match[2]:
                    raise SflError(f"Line {number}: reserved speaker cannot use a speech modifier")
                kind = "system" if who == "系统" else "narration"
            entry = {"who": who, "kind": kind, "line": match[3]}
            if len(modifiers) > 1:
                entry["delivery_notes"] = "，".join(modifiers[1:])
            scene.lines.append(entry)
    if not episode or not title or not hook or not cliffhanger or not scenes:
        raise SflError("Script needs # EPnn title, hook, cliffhanger and at least one scene")
    if len({s.id for s in scenes}) != len(scenes):
        raise SflError("Duplicate scene IDs")
    for scene in scenes:
        if not scene.cast or len(scene.cast) != len(set(scene.cast)):
            raise SflError(f"{scene.id}: missing or duplicate cast members")
        for line in scene.lines:
            if line.get("kind") in ("speech", "offscreen", "thought") and line["who"] not in scene.cast:
                raise SflError(f"{scene.id}: speaker {line['who']} missing from cast")
    return Screenplay(episode, title, hook, cliffhanger, scenes)


def parse_bible(text: str) -> dict:
    result = {"Characters": {}, "Extras": {}, "Locations": {}, "Voices": {}, "Setting": {}, "other": {}}
    section, headers = "other", None
    for raw in text.splitlines():
        line = raw.strip().replace("\\_", "_")
        if line.startswith("## "):
            section = line[3:].strip()
            result.setdefault(section, {})
            headers = None
        elif line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r"[-: ]+", c) for c in cells):
                continue
            if headers is None:
                headers = cells
            elif cells and cells[0]:
                row = dict(zip(headers, cells))
                result[section][cells[0]] = row
        elif line and not line.startswith("#"):
            if section == "Extras":
                result[section].update({name: {"name": name} for name in names(line.rstrip(".…"))})
            else:
                result[section][str(len(result[section]))] = line
    return result


def known_names(bible: dict) -> set[str]:
    return set(bible.get("Characters", {})) | set(bible.get("Extras", {})) | set(bible.get("Voices", {})) | {"系统", "旁白"}


def bible_slice(text: str, selected: list[str], locations: list[str]) -> dict:
    bible = parse_bible(text)
    return {"Characters": {k: v for k, v in bible["Characters"].items() if k in selected},
            "Extras": {k: v for k, v in bible["Extras"].items() if k in selected},
            "Locations": {k: v for k, v in bible["Locations"].items() if k in locations},
            "Voices": {k: v for k, v in bible["Voices"].items() if k in selected},
            "Props": {k:v for k,v in bible.get("Props", {}).items() if k in selected},
            "UI": {k:v for k,v in bible.get("UI", {}).items() if k in selected},
            "Setting": bible["Setting"], "Visual style": bible.get("Visual style", {})}
