"""Serve independently configured RaceTec events on one results website."""

import html
import importlib.util
import io
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
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


def event_handler(settings, data_root, export_root, config_dir):
    slug = settings["slug"]
    title = html.escape(settings["name"])
    prefix = f"/events/{slug}"
    generic = not settings.get("owar", False)
    archived = settings.get("archive", False)
    palette = settings.get("palette", {})
    if any(key not in {"accent", "header"} or not re.fullmatch(r"#[0-9a-fA-F]{6}", value)
           for key, value in palette.items()):
        raise ValueError(f"Invalid colour palette for {slug}")
    source = (export_root / settings.get("export", f"{slug}/results.rdf")).resolve()
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
    if archived and not app.DB_FILE.exists():
        seed = config_dir / f"{slug}.sqlite"
        temporary = app.DB_FILE.with_suffix(".importing")
        with closing(sqlite3.connect(seed.as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as destination:
                original.backup(destination)
                if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError(f"Invalid archive database for {slug}")
        temporary.replace(app.DB_FILE)
    if not archived:
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
            if archived and path.name == "index.html" and route in {"/", "/index.html"}:
                data = data.replace(b"<h1>Live results</h1>", b"<h1>Results</h1>")
                data = data.replace(b"Automatic results feed", b"Event archive")
            if archived and path.name == "live.js":
                script = data.decode("utf-8").replace(
                    "BracketProjection?BracketProjection.stageOfItem(item):'Qualifiers'",
                    "(item.e.level||'Results')",
                ).replace(
                    "BracketProjection?BracketProjection.stageOfItem(x):'Qualifiers'",
                    "(x.e.level||'Results')",
                ).replace(
                    "const rawTs=[...new Set((f.events||[]).map(e=>e.tournament||'Tournament'))];",
                    "const archiveTab=e=>e.level==='Qualifiers'?'Qualifiers':displayTournament(e.tournament||'Tournament');",
                ).replace(
                    "rawTs.map(displayTournament)",
                    "(f.events||[]).map(archiveTab)",
                ).replace(
                    "displayTournament(item.e.tournament||'Tournament')===selected",
                    "archiveTab(item.e)===selected",
                ).replace(
                    "const order=['open'",
                    "const order=['qualifiers','sts championship','eol points','open'",
                ).replace(
                    "timeLabel=e.multi_lap?'Fastest lap':'Time'",
                    "timeLabel=e.tournament==='EOL Points'?'Points':e.tournament==='STS Championship'?'Time recorded':e.multi_lap?'Fastest lap':'Time'",
                ).replace(
                    "const all=[...enriched.bracket,...enriched.extras,...seedingExtras];",
                    """let all=[...enriched.bracket,...enriched.extras,...seedingExtras];
    if(selected==='Qualifiers'){
      const multipleCategories=all.length>1;
      if(multipleCategories)all.sort((a,b)=>tournamentRank(a.e.tournament)-tournamentRank(b.e.tournament));
      const heading=(item,label)=>multipleCategories?item.e.tournament+' - '+label:label;
      const timeValue=value=>{
        if(!/^\\d+(?::\\d+){1,2}(?:\\.\\d+)?$/.test(value||''))return Infinity;
        return value.split(':').reduce((seconds,part)=>seconds*60+Number(part),0);
      };
      all=all.flatMap(item=>{
        const lapNumbers=[...new Set(item.r.flatMap(row=>(row.laps||[]).map(lap=>Number(lap.lap_number))))].sort((a,b)=>a-b);
        const laps=lapNumbers.map(number=>{
          const label='Lap '+number;
          const rows=item.r.map(row=>{
            const lap=(row.laps||[]).find(entry=>Number(entry.lap_number)===number);
            return {...row,position:null,finishPos:null,time:lap?.time||''};
          }).sort((a,b)=>timeValue(a.time)-timeValue(b.time));
          return {...item,e:{...item.e,id:item.e.id+'-lap-'+number,name:heading(item,label),level:label},r:rows};
        });
        return [...laps,{...item,e:{...item.e,name:heading(item,'Overall Results'),level:'Overall Results'}}];
      });
    }""",
                ).replace(
                    "if(!selectedLevel||!levels.includes(selectedLevel))selectedLevel=levels[0];",
                    "if(!selectedLevel||!levels.includes(selectedLevel))selectedLevel=selected==='Qualifiers'?'Overall Results':levels[0];",
                ).replace("if(!liveTabs.includes('Lap Times'))", "if(liveTabs.length&&!liveTabs.includes('Lap Times'))")
                script = script.replace("function renderUpdateAge(){", "function renderUpdateAge(){ const archiveLabel=$('#updated-status');if(archiveLabel){archiveLabel.textContent='Archived results';return;}")
                data = script.encode("utf-8")
            if palette and path.name == "styles.css":
                accent = palette.get("accent", "#f5cc24")
                header = palette.get("header", "#101010")
                data += (f":root{{--yellow:{accent};--yellow2:{accent};--paper:#f6f8ef}}"
                         f".brandbar,.race-table>header,.rider-hero{{background:{header}}}"
                         "body{background:var(--paper)}").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(path)) + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return io.BytesIO(data)

    return Event


def run():
    config_path = Path(os.getenv("EVENTS_CONFIG", ROOT / "events.json")).resolve()
    settings = json.loads(config_path.read_text(encoding="utf-8-sig"))
    data_root = Path(os.getenv("RESULTS_DATA_DIR", "/data")).resolve()
    export_root = Path(os.getenv("RACE_EXPORT_DIR", "/race-export")).resolve()
    events = {}
    links = []
    for event in settings:
        slug = event["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in events:
            raise ValueError(f"Invalid or duplicate event slug: {slug}")
        events[slug] = event_handler(event, data_root, export_root, config_path.parent)
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
