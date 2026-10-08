from __future__ import annotations
from copy import deepcopy
import re

from storyforge import ROOT
from storyforge.config import SflError, load_yaml
from storyforge.checks import asset_rows, style_lock as check_style_lock, storyboard as check_storyboard, references as check_references, prompt as check_prompt, warnings, rules
from storyforge.checks.parsers import parse_script, parse_bible
from storyforge.delivery import export
from storyforge.delivery.prompts import render, style_document, style_parts, unit_label
from storyforge.llm import CallPaused
from storyforge.packets import Source, file_source, external_source
from storyforge.refs import mapping, MissingAssets
from storyforge.runner import StageBlocked, ScriptRevisionNeeded
from storyforge.store import Store, digest, serialize


NON_CHARACTER_ID = re.compile(r"^(?:prop|loc|ui|voice|music|layout):")


def normalize_cast_names(board: dict) -> None:
    """on_screen, offscreen and absent hold character names; a model sometimes writes asset IDs (char:沈砚@受伤, prop:旧玉佩) there."""
    def clean(name):
        name = str(name).strip()
        if NON_CHARACTER_ID.match(name):
            return None  # a prop or a place belongs in assets, never in the cast lists
        if name.startswith("char:"):
            name = name[5:]
        return name.split("@")[0].strip() or None
    for unit in board.get("units", []):
        for shot in unit.get("shots", []):
            shot["on_screen"] = list(dict.fromkeys(n for n in map(clean, shot.get("on_screen", [])) if n))
        unit["offscreen"] = list(dict.fromkeys(n for n in map(clean, unit.get("offscreen", [])) if n))
        seen = set()
        kept = []
        for entry in unit.get("absent", []):
            name = clean(entry.get("name", "")) if isinstance(entry, dict) else None
            if name and name not in seen:
                seen.add(name)
                kept.append({**entry, "name": name})
        unit["absent"] = kept


def normalize_scenes(board: dict, scene_ids: list[str]) -> None:
    """A unit's scene is the ID (S01); a model sometimes writes the whole heading (S01 后山草棚 · 夜 · 内)."""
    for unit in board.get("units", []):
        match = re.match(r"S\d+", str(unit.get("scene", "")).strip())
        if match and match[0] in scene_ids and unit["scene"] != match[0]:
            unit["scene"] = match[0]


def normalize_request(request: str) -> str:
    """An asset request names an ID or placeholder; a model sometimes appends a description in brackets, which is not part of the ID."""
    return re.split(r"[（(]", request.strip(), maxsplit=1)[0].strip()


