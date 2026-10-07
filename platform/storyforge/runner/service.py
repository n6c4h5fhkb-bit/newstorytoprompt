from __future__ import annotations
from pathlib import Path
import json
import re
import uuid
import math

import yaml

from storyforge import ROOT
from storyforge.config import SflError, configuration, load_yaml, merge, model_card
from storyforge.store import Store, create_project, digest, serialize, now, atomic_write
from storyforge.runner import Runner, StageBlocked, import_episode, bind_adopted_script
from storyforge.cards import Cards
from storyforge.runner.metrics import production_metrics, review_signal
from storyforge.checks import prompt as check_prompt, references as check_references, rules
from storyforge.delivery import ready as delivery_ready
from storyforge.packets import file_source
from storyforge.delivery.prompts import style_parts, unit_label


def project_path(name: str, projects: Path) -> Path:
    if not name or name in (".", "..") or any(c in name for c in "/\\:"):
        raise SflError("Use a project folder name without path separators")
    path = (projects / name).resolve()
    if not path.is_relative_to(projects.resolve()):
        raise SflError("Project must stay inside the projects directory")
    return path


def new(name: str, projects: Path, *, novel: Path | None = None, novel_text: str | None = None, model: str = "seedance-2.0", demo=False) -> Store:
    config = {"name": name, "output": {"model_card": model, "ratio": "16:9", "music": "none"}}
    if demo:
        replay = {"provider": "replay", "model": "replay-fixture", "replay_dir": str(ROOT / "tests/fixtures/golden_project/responses")}
        config.update(demo=True, models={tier: dict(replay) for tier in ("strong", "cheap", "reviewer")})
    # Validate inputs before creating any persistent project state.
    if novel is not None and novel_text is not None:
        raise SflError("Provide a novel file or pasted text, not both")
    novel_text = novel.read_text(encoding="utf-8-sig") if novel else novel_text
    store = create_project(project_path(name, projects), config)
    if novel_text is not None:
        store.write("source/novel.txt", novel_text)
        store.append("decisions",{"event":"novel_imported","stage":"import","target":"project","choice":"adopt","by":"user","demo":bool(demo)})
        store.snapshot("import novel")
    if demo:
        fixture = ROOT / "tests/fixtures/golden_project"
        import_episode(store, (fixture / "ep01.md").read_text(encoding="utf-8"),
            (fixture / "bible.md").read_text(encoding="utf-8"), (fixture / "ep00.md").read_text(encoding="utf-8"))
        store.write(".state/demo.json", {"demo": True}, json_data=True)
        store.snapshot("mark explicit demo")
    return store


def status(store: Store) -> dict:
    stale = store.stale_artifacts()
    cards = Cards(store, configuration(store.root)).list()
    return {"project": store.root.name, "path": str(store.root), "demo": bool(configuration(store.root).get("demo")),
            "episodes": sorted(p.stem for p in (store.root / "episodes").glob("ep*.md")),
            "progress": {p.stem: {"script": bool(store.json(".state/locks.json", {}).get(p.stem, {}).get("locked")) and store.json(".state/locks.json",{}).get(p.stem,{}).get("script_fingerprint",digest(store.text(f"episodes/{p.stem}.md")))==digest(store.text(f"episodes/{p.stem}.md")) and (store.current(f"episodes/{p.stem}.md") or store.json(".state/locks.json", {}).get(p.stem, {}).get("source")=="user_import" and f"episodes/{p.stem}.md" not in stale),
                "script_stale":f"episodes/{p.stem}.md" in stale,
                "storyboard": store.current(f"storyboard/{p.stem}.json"),
                "prompts": sum(store.current(f"prompts/{p.stem}/{u['id'].split('_')[1]}.md") for u in store.json(f"storyboard/{p.stem}.json", {"units":[]})["units"]),
                "units": len(store.json(f"storyboard/{p.stem}.json", {"units":[]})["units"]),
                "delivery": delivery_ready(store, p.stem)} for p in (store.root / "episodes").glob("ep*.md")},
            "jobs": store.jobs(), "cards": cards, "stale": list(stale), "metrics": metrics(store)}


