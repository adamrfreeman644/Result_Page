# Race Results

A multi-event copy of the OWAR results site for Byte-Me. The homepage lists configured events. Each event has its own results URL, RaceTec export, SQLite database, and admin controls. The OWAR profile retains its original branding and bracket features. Other events get a neutral header and publish imported results as they arrive, subject to their own visibility controls.

## Run on Byte-Me

1. Copy `.env.example` to `.env`. Set a long random `ADMIN_TOKEN` and point `RACE_EXPORT_DIR` at a private shared folder.
2. Copy `events.example.json` to `events.json`. Use one entry per event with a unique URL slug, display name, and relative RDF path. Use `"owar": true` for the OWAR profile. An optional `"admin_token_env": "MY_EVENT_TOKEN"` selects a per-event token defined in `.env`.
3. Point each RaceTec export at its corresponding file inside the share. For the sample OWAR event this is `owar-2026/results.rdf`.
4. Run `docker compose up -d --build`. Open `http://byte-me:6544/` for results or `http://byte-me:6544/admin/` for the admin directory. Add or edit events in `events.json` and run `docker compose restart results` to apply them.

The importer checks each RDF every 30 seconds and waits for two matching reads. Docker mounts the share read-only and persists the event databases in the `multi-event-results_event-data` volume. The new stack uses its own volume and port. To bring over an existing OWAR database, stop writers and restore a consistent SQLite backup into the new volume at `owar-2026/results.sqlite` before starting the service.
