from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid

import yaml

from storyforge.config import SflError
from storyforge.checks.parsers import bible_slice

ASSET_COLUMNS = ["id", "type", "name", "parent", "what_changed", "image_prompt", "placeholder", "status", "description", "identity_notes"]
CREATIVE_PATHS = ("source", "summaries", "bible.md", "style.md", "breakdown.md", "breakdown.json", "plan.md", "plan.json", "amplified", "ledger", "episodes", "storyboard", "assets.csv", "refs", "prompts", "delivery", "notes", "findings", ".state/examples", ".state/script_reviews", ".state/direction.json", ".state/locks.json")
_mutexes: dict[str, threading.RLock] = {}
_mutex_guard = threading.Lock()
_lock_state = threading.local()


def resolved_path(path: Path) -> Path:
    resolved = path.resolve()
    if os.name=="nt":
        text = str(resolved)
        # Windows realpath can return the device prefix while another thread
        # creates a missing parent. Normalize both sides of containment checks.
        if text.startswith("\\\\?\\UNC\\"):return Path("\\\\"+text[8:])
        if text.startswith("\\\\?\\"):return Path(text[4:])
    return resolved


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def serialize(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def digest(value) -> str:
    data = value if isinstance(value, bytes) else serialize(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def select_text(text: str, selector: dict):
    kind = selector.get("kind")
    if kind=="bible":
        return bible_slice(text,selector["names"],selector["locations"])
    if kind=="assets":
        rows = [a for a in csv.DictReader(io.StringIO(text)) if a["id"] in selector["ids"]]
        fields = selector.get("fields")
        return [{key:row.get(key,"") for key in fields} for row in rows] if fields else rows
    if kind=="yaml_keys":
        value = yaml.safe_load(text)
        return {key:value.get(key) for key in selector["keys"]}
    if kind in ("script_hooks","last_scene"):
        from storyforge.checks.parsers import parse_script
        script = parse_script(text)
        return {"hook":script.hook,"cliffhanger":script.cliffhanger} if kind=="script_hooks" else script.scenes[-1].text
    if kind in ("unit","unit_carry","key","passages","episode_plan","moment","moment_beats","beats","script_notes","continuity"):
        value = json.loads(text)
        if kind=="unit_carry":return next((u["carry_out"] for u in value["units"] if u["id"]==selector["id"]),None)
        if kind=="unit":return next((u for u in value["units"] if u["id"]==selector["id"]),None)
        if kind=="key":return value.get(selector["key"])
        if kind=="passages":return [value["paragraphs"][r] for r in selector["refs"] if r in value["paragraphs"]]
        if kind=="episode_plan":return next((e for e in value["episodes"] if e["number"]==selector["episode"]),None)
        if kind=="moment":return next((m for m in value["moments"] if m["id"]==selector["id"]),None)
        if kind=="moment_beats":
            moment = next(m for m in value["moments"] if m["id"]==selector["id"])
            ids = set(moment["beat_ids"])
            ids.update(d["beat_id"] for d in value["decisions"] if d["action"]=="merge" and d["merge_into"] in moment["beat_ids"])
            return sorted(ids)
        if kind=="beats":return [b for b in value["beats"] if b["id"] in selector["ids"]]
        if kind=="script_notes":return [{key:row[key] for key in ("id","time","target","note","by","origin") if key in row} for row in value if row["target"]==selector["episode"] and row.get("status")!="dismissed"]
        if kind=="continuity":return [entry for target,entries in value.items() for entry in entries if int(target.split("_")[0][2:])<=selector["episode"]]
    return text


def atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".sfl-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()


@contextmanager
def file_lock(path: Path, timeout: float = 15):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        started = time.monotonic()
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (OSError, BlockingIOError):
                if time.monotonic() - started >= timeout:
                    raise SflError("Project is busy; another process owns this operation")
                time.sleep(0.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, root: Path):
        self.root = resolved_path(root)
        if not (self.root / "project.yaml").exists():
            raise SflError(f"Not a StoryForge project: {self.root}")
        self.runtime = self.root / ".runtime"
        self.runtime.mkdir(exist_ok=True)
        with _mutex_guard:
            self.mutex = _mutexes.setdefault(str(self.root), threading.RLock())
        with self.db() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, stage TEXT NOT NULL, target TEXT NOT NULL,
                    status TEXT NOT NULL, started TEXT, ended TEXT,
                    inputs TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_target ON jobs(stage, target, status);
                CREATE TABLE IF NOT EXISTS cards (
                    id TEXT PRIMARY KEY, dedupe TEXT UNIQUE NOT NULL,
                    phase TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, created TEXT NOT NULL, answer TEXT
                );
            """)
        self.backup_daily()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.runtime / "jobs.sqlite", timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def locked(self):
        with self.mutex:
            held = getattr(_lock_state, "held", set())
            key = str(self.root)
            if key in held:
                yield
            else:
                with file_lock(self.runtime / "write.lock"):
                    _lock_state.held = held | {key}
                    try:
                        yield
                    finally:
                        _lock_state.held = held

    @contextmanager
    def run_lease(self):
        with file_lock(self.runtime / "run.lock", timeout=0):
            self.recover_transactions()
            with self.db() as conn:
                conn.execute("UPDATE jobs SET status='pending', error='Recovered after interrupted run' WHERE status='running'")
            yield

    def path(self, relative: str) -> Path:
        path = resolved_path(self.root / relative)
        if not path.is_relative_to(self.root) or path == self.root:
            raise SflError(f"Path outside project: {relative} resolves to {path}; project is {self.root}")
        return path

    def text(self, relative: str, default: str | None = None) -> str:
        with self.locked():
            path = self.path(relative)
            if not path.exists():
                if default is not None:
                    return default
                raise SflError(f"Missing project file: {relative}")
            return path.read_text(encoding="utf-8-sig")

    def json(self, relative: str, default=None):
        with self.locked():
            path = self.path(relative)
            return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else default

    def write(self, relative: str, value, *, json_data: bool = False):
        with self.locked():
            atomic_write(self.path(relative), serialize(value) + "\n" if json_data else str(value))

    def assets(self) -> list[dict]:
        return list(csv.DictReader(io.StringIO(self.text("assets.csv", ""))))

    @staticmethod
    def assets_text(rows: list[dict]) -> str:
        output = io.StringIO(newline="")
        writer = csv.DictWriter(output, fieldnames=ASSET_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: row.get(k, "") for k in ASSET_COLUMNS} for row in rows)
        return output.getvalue()

    def logs(self, name: str) -> list[dict]:
        if name not in ("calls", "decisions", "feedback"):
            raise SflError("Unknown log")
        text = self.text(f"logs/{name}.jsonl", "")
        rows = []
        for index, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except ValueError as exc:
                    raise SflError(f"Invalid append-only log {name}, line {index}; preserve and repair it") from exc
        return rows

    def append(self, name: str, record: dict, *, unique_id: str | None = None):
        with self.locked():
            if unique_id and any(r.get("id") == unique_id for r in self.logs(name)):
                return
            record = {"time": now(), **record}
            path = self.path(f"logs/{name}.jsonl")
            path.parent.mkdir(exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())

    def binding(self, relative: str, selector: dict | None = None, *, style_reference: bool = False) -> dict:
        binding = {"path": relative, "selector": selector or {}, "style_reference": style_reference}
        binding["fingerprint"] = self.fingerprint(binding)
        return binding

    def selected(self, binding: dict):
        path = binding["path"]
        if path.startswith("external:"):
            source = Path(path[9:])
            return source.read_text(encoding="utf-8-sig") if source.exists() else None
        selector = binding.get("selector", {})
        kind = selector.get("kind")
        if not self.path(path).exists():
            return [] if kind in ("script_notes", "continuity") else None
        if kind == "decision":
            matching = [r for r in self.logs("decisions") if r.get("event") == "card_answered" and r.get("card_id") == selector["card_id"]]
            return matching[-1] if matching else None
        return select_text(self.text(path),selector)

    def fingerprint(self, binding: dict) -> str:
        return digest(self.selected(binding))

    def unchanged(self, bindings: list[dict], *, include_style: bool = True) -> bool:
        return all(self.fingerprint(b) == b["fingerprint"] for b in bindings if include_style or not b.get("style_reference"))

    def start_job(self, stage: str, target: str, bindings: list[dict]) -> str:
        job_id = "j_" + uuid.uuid4().hex
        with self.db() as conn:
            conn.execute("INSERT INTO jobs(id, stage, target, status, started, inputs, attempts) VALUES(?,?,?,?,?,?,1)",
                         (job_id, stage, target, "running", now(), serialize(bindings)))
        return job_id

    def end_job(self, job_id: str, status: str, error: str | None = None):
        with self.db() as conn:
            conn.execute("UPDATE jobs SET status=?, ended=?, error=? WHERE id=?", (status, now(), error, job_id))

    def preserve_candidates(self, job_id: str, outputs: dict, removals: list[str]):
        for path, value in outputs.items():
            atomic_write(self.path(f".state/stale_candidates/{job_id}/{path}"), value)
        if removals:
            atomic_write(self.path(f".state/stale_candidates/{job_id}/.removals.json"), serialize(removals))

    def accept(self, job_id: str, stage: str, target: str, bindings: list[dict], outputs, *, metadata: dict | None = None, output_scope=None, removals: list[str] | None = None) -> bool:
        with self.locked():
            outputs = outputs(self) if callable(outputs) else outputs
            removals = list(dict.fromkeys(removals or []))
            output_paths = {self.path(p) for p in outputs}
            for path in removals:
                resolved = self.path(path)
                if resolved in output_paths or resolved.is_dir():
                    raise SflError("Transaction removals must be individual files outside the write set")
            if not self.unchanged(bindings):
                self.preserve_candidates(job_id, outputs, removals)
                self.end_job(job_id, "stale", "Inputs changed while the job was running")
                return False
            adjusted = [{**b,"fingerprint":digest(select_text(outputs[b["path"]],b["selector"]))} if b["path"] in outputs else b for b in bindings]
            artifacts = {}
            for path in outputs:
                scoped = output_scope(path,adjusted) if output_scope else adjusted
                artifacts[path] = {"job_id":job_id,"stage":stage,"target":target,"bindings":scoped,"job_bindings":adjusted,
                    "aggregate":path in ("bible.md","assets.csv","source/index.json","plan.json",".state/locks.json","notes/script_notes.json"),
                    "propagate_on_stale":not path.startswith("ledger/"),"fingerprint":digest(outputs[path]),"stale":False,**(metadata or {})}
            # A complete write set and its scopes precede every multi-file commit.
            transaction = {"job_id": job_id, "stage": stage, "target": target,
                           "bindings": bindings, "outputs": outputs, "removals":removals,"artifacts":artifacts,"state":"prepared",
                           "previous":{p:digest(self.text(p)) if self.path(p).exists() else None for p in [*outputs, *removals]}}
            journal = self.path(f".runtime/transactions/{job_id}.json")
            atomic_write(journal, serialize(transaction))
            for path, value in outputs.items():
                atomic_write(self.path(path), value)
            for path in removals:
                self.path(path).unlink(missing_ok=True)
            index = self.json(".state/artifacts.json", {})
            index.update(artifacts)
            for path in removals:
                index.pop(path, None)
            atomic_write(self.path(".state/artifacts.json"), serialize(index))
            self.end_job(job_id, "complete")
            self.record_completion(job_id,stage,target,metadata or {})
            transaction["state"] = "committed"
            atomic_write(journal,serialize(transaction))
        self.snapshot(f"{stage} {target}")
        journal.unlink(missing_ok=True)
        return True

    def recover_transactions(self):
        with self.locked():
            for journal in sorted(self.path(".runtime/transactions").glob("*.json")):
                transaction = json.loads(journal.read_text(encoding="utf-8"))
                job, outputs = transaction["job_id"],transaction["outputs"]
                removals = transaction.get("removals", [])
                if "artifacts" not in transaction or "previous" not in transaction:
                    raise SflError("Unrecognized interrupted transaction; preserve it for inspection")
                safe = transaction.get("state")=="committed"
                if not safe:
                    def allowed(binding):
                        expected = {binding["fingerprint"]}
                        if binding["path"] in outputs:
                            expected.add(digest(select_text(outputs[binding["path"]],binding["selector"])))
                        if binding["path"] in removals:
                            expected.add(digest(None))
                        return self.fingerprint(binding) in expected
                    safe = all(allowed(b) for b in transaction["bindings"]) and all(
                        (digest(self.text(p)) if self.path(p).exists() else None) in (transaction["previous"][p],digest(v)) for p,v in outputs.items()) and all(
                        (digest(self.text(p)) if self.path(p).exists() else None) in (transaction["previous"][p],None) for p in removals)
                    if safe:
                        for path,value in outputs.items():atomic_write(self.path(path),value)
                        for path in removals:self.path(path).unlink(missing_ok=True)
                        index = self.json(".state/artifacts.json",{})
                        index.update(transaction["artifacts"])
                        for path in removals:index.pop(path,None)
                        atomic_write(self.path(".state/artifacts.json"),serialize(index))
                    else:
                        self.preserve_candidates(job,outputs,removals)
                self.end_job(job,"complete" if safe else "stale",None if safe else "Inputs or outputs changed after interruption")
                if safe:
                    self.record_completion(job,transaction["stage"],transaction["target"],next(iter(transaction["artifacts"].values()),{}))
                self.snapshot(f"recover {transaction['stage']} {transaction['target']}")
                journal.unlink()

    def record_completion(self, job_id, stage, target, metadata):
        event_id = "complete:"+job_id
        self.append("decisions",{"id":event_id,"event":"stage_completed","stage":stage,"target":target,
            "job_id":job_id,"demo":metadata.get("demo"),"by":"code","choice":"complete"},unique_id=event_id)

    def stale_artifacts(self) -> dict:
        with self.locked():
            index = self.json(".state/artifacts.json", {})
            for path, meta in index.items():
                if not meta.get("aggregate") and (not self.path(path).exists() or digest(self.text(path)) != meta["fingerprint"]):
                    meta["stale"] = True
                if not self.unchanged(meta["bindings"], include_style=False):
                    meta["stale"] = True
            # A stale prerequisite also makes the descendants stale before any
            # replacement output is written.
            changed = True
            while changed:
                changed = False
                for meta in index.values():
                    if not meta["stale"] and any(index.get(b["path"], {}).get("stale") for b in meta["bindings"]
                        if not b.get("style_reference") and index.get(b["path"], {}).get("propagate_on_stale", True)
                        and not (b.get("selector") and index.get(b["path"], {}).get("aggregate"))
                        and b.get("selector", {}).get("kind") not in ("script_hooks", "last_scene", "unit_carry")):
                        meta["stale"] = True
                        changed = True
            atomic_write(self.path(".state/artifacts.json"), serialize(index))
            return {path: meta for path, meta in index.items() if meta["stale"]}

    def current(self, path: str) -> bool:
        meta = self.json(".state/artifacts.json", {}).get(path)
        return self.path(path).exists() and bool(meta) and not meta["stale"] and digest(self.text(path)) == meta["fingerprint"] and self.unchanged(meta["bindings"], include_style=False)

    def jobs(self) -> list[dict]:
        with self.db() as conn:
            return [dict(row) for row in conn.execute("SELECT id,stage,target,status,error,started,ended FROM jobs ORDER BY started")]

    def backup_daily(self):
        backup = self.runtime / "backups" / f"jobs-{datetime.now(timezone.utc):%Y-%m-%d}.sqlite"
        if backup.exists():
            return
        backup.parent.mkdir(exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".backup-", dir=backup.parent)
        os.close(fd)
        try:
            with self.db() as source:
                destination = sqlite3.connect(temporary)
                try:
                    source.backup(destination)
                finally:
                    destination.close()
            os.replace(temporary, backup)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink()

    def git(self, *args: str, check: bool = True, strip: bool = True) -> str:
        # The importer and local server can run under different Windows users.
        # Trust only this validated project for this invocation, without changing
        # the user's Git configuration or trusting unrelated repositories.
        result = subprocess.run(["git", "-c", "safe.directory=" + self.root.as_posix(), "-C", str(self.root), *args],
                                capture_output=True, text=True, encoding="utf-8")
        if check and result.returncode:
            raise SflError("Git snapshot failed: " + result.stderr.strip())
        return result.stdout.strip() if strip else result.stdout

    def snapshot(self, message: str) -> str:
        with self.locked():
            if not (self.root / ".git").exists():
                self.git("init", "--quiet")
            extensions = {".md", ".txt", ".yaml", ".yml", ".csv", ".json", ".jsonl"}
            candidates = set(self.git("ls-files").splitlines())
            for path in self.root.rglob("*"):
                relative = path.relative_to(self.root)
                if not any(part in (".git", ".runtime", ".cache", "__pycache__") for part in relative.parts) and path.is_file():
                    if path.suffix.lower() in extensions or path.name == ".gitignore":
                        candidates.add(relative.as_posix())
            selected = sorted(p for p in candidates if Path(p).suffix.lower() in extensions or Path(p).name == ".gitignore")
            if selected:
                pathspec = self.runtime / "snapshot-paths.bin"
                pathspec.write_bytes(b"\0".join(p.encode("utf-8") for p in selected) + b"\0")
                self.git("--literal-pathspecs", "add", "--all", "--pathspec-from-file=" + str(pathspec), "--pathspec-file-nul")
            if self.git("diff", "--cached", "--name-only"):
                self.git("-c", "user.name=StoryForge Lite", "-c", "user.email=local@storyforge.invalid", "commit", "--quiet", "-m", message)
            return self.git("rev-parse", "HEAD")

    def revert(self, target: str, snapshot: str):
        if not re.fullmatch(r"[0-9a-fA-F]{7,40}", snapshot):
            raise SflError("Snapshot must be a Git commit hash")
        paths = target_paths(target)
        self.git("rev-parse", "--verify", f"{snapshot}^{{commit}}")
        with self.locked():
            tracked = self.git("ls-files").splitlines()
            historical = self.git("ls-tree", "-r", "--name-only", snapshot).splitlines()
            selected = [p for p in set(tracked + historical) if any(p == root or p.startswith(root + "/") for root in paths)]
            for relative in selected:
                path = self.path(relative)
                if relative in historical:
                    content = self.git("show", f"{snapshot}:{relative}", strip=False)
                    atomic_write(path, content)
                elif path.exists():
                    path.unlink()
            index = self.json(".state/artifacts.json", {})
            for relative, meta in index.items():
                if any(relative == p or relative.startswith(p + "/") for p in paths):
                    meta["stale"] = True
            atomic_write(self.path(".state/artifacts.json"), serialize(index))
        self.append("decisions", {"stage": "revert", "target": target, "snapshot": snapshot, "by": "user", "choice": "restore"})
        self.stale_artifacts()
        self.snapshot(f"revert {target} to {snapshot[:8]}")


def target_paths(target: str) -> list[str]:
    if target == "project":
        return list(CREATIVE_PATHS)
    if re.fullmatch(r"ep\d{2,}", target):
        return [f"episodes/{target}.md", f"ledger/{target}.md", f".state/script_reviews/{target}.json", f"storyboard/{target}.json", f"refs/{target}.json", f"prompts/{target}", f"delivery/{target}"]
    if re.fullmatch(r"ep\d{2,}_u\d{2,}", target):
        episode, unit = target.split("_")
        return [f"prompts/{episode}/{unit}.md", f"delivery/{episode}/{unit}.md"]
    raise SflError("Revert target must be project, epNN or epNN_uNN")


def create_project(path: Path, config: dict):
    if path.exists() and any(path.iterdir()):
        raise SflError(f"Project folder is not empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    atomic_write(path / "project.yaml", yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    atomic_write(path / ".gitignore", ".runtime/\n.cache/\n*.sqlite*\n__pycache__/\n")
    for name in ("source/chunks", "summaries", "episodes", "ledger", "notes", "storyboard", "refs", "prompts", "delivery", "findings", "logs", ".state"):
        (path / name).mkdir(parents=True, exist_ok=True)
    for log in ("calls", "decisions", "feedback"):
        atomic_write(path / "logs" / f"{log}.jsonl", "")
    store = Store(path)
    store.snapshot("create project")
    return store
