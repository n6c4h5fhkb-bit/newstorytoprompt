"""Loopback-only UI; all production actions call the CLI's service functions."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
from urllib.parse import parse_qs, urlsplit

from storyforge.config import SflError, configuration
from storyforge.cards import Cards
from storyforge.refs import edit
from storyforge.runner import Runner, import_episode
from storyforge.runner import service
from storyforge.store import Store
from storyforge.delivery import package
from storyforge.ui.render import render, document, form_command, url, VIEWS

ASSETS = Path(__file__).parent / "assets"


class LoopbackServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR lets two listeners serve the same port, splitting
    # commands between independent background queues during a restart.
    allow_reuse_address = not hasattr(socket, "SO_EXCLUSIVEADDRUSE")

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Application:
    def __init__(self, projects: Path):
        self.projects = projects.resolve()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sfl-ui")
        self.futures, self.results = {}, {}
        self.lock = threading.Lock()

    def store(self, name):
        return Store(service.project_path(name, self.projects))

    def background(self, name):
        with self.lock:
            future = self.futures.get(name)
            return {"running": bool(future and not future.done()), "report": self.results.get(name)}

    def start(self, name, action):
        with self.lock:
            if name in self.futures and not self.futures[name].done():
                return {"running": True}
            self.results.pop(name, None)
            def work():
                try:
                    result = action()
                except Exception as exc:
                    result = {"error": str(exc)}
                with self.lock:
                    self.results[name] = result
                return result
            self.futures[name] = self.pool.submit(work)
        return {"running": True}

    def command(self, body):
        action, name = body["action"], body.get("project")
        if action in ("new", "demo"):
            store = service.new(name, self.projects, novel_text=body.get("novel") or None, demo=action == "demo")
            return {"project": name, "demo": action == "demo"}
        if action == "settings":
            return service.update_settings(body["values"])
        store = self.store(name)
        if action == "project_settings":
            return service.update_project_settings(store, body["values"])
        if action == "import":
            return {"imported": import_episode(store, body["script"], body["bible"], body["ledger"], assets_text=body.get("assets") or None)}
        if action == "answer":
            return Cards(store, configuration(store.root)).answer(body["card"], body["option"], body.get("note", ""))
        if action == "note":
            return service.note(store, body["target"], body["note"])
        if action == "feedback":
            service.feedback(store, body["unit"], body["result"], body.get("note", ""),generations=body.get("generations"),user_minutes=body.get("user_minutes"))
            return service.metrics(store)
        if action == "finish":
            service.finish_episode(store,body["episode"],user_minutes=body.get("user_minutes"),note=body.get("note",""))
            return service.metrics(store)
        if action == "refs":
            edit(store, body["unit"], body["operation"], body["placeholder"])
            return {"edited": body["unit"]}
        if action == "pause":
            return service.pause(store)
        if action in ("run", "retry"):
            # A plain Continue advances past the checkpoint but retains the
            # user's selected episodes. An explicit new scope replaces it.
            scope = store.json(".runtime/run_scope.json", {}) if action == "retry" or not {"until", "episodes"} & body.keys() else {}
            until = body.get("until", scope.get("until", "B9") if action == "retry" else "B9")
            episodes = body.get("episodes", scope.get("episodes"))
            service.resume(store)
            return self.start(name, lambda: Runner(self.store(name)).run(until=until, episodes=episodes))
        if action == "rerun":
            return self.start(name, lambda: service.rerun(self.store(name), body["stage"], body["target"], body.get("note")))
        raise SflError("Unknown action")


def make_server(projects: Path, port=8765):
    app = Application(projects)
    class Handler(BaseHTTPRequestHandler):
        def respond(self, value, status=200, mime="application/json; charset=utf-8", *, download=None):
            data = json.dumps(value, ensure_ascii=False).encode("utf-8") if mime.startswith("application/json") else value
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="{download}"')
            self.end_headers()
            self.wfile.write(data)

        def trusted(self):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") not in (expected, f"localhost:{self.server.server_port}"):
                raise SflError("Unexpected host")
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://" + expected, f"http://localhost:{self.server.server_port}"):
                raise SflError("Cross-origin request refused")

        def do_GET(self):
            try:
                self.trusted()
                parsed = urlsplit(self.path)
                query = {k:v[0] for k,v in parse_qs(parsed.query).items()}
                if parsed.path == "/":
                    html = document((ASSETS / "index.html").read_text(encoding="utf-8"), render(app, query))
                    return self.respond(html.encode("utf-8"), mime="text/html; charset=utf-8")
                if parsed.path == "/api/view":
                    return self.respond(render(app, query))
                if parsed.path in ("/app.js", "/styles.css"):
                    filename = parsed.path[1:]
                    mime = {"app.js":"text/javascript", "styles.css":"text/css"}[filename] + "; charset=utf-8"
                    return self.respond((ASSETS / filename).read_bytes(), mime=mime)
                if parsed.path == "/api/projects":
                    return self.respond({"projects":[{"name":p.name, "demo": bool(configuration(p).get("demo"))}
                        for p in sorted(app.projects.glob("*")) if (p / "project.yaml").exists()]})
                if parsed.path == "/api/settings":
                    return self.respond(configuration(app.store(query["project"]).root) if query.get("project") else service.settings())
                store = app.store(query["project"])
                if parsed.path == "/api/status":
                    result = service.status(store)
                    result["background"] = app.background(store.root.name)
                    # Logs and job internals are available through explicit details only.
                    result.pop("jobs", None)
                    return self.respond(result)
                if parsed.path == "/api/inbox":
                    return self.respond(service.inbox(store))
                if parsed.path == "/api/episode":
                    return self.respond(service.episode_details(store, query["episode"]))
                if parsed.path == "/api/file":
                    relative = query["path"]
                    allowed = ("episodes/", "ledger/", "notes/", "storyboard/", "refs/", "prompts/", "delivery/", "findings/", ".state/stuck/")
                    if relative not in ("style.md", "bible.md", "assets.csv", "plan.md", "breakdown.md") and not relative.startswith(allowed):
                        raise SflError("This file is not exposed by the UI")
                    if store.path(relative).suffix not in (".md", ".json", ".csv", ".txt"):
                        raise SflError("Only production text is exposed")
                    return self.respond({"path": str(store.path(relative)), "text": store.text(relative)})
                if parsed.path == "/api/delivery":
                    episode = query["episode"]
                    return self.respond(package(store, episode), mime="application/zip", download=episode + ".zip")
                self.respond({"error":"Not found"}, 404)
            except (SflError, KeyError, OSError, ValueError) as exc:
                self.respond({"error": str(exc)}, 400)

        def do_POST(self):
            try:
                self.trusted()
                content_type = self.headers.get("Content-Type", "").split(";")[0]
                native = self.path == "/action" and content_type == "application/x-www-form-urlencoded"
                if not native and (self.path != "/api/command" or content_type != "application/json"):
                    raise SflError("Expected a JSON command or a local form")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4 * 1024 * 1024:
                    raise SflError("Command is empty or too large")
                text = self.rfile.read(length).decode("utf-8")
                fields = parse_qs(text, keep_blank_values=True) if native else None
                body = form_command(fields) if native else json.loads(text)
                if not isinstance(body, dict):
                    raise SflError("Expected a command object")
                result = app.command(body)
                if native:
                    ctx = {"project": result.get("project", body.get("project", "")),
                           "view": fields.get("_view", ["projects"])[-1],
                           "episode": fields.get("_episode", [None])[-1], "item": fields.get("_item", [0])[-1]}
                    if ctx["view"] not in VIEWS:
                        ctx["view"] = "projects"
                    self.send_response(303)
                    self.send_header("Location", url(ctx))
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                else:
                    self.respond(result)
            except (SflError, KeyError, OSError, ValueError, TypeError) as exc:
                self.respond({"error": str(exc)}, 400)

        def log_message(self, *args):
            pass
    server = LoopbackServer(("127.0.0.1", port), Handler)
    server.app = app
    return server


def serve(projects: Path, port=8765):
    server = make_server(projects, port)
    print(f"StoryForge Lite: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        server.app.pool.shutdown(wait=True)
