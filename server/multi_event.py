"""Serve independently configured RaceTec events on one results website."""

import html
import importlib.util
import io
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "dist"
EVENT_PATH = re.compile(r"/events/([a-z0-9]+(?:-[a-z0-9]+)*)(/.*)?")
INTERNAL_URL = re.compile(
    r"""(["'`])/(?=(?:api/|assets/|admin/|registration/|registrationdisplay/|"""
    r"""rider/|team/|seeding/|data/|[\w-]+\.(?:js|css|png|svg)\b)|["'`])"""
)


def page(title, content):
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title}</title><style>'
        'body{margin:0;background:#f3f5f7;color:#142334;font:1rem system-ui}'
        'main{max-width:46rem;margin:auto;padding:2rem 1rem}'
        'h1{font-size:clamp(2rem,6vw,3rem)}ul{padding:0;list-style:none}'
        'li a{display:block;margin:.65rem 0;padding:1rem 1.25rem;background:#fff;'
        'border:1px solid #c4ced8;border-radius:.5rem;color:#124985;font-weight:700}'
        f'</style><main><h1>{title}</h1>{content}</main></html>'
    ).encode("utf-8")


def scoped_asset(source, prefix, title, generic):
    if generic:
        source = re.sub(
            r'<header class="brandbar">.*?</header>',
            lambda _: '<header class="brandbar"><div class="brandbar__inner">'
            '<a class="brand" href="/">All events</a>'
            f'<div class="event-title"><strong>{title}</strong></div>'
            '<span></span></div></header>',
            source,
            flags=re.S,
        )
        source = re.sub(r'<footer class="footer">.*?</footer>', '', source, flags=re.S)
        source = re.sub(r'<script src="/(?:translate|bracket)\.js(?:\?[^"]*)?"></script>', '', source)
        source = source.replace("European Onewheel League Finals", "Official race results")
    source = INTERNAL_URL.sub(lambda match: match[1] + prefix + "/", source)
    source = source.replace('class="brand" href="' + prefix + '/">All events',
                            'class="brand" href="/">All events')
    source = source.replace("OWAR 2026", title)
    if not generic and '<main' in source:
        source = source.replace('</header>', '</header><nav class="shell" aria-label="Event directory">'
                                '<a href="/">All events</a></nav>', 1)
    return source


def event_handler(settings, data_root, export_root):
    slug = settings["slug"]
    title = html.escape(settings["name"])
    prefix = f"/events/{slug}"
    generic = not settings.get("owar", False)
    source = (export_root / settings["export"]).resolve()
    if export_root not in source.parents or source.suffix.lower() != ".rdf":
        raise ValueError(f"Invalid RDF export path for {slug}")
    if "admin_token_env" in settings:
        token = os.environ[settings["admin_token_env"]]
    else:
        token = os.getenv("ADMIN_TOKEN", "")
    if not token:
        raise ValueError(f"Set an admin token for {slug}")

    spec = importlib.util.spec_from_file_location(f"event_{slug}", ROOT / "server" / "app.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    app.DB_FILE = data_root / slug / "results.sqlite"
    app.DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    app.EXPORT_FILE = source
    app.EXPORT_DIR = source.parent
    app.EXPORT_FILENAME = source.name
    app.EXPORT_HOST_DIR = str(source.parent)
    app.ADMIN_TOKEN = token
    app.GENERIC_EVENT = generic
    if not generic:
        app.import_historical()
    threading.Thread(target=app.watch, name=f"racetec-{slug}", daemon=True).start()

    class Event(app.App):
        def send_header(self, keyword, value):
            if keyword == "Location" and value.startswith("/"):
                value = prefix + value
            super().send_header(keyword, value)

        def send_head(self):
            route = urlsplit(self.path).path
            path = Path(self.translate_path(self.path))
            if route.endswith("/") and path.is_dir():
                path /= "index.html"
            if generic and (path == STATIC / "data" / "seeds.json"
                            or STATIC / "seeding" in path.parents
                            or path == STATIC / "seeding"):
                self.send_error(404)
                return None
            if path.suffix not in {".html", ".js", ".css"} or not path.is_file():
                if path.is_dir() and route.endswith("/"):
                    self.send_error(404)
                    return None
                return super().send_head()
            source = path.read_text(encoding="utf-8")
            data = scoped_asset(source, prefix, title, generic).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(path)) + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return io.BytesIO(data)

    return Event


def run():
    config_path = Path(os.getenv("EVENTS_CONFIG", ROOT / "events.json"))
    settings = json.loads(config_path.read_text(encoding="utf-8-sig"))
    data_root = Path(os.getenv("RESULTS_DATA_DIR", "/data")).resolve()
    export_root = Path(os.getenv("RACE_EXPORT_DIR", "/race-export")).resolve()
    events = {}
    links = []
    for event in settings:
        slug = event["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in events:
            raise ValueError(f"Invalid or duplicate event slug: {slug}")
        events[slug] = event_handler(event, data_root, export_root)
        links.append(f'<li><a href="/events/{slug}/">{html.escape(event["name"])}</a></li>')
    home = page("Race Results", '<ul>' + ''.join(links) + '</ul>' if links
                else '<p>No events have been configured yet.</p>')
    admin_links = ''.join(
        f'<li><a href="/events/{event["slug"]}/admin/">{html.escape(event["name"])}</a></li>'
        for event in settings
    )
    admin = page("Event Admin", '<p><a href="/">All events</a></p><ul>' + admin_links + '</ul>')

    class Directory(BaseHTTPRequestHandler):
        def parse_request(self):
            if not super().parse_request():
                return False
            target = urlsplit(self.path)
            match = EVENT_PATH.fullmatch(target.path)
            if match and match[1] in events and match[2] is not None:
                route = unquote(match[2])
                if route.startswith("//") or "\\" in route or any(
                    segment in {".", ".."} for segment in route.split("/")
                ):
                    self.send_error(404)
                    return False
                self.path = match[2] + (f"?{target.query}" if target.query else "")
                self.__class__ = events[match[1]]
            return True

        def do_GET(self):
            path = urlsplit(self.path).path
            match = EVENT_PATH.fullmatch(path)
            if path == "/admin" or (match and match[1] in events and not match[2]):
                self.send_response(308)
                self.send_header("Location", path + "/")
                self.end_headers()
                return
            document = {"/": home, "/admin/": admin}.get(path)
            if document is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(document)

        do_HEAD = do_GET

    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "6544"))), Directory).serve_forever()


if __name__ == "__main__":
    run()