def call_metrics(calls: list[dict], first: dict) -> dict:
    real = [r for r in calls if not r.get("cached") and not r.get("demo")]
    unknown_cost = any(r.get("cost") is None for r in real)
    return {"real_calls": len(real), "demo_calls": sum(bool(r.get("demo")) and not r.get("cached") for r in calls),
            "cache_hits": sum(bool(r.get("cached")) for r in calls),
            "input_tokens": sum(r.get("input_tokens") or 0 for r in real), "output_tokens": sum(r.get("output_tokens") or 0 for r in real),
            "token_usage_incomplete": any(r.get("input_tokens") is None or r.get("output_tokens") is None for r in real),
            "cost": None if unknown_cost else sum(r["cost"] for r in real),
            "model_seconds": sum(r.get("seconds") or 0 for r in real), "reported_units": len(first),
            "first_pass_usable_rate": sum(r["result"] == "ok" for r in first.values()) / len(first) if first else None}


def metrics(store: Store) -> dict:
    calls, first = store.logs("calls"), {}
    for feedback in store.logs("feedback"):
        if feedback.get("result") in ("ok", "redo") and not feedback.get("demo"):
            first.setdefault(feedback["unit"], feedback)
    result = call_metrics(calls, first)
    episode_calls = {}
    for row in calls:
        match = re.match(r"ep\d+(?=[:_]|$)", row.get("target", ""))
        if match:
            episode_calls.setdefault(match[0], []).append(row)
    episodes = set(episode_calls) | {unit.split("_")[0] for unit in first}
    result["per_episode"] = {episode: call_metrics(episode_calls.get(episode, []),
        {unit: row for unit, row in first.items() if unit.split("_")[0] == episode}) for episode in sorted(episodes)}
    result["cards_created"] = sum(r.get("event") == "card_opened" for r in store.logs("decisions"))
    config = configuration(store.root)
    overrides = {c["target"]:c["answer"] for c in Cards(store,config).list(include_resolved=True) if c["kind"]=="budget" and c["answer"]}
    result["budgets"] = {}
    for episode,rows in episode_calls.items():
        used = sum((r.get("input_tokens") or 0)+(r.get("output_tokens") or 0) for r in rows)
        decision = overrides.get(episode,{})
        limit = int(decision["note"]) if decision.get("choice")=="raise" else config["token_budget"]["per_episode"]
        result["budgets"][episode] = {"used":used,"limit":limit,"alert":limit is not None and used>=limit*config["token_budget"]["alert_at"],
            "exceeded":limit is not None and used>=limit,"stopped":decision.get("choice")=="stop","usage_incomplete":any(r.get("input_tokens") is None or r.get("output_tokens") is None for r in rows)}
    measurements = production_metrics(store)
    for episode,measurement in measurements.pop("per_episode").items():
        if episode not in result["per_episode"]:
            result["per_episode"][episode] = call_metrics(episode_calls.get(episode,[]),{u:r for u,r in first.items() if u.startswith(episode+"_")})
        result["per_episode"][episode].update(measurement)
    result.update(measurements)
    result["review_signal"] = review_signal(store)
    return result


def feedback(store: Store, unit: str, result: str, note: str = "", *, generations=None, user_minutes=None, reasons=None):
    if result not in ("ok", "redo") or not re.fullmatch(r"ep\d{2,}_u\d{2,}", unit):
        raise SflError("Use a known unit and result ok or redo")
    episode = unit.split("_")[0]
    manifest = store.json(f"delivery/{episode}/manifest.json")
    delivered = next((u for u in (manifest or {}).get("units", []) if u["id"] == unit), None)
    if not delivered:
        raise SflError("Unit has not been delivered")
    reasons = [r for r in (reasons or []) if r]
    allowed = rules()["redo_reasons"]
    if reasons and result != "redo":
        raise SflError("Reasons only apply to a redo")
    if set(reasons) - set(allowed):
        raise SflError("Unknown redo reason; use: " + ", ".join(allowed))
    reported = {}
    if generations is not None:
        if type(generations) is not int or generations<1:
            raise SflError("Cumulative generations must be an integer of at least 1")
        reported["generations"] = generations
    if user_minutes is not None:
        if isinstance(user_minutes,bool) or not isinstance(user_minutes,(int,float)) or not math.isfinite(user_minutes) or user_minutes<0:
            raise SflError("Cumulative user minutes must be finite and nonnegative")
        reported["user_minutes"] = user_minutes
    with store.locked():
        previous = [r for r in store.logs("feedback") if r.get("unit")==unit]
        for field,value in reported.items():
            known = [r[field] for r in previous if r.get(field) is not None]
            if known and value<max(known):
                raise SflError(f"Cumulative {field} cannot decrease")
        store.append("feedback", {"unit": unit, "result": result, "note": note, "reasons": reasons, "prompt_fingerprint": delivered["prompt_fingerprint"],
                                  "demo": bool((manifest or {}).get("demo")),**reported})


