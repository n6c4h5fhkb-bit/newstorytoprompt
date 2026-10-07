from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.support_writer import novel_project, writer_runner, approve_first, plan, timeline, PARAGRAPH
from storyforge.checks import writer as check
from storyforge.checks.schema import require, schema_for
from storyforge.config import SflError
from storyforge.packets import Source, build
from storyforge.runner.source import split
from storyforge.runner.service import note, inbox
from storyforge.runner.writer import plan_text


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-writer-")
        self.addCleanup(self.temporary.cleanup)
        self.store = novel_project(Path(self.temporary.name)/"novel")

    def test_source_addresses_survive_chunk_boundaries(self):
        text = "第一章 测试\n\n第一段。\n\n第二段。\n第二章 下一章\n第三段。"
        small, large = split(text,4), split(text,12000)
        self.assertEqual(small["paragraphs"],large["paragraphs"])
        self.assertEqual(set(small["paragraphs"]),{"ch001:p0001","ch001:p0002","ch002:p0001"})
        self.assertEqual(len(small["chunks"]),3)
        for text in (None,"", "第一章 空白"):
            with self.assertRaises(SflError):
                split(text)

    def test_plan_coverage_hooks_and_paragraph_checks(self):
        index = split(self.store.text("source/novel.txt"))
        clean, beats = plan(), timeline()
        self.assertFalse(check.plan(clean,beats,index))
        for mutation in ("duplicate","missing","bad_ref","wrong_moment","missing_key_turn"):
            with self.subTest(case=mutation):
                candidate = deepcopy(clean)
                if mutation=="duplicate":candidate["episodes"][1]["beats"].append("b01")
                if mutation=="missing":candidate["episodes"][0]["beats"]=[]
                if mutation=="bad_ref":candidate["episodes"][0]["source_refs"]=["ch009:p0001"]
                if mutation=="wrong_moment":candidate["moments"][0]["episode"]=2
                if mutation=="missing_key_turn":candidate["episodes"][1]["major_turn"]=True
                self.assertTrue(check.plan(candidate,beats,index))
        clean["episodes"][0]["end_hook"]["type"]=""
        with self.assertRaises(SflError):require(clean,schema_for("adaptation_plan"))
        clean = plan(); clean["episodes"][0]["emotions"] *= 3
        with self.assertRaises(SflError):require(clean,schema_for("adaptation_plan"))
        for field,value in (("changes",[]),("changes",["a","b","c","d"]),("conflict",""),("immutable",[])):
            clean = plan(); clean["episodes"][0][field] = value
            with self.subTest(field=field,value=value),self.assertRaises(SflError):require(clean,schema_for("adaptation_plan"))
        self.assertIn("改编：原文私下交易", plan_text(plan(1)))

    def test_copied_dialogue_is_flagged_high_but_rewritten_dialogue_is_not(self):
        from tests.support import FIXTURE
        from storyforge.checks.parsers import parse_script
        from storyforge.config import configuration
        script = (FIXTURE / "ep01.md").read_text(encoding="utf-8")
        copied = "".join(l["line"] for s in parse_script(script).scenes for l in s.lines if "who" in l)
        config = configuration()
        found = check.warnings(210, config, "ep01", script=script, passages=[{"text": copied}])
        self.assertEqual([w["level"] for w in found if w["kind"] == "source_overlap"], ["high"])
        self.assertIn("高重合", found[0]["message"])
        self.assertFalse([w for w in check.warnings(210, config, "ep01", script=script, passages=[{"text": "与剧本毫无关系的一段叙述文字。"}]) if w["kind"] == "source_overlap"])

    def test_plan_rejects_unparseable_bible_and_unregistered_plan_names(self):
        index = split(self.store.text("source/novel.txt"))
        for mutation in ("prose", "prose_under_heading", "missing_voice", "unknown_character", "unknown_location"):
            with self.subTest(case=mutation):
                value = plan()
                if mutation == "prose":
                    value["bible"] = "林恒：年轻男声。云清禾：年轻女声。妖商：沙哑男声。"
                elif mutation == "prose_under_heading":
                    value["bible"] = "## Characters\n林恒：年轻男声。云清禾：年轻女声。妖商：沙哑男声。"
                elif mutation == "missing_voice":
                    value["bible"] = value["bible"].replace("年轻男声，冷静", "")
                elif mutation == "unknown_character":
                    value["episodes"][0]["characters"].append("未登记人物")
                else:
                    value["episodes"][0]["locations"].append("未登记地点")
                errors = check.plan(value, timeline(), index)
                self.assertTrue(errors)
                if mutation in ("prose", "prose_under_heading", "missing_voice"):
                    self.assertTrue(any("## Characters" in error and "voice" in error for error in errors))
        self.assertFalse(check.plan(plan(), timeline(), index))

    def test_moment_can_reference_a_beat_merged_into_its_own_episode(self):
        index = split("\n".join(f"第{n}章 开锁\n{PARAGRAPH}" for n in range(1, 4)))
        clean, beats = plan(), timeline(3)
        clean["decisions"].append({"beat_id":"b03", "action":"merge", "merge_into":"b01", "reason":"同一动作"})
        clean["episodes"][0]["source_refs"].append("ch003:p0001")
        clean["moments"][0]["source_refs"] = ["ch003:p0001"]
        for ids in (["b03"], ["b01", "b03"]):
            with self.subTest(valid_ids=ids):
                clean["moments"][0]["beat_ids"] = ids
                self.assertFalse(check.plan(clean, beats, index))
        for mutation in ("other_episode", "cut", "unknown", "missing_source"):
            with self.subTest(invalid=mutation):
                value = deepcopy(clean)
                if mutation == "other_episode":
                    value["decisions"][-1]["merge_into"] = "b02"
                    value["episodes"][1]["source_refs"].append("ch003:p0001")
                elif mutation == "cut":
                    value["decisions"][-1].update(action="cut", merge_into="")
                elif mutation == "unknown":
                    value["moments"][0]["beat_ids"] = ["b99"]
                else:
                    value["episodes"][0]["source_refs"].remove("ch003:p0001")
                errors = check.plan(value, beats, index)
                self.assertTrue(errors)
                if mutation in ("other_episode", "cut"):
                    self.assertTrue(any("moment beats must belong" in e for e in errors))

    def test_merged_moment_reaches_amplification_with_original_source(self):
        def merge(value, packet, target, count):
            if packet.role == "adapt_plan":
                value["decisions"][1].update(action="merge", merge_into="b01")
                value["episodes"] = value["episodes"][:1]
                value["episodes"][0]["source_refs"].append("ch002:p0001")
                value["moments"] = value["moments"][:1]
                value["moments"][0].update(beat_ids=["b02"], source_refs=["ch002:p0001"])
        runner, model = writer_runner(self.store, mutate=merge)
        report = runner.run(until="A8")
        self.assertFalse(report["paused"], report)
        self.assertEqual(model.counts["adapt_plan:project"], 1)
        self.assertTrue(any(c["stage"] == "A8" for c in runner.cards.list()))
        packet = next(data for role, target, data, repairs in model.packets if role == "amplify")
        self.assertEqual(packet["moment"]["beat_ids"], ["b02"])
        self.assertEqual([p["ref"] for p in packet["source_passages"]], ["ch002:p0001"])

    def test_planner_and_reviewer_receive_bible_and_starting_state_contract(self):
        for role, fields in (("adapt_plan", ("breakdown", "source_passages", "episode_target")),
                             ("plan_reviewer", ("plan", "breakdown", "source_passages", "warnings"))):
            packet = build(self.store, role, {name: Source({}, []) for name in fields})
            self.assertIn("## Characters", packet.instruction)
            self.assertIn("name | role | look | voice | source name", packet.instruction)
            self.assertIn("ledger_in 只描述所选开篇之前的状态", packet.instruction)
            self.assertTrue(any(b["path"].endswith("bible_and_ledger.md") for b in packet.bindings))

    def test_amplification_and_judge_receive_complete_scoped_source(self):
        self.store.write("source/novel.txt", "第一章 开锁\n\n" + PARAGRAPH
            + "\n\n钥匙一直在妖商腰间。\n第二章 后续\n\n下一集才归还欠款。")
        def with_setup(value, packet, target, count):
            if packet.role == "chunk_summarizer":
                for n, event in enumerate(value["events"]):
                    event["id"] = f"{target}_b{n:02d}"
            elif packet.role == "breakdown":
                value["beats"][0]["source_refs"].append("ch001:p0002")
            elif packet.role == "adapt_plan":
                value["episodes"][0]["source_refs"].append("ch001:p0002")
                value["moments"][0]["summary"] = "只到看见腰间钥匙，暂未开锁。"
        runner, model = writer_runner(self.store, mutate=with_setup)
        report = runner.run(until="A6", episodes=[1])
        self.assertFalse(report["paused"], report)
        self.assertFalse(report["waiting"], report)
        for role in ("amplify", "payoff_judge"):
            packet = next(data for name, target, data, repairs in model.packets if name == role)
            self.assertEqual([p["ref"] for p in packet["source_passages"]], ["ch001:p0001", "ch001:p0002"])
            self.assertIn("暂未开锁", packet["moment"]["summary"])
            self.assertEqual([b["id"] for b in packet["moment"]["source_beats"]], ["b01"])
            self.assertNotIn("下一集才归还欠款", str(packet))
            if role == "payoff_judge":
                self.assertEqual(set(packet), {"moment", "versions", "source_passages"})
        # A changed omitted setup must invalidate this moment's factual input.
        bindings = self.store.json(".state/artifacts.json")["amplified/m01.json"]["bindings"]
        breakdown = self.store.json("breakdown.json")
        breakdown["beats"][1]["summary"] = "下一集修改，与当前开篇无关。"
        self.store.write("breakdown.json", breakdown, json_data=True)
        self.assertTrue(self.store.unchanged(bindings, include_style=False))
        index = self.store.json("source/index.json")
        index["paragraphs"]["ch001:p0002"]["text"] = "钥匙已经不在妖商身上。"
        self.store.write("source/index.json", index, json_data=True)
        self.assertFalse(self.store.unchanged(bindings, include_style=False))

    def test_moment_source_includes_merged_setup_but_not_unrelated_beats(self):
        def merge(value, packet, target, count):
            if packet.role == "adapt_plan":
                value["decisions"][1].update(action="merge", merge_into="b01")
                value["episodes"] = value["episodes"][:1]
                value["episodes"][0]["source_refs"].append("ch002:p0001")
                value["moments"] = value["moments"][:1]
        runner, model = writer_runner(self.store, mutate=merge)
        self.assertFalse(runner.run(until="A6")["paused"])
        for role in ("amplify", "payoff_judge"):
            packet = next(data for name, target, data, repairs in model.packets if name == role)
            self.assertEqual([p["ref"] for p in packet["source_passages"]], ["ch001:p0001", "ch002:p0001"])
            self.assertEqual([b["id"] for b in packet["moment"]["source_beats"]], ["b01", "b02"])

    def test_voice_cast_completion_reaches_review_without_author_rewrite(self):
        def voice_only(value, packet, target, count):
            if packet.role == "episode_writer":
                value["script"] += "\n议价声（画外）：三十文。\n"
                value["bible_additions"] = [{"kind":"voice", "name":"议价声",
                    "description":"场外议价的人声", "voice":"低沉男声"}]
        runner, model = writer_runner(self.store, mutate=voice_only)
        report = runner.run(until="A8")
        self.assertFalse(report["paused"], report)
        self.assertTrue(any(c["stage"] == "A8" for c in runner.cards.list()))
        self.assertEqual(model.counts["episode_writer:ep01"], 1)
        script = self.store.text("episodes/ep01.md")
        review = self.store.json(".state/script_reviews/ep01.json")
        self.assertTrue(review["passed"])
        self.assertEqual(review["format_repairs"], [{"scene":"S01", "voice_sources":["议价声"]}])
        for role in ("viewer", "story_check"):
            packet = next(data for name, target, data, repairs in model.packets if name == role)
            self.assertEqual(packet["script"], script)
            self.assertIn("、议价声", script.split("出场：", 1)[1].splitlines()[0])

    def test_first_checkpoint_then_rolling_lock_has_clean_review_packets(self):
        runner, model = writer_runner(self.store)
        self.assertTrue(runner.run(until="A10")["waiting"])
        preview = next(i["preview"] for i in inbox(self.store)["items"] if i.get("card",{}).get("stage")=="A8")
        self.assertIn("# EP01",preview["script"])
        self.assertTrue(preview["review"]["passed"])
        approve_first(runner)
        self.assertFalse(self.store.json(".state/locks.json",{}))
        report = runner.run(until="A10")
        self.assertFalse(report["waiting"],report)
        self.assertFalse(report["paused"],report)
        self.assertTrue(all(l["locked"] for l in self.store.json(".state/locks.json").values()))
        self.assertEqual(len(runner.cards.list(include_resolved=True)),1)
        self.assertEqual(model.counts["payoff_judge:ep01:m01"],1)
        self.assertNotIn("payoff_judge:ep02:m02",model.counts)
        for role,target,data,repairs in model.packets:
            if role=="viewer":
                self.assertEqual(set(data),{"script","recap","example","warnings"})
                self.assertFalse(repairs)
                if target=="ep01":self.assertFalse(data["recap"])
                else:self.assertEqual(set(data["recap"][0]),{"episode","hook","cliffhanger"})
            if role=="episode_writer" and target=="ep02":
                self.assertEqual(data["ledger_in"],self.store.text("ledger/ep01.md"))
                self.assertTrue(data["example"])
        before = sum(model.counts.values())
        self.assertFalse(runner.run(until="A10")["waiting"])
        self.assertEqual(sum(model.counts.values()),before)

    def test_first_episode_preview_defers_later_amplification_until_continuation(self):
        runner, model = writer_runner(self.store)
        report = runner.run(until="A8")
        self.assertTrue(report["waiting"])
        self.assertEqual(model.counts["amplify:ep01:m01"], 1)
        self.assertEqual(model.counts["amplify:ep02:m02"], 0)
        self.assertFalse(self.store.path("amplified/m02.json").exists())
        self.assertFalse(self.store.path("episodes/ep02.md").exists())
        card = next(c for c in runner.cards.list() if c["stage"] == "A8")
        runner.cards.answer(card["id"], "approve")
        report = runner.run(until="A10")
        self.assertFalse(report["waiting"], report)
        self.assertFalse(report["paused"], report)
        self.assertEqual(model.counts["amplify:ep01:m01"], 1)
        self.assertEqual(model.counts["amplify:ep02:m02"], 1)
        self.assertTrue(self.store.json(".state/locks.json")["ep02"]["locked"])

    def test_selected_episode_prepares_only_itself_and_required_first_episode(self):
        store = novel_project(Path(self.temporary.name)/"three-episodes", count=3)
        runner, model = writer_runner(store, count=3)
        report = runner.run(until="A10", episodes=[2])
        self.assertTrue(report["waiting"])
        self.assertEqual(model.counts["amplify:ep03:m03"], 0)
        card = next(c for c in runner.cards.list() if c["stage"] == "A8")
        runner.cards.answer(card["id"], "approve")
        report = runner.run(until="A10", episodes=[2])
        self.assertFalse(report["waiting"], report)
        self.assertFalse(report["paused"], report)
        self.assertEqual(set(store.json(".state/locks.json")), {"ep01", "ep02"})
        self.assertEqual(model.counts["amplify:ep03:m03"], 0)
        self.assertFalse(store.path("amplified/m03.json").exists())
        report = runner.run(until="A10")
        self.assertFalse(report["waiting"], report)
        self.assertEqual(model.counts["amplify:ep03:m03"], 1)
        self.assertTrue(store.json(".state/locks.json")["ep03"]["locked"])

    def test_key_summary_misread_is_repaired_from_original_passage(self):
        def mutate(value,packet,target,count):
            if packet.role=="chunk_summarizer":value["events"][0]["text"]="林恒手握铜钥匙。"
            if packet.role=="adapt_plan" and count==1:value["adaptation"]="林恒手握铜钥匙。"
            if packet.role=="plan_reviewer" and "林恒手握铜钥匙。" in packet.data["plan"]["adaptation"]:
                self.assertEqual(packet.data["source_passages"][0]["text"],PARAGRAPH)
                value["findings"]=[{"severity":"blocker","kind":"omission","location":"b01","evidence":"妖商手握铜钥匙。","problem":"计划把持有者写错了。","suggested_fix":"恢复原文持有者。"}]
        runner, model = writer_runner(self.store,mutate=mutate)
        approve_first(runner)
        self.assertEqual(model.counts["adapt_plan:project"],2)
        self.assertNotIn("林恒手握铜钥匙。",self.store.text("plan.md"))

    def test_ledger_contradiction_and_missing_hook_are_repaired(self):
        def mutate(value,packet,target,count):
            if packet.role=="episode_writer" and target=="ep01" and count==1:
                value["script"]=value["script"].replace("妖商站在柜台后，手握铜钥匙", "妖商站在柜台后，云清禾手握铜钥匙")
            if packet.role=="story_check" and "云清禾手握铜钥匙" in packet.data["script"]:
                self.assertIn("铜钥匙",packet.data["ledger_in"])
                value["findings"]=[{"severity":"blocker","kind":"error","location":"S01","evidence":"云清禾手握铜钥匙","problem":"与起始持有物冲突。","suggested_fix":"让妖商持有钥匙。"}]
            if packet.role=="episode_writer" and target=="ep02" and count==1:
                value["script"]="\n".join(l for l in value["script"].splitlines() if not l.startswith("cliffhanger:"))
        runner, model = writer_runner(self.store,mutate=mutate)
        approve_first(runner)
        self.assertFalse(runner.run(until="A10")["waiting"])
        self.assertEqual(model.counts["episode_writer:ep01"],2)
        self.assertEqual(model.counts["episode_writer:ep02"],2)

    def test_local_script_note_does_not_stale_later_episode(self):
        def mutate(value,packet,target,count):
            if packet.role=="episode_writer":
                value["script"] += "\n## S02 妖商店 · 日 · 内\n出场：妖商\n△ 妖商留在柜台后，铜钥匙仍在柜台上。\n"
        runner, model = writer_runner(self.store,mutate=mutate)
        approve_first(runner); runner.run(until="A10")
        before = model.counts["episode_writer:ep02"]
        note(self.store,"ep01","只调整中间一句，保持结束状态、最后一场和钩子。")
        stale = self.store.stale_artifacts()
        self.assertIn("episodes/ep01.md",stale)
        self.assertNotIn("episodes/ep02.md",list(stale))
        self.assertFalse(runner.run(until="A10")["waiting"])
        self.assertEqual(model.counts["episode_writer:ep02"],before)
        self.assertIn("林恒：把锁打开。",self.store.text("episodes/ep01.md"))
        self.assertEqual(self.store.json("notes/script_notes.json")[0]["status"],"applied")

    def test_direction_card_requires_all_three_conditions(self):
        def mutate(value,packet,target,count):
            if packet.role=="adapt_plan":
                value["hooks"][0].update(expensive=True,score=.8)
                value["hooks"][1]["score"] = .79
        runner, model = writer_runner(self.store,mutate=mutate)
        self.assertTrue(runner.run(until="A10")["waiting"])
        direction = next(c for c in runner.cards.list() if c["stage"]=="A5")
        runner.cards.answer(direction["id"],"b")
        approve_first(runner)
        self.assertFalse(runner.run(until="A10")["waiting"])
        self.assertEqual(self.store.json(".state/direction.json")["key"],"b")
        self.assertEqual(len(runner.cards.list(include_resolved=True)),2)

    def test_runtime_is_a_warning_and_future_ledger_is_not_an_input(self):
        self.store.write("ledger/ep99.md","绝不可见的未来状态")
        def mutate(value,packet,target,count):
            if packet.role=="episode_writer":value["estimated_seconds"]=100
        runner, model = writer_runner(self.store,mutate=mutate)
        approve_first(runner)
        self.assertFalse(runner.run(until="A10")["waiting"])
        self.assertEqual(self.store.json(".state/script_reviews/ep01.json")["warnings"][0]["kind"],"runtime")
        self.assertTrue(all("绝不可见" not in str(data) for _,_,data,_ in model.packets))

    def test_later_amplification_card_does_not_block_first_episode(self):
        runner, model = writer_runner(self.store)
        runner.cards.create(kind="stuck",stage="A6",target="ep02:m02",question="第二集需要决定",options=[{"key":"retry","label":"重试"},{"key":"stop","label":"停止"}],recommended="retry",reason="测试隔离",dedupe="future-amplification")
        approve_first(runner)
        report = runner.run(until="A10")
        self.assertTrue(report["waiting"])
        self.assertTrue(self.store.json(".state/locks.json")["ep01"]["locked"])
        self.assertFalse(self.store.path("episodes/ep02.md").exists())
        self.assertEqual(model.counts["episode_writer:ep01"],1)


if __name__=="__main__":unittest.main()
