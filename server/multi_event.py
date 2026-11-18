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
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "dist"
EVENT_PATH = re.compile(r"/events/([a-z0-9]+(?:-[a-z0-9]+)*)(/.*)?")
INTERNAL_URL = re.compile(
    r"""(["'`])/(?=(?:api/|assets/|admin/|registration/|registrationdisplay/|"""
    r"""rider/|team/|seeding/|data/|[\w-]+\.(?:js|css|png|svg)\b)|\?event=|["'`])"""
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


def event_colours(settings):
    palette = settings.get("palette", {})
    if any(key not in {"accent", "header"} or not isinstance(value, str)
           or not re.fullmatch(r"#[0-9a-fA-F]{6}", value) for key, value in palette.items()):
        raise ValueError(f"Invalid colour palette for {settings['slug']}")
    return palette.get("accent", "#f5cc24"), palette.get("header", "#162c43")


def event_date(settings):
    value = settings.get("date")
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    return None


def directory_page(settings, sort):
    options = {"newest": "Newest first", "oldest": "Oldest first", "name": "Name (A to Z)"}
    if sort not in options:
        sort = "newest"
    events = sorted(settings, key=lambda event: event["name"].casefold())
    if sort != "name":
        events.sort(key=lambda event: (
            event_date(event) is None,
            (event_date(event) or date.min).toordinal() * (-1 if sort == "newest" else 1),
        ))
    cards = []
    for event in events:
        name = html.escape(event["name"])
        accent, _ = event_colours(event)
        when = event_date(event)
        date_label = (
            f'<time datetime="{when.isoformat()}">{when.day} {when:%b %Y}</time>' if when else ""
        )
        cards.append(
            f'<li><a class="directory-card" style="--event-accent:{accent}" href="/events/{event["slug"]}/">'
            f'<strong>{name}</strong>{date_label}<span>View results</span></a></li>'
        )
    sort_options = ''.join(
        f'<option value="{key}"{" selected" if sort == key else ""}>{label}</option>'
        for key, label in options.items()
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>Race Results</title><link rel="stylesheet" href="/event-chrome.css"></head>'
        '<body class="event-directory"><header class="directory-header"><div class="directory-inner">'
        '<h1>Race Results</h1><p>Choose an event to view its results and lap times.</p>'
        '</div></header><main class="directory-main"><div class="directory-inner">'
        '<div class="directory-toolbar"><h2>Events</h2><form class="directory-sort" method="get" action="/">'
        '<label for="sort">Sort by</label>'
        f'<select id="sort" name="sort">{sort_options}</select><button type="submit">Apply</button></form></div>'
        f'<ul class="directory-grid">{"".join(cards)}</ul>'
        '</div></main></body></html>'
    ).encode("utf-8")


def scoped_asset(source, prefix, title, generic):
    if generic:
        source = re.sub(
            r'<header class="brandbar">.*?</header>',
            lambda _: '<header class="brandbar"><div class="brandbar__inner">'
            f'<div class="event-title"><strong>{title}</strong></div></div></header>',
            source,
            flags=re.S,
        )
        source = re.sub(r'<footer class="footer">.*?</footer>', '', source, flags=re.S)
        source = re.sub(r'<script src="/(?:translate|bracket)\.js(?:\?[^"]*)?"></script>', '', source)
        source = source.replace("European Onewheel League Finals", "Official race results")
    source = INTERNAL_URL.sub(lambda match: match[1] + prefix + "/", source)
    source = source.replace("OWAR 2026", title)
    return source


def event_handler(settings, data_root, export_root, config_dir):
    slug = settings["slug"]
    title = html.escape(settings["name"])
    prefix = f"/events/{slug}"
    generic = not settings.get("owar", False)
    archived = settings.get("archive", False)
    category_qualifiers = settings.get("qualifiers_in_categories", False)
    accent, header = event_colours(settings)
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
        def js(self, body, code=200):
            if archived and isinstance(body, dict) and body.get("teams"):
                with closing(app.db()) as connection:
                    has_roster = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name='archive_team_members'"
                    ).fetchone()
                    if has_roster:
                        for team in body["teams"]:
                            team["members"] = [
                                row[0] for row in connection.execute(
                                    "SELECT member FROM archive_team_members WHERE team=? ORDER BY sort_order",
                                    (team["team"],),
                                )
                            ]
            return super().js(body, code)

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
            scoped = scoped_asset(source, prefix, title, generic)
            if path.suffix == ".html":
                styles = '<link rel="stylesheet" href="/event-chrome.css">'
                if generic:
                    styles += f'<style>:root{{--event-accent:{accent};--event-header:{header}}}</style>'
                    scoped = re.sub(r'<body\b', '<body data-event-theme="archive"', scoped, count=1)
                scoped = scoped.replace("</head>", styles + "</head>", 1)
                back = '<a class="event-directory-link" href="/">All events</a>'
                if "</footer>" in scoped:
                    scoped = scoped.replace('<footer class="footer">', '<footer class="footer event-site-footer">', 1)
                    scoped = scoped.replace("</footer>", back + "</footer>", 1)
                else:
                    scoped = scoped.replace("</body>", f'<footer class="event-site-footer">{back}</footer></body>', 1)
            data = scoped.encode("utf-8")
            if archived and path.name == "index.html" and route in {"/", "/index.html"}:
                data = data.replace(b"<h1>Live results</h1>", b"<h1>Results</h1>")
                data = data.replace(b"Automatic results feed", b"Event archive")
            if archived and path.name == "team.js":
                data = data.replace(
                    b"</p></div></section><section",
                    b"</p>${team.members?.length?`<p>${team.members.map(esc).join(', ')}</p>`:''}</div></section><section>",
                    1,
                )
            if archived and path.name == "live.js":
                script = data.decode("utf-8").replace(
                    "BracketProjection?BracketProjection.stageOfItem(item):'Qualifiers'",
                    "(item.e.level||'Results')",
                ).replace(
                    "BracketProjection?BracketProjection.stageOfItem(x):'Qualifiers'",
                    "(x.e.level||'Results')",
                ).replace(
                    "const rawTs=[...new Set((f.events||[]).map(e=>e.tournament||'Tournament'))];",
                    "const archiveTab=e=>!categoryQualifiers&&e.level==='Qualifiers'?'Qualifiers':displayTournament(e.tournament||'Tournament');",
                ).replace(
                    "rawTs.map(displayTournament)",
                    "(f.events||[]).map(archiveTab)",
                ).replace(
                    "displayTournament(item.e.tournament||'Tournament')===selected",
                    "archiveTab(item.e)===selected",
                ).replace(
                    "const order=['open'",
                    "const order=['qualifiers',...(categoryQualifiers?[]:['sts championship','eol points']),'open'",
                ).replace(
                    "timeLabel=e.multi_lap?'Fastest lap':'Time'",
                    "timeLabel=e.tournament==='EOL Points'?'Points':e.tournament==='STS Championship'?'Time recorded':e.multi_lap?'Fastest lap':'Time'",
                ).replace(
                    "const all=[...enriched.bracket,...enriched.extras,...seedingExtras];",
                    """let all=[...enriched.bracket,...enriched.extras,...seedingExtras];
    if(all.some(item=>item.e.level==='Qualifiers')){
      const multipleCategories=all.length>1;
      if(multipleCategories)all.sort((a,b)=>tournamentRank(a.e.tournament)-tournamentRank(b.e.tournament));
      const heading=(item,label)=>multipleCategories?item.e.tournament+' - '+label:label;
      const timeValue=value=>{
        if(!/^\\d+(?::\\d+){1,2}(?:\\.\\d+)?$/.test(value||''))return Infinity;
        return value.split(':').reduce((seconds,part)=>seconds*60+Number(part),0);
      };
      all=all.flatMap(item=>{
        if(item.e.level!=='Qualifiers')return [item];
        const lapNumbers=[...new Set(item.r.flatMap(row=>(row.laps||[]).map(lap=>Number(lap.lap_number))))].sort((a,b)=>a-b);
        const laps=lapNumbers.map(number=>{
          const label='Lap '+number;
          const rows=item.r.map(row=>{
            const lap=(row.laps||[]).find(entry=>Number(entry.lap_number)===number);
            return {...row,position:null,finishPos:null,time:lap?.time||''};
          }).sort((a,b)=>timeValue(a.time)-timeValue(b.time));
          return {...item,e:{...item.e,id:item.e.id+'-lap-'+number,name:heading(item,label),level:categoryQualifiers?'Qualifiers':label},qualifierView:label,r:rows};
        });
        return [...laps,{...item,e:{...item.e,name:heading(item,'Overall Results'),level:categoryQualifiers?'Qualifiers':'Overall Results'},qualifierView:'Overall Results'}];
      });
    }""",
                ).replace(
                    "if(!selectedLevel||!levels.includes(selectedLevel))selectedLevel=levels[0];",
                    "if(!selectedLevel||!levels.includes(selectedLevel))selectedLevel=selected==='Qualifiers'?'Overall Results':levels[0];",
                ).replace(
                    "const set=all.filter(x=>((x.e.level||'Results'))===selectedLevel);",
                    """let set=all.filter(x=>(x.e.level||'Results')===selectedLevel);
    let qualifierNav='';
    if(categoryQualifiers&&selectedLevel==='Qualifiers'){
      const views=[...new Set(set.map(item=>item.qualifierView))];
      if(!views.includes(selectedQualifierView))selectedQualifierView='Overall Results';
      qualifierNav='<nav class="tabs" aria-label="Qualifier results">'+views.map(view=>
        '<button type="button" class="tab'+(view===selectedQualifierView?' active':'')+'" data-qualifier-view="'+esc(view)+'" aria-pressed="'+(view===selectedQualifierView)+'">'+esc(view)+'</button>'
      ).join('')+'</nav>';
      set=set.filter(item=>item.qualifierView===selectedQualifierView);
    }""",
                ).replace(
                    '=`<div class="race-grid ${set.length',
                    '=`${qualifierNav}<div class="race-grid ${set.length',
                ).replace(
                    "    $('#stage-results').querySelectorAll('.race-card-header",
                    "    $('#stage-results').querySelectorAll('[data-qualifier-view]').forEach(button=>button.onclick=()=>{selectedQualifierView=button.dataset.qualifierView;render()});\n    $('#stage-results').querySelectorAll('.race-card-header",
                ).replace("if(!liveTabs.includes('Lap Times'))", "if(liveTabs.length&&!liveTabs.includes('Lap Times'))")
                script = f"const categoryQualifiers={'true' if category_qualifiers else 'false'};\nlet selectedQualifierView='Overall Results';\n" + script
                script = script.replace("function renderUpdateAge(){", "function renderUpdateAge(){ const archiveLabel=$('#updated-status');if(archiveLabel){archiveLabel.textContent='Archived results';return;}")
                data = script.encode("utf-8")
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
    chrome_styles = (STATIC / "event-chrome.css").read_bytes()
    for event in settings:
        slug = event["slug"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) or slug in events:
            raise ValueError(f"Invalid or duplicate event slug: {slug}")
        events[slug] = event_handler(event, data_root, export_root, config_path.parent)
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
            target = urlsplit(self.path)
            path = target.path
            match = EVENT_PATH.fullmatch(path)
            if path == "/admin" or (match and match[1] in events and not match[2]):
                self.send_response(308)
                self.send_header("Location", path + "/")
                self.end_headers()
                return
            content_type = "text/html"
            if path == "/":
                document = directory_page(settings, parse_qs(target.query).get("sort", ["newest"])[0])
            elif path == "/event-chrome.css":
                document = chrome_styles
                content_type = "text/css"
            else:
                document = admin if path == "/admin/" else None
            if document is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(document)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(document)

        do_HEAD = do_GET

    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "6544"))), Directory).serve_forever()


if __name__ == "__main__":
    run()
