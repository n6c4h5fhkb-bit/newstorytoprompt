"""Novel-to-locked-episode stages on the shared runner backbone."""
import re
from concurrent.futures import ThreadPoolExecutor

from storyforge import ROOT
from storyforge.checks import writer as check
from storyforge.checks.parsers import parse_bible, parse_script, bible_slice, complete_voice_cast
from storyforge.config import SflError
from storyforge.llm import CallPaused
from storyforge.packets import Source, file_source, json_source, external_source
from storyforge.runner import StageBlocked
from storyforge.runner.source import import_chunks
from storyforge.store import digest, serialize


def clean_cell(value):
    return value.replace("|", "／").replace("\n", " ")


def add_entities(text, entries):
    """Append declared entities, retaining all existing human-authored rows."""
    sections = {"character":("Characters",["name","role","look","voice","source name"]),
                "location":("Locations",["name","look"]), "prop":("Props",["name","look"]),
                "ui":("UI",["name","look"]), "voice":("Voices",["speaker","voice profile"])}
    for entry in entries:
        section, default_headers = sections[entry["kind"]]
        if entry["name"] in parse_bible(text).get(section, {}):
            continue
        if entry["kind"] == "character" and not entry["voice"].strip():
            raise SflError("New character needs a voice description")
        row = {"name":entry["name"],"speaker":entry["name"],"role":"supporting",
               "look":entry["description"],"voice":entry["voice"],"source name":entry["name"],
               "voice profile":entry["voice"] or entry["description"]}
        match = re.search(rf"^## {re.escape(section)}\s*\n(.*?)(?=^## |\Z)", text, re.M|re.S)
        if match:
            table = next((line for line in match[1].splitlines() if line.strip().startswith("|")), None)
            headers = [v.strip() for v in table.strip().strip("|").split("|")] if table else default_headers
            block = match[1].rstrip() + "\n"
            if table is None:
                block += "| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n"
            block += "| " + " | ".join(clean_cell(row.get(h, entry["description"])) for h in headers) + " |\n\n"
            text = text[:match.start(1)] + block + text[match.end(1):]
        else:
            headers = default_headers
            text = text.rstrip() + "\n\n## " + section + "\n| " + " | ".join(headers) + " |\n| " + " | ".join("---" for _ in headers) + " |\n"
            text += "| " + " | ".join(clean_cell(row[h]) for h in headers) + " |\n"
    return text


def breakdown_text(value):
    text = "# 剧情拆解\n\n## 主线\n\n" + value["main_plot"] + "\n\n## 支线\n\n"
    text += "\n".join("- " + t for t in value["subplots"]) + "\n\n## 人物弧线\n\n"
    text += "\n".join(f"- {a['name']}：{a['arc']}" for a in value["character_arcs"]) + "\n\n## 节拍时间线\n\n"
    return text + "\n".join(f"- {b['id']} · {'/'.join(b['source_refs'])} · {b['summary']}" for b in value["beats"]) + "\n"


def plan_text(value):
    text = "# 改编方案\n\n" + value["adaptation"] + "\n\n## 差异化\n\n" + value["differentiation"] + "\n\n## 分集节拍表\n\n"
    text += "| 集 | 标题 | 估算秒数 | 核心情绪 | 转向 | 结尾钩子 | 大转折 | 留存点 | 源文 | 节拍 |\n| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    for ep in value["episodes"]:
        fields = [f"ep{ep['number']:02d}",ep["title"],str(ep["estimated_seconds"]),"、".join(ep["emotions"]),ep["turn"],ep["end_hook"]["type"]+"："+ep["end_hook"]["description"],"是" if ep["major_turn"] else "", "是" if ep["retention_checkpoint"] else "","、".join(ep["source_refs"]),"、".join(ep["beats"])]
        text += "| " + " | ".join(clean_cell(f) for f in fields) + " |\n"
    text += "\n## 剪合决定\n\n" + "\n".join(f"- {d['beat_id']}：{d['action']} {d['merge_into']} · {d['reason']}" for d in value["decisions"]) + "\n"
    text += "\n## 开篇候选\n\n" + "\n".join(f"- {h['key']}：{h['label']} · {h['score']:g}" for h in value["hooks"]) + f"\n\n选择：{value['selected_hook']} · {value['reason']}\n"
    text += "\n## 关键与放大时刻\n\n" + "\n".join(f"- {m['id']} · ep{m['episode']:02d} · {'关键' if m['key'] else '普通'} · {m['summary']} · {'、'.join(m['source_refs'])}" for m in value["moments"]) + "\n"
    return text


