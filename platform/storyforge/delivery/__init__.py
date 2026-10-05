from __future__ import annotations
import json
import os
import io
from pathlib import Path
import re
import zipfile

from storyforge import ROOT
from storyforge.config import SflError, configuration
from storyforge.checks import prompt as check_prompt, references as check_references, warnings
from storyforge.store import Store, digest, serialize


def table_cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", "<br>")


def asset_sheet(store: Store, references: list[dict]) -> list[dict]:
    """Include preparation masters without changing any unit's references."""
    by_id = {a["id"]: a for a in store.assets()}
    used = {r["asset_id"]: r for r in references}
    ordered = {}

    def add(asset):
        if asset["status"] != "approved" or not asset["description"].strip() or not asset["image_prompt"].strip():
            raise SflError(f"Asset preparation is incomplete: {asset['id']}")
        ordered.setdefault(asset["id"], {**asset, "asset_id": asset["id"], "asset_prompt": asset["image_prompt"],
            "preparation_only": asset["id"] not in used,
            "parent_placeholder": by_id[asset["parent"]]["placeholder"] if asset["parent"] else ""})

    for asset_id, reference in used.items():
        if asset_id == "external:previous_frame":
            ordered[asset_id] = {**reference, "preparation_only": False, "parent_placeholder": ""}
            continue
        asset = by_id.get(asset_id)
        if asset is None:
            raise SflError(f"Missing delivery asset: {asset_id}")
        if any(reference[key] != asset[source] for key, source in (
                ("placeholder", "placeholder"), ("description", "description"), ("asset_prompt", "image_prompt"),
                ("status", "status"), ("type", "type"), ("name", "name"))):
            raise SflError(f"Reference asset changed; update its mapping: {asset_id}")
        if asset["parent"]:
            parent = by_id.get(asset["parent"])
            if not parent or parent["parent"] or (parent["type"], parent["name"]) != (asset["type"], asset["name"]):
                raise SflError(f"Invalid preparation master for {asset_id}: {asset['parent']}")
            add(parent)
        add(asset)
    return list(ordered.values())


def package_files(store: Store, episode: str) -> list[Path]:
    if not re.fullmatch(r"ep\d{2,}", episode):
        raise SflError("Delivery episode must use epNN")
    prefix = f"delivery/{episode}/"
    with store.locked():
        store.stale_artifacts()
        if not store.current(prefix + "manifest.json"):
            raise SflError("Delivery is stale or incomplete; rerun first")
        manifest = store.json(prefix + "manifest.json")
        if not isinstance(manifest, dict):
            raise SflError("Delivery manifest must be an object")
        units = manifest.get("units")
        if manifest.get("episode") != episode or not isinstance(units, list) or not units:
            raise SflError("Delivery manifest has no valid episode units")
        names = ["manifest.json", "assets.md", "overlays.md"]
        for unit in units:
            if not isinstance(unit, dict):
                raise SflError("Delivery manifest has an invalid unit")
            uid = unit.get("id", "")
            name = unit.get("prompt_file", "")
            if not isinstance(uid, str) or not re.fullmatch(rf"{episode}_u\d{{2,}}", uid) or name != uid.split("_")[1] + ".md" or name in names:
                raise SflError("Delivery manifest has an invalid or repeated unit file")
            names.append(name)
        if not all(store.current(prefix + name) for name in names):
            raise SflError("Delivery is stale or incomplete; rerun first")
        return [store.path(prefix + name) for name in names]