class Director:
    def __init__(self, runner, number: int):
        self.runner, self.store, self.config = runner, runner.store, runner.config
        self.number, self.episode = number, f"ep{number:02d}"
        self._script_source = file_source(self.store,f"episodes/{self.episode}.md")
        self.script = parse_script(self._script_source.data)
        self.bible = parse_bible(self.store.text("bible.md"))
        locks = self.store.json(".state/locks.json", {})
        if not locks.get(self.episode, {}).get("locked"):
            raise SflError("Pipeline B requires a locked episode")
        fingerprint = locks[self.episode].get("script_fingerprint")
        if fingerprint and fingerprint!=digest(self.store.text(f"episodes/{self.episode}.md")):
            raise StageBlocked("Lock the current script version before pipeline B")
        if f"episodes/{self.episode}.md" in self.store.stale_artifacts() or any(n["target"]==self.episode and n["status"]=="pending" for n in self.store.json("notes/script_notes.json",[])):
            raise StageBlocked("Apply and review this episode's script changes before pipeline B")

    def sources(self) -> dict[str, Source]:
        names = {n for s in self.script.scenes for n in s.cast} | {l["who"] for s in self.script.scenes for l in s.lines if "who" in l}
        text = self._script_source.data
        names.update(name for section in ("Props", "UI") for name in self.bible.get(section,{}) if name in text)
        names = sorted(names)
        locations = sorted({s.location for s in self.script.scenes})
        model = external_source(self.store, ROOT / "model_cards" / f"{self.config['output']['model_card']}.yaml")
        model.bindings += file_source(self.store, "project.yaml", {"kind": "yaml_keys", "keys": ["output", "episode_minutes"]}).bindings
        model.data = self.runner.card
        return {"bible": file_source(self.store, "bible.md", {"kind": "bible", "names": names, "locations": locations}),
                "script": deepcopy(self._script_source),
                "beats": file_source(self.store, f"beats/{self.episode}.json"),
                "style": file_source(self.store, "style.md"),
                "assets": file_source(self.store, "assets.csv"),
                "ledger_in": file_source(self.store, f"ledger/ep{self.number-1:02d}.md"),
                "model_card": model,
                "continuity_notes": file_source(self.store, "notes/continuity_notes.json", {"kind":"continuity", "episode":self.number}),
                "asset_requests": file_source(self.store, f".state/asset_requests/{self.episode}.json")}

    def marker(self, stage: str) -> str:
        return f".state/stages/{self.episode}_{stage}.json"

    def revision(self, stage: str, target: str) -> dict | None:
        """A pending user-requested rewrite (sfl rerun --note), consumed once the new result is saved."""
        request = self.store.json(f".state/revision_requests/{stage}_{target}.json")
        return request if request and not request.get("applied") else None

    def finish_revision(self, stage: str, target: str):
        path = f".state/revision_requests/{stage}_{target}.json"
        self.store.write(path, {**self.store.json(path), "applied": True}, json_data=True)

    @staticmethod
    def revision_repairs(request: dict, previous) -> dict:
        # A fresh retry_id makes the call miss the cache, so a rerun never returns the identical saved answer.
        repairs = {"retry_id": request["id"]}
        if request.get("note"):
            repairs["user_note"] = {"choice": "retry", "note": request["note"]}
            if previous is not None:
                repairs["previous_output"] = previous
        return repairs

    def art_direction(self, stage: str):
        rejected = [c for c in self.runner.cards.list(include_resolved=True) if c["kind"] == "checkpoint" and c["stage"] == "B4"
                    and c["answer"] and c["answer"]["choice"] == "reject" and not self.store.path(f".state/look_revisions/{c['id']}.json").exists()]
        if self.store.current("style.md") and style_parts(self.store.text("style.md"))["art_prompt"] and not rejected:
            return
        sources = self.sources()
        for binding in sources["script"].bindings:
            binding["style_reference"] = True
        repairs = {}
        if rejected:
            repairs = {"user_note": rejected[-1]["answer"], "previous_output": self.store.text("style.md", "")}
            sources["script"].bindings.append(self.store.binding("logs/decisions.jsonl", {"kind": "decision", "card_id": rejected[-1]["id"]}))
        def output(value, store):
            result = {"style.md": style_document(value)}
            if rejected:
                result[f".state/look_revisions/{rejected[-1]['id']}.json"] = serialize({"applied": True}) + "\n"
            return result
        self.runner.work(stage, self.episode, sources, lambda value: check_style_lock(value["style_lock"]), output, repairs=repairs)

    def asset_extract(self, stage: str, *, force=False):
        if not force and self.store.current(self.marker(stage)):
            return
        sources = self.sources()
        rows = self.store.assets()
        sources["assets"].data = rows
        # Existing rows are used during extraction, but later brief/approval writes
        # to those same rows must not restart the completed extraction stage.
        for binding in sources["assets"].bindings:
            binding["style_reference"] = True
        requests = [normalize_request(r) for r in self.store.json(f".state/asset_requests/{self.episode}.json", [])]
        sources["asset_requests"].data = requests
        def merged(value, current):
            result = {row["id"]: row for row in current}
            for asset in value["assets"]:
                prefix = rules()["asset_id_prefixes"][asset["type"]]
                asset_id = prefix + ":" + asset["name"] + ("@" + asset["variant"] if asset["variant"] else "")
                old = result.get(asset_id, {})
                if old and (old.get("type"), old.get("name"), old.get("parent") or "", old.get("placeholder")) == (asset["type"], asset["name"], asset["parent"], asset["placeholder"]):
                    # A finished asset is never silently rewritten by a later extraction; change it with `sfl asset`.
                    if not old.get("identity_notes") and asset["identity_notes"]:
                        old["identity_notes"] = asset["identity_notes"]
                    continue
                row = {"id": asset_id, **{k: asset[k] for k in ("type", "name", "parent", "what_changed", "placeholder", "description", "identity_notes")},
                       "image_prompt": "", "status": "needed"}
                # identity_notes only shortens the prompt's reference line; it never invalidates a finished brief.
                if all(old.get(k) == row[k] for k in ("type", "name", "parent", "what_changed", "placeholder", "description")):
                    row.update(image_prompt=old["image_prompt"], status=old["status"])
                result[asset_id] = row
            return list(result.values())
        def validate(value):
            merged_rows = merged(value, rows)
            available = {r["id"] for r in merged_rows} | {r["placeholder"] for r in merged_rows}
            missing_notes = [a["placeholder"] for a in value["assets"] if a["type"] in ("character", "location", "prop") and not a["identity_notes"].strip()]
            return asset_rows(merged_rows, self.bible) + [f"identity_notes required for {placeholder}" for placeholder in missing_notes] + [f"Requested asset still missing: {request}" for request in requests if request not in available]
        self.runner.work(stage, self.episode, sources,
            validate,
            lambda value, store: {"assets.csv": Store.assets_text(merged(value, store.assets())),
                                  self.marker(stage): serialize({"assets": [a["placeholder"] for a in value["assets"]]}) + "\n"})

    def asset_briefs(self, stage: str):
        # Extraction order is not a dependency order. A child receives the
        # completed master brief, including when both are new in this episode.
        for asset in sorted(self.store.assets(), key=lambda a: bool(a["parent"])):
            marker = f".state/asset_briefs/{digest(asset['id'])[:24]}.json"
            if asset["status"] != "needed" and self.store.current(marker):
                continue
            sources = self.sources()
            fields = ["id", "type", "name", "parent", "what_changed", "placeholder", "description"]
            source = file_source(self.store, "assets.csv", {"kind": "assets", "ids": [asset["id"]], "fields": fields})
            source.data = source.data[0]
            parent = next((a for a in self.store.assets() if a["id"] == asset["parent"]), None)
            parent_source = file_source(self.store, "assets.csv", {"kind": "assets", "ids": [asset["parent"]], "fields": fields + ["image_prompt"]})
            parent_source.data = parent_source.data[0] if parent else None
            sources.update(asset=source, parent=parent_source)
            # Adopt an imported description once. Generated briefs retain their
            # individual dependencies even though the CSV is shared.
            if asset["status"] != "needed" and asset.get("image_prompt") and not self.store.path(marker).exists():
                bindings = source.bindings + parent_source.bindings + sources["style"].bindings
                job = self.store.start_job(stage, f"{self.episode}:asset:{asset['id']}", bindings)
                if not self.store.accept(job, stage, self.episode, bindings,
                    {marker: serialize({"asset_id": asset["id"], "brief": asset["image_prompt"], "source": "user_import"}) + "\n"}):
                    raise StageBlocked("Imported asset description changed during adoption")
                continue
            def output(value, store, asset_id=asset["id"], marker=marker):
                rows = store.assets()
                for row in rows:
                    if row["id"] == asset_id:
                        row.update(image_prompt=value["brief"], status="approved" if store.current(".state/look.json") else "described")
                return {"assets.csv": Store.assets_text(rows), marker: serialize({"asset_id": asset_id, "brief": value["brief"], "source": "model"}) + "\n"}
            self.runner.work(stage, f"{self.episode}:asset:{asset['id']}", sources, lambda value: [], output)

    def look_approval(self, stage: str):
        if self.store.current(".state/look.json"):
            # New descriptions follow the approved style without another fixed checkpoint.
            rows = self.store.assets()
            if any(a["status"] == "described" for a in rows):
                for row in rows:
                    if row["status"] == "described":
                        row["status"] = "approved"
                self.store.write("assets.csv", Store.assets_text(rows))
                self.store.snapshot("approve new descriptions under project look")
            return
        rows = self.store.assets()
        if not rows or any(a["status"] == "needed" for a in rows):
            raise StageBlocked("Asset descriptions are not ready for look approval")
        style_binding = self.store.binding("style.md")
        selected_ids = [a["id"] for a in rows if a["type"] == "character"]
        bindings = [style_binding, self.store.binding("assets.csv", {"kind": "assets", "ids": selected_ids})]
        revision = self.store.json(".state/artifacts.json", {}).get("style.md", {}).get("job_id")
        key = "look:" + digest([bindings, revision])
        card = self.runner.cards.find(key)
        if card and card["answer"]:
            if card["answer"]["choice"] == "reject":
                raise StageBlocked("Look rejected; use rerun B1 with the user's note")
            for row in rows:
                row["status"] = "approved"
            job = self.store.start_job(stage, self.episode, bindings)
            if not self.store.accept(job, stage, self.episode, bindings,
                {"assets.csv": Store.assets_text(rows), ".state/look.json": serialize({"card": card["id"], "approved": True}) + "\n"}):
                raise StageBlocked("Look inputs changed after approval")
            return
        card = self.runner.cards.create(kind="checkpoint", stage=stage, target="project",
            question="确认项目视觉方案和主要角色资产描述", options=[{"key": "approve", "label": "批准描述"}, {"key": "reject", "label": "备注修改要求"}],
            recommended="approve", reason="美术风格和人物设计需要你的偏好判断", dedupe=key,
            details={"episode":self.episode,"style": "style.md", "assets": "assets.csv", "asset_preview": rows,
                     "characters": [a for a in rows if a["type"] == "character"]})
        raise StageBlocked("Look approval required: " + card["id"])

    def previous_carry(self) -> Source:
        if self.number == 1:
            return Source("", [])
        path = f"storyboard/ep{self.number-1:02d}.json"
        board = self.store.json(path)
        if not board or not self.store.current(path):
            raise StageBlocked("Previous episode storyboard must finish before this episode's storyboard")
        unit = board["units"][-1]
        notes = file_source(self.store, "notes/continuity_notes.json", {"kind":"continuity", "episode":self.number-1})
        carry = unit["carry_out"]
        format_bindings = []
        if notes.data:
            template = load_yaml(ROOT / "skills/director/taste/continuity_format.yaml")["carry_in"]
            format_bindings = external_source(self.store, ROOT / "skills/director/taste/continuity_format.yaml", style_reference=True).bindings
            carry = template.format(planned=carry, notes="；".join(n["target"] + "：" + n["note"] for n in notes.data))
        return Source(carry, file_source(self.store, path, {"kind": "unit_carry", "id": unit["id"]}).bindings + notes.bindings + format_bindings)

    def scene_reviews(self, candidate: dict, challenges: list[dict], previous=None) -> list[dict]:
        result = []
        previous_carry = self.previous_carry()
        for scene in self.script.scenes:
            units = [u for u in candidate["units"] if u["scene"] == scene.id]
            sources = self.sources()
            sources.update(script_scene=Source(scene.text, sources["script"].bindings), units=Source(units, []),
                previous_carry_out=previous_carry, warnings=Source(warnings(candidate, self.config), []),
                previous_findings=Source(previous or [], []))
            ids = sorted({a for u in units for a in u["assets"]})
            sources["assets"] = file_source(self.store, "assets.csv", {"kind": "assets", "ids": ids})
            reviewed = self.runner.review("scene_fidelity", "scene_review", "B5", f"{self.episode}:{scene.id}", sources, challenge=challenges)
            self.store.write(f"findings/scene_fidelity/{self.episode}_{scene.id}_{digest(reviewed)[:12]}.json", reviewed, json_data=True)
            if reviewed["script_notes"]:
                from storyforge.runner.service import record_script_note
                note_ids = []
                for note in reviewed["script_notes"]:
                    origin = {"reviewer":"scene_fidelity","stage":"B5","artifact":f"{self.episode}:{scene.id}",
                        "script_fingerprint":digest(self._script_source.data),"evidence":note["evidence"],"location":note["location"]}
                    record = record_script_note(self.store,self.episode,note["note"],by="model",origin=origin,
                        dedupe=digest({**origin,"note":note["note"]}),bindings=reviewed["source_bindings"])
                    note_ids.append(record["id"])
                raise ScriptRevisionNeeded(note_ids)
            result.extend(reviewed["findings"])
            if units:
                previous_carry = Source(units[-1]["carry_out"], [])
        return result

    def storyboard(self, stage: str, *, force=False, repairs=None):
        path = f"storyboard/{self.episode}.json"
        resolution = self.runner.cards.resolution(stage,self.episode)
        retry_path = f".state/script_retries/{resolution['id']}.json" if resolution else None
        if resolution and resolution["answer"]["choice"]=="retry" and resolution["details"].get("script_note_ids") and not self.store.json(retry_path):
            from storyforge.runner.service import record_script_note
            text = resolution["answer"].get("note","").strip()
            if text:
                record_script_note(self.store,self.episode,text,origin={"card_id":resolution["id"]},dedupe="script-retry:"+resolution["id"])
            else:
                # A deliberate retry gets a new note identity, so unchanged
                # automatic attempts do not masquerade as a new user decision.
                for note in self.store.json("notes/script_notes.json",[]):
                    if note["id"] in resolution["details"]["script_note_ids"] and note.get("origin",{}).get("script_fingerprint")==digest(self._script_source.data):
                        record_script_note(self.store,self.episode,note["note"],by="model",origin={**note["origin"],"retry_card":resolution["id"]},dedupe="script-retry:"+resolution["id"]+":"+note["id"])
            from storyforge.runner.writer import Screenwriter
            writer = Screenwriter(self.runner)
            writer.write_episode("A7" if self.number==1 else "A9",self.number)
            writer.lock_episode("A10",self.number)
            self._script_source = file_source(self.store,f"episodes/{self.episode}.md")
            self.script = parse_script(self._script_source.data)
            self.bible = parse_bible(self.store.text("bible.md"))
            self.asset_extract("B2",force=True)
            self.asset_briefs("B3")
            self.look_approval("B4")
            self.store.write(retry_path,{"applied":True},json_data=True)
        request = self.revision("B5", self.episode) if repairs is None else None
        if request:
            repairs, force = self.revision_repairs(request, self.store.json(path)), True
        if not force and self.store.current(path):
            return
        script_notes = []
        for _ in range(self.config["fix_rounds"] + 1):
            sources = self.sources()
            episode_placeholders = set(self.store.json(self.marker("B2"), {}).get("assets", []))
            selected = [a for a in self.store.assets() if a["placeholder"] in episode_placeholders]
            parents = {a["parent"] for a in selected if a["parent"]}
            selected += [a for a in self.store.assets() if a["id"] in parents and a not in selected]
            ids = [a["id"] for a in selected]
            sources["assets"] = file_source(self.store, "assets.csv", {"kind": "assets", "ids": ids})
            sources["assets"].data = selected
            sources["previous_carry_out"] = self.previous_carry()
            def validate(value):
                normalize_scenes(value, [s.id for s in self.script.scenes])
                normalize_cast_names(value)
                if value["asset_requests"]:
                    raise MissingAssets([normalize_request(r) for r in value["asset_requests"]])
                return check_storyboard(value, self.script, self.bible, selected, self.runner.card, speech_rate=self.config["speech_rate_chars_per_sec"])
            def output(value, store):
                normalized = deepcopy(value)
                normalized.pop("responses", None)
                normalized.pop("asset_requests", None)
                for unit in normalized["units"]:
                    unit["absent"] = {a["name"]: a["reason"] for a in unit["absent"]}
                return {path: serialize(normalized) + "\n"}
            try:
                self.runner.work(stage, self.episode, sources, validate, output,
                    reviewer=self.scene_reviews if "scene_fidelity" in self.runner.stages[stage]["reviewers"] else None, repairs=repairs)
                if request:
                    self.finish_revision("B5", self.episode)
                return
            except MissingAssets as exc:
                self.store.write(f".state/asset_requests/{self.episode}.json", exc.ids, json_data=True)
                self.asset_extract("B2", force=True)
                self.asset_briefs("B3")
                self.look_approval("B4")
            except ScriptRevisionNeeded as exc:
                script_notes.extend(exc.note_ids)
                from storyforge.runner.writer import Screenwriter
                writer = Screenwriter(self.runner)
                writer.write_episode("A7" if self.number==1 else "A9",self.number)
                writer.lock_episode("A10",self.number)
                self._script_source = file_source(self.store,f"episodes/{self.episode}.md")
                self.script = parse_script(self._script_source.data)
                self.bible = parse_bible(self.store.text("bible.md"))
                self.asset_extract("B2",force=True)
                self.asset_briefs("B3")
                self.look_approval("B4")
        requests = self.store.json(f".state/asset_requests/{self.episode}.json", [])
        resolution = self.runner.cards.resolution(stage, self.episode)
        card = self.runner.cards.create(kind="stuck", stage=stage, target=self.episode,
            question="源剧本问题修复后仍需处理" if script_notes else "分镜反复请求新资产，尚未形成有效分镜",
            options=[{"key":"retry","label":"备注后重试"},{"key":"stop","label":"停止此项"}], recommended="retry",
            reason="请在备注中说明剧本应如何调整，重试后交回编剧" if script_notes else "新增资产后分镜仍未通过", dedupe=f"asset-loop:{self.episode}:{digest([requests,script_notes, resolution['id'] if resolution else None])}",
            details={"asset_requests": requests,"script_note_ids":list(dict.fromkeys(script_notes))})
        raise StageBlocked(("Script revision" if script_notes else "Asset request")+" decision required: " + card["id"])

    def reference_mapping(self, stage: str):
        resolution = self.runner.cards.resolution(stage, self.episode)
        if resolution and resolution["answer"]["choice"] == "stop":
            raise StageBlocked("User stopped reference assignment for this episode")
        retry_path = f".state/ref_retries/{resolution['id']}.json" if resolution else None
        if resolution and resolution["answer"]["choice"] == "retry" and not self.store.json(retry_path):
            self.storyboard("B5", force=True, repairs={"user_note": resolution["answer"], "retry_id": resolution["id"]})
            self.store.write(retry_path, {"applied": True}, json_data=True)
        for attempt in range(self.config["fix_rounds"] + 1):
            board_path = f"storyboard/{self.episode}.json"
            board = self.store.json(board_path)
            mappings, errors, bindings, parts = {}, [], [self.store.binding(board_path)], {}
            for unit in board["units"]:
                try:
                    references = mapping(self.store, unit, self.config)
                except MissingAssets as exc:
                    self.store.write(f".state/asset_requests/{self.episode}.json", exc.ids, json_data=True)
                    self.asset_extract("B2", force=True)
                    self.asset_briefs("B3")
                    self.look_approval("B4")
                    references = mapping(self.store, unit, self.config)
                errors += [f"{unit['id']}: {e}" for e in check_references(references, self.runner.card)]
                mappings[unit["id"]] = references
                unit_bindings = [self.store.binding(board_path, {"kind": "unit", "id": unit["id"]}),
                                 self.store.binding(f".state/ref_edits/{unit['id']}.json")]
                asset_ids = [r["asset_id"] for r in references if not r["asset_id"].startswith("external:")]
                unit_bindings.append(self.store.binding("assets.csv", {"kind": "assets", "ids": asset_ids}))
                unit_bindings += external_source(self.store, ROOT / "model_cards" / f"{self.config['output']['model_card']}.yaml").bindings
                unit_bindings.append(self.store.binding("project.yaml", {"kind": "yaml_keys", "keys": ["output"]}))
                parts[unit["id"]] = unit_bindings
                bindings.extend(unit_bindings)
            if errors:
                if attempt == self.config["fix_rounds"]:
                    card = self.runner.cards.create(kind="stuck", stage=stage, target=self.episode,
                        question="参考超限，分镜修复后仍未解决", options=[{"key": "retry", "label": "备注调整方案"}, {"key": "stop", "label": "停止此项"}],
                        recommended="retry", reason="参考素材必须符合账号的模型限制", dedupe="refs:" + digest([errors, resolution["id"] if resolution else None]), details={"errors": errors})
                    raise StageBlocked("Reference card required: " + card["id"])
                self.storyboard("B5", force=True, repairs={"reference_limit_errors": errors})
                continue
            ids = sorted({r["asset_id"] for refs in mappings.values() for r in refs if not r["asset_id"].startswith("external:")})
            bindings.append(self.store.binding("assets.csv", {"kind": "assets", "ids": ids}))
            bindings += external_source(self.store, ROOT / "model_cards" / f"{self.config['output']['model_card']}.yaml").bindings
            bindings.append(self.store.binding("project.yaml", {"kind": "yaml_keys", "keys": ["output"]}))
            for uid, unit_bindings in parts.items():
                part_path = f"refs/units/{uid}.json"
                if self.store.current(part_path):
                    continue
                part_job = self.store.start_job(stage, uid, unit_bindings)
                if not self.store.accept(part_job, stage, uid, unit_bindings, {part_path: serialize(mappings[uid]) + "\n"}):
                    raise StageBlocked("Reference inputs changed")
            job = self.store.start_job(stage, self.episode, bindings)
            if not self.store.accept(job, stage, self.episode, bindings, {f"refs/{self.episode}.json": serialize(mappings) + "\n"}):
                raise StageBlocked("Reference inputs changed")
            return

    def style_lock(self) -> str:
        block = style_parts(self.store.text("style.md"))["style_lock"]
        if not block:
            raise SflError("style.md has no style-lock block")
        return block

    def unit_prompts(self, stage: str, *, only: list[str] | None = None, repairs=None, scene_id=None):
        if scene_id is None and len(self.script.scenes)>1:
            from storyforge.runner.parallel import run
            return run(self.script.scenes,lambda s:self.unit_prompts(stage,only=only,repairs=repairs,scene_id=s.id),self.config["concurrency"]["llm"])
        board_path = f"storyboard/{self.episode}.json"
        board = self.store.json(board_path)
        mappings_path = f"refs/{self.episode}.json"
        mappings = self.store.json(mappings_path)
        last = None
        waiting, paused = [], []
        for scene in self.script.scenes:
            if scene_id is not None and scene.id!=scene_id:
                continue
            if repairs is None:
                for unit in board["units"]:
                    if unit["scene"] == scene.id and (only is None or unit["id"] in only) and self.revision("B7", unit["id"]):
                        fragment = self.store.json(f".state/fragments/{unit['id']}.json")
                        previous = {k: v for k, v in fragment.items() if k != "responses"} if fragment else None
                        adopted = self.store.json(f".state/adopted/{unit['id']}.json") or {}
                        current_text = self.store.text(f"prompts/{self.episode}/{unit['id'].split('_')[1]}.md", "")
                        if adopted.get("fingerprint") == digest(current_text):
                            previous = {"prompt_text": current_text}  # the user's own wording, not the model's fragments
                        self.unit_prompts(stage, only=[unit["id"]], repairs=self.revision_repairs(self.revision("B7", unit["id"]), previous), scene_id=scene.id)
                        self.finish_revision("B7", unit["id"])
            units = [u for u in board["units"] if u["scene"] == scene.id and (only is None or u["id"] in only)
                     and (repairs or not self.store.current(f"prompts/{self.episode}/{u['id'].split('_')[1]}.md"))]
            if not units:
                continue
            deferred = [u for u in units if not repairs and self.store.path(f"delivery/{self.episode}/{u['id'].split('_')[1]}.md").exists() and not self.runner.may_update(u["id"])]
            waiting.extend(u["id"] for u in deferred)
            units = [u for u in units if u not in deferred]
            if not units:
                continue
            if not repairs and all(self.store.current(f"prompts/{self.episode}/{u['id'].split('_')[1]}.md") for u in units):
                continue
            sources = self.sources()
            sources["units"] = Source(units, [self.store.binding(board_path, {"kind": "unit", "id": u["id"]}) for u in units])
            sources["mappings"] = Source({u["id"]: mappings[u["id"]] for u in units}, [self.store.binding(f"refs/units/{u['id']}.json") for u in units])
            sources["warnings"] = Source(warnings(board, self.config), [])
            sources["style"].bindings += external_source(self.store, ROOT / "skills/director/taste/prompt_parts.yaml", style_reference=True).bindings
            requested_units = {u["id"]: u for u in units}
            def render_unit(fragment):
                unit = requested_units[fragment["unit_id"]]
                return render(fragment, mappings[unit["id"]], self.style_lock(), self.config, unit,
                              label=unit_label(unit, board["units"]))
            def validate(value):
                expected = {u["id"] for u in units}
                emitted = [p["unit_id"] for p in value["prompts"]]
                errors = [] if set(emitted) == expected and len(emitted) == len(expected) else ["Output must contain each requested unit exactly once"]
                for fragment in value["prompts"]:
                    if fragment["unit_id"] in expected:
                        text = render_unit(fragment)
                        errors += check_prompt(text, mappings[fragment["unit_id"]])
                return errors
            def output(value, store):
                outputs = {}
                for fragment in value["prompts"]:
                    uid = fragment["unit_id"]
                    outputs[f"prompts/{self.episode}/{uid.split('_')[1]}.md"] = render_unit(fragment)
                    outputs[f".state/fragments/{uid}.json"] = serialize({**fragment, "responses": value["responses"]}) + "\n"
                return outputs
            def scope(path, bindings):
                uid = next(u["id"] for u in units if path.endswith(u["id"].split("_")[1] + ".md") or path.endswith(u["id"] + ".json"))
                return [b for b in bindings if not (b["path"] == board_path and b["selector"].get("id") != uid)
                        and not (b["path"].startswith("refs/units/") and b["path"] != f"refs/units/{uid}.json")]
            try:
                last = self.runner.work(stage, f"{self.episode}:{scene.id}", sources, validate, output, repairs=repairs, output_scope=scope)
            except StageBlocked as exc:
                waiting.append(str(exc))
            except CallPaused as exc:
                paused.append(str(exc))
        if paused:
            raise CallPaused("; ".join(paused))
        if waiting:
            raise StageBlocked("Units need an Inbox decision: " + ", ".join(waiting))
        return last

    def reconstruction(self, stage: str, *, only=None):
        from storyforge.packets import build
        board = self.store.json(f"storyboard/{self.episode}.json")
        if only is None:
            from storyforge.runner.parallel import run
            return run(board["units"],lambda u:self.reconstruction(stage,only=[u["id"]]),self.config["concurrency"]["llm"])
        waiting, paused = [], []
        for unit in board["units"]:
            uid = unit["id"]
            if uid not in only:
                continue
            if not self.store.current(f"prompts/{self.episode}/{uid.split('_')[1]}.md"):
                waiting.append(uid)
                continue
            if self.runner.cards.blocking(stage, uid):
                waiting.append(uid)
                continue
            resolution = self.runner.cards.resolution(stage, uid)
            if resolution and resolution["answer"]["choice"] == "stop":
                waiting.append(uid)
                continue
            review_path = f".state/reviews/{uid}.json"
            if self.store.current(review_path):
                continue
            if resolution and resolution["answer"]["choice"] == "retry":
                self.unit_prompts("B7", only=[uid], repairs={"user_note": resolution["answer"]})
            earlier = []  # actionable findings of the previous round, re-checked instead of starting a new open-ended review
            for attempt in range(self.config["fix_rounds"] + 1):
                mappings_path = f"refs/{self.episode}.json"
                refs = self.store.json(mappings_path)[uid]
                prompt_path = f"prompts/{self.episode}/{uid.split('_')[1]}.md"
                text = self.store.text(prompt_path)
                prompt_warnings = [w for w in warnings(board, self.config, {uid: text}) if w["target"] == uid]
                sources = self.sources()
                sources.update(prompt=file_source(self.store, prompt_path),
                    reference_descriptions=Source([{k: r[k] for k in ("placeholder", "type", "name", "description", "position")} for r in refs],
                        [self.store.binding(f"refs/units/{uid}.json")]),
                    warnings=Source(prompt_warnings, []))
                unit_binding = self.store.binding(f"storyboard/{self.episode}.json", {"kind": "unit", "id": uid})
                sources.update(reconstruction=Source("", []), unit=Source(unit, [unit_binding]), previous_findings=Source(earlier, []))
                # The blind reader may only see warnings that can be read off the prompt text itself;
                # shot-level ones (speech fit, overlays, runtime) come from the storyboard.
                blind_kinds = rules()["blind_warning_kinds"]
                blind_sources = {**sources, "warnings": Source([w for w in prompt_warnings if w["kind"] in blind_kinds], [])}
                bindings = build(self.store, "reconstruction_blind", blind_sources).bindings
                bindings += build(self.store, "reconstruction_compare", sources).bindings
                bindings += external_source(self.store, ROOT / "platform/schemas.yaml", style_reference=True).bindings
                bindings += external_source(self.store, ROOT / "skills/shared/check_rules.yaml", style_reference=True).bindings
                job = self.store.start_job(stage, uid, bindings)
                try:
                    errors = check_references(refs, self.runner.card) + check_prompt(text, refs)
                    if errors:
                        findings = []
                    else:
                        responses = self.store.json(f".state/fragments/{uid}.json", {}).get("responses", [])
                        challenges = [r for r in responses if r["disposition"] == "reject"]
                        blind = self.runner.review("reconstruction_blind", "reconstruction", stage, uid, blind_sources)
                        sources["reconstruction"] = Source(blind["description"], bindings)
                        compared = self.runner.review("reconstruction_compare", "findings", stage, uid, sources, challenge=challenges)
                        from storyforge.runner import limit_new_findings
                        findings = limit_new_findings(blind["findings"] + compared["findings"], earlier)
                        self.store.write(f"findings/reconstruction/{uid}_{digest(findings)[:12]}.json", {"blind": blind, "compare": compared}, json_data=True)
                    actionable = [f for f in findings if f["severity"] in ("blocker", "major")]
                    # The user's own wording is final: reviewer findings stay in the record as notes, never trigger a rewrite.
                    adopted = (self.store.json(f".state/adopted/{uid}.json") or {}).get("fingerprint") == digest(text)
                    rejected_ids = {r["finding_id"] for r in self.store.json(f".state/fragments/{uid}.json", {}).get("responses", []) if r["disposition"] == "reject"}
                    held = bool({f["id"] for f in actionable if f["severity"] == "blocker"} & rejected_ids)
                    # With open_major=accept, majors that survive the fix rounds travel with the delivery as review notes instead of a card.
                    keep_open = (self.config.get("fix_policy", {}).get("open_major", "card") == "accept" and attempt == self.config["fix_rounds"]
                                 and not held and not any(f["severity"] == "blocker" for f in actionable))
                    if not errors and (adopted or not actionable or keep_open):
                        result = {"passed": True, "prompt_fingerprint": digest(text), "findings": findings, "adopted": adopted,
                                  "open_findings": actionable if (adopted or keep_open) else [],
                                  "warnings": prompt_warnings, "description": blind["description"]}
                        if not self.store.accept(job, stage, uid, bindings, {review_path: serialize(result) + "\n"}, metadata={"reviewed": True}):
                            raise StageBlocked("Inputs changed during reconstruction")
                        break
                    self.store.end_job(job, "superseded", "Prompt needs repair")
                    if attempt == self.config["fix_rounds"] or held:
                        card = self.runner.cards.create(kind="stuck", stage=stage, target=uid,
                            question=f"{uid} 提示词审查仍有问题", options=[{"key": "retry", "label": "备注后重试"}, {"key": "stop", "label": "停止此项"}],
                            recommended="retry", reason="文字重构仍不能匹配分镜", dedupe=f"review:{uid}:{digest([errors,findings,digest(text),resolution['id'] if resolution else None])}", details={"errors": errors, "findings": actionable})
                        self.store.end_job(job, "blocked", "Awaiting " + card["id"])
                        waiting.append(uid)
                        break
                    earlier = actionable
                    self.unit_prompts("B7", only=[uid], repairs={"hard_errors": errors, "findings": actionable})
                except CallPaused as exc:
                    self.store.end_job(job, "paused", str(exc))
                    paused.append(uid + ": " + str(exc))
                    break
                except StageBlocked:
                    self.store.end_job(job, "blocked", "Awaiting decision")
                    reviewer_stopped = any((self.runner.cards.resolution(stage, f"{uid}:review:{role}", kind="technical") or {}).get("answer", {}).get("choice") == "stop"
                        for role in ("reconstruction_blind", "reconstruction_compare"))
                    if self.runner.cards.blocking(stage, uid) or reviewer_stopped:
                        waiting.append(uid)
                        break
                    raise
        if paused:
            raise CallPaused("; ".join(paused))
        if waiting:
            raise StageBlocked("Units awaiting decisions: " + ", ".join(waiting))

    def delivery(self, stage: str):
        for unit in self.store.json(f"storyboard/{self.episode}.json")["units"]:
            if not self.store.current(f".state/reviews/{unit['id']}.json"):
                raise StageBlocked("Remaining units must pass reconstruction before delivery")
        export(self.store, self.episode, self.runner.card, config=self.config)
