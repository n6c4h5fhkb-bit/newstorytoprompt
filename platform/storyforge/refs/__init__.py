from __future__ import annotations
from storyforge.config import SflError
from storyforge.checks import rules
from storyforge.store import Store


class MissingAssets(SflError):
    def __init__(self, ids: list[str]):
        super().__init__("Missing references: " + ", ".join(ids))
        self.ids = ids


def mapping(store: Store, unit: dict, config: dict) -> list[dict]:
    return mapping_from_assets(store.assets(), unit, config,
        store.json(f".state/ref_edits/{unit['id']}.json", {"add": [], "remove": []}))


def mapping_from_assets(all_assets: list[dict], unit: dict, config: dict, edits=None) -> list[dict]:
    by_id = {a["id"]: a for a in all_assets}
    selected = [by_id[a] for a in unit["assets"] if a in by_id]
    missing = [a for a in unit["assets"] if a not in by_id]
    palette = next((a for a in all_assets if a["placeholder"] == rules()["palette_placeholder"]), None)
    if palette:
        selected.append(palette)
    else:
        missing.append("layout:色卡")
    speakers = {d["who"] for s in unit["shots"] for d in s["dialogue"]}
    for speaker in sorted(speakers):
        voice = next((a for a in all_assets if a["type"] == "voice" and a["name"] == speaker), None)
        if voice:
            selected.append(voice)
        else:
            missing.append("voice:" + speaker)
    music = config["output"]["music"]
    if music == "cue":
        placeholder = config["output"].get("music_placeholder")
        if not placeholder:
            raise SflError("Set output.music_placeholder when music=cue")
        asset = next((a for a in all_assets if a["placeholder"] == placeholder and a["type"] == "music"), None)
        if asset:
            selected.append(asset)
        else:
            missing.append(str(placeholder))
    elif music != "none":
        raise SflError("Music must be none or cue")
    if missing:
        raise MissingAssets(missing)
    edits = edits or {"add": [], "remove": []}
    added, removed = set(edits["add"]), set(edits["remove"])
    tail_placeholder = rules()["continuation_placeholder"]
    by_placeholder = {a["placeholder"]: a for a in all_assets}
    for placeholder in edits["add"]:
        if placeholder == tail_placeholder:
            continue
        if placeholder not in by_placeholder:
            raise MissingAssets([placeholder])
        selected.append(by_placeholder[placeholder])
    exclusions = {e["placeholder"] for e in unit.get("reference_exclusions", []) if e["reason"].strip()}
    excluded = removed | (exclusions - added)
    selected = list({a["id"]: a for a in selected if a["placeholder"] not in excluded}.values())
    roots = [a.get("parent") or a["id"] for a in selected]
    if len(roots) != len(set(roots)):
        raise SflError("Reference edit selects both master and child; remove one explicitly")
    on_screen = {name for shot in unit["shots"] for name in shot["on_screen"]}
    result = []
    for row in selected:
        position = "画内" if row["type"] == "character" and row["name"] in on_screen else "画外，身份参考" if row["type"] == "character" and row["name"] in unit["offscreen"] else "参考"
        result.append({"asset_id": row["id"], "placeholder": row["placeholder"], "type": row["type"], "name": row["name"],
                       "description": row["description"], "position": position, "status": row["status"],
                       "category": rules()["reference_categories"][row["type"]], "asset_prompt": row["image_prompt"]})
    if (unit["chain_from_previous"] or tail_placeholder in added) and tail_placeholder not in excluded:
        result.append({"asset_id": "external:previous_frame", "placeholder": tail_placeholder, "type": "layout", "name": "上段尾帧",
                       "description": unit["carry_in"], "position": "用户保留的上一段视频尾帧，连续性参考", "status": "approved",
                       "category": "images", "asset_prompt": "用户在自己的工具里选取实际尾帧"})
    return result


def edit(store: Store, unit_id: str, operation: str, placeholder: str):
    if operation not in ("add", "remove"):
        raise SflError("Reference operation must be add or remove")
    episode = unit_id.split("_")[0]
    board = store.json(f"storyboard/{episode}.json")
    if not board or not any(u["id"] == unit_id for u in board["units"]):
        raise SflError("Unknown storyboard unit")
    if placeholder not in {a["placeholder"] for a in store.assets()} | {rules()["continuation_placeholder"]}:
        raise SflError("Unknown asset placeholder")
    path = f".state/ref_edits/{unit_id}.json"
    changes = store.json(path, {"add": [], "remove": []})
    opposite = "remove" if operation == "add" else "add"
    changes[opposite] = [p for p in changes[opposite] if p != placeholder]
    if placeholder not in changes[operation]:
        changes[operation].append(placeholder)
    store.write(path, changes, json_data=True)
    store.append("decisions", {"stage": "B6", "target": unit_id, "choice": operation, "placeholder": placeholder, "by": "user"})
    store.stale_artifacts()
    store.snapshot(f"references {unit_id}")
