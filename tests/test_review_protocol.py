from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tests.support import RESPONSES, project
from storyforge.config import configuration
from storyforge.llm import Client
from storyforge.packets import Source, file_source
from storyforge.runner import Runner, StageBlocked
from storyforge.store import serialize


class ReviewProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sfl-review-")
        self.addCleanup(self.temporary.cleanup)
        self.store = project(Path(self.temporary.name) / "novel")

    def test_invalid_blind_evidence_raises_card_and_retry_keeps_intent_hidden(self):
        packets = []
        def transport(profile, packet, schema, **kwargs):
            packets.append(packet)
            value = deepcopy(RESPONSES["reconstruction_blind:ep01_u01"])
            if not packet.repairs.get("retry_id"):
                value["findings"] = [{"severity":"blocker", "kind":"omission", "location":"prompt", "evidence":"SECRET_INTENT",
                    "problem":"This quote is outside the blind packet", "suggested_fix":"Use visible evidence"}]
            return {"text": serialize(value), "usage": {"input_tokens": 5, "output_tokens": 5}, "error": None}
        config = configuration()
        runner = Runner(self.store, config=config, client=Client(self.store, config, codex_transport=transport))
        sources = {"prompt": Source("她站在门口。", []), "reference_descriptions": Source([], []),
                   "model_card": Source(runner.card, []), "warnings": Source([], []), "script": Source("SECRET_INTENT", [])}
        with self.assertRaises(StageBlocked):
            runner.review("reconstruction_blind", "reconstruction", "B8", "ep01_u01", sources)
        card = runner.cards.list()[0]
        self.assertEqual(card["kind"], "technical")
        self.assertTrue(runner.cards.blocking("B8", "ep01_u01"))
        self.assertFalse(runner.cards.blocking("B8", "ep01_u02"))
        count = len(packets)
        with self.assertRaises(StageBlocked):
            runner.review("reconstruction_blind", "reconstruction", "B8", "ep01_u01", sources)
        self.assertEqual(len(packets), count)
        runner.cards.answer(card["id"], "retry", "不要把剧本意图传给盲审")
        result = runner.review("reconstruction_blind", "reconstruction", "B8", "ep01_u01", sources)
        self.assertFalse(result["findings"])
        self.assertTrue(all("script" not in p.data and "user_note" not in p.repairs for p in packets))
        self.assertEqual(packets[-1].repairs, {"retry_id": card["id"]})

    def test_episode_budget_does_not_include_longer_episode_number(self):
        runner = Runner(self.store)
        runner.config["token_budget"]["per_episode"] = 1
        self.store.append("calls", {"target": "ep100_u01", "input_tokens": 1000, "output_tokens": 100})
        runner.budget("B7", "ep10_u01")
        self.assertFalse(runner.cards.list())

    def test_challenges_reach_only_their_reviewer_and_artifact(self):
        packets = []
        def transport(profile, packet, schema, **kwargs):
            packets.append(packet)
            value = {"findings":[]}
            if packet.role=="viewer":value.update(keep_watching=True,reason="Test")
            return {"text":serialize(value),"usage":{"input_tokens":5,"output_tokens":5},"error":None}
        config = configuration()
        runner = Runner(self.store,config=config,client=Client(self.store,config,codex_transport=transport))
        sources = {key:Source(value,[]) for key,value in {"script":"测试剧本", "recap":[], "example":"", "warnings":[],
            "plan_slice":{},"adopted_script":"", "bible":"", "ledger_in":"", "source_passages":[], "script_notes":[]}.items()}
        challenges = [{"finding_id":key,"disposition":"reject","reason":key,"reviewer":role,"artifact":target}
            for key,role,target in (("own","viewer","ep01"),("story","story_check","ep01"),("future","viewer","ep02"))]
        challenges.append({"finding_id":"unscoped","disposition":"reject","reason":"unscoped"})
        runner.review("viewer","viewer","A7","ep01",sources,challenge=challenges)
        runner.review("story_check","findings","A7","ep01",sources,challenge=challenges)
        self.assertEqual([r["reason"] for r in packets[0].repairs["challenge"]],["own"])
        self.assertEqual([r["reason"] for r in packets[1].repairs["challenge"]],["story"])
        self.assertNotIn("script_notes",packets[0].data)

    def test_unknown_response_id_cannot_be_accepted(self):
        def transport(profile, packet, schema, **kwargs):
            value = deepcopy(RESPONSES["art_director:ep01"])
            value["responses"] = [{"finding_id":"invented","disposition":"fix","reason":"Test"}]
            return {"text":serialize(value),"usage":{"input_tokens":5,"output_tokens":5},"error":None}
        config = configuration()
        runner = Runner(self.store,config=config,client=Client(self.store,config,codex_transport=transport))
        with self.assertRaises(StageBlocked):
            runner.work("B1","ep01",{"bible":Source("测试设定",[]),"script":Source("测试剧本",[])},
                        lambda value:[],lambda value,store:{"forged.txt":"accepted"})
        self.assertFalse(self.store.path("forged.txt").exists())
        self.assertIn("exactly",runner.cards.list()[0]["details"]["errors"][0])

    def test_mixed_hard_errors_and_findings_repair_only_declared_response_ids(self):
        packets = []
        def transport(profile, packet, schema, **kwargs):
            packets.append(packet)
            value = deepcopy(RESPONSES["art_director:ep01"])
            value["responses"] = [{"finding_id":"f_review", "disposition":"fix", "reason":"已修复审查问题"}]
            if len(packets) == 1:
                value["responses"].append({"finding_id":"M05C", "disposition":"fix", "reason":"已修复机械错误"})
            else:
                error = "\n".join(packet.repairs["hard_errors"])
                self.assertIn("Expected IDs: ['f_review']", error)
                self.assertIn("received IDs: ['f_review', 'M05C']", error)
                self.assertIn("hard_errors are not findings", error)
            return {"text":serialize(value), "usage":{"input_tokens":5,"output_tokens":5}, "error":None}
        config = configuration()
        runner = Runner(self.store, config=config, client=Client(self.store, config, codex_transport=transport))
        finding = {"id":"f_review", "severity":"major", "kind":"error", "location":"style", "evidence":"测试",
            "problem":"测试问题", "suggested_fix":"修正", "reviewer":"plan_reviewer", "artifact":"ep01"}
        result = runner.work("B1", "ep01", {"bible":Source("测试设定",[]), "script":Source("测试剧本",[])},
            lambda value:[], lambda value,store:{"repaired.txt":"accepted"}, reviewer=lambda value,challenges:[],
            repairs={"findings":[finding], "hard_errors":["M05C: mechanical validation error"]})
        self.assertEqual(len(packets), 2)
        self.assertIn("hard_errors、reference_limit_errors、user_note 都不是 findings", packets[0].instruction)
        self.assertEqual([r["finding_id"] for r in result["responses"]], ["f_review"])
        self.assertEqual(result["responses"][0]["reviewer"], "plan_reviewer")
        self.assertEqual(self.store.text("repaired.txt"), "accepted")
        self.assertFalse(runner.cards.list())

    def test_format_errors_do_not_use_up_the_rewrites_a_review_still_needs(self):
        calls = []
        def transport(profile, packet, schema, **kwargs):
            calls.append(packet)
            value = deepcopy(RESPONSES["art_director:ep01"])
            value["responses"] = [{"finding_id": f["id"], "disposition": "fix", "reason": "已修复"} for f in packet.repairs.get("findings", []) if f["severity"] in ("major", "blocker")]
            return {"text": serialize(value), "usage": {"input_tokens": 5, "output_tokens": 5}, "error": None}
        config = configuration()
        runner = Runner(self.store, config=config, client=Client(self.store, config, codex_transport=transport))
        finding = {"id": "f_late", "severity": "major", "kind": "error", "location": "style", "evidence": "测试",
                   "problem": "审查指出的问题", "suggested_fix": "修正", "reviewer": "plan_reviewer", "artifact": "ep01"}
        validations, reviews = [], []
        def validator(value):
            validations.append(1)
            return [f"格式错误{len(validations)}"] if len(validations) <= 2 else []
        def reviewer(value, challenges, previous=None):
            reviews.append(previous)
            return [dict(finding)] if len(reviews) == 1 else []
        runner.work("B1", "ep01", {"bible": Source("测试设定", []), "script": Source("测试剧本", [])}, validator,
                    lambda value, store: {"done.txt": "accepted"}, reviewer=reviewer)
        # Two format retries, the first review that finds a major, then the rewrite it asked for.
        self.assertEqual(len(calls), 4)
        self.assertEqual(reviews[0], [])
        self.assertEqual([f["id"] for f in reviews[1]], ["f_late"])
        self.assertEqual(self.store.text("done.txt"), "accepted")
        self.assertFalse(runner.cards.list())

    def test_card_retry_restores_candidate_only_while_its_inputs_are_current(self):
        for changed in (False, True):
            with self.subTest(changed_inputs=changed):
                store = project(Path(self.temporary.name) / str(changed))
                packets = []
                def transport(profile, packet, schema, **kwargs):
                    packets.append(packet)
                    value = deepcopy(RESPONSES["art_director:ep01"])
                    if packet.repairs.get("user_note"):
                        if changed:
                            self.assertNotIn("previous_output", packet.repairs)
                            self.assertNotIn("findings", packet.repairs)
                            self.assertIn("新输入", packet.data["script"])
                            value["responses"] = []
                        else:
                            self.assertEqual(packet.repairs["previous_output"], saved["candidate"])
                            self.assertEqual(packet.repairs["findings"], saved["findings"])
                            self.assertEqual(packet.repairs["hard_errors"], saved["errors"])
                            value["responses"] = [{"finding_id":"f_review", "disposition":"fix", "reason":"保留已修订内容并修复格式"}]
                    else:
                        value["responses"] = [{"finding_id":"M05C", "disposition":"fix", "reason":"错误编号"}]
                    return {"text":serialize(value), "usage":{"input_tokens":5,"output_tokens":5}, "error":None}
                config = configuration()
                runner = Runner(store, config=config, client=Client(store, config, codex_transport=transport))
                def sources():
                    return {"bible":file_source(store,"bible.md"), "script":file_source(store,"episodes/ep01.md")}
                finding = {"id":"f_review", "severity":"major", "kind":"error", "location":"style", "evidence":"测试",
                    "problem":"测试问题", "suggested_fix":"修正", "reviewer":"test_reviewer", "artifact":"ep01"}
                with self.assertRaises(StageBlocked):
                    runner.work("B1", "ep01", sources(), lambda value:[], lambda value,store:{"retry-result.txt":"accepted"},
                        reviewer=lambda value,challenges:[], repairs={"findings":[finding]})
                card = runner.cards.list()[0]
                saved = store.json(card["details"]["candidate_file"])
                # The initial supplied findings must remain available even if
                # hard errors prevent another reviewer call in this run.
                self.assertEqual(saved["findings"], [finding])
                runner.cards.answer(card["id"], "retry", "保留已有修订")
                if changed:
                    store.write("episodes/ep01.md", store.text("episodes/ep01.md") + "\n新输入\n")
                runner = Runner(store, config=config, client=Client(store, config, codex_transport=transport))
                runner.work("B1", "ep01", sources(), lambda value:[], lambda value,store:{"retry-result.txt":"accepted"}, reviewer=lambda value,challenges:[])
                self.assertEqual(store.text("retry-result.txt"), "accepted")
                self.assertFalse(runner.cards.list())
                bound_paths = {b["path"] for b in store.json(".state/artifacts.json")["retry-result.txt"]["bindings"]}
                self.assertEqual(card["details"]["candidate_file"] in bound_paths, not changed)


