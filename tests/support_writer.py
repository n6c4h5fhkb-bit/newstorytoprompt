"""Explicit synthetic responses for protocol tests, never quality acceptance."""
from collections import Counter
from copy import deepcopy
import json
import re

from tests.support import FIXTURE, RESPONSES
from storyforge.config import configuration
from storyforge.llm import Client
from storyforge.runner import Runner
from storyforge.store import create_project, serialize

SCRIPT = (FIXTURE / "ep01.md").read_text(encoding="utf-8")
BIBLE = (FIXTURE / "bible.md").read_text(encoding="utf-8")
LEDGER = (FIXTURE / "ep00.md").read_text(encoding="utf-8")
BEATS = [
    {"scene": "S01", "kind": "press", "intensity": 2, "at_seconds": 5, "summary": "妖商把腕镣钥匙压在柜台上不松手"},
    {"scene": "S01", "kind": "press", "intensity": 3, "at_seconds": 25, "summary": "妖商开价，云清禾被奴印压得抓桌"},
    {"scene": "S01", "kind": "burst", "intensity": 5, "at_seconds": 60, "summary": "林恒当众亮出系统提示，逼妖商开锁"},
    {"scene": "S01", "kind": "payoff", "intensity": 4, "at_seconds": 90, "summary": "腕镣落地，妖商失态"},
    {"scene": "S01", "kind": "turn", "intensity": 3, "at_seconds": 130, "summary": "云清禾离店，林恒追出"},
    {"scene": "S01", "kind": "hook", "intensity": 4, "at_seconds": 200, "summary": "忠诚度为何为负？"},
]
PARAGRAPH = "妖商手握铜钥匙。林恒要求开锁，云清禾离开妖商店。"


def novel_project(path, count=2):
    store = create_project(path, {"name":path.name,"demo":True})
    store.write("source/novel.txt", "\n\n".join(f"第{n}章 开锁\n\n{PARAGRAPH}" for n in range(1,count+1)))
    store.snapshot("import explicitly synthetic novel")
    return store


def timeline(count=2):
    return {"main_plot":"林恒让云清禾摆脱束缚。","subplots":[],"character_arcs":[{"name":"林恒","arc":"主动解决问题"}],
        "beats":[{"id":f"b{n:02d}","summary":PARAGRAPH,"characters":["林恒","云清禾","妖商"],"tags":["悬念"],"intensity":4,
                  "source_refs":[f"ch{n:03d}:p0001"],"key":True} for n in range(1,count+1)],"responses":[]}


def plan(count=2):
    episodes = [{"number":n,"title":"开锁","beats":[f"b{n:02d}"],"source_refs":[f"ch{n:03d}:p0001"],"estimated_seconds":210,
        "emotions":["愤怒","期待"],"turn":"云清禾摆脱束缚", "conflict":"林恒当众逼妖商交出钥匙，妖商一步步加价拖延", "changes":["原文私下交易 → 改为当众限时三十秒 → 加入旁观者和时限，压迫更直接"], "immutable":["妖商手握铜钥匙","云清禾最终离开妖商店"], "end_hook":{"type":"pending_reveal","description":"忠诚度为何为负？"},
        "major_turn":n%3==0,"retention_checkpoint":n%6==0,"characters":["林恒","云清禾","妖商"],"locations":["妖商店"],"entities":[]}
        for n in range(1,count+1)]
    moments = [{"id":f"m{n:02d}","episode":n,"beat_ids":[f"b{n:02d}"],"summary":"开锁后得到自由","source_refs":[f"ch{n:03d}:p0001"],
        "key":n==1 or n%3==0,"amplify":True} for n in range(1,count+1)]
    return {"adaptation":"两段情绪推动一次行动。","differentiation":"开锁后忠诚度反而为负。","decisions":[{"beat_id":f"b{n:02d}","action":"keep","merge_into":"","reason":"主线行动"} for n in range(1,count+1)],
        "episodes":episodes,"hooks":[{"key":key,"label":label,"score":score,"expensive":False,"taste":True} for key,label,score in [("a","开锁",.9),("b","等待",.5),("c","离开",.3)]],
        "selected_hook":"a","reason":"开篇行动明确。","moments":moments,"bible":BIBLE,"ledger_in":LEDGER,"responses":[]}


