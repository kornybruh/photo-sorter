# CLAUDE.md

Context for Claude Code (or any future contributor) working in this repo.

## What this app is

A local Flask web app for sorting large batches of event photos into
categories (people, "atmosphere," anything custom) by clicking through them
one at a time or selecting many at once in bulk mode. Supports multiple
people sorting the same event in parallel from different devices over the
same WiFi network — the photo list splits into equal "sections," one per
player, so nobody works the same photo twice.

The app never moves or modifies original photos. Every "file this photo"
action is a `shutil.copy2` into `<active-folder>/sorted/<category>/`.

## Where everything lives — file-by-file

```
apps/event-photo-sorter/
├── web_sorter.py            Entry point. Just imports and calls sorter.main().
├── run_sorter.bat           Windows double-click launcher, runs web_sorter.py.
├── requirements.txt         flask, pillow.
└── sorter/                  The actual application package.
    ├── __init__.py          Restores the last-used folder on startup, imports
    │                        routes.py (which registers all endpoints on the
    │                        Flask `app` object as a side effect), and defines
    │                        main() which starts the dev server bound to 0.0.0.0
    │                        (LAN-visible) and auto-opens the browser.
    ├── app.py                Just `app = Flask(__name__)` and `state_lock =
    │                        threading.Lock()`. Kept separate from __init__.py
    │                        so routes.py can import `app` without a circular
    │                        import back through __init__.py.
    ├── config.py             Constants (APP_DIR, PORT, TRASH_CATEGORY) and the
    │                        "which folder is currently being sorted" state:
    │                        SRC, SORTED_DIR, PROGRESS_FILE, SKIPPED_FILE,
    │                        ROOM_FILE. These start as None and get set by
    │                        set_active_folder(path), which also creates
    │                        <path>/sorted/ and remembers the choice in
    │                        last_folder.json (next to the app, not inside the
    │                        photo folder) for next launch.
    ├── storage.py             load/save for the three small JSON files that
    │                        live in <folder>/sorted/: manual_progress.json
    │                        (filename -> category), skipped.json (set of
    │                        temporarily-skipped filenames), and
    │                        multiplayer_room.json (room size, which client_id
    │                        is assigned to which section, and each section's
    │                        most-recently-assigned file for Undo scoping).
    ├── photos.py              list_files(), fast_open() (JPEG draft-mode
    │                        decoding so large photos don't fully decode just
    │                        for a thumbnail), and get_photo_info() (EXIF:
    │                        camera, ISO, aperture, focal length, date, size).
    ├── logic.py               Pure logic with no I/O side effects beyond
    │                        reading config: category listing/sorting
    │                        (existing_categories, always appends "trash" and
    │                        always sorts it last), the undecided-photo queue
    │                        (first_undecided), and splitting the file list
    │                        across players (get_partition, partition_slice).
    ├── network.py             get_lan_ip() and get_local_addresses()
    │                        (LOCAL_ADDRESSES) -- used to tell "this request
    │                        came from the host machine itself" apart from
    │                        "someone else on the WiFi," which gates Clear All
    │                        and the folder picker.
    ├── routes.py              Every Flask route: the index page and the full
    │                        JSON API (state, assign, bulk_assign, skip, undo,
    │                        rename, delete_category, sync, clear_all, join,
    │                        sections_progress, photo/photo_info, folder
    │                        picker). This is the biggest file and the one
    │                        most feature work touches.
    ├── templates/index.html   Page markup only. No inline <style> or <script>
    │                        blocks -- everything is in static/.
    └── static/
        ├── css/style.css      All styling. Token-based (CSS custom properties
        │                    for colors/radii), light+dark theme via
        │                    [data-bs-theme], navy-blue gradient button system.
        └── js/app.js           All frontend behavior. Single global script,
                              no bundler/framework -- Bootstrap 5 + vanilla JS.
```

## Key patterns worth knowing before you touch things

**The active-folder globals are reassigned, not passed as arguments.**
`config.SRC` / `SORTED_DIR` / etc. start as `None` and get set once by
`set_active_folder()`. Every other module reads them via `config.SRC` (module
attribute access), **never** via `from .config import SRC`  — the latter
would freeze a `None` reference at import time and never see later
reassignment. If you add a new module that needs the active folder, import
the module (`from . import config`) and always reference `config.SRC`.

**The frontend polls, it doesn't use websockets.** Live multiplayer sync
(other players' picks, new categories appearing) happens via a 3-second
`setInterval` in `app.js` (`startCategoryAutoSync`) hitting `/api/state`.
The strip-rendering code (`renderStrip`) diffs against what's already on
screen and only touches DOM nodes that actually changed, specifically to
avoid flicker on every poll tick.

**Multiplayer identity is a client-generated UUID in localStorage
(`clientId`)**, not a login. The server maps `client_id -> section number`
in `multiplayer_room.json`, persisted to disk (not just in memory) so a
server restart doesn't scramble anyone's assignment or Undo scoping. See the
`api_join` history in git/commit messages if this ever regresses — there was
a real bug here once where everyone joining while `sections==1` collided
onto section 1 via a bad modulo fallback; the fix was to return
`{"room_full": true}` instead of silently doubling someone up.

**Host-only actions** (Clear All, the folder picker, setting room size) are
gated by checking `request.remote_addr` against `network.LOCAL_ADDRESSES`,
which includes `127.0.0.1` **and** the machine's own LAN-facing IP — the
host's own browser gets auto-opened to the LAN URL, not localhost, so
checking only `127.0.0.1` was a real bug once (host got locked out of their
own host-only controls).

**No test suite exists yet.** Development so far has been manual smoke
testing: `python -m py_compile` for syntax, then spinning up the Flask app
in a background thread and hitting endpoints with `urllib.request` to check
real responses, cleaning up any test data written into the live progress
files afterward. If you add a test suite, `pytest` + Flask's test client
would be the natural fit — nothing about the current structure fights that.

## If you're adding a feature

- New API behavior → `routes.py` (+ helper logic in `logic.py`/`photos.py`
  if it's reusable).
- New UI element → add the HTML in `templates/index.html`, styles in
  `static/css/style.css`, behavior in `static/js/app.js`.
- Anything touching "who am I / which section" → remember non-host devices
  never pick their own player number; the host sets room size once, everyone
  else is auto-assigned. Don't reintroduce manual section-picking for
  non-host devices.