class Screenwriter:
    def __init__(self, runner):
        self.runner, self.store, self.config = runner, runner.store, runner.config

    def target(self, number=None):
        data = {"episode":number,"episode_minutes":self.config["episode_minutes"],"speech_rate_chars_per_sec":self.config["speech_rate_chars_per_sec"]}
        bindings = [self.store.binding("project.yaml", {"kind":"yaml_keys","keys":["episode_minutes","speech_rate_chars_per_sec"]})]
        bindings += external_source(self.store, ROOT / "config.yaml", style_reference=True).bindings
        return Source(data, bindings)

    def passages(self, refs):
        refs = sorted(set(refs), key=lambda s:tuple(map(int,re.findall(r"\d+",s))))
        return file_source(self.store, "source/index.json", {"kind":"passages","refs":refs})

    def plan_slice(self, number):
        source = file_source(self.store, "plan.json", {"kind":"episode_plan","episode":number})
        plan = self.store.json("plan.json")
        source.data = {**source.data,"adaptation":plan["adaptation"],"differentiation":plan["differentiation"]}
        source.bindings += [self.store.binding("plan.json", {"kind":"key","key":k}) for k in ("adaptation","differentiation")]
        return source

    def import_novel(self, stage):
        return import_chunks(self.store, stage)

    def summaries(self, stage):
        index = self.store.json("source/index.json")
        from storyforge.runner.parallel import run
        def summarize(chunk):
            path = f"summaries/{chunk['id']}.json"
            if self.store.current(path):
                return
            sources = {"chunk":json_source(self.store,f"source/chunks/{chunk['id']}.json")}
            self.runner.work(stage, chunk["id"], sources, lambda v:check.summary(v,chunk,index),
                lambda v,s:{path:serialize(v)+"\n"})
        return run(index["chunks"],summarize,self.config["concurrency"]["llm"])

    def breakdown(self, stage):
        if self.store.current("breakdown.json"):
            return
        index = self.store.json("source/index.json")
        sources = [json_source(self.store,f"summaries/{c['id']}.json") for c in index["chunks"]]
        self.runner.work(stage,"project",{"summaries":Source([s.data for s in sources],[b for s in sources for b in s.bindings])},
            lambda v:check.breakdown(v,index), lambda v,s:{"breakdown.json":serialize(v)+"\n","breakdown.md":breakdown_text(v)})

    def adaptation(self, stage):
        if self.store.current("plan.json"):
            return
        timeline = json_source(self.store,"breakdown.json")
        refs = [r for b in timeline.data["beats"] if b["key"] for r in b["source_refs"]]
        sources = {"breakdown":timeline,"source_passages":self.passages(refs),"episode_target":self.target()}
        def reviews(value, challenges):
            sources["plan"] = Source(value,[])
            sources["warnings"] = Source([w for e in value["episodes"] for w in check.warnings(e["estimated_seconds"],self.config,f"ep{e['number']:02d}")],[])
            if "plan_reviewer" not in self.runner.stages[stage].get("reviewers",[]):
                return []
            review = self.runner.review("plan_reviewer","findings",stage,"project",sources,challenge=challenges)
            self.store.write(f"findings/plan_reviewer/{digest(review)[:20]}.json",review,json_data=True)
            return review["findings"]
        self.runner.work(stage,"project",sources,lambda v:check.plan(v,timeline.data,self.store.json("source/index.json")),
            lambda v,s:{"plan.json":serialize(v)+"\n","plan.md":plan_text(v),"bible.md":v["bible"],"ledger/ep00.md":v["ledger_in"]},reviewer=reviews)

    def direction(self, stage):
        path = ".state/direction.json"
        if self.store.current(path):
            return
        plan = self.store.json("plan.json")
        options = plan["hooks"]
        resolution = self.runner.cards.resolution(stage,"project",kind="creative")
        if resolution and self.runner.cards.blocking(stage,"project"):
            raise StageBlocked("Opening direction awaits a decision")
        decision = next(h for h in options if h["key"] == plan["selected_hook"])
        bindings = [self.store.binding("plan.json",{"kind":"key","key":k}) for k in ("hooks","selected_hook","reason")]
        ranked = sorted(options,key=lambda h:h["score"],reverse=True)
        if ranked[0]["score"]-ranked[1]["score"] <= self.config["card"]["score_margin"] and decision["expensive"] and decision["taste"]:
            visible = ranked[:3]
            if decision not in visible:
                visible[-1] = decision
            card = self.runner.cards.create(kind="creative",stage=stage,target="project",question="选择开篇方向",options=visible,
                recommended=decision["key"],reason=plan["reason"],dedupe="direction:"+digest([options,plan["selected_hook"]]),details={"episode":"ep01","all_candidates":options})
            if not card.get("answer"):
                raise StageBlocked("Direction card required: "+card["id"])
            decision = next(h for h in options if h["key"] == card["answer"]["choice"])
            bindings.append(self.store.binding("logs/decisions.jsonl",{"kind":"decision","card_id":card["id"]}))
        else:
            self.store.append("decisions",{"stage":stage,"target":"project","options":options,"choice":decision["key"],"reason":plan["reason"],"by":"model"})
        job = self.store.start_job(stage,"project",bindings)
        if not self.store.accept(job,stage,"project",bindings,{path:serialize(decision)+"\n"}):
            raise StageBlocked("Direction changed during selection")

    def selected_episodes(self, episodes):
        available = [e["number"] for e in self.store.json("plan.json")["episodes"]]
        selected = list(available if episodes is None else episodes)
        if not set(selected) <= set(available):
            raise SflError("Selected episode does not exist in the plan")
        if 1 not in selected and not self.store.json(".state/locks.json", {}).get("ep01", {}).get("locked"):
            selected.insert(0, 1)
        return selected

    def moment_sources(self, moment_id):
        # A planner's short reference list can omit an action's setup. Include
        # all original passages of its declared beats, including merged sources.
        with self.store.locked():
            moment = file_source(self.store,"plan.json",{"kind":"moment","id":moment_id})
            ids = file_source(self.store,"plan.json",{"kind":"moment_beats","id":moment_id})
            beats = file_source(self.store,"breakdown.json",{"kind":"beats","ids":ids.data})
            moment.data = {**moment.data,"source_beats":beats.data}
            moment.bindings += ids.bindings + beats.bindings
            refs = moment.data["source_refs"] + [ref for beat in beats.data for ref in beat["source_refs"]]
            return moment, self.passages(refs)

    def amplify(self, stage, *, episodes=None):
        plan = self.store.json("plan.json")
        self.amplify_issues = []
        for moment in plan["moments"]:
            if episodes is not None and moment["episode"] not in episodes:
                continue
            if not moment["key"] and not moment["amplify"]:
                continue
            path = f"amplified/{moment['id']}.json"
            if self.store.current(path):
                continue
            count = self.config["best_of_n"]["key_moments" if moment["key"] else "other_moments"]
            moment_source, passages = self.moment_sources(moment["id"])
            sources = {"moment":moment_source,
                "plan_slice":self.plan_slice(moment["episode"]),"source_passages":passages,
                "direction":json_source(self.store,".state/direction.json"),"variant_count":Source(count,[])}
            picked = {}
            def validate(value):
                keys = [v["key"] for v in value["versions"]]
                errors = [] if len(keys)==count and len(keys)==len(set(keys)) else [f"Exactly {count} unique versions required"]
                try:
                    for version in value["versions"]:
                        add_entities(self.store.text("bible.md"),version["entities"])
                except SflError as exc:
                    errors.append(str(exc))
                return errors
            def reviews(value,challenges):
                if not moment["key"] or "payoff_judge" not in self.runner.stages[stage].get("reviewers",[]):
                    only = value["versions"][0]
                    picked.update(choice=only["key"],reason="single_version",sharpened=only["text"],entities=only["entities"],findings=[])
                    return []
                sources["versions"] = Source(value["versions"],[])
                result = self.runner.review("payoff_judge","payoff",stage,f"ep{moment['episode']:02d}:{moment['id']}",sources,challenge=challenges)
                picked.clear(); picked.update(result)
                self.store.write(f"findings/payoff_judge/{moment['id']}_{digest(result)[:12]}.json",result,json_data=True)
                return result["findings"]
            def output(value,store):
                selected = next(v for v in value["versions"] if v["key"]==picked["choice"])
                entries = selected["entities"]+picked["entities"]
                record = {"id":moment["id"],"episode":moment["episode"],"text":picked["sharpened"],"entities":entries,"choice":picked["choice"],"reason":picked["reason"]}
                return {path:serialize(record)+"\n","bible.md":add_entities(store.text("bible.md"),entries)}
            try:
                self.runner.work(stage,f"ep{moment['episode']:02d}:{moment['id']}",sources,validate,output,reviewer=reviews)
            except (StageBlocked,CallPaused) as exc:
                self.amplify_issues.append({"episode":f"ep{moment['episode']:02d}","stage":stage,"reason":str(exc),"paused":isinstance(exc,CallPaused)})

    def episode_sources(self, number):
        if not self.store.path("source/novel.txt").exists():
            return self.adopted_sources(number)
        plan = self.store.json("plan.json")
        slice_source = self.plan_slice(number)
        moments = [m for m in plan["moments"] if m["episode"]==number and (m["key"] or m["amplify"])]
        if any(not self.store.current(f"amplified/{m['id']}.json") for m in moments):
            raise StageBlocked("This episode needs its own current amplified moments before writing")
        amplified = [json_source(self.store,f"amplified/{m['id']}.json") for m in moments]
        entries = [e for a in amplified for e in a.data.get("entities",[])]
        names = set(slice_source.data["characters"]+slice_source.data["entities"]+["系统","旁白"]+[e["name"] for e in entries])
        locations = set(slice_source.data["locations"]+[e["name"] for e in entries if e["kind"]=="location"])
        sources = {"plan_slice":slice_source,"direction":json_source(self.store,".state/direction.json"),
            "amplified":Source([a.data for a in amplified],[b for a in amplified for b in a.bindings]),
            "adopted_script":Source("",[]),
            "bible":file_source(self.store,"bible.md",{"kind":"bible","names":sorted(names),"locations":sorted(locations)}),
            "ledger_in":file_source(self.store,f"ledger/ep{number-1:02d}.md"),
            "source_passages":self.passages(slice_source.data["source_refs"]),
            "script_notes":file_source(self.store,"notes/script_notes.json",{"kind":"script_notes","episode":f"ep{number:02d}"}),
            "episode_target":self.target(number),"previous_last_scene":Source("",[]),"example":Source("",[])}
        recaps = []
        if number > 1:
            if not self.store.current(f"ledger/ep{number-1:02d}.md"):
                raise StageBlocked("Previous ending ledger must finish before writing this episode")
            sources["previous_last_scene"] = file_source(self.store,f"episodes/ep{number-1:02d}.md",{"kind":"last_scene"})
            sources["example"] = file_source(self.store,".state/examples/ep01.md")
            for binding in sources["example"].bindings:
                binding["style_reference"] = True
            locks = self.store.json(".state/locks.json",{})
            for earlier in range(1,number):
                if locks.get(f"ep{earlier:02d}",{}).get("locked"):
                    source = file_source(self.store,f"episodes/ep{earlier:02d}.md",{"kind":"script_hooks"})
                    recaps.append(Source({"episode":earlier,**source.data},source.bindings))
        sources["recap"] = Source([r.data for r in recaps],[b for r in recaps for b in r.bindings])
        return sources

    def adopted_sources(self, number):
        episode = f"ep{number:02d}"
        script_source = file_source(self.store,f"episodes/{episode}.md")
        script = parse_script(script_source.data)
        bible = parse_bible(self.store.text("bible.md"))
        names = {n for scene in script.scenes for n in scene.cast} | {line["who"] for scene in script.scenes for line in scene.lines if "who" in line}
        names.update(name for section in ("Props","UI") for name in bible.get(section,{}) if name in script_source.data)
        locations = sorted({scene.location for scene in script.scenes})
        ledger_path = f"ledger/ep{number-1:02d}.md"
        if not self.store.text(ledger_path,"").strip() or ledger_path in self.store.stale_artifacts():
            raise StageBlocked("The adopted episode needs its current starting ledger before revision")
        sources = {"plan_slice":Source({},[]),"direction":Source({},[]),"amplified":Source([],[]),
            "adopted_script":script_source,
            "bible":file_source(self.store,"bible.md",{"kind":"bible","names":sorted(names),"locations":locations}),
            "ledger_in":file_source(self.store,ledger_path),"source_passages":Source([],[]),
            "script_notes":file_source(self.store,"notes/script_notes.json",{"kind":"script_notes","episode":episode}),
            "episode_target":self.target(number),"previous_last_scene":Source("",[]),"example":Source("",[])}
        locks = self.store.json(".state/locks.json",{})
        recaps = []
        for earlier in range(1,number):
            path = f"episodes/ep{earlier:02d}.md"
            if locks.get(f"ep{earlier:02d}",{}).get("locked") and self.store.path(path).exists():
                source = file_source(self.store,path,{"kind":"script_hooks"})
                recaps.append(Source({"episode":earlier,**source.data},source.bindings))
                if earlier==number-1:
                    sources["previous_last_scene"] = file_source(self.store,path,{"kind":"last_scene"})
        if number>1 and locks.get("ep01",{}).get("locked"):
            path = ".state/examples/ep01.md" if self.store.path(".state/examples/ep01.md").exists() else "episodes/ep01.md"
            if self.store.path(path).exists():
                sources["example"] = file_source(self.store,path)
                for binding in sources["example"].bindings:binding["style_reference"] = True
        sources["recap"] = Source([r.data for r in recaps],[b for r in recaps for b in r.bindings])
        return sources

    def write_episode(self, stage, number):
        episode, path = f"ep{number:02d}",f"episodes/ep{number:02d}.md"
        lock = self.store.json(".state/locks.json",{}).get(episode,{})
        pending = [n for n in self.store.json("notes/script_notes.json",[]) if n["target"]==episode and n["status"]=="pending"]
        rejected = [c for c in self.runner.cards.list(include_resolved=True) if number==1 and c["stage"]=="A8" and c["answer"] and c["answer"]["choice"]=="reject" and not self.store.path(f".state/ep1_rejections/{c['id']}.json").exists()]
        if self.store.current(path) and not pending and not rejected:
            return
        if lock.get("locked") and not pending and not self.runner.may_update(episode):
            raise StageBlocked("Locked episode inputs changed; use script note or Inbox rerun")
        sources = self.episode_sources(number)
        if pending or rejected:
            sources["script_notes"].data = sources["script_notes"].data or []
        repairs = {"user_note":rejected[-1]["answer"]} if rejected and number==1 else {}
        extra = list(sources["recap"].bindings)
        if (pending or rejected or sources["adopted_script"].data) and self.store.path(path).exists():
            repairs["previous_output"] = {"script":self.store.text(path),"ledger_out":self.store.text(f"ledger/{episode}.md","")}
            extra += [self.store.binding(path),self.store.binding(f"ledger/{episode}.md")]
        if repairs:
            if rejected:
                extra += [self.store.binding("logs/decisions.jsonl",{"kind":"decision","card_id":rejected[-1]["id"]})]
        review_record = {}
        format_repairs = []
        original_bible = self.store.text("bible.md")
        def validate(value):
            try:
                candidate_bible = add_entities(original_bible,value["bible_additions"])
                value["script"], repairs = complete_voice_cast(value["script"],parse_bible(candidate_bible))
                format_repairs.clear(); format_repairs.extend(repairs)
                errors = check.episode(value,number,candidate_bible)
                parsed = parse_script(value["script"])
                visible = sources["bible"].data
                additions = {e["name"] for e in value["bible_additions"]}
                allowed_names = set(visible["Characters"]) | set(visible["Extras"]) | set(visible["Voices"]) | additions | {"系统","旁白"}
                for scene in parsed.scenes:
                    if set(scene.cast)-allowed_names or scene.location not in set(visible["Locations"]) | additions:
                        errors.append("Script entities must come from the episode bible slice or explicit bible_additions")
                return errors
            except SflError as exc:
                return [str(exc)]
        def reviews(value,challenges):
            candidate_bible = add_entities(original_bible,value["bible_additions"])
            script = parse_script(value["script"])
            used = sorted({n for scene in script.scenes for n in scene.cast} | {l["who"] for scene in script.scenes for l in scene.lines if "who" in l})
            visible = bible_slice(candidate_bible,used,sorted({s.location for s in script.scenes}))
            warn = check.warnings(value["estimated_seconds"],self.config,episode,script=value["script"],passages=sources["source_passages"].data)
            review_sources = {**sources,"script":Source(value["script"],[]),"bible":Source(visible,sources["bible"].bindings),"warnings":Source(warn,[])}
            roles = self.runner.stages[stage].get("reviewers",[])
            def review_role(role):
                result = self.runner.review(role,"viewer" if role=="viewer" else "findings",stage,episode,review_sources,challenge=challenges)
                self.store.write(f"findings/{role}/{episode}_{digest(result)[:12]}.json",result,json_data=True)
                return role, result
            from storyforge.runner.parallel import run
            results = dict(run(roles,review_role,self.config["concurrency"]["llm"]))
            # Keep stage-defined finding order despite different completion times.
            findings = [finding for role in roles for finding in results[role]["findings"]]
            review_record.clear(); review_record.update(passed=not any(f["severity"] in ("blocker","major") for f in findings),
                script_fingerprint=digest(value["script"]),reviewers=results,warnings=warn,hook_type=value["hook_type"],format_repairs=list(format_repairs))
            return findings
        def output(value,store):
            result = {path:value["script"],f"ledger/{episode}.md":value["ledger_out"],f".state/script_reviews/{episode}.json":serialize(review_record)+"\n",
                "bible.md":add_entities(store.text("bible.md"),value["bible_additions"])}
            if pending:
                notes = store.json("notes/script_notes.json",[])
                for note in notes:
                    if note["id"] in {n["id"] for n in pending}:
                        note["status"] = "applied"
                result["notes/script_notes.json"] = serialize(notes)+"\n"
            if rejected and number==1:
                result[f".state/ep1_rejections/{rejected[-1]['id']}.json"] = serialize({"applied":True})+"\n"
            return result
        self.runner.work(stage,episode,sources,validate,output,reviewer=reviews,repairs=repairs,extra_bindings=extra)

    def first_episode(self, stage):
        return self.write_episode(stage,1)

    def remaining_episode(self, stage, number):
        return self.write_episode(stage,number)

    def episode_approval(self, stage):
        path = "episodes/ep01.md"
        approvals = [c for c in self.runner.cards.list(include_resolved=True) if c["stage"]==stage and c["answer"] and c["answer"]["choice"]=="approve"]
        if approvals:
            return
        metadata = self.store.json(".state/artifacts.json")[path]
        card = self.runner.cards.create(kind="checkpoint",stage=stage,target="ep01",question="确认第一集的故事与节奏",options=[{"key":"approve","label":"批准第一集"},{"key":"reject","label":"备注修改要求"}],
            recommended="approve",reason="第一集会作为后续分集的风格示例",dedupe="episode1:"+metadata["job_id"],
            details={"script":self.store.text(path),"review":self.store.json(".state/script_reviews/ep01.json"),"file":path})
        if not card.get("answer") or card["answer"]["choice"]!="approve":
            raise StageBlocked("Episode 1 approval required: "+card["id"])

    def lock_episode(self, stage, number):
        episode = f"ep{number:02d}"
        path = f"episodes/{episode}.md"
        review_path = f".state/script_reviews/{episode}.json"
        review = self.store.json(review_path,{})
        if not self.store.current(path) or not self.store.current(review_path) or not review.get("passed") or review["script_fingerprint"]!=digest(self.store.text(path)):
            raise StageBlocked("Episode needs current hard checks and independent review before locking")
        locks = self.store.json(".state/locks.json",{})
        fingerprint = digest(self.store.text(path))
        if locks.get(episode,{}).get("script_fingerprint")==fingerprint and locks[episode].get("locked"):
            return
        bindings = [self.store.binding(path),self.store.binding(review_path),self.store.binding(f"ledger/{episode}.md")]
        locks[episode] = {"locked":True,"source":"screenwriter","script_fingerprint":fingerprint,"review":review_path}
        outputs = {".state/locks.json":serialize(locks)+"\n"}
        if number==1:
            outputs[".state/examples/ep01.md"] = self.store.text(path)
        job = self.store.start_job(stage,episode,bindings)
        if not self.store.accept(job,stage,episode,bindings,outputs):
            raise StageBlocked("Episode changed during lock")

    def run(self, *, until, episodes):
        if until not in self.runner.stages:
            raise SflError("Unknown --until stage")
        report = {"demo":bool(self.config.get("demo")),"completed":[],"waiting":[],"paused":[]}
        def execute(stage, target, action):
            try:
                self.runner.check_control()
                if self.runner.cards.blocking(stage,target):
                    raise StageBlocked("Decision required")
                action()
                report["completed"].append(target+":"+stage)
                return True
            except StageBlocked as exc:
                report["waiting"].append({"episode":target,"stage":stage,"reason":str(exc)})
            except CallPaused as exc:
                report["paused"].append({"episode":target,"stage":stage,"reason":str(exc)})
            return False
        with self.store.run_lease():
            self.runner.remember_scope(until, episodes)
            self.store.stale_artifacts()
            for stage,spec in self.runner.stages.items():
                if stage.startswith("A") and int(stage[1:])<=6:
                    if stage == "A6":
                        selected = self.selected_episodes(episodes)
                        if until in ("A7", "A8"):
                            selected = [n for n in selected if n == 1]
                        success = execute(stage, "project", lambda: self.amplify(stage, episodes=selected))
                    else:
                        success = execute(stage,"project",lambda stage=stage,spec=spec:getattr(self,spec["handler"])(stage))
                    if stage=="A6" and getattr(self,"amplify_issues",[]):
                        report["completed"].remove("project:A6")
                        for issue in self.amplify_issues:
                            report["paused" if issue["paused"] else "waiting"].append({k:v for k,v in issue.items() if k!="paused"})
                    if not success or stage==until:
                        return report
            selected = self.selected_episodes(episodes)
            def direct_episode(number):
                episode = f"ep{number:02d}"
                from storyforge.runner.director import Director
                director = Director(self.runner,number)
                for stage,spec in self.runner.stages.items():
                    if not stage.startswith("B"):
                        continue
                    def direct(stage=stage,spec=spec):
                        if stage=="B5" and self.store.path(f"delivery/{episode}/manifest.json").exists() and not self.store.current(f"storyboard/{episode}.json") and not self.runner.may_update(episode):
                            raise StageBlocked("Packaged episode inputs changed; choose rerun in Inbox")
                        return getattr(director,spec["handler"])(stage)
                    success = execute(stage,episode,direct)
                    if stage==until or not success and stage!="B7":
                        break
            # The writer retains episode order. A separate ordered B worker can
            # begin an already locked episode while A writes the next one.
            with ThreadPoolExecutor(max_workers=1,thread_name_prefix="sfl-director") as pool:
                futures = []
                for number in sorted(selected):
                    episode = f"ep{number:02d}"
                    stage = "A7" if number==1 else "A9"
                    if until in ("A7","A8") and number!=1:
                        break
                    if not execute(stage,episode,lambda:self.write_episode(stage,number)):
                        break
                    if stage==until:
                        if until=="A9":continue
                        break
                    if number==1:
                        if not execute("A8",episode,lambda:self.episode_approval("A8")) or until=="A8":
                            break
                    if not execute("A10",episode,lambda:self.lock_episode("A10",number)):
                        break
                    if not until.startswith("A"):
                        futures.append(pool.submit(direct_episode,number))
                for future in futures:future.result()
        return report
