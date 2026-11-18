"""Name-based index of published rider times across event databases."""

import html
import sqlite3
import unicodedata
from collections import defaultdict
from contextlib import closing
from urllib.parse import urlencode


def name_key(name):
    return " ".join(unicodedata.normalize("NFC", name).casefold().split())


def rider_href(name):
    return "/riders/?" + urlencode({"name": name})


def collect_times(events):
    riders = {}
    for event, app in events:
        if not app.DB_FILE.is_file():
            continue
        href = f'/events/{event["slug"]}/'
        with closing(sqlite3.connect(app.DB_FILE.as_uri() + "?mode=ro", uri=True)) as db:
            source = db.execute("SELECT value FROM meta WHERE key='source_mode'").fetchone()
            source_check = "LIKE" if source and source[0] == "paste" else "NOT LIKE"
            mapping_check = "" if app.GENERIC_EVENT else (
                " AND EXISTS(SELECT 1 FROM event_mappings m WHERE m.event_id=e.id)"
            )
            published = (
                "e.publish_mode!='hide' AND e.id " + source_check + " 'paste:%'" + mapping_check
            )
            solo = db.execute(
                "SELECT a.name,e.name,r.time,l.lap_number,l.time "
                "FROM results r JOIN events e ON e.id=r.event_id "
                "JOIN athletes a ON a.id=r.athlete_id "
                "LEFT JOIN result_laps l ON l.event_id=r.event_id AND l.athlete_id=r.athlete_id "
                "WHERE " + published +
                " AND lower(e.tournament) NOT IN ('eol points','team race') "
                "ORDER BY e.sort_order,e.name,l.lap_number"
            )
            team = db.execute(
                "SELECT l.rider_name,e.name,'',l.lap_number,l.time "
                "FROM team_laps l JOIN events e ON e.id=l.event_id "
                "WHERE " + published +
                " AND l.is_start=0 AND trim(l.rider_name)!='' "
                "ORDER BY e.sort_order,e.name,l.lap_number"
            )
            for rows in (solo, team):
                for name, race, result, lap, lap_time in rows:
                    key = name_key(name)
                    if not key or app.placeholder_rider_name(name):
                        continue
                    display, records = riders.setdefault(key, (" ".join(name.split()), []))
                    timing = lap_time if lap is not None else result
                    milliseconds = app.time_ms(timing)
                    if milliseconds is None or milliseconds <= 0:
                        continue
                    minutes, remainder = divmod(milliseconds, 60_000)
                    seconds, fraction = divmod(remainder, 1000)
                    records.append((event["name"], href, race, lap, f"{minutes:02}:{seconds:02}.{fraction:03}"))
    return riders


def archive_page(riders, search, name):
    escape = html.escape
    nav = '<nav class="archive-nav"><a href="/">All events</a> <a href="/riders/">All riders</a></nav>'
    rider = riders.get(name_key(name)) if name else None
    if name:
        display, records = rider or (name, [])
        title = escape(display)
        groups = defaultdict(list)
        for event, href, race, lap, time in records:
            kind = f"Lap {lap}" if lap is not None else "Race time"
            groups[(event, href)].append(
                f'<tr><td>{escape(race)}</td><td>{kind}</td><td>{time}</td></tr>'
            )
        content = ''.join(
            f'<section class="archive-event"><h2><a href="{href}">{escape(event)}</a></h2>'
            '<div class="archive-table"><table><thead><tr><th>Race</th><th>Timing</th>'
            '<th>Time</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></section>'
            for (event, href), rows in groups.items()
        ) or '<p>No recorded times were found for this name.</p>'
        subtitle = f"{len(records)} recorded times across {len(groups)} events"
    else:
        title = "All riders"
        subtitle = "Find laps and race times across published events."
        matches = sorted(
            (display for key, (display, _) in riders.items() if name_key(search) in key),
            key=name_key,
        )
        links = ''.join(
            f'<li><a href="{escape(rider_href(display))}">{escape(display)}</a></li>'
            for display in matches
        )
        content = (
            '<form class="directory-sort archive-search" method="get" action="/riders/">'
            '<label for="rider-search">Rider name</label>'
            f'<input id="rider-search" name="search" type="search" value="{escape(search)}">'
            '<button type="submit">Search</button></form>'
            + (f'<ul class="archive-riders">{links}</ul>' if links else '<p>No matching riders.</p>')
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{title} | Race Results</title><link rel="stylesheet" href="/event-chrome.css"></head>'
        '<body class="event-directory"><header class="directory-header"><div class="directory-inner">'
        f'{nav}<h1>{title}</h1><p>{subtitle}</p></div></header>'
        f'<main class="directory-main"><div class="directory-inner">{content}</div></main>'
        '<footer class="event-site-footer"><a class="event-directory-link" href="/">All events</a></footer>'
        '</body></html>'
    ).encode("utf-8")
