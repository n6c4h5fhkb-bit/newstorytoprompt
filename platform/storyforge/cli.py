from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys

from storyforge import ROOT
from storyforge.config import SflError, configuration, model_card
from storyforge.cards import Cards
from storyforge.delivery import export
from storyforge.llm.codex_cli import doctor
from storyforge.llm.claude_cli import doctor as claude_doctor
from storyforge.refs import edit
from storyforge.runner import Runner, import_episode
from storyforge.runner import service
from storyforge.store import Store


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="sfl", description="StoryForge Lite · text-only production")
    root.add_argument("--projects-dir", type=Path, default=ROOT / "projects")
    root.add_argument("--project", help="Project for card/unit commands; inferred when unambiguous")
    commands = root.add_subparsers(dest="command", required=True)
    p = commands.add_parser("new")
    p.add_argument("project")
    p.add_argument("--novel", type=Path)
    p.add_argument("--model", default="seedance-2.0")
    p = commands.add_parser("demo", help="Create an explicitly labelled replay demonstration")
    p.add_argument("project", nargs="?", default="demo")
    p = commands.add_parser("import")
    p.add_argument("project")
    p.add_argument("--episode", required=True, type=Path)
    p.add_argument("--bible", required=True, type=Path)
    p.add_argument("--ledger-in", required=True, type=Path)
    p.add_argument("--assets", type=Path)
    p = commands.add_parser("run")
    p.add_argument("project")
    p.add_argument("--until", default="B9")
    p.add_argument("--episodes")
    for name in ("status", "cards"):
        p = commands.add_parser(name)
        p.add_argument("project")
    p = commands.add_parser("answer")
    p.add_argument("card_id")
    p.add_argument("option")
    p.add_argument("--note", default="")
    p = commands.add_parser("refs")
    p.add_argument("unit_id")
    p.add_argument("operation", choices=("add", "remove"))
    p.add_argument("placeholder")
    p = commands.add_parser("export")
    p.add_argument("project")
    p.add_argument("episode")
    p = commands.add_parser("feedback")
    p.add_argument("unit_id")
    p.add_argument("result", choices=("ok", "redo"))
    p.add_argument("--note", default="")
    p.add_argument("--generations",type=int,help="Cumulative external generations for this unit")
    p.add_argument("--user-minutes",type=float,help="Cumulative human minutes for this unit")
    p = commands.add_parser("finish",help="Report an externally completed episode")
    p.add_argument("target")
    p.add_argument("--user-minutes",type=float,help="Cumulative human minutes for the whole episode")
    p.add_argument("--note",default="")
    p = commands.add_parser("rerun", help="Regenerate a stage result; with --note, revise the B5 storyboard or a B7 unit prompt per the note")
    p.add_argument("stage")
    p.add_argument("target")
    p.add_argument("--note", default="", help="What to change (B5 target epNN, B7 target epNN_uNN)")
    p = commands.add_parser("revert")
    p.add_argument("target")
    p.add_argument("snapshot")
    p = commands.add_parser("note")
    p.add_argument("target")
    p.add_argument("text")
    p = commands.add_parser("adopt", help="Adopt your own edit of a unit prompt file (prompts/epNN/uNN.md) after it passes the hard checks")
    p.add_argument("unit_id")
    p = commands.add_parser("asset", help="Edit one asset's description, short identity notes or image prompt")
    p.add_argument("placeholder")
    p.add_argument("--description")
    p.add_argument("--identity-notes", dest="identity_notes")
    p.add_argument("--image-prompt", dest="image_prompt")
    p = commands.add_parser("history", help="List snapshots usable with revert")
    p.add_argument("project")
    p.add_argument("target", nargs="?", default="project")
    p.add_argument("--limit", type=int, default=30)
    p = commands.add_parser("dismiss", help="Reject a pending script note (id from notes/script_notes.json), e.g. a reviewer's mistake")
    p.add_argument("note_id")
    p.add_argument("--reason", default="")
    p = commands.add_parser("pause")
    p.add_argument("project")
    p = commands.add_parser("inbox")
    p.add_argument("project")
    p = commands.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    commands.add_parser("doctor")
    return root


