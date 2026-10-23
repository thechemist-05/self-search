# Privacy

Self Search is designed to be **fully local and private**.

- All processing happens on-device. No user data leaves the machine.
- No telemetry, analytics, or external API calls at runtime.
- Remote image loading is off unless `SHELFSENSE_LOAD_REMOTE_IMAGES=true`. The demo launcher turns it on, and `-NoImages` turns it off. When it is on, the **UI server**, not the browser, requests each shown product's image from Amazon's image host (`m.media-amazon.com`). This reveals which product images were viewed and needs an internet connection. No user data is sent with the request.

> See [master-spec.md](master-spec.md) Section 18 for the full security and privacy design.

## No user profiles

User profiles were removed on 2026-10-21. Self Search stores **no personal data**: no accounts, profiles, preferences, wishlists, feedback or chat history. A conversation lives only in memory (the browser tab and the API session) and is lost when the server restarts. Search ranking is the same for everyone.

**Old profile data.** The `users`, `user_preferences`, `wishlist` and `feedback` tables still exist in the schema but are no longer read or written. In the project database they were empty when profiles were removed (0 rows each). Their removal is a separate, approved database cleanup (see [database-schema.md](database-schema.md)). Backups made by `scripts/migrate_db.py` are copies of the database at migration time.

**Dataset reviews.** Reviewer identifiers and names in source datasets are dropped during ingestion and never stored.

**Binding:** both `python -m uvicorn src.api.app:app` and `docker compose up` (ports published as `127.0.0.1:…`, fixed 2026-10-19) are reachable from this machine only. A manual `docker run -p 8000:8000` would expose the API on every interface; use `-p 127.0.0.1:8000:8000`. Before exposing Self Search beyond localhost, see [deployment.md](deployment.md). Setting `API_KEY` is required, but it still does not provide per-user authentication.

See [limitations.md](limitations.md).