def finish_episode(store: Store, episode: str, *, user_minutes=None, note: str = ""):
    if not re.fullmatch(r"ep\d{2,}",episode):
        raise SflError("Use an episode such as ep01")
    if user_minutes is not None and (isinstance(user_minutes,bool) or not isinstance(user_minutes,(int,float))
            or not math.isfinite(user_minutes) or user_minutes<0):
        raise SflError("Episode user minutes must be finite and nonnegative")
    with store.locked():
        manifest = store.json(f"delivery/{episode}/manifest.json")
        if not manifest or not manifest.get("units"):
            raise SflError("Episode has not been delivered")
        previous = [r["user_minutes"] for r in store.logs("feedback") if r.get("episode")==episode
                    and r.get("result")=="finished" and r.get("user_minutes") is not None]
        if user_minutes is not None and previous and user_minutes<max(previous):
            raise SflError("Cumulative episode user minutes cannot decrease")
        record = {"episode":episode,"result":"finished","note":note,"by":"user","demo":bool(manifest.get("demo")),
                  "delivery_fingerprint":digest(manifest)}
        if user_minutes is not None:record["user_minutes"] = user_minutes
        store.append("feedback",record)


REWRITE_STAGES = ("B5", "B7")


def rerun(store: Store, stage: str, target: str, note: str | None = None):
    note = (note or "").strip()
    if note and stage not in REWRITE_STAGES:
        raise SflError("A rewrite note is supported for B5 (storyboard, target epNN) and B7 (prompt, target epNN_uNN)")
    if note and not re.fullmatch(r"ep\d{2,}_u\d{2,}" if stage == "B7" else r"ep\d{2,}", target):
        raise SflError("Use epNN_uNN for a B7 prompt note and epNN for a B5 storyboard note")
    index = store.json(".state/artifacts.json", {})
    affected = False
    for path, meta in index.items():
        if meta["stage"] == stage and (meta["target"] == target or meta["target"].startswith(target + ":")
            or re.fullmatch(r"ep\d+_u\d+", target) and (path.endswith("/" + target + ".json")
                or path == f"prompts/{target.split('_')[0]}/{target.split('_')[1]}.md")):
            meta["stale"] = True
            affected = True
    if not affected:
        raise SflError("No completed artifact matches this stage and target")
    store.write(".state/artifacts.json", index, json_data=True)
    store.stale_artifacts()
    if stage in REWRITE_STAGES and (note or re.fullmatch(r"ep\d{2,}_u\d{2,}" if stage == "B7" else r"ep\d{2,}", target)):
        request = {"id": "r_" + uuid.uuid4().hex, "time": now(), "note": note, "applied": False}
        store.write(f".state/revision_requests/{stage}_{target}.json", request, json_data=True)
    store.append("decisions", {"stage": stage, "target": target, "choice": "rerun", "by": "user", **({"note": note} if note else {})})
    number = int(re.match(r"ep(\d+)", target)[1]) if re.match(r"ep(\d+)", target) else None
    resume(store)
    return Runner(store).run(episodes=[number] if number else None, allow_stale=[target])


