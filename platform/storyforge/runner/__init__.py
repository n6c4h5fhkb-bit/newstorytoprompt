from __future__ import annotations
from pathlib import Path
import re

import yaml

from storyforge import ROOT
from storyforge.config import SflError, configuration, load_yaml, model_card
from storyforge.checks import script_names
from storyforge.checks.parsers import parse_bible, parse_script
from storyforge.cards import Cards
from storyforge.llm import Client, CallPaused, OutputError
from storyforge.packets import Source, build, file_source, json_source, external_source, methodology_bindings
from storyforge.store import Store, digest, serialize


class StageBlocked(SflError):
    pass


class ScriptRevisionNeeded(StageBlocked):
    def __init__(self, note_ids):
        self.note_ids = note_ids
        super().__init__("Source script notes need independent A review: "+", ".join(note_ids))


class Runner:
    def __init__(self, store: Store, *, config: dict | None = None, client: Client | None = None):
        self.store = store
        self.config = config or configuration(store.root)
        self.card = model_card(self.config)
        self.cards = Cards(store, self.config)
        self.client = client or Client(store, self.config)
        self.stages = load_yaml(ROOT / "platform" / "stages.yaml")
        self.allowed_stale = []

    def check_control(self):
        if self.store.json(".runtime/control.json", {}).get("paused"):
            raise CallPaused("Project paused by user; resume to continue")

    def remember_scope(self, until, episodes):
        # Called under the project run lease, so a rejected concurrent start
        # cannot change what a later UI retry resumes.
        self.store.write(".runtime/run_scope.json", {"until": until, "episodes": episodes}, json_data=True)

    def may_update(self, target: str) -> bool:
        return any(target == allowed or target.startswith(allowed + "_") or target.startswith(allowed + ":") for allowed in self.allowed_stale)

    def budget(self, stage: str, target: str):
        match = re.match(r"ep\d+", target)
        if not match:
            return
        episode = match[0]
        rows = [r for r in self.store.logs("calls") if re.match(r"ep\d+", r.get("target", ""))
                and re.match(r"ep\d+", r["target"])[0] == episode]
        used = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in rows)
        limit = self.config["token_budget"].get("per_episode")
        for card in self.cards.list(include_resolved=True):
            if card["kind"] == "budget" and card["target"] == episode and card["answer"]:
                if card["answer"]["choice"] == "stop":
                    raise StageBlocked(f"{episode}: user stopped budget spending")
                limit = int(card["answer"]["note"])
        if limit is not None and used >= limit:
            card = self.cards.create(kind="budget", stage=stage, target=episode,
                question=f"{episode} 已使用 {used} tokens，达到预算 {limit}",
                options=[{"key": "raise", "label": "提高预算（备注填写新 tokens 上限）"}, {"key": "stop", "label": "停止该集"}],
                recommended="stop", reason="后续模型调用需要新的预算决定", dedupe=f"budget:{episode}:{limit}", details={"used": used, "limit": limit})
            raise StageBlocked(f"Budget card waiting: {card['id']}")

    def call(self, role: str, tier: str, schema: str, stage: str, target: str, sources: dict, repairs: dict | None = None):
        self.check_control()
        self.budget(stage, target)
        packet = build(self.store, role, sources, repairs=repairs)
        result = self.client.call(packet, tier, schema, stage, target)
        self.budget(stage,target)
        return result, packet

    def review(self, role: str, schema: str, stage: str, target: str, sources: dict, *, challenge: list[dict] | None = None) -> dict:
        challenge = [r for r in (challenge or []) if r.get("reviewer")==role and r.get("artifact")==target]
        repairs = {"challenge": challenge} if challenge else {}
        review_target = f"{target}:review:{role}"
        resolution = self.cards.resolution(stage, review_target, kind="technical")
        if self.cards.blocking(stage, review_target):
            raise StageBlocked("Reviewer decision required")
        if resolution:
            if resolution["answer"]["choice"] == "stop":
                raise StageBlocked("User stopped this reviewer target")
            # A retry ID changes the call fingerprint without exposing user intent
            # to a blind reviewer.
            repairs["retry_id"] = resolution["id"]
        result = None
        for attempt in range(self.config["fix_rounds"] + 1):
            try:
                result, packet = self.call(role, "reviewer", schema, stage, target, sources, repairs)
                evidence_pool = leaf_strings(packet.data)
                errors = [f"Finding evidence is not verbatim input: {f['evidence']}" for f in result["findings"]
                          if not any(f["evidence"] in text for text in evidence_pool)]
                if role=="scene_fidelity":
                    errors += ["Script note evidence must quote the source scene" for note in result.get("script_notes",[])
                               if note["evidence"] not in packet.data["script_scene"]]
                if role == "payoff_judge":
                    expected = {v["key"] for v in packet.data["versions"]}
                    keys = [s["key"] for s in result["scores"]]
                    if result["choice"] not in expected or set(keys) != expected or len(keys) != len(expected):
                        errors.append("Payoff choice and scores must cover the supplied versions")
                    if any(e["kind"]=="character" and not e["voice"].strip() for e in result["entities"]):
                        errors.append("New character needs a voice description")
                if not errors:
                    for finding in result["findings"]:
                        finding.update(reviewer=role, artifact=target,
                                       id="f_" + digest({**finding, "reviewer": role, "target": target})[:12])
                    if role=="scene_fidelity" and result.get("script_notes"):
                        result["source_bindings"] = packet.bindings
                    return result
                repairs["hard_errors"] = errors
            except OutputError as exc:
                repairs["hard_errors"] = [str(exc)]
        blocks = [target.split(":")[0]] if stage == "B5" else [target]
        card = self.cards.create(kind="technical", stage=stage, target=review_target,
            question=f"{role} 审查结果格式或证据无效",
            options=[{"key": "retry", "label": "重新审查"}, {"key": "stop", "label": "停止此项"}],
            recommended="retry", reason="审查结果修复后仍不符合输出协议",
            dedupe=f"review-format:{role}:{target}:{digest([repairs, {k:s.data for k,s in sources.items()}])}",
            blocks=blocks, details={"errors": repairs.get("hard_errors", [])})
        raise StageBlocked("Reviewer decision required: " + card["id"])

    def work(self, stage: str, target: str, sources: dict, validator, outputs, *, reviewer=None, repairs: dict | None = None, output_scope=None, extra_bindings=None):
        blocking = self.cards.blocking(stage, target)
        if blocking:
            raise StageBlocked("Decision required: " + ", ".join(c["id"] for c in blocking))
        resolution = self.cards.resolution(stage, target)
        if resolution and resolution["answer"]["choice"] == "stop":
            raise StageBlocked("User stopped this stage target")
        repairs = dict(repairs or {})
        retry_bindings = []
        if resolution and resolution["answer"]["choice"] == "retry":
            candidate_path = resolution["details"].get("candidate_file", "")
            if candidate_path.startswith(".state/stuck/"):
                saved_source = json_source(self.store, candidate_path)
                saved = saved_source.data
                if isinstance(saved, dict) and saved.get("candidate") is not None and saved.get("bindings") \
                        and self.store.unchanged(saved["bindings"], include_style=False):
                    repairs = {"previous_output": saved["candidate"], "hard_errors": saved.get("errors", []),
                        "findings": [f for f in saved.get("findings", []) if f["severity"] in ("blocker", "major")], **repairs}
                    # Bind the saved draft and its still-current factual inputs;
                    # revised methods apply to the new attempt as usual.
                    retry_bindings = saved_source.bindings + [b for b in saved["bindings"] if not b.get("style_reference")]
            repairs["user_note"] = resolution["answer"]
        spec = self.stages[stage]
        packet = build(self.store, spec["role"], sources, repairs=repairs)
        extra = external_source(self.store, ROOT / "platform/stages.yaml", style_reference=True).bindings
        extra += external_source(self.store, ROOT / "platform/schemas.yaml", style_reference=True).bindings
        extra += external_source(self.store, ROOT / "skills/shared/check_rules.yaml", style_reference=True).bindings
        extra += methodology_bindings(self.store, spec.get("reviewers", []))
        extra += extra_bindings or []
        extra += retry_bindings
        if resolution:
            extra.append(self.store.binding("logs/decisions.jsonl", {"kind": "decision", "card_id": resolution["id"]}))
        bindings = packet.bindings + extra
        job = self.store.start_job(stage, target, bindings)
        last, errors, findings, held = None, [], list(repairs.get("findings", [])), set()
        try:
            for attempt in range(self.config["fix_rounds"] + 1):
                self.check_control()
                self.budget(stage, target)
                packet = build(self.store, spec["role"], sources, repairs=repairs)
                try:
                    last = self.client.call(packet, spec["tier"], spec["schema"], stage, target)
                    self.budget(stage,target)
                    errors = validator(last)
                    expected = {f["id"] for f in repairs.get("findings",[]) if f["severity"] in ("blocker","major")}
                    responded = [r["finding_id"] for r in last.get("responses",[])]
                    if set(responded)!=expected or len(responded)!=len(expected):
                        errors.append("responses must cover exactly the supplied blocker/major finding IDs once. "
                            f"Expected IDs: {sorted(expected)}; received IDs: {responded}. "
                            "hard_errors are not findings; do not add their target labels as finding IDs.")
                    if not errors:
                        origins = {f["id"]:f for f in repairs.get("findings",[])}
                        last["responses"] = [{**r,"reviewer":origins[r["finding_id"]]["reviewer"],"artifact":origins[r["finding_id"]]["artifact"]} for r in last.get("responses",[])]
                except OutputError as exc:
                    errors, last = [str(exc)], exc.previous
                if errors:
                    repairs = {**repairs, "hard_errors": errors, "previous_output": last}
                    continue
                responses = [r for r in last.get("responses", []) if r["disposition"] == "reject"]
                findings = reviewer(last, responses) if reviewer else []
                held = {f["id"] for f in findings if f["severity"] == "blocker"} & {r["finding_id"] for r in responses}
                actionable = [f for f in findings if f["severity"] in ("blocker", "major")]
                if not actionable:
                    metadata = {"findings": findings, "reviewed": bool(reviewer)}
                    if not self.store.accept(job, stage, target, bindings, lambda s: outputs(last, s), metadata=metadata, output_scope=output_scope):
                        raise StageBlocked("Inputs changed; result retained as stale candidate")
                    return last
                repairs = {**repairs, "findings": actionable, "previous_output": last}
                if held:
                    break
            if last is not None and not errors and not held and self.config.get("fix_policy", {}).get("open_major", "card") == "accept" \
                    and not any(f["severity"] == "blocker" for f in findings):
                # Reviewers keep finding something new after every rewrite; majors that survive the fix rounds are recorded, not escalated.
                open_findings = [f for f in findings if f["severity"] == "major"]
                metadata = {"findings": findings, "reviewed": bool(reviewer), "open_findings": open_findings}
                if not self.store.accept(job, stage, target, bindings, lambda s: outputs(last, s), metadata=metadata, output_scope=output_scope):
                    raise StageBlocked("Inputs changed; result retained as stale candidate")
                self.store.append("decisions", {"stage": stage, "target": target, "event": "open_findings_accepted", "choice": "accept",
                                                "by": "code", "findings": [f["id"] for f in open_findings]})
                return last
            state = {"candidate": last, "errors": errors, "findings": findings, "bindings": bindings}
            self.store.write(f".state/stuck/{job}.json", state, json_data=True)
            shown = [f for f in findings if f["severity"] == "blocker"] + [f for f in findings if f["severity"] != "blocker"][:5]
            card = self.cards.create(kind="stuck", stage=stage, target=target,
                question=f"{spec.get('label', stage)}（{target}）自动修复后仍有问题",
                options=[{"key": "retry", "label": "带备注重试"}, {"key": "stop", "label": "停止此项"}], recommended="retry",
                reason="硬错误或独立审查问题仍存在", dedupe=f"stuck:{stage}:{target}:{digest(state)}",
                details={"errors": errors, "findings": shown, "candidate_file": f".state/stuck/{job}.json"})
            self.store.end_job(job, "blocked", "Waiting for " + card["id"])
            raise StageBlocked("Decision required: " + card["id"])
        except StageBlocked as exc:
            if isinstance(exc,ScriptRevisionNeeded):
                self.store.write(f".state/stuck/{job}.json",{"candidate":last,"errors":errors,"findings":findings,"bindings":bindings,"script_note_ids":exc.note_ids},json_data=True)
            with self.store.db() as conn:
                conn.execute("UPDATE jobs SET status='blocked', error=? WHERE id=? AND status='running'", ("Waiting for dependency or budget", job))
            raise
        except CallPaused as exc:
            self.store.end_job(job, "paused", str(exc))
            raise
        except Exception as exc:
            self.store.end_job(job, "paused", str(exc))
            raise

    def run(self, *, until: str = "B9", episodes: list[int] | None = None, allow_stale: list[str] | None = None) -> dict:
        self.allowed_stale = allow_stale or []
        if self.store.path("source/novel.txt").exists():
            from storyforge.runner.writer import Screenwriter
            return Screenwriter(self).run(until=until, episodes=episodes)
        from storyforge.runner.director import Director
        if until not in self.stages:
            raise SflError("Unknown --until stage")
        available = sorted(int(p.stem[2:]) for p in (self.store.root / "episodes").glob("ep*.md") if re.fullmatch(r"ep\d+", p.stem))
        selected = available if episodes is None else episodes
        if not selected:
            raise SflError("Import a locked episode first")
        if set(selected) - set(available):
            raise SflError("One or more selected episodes have not been imported")
        report = {"demo": bool(self.config.get("demo")), "completed": [], "waiting": [], "paused": []}
        self.allowed_stale = allow_stale or []
        with self.store.run_lease():
            self.remember_scope(until, episodes)
            self.store.stale_artifacts()
            for number in sorted(selected):
                episode = f"ep{number:02d}"
                path = f"episodes/{episode}.md"
                pending = any(n["target"]==episode and n["status"]=="pending" for n in self.store.json("notes/script_notes.json",[]))
                artifact = self.store.json(".state/artifacts.json",{}).get(path)
                lock = self.store.json(".state/locks.json",{}).get(episode,{})
                needs_lock = artifact and artifact["stage"] in ("A7","A9") and (lock.get("script_fingerprint")!=digest(self.store.text(path)) or not self.store.current(f".state/script_reviews/{episode}.json"))
                # An imported screenplay is already a human adoption. Its notes
                # enter A7/A9 directly; there is no invented novel or A1-A6 plan.
                if pending or needs_lock or artifact and not self.store.current(path):
                    from storyforge.runner.writer import Screenwriter
                    writer = Screenwriter(self)
                    stage = "A7" if number==1 else "A9"
                    try:
                        writer.write_episode(stage,number)
                        report["completed"].append(f"{episode}:{stage}")
                        if until==stage:
                            continue
                        stage = "A10"
                        writer.lock_episode(stage,number)
                        report["completed"].append(f"{episode}:{stage}")
                    except StageBlocked as exc:
                        report["waiting"].append({"episode":episode,"stage":stage,"reason":str(exc)})
                        continue
                    except CallPaused as exc:
                        report["paused"].append({"episode":episode,"stage":stage,"reason":str(exc)})
                        continue
                if until.startswith("A"):
                    continue
                try:
                    director = Director(self, number)
                except StageBlocked as exc:
                    report["waiting"].append({"episode":episode,"stage":"A10","reason":str(exc)})
                    continue
                for stage, spec in self.stages.items():
                    if not stage.startswith("B"):
                        continue
                    blocking = self.cards.blocking(stage, episode)
                    if blocking:
                        report["waiting"].append({"episode": episode, "stage": stage, "cards": [c["id"] for c in blocking]})
                        break
                    try:
                        self.check_control()
                        if stage == "B5" and self.store.path(f"delivery/{episode}/manifest.json").exists() and not self.store.current(f"storyboard/{episode}.json") and not self.may_update(episode):
                            raise StageBlocked("Packaged episode inputs changed; choose rerun in Inbox")
                        getattr(director, spec["handler"])(stage)
                        report["completed"].append(f"{episode}:{stage}")
                    except StageBlocked as exc:
                        report["waiting"].append({"episode": episode, "stage": stage, "reason": str(exc)})
                        if stage == "B7" and stage != until:
                            continue
                        break
                    except CallPaused as exc:
                        report["paused"].append({"episode": episode, "stage": stage, "reason": str(exc)})
                        if stage == "B7" and stage != until and not self.store.json(".runtime/control.json", {}).get("paused"):
                            continue
                        break
                    if stage == until:
                        break
        return report


