# Event Photo Sorter

A local, self-hosted web app for sorting thousands of event photos into
people/categories quickly — built because manually dragging hundreds (or
thousands) of photos into folders one at a time is miserable, and existing
photo-management tools don't make it easy to say "this batch is Person A,
that batch is Person B, this pile is just venue shots" at speed, let alone
split the work across multiple people at the same event.

Point the app at a folder of photos, click through them filing each one into
a category (or select a bunch at once in bulk mode), and it copies them into
neatly organized subfolders — your originals are never touched. If more than
one person is helping sort, everyone can join from their own phone or laptop
over the same WiFi and the workload splits automatically.

## Requirements

- Python 3.9+
- `pip install flask pillow`

## Run it

```
cd apps/event-photo-sorter
python web_sorter.py
```

or double-click `run_sorter.bat` on Windows.

Your browser opens automatically. The first time you run it, it'll ask you to
pick which folder of photos you want to sort — a `sorted/` folder gets created
inside it automatically, and the choice is remembered for next time (change it
anytime from Settings → *Change photo folder*).

You can install this once and reuse it for every event — it doesn't need to
live inside the photo folder itself.

## Project layout

```
apps/event-photo-sorter/
├── web_sorter.py           entry point -- just calls sorter.main()
├── run_sorter.bat           double-click launcher (Windows)
├── requirements.txt
└── sorter/                  the actual app, split by concern
    ├── config.py            constants + "which folder is active" state
    ├── storage.py            progress / skipped / multiplayer-room JSON files
    ├── photos.py             image decoding, thumbnails, EXIF
    ├── logic.py              categories, queue, splitting work across players
    ├── network.py            LAN IP / "is this the host" detection
    ├── app.py                the Flask app instance
    ├── routes.py             all HTTP + JSON API endpoints
    ├── templates/index.html  page markup
    └── static/               css/style.css, js/app.js
```

## What it does

- Click a category card (or type a new one) to file the photo currently on
  screen — people slots, "atmosphere," or anything you want to call it.
- **Bulk mode**: grid view, tap to select or press-and-drag to sweep-select a
  rectangle of photos (like Photos/Gallery apps), file them all at once.
- **Multiplayer**: the server listens on your whole WiFi network. Share the
  LAN link (QR code included) and other people can sort in parallel — the
  photo list splits automatically so nobody works the same photo twice.
- Light/dark theme, resizable panels, drag-and-drop filing, EXIF details
  (camera, ISO, aperture, focal length), click-to-zoom lightbox, and more.
- Nothing here ever touches your original photos — everything happens via
  copies into `sorted/<category>/`.

## Notes

- This runs Flask's built-in dev server, bound to your LAN (`0.0.0.0`) so
  other devices on your WiFi can reach it. There's no login — anyone on your
  network with the link can sort or delete. Don't run this on a network you
  don't trust.
- `Clear All` and the folder picker are restricted to the host machine only
  (checked by IP), not accessible from other devices on the LAN.

## Built with Claude

This project was built collaboratively with [Claude](https://claude.ai)
(Anthropic) — from the original click-to-sort concept through the web
rewrite, multiplayer support, and all the UI/UX iteration in between.

## License

No license specified yet — treat as all-rights-reserved until one is added.