def adopt_prompt(store: Store, unit: str, text: str | None = None) -> dict:
    """Make the user's own wording of a unit prompt current. With text, write it first; otherwise adopt the file as edited."""
    if not re.fullmatch(r"ep\d{2,}_u\d{2,}", unit):
        raise SflError("Use a unit such as ep01_u02")
    episode, short = unit.split("_")
    path = f"prompts/{episode}/{short}.md"
    with store.locked():
        index = store.json(".state/artifacts.json", {})
        if path not in index:
            raise SflError("This unit's prompt has not been generated yet")
        references = store.json(f"refs/units/{unit}.json")
        if not references:
            raise SflError("This unit has no reference mapping yet")
        candidate = text if text is not None else store.text(path)
        if not candidate.strip():
            raise SflError("A prompt cannot be empty")
        candidate = candidate.rstrip("\n") + "\n"
        errors = check_references(references, model_card(configuration(store.root))) + check_prompt(candidate, references)
        if errors:
            raise SflError("The prompt does not pass the hard checks: " + "; ".join(errors))
        if digest(candidate) == index[path]["fingerprint"] and digest(store.text(path)) == index[path]["fingerprint"]:
            raise SflError("The prompt is unchanged; nothing to adopt")
        if text is not None:
            store.write(path, candidate)
        index[path]["fingerprint"], index[path]["stale"], index[path]["adopted"] = digest(candidate), False, True
        store.write(".state/artifacts.json", index, json_data=True)
        store.write(f".state/adopted/{unit}.json", {"fingerprint": digest(candidate), "time": now()}, json_data=True)
        store.append("decisions", {"stage": "B7", "target": unit, "choice": "adopt", "by": "user", "event": "prompt_adopted"})
        store.stale_artifacts()
        store.snapshot(f"adopt prompt {unit}")
    return {"adopted": unit, "needs_run": True}


ASSET_FIELDS = ("description", "identity_notes", "image_prompt")


def edit_asset(store: Store, placeholder: str, *, description=None, identity_notes=None, image_prompt=None) -> dict:
    """Edit one asset's description, short identity notes or image prompt; prompts that use it become stale."""
    values = {k: v.strip() for k, v in (("description", description), ("identity_notes", identity_notes), ("image_prompt", image_prompt)) if v is not None}
    if not values:
        raise SflError("Give at least one of description, identity_notes or image_prompt")
    for key in ("description", "image_prompt"):
        if key in values and not values[key]:
            raise SflError(f"{key} cannot be empty")
    with store.locked():
        rows = store.assets()
        row = next((r for r in rows if r["placeholder"] == placeholder), None)
        if not row:
            raise SflError("Unknown asset placeholder")
        changed = {k: v for k, v in values.items() if (row.get(k) or "") != v}
        if not changed:
            raise SflError("Nothing to change")
        look_was_current = store.current(".state/look.json")
        row.update(changed)
        if "image_prompt" in changed and row["status"] == "needed":
            row["status"] = "described"
        store.write("assets.csv", Store.assets_text(rows))
        marker = f".state/asset_briefs/{digest(row['id'])[:24]}.json"
        meta = store.json(".state/artifacts.json", {}).get(marker)
        if meta and "description" in changed and "image_prompt" in changed:
            # The new description was written together with its own prompt, so the saved brief stays current.
            fields = ["id", "type", "name", "parent", "what_changed", "placeholder", "description"]
            source = file_source(store, "assets.csv", {"kind": "assets", "ids": [row["id"]], "fields": fields})
            parent = file_source(store, "assets.csv", {"kind": "assets", "ids": [row["parent"]], "fields": fields + ["image_prompt"]})
            bindings = source.bindings + parent.bindings + file_source(store, "style.md").bindings
            job = store.start_job("B3", f"{meta['target']}:asset:{row['id']}", bindings)
            store.accept(job, "B3", meta["target"], bindings,
                {marker: serialize({"asset_id": row["id"], "brief": row["image_prompt"], "source": "user_edit"}) + "\n"})
        if look_was_current:
            # Editing an asset is the user's own approval of it; do not ask for the look again.
            look_meta = store.json(".state/artifacts.json", {}).get(".state/look.json", {})
            ids = [a["id"] for a in rows if a["type"] == "character"]
            bindings = [store.binding("style.md"), store.binding("assets.csv", {"kind": "assets", "ids": ids})]
            job = store.start_job("B4", look_meta.get("target", "project"), bindings)
            store.accept(job, "B4", look_meta.get("target", "project"), bindings, {".state/look.json": serialize(store.json(".state/look.json")) + "\n"})
        store.append("decisions", {"stage": "B3", "target": row["id"], "choice": "edit_asset", "by": "user", "event": "asset_edited", "fields": sorted(changed)})
        store.stale_artifacts()
        store.snapshot(f"edit asset {placeholder}")
    return {"edited": placeholder, "fields": sorted(changed), "needs_run": True}