class FollowUpTests(unittest.TestCase):
    def finding(self, severity, location, problem, evidence="一段足够长的证据文字"):
        return {"severity": severity, "location": location, "problem": problem, "evidence": evidence, "kind": "error", "suggested_fix": "改"}

    def test_a_new_major_that_touches_no_earlier_finding_becomes_a_suggestion(self):
        from storyforge.runner import limit_new_findings
        earlier = [self.finding("major", "ep01_u02 镜头3", "沈浩手里的酒杯去向不明", "沈浩转着手中酒杯")]
        same_place = self.finding("major", "ep01_u02 镜头3", "酒杯仍未交代")
        same_problem = self.finding("major", "S07", "沈浩手里的酒杯去向不明，可能凭空多出一只")
        brand_new = self.finding("major", "ep01_u09 镜头1", "台词偏紧", "完全不同的另一句证据")
        blocker = self.finding("blocker", "ep01_u11 镜头2", "人物凭空消失")
        result = limit_new_findings([same_place, same_problem, brand_new, blocker], earlier)
        self.assertEqual([f["severity"] for f in result], ["major", "major", "minor", "blocker"])
        self.assertTrue(result[2]["demoted"])
        self.assertTrue(result[2]["problem"].startswith("（复核时的新发现"))
        # The first round has nothing to follow up, so nothing is changed.
        self.assertEqual(limit_new_findings([brand_new], []), [brand_new])

    def test_a_finding_the_user_already_decided_on_never_blocks_again(self):
        from storyforge.runner import respect_user_decision
        decided = [self.finding("major", "S02 结尾", "第一集目标不清", "沈砚：“这五枚铜板，你亲手还我。”")]
        again = self.finding("major", "S02 结尾 / 沈砚立誓", "上一轮指出的目标不清仍未解决")
        different = self.finding("major", "S05", "完全无关的道具数量问题", "另一处证据证据证据")
        blocker = self.finding("blocker", "S02 结尾", "第一集目标不清且人物消失")
        result = respect_user_decision([again, different, blocker], decided)
        self.assertEqual([f["severity"] for f in result], ["minor", "major", "blocker"])
        self.assertTrue(result[0]["user_decided"])
        self.assertEqual(respect_user_decision([again], []), [again])

    def test_the_reviewers_that_iterate_receive_the_previous_findings(self):
        import yaml
        from storyforge import ROOT
        policies = yaml.safe_load((ROOT / "platform/packets.yaml").read_text(encoding="utf-8"))
        for role in ("plan_reviewer", "story_check", "scene_fidelity", "reconstruction_compare"):
            self.assertIn("previous_findings", policies[role]["inputs"], role)
            self.assertIn("skills/shared/re_review.md", policies[role]["references"], role)
        self.assertNotIn("previous_findings", policies["reconstruction_blind"]["inputs"])
        self.assertNotIn("previous_findings", policies["viewer"]["inputs"])


if __name__ == "__main__":
    unittest.main()
