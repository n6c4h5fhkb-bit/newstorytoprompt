"""Read-only finished examples, independent of a project's approval state."""
import json

from storyforge import ROOT
from storyforge.checks import rules
from storyforge.delivery.prompts import render, style_document, style_parts, unit_label
from storyforge.refs import mapping_from_assets


def preview(episode: str, config: dict) -> dict | None:
    values = json.loads((ROOT / "tests/fixtures/golden_project/responses/responses.json").read_text(encoding="utf-8"))
    board = values.get("storyboard:" + episode)
    style = values.get("art_director:" + episode)
    extracted = values.get("asset_extract:" + episode)
    if not board or not style or not extracted:
        return None
    assets = []
    for asset in extracted["assets"]:
        asset_id = rules()["asset_id_prefixes"][asset["type"]] + ":" + asset["name"] + ("@" + asset["variant"] if asset["variant"] else "")
        assets.append({"id": asset_id, **{k: asset[k] for k in ("type", "name", "parent", "what_changed", "placeholder", "description", "identity_notes")},
                       "image_prompt": values[f"asset_prompt:{episode}:asset:{asset_id}"]["brief"], "status": "described"})
    units = []
    for unit in board["units"]:
        refs = mapping_from_assets(assets, unit, config)
        fragment = next(p for p in values[f"unit_prompt:{episode}:{unit['scene']}"]["prompts"] if p["unit_id"] == unit["id"])
        label = unit_label(unit, board["units"])
        units.append({**unit, "label": label, "references": refs,
                      "prompt": render(fragment, refs, style["style_lock"], config, unit, label=label)})
    return {"episode": episode, "kind": "fixed_example", "art_direction": style_parts(style_document(style)),
            "assets": assets, "units": units}