class WriterCodex:
    def __init__(self, count=2, mutate=None):
        self.count, self.mutate = count, mutate
        self.counts, self.packets = Counter(), []

    def __call__(self, profile, packet, schema, *, stage, target):
        assert profile["model"]=="gpt-6-sol" and profile["effort"]=="high"
        role, data = packet.role, packet.data
        self.counts[role+":"+target] += 1
        self.packets.append((role,target,deepcopy(data),deepcopy(packet.repairs)))
        if role=="chunk_summarizer":
            chunk = data["chunk"]
            value = {"chunk_id":chunk["id"],"events":[{"id":chunk["id"]+"_b01","text":p["text"],"characters":["林恒","云清禾","妖商"],"tags":["悬念"],"intensity":4,"source_refs":[p["ref"]],"fact_status":"fact"} for p in chunk["paragraphs"]],"entities":[],"responses":[]}
        elif role=="breakdown":
            value = timeline(self.count)
        elif role=="adapt_plan":
            value = plan(self.count)
        elif role=="amplify":
            value = {"versions":[{"key":f"v{n}","label":f"方案{n}","text":"云清禾主动离开。","gain":"恢复自由。","entities":[]} for n in range(1,data["variant_count"]+1)],"responses":[]}
        elif role=="payoff_judge":
            value = {"choice":data["versions"][0]["key"],"reason":"行动清晰。","sharpened":"云清禾主动离开。","entities":[],"scores":[{"key":v["key"],"score":.8} for v in data["versions"]],"findings":[]}
        elif role=="episode_writer":
            n = data["episode_target"]["episode"]
            script = SCRIPT.replace("# EP01",f"# EP{n:02d}")
            # A local script note deliberately changes a middle line only.
            if data["script_notes"]:
                script = script.replace("林恒：打开它。", "林恒：把锁打开。")
            value = {"episode":n,"script":script,"ledger_out":"云清禾、林恒已离店，妖商留在柜台后。\n","hook_type":"pending_reveal","estimated_seconds":210,"beats":BEATS,"bible_additions":[],"responses":[]}
        elif role=="viewer":
            value = {"keep_watching":True,"reason":"悬念明确。","findings":[]}
        elif role in ("story_check","plan_reviewer"):
            value = {"findings":[]}
        else:
            n = int(re.match(r"ep(\d+)",target)[1])
            key = role+":"+target.replace(f"ep{n:02d}","ep01")
            value = json.loads(serialize(RESPONSES[key]).replace("ep01",f"ep{n:02d}"))
            if role=="storyboard":value["episode"]=n
            if role=="unit_prompt":
                requested = {u["id"] for u in data["units"]}
                value["prompts"] = [p for p in value["prompts"] if p["unit_id"] in requested]
        if self.mutate:
            self.mutate(value,packet,target,self.counts[role+":"+target])
        if "responses" in value and packet.repairs.get("findings"):
            value["responses"] = [{"finding_id":f["id"],"disposition":"fix","reason":"Synthetic corrected response"} for f in packet.repairs["findings"]]
        return {"text":serialize(value),"usage":{"input_tokens":100,"output_tokens":30,"cached_input_tokens":20},"error":None,"exit_code":0,"tool_items":[],"complete":True}


def writer_runner(store, count=2, mutate=None):
    config = configuration(store.root)
    transport = WriterCodex(count,mutate)
    return Runner(store,config=config,client=Client(store,config,codex_transport=transport)), transport


def approve_first(runner):
    report = runner.run(until="A10")
    assert report["waiting"] and not report["paused"],report
    card = next(c for c in runner.cards.list() if c["stage"]=="A8")
    runner.cards.answer(card["id"],"approve")
    return card