def gold_add(store: Store, unit: str, label: str, note: str = "") -> dict:
    """Keep a delivered unit as a labelled example, so a later rule change can be checked against what you judged good or bad."""
    if label not in ("good", "bad") or not re.fullmatch(r"ep\d{2,}_u\d{2,}", unit):
        raise SflError("Use a unit such as ep01_u02 and label good or bad")
    episode, short = unit.split("_")
    prompt = store.text(f"prompts/{episode}/{short}.md", "")
    mapping = store.json(f"refs/units/{unit}.json")
    board = store.json(f"storyboard/{episode}.json", {"units": []})
    entry = next((u for u in board["units"] if u["id"] == unit), None)
    if not prompt or not mapping or not entry:
        raise SflError("The unit needs a prompt, a reference mapping and a storyboard entry")
    store.write(f"gold/{unit}.json", {"unit": unit, "label": label, "note": note, "time": now(), "prompt": prompt, "mapping": mapping, "storyboard_unit": entry}, json_data=True)
    store.snapshot(f"gold example {unit} {label}")
    return {"gold": unit, "label": label}


def gold_check(store: Store) -> dict:
    """Re-run the mechanical checks over the labelled examples. A good example that now fails means a rule got stricter than your taste."""
    card = model_card(configuration(store.root))
    report = {"examples": 0, "good_now_failing": [], "bad_caught": [], "bad_not_mechanical": []}
    for path in sorted(store.path("gold").glob("*.json")) if store.path("gold").exists() else []:
        entry = json.loads(path.read_text(encoding="utf-8"))
        errors = check_references(entry["mapping"], card) + check_prompt(entry["prompt"], entry["mapping"])
        report["examples"] += 1
        if entry["label"] == "good" and errors:
            report["good_now_failing"].append({"unit": entry["unit"], "errors": errors})
        elif entry["label"] == "bad":
            report["bad_caught" if errors else "bad_not_mechanical"].append(entry["unit"])
    return report


def history(store: Store, target: str | None = None, limit: int = 30) -> list[dict]:
    """Snapshots the user can pass to sfl revert, newest first."""
    from storyforge.store import target_paths
    paths = ["--"] + target_paths(target) if target and target != "project" else []
    output = store.git("log", f"-{int(limit)}", "--date=iso-strict", "--pretty=format:%h%x09%cd%x09%s", *paths, check=False)
    return [dict(zip(("snapshot", "time", "message"), line.split("\t", 2))) for line in output.splitlines() if line.strip()]


def dismiss_script_note(store: Store, note_id: str, reason: str = "") -> dict:
    """The user rejects a pending script note (for example a reviewer's mistake); the writer and reviewers stop seeing it."""
    with store.locked():
        entries = store.json("notes/script_notes.json", [])
        entry = next((n for n in entries if n["id"] == note_id), None)
        if not entry:
            raise SflError("Unknown script note")
        if entry["status"] != "pending":
            raise SflError("Only a pending script note can be dismissed")
        entry["status"] = "dismissed"
        store.write("notes/script_notes.json", entries, json_data=True)
        store.write("notes/script_notes.md", "\n".join(f"- {n['target']}: {n['note'].replace(chr(10), ' ')}" for n in entries if n["status"] != "dismissed") + "\n")
        # Nothing was ever built from a pending note, so dropping it must not make finished work stale.
        index = store.json(".state/artifacts.json", {})
        for meta in index.values():
            for key in ("bindings", "job_bindings"):
                for binding in meta.get(key, []):
                    if binding["path"] == "notes/script_notes.json":
                        binding["fingerprint"] = store.fingerprint(binding)
        store.write(".state/artifacts.json", index, json_data=True)
        store.append("decisions", {"event": "script_note_dismissed", "note_id": note_id, "target": entry["target"], "choice": "dismiss", "by": "user", "reason": reason})
        store.stale_artifacts()
        store.snapshot(f"dismiss script note {entry['target']}")
    return {"dismissed": note_id, "target": entry["target"]}


def note(store: Store, target: str, text: str) -> dict:
    with store.locked():
        return _note(store, target, text)