def ready(store: Store, episode: str) -> bool:
    try:
        package_files(store, episode)
        return True
    except (SflError, OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def package(store: Store, episode: str) -> bytes:
    # Hold the project lock through validation and reads so a simultaneous
    # export cannot combine different delivery versions in one archive.
    with store.locked():
        files = package_files(store, episode)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                archive.writestr(file.name, file.read_bytes())
        return buffer.getvalue()


def outputs(store: Store, episode: str, card: dict, *, config: dict | None = None) -> dict[str, str]:
    board = store.json(f"storyboard/{episode}.json")
    if not board or not store.current(f"storyboard/{episode}.json"):
        raise SflError("A current, checked storyboard is required for export")
    mappings = store.json(f"refs/{episode}.json", {})
    episode_warnings = [w for w in warnings(board, config or configuration(store.root)) if w["target"] == episode]
    episode_warnings += store.json(f".state/script_reviews/{episode}.json",{}).get("warnings",[])
    result, used, overlays, manifests, all_warnings = {}, {}, [], [], episode_warnings
    for unit in board["units"]:
        uid, short = unit["id"], unit["id"].split("_")[1]
        mapping = mappings.get(uid)
        if mapping is None:
            raise SflError(f"Missing reference mapping for {uid}")
        prompt_path = f"prompts/{episode}/{short}.md"
        text = store.text(prompt_path)
        review_path = f".state/reviews/{uid}.json"
        review = store.json(review_path)
        if not store.current(prompt_path) or not store.current(review_path) or not review or not review.get("passed") or review["prompt_fingerprint"] != digest(text):
            raise SflError(f"{uid}: export requires current text-only reconstruction approval")
        errors = check_references(mapping, card) + check_prompt(text, mapping)
        if errors:
            raise SflError("; ".join(errors))
        header = f"# {episode.upper()} · {short.upper()} · {unit['seconds']}s · 16:9\n\n## 参考映射\n\n"
        table = "| 占位符 | 职责 | 描述 | 位置/用途 |\n| --- | --- | --- | --- |\n"
        table += "".join("| " + " | ".join(table_cell(r[k]) for k in ("placeholder", "type", "description", "position")) + " |\n" for r in mapping)
        result[f"delivery/{episode}/{short}.md"] = header + table + "\n## 提示词\n\n" + text
        used.update({r["placeholder"]: r for r in mapping})
        for shot in unit["shots"]:
            for overlay in shot["overlays"]:
                overlays.append({"unit": uid, **overlay})
        all_warnings.extend(review.get("warnings", []))
        manifests.append({"id": uid, "seconds": unit["seconds"], "prompt_file": short + ".md", "prompt_fingerprint": digest(text),
                          "mapping": mapping, "warnings": review.get("warnings", []), "review": "passed"})
    assets = "# 资产与美术提示词\n\n" + store.text("style.md") + "\n\n# 资产清单\n\n"
    assets += "按下列顺序准备资产。先制作母资产，再基于母资产制作子资产；每段视频使用的参考以对应单元映射为准。\n\n"
    for row in asset_sheet(store, list(used.values())):
        assets += f"## {row['placeholder']}\n\n- 类型：{row['type']}\n- 描述：{row['description']}\n"
        if row["preparation_only"]:
            assets += "- 用途：供子资产制作使用的母资产\n"
        if row["parent_placeholder"]:
            assets += f"- 母资产：{row['parent_placeholder']}\n- 变化：{row['what_changed']}\n"
        assets += f"\n{row['asset_prompt']}\n\n"
    overlay_text = "# 后期文字清单\n\n| 单元 | 开始（秒） | 结束（秒） | 文字 |\n| --- | --- | --- | --- |\n"
    overlay_text += "".join("| " + " | ".join(table_cell(o[k]) for k in ("unit", "start", "end", "text")) + " |\n" for o in overlays)
    result[f"delivery/{episode}/assets.md"] = assets
    result[f"delivery/{episode}/overlays.md"] = overlay_text
    result[f"delivery/{episode}/manifest.json"] = serialize({"episode": episode, "ratio": "16:9", "text_only": True,
        "demo": bool(configuration(store.root).get("demo") or store.json(".state/demo.json", {}).get("demo")), "units": manifests, "warnings": all_warnings}) + "\n"
    return result


def export(store: Store, episode: str, card: dict, *, config: dict | None = None):
    with store.locked():
        return _export(store, episode, card, config=config)


def _export(store: Store, episode: str, card: dict, *, config: dict | None = None):
    if not re.fullmatch(r"ep\d{2,}", episode):
        raise SflError("Export episode must use epNN")
    config = config or configuration(store.root)
    store.stale_artifacts()
    bindings = [store.binding(f"storyboard/{episode}.json"), store.binding(f"refs/{episode}.json"), store.binding("style.md")]
    if store.path(f".state/script_reviews/{episode}.json").exists():
        bindings.append(store.binding(f".state/script_reviews/{episode}.json"))
    configuration_path = Path(os.environ.get("SFL_CONFIG", ROOT / "config.yaml")).resolve()
    bindings.append(store.binding("external:" + str(configuration_path), style_reference=True))
    bindings.append(store.binding("project.yaml", {"kind":"yaml_keys", "keys":["episode_minutes", "speech_rate_chars_per_sec", "warnings"]}, style_reference=True))
    board = store.json(f"storyboard/{episode}.json")
    if not board:
        raise SflError("A current, checked storyboard is required for export")
    mappings = store.json(f"refs/{episode}.json", {})
    sheet = asset_sheet(store, [r for unit in board["units"] for r in mappings.get(unit["id"], [])])
    bindings.append(store.binding("assets.csv", {"kind": "assets",
        "ids": sorted(a["asset_id"] for a in sheet if not a["asset_id"].startswith("external:"))}))
    for unit in board["units"]:
        bindings.extend([store.binding(f"prompts/{episode}/{unit['id'].split('_')[1]}.md"), store.binding(f".state/reviews/{unit['id']}.json")])
    job = store.start_job("B9", episode, bindings)
    try:
        data = outputs(store, episode, card, config=config)
    except Exception as exc:
        store.end_job(job, "paused", str(exc))
        raise
    def scope(path, all_bindings):
        # Preparation masters are read only by the shared asset sheet. Unit
        # deliveries retain the narrower asset bindings of their own mappings.
        if path != f"delivery/{episode}/assets.md":
            all_bindings = [b for b in all_bindings if b["path"] != "assets.csv"]
        match = re.fullmatch(rf"delivery/{episode}/(u\d+)\.md", path)
        if not match:
            return all_bindings
        uid, short = episode + "_" + match[1], match[1]
        return [b for b in all_bindings if not b["path"].startswith((f"prompts/{episode}/", ".state/reviews/", f"refs/{episode}"))
                and b["path"] != f"storyboard/{episode}.json"] + [
                    store.binding(f"storyboard/{episode}.json", {"kind": "unit", "id": uid}),
                    store.binding(f"refs/units/{uid}.json"), store.binding(f"prompts/{episode}/{short}.md"),
                    store.binding(f".state/reviews/{uid}.json")]
    obsolete = [path for path, meta in store.json(".state/artifacts.json", {}).items()
                if path.startswith(f"delivery/{episode}/") and meta["stage"] == "B9" and meta["target"] == episode and path not in data]
    if not store.accept(job, "B9", episode, bindings, data, output_scope=scope, removals=obsolete,
                        metadata={"demo":json.loads(data[f"delivery/{episode}/manifest.json"])["demo"]}):
        raise SflError("Inputs changed while exporting")
    return store.path(f"delivery/{episode}")