def select(args) -> Store:
    project = getattr(args, "project", None)
    if project:
        return Store(service.project_path(project, args.projects_dir))
    candidates = [p for p in args.projects_dir.glob("*") if p.is_dir() and (p / "project.yaml").exists()]
    if hasattr(args, "card_id"):
        candidates = [p for p in candidates if any(c["id"] == args.card_id for c in Cards(Store(p), configuration(p)).list(include_resolved=True))]
    elif hasattr(args, "placeholder") and not hasattr(args, "operation"):
        candidates = [p for p in candidates if any(a["placeholder"] == args.placeholder for a in Store(p).assets())]
    elif hasattr(args, "note_id"):
        candidates = [p for p in candidates if any(n["id"] == args.note_id for n in (Store(p).json("notes/script_notes.json", [])))]
    elif hasattr(args, "unit_id") or hasattr(args, "target"):
        target = getattr(args, "unit_id", None) or args.target
        episode = target.split("_")[0].split(":")[0]
        if episode.startswith("ep"):
            candidates = [p for p in candidates if (p / "episodes" / f"{episode}.md").exists()]
    if len(candidates) != 1:
        raise SflError("Use --project NAME before the command to select an unambiguous project")
    return Store(candidates[0])


def episodes(text: str | None) -> list[int] | None:
    if text is None:
        return None
    try:
        result = set()
        for part in text.split(","):
            if "-" in part:
                first, last = map(int, part.split("-"))
                if last < first:
                    raise ValueError()
                result.update(range(first, last + 1))
            else:
                result.add(int(part))
        if not result or min(result) < 1:
            raise ValueError()
        return sorted(result)
    except ValueError as exc:
        raise SflError("Use --episodes 1-5 or --episodes 1,3,5") from exc


def read_text(path: Path) -> str:
    return sys.stdin.read() if str(path) == "-" else path.read_text(encoding="utf-8-sig")


def dispatch(args):
    command = args.command
    if command == "serve":
        from storyforge.ui import serve
        return serve(args.projects_dir, args.port)
    if command == "doctor":
        config = configuration()
        return {"roles": {tier: doctor(profile) if profile["provider"] == "codex_cli" else claude_doctor(profile) if profile["provider"] == "claude_cli" else {"provider": profile["provider"]}
                          for tier, profile in config["models"].items()}, "token_budget": config["token_budget"]}
    if command in ("new", "demo"):
        store = service.new(args.project, args.projects_dir, novel=getattr(args, "novel", None),
                            model=getattr(args, "model", "seedance-2.0"), demo=command == "demo")
        return {"project": str(store.root), "demo": command == "demo"}
    if command == "history":
        return {"snapshots": service.history(Store(service.project_path(args.project, args.projects_dir)), args.target, args.limit)}
    store = select(args)
    if command == "dismiss":
        return service.dismiss_script_note(store, args.note_id, args.reason)
    if command == "adopt":
        return service.adopt_prompt(store, args.unit_id)
    if command == "asset":
        return service.edit_asset(store, args.placeholder, description=args.description, identity_notes=args.identity_notes, image_prompt=args.image_prompt)
    if command == "import":
        stdin_count = sum(str(p) == "-" for p in (args.episode, args.bible, args.ledger_in, args.assets) if p is not None)
        if stdin_count > 1:
            raise SflError("Only one input can read stdin at a time")
        return {"imported": import_episode(store, read_text(args.episode), read_text(args.bible), read_text(args.ledger_in),
                                           assets_text=read_text(args.assets) if args.assets else None)}
    if command == "run":
        service.resume(store)
        return Runner(store).run(until=args.until, episodes=episodes(args.episodes))
    if command == "status":
        return service.status(store)
    if command == "cards":
        return {"cards": Cards(store, configuration(store.root)).list()}
    if command == "inbox":
        return service.inbox(store)
    if command == "pause":
        return service.pause(store)
    if command == "note":
        return service.note(store, args.target, args.text)
    if command == "answer":
        return Cards(store, configuration(store.root)).answer(args.card_id, args.option, args.note)
    if command == "refs":
        edit(store, args.unit_id, args.operation, args.placeholder)
        return {"edited": args.unit_id}
    if command == "export":
        return {"delivery": str(export(store, args.episode, model_card(configuration(store.root))))}
    if command == "feedback":
        service.feedback(store, args.unit_id, args.result, args.note,generations=args.generations,user_minutes=args.user_minutes)
        return service.metrics(store)
    if command == "finish":
        service.finish_episode(store,args.target,user_minutes=args.user_minutes,note=args.note)
        return service.metrics(store)
    if command == "rerun":
        return service.rerun(store, args.stage, args.target, args.note)
    if command == "revert":
        store.revert(args.target, args.snapshot)
        return {"reverted": args.target, "snapshot": args.snapshot}
    raise SflError("Unknown command")


def main(argv=None) -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if isinstance(result, dict) and (result.get("waiting") or result.get("paused")) else 0
    except (SflError, OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