def record_script_note(store: Store, target: str, text: str, *, by="user", origin=None, dedupe=None, bindings=None) -> dict:
    with store.locked():
        text = " ".join(text.split())
        if not text or not re.fullmatch(r"ep\d{2,}",target) or not store.path(f"episodes/{target}.md").exists():
            raise SflError("A script note requires a known episode and nonempty text")
        if by not in ("user","model"):
            raise SflError("Unknown script note author")
        if bindings and not store.unchanged(bindings):
            raise StageBlocked("Script review inputs changed; no obsolete note was adopted")
        entries = store.json("notes/script_notes.json",[])
        existing = next((n for n in entries if dedupe and n.get("dedupe")==dedupe),None)
        if existing:
            return existing
        if f"episodes/{target}.md" not in store.json(".state/artifacts.json",{}) and store.json(".state/locks.json",{}).get(target,{}).get("source")=="user_import":
            bind_adopted_script(store,int(target[2:]))
        record = {"id":"n_"+uuid.uuid4().hex,"time":now(),"target":target,"note":text,"status":"pending","by":by}
        if origin:record["origin"] = origin
        if dedupe:record["dedupe"] = dedupe
        entries.append(record)
        store.write("notes/script_notes.json",entries,json_data=True)
        store.write("notes/script_notes.md","\n".join(f"- {n['target']}: {n['note'].replace(chr(10), ' ')}" for n in entries)+"\n")
        store.append("decisions",{"event":"script_note",**record,"choice":"note","by":by})
        store.snapshot(f"script note {target}")
        return record


def _note(store: Store, target: str, text: str) -> dict:
    text = text.strip()
    if not text:
        raise SflError("A note cannot be empty")
    if re.fullmatch(r"ep\d{2,}", target):
        record_script_note(store,target,text)
        return {"target": target, "kind": "script", "pending": True}
    if not re.fullmatch(r"ep\d{2,}_u\d{2,}", target):
        raise SflError("Note target must be epNN or epNN_uNN")
    episode = target.split("_")[0]
    path = f"storyboard/{episode}.json"
    board = store.json(path)
    if not board or not store.current(path) or not any(u["id"] == target for u in board["units"]):
        raise SflError("A current storyboard unit is required for a continuity note")
    record = {"id": "n_" + uuid.uuid4().hex, "time": now(), "target": target, "note": text}
    notes = store.json("notes/continuity_notes.json", {})
    notes.setdefault(target, []).append(record)
    store.write("notes/continuity_notes.json", notes, json_data=True)
    baseline = store.json("notes/continuity_baselines.json", {})
    format_text = load_yaml(ROOT / "skills/director/taste/continuity_format.yaml")["carry_in"]
    changed = []
    found = False
    index = store.json(".state/artifacts.json", {})
    for board_file in sorted((store.root / "storyboard").glob("ep*.json"), key=lambda p: int(p.stem[2:])):
        relative = board_file.relative_to(store.root).as_posix()
        value = store.json(relative)
        edited = False
        for unit in value["units"]:
            if unit["id"] == target:
                found = True
                continue
            if not found:
                continue
            uid = unit["id"]
            baseline.setdefault(uid, unit["carry_in"])
            applicable = [n["note"] for source, entries in notes.items() for n in entries
                          if tuple(map(int, re.findall(r"\d+", source))) < tuple(map(int, re.findall(r"\d+", uid)))]
            unit["carry_in"] = format_text.format(planned=baseline[uid], notes="；".join(applicable))
            changed.append(uid)
            edited = True
        if edited:
            rendered = serialize(value) + "\n"
            store.write(relative, rendered)
            if relative in index:
                index[relative]["fingerprint"] = digest(rendered)
                index[relative].setdefault("continuity_notes", []).append(record["id"])
        if relative in index:
            # The explicit adoption updates only the note inputs of this canonical
            # board. Other stale inputs and the original model job record survive.
            for binding in index[relative]["bindings"]:
                if binding["path"] == "notes/continuity_notes.json":
                    binding["fingerprint"] = store.fingerprint(binding)
    store.write("notes/continuity_baselines.json", baseline, json_data=True)
    store.write(".state/artifacts.json", index, json_data=True)
    store.write("notes/continuity_notes.md", "\n".join(f"- {n['target']}: {n['note'].replace(chr(10), ' ')}" for entries in notes.values() for n in entries) + "\n")
    store.append("feedback", {"unit": target, "result": "continuity", "note": text, "note_id": record["id"],
                              "demo": bool(configuration(store.root).get("demo"))})
    store.append("decisions", {"event": "continuity_note", **record, "choice": "adopt", "by": "user", "following_units": changed})
    store.stale_artifacts()
    store.snapshot(f"continuity note {target}")
    return {"target": target, "kind": "continuity", "following_units": changed}


