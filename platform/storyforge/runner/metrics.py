"""Reported production measurements; unknown values stay unknown."""
from datetime import datetime, timezone
import re


def real_units(store):
    result = {}
    for path in store.path("delivery").glob("ep*/manifest.json"):
        manifest = store.json(path.relative_to(store.root).as_posix())
        if manifest.get("demo") is False:
            result[manifest["episode"]] = {u["id"] for u in manifest["units"]}
    return result


def reported(rows):
    values = {}
    for row in rows:
        if row.get("demo") or row.get("result") not in ("ok","redo"):
            continue
        value = values.setdefault(row["unit"],{})
        value.update(result=row["result"],time=row["time"],prompt_fingerprint=row.get("prompt_fingerprint"))
        for field in ("generations","user_minutes"):
            if row.get(field) is not None:value[field] = row[field]
    return values


def report_metrics(expected, values):
    known = expected or set(values)
    generations = {unit:values[unit]["generations"] for unit in known if unit in values and "generations" in values[unit]}
    minutes = {unit:values[unit]["user_minutes"] for unit in known if unit in values and "user_minutes" in values[unit]}
    return {"expected_units":len(expected),"generations_reported_units":len(generations),"user_minutes_reported_units":len(minutes),
        "reported_generations_per_unit":sum(generations.values())/len(generations) if generations else None,
        "generations_per_unit":sum(generations.values())/len(expected) if expected and set(generations)==expected else None,
        "reported_user_minutes":sum(minutes.values()) if minutes else None,
        "user_minutes":sum(minutes.values()) if expected and set(minutes)==expected else None}


def moment(text):
    value = datetime.fromisoformat(text.replace("Z","+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def production_metrics(store):
    expected = real_units(store)
    feedback = store.logs("feedback")
    values = reported(feedback)
    finished = {}
    for row in feedback:
        if row.get("demo") or row.get("result")!="finished":continue
        value = finished.setdefault(row["episode"],{"finished_at":row["time"]})
        if row.get("user_minutes") is not None:value["user_minutes"] = row["user_minutes"]
    events = store.logs("decisions")
    cards, shared = {},0
    imports, deliveries = [],{}
    for row in events:
        if row.get("event")=="card_opened":
            card = row["card"]
            episode = card.get("details",{}).get("episode")
            match = re.match(r"ep\d+(?=[:_]|$)",card["target"])
            episode = episode or (match[0] if match else None)
            if episode:cards[episode] = cards.get(episode,0)+1
            else:shared += 1
        if row.get("demo") is False and row.get("stage")=="import" and row.get("choice")=="adopt":
            imports.append(row["time"])
        if row.get("demo") is False and row.get("event")=="stage_completed" and row.get("stage")=="B9":
            target = row["target"]
            if target not in deliveries or moment(row["time"])<moment(deliveries[target]):deliveries[target] = row["time"]
    episodes = set(expected) | {unit.split("_")[0] for unit in values} | set(cards) | set(deliveries) | set(finished)
    per_episode = {episode:{**report_metrics(expected.get(episode,set()),{u:v for u,v in values.items() if u.startswith(episode+"_")}),
        "cards_created":cards.get(episode,0),"first_delivered_at":deliveries.get(episode)} for episode in sorted(episodes)}
    for episode,value in finished.items():
        measurement = per_episode[episode]
        measurement["finished_at"] = value["finished_at"]
        # Whole-episode totals include shared preparation/editing and take
        # precedence over unit totals; the two are never added together.
        measurement["finished_user_minutes"] = value.get("user_minutes",measurement["user_minutes"])
    imported_at = min(imports,key=moment) if imports else None
    delivered_at = min(deliveries.values(),key=moment) if deliveries else None
    days = (moment(delivered_at)-moment(imported_at)).total_seconds()/86400 if imported_at and delivered_at else None
    finished_at = min((r["finished_at"] for r in finished.values()),key=moment,default=None)
    finish_days = (moment(finished_at)-moment(imported_at)).total_seconds()/86400 if imported_at and finished_at else None
    finished_minutes = [m["finished_user_minutes"] for m in per_episode.values() if m.get("finished_user_minutes") is not None]
    all_units = set().union(*expected.values()) if expected else set()
    return {**report_metrics(all_units,values),"per_episode":per_episode,"cards_shared":shared,
        "imported_at":imported_at,"first_delivered_at":delivered_at,"days_to_first_delivery":days if days is not None and days>=0 else None,
        "finished_episodes":len(finished),"finished_user_minutes_reported_episodes":len(finished_minutes),
        "user_minutes_per_finished_episode":sum(finished_minutes)/len(finished) if finished and len(finished_minutes)==len(finished) else None,
        "first_finished_at":finished_at,"days_to_first_finished_episode":finish_days if finish_days is not None and finish_days>=0 else None,
        "reported_unit_details":values}


def review_signal(store, *, minimum_units: int = 5):
    """Does an open text-review note predict a redo? The kill rule needs this before B8 stays a gate."""
    flagged = {}
    for path in store.path("delivery").glob("ep*/manifest.json"):
        manifest = store.json(path.relative_to(store.root).as_posix())
        if manifest.get("demo") is False:
            for unit in manifest["units"]:
                flagged[unit["id"]] = bool(unit.get("open_review_notes"))
    first = {}
    for row in store.logs("feedback"):
        if not row.get("demo") and row.get("result") in ("ok", "redo"):
            first.setdefault(row["unit"], row)
    groups = {"with_open_notes": {"units": 0, "redo": 0}, "without_open_notes": {"units": 0, "redo": 0}}
    for unit, row in first.items():
        if unit in flagged:
            group = groups["with_open_notes" if flagged[unit] else "without_open_notes"]
            group["units"] += 1
            group["redo"] += row["result"] == "redo"
    reasons = {}
    for row in first.values():
        for reason in row.get("reasons") or []:
            reasons[reason] = reasons.get(reason, 0) + 1
    rate = lambda g: g["redo"] / g["units"] if g["units"] else None
    with_notes, without = rate(groups["with_open_notes"]), rate(groups["without_open_notes"])
    if min(groups["with_open_notes"]["units"], groups["without_open_notes"]["units"]) < minimum_units:
        verdict = "not_enough_data"
    elif with_notes - without < 0.1:
        verdict = "reviews_do_not_predict_redo"
    else:
        verdict = "reviews_predict_redo"
    return {"groups": groups, "redo_rate_with_open_notes": with_notes, "redo_rate_without": without, "redo_reasons": reasons, "verdict": verdict}
