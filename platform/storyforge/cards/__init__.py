from __future__ import annotations
import json

from storyforge.config import SflError
from storyforge.store import Store, digest, now, serialize


class Cards:
    def __init__(self, store: Store, config: dict):
        self.store, self.config = store, config
        self.restore()

    def restore(self):
        events = self.store.logs("decisions")
        with self.store.db() as conn:
            for event in events:
                if event.get("event") == "card_opened":
                    card = event["card"]
                    conn.execute("INSERT OR IGNORE INTO cards(id,dedupe,phase,target,status,payload,created) VALUES(?,?,?,?,?,?,?)",
                        (card["id"], card["dedupe"], card["phase"], card["target"], card["status"], serialize(card), event["time"]))
                elif event.get("event") == "card_answered":
                    conn.execute("UPDATE cards SET status='resolved',answer=? WHERE id=?", (serialize(event), event["card_id"]))
        self.promote()

    def promote(self):
        with self.store.db() as conn:
            phases = [r[0] for r in conn.execute("SELECT DISTINCT phase FROM cards WHERE status != 'resolved'")]
            for phase in phases:
                open_count = conn.execute("SELECT COUNT(*) FROM cards WHERE phase=? AND status='open'", (phase,)).fetchone()[0]
                capacity = self.config["card"]["max_open_per_phase"] - open_count
                queued = conn.execute("SELECT id FROM cards WHERE phase=? AND status='queued' ORDER BY created,id LIMIT ?", (phase, max(capacity, 0))).fetchall()
                for row in queued:
                    conn.execute("UPDATE cards SET status='open' WHERE id=?", (row["id"],))

    def create(self, *, kind: str, stage: str, target: str, question: str, options: list[dict],
               recommended: str, reason: str, dedupe: str, blocks: list[str] | None = None,
               details: dict | None = None) -> dict:
        with self.store.locked():
            return self._create(kind=kind,stage=stage,target=target,question=question,options=options,recommended=recommended,reason=reason,dedupe=dedupe,blocks=blocks,details=details)

    def _create(self, *, kind, stage, target, question, options, recommended, reason, dedupe, blocks, details):
        if not 2 <= len(options) <= 3 or recommended not in [o["key"] for o in options]:
            raise SflError("Card needs 2–3 options and a valid recommendation")
        existing = self.find(dedupe)
        if existing:
            return existing
        phase = stage[0]
        open_count = sum(c["phase"] == phase and c["status"] == "open" for c in self.list())
        card = {"id": "c_" + digest(dedupe)[:12], "kind": kind, "stage": stage, "phase": phase,
                "target": target, "question": question, "options": options, "recommended": recommended,
                "reason": reason, "dedupe": dedupe, "blocks": blocks or [target], "details": details or {},
                "status": "open" if open_count < self.config["card"]["max_open_per_phase"] else "queued"}
        # The file event precedes the database projection; restore is idempotent.
        self.store.append("decisions", {"event": "card_opened", "card_id": card["id"], "stage": stage,
                           "target": target, "card": card, "by": "model", "choice": None})
        with self.store.db() as conn:
            conn.execute("INSERT OR IGNORE INTO cards(id,dedupe,phase,target,status,payload,created) VALUES(?,?,?,?,?,?,?)",
                (card["id"], dedupe, phase, target, card["status"], serialize(card), now()))
        return card

    def list(self, *, include_resolved: bool = False) -> list[dict]:
        with self.store.db() as conn:
            rows = conn.execute("SELECT * FROM cards " + ("" if include_resolved else "WHERE status!='resolved' ") + "ORDER BY created,id").fetchall()
        result = []
        for row in rows:
            card = json.loads(row["payload"])
            card["status"] = row["status"]
            card["answer"] = json.loads(row["answer"]) if row["answer"] else None
            result.append(card)
        return result

    def find(self, dedupe: str) -> dict | None:
        return next((c for c in self.list(include_resolved=True) if c["dedupe"] == dedupe), None)

    def answer(self, card_id: str, option: str, note: str = "") -> dict:
        with self.store.locked():
            return self._answer(card_id,option,note)

    def _answer(self, card_id, option, note):
        card = next((c for c in self.list(include_resolved=True) if c["id"] == card_id), None)
        if not card:
            raise SflError("Card does not exist in this project")
        if card["status"] == "resolved":
            raise SflError("Card is already answered; history is append-only")
        if card["status"] == "queued":
            raise SflError("Answer the visible cards before this queued card")
        if option not in [o["key"] for o in card["options"]]:
            raise SflError("Invalid card option")
        if card["kind"] == "budget" and option == "raise":
            try:
                value = int(note)
            except ValueError as exc:
                raise SflError("Put the new token budget in --note, e.g. --note 200000") from exc
            used = card["details"]["used"]
            if value <= used:
                raise SflError("New budget must exceed tokens already used")
        event = {"time": now(), "event": "card_answered", "card_id": card_id, "stage": card["stage"], "target": card["target"],
                 "choice": option, "by": "user", "note": note, "options": card["options"]}
        self.store.append("decisions", event)
        with self.store.db() as conn:
            conn.execute("UPDATE cards SET status='resolved',answer=? WHERE id=?", (serialize(event), card_id))
        self.promote()
        return {**card, "status": "resolved", "answer": event}

    def resolution(self, stage: str, target: str, *, kind: str = "stuck") -> dict | None:
        matching = [c for c in self.list(include_resolved=True) if c["kind"] == kind and c["stage"] == stage
                    and c["target"] == target and c["answer"]]
        return matching[-1] if matching else None

    def blocking(self, stage: str, target: str) -> list[dict]:
        def matches(block: str) -> bool:
            return block == "project" or target == block or target.startswith(block + "_") or target.startswith(block + ":")
        return [c for c in self.list() if c["phase"] == stage[0] and any(matches(b) for b in c["blocks"])
                and (c["kind"] == "budget" or int(stage[1:]) >= int(c["stage"][1:]))]

    def model_choice(self, stage: str, target: str, options: list[dict], choice: str, reason: str,
                     *, expensive: bool, taste: bool):
        ranked = sorted(options, key=lambda o: o["score"], reverse=True)
        close = len(ranked) > 1 and ranked[0]["score"] - ranked[1]["score"] <= self.config["card"]["score_margin"]
        if close and expensive and taste:
            return self.create(kind="creative", stage=stage, target=target, question=reason,
                options=options, recommended=choice, reason=reason, dedupe=f"choice:{stage}:{target}:{digest(options)}")
        self.store.append("decisions", {"stage": stage, "target": target, "options": options, "choice": choice,
                                      "by": "model", "reason": reason})
        return None