def episode_assets(store: Store, episode: str) -> list[dict]:
    placeholders = set(store.json(f".state/stages/{episode}_B2.json", {}).get("assets", []))
    ids = {asset for unit in store.json(f"storyboard/{episode}.json", {"units": []})["units"] for asset in unit["assets"]}
    rows = store.assets()
    selected = {a["id"] for a in rows if a["placeholder"] in placeholders or a["id"] in ids}
    while True:
        parents = {a["parent"] for a in rows if a["id"] in selected and a["parent"]}
        if parents <= selected:
            break
        selected |= parents
    return [a for a in rows if a["id"] in selected]


def episode_details(store: Store, episode: str) -> dict:
    if not re.fullmatch(r"ep\d{2,}", episode) or not store.path(f"episodes/{episode}.md").exists():
        raise SflError("Unknown episode")
    board = store.json(f"storyboard/{episode}.json", {"units": []})
    stale = store.stale_artifacts()
    units = []
    for unit in board["units"]:
        uid, short = unit["id"], unit["id"].split("_")[1]
        prompt = f"prompts/{episode}/{short}.md"
        units.append({**unit, "label": unit_label(unit, board["units"]), "prompt": store.text(prompt, ""), "references": store.json(f"refs/units/{uid}.json", []),
                      "stale": prompt in stale or not store.current(prompt), "delivered": store.path(f"delivery/{episode}/{short}.md").exists()})
    sample = None
    if configuration(store.root).get("demo") and not units:
        from storyforge.demo import preview
        sample = preview(episode, configuration(store.root))
    return {"episode": episode, "script": store.text(f"episodes/{episode}.md"), "units": units,
            "art_direction": style_parts(store.text("style.md", "")), "demo_preview": sample,
            "script_notes":[n for n in store.json("notes/script_notes.json",[]) if n["target"]==episode],
            "script_review":store.json(f".state/script_reviews/{episode}.json",{}),
            "beat_sheet": [line for line in store.text("plan.md", "").splitlines() if episode in line.lower()],
            "assets": store.assets(), "episode_assets": episode_assets(store, episode),
            "delivery_ready": delivery_ready(store, episode)}


def inbox(store: Store) -> dict:
    state = status(store)
    items = [{"id": c["id"], "type": "card", "card": c} for c in state["cards"] if c["status"] == "open"]
    for item in items:
        card = item["card"]
        if card["kind"] == "checkpoint" and card["stage"] == "B4":
            saved = card["details"].get("asset_preview", card["details"].get("characters", []))
            # The checkpoint approves character designs. Keep that snapshot;
            # the accompanying scene/prop preparation is the current asset sheet.
            assets = [a for a in saved if a["type"] == "character"] + [a for a in store.assets() if a["type"] != "character"]
            item["preview"] = {"style": store.text("style.md", ""), "art_direction": style_parts(store.text("style.md", "")), "assets": assets,
                               "characters": [a for a in assets if a["type"] == "character"]}
        elif card["kind"] == "checkpoint" and card["stage"] == "A8":
            item["preview"] = {"script":store.text("episodes/ep01.md", ""),"review":store.json(".state/script_reviews/ep01.json",{})}
        elif card["kind"] == "stuck" and card["stage"] in ("A7", "A9"):
            path = card["details"].get("candidate_file", "")
            saved = store.json(path, {}) if path.startswith(".state/stuck/") else {}
            candidate = saved.get("candidate") or {}
            if isinstance(candidate, dict) and isinstance(candidate.get("script"), str):
                item["preview"] = {"script":candidate["script"], "script_title":"待修订候选稿 · 尚未通过审查",
                    "review":{"passed":False,"errors":saved.get("errors",[]),"findings":saved.get("findings",[])}}
    grouped = {}
    for path, meta in store.stale_artifacts().items():
        # Applied rejection receipts are historical acknowledgements, not
        # creative outputs that need to be generated again under a newer look.
        if path.startswith((".state/fragments/", ".state/look_revisions/")) or re.fullmatch(r"refs/ep\d+\.json", path):
            continue
        target = meta["target"]
        unit = re.search(r"ep\d+_u\d+", path)
        prompt = re.fullmatch(r"(?:prompts|delivery)/(ep\d+)/(u\d+)\.md", path)
        if meta["stage"] == "B9":
            target = target.split(":")[0].split("_")[0]
        elif unit:
            target = unit[0]
        elif prompt:
            target = prompt[1] + "_" + prompt[2]
        if not re.match(r"ep\d+", target):
            continue
        key = target.split(":")[0]
        stage = meta["stage"]
        if key not in grouped or (stage[0],int(stage[1:])) < (grouped[key]["stage"][0],int(grouped[key]["stage"][1:])):
            grouped[key] = {"id": "stale:" + key, "type": "stale", "target": key, "stage": stage,
                            "question": f"{key} 的输入已变化，需要重新生成", "action": "rerun"}
    episode_flags = {k for k,v in grouped.items() if "_" not in k and v["stage"] != "B9"}
    unit_flags = {k.split("_")[0] for k in grouped if "_" in k}
    items += [v for k,v in grouped.items() if (k.split("_")[0] not in episode_flags or k in episode_flags)
              and not ("_" not in k and v["stage"] == "B9" and k in unit_flags)]
    latest = {}
    for job in state["jobs"]:
        latest[(job["stage"], job["target"])] = job
    items += [{"id": j["id"], "type": "paused", "target": j["target"], "stage": j["stage"],
               "question": j["error"], "action": "retry"} for j in latest.values() if j["status"] == "paused"]
    return {"project": store.root.name, "demo": state["demo"], "items": items, "queued_cards": sum(c["status"] == "queued" for c in state["cards"])}