def leaf_strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in leaf_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in leaf_strings(v)]
    return []


def import_episode(store: Store, script_text: str, bible_text: str, ledger_text: str, *, assets_text: str | None = None):
    with store.locked():
        return _import_episode(store,script_text,bible_text,ledger_text,assets_text=assets_text)


def _import_episode(store: Store, script_text: str, bible_text: str, ledger_text: str, *, assets_text: str | None = None):
    script = parse_script(script_text)
    errors = script_names(script, parse_bible(bible_text))
    if errors:
        raise SflError("; ".join(errors))
    if not ledger_text.strip():
        raise SflError("Starting ledger cannot be empty")
    ledger_path = f"ledger/ep{script.episode - 1:02d}.md"
    existing = store.text(ledger_path, "")
    if existing and existing.strip() != ledger_text.strip():
        raise SflError(f"Existing {ledger_path} differs; resolve the canonical state before importing")
    if assets_text is not None:
        import csv, io
        from storyforge.checks import asset_rows
        rows = list(csv.DictReader(io.StringIO(assets_text)))
        errors = asset_rows(rows, parse_bible(bible_text))
        if errors:
            raise SflError("; ".join(errors))
    episode = f"ep{script.episode:02d}"
    store.write("bible.md", bible_text)
    store.write(f"episodes/{episode}.md", script_text)
    store.write(ledger_path, ledger_text)
    locks = store.json(".state/locks.json", {})
    locks[episode] = {"locked": True, "source": "user_import", "script_fingerprint": digest(script_text)}
    store.write(".state/locks.json", locks, json_data=True)
    if assets_text is not None:
        store.write("assets.csv", Store.assets_text(rows))
    bind_adopted_script(store,script.episode)
    store.append("decisions", {"stage": "import", "target": episode, "choice": "adopt", "by": "user", "demo":bool(configuration(store.root).get("demo")), "reason": "Imported locked screenplay and starting ledger"})
    store.stale_artifacts()
    store.snapshot(f"import {episode}")
    return episode


def bind_adopted_script(store: Store, number: int):
    """Bind a human-adopted script to its actual starting context and notes."""
    with store.locked():
        episode, path = f"ep{number:02d}",f"episodes/ep{number:02d}.md"
        text = store.text(path)
        script = parse_script(text)
        bible = parse_bible(store.text("bible.md"))
        names = {name for scene in script.scenes for name in scene.cast} | {line["who"] for scene in script.scenes for line in scene.lines if "who" in line}
        names.update(name for section in ("Props","UI") for name in bible.get(section,{}) if name in text)
        bindings = file_source(store,"bible.md",{"kind":"bible","names":sorted(names),"locations":sorted({scene.location for scene in script.scenes})}).bindings
        bindings += [store.binding(path),store.binding(f"ledger/ep{number-1:02d}.md"),store.binding("notes/script_notes.json",{"kind":"script_notes","episode":episode})]
        job = store.start_job("A10",episode,bindings)
        if not store.accept(job,"A10",episode,bindings,{path:text},metadata={"source":"user_import","reviewed":False}):
            raise StageBlocked("Adopted script context changed during import")