def pause(store: Store) -> dict:
    store.write(".runtime/control.json", {"paused": True}, json_data=True)
    return {"paused": True, "project": store.root.name}


def resume(store: Store):
    store.write(".runtime/control.json", {"paused": False}, json_data=True)


def settings() -> dict:
    return load_yaml(ROOT / "config.yaml")


def update_settings(values: dict) -> dict:
    allowed = {"models", "token_budget", "card", "warnings", "episode_minutes", "speech_rate_chars_per_sec", "concurrency", "output", "fix_rounds", "hard_retries", "fix_policy", "external_attempts"}
    if set(values) - allowed:
        raise SflError("Unknown setting")
    updated = merge(settings(), values)
    validate_settings(updated)
    atomic_write(ROOT / "config.yaml", yaml.safe_dump(updated, allow_unicode=True, sort_keys=False))
    return updated


def validate_settings(updated: dict):
    if updated["output"]["ratio"] != "16:9":
        raise SflError("Only 16:9 is supported")
    if type(updated["concurrency"]["llm"]) is not int or not 1 <= updated["concurrency"]["llm"] <= 16:
        raise SflError("Concurrency must be 1–16")
    if not 1 <= updated["card"]["max_open_per_phase"] <= 3:
        raise SflError("At most three cards per phase")
    budget = updated["token_budget"]["per_episode"]
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise SflError("Token budget must be positive or null")
    if not 0 < updated["token_budget"]["alert_at"] <= 1:
        raise SflError("Budget alert fraction must be between zero and one")
    if updated["speech_rate_chars_per_sec"] <= 0 or not 0 < updated["episode_minutes"]["min"] <= updated["episode_minutes"]["max"]:
        raise SflError("Speech rate and episode range must be positive")
    model_card(updated)
    for profile in updated["models"].values():
        if profile["provider"] == "codex_cli" and (not profile.get("model") or profile.get("effort") not in ("low", "medium", "high", "xhigh", "max", "ultra")):
            raise SflError("Set a model and a valid reasoning effort")
    if not 0 <= updated["card"]["score_margin"] <= 1 or not 0 <= updated["warnings"]["source_overlap"] <= 1 or updated["warnings"]["prompt_chars"] <= 0:
        raise SflError("Invalid card or warning threshold")


def update_project_settings(store: Store, values: dict) -> dict:
    if set(values) - {"output", "episode_minutes", "speech_rate_chars_per_sec", "token_budget", "card", "warnings"}:
        raise SflError("Unknown project setting")
    updated = merge(configuration(store.root), values)
    validate_settings(updated)
    with store.locked():
        project_config = merge(load_yaml(store.path("project.yaml")), values)
        store.write("project.yaml", yaml.safe_dump(project_config, allow_unicode=True, sort_keys=False))
        store.append("decisions", {"event":"settings_changed", "target":"project", "choice":"update", "by":"user", "fields":list(values)})
        store.stale_artifacts()
        store.snapshot("update project settings")
    return project_config
