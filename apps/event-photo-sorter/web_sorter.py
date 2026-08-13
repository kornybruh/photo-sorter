"""
Event Photo Sorter -- Web version.

Install once, anywhere -- the app itself doesn't need to live inside your
photo folder. On first launch it asks you to pick which folder to sort;
it remembers that choice for next time, and creates <folder>/sorted/
automatically. Run it, it opens your browser to a local page where you
sort photos into categories with click-only, modern UI.

Multiplayer: the server listens on your whole WiFi network (not just this
PC), so other people on the same network can open the LAN URL printed at
startup on their own phone/laptop and sort in parallel. The host sets
"how many people are sorting" once; every other device that opens the
link gets auto-assigned the next free player number, no picking required.
The photo list is split into that many equal sections so nobody works on
the same photo twice. No login/auth -- anyone on your WiFi who has the
URL can sort or delete, same as you.

Usage:
    python web_sorter.py
"""
import os
import io
import json
import shutil
import socket
import threading
import webbrowser

from flask import Flask, jsonify, request, Response
from PIL import Image, ImageOps

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_FOLDER_FILE = os.path.join(APP_DIR, "last_folder.json")
TRASH_CATEGORY = "trash"
PORT = 8765

# these describe whichever folder is currently being sorted -- None until a
# folder is picked (or the last one is restored). Reassigned by
# set_active_folder(); every other function just reads these bare globals,
# so nothing else needs to change when the active folder changes.
SRC = None
SORTED_DIR = None
PROGRESS_FILE = None
SKIPPED_FILE = None
ROOM_FILE = None


def set_active_folder(path):
    global SRC, SORTED_DIR, PROGRESS_FILE, SKIPPED_FILE, ROOM_FILE
    SRC = os.path.abspath(path)
    SORTED_DIR = os.path.join(SRC, "sorted")
    PROGRESS_FILE = os.path.join(SORTED_DIR, "manual_progress.json")
    SKIPPED_FILE = os.path.join(SORTED_DIR, "skipped.json")
    ROOM_FILE = os.path.join(SORTED_DIR, "multiplayer_room.json")
    os.makedirs(SORTED_DIR, exist_ok=True)
    try:
        with open(LAST_FOLDER_FILE, "w", encoding="utf-8") as f:
            json.dump({"path": SRC}, f)
    except Exception:
        pass


def _restore_last_folder():
    if os.path.exists(LAST_FOLDER_FILE):
        try:
            with open(LAST_FOLDER_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f).get("path")
            if saved and os.path.isdir(saved):
                set_active_folder(saved)
        except Exception:
            pass


_restore_last_folder()

_bat_path = os.path.join(APP_DIR, "run_sorter.bat")
try:
    with open(_bat_path, "w", encoding="utf-8") as _f:
        _f.write(
            "@echo off\r\n"
            "title Event Photo Sorter\r\n"
            'python "%~dp0web_sorter.py"\r\n'
            "pause\r\n"
        )
except Exception:
    pass

app = Flask(__name__)
state_lock = threading.Lock()


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_progress(progress):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def load_skipped():
    if os.path.exists(SKIPPED_FILE):
        with open(SKIPPED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_skipped(skipped):
    with open(SKIPPED_FILE, "w") as f:
        json.dump(sorted(skipped), f, indent=2)


def load_room():
    if os.path.exists(ROOM_FILE):
        try:
            with open(ROOM_FILE, "r") as f:
                room = json.load(f)
            room.setdefault("sections", 1)
            room.setdefault("assignments", {})
            room.setdefault("last_assigned", {})
            return room
        except Exception:
            pass
    return {"sections": 1, "assignments": {}, "last_assigned": {}}


def save_room(room):
    with open(ROOM_FILE, "w") as f:
        json.dump(room, f, indent=2)


def list_files():
    return sorted(
        f for f in os.listdir(SRC)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    )


def fast_open(path, target_size):
    img = Image.open(path)
    try:
        img.draft("RGB", target_size)
    except Exception:
        pass
    return ImageOps.exif_transpose(img)


def existing_categories(progress):
    cats = []
    if os.path.exists(SORTED_DIR):
        cats = [d for d in os.listdir(SORTED_DIR)
                if os.path.isdir(os.path.join(SORTED_DIR, d))]
    counts = {}
    for c in progress.values():
        counts[c] = counts.get(c, 0) + 1
    cats = [c for c in cats if counts.get(c, 0) > 0]
    if TRASH_CATEGORY not in cats:
        cats.append(TRASH_CATEGORY)  # always shown, even empty

    def sort_key(name):
        if name == TRASH_CATEGORY:
            return (3, 0)  # always last
        if name.startswith("people_"):
            try:
                return (0, int(name.split("_")[1]))
            except (IndexError, ValueError):
                pass
        if name == "atmosphere":
            return (1, 0)
        return (2, name)
    return sorted(cats, key=sort_key), counts


def category_last_photo(progress, cat):
    last = None
    for fname, c in progress.items():
        if c == cat:
            last = fname
    return last


def next_new_person_number(progress):
    cats, _ = existing_categories(progress)
    nums = []
    for name in cats:
        if name.startswith("people_"):
            try:
                nums.append(int(name.split("_")[1]))
            except (IndexError, ValueError):
                pass
    return (max(nums) + 1) if nums else 1


def sanitize_name(name):
    invalid = '<>:"/\\|?*'
    return "".join(ch for ch in (name or "").strip() if ch not in invalid)


def first_undecided(files, progress, skipped=None):
    skipped = skipped or set()
    for i, f in enumerate(files):
        if f not in progress and f not in skipped:
            return i, False
    for i, f in enumerate(files):
        if f not in progress:
            return i, True
    return len(files), False


def get_partition(args_source):
    try:
        sections = max(1, min(16, int(args_source.get("sections", 1))))
    except (TypeError, ValueError):
        sections = 1
    try:
        section = max(1, min(sections, int(args_source.get("section", 1))))
    except (TypeError, ValueError):
        section = 1
    return section, sections


def partition_slice(files, section, sections):
    n = len(files)
    if sections <= 1:
        return files
    base, rem = divmod(n, sections)
    start = 0
    for i in range(1, section):
        start += base + (1 if i <= rem else 0)
    length = base + (1 if section <= rem else 0)
    return files[start:start + length]


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def get_local_addresses():
    # every address this machine could be reached at, including its own
    # LAN-facing IP -- the host's browser gets auto-opened to that address,
    # not 127.0.0.1, so "am I the host" has to recognize both
    addrs = {"127.0.0.1", "::1", get_lan_ip()}
    try:
        addrs.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except Exception:
        pass
    return addrs


LOCAL_ADDRESSES = get_local_addresses()


@app.route("/api/server_info")
def api_server_info():
    return jsonify({"lan_url": f"http://{get_lan_ip()}:{PORT}"})


@app.route("/api/current_folder")
def api_current_folder():
    return jsonify({"folder": SRC})


@app.route("/api/pick_folder", methods=["POST"])
def api_pick_folder():
    # opens a native folder-picker dialog ON THE HOST MACHINE -- only makes
    # sense (and is only allowed) from the device actually running the server
    if request.remote_addr not in LOCAL_ADDRESSES:
        return jsonify({"ok": False, "error": "Only the host machine can pick a folder"}), 403
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Pick the folder of photos to sort")
        root.destroy()
    except Exception as e:
        return jsonify({"ok": False, "error": f"Couldn't open folder picker: {e}"}), 500
    if not path:
        return jsonify({"ok": False, "error": "cancelled"})
    with state_lock:
        set_active_folder(path)
    return jsonify({"ok": True, "path": SRC})


# ---------------- API ----------------

@app.route("/api/state")
def api_state():
    if SRC is None:
        return jsonify({"no_folder": True, "current": None, "total": 0, "reviewed": 0,
                         "section_total": 0, "section_reviewed": 0, "upcoming": [], "categories": []})
    section, sections = get_partition(request.args)
    with state_lock:
        files = list_files()
        progress = load_progress()
        skipped = load_skipped()
        my_files = partition_slice(files, section, sections)

        idx, revisiting = first_undecided(my_files, progress, skipped)
        if revisiting and skipped:
            skipped = set()
            save_skipped(skipped)
            idx, _ = first_undecided(my_files, progress, skipped)

        cats, counts = existing_categories(progress)
        current = my_files[idx] if idx < len(my_files) else None
        upcoming = [f for f in my_files[idx + 1:] if f not in progress][:5] if idx < len(my_files) else []
        section_total = len(my_files)
        section_reviewed = sum(1 for f in my_files if f in progress)

        return jsonify({
            "current": current,
            "total": len(files),
            "reviewed": len(progress),
            "section_total": section_total,
            "section_reviewed": section_reviewed,
            "upcoming": upcoming,
            "categories": [
                {"name": c, "count": counts.get(c, 0),
                 "thumb": category_last_photo(progress, c)}
                for c in cats
            ],
        })


@app.route("/api/undecided_list")
def api_undecided_list():
    section, sections = get_partition(request.args)
    try:
        limit = max(1, min(500, int(request.args.get("limit", 150))))
    except (TypeError, ValueError):
        limit = 150
    with state_lock:
        files = list_files()
        progress = load_progress()
        my_files = partition_slice(files, section, sections)
        undecided = [f for f in my_files if f not in progress][:limit]
    return jsonify({"files": undecided})


@app.route("/api/photo/<path:fname>")
def api_photo(fname):
    fname = os.path.basename(fname)
    path = os.path.join(SRC, fname)
    if not os.path.exists(path):
        return "not found", 404
    try:
        size = int(request.args.get("size", 1600))
    except ValueError:
        size = 1600
    img = fast_open(path, (size, size))
    img.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    buf.seek(0)
    resp = Response(buf.read(), mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def _format_exposure(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return f"1/{round(1 / f)}s" if f < 1 else f"{f:g}s"


def _format_fnumber(v):
    try:
        return f"f/{float(v):.1f}"
    except (TypeError, ValueError):
        return None


def _format_focal(v):
    try:
        return f"{float(v):.0f}mm"
    except (TypeError, ValueError):
        return None


def get_photo_info(path):
    info = {
        "width": None, "height": None, "size_bytes": None,
        "date": None, "camera": None, "iso": None,
        "exposure": None, "fnumber": None, "focal_length": None,
    }
    try:
        info["size_bytes"] = os.path.getsize(path)
    except OSError:
        pass
    try:
        img = Image.open(path)
        info["width"], info["height"] = img.size
        exif = img.getexif()
        make = exif.get(0x010F)
        model = exif.get(0x0110)
        if model:
            model = str(model).strip()
            info["camera"] = f"{str(make).strip()} {model}".strip() if make else model

        try:
            exif_ifd = exif.get_ifd(0x8769)
        except Exception:
            exif_ifd = {}

        date = exif_ifd.get(0x9003) or exif.get(0x0132)
        if date:
            info["date"] = str(date).replace(":", "-", 2)

        iso = exif_ifd.get(0x8827)
        if iso is not None:
            info["iso"] = iso[0] if isinstance(iso, (tuple, list)) else iso

        exposure = exif_ifd.get(0x829A)
        if exposure is not None:
            info["exposure"] = _format_exposure(exposure)

        fnumber = exif_ifd.get(0x829D)
        if fnumber is not None:
            info["fnumber"] = _format_fnumber(fnumber)

        focal = exif_ifd.get(0x920A)
        if focal is not None:
            info["focal_length"] = _format_focal(focal)
    except Exception:
        pass
    return info


@app.route("/api/photo_info/<path:fname>")
def api_photo_info(fname):
    fname = os.path.basename(fname)
    path = os.path.join(SRC, fname)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    return jsonify(get_photo_info(path))


def _do_assign(fname, category):
    cat_dir = os.path.join(SORTED_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    shutil.copy2(os.path.join(SRC, fname), os.path.join(cat_dir, fname))
    progress = load_progress()
    progress[fname] = category
    save_progress(progress)
    skipped = load_skipped()
    if fname in skipped:
        skipped.discard(fname)
        save_skipped(skipped)


@app.route("/api/assign", methods=["POST"])
def api_assign():
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    section, sections = get_partition(data)
    if not category:
        return jsonify({"ok": False, "error": "empty category"}), 400
    with state_lock:
        files = list_files()
        progress = load_progress()
        skipped = load_skipped()
        my_files = partition_slice(files, section, sections)
        idx, revisiting = first_undecided(my_files, progress, skipped)
        if revisiting and skipped:
            skipped = set()
            save_skipped(skipped)
            idx, _ = first_undecided(my_files, progress, skipped)
        if idx >= len(my_files):
            return jsonify({"ok": False, "error": "nothing to assign"}), 400
        fname = my_files[idx]
        try:
            _do_assign(fname, category)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        # persisted (not just in memory) so a server restart -- e.g. the
        # host closing and reopening the terminal -- doesn't scramble whose
        # Undo undoes what, or lose anyone's spot in the room
        room = load_room()
        room["last_assigned"][f"{section}:{sections}"] = fname
        save_room(room)
    return jsonify({"ok": True})


@app.route("/api/bulk_assign", methods=["POST"])
def api_bulk_assign():
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    files_in = [os.path.basename(f) for f in data.get("files", [])]
    if not category or not files_in:
        return jsonify({"ok": False, "error": "missing category or files"}), 400
    with state_lock:
        cat_dir = os.path.join(SORTED_DIR, category)
        os.makedirs(cat_dir, exist_ok=True)
        progress = load_progress()
        count = 0
        for fname in files_in:
            src = os.path.join(SRC, fname)
            if not os.path.exists(src):
                continue
            try:
                shutil.copy2(src, os.path.join(cat_dir, fname))
            except Exception:
                continue
            progress[fname] = category
            count += 1
        save_progress(progress)
    return jsonify({"ok": True, "count": count})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    data = request.get_json(force=True)
    section, sections = get_partition(data)
    with state_lock:
        files = list_files()
        progress = load_progress()
        skipped = load_skipped()
        my_files = partition_slice(files, section, sections)
        idx, revisiting = first_undecided(my_files, progress, skipped)
        if revisiting and skipped:
            skipped = set()
        if idx < len(my_files):
            skipped.add(my_files[idx])
        save_skipped(skipped)
    return jsonify({"ok": True})


@app.route("/api/undo", methods=["POST"])
def api_undo():
    data = request.get_json(force=True)
    section, sections = get_partition(data)
    with state_lock:
        progress = load_progress()
        room = load_room()
        key = f"{section}:{sections}"
        fname = room["last_assigned"].pop(key, None)
        save_room(room)
        if not fname or fname not in progress:
            # fall back to the globally most recent assignment
            if not progress:
                return jsonify({"ok": False, "error": "nothing to undo"}), 400
            fname = list(progress.keys())[-1]
        category = progress.pop(fname)
        path = os.path.join(SORTED_DIR, category, fname)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        save_progress(progress)
    return jsonify({"ok": True, "restored": fname})


@app.route("/api/rename", methods=["POST"])
def api_rename():
    data = request.get_json(force=True)
    old_name = data.get("old", "")
    new_name = sanitize_name(data.get("new", ""))
    if not new_name or new_name == old_name:
        return jsonify({"ok": False, "error": "invalid name"}), 400
    with state_lock:
        old_dir = os.path.join(SORTED_DIR, old_name)
        new_dir = os.path.join(SORTED_DIR, new_name)
        os.makedirs(new_dir, exist_ok=True)
        if os.path.isdir(old_dir):
            for entry in os.listdir(old_dir):
                s = os.path.join(old_dir, entry)
                d = os.path.join(new_dir, entry)
                if not os.path.exists(d):
                    try:
                        shutil.move(s, d)
                    except Exception:
                        pass
            try:
                os.rmdir(old_dir)
            except OSError:
                pass
        progress = load_progress()
        for fname in list(progress.keys()):
            if progress[fname] == old_name:
                progress[fname] = new_name
        save_progress(progress)
    return jsonify({"ok": True})


@app.route("/api/delete_category", methods=["POST"])
def api_delete_category():
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    with state_lock:
        path = os.path.join(SORTED_DIR, category)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        progress = load_progress()
        for fname in list(progress.keys()):
            if progress[fname] == category:
                del progress[fname]
        save_progress(progress)
    return jsonify({"ok": True})


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    path = os.path.join(SORTED_DIR, category)
    os.makedirs(path, exist_ok=True)
    try:
        os.startfile(path)  # only meaningful when opened from the host machine
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sync", methods=["POST"])
def api_sync():
    with state_lock:
        progress = load_progress()
        changed = False
        for fname, cat in list(progress.items()):
            if not os.path.exists(os.path.join(SORTED_DIR, cat, fname)):
                del progress[fname]
                changed = True
        if changed:
            save_progress(progress)
    return jsonify({"ok": True, "changed": changed})


@app.route("/api/clear_all", methods=["POST"])
def api_clear_all():
    if request.remote_addr not in LOCAL_ADDRESSES:
        return jsonify({"ok": False, "error": "Clear All can only be run from the host machine"}), 403
    with state_lock:
        if os.path.exists(SORTED_DIR):
            for entry in os.listdir(SORTED_DIR):
                full = os.path.join(SORTED_DIR, entry)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                elif entry != "manual_progress.json":
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        save_progress({})
        save_skipped(set())
        room = load_room()
        room["last_assigned"] = {}
        save_room(room)
    return jsonify({"ok": True})


@app.route("/api/next_person_number")
def api_next_person_number():
    progress = load_progress()
    return jsonify({"n": next_new_person_number(progress)})


@app.route("/api/join", methods=["POST"])
def api_join():
    # Assigns (or re-fetches) a player/section number for this browser.
    # Only the host machine (127.0.0.1) may set the room size; every other
    # device just gets auto-assigned the next free slot -- no manual picking.
    data = request.get_json(force=True)
    client_id = str(data.get("client_id") or "")[:64]
    requested_sections = data.get("requested_sections")
    is_actual_host = request.remote_addr in LOCAL_ADDRESSES

    with state_lock:
        room = load_room()

        if requested_sections is not None and is_actual_host:
            try:
                n = max(1, min(16, int(requested_sections)))
                room["sections"] = n
            except (TypeError, ValueError):
                pass

        sections = room["sections"]
        assigned = room["assignments"].get(client_id)

        if assigned is None or assigned > sections:
            used = {v for k, v in room["assignments"].items() if k != client_id}
            candidate = next((i for i in range(1, sections + 1) if i not in used), None)
            if candidate is None:
                # genuinely no free slot left -- say so, don't silently
                # double someone up (that was the old bug: sharing a slot
                # via modulo always landed everyone on slot 1 when
                # sections==1, since anything % 1 is 0)
                save_room(room)
                return jsonify({
                    "room_full": True,
                    "sections": sections,
                    "is_host": is_actual_host,
                })
            room["assignments"][client_id] = candidate
            assigned = candidate

        save_room(room)

    return jsonify({
        "room_full": False,
        "section": assigned,
        "sections": room["sections"],
        "is_host": is_actual_host,
    })


@app.route("/api/sections_progress")
def api_sections_progress():
    try:
        sections = max(1, min(16, int(request.args.get("sections", 1))))
    except (TypeError, ValueError):
        sections = 1
    with state_lock:
        files = list_files()
        progress = load_progress()
        skipped = load_skipped()
        result = []
        for sec in range(1, sections + 1):
            my_files = partition_slice(files, sec, sections)
            total = len(my_files)
            reviewed = sum(1 for f in my_files if f in progress)
            idx, _ = first_undecided(my_files, progress, skipped)
            current = my_files[idx] if idx < len(my_files) else None
            result.append({
                "section": sec, "total": total, "reviewed": reviewed, "current": current
            })
    return jsonify({
        "sections": result,
        "grand_total": len(files),
        "grand_reviewed": len(progress),
    })


# ---------------- frontend ----------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Event Photo Sorter</title>
<script>
(function () {
  var saved = localStorage.getItem('theme');
  var theme = saved || ((window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light');
  document.documentElement.setAttribute('data-bs-theme', theme);
})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css">
<style>
  /* ---------- design tokens: navy-blue brand, light + dark ---------- */
  :root {
    --bg: #f3f5fa; --panel: #ffffff; --card: #eef1f8; --border: #dde3ef;
    --text: #10192b; --text-muted: #57647c;
    --navy-1: #004397; --navy-2: #0060d0; --accent: #0060d0;
    --track: rgba(16, 25, 43, .08);
    --photo-bg: #05070c;
    --font-ui: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
    --font-mono: 'JetBrains Mono', ui-monospace, 'SFMono-Regular', monospace;
    --r-lg: 16px; --r-md: 12px; --r-sm: 9px; --r-xs: 7px;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-bs-theme="light"]) {
      --bg: #0a0e16; --panel: #121826; --card: #1a2233; --border: #232c40;
      --text: #e6eaf2; --text-muted: #8b96ab;
      --navy-1: #0a3d91; --navy-2: #2f7bf0; --accent: #4fa3ff;
      --track: rgba(255, 255, 255, .08);
    }
  }
  :root[data-bs-theme="dark"] {
    --bg: #0a0e16; --panel: #121826; --card: #1a2233; --border: #232c40;
    --text: #e6eaf2; --text-muted: #8b96ab;
    --navy-1: #0a3d91; --navy-2: #2f7bf0; --accent: #4fa3ff;
    --track: rgba(255, 255, 255, .08);
  }

  * { font-variant-numeric: tabular-nums; }
  body { background: var(--bg); color: var(--text); font-family: var(--font-ui); }
  h1, h2, h3, h4, h5, h6, .modal-title { font-family: var(--font-ui); font-weight: 800; letter-spacing: -.01em; }
  code, .mono { font-family: var(--font-mono); }

  /* ---------- gradient button system ---------- */
  .btn {
    font-family: var(--font-ui); font-weight: 600; border: none; border-radius: var(--r-sm);
    transition: transform .15s ease, box-shadow .15s ease, filter .15s ease;
  }
  .btn:hover { transform: translateY(-1px); }
  .btn:active { transform: translateY(0); }
  .btn-sm { border-radius: var(--r-xs); }
  .btn-primary { background: linear-gradient(135deg, var(--navy-1), var(--navy-2)) !important; color: #fff !important; box-shadow: 0 2px 8px rgba(0, 67, 151, .25) !important; }
  .btn-primary:hover { box-shadow: 0 4px 14px rgba(0, 67, 151, .35) !important; }
  .btn-success { background: linear-gradient(135deg, #0d6b3f, #17a866) !important; color: #fff !important; box-shadow: 0 2px 8px rgba(13, 107, 63, .25) !important; }
  .btn-warning { background: linear-gradient(135deg, #9a5c0a, #d98d1c) !important; color: #fff !important; box-shadow: 0 2px 8px rgba(154, 92, 10, .25) !important; }
  .btn-outline-warning { background: var(--card) !important; border: 1.5px solid var(--border) !important; color: var(--text) !important; box-shadow: none !important; }
  .btn-outline-warning:hover { background: var(--border) !important; }
  .btn-info { background: linear-gradient(135deg, #0a6478, #14a3c4) !important; color: #fff !important; box-shadow: 0 2px 8px rgba(10, 100, 120, .25) !important; }
  .btn-danger { background: linear-gradient(135deg, #7d1d1d, #c22e2e) !important; color: #fff !important; box-shadow: 0 2px 8px rgba(125, 29, 29, .25) !important; }
  .btn-secondary { background: linear-gradient(135deg, #394254, #565f76) !important; color: #fff !important; box-shadow: 0 2px 8px rgba(57, 66, 84, .2) !important; }
  .btn-outline-light { background: var(--card) !important; border: 1.5px solid var(--border) !important; color: var(--text) !important; box-shadow: none !important; }
  .btn-outline-light:hover { background: var(--border) !important; }
  .btn.is-cooling { opacity: .55; pointer-events: none; }

  /* ---------- layout ---------- */
  #stickyTop {
    position: sticky; top: 0; z-index: 1030;
    background: var(--bg);
    padding-top: 1rem; margin-top: -1rem;
    padding-bottom: .5rem;
  }
  #layoutFlex { display: flex; flex-direction: column; }
  #categoryZone { order: 1; min-width: 0; }
  #previewZone { order: 2; margin-top: .5rem; min-width: 0; }
  #layoutFlex.layout-swapped #categoryZone { order: 2; margin-top: .5rem; }
  #layoutFlex.layout-swapped #previewZone { order: 1; margin-top: 0; }
  #layoutFlex.layout-side {
    flex-direction: row; align-items: stretch; gap: .5rem; flex-wrap: wrap;
  }
  #layoutFlex.layout-side #previewZone {
    order: 1; flex: 0 1 var(--side-split, 60%); margin-top: 0; min-width: 260px;
  }
  #layoutFlex.layout-side #categoryZone {
    order: 3; flex: 1 1 200px; margin-top: 0; max-width: none;
    display: flex; flex-direction: column;
  }
  #sideResizeHandle { display: none; }
  #layoutFlex.layout-side #sideResizeHandle {
    display: flex; align-items: center; justify-content: center;
    order: 2; width: 14px; align-self: stretch; cursor: ew-resize; touch-action: none;
    background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-xs);
    color: var(--text-muted); font-size: .75rem;
  }
  #sideResizeHandle:hover, #sideResizeHandle.dragging-active {
    background: var(--border); color: var(--accent);
  }
  /* action buttons move to the top of the side column, right-aligned */
  #layoutFlex.layout-side #categoryZone .toolbar {
    order: -1; justify-content: flex-end; margin-bottom: .75rem;
  }
  /* in the narrow side-column, ms-auto's "push to the far right" doesn't
     read well once the toolbar wraps -- let it sit inline like every other button */
  #layoutFlex.layout-side .toolbar .btn.ms-auto { margin-left: 0; }
  /* the strip becomes a wrapping multi-row grid instead of one scrolling row */
  #layoutFlex.layout-side #stripOuter {
    max-height: 60vh; overflow-y: auto; overflow-x: hidden; white-space: normal;
  }
  :root { --photo-h: 58vh; }
  #photoWrap {
    height: var(--photo-h); position: relative;
    display: flex; align-items: center; justify-content: center;
    background: var(--photo-bg); border-radius: var(--r-lg); overflow: hidden; cursor: zoom-in;
  }
  #photoWrap img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .photo-zoom-controls {
    position: absolute; top: 10px; right: 10px; z-index: 5;
    display: flex; gap: 4px; opacity: .75; transition: opacity .15s ease;
  }
  .photo-zoom-controls:hover { opacity: 1; }
  .photo-zoom-controls .btn { padding: .3rem .55rem; backdrop-filter: blur(4px); }
  #stripOuter {
    background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-lg);
    overflow-x: auto; overflow-y: hidden; white-space: nowrap; padding: .75rem;
  }
  #stripOuter::-webkit-scrollbar { height: 8px; }
  #stripOuter::-webkit-scrollbar-thumb { background: var(--border); border-radius: 4px; }
  :root { --cat-card-w: 130px; --cat-thumb-h: 90px; }
  .cat-card {
    display: inline-block; vertical-align: top; width: var(--cat-card-w);
    background: var(--card); border: 1px solid var(--border); border-radius: var(--r-md); padding: .5rem;
    margin-right: .6rem; cursor: pointer; transition: transform .12s ease, box-shadow .12s ease;
    white-space: normal;
  }
  .cat-card:hover { transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0, 67, 151, .18); }
  .cat-card.drag-over { outline: 2px dashed var(--accent); }
  .cat-card img { width: 100%; height: var(--cat-thumb-h); object-fit: cover; border-radius: var(--r-xs); background: #000; }
  .cat-card .noimg { width: 100%; height: var(--cat-thumb-h); display: flex; align-items: center; justify-content: center; background: var(--track); border-radius: var(--r-xs); color: var(--text-muted); }
  .cat-card.is-cooling { opacity: .55; pointer-events: none; }
  #stripResizeHandle {
    height: 10px; margin: 2px 4px 8px; border-radius: 5px; background: var(--panel);
    border: 1px solid var(--border); cursor: ns-resize; position: relative;
  }
  #stripResizeHandle::after {
    content: ''; position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%);
    width: 36px; height: 4px; border-radius: 2px; background: var(--border);
  }
  #stripResizeHandle:hover::after { background: var(--accent); }
  .cat-card .cat-label { font-size: .8rem; font-weight: 700; margin: .4rem 0 .3rem; text-align: center; }
  .cat-card .mini-btns { display: flex; gap: .25rem; }
  .cat-card .mini-btns .btn { flex: 1; font-size: .68rem; padding: .2rem; border-radius: var(--r-xs); }
  #statusBar { color: var(--accent); font-weight: 600; min-height: 1.4em; }
  #photoDetails { font-family: var(--font-mono); color: var(--text-muted); }
  #photoDetails span { background: var(--card); border: 1px solid var(--border); border-radius: var(--r-xs); padding: .15rem .55rem; }
  .badge-progress { font-size: .85rem; font-family: var(--font-mono); }
  #progressBarWrap, .mini-bar-wrap { height: 10px; border-radius: 5px; background: var(--track); overflow: hidden; }
  #progressBarFill, .mini-bar-fill { height: 100%; background: linear-gradient(90deg, var(--navy-1), var(--navy-2)); transition: width .25s ease; }
  #progressBarWrap { position: relative; height: 18px; }
  #progressBarPct {
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
    font-family: var(--font-mono); font-size: .68rem; font-weight: 700; color: var(--text);
    text-shadow: 0 1px 2px rgba(0, 0, 0, .5);
  }
  .player-pill {
    font-family: var(--font-mono); font-size: .7rem; font-weight: 700; color: #fff;
    border-radius: var(--r-xs); padding: .15rem .5rem;
  }
  .player-pill.is-me { outline: 2px solid var(--text); outline-offset: 1px; }
  #hoverPreview { display: none; position: fixed; z-index: 2000; pointer-events: none; }
  #hoverPreview img {
    max-width: 340px; max-height: 340px; border-radius: var(--r-md);
    box-shadow: 0 10px 30px rgba(0, 0, 0, .5); border: 2px solid var(--accent);
  }
  :root { --bulk-thumb-w: 110px; --bulk-thumb-h: 90px; }
  .bulk-thumb { position: relative; display: inline-block; width: var(--bulk-thumb-w); margin: 4px; vertical-align: top; }
  .bulk-thumb img { width: 100%; height: var(--bulk-thumb-h); object-fit: cover; border-radius: var(--r-xs); cursor: pointer; }
  .bulk-thumb.selected img { outline: 3px solid var(--accent); }
  .bulk-thumb .chk { position: absolute; top: 4px; left: 4px; }
  #bulkGrid { max-height: 50vh; overflow-y: auto; background: var(--panel); border: 1px solid var(--border); border-radius: var(--r-lg); padding: .5rem; touch-action: pan-y; }
  #bulkGrid.dragging { touch-action: none; user-select: none; overflow-y: hidden; }
  #bulkSelectionBox {
    display: none; position: fixed; z-index: 1500; pointer-events: none;
    background: rgba(0, 96, 208, .18); border: 2px solid var(--accent); border-radius: 4px;
  }
  .mp-segmented {
    display: flex; height: 26px; border-radius: 8px; overflow: hidden;
    background: var(--track); border: 1px solid var(--border);
  }
  .mp-segmented .seg {
    height: 100%; transition: width .4s ease; display: flex; align-items: center;
    justify-content: center; font-family: var(--font-mono); font-size: .68rem; color: #fff; font-weight: 700;
    white-space: nowrap; overflow: hidden;
  }
  .mp-card {
    background: var(--card); border: 1px solid var(--border); border-radius: var(--r-md); padding: .6rem .8rem;
    min-width: 170px; flex: 1 1 170px; border-left: 4px solid;
  }
  .mp-card .mp-bar-wrap { height: 8px; border-radius: 4px; background: var(--track); overflow: hidden; margin-top: .4rem; }
  .mp-card .mp-bar-fill { height: 100%; transition: width .4s ease; }

  /* bootstrap surface polish to match the token system */
  .modal-content { background: var(--panel); color: var(--text); border-radius: var(--r-lg); border: 1px solid var(--border); }
  .form-control, .form-select { background: var(--card); color: var(--text); border-color: var(--border); border-radius: var(--r-xs); }
  .form-control:focus, .form-select:focus { background: var(--card); color: var(--text); border-color: var(--accent); box-shadow: 0 0 0 .2rem rgba(0, 96, 208, .15); }
  /* the closed <select> box honors the CSS above, but browsers often render
     the expanded native option list with their own black-on-white regardless
     -- color-scheme tells the browser which native-widget palette to use,
     and styling <option> directly covers browsers that still ignore that */
  :root { color-scheme: light; }
  :root[data-bs-theme="dark"] { color-scheme: dark; }
  .form-select option { background: var(--card); color: var(--text); }
  .badge { border-radius: var(--r-xs); font-weight: 600; }
</style>
</head>
<body class="p-3">
<div class="container-fluid">
  <div id="stickyTop">
    <div class="d-flex justify-content-between align-items-center mb-2 flex-wrap gap-2">
      <h4 class="m-0"><i class="bi bi-images"></i> Event Photo Sorter</h4>
      <div class="d-flex align-items-center gap-2">
        <span id="sectionBadge" class="badge text-bg-dark badge-progress d-none"></span>
        <span id="myProgressBadge" class="badge text-bg-info badge-progress d-none"></span>
        <span id="progressBadge" class="badge text-bg-secondary badge-progress">-- / --</span>
        <span id="sessionTimer" class="badge text-bg-dark badge-progress" title="Time since you opened this page"><i class="bi bi-stopwatch"></i> 00:00:00</span>
        <button id="multiplayerBtn" class="btn btn-sm btn-outline-light" data-bs-toggle="modal" data-bs-target="#multiplayerModal" title="Multiplayer progress">
          <i class="bi bi-people-fill"></i>
        </button>
        <button id="bulkModeBtn" class="btn btn-sm btn-outline-light" onclick="toggleBulkMode()" title="Bulk select mode">
          <i class="bi bi-grid-3x3-gap"></i>
        </button>
        <button class="btn btn-sm btn-outline-light" data-bs-toggle="modal" data-bs-target="#settingsModal" title="Settings">
          <i class="bi bi-gear"></i>
        </button>
        <button class="btn btn-sm btn-outline-light" data-bs-toggle="modal" data-bs-target="#helpModal" title="Help">
          <i class="bi bi-question-circle"></i>
        </button>
        <button class="btn btn-sm btn-outline-light" onclick="toggleTheme()" title="Toggle light/dark theme">
          <i class="bi bi-moon-stars" id="themeIcon"></i>
        </button>
      </div>
    </div>

    <div id="progressBarWrap" class="mb-3">
      <div id="progressBarFill" style="width:0%"></div>
      <span id="progressBarPct"></span>
    </div>
  </div>

  <div id="layoutFlex">
    <div id="categoryZone">
      <div id="stripOuter" class="mb-1"></div>
      <div id="stripResizeHandle" class="mb-2" title="Drag to resize the category cards"></div>

      <div class="toolbar d-flex flex-wrap gap-2 mb-3">
        <button class="btn btn-warning" onclick="customCategory()"><i class="bi bi-plus-circle"></i> Custom category...</button>
        <button class="btn btn-outline-light" onclick="toggleLayout()" title="Switch layout (Preview & Category)">
          <i class="bi bi-arrow-down-up"></i> Switch layout
        </button>
        <button class="btn btn-info" onclick="syncFromDisk()"><i class="bi bi-arrow-repeat"></i> Sync from disk</button>
        <button class="btn btn-secondary" onclick="skip()"><i class="bi bi-skip-forward"></i> Skip</button>
        <button class="btn btn-outline-warning" onclick="undo()"><i class="bi bi-arrow-counterclockwise"></i> Undo</button>
        <button id="clearAllBtn" class="btn btn-danger ms-auto d-none" onclick="clearAll()"><i class="bi bi-trash3"></i> Clear All</button>
      </div>
    </div>

    <div id="sideResizeHandle" title="Drag to resize"><i class="bi bi-arrow-left-right"></i></div>

    <div id="previewZone">
      <div id="singleView">
        <div id="photoWrap" class="mb-2">
          <img id="photo" src="" alt="" draggable="true" onclick="openLightbox()">
          <div id="doneMsg" class="text-center text-muted d-none">
            <i class="bi bi-check2-circle" style="font-size:3rem;"></i>
            <div class="mt-2 fs-5">All photos reviewed (in your section)!</div>
          </div>
          <div class="photo-zoom-controls">
            <button class="btn btn-sm btn-outline-light" onclick="changePhotoSize(-6)" title="Smaller preview"><i class="bi bi-dash-lg"></i></button>
            <button class="btn btn-sm btn-outline-light" onclick="changePhotoSize(6)" title="Bigger preview"><i class="bi bi-plus-lg"></i></button>
          </div>
        </div>
      </div>

      <div id="bulkView" class="d-none mb-2">
        <div class="d-flex flex-wrap gap-2 mb-2 align-items-center">
          <span class="small text-muted">Click photos to select, then either click a category card up top or use the dropdown here:</span>
          <span id="bulkSelectedCount" class="badge text-bg-info badge-progress">0 selected</span>
          <select id="bulkCategorySelect" class="form-select form-select-sm" style="width:auto;"></select>
          <button class="btn btn-sm btn-primary" onclick="bulkAssignSelected()"><i class="bi bi-check2-all"></i> Assign selected</button>
          <button class="btn btn-sm btn-outline-warning" onclick="deselectAllBulk()"><i class="bi bi-x-circle"></i> Deselect all</button>
          <button class="btn btn-sm btn-outline-light" onclick="loadBulkGrid()"><i class="bi bi-arrow-clockwise"></i> Refresh</button>
          <button class="btn btn-sm btn-outline-light" onclick="changeBulkThumbSize(-20)" title="Smaller thumbnails"><i class="bi bi-dash-lg"></i></button>
          <button class="btn btn-sm btn-outline-light" onclick="changeBulkThumbSize(20)" title="Bigger thumbnails"><i class="bi bi-plus-lg"></i></button>
        </div>
        <div id="bulkGrid"></div>
        <div id="bulkSelectionBox"></div>
      </div>

      <div id="photoDetails" class="small d-flex flex-wrap gap-2 mb-2"></div>
    </div>
  </div>

  <div id="statusBar" class="small"></div>
</div>

<div id="hoverPreview"><img id="hoverPreviewImg" src=""></div>

<!-- Pick-folder modal: shown until a photo folder is chosen -->
<div class="modal" id="pickFolderModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
  <div class="modal-dialog">
    <div class="modal-content text-center">
      <div class="modal-header border-0 justify-content-center">
        <h5 class="modal-title">👋 Hi!</h5>
      </div>
      <div class="modal-body pb-4">
        <p>Pick the folder of photos you want to sort. A <code>sorted/</code> folder will be created inside
          it automatically, and this choice is remembered for next time.</p>
        <button class="btn btn-primary" onclick="choosePhotoFolder()">
          <i class="bi bi-folder2-open"></i> Choose Folder
        </button>
        <div id="pickFolderStatus" class="small text-muted mt-3"></div>
        <div class="small text-muted mt-2">If you're not the host, hang tight -- this'll continue automatically
          once they pick a folder.</div>
      </div>
    </div>
  </div>
</div>

<!-- Host setup modal: only the host can set room size -->
<div class="modal" id="setupModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-people"></i> Who's sorting?</h5>
      </div>
      <div class="modal-body">
        <p class="small text-muted">If more than one of you is sorting on different devices over the same WiFi,
          the photo list gets split so nobody works on the same photo. Just you? Leave it at 1. Everyone else who
          opens the link gets assigned a player number automatically -- they don't pick it themselves.</p>
        <label class="form-label">How many people total (including you)?</label>
        <input type="number" id="setupSections" class="form-control" min="1" max="16" value="1">
      </div>
      <div class="modal-footer">
        <button class="btn btn-primary" onclick="confirmSetup()">Start sorting</button>
      </div>
    </div>
  </div>
</div>

<!-- Non-host welcome modal: auto-assigned, nothing to pick -->
<div class="modal" id="welcomeModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-person-check"></i> You're in!</h5>
      </div>
      <div class="modal-body text-center py-4">
        <div class="display-6 mb-2" id="welcomePlayerLabel">Player 2</div>
        <div class="text-muted small">You've been assigned automatically -- your own slice of the photos is
          ready, nobody else is working on the same ones.</div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-primary" onclick="confirmWelcome()">Start sorting</button>
      </div>
    </div>
  </div>
</div>

<!-- Room full: every slot already taken by someone else -->
<div class="modal" id="roomFullModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
  <div class="modal-dialog">
    <div class="modal-content text-center">
      <div class="modal-body py-4">
        <div class="display-6 mb-2">😱 OH NO!</div>
        <div class="fs-5 fw-bold mb-2">Max players reached!</div>
        <div class="text-muted small">Every slot in this room is already taken. Please contact the host to
          increase the room size, then try again.</div>
      </div>
      <div class="modal-footer justify-content-center">
        <button class="btn btn-primary" onclick="retryJoin()"><i class="bi bi-arrow-clockwise"></i> Try again</button>
      </div>
    </div>
  </div>
</div>

<!-- Server unreachable: host machine likely closed the app, slept, or dropped WiFi -->
<div class="modal" id="hostGoneModal" tabindex="-1" data-bs-backdrop="static" data-bs-keyboard="false">
  <div class="modal-dialog">
    <div class="modal-content text-center">
      <div class="modal-body py-4">
        <div class="display-6 mb-2">📡💀</div>
        <div class="fs-5 fw-bold mb-2">YO... the host is gone!</div>
        <div class="text-muted small">Lost connection to the server. The host's computer may have closed the
          app, gone to sleep, or dropped off the WiFi. Ask them to check, then try again.</div>
      </div>
      <div class="modal-footer justify-content-center">
        <button class="btn btn-primary" onclick="retryConnection()"><i class="bi bi-arrow-clockwise"></i> Try again</button>
      </div>
    </div>
  </div>
</div>

<!-- Lightbox -->
<div class="modal fade" id="lightboxModal" tabindex="-1">
  <div class="modal-dialog modal-fullscreen">
    <div class="modal-content bg-black">
      <div class="modal-header border-0">
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body d-flex align-items-center justify-content-center">
        <img id="lightboxImg" src="" style="max-width:100%; max-height:100%; object-fit:contain;">
      </div>
    </div>
  </div>
</div>

<!-- Multiplayer progress modal -->
<div class="modal fade" id="multiplayerModal" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">
          <i class="bi bi-people-fill"></i> Multiplayer Progress
          <span class="badge text-bg-success ms-2" id="mpLiveBadge" style="font-size:.6rem; vertical-align:middle;">
            <i class="bi bi-broadcast"></i> live
          </span>
        </h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <div id="mpHostControls" class="d-none mb-3 p-2" style="background:var(--card); border:1px solid var(--border); border-radius:var(--r-md);">
          <label class="form-label small mb-1 fw-bold">Room size (max players)</label>
          <div class="d-flex gap-2">
            <input type="number" id="mpSectionsInput" class="form-control form-control-sm" min="1" max="16" style="width:90px;">
            <button class="btn btn-sm btn-primary" onclick="saveRoomSizeFromModal()">Save</button>
          </div>
          <div class="form-text">Changing this never breaks anyone -- if someone no longer fits, they're
            reassigned automatically next time they load the page.</div>
        </div>

        <div class="d-flex flex-column flex-sm-row gap-3 align-items-center mb-3 p-2" style="background:var(--card); border:1px solid var(--border); border-radius:var(--r-md);">
          <canvas id="mpQrCanvas"></canvas>
          <div class="flex-grow-1">
            <div class="small text-muted mb-1">Scan to join from another device on this WiFi:</div>
            <div class="input-group input-group-sm">
              <input type="text" id="mpLanUrlInput" class="form-control mono" readonly>
              <button class="btn btn-outline-light" onclick="copyMpLanUrl()"><i class="bi bi-clipboard"></i></button>
            </div>
          </div>
        </div>

        <div class="mb-3">
          <div class="d-flex justify-content-between small mb-1">
            <span>Whole event, combined</span>
            <span id="mpGrandLabel">-- / -- (--%)</span>
          </div>
          <div id="mpSegmentedBar" class="mp-segmented"></div>
        </div>
        <div id="mpCards" class="d-flex flex-wrap gap-2"></div>
      </div>
    </div>
  </div>
</div>

<!-- Settings modal -->
<div class="modal fade" id="settingsModal" tabindex="-1">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-gear"></i> Settings</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <label for="cooldownInput" class="form-label">
          Click cooldown (ms) -- ignores extra clicks right after one lands, to stop misclicks/double-filing.
        </label>
        <input type="number" id="cooldownInput" class="form-control mb-3" min="0" max="2000" step="10" value="100">
        <div class="form-text mb-3">Default 100ms. Set to 0 to disable.</div>
        <button class="btn btn-outline-light btn-sm" onclick="reopenSetup()">
          <i class="bi bi-people"></i> Change multiplayer setup
        </button>
        <button class="btn btn-outline-light btn-sm" onclick="choosePhotoFolder()">
          <i class="bi bi-folder2-open"></i> Change photo folder
        </button>
        <div class="mt-3">
          <div class="small text-muted">Share this with others on your WiFi so they can sort too:</div>
          <div class="input-group mt-1">
            <input type="text" id="lanUrlInput" class="form-control form-control-sm" readonly>
            <button class="btn btn-sm btn-outline-light" onclick="copyLanUrl()"><i class="bi bi-clipboard"></i></button>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-primary" onclick="saveSettings()" data-bs-dismiss="modal">Save</button>
      </div>
    </div>
  </div>
</div>

<!-- Help modal -->
<div class="modal fade" id="helpModal" tabindex="-1">
  <div class="modal-dialog modal-lg">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-question-circle"></i> How to use Event Photo Sorter</h5>
        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <ul>
          <li>The photo on screen is the one you're deciding right now. Click it (or drag it onto a category
              card) to view full-size / file it.</li>
          <li><b>Custom category</b> makes any category you want -- type a name, e.g. "people_1" for a person
              slot, "atmosphere" for venue/no-people shots, or anything else.</li>
          <li>Every category becomes a card in the scrollable strip up top: click its thumbnail to file the
              current photo there too (hover to preview it bigger first). <b>Open</b> opens that folder in
              Windows Explorer, the pencil renames it, the trash bulk-deletes the whole category (sends every
              photo in it back to unsorted).</li>
          <li>The grid icon toggles <b>bulk mode</b> -- select several photos at once and file them together in
              one click, good for near-identical burst shots.</li>
          <li><b>Skip</b> leaves a photo for later; skipped photos come back into rotation once everything
              else is decided. <b>Undo</b> reverts your last decision.</li>
          <li><b>Sync from disk</b> reconciles progress if you deleted photos via Explorer.
              <b>Clear All</b> wipes every sorted folder + progress for everyone (only the copies -- originals
              are always safe) -- it asks for confirmation, and only works from the host PC itself, not from
              other devices on WiFi.</li>
          <li>Multiple people can sort at once from different devices on the same WiFi -- the host sets "how many
              people" once, and everyone else who opens the link gets auto-assigned a player number, no picking
              required. The people icon opens a live view of everyone's progress, updating every 2 seconds.</li>
          <li>To sort a different event: copy <code>web_sorter.py</code> into that event's photo folder and run
              it there. Everything (sorted output, progress) is regenerated fresh in whichever folder it sits in.</li>
        </ul>
      </div>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/qrcode/build/qrcode.min.js"></script>

<script>
let currentFile = null;
let cooldownUntil = 0;
let bulkMode = false;
let bulkSelected = new Set();
const preloadedUrls = new Set();
let mySection = 1, mySections = 1;
let isHost = false;
let setupModalInstance = null;
let welcomeModalInstance = null;

// ---------- theme (light/dark, navy-blue brand) ----------
function applyTheme(theme) {
  document.documentElement.setAttribute('data-bs-theme', theme);
  document.getElementById('themeIcon').className = theme === 'light' ? 'bi bi-moon-stars' : 'bi bi-sun';
  localStorage.setItem('theme', theme);
}
function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-bs-theme') || 'light';
  applyTheme(cur === 'light' ? 'dark' : 'light');
}
applyTheme(document.documentElement.getAttribute('data-bs-theme') || 'light');

// ---------- multiplayer: server-assigned player numbers ----------
function getClientId() {
  let id = localStorage.getItem('clientId');
  if (!id) {
    id = (crypto.randomUUID ? crypto.randomUUID() :
      'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
        const r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
      }));
    localStorage.setItem('clientId', id);
  }
  return id;
}

async function joinRoom(requestedSections) {
  const body = {client_id: getClientId()};
  if (requestedSections !== undefined) body.requested_sections = requestedSections;
  const r = await api('/api/join', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  isHost = r.is_host;
  if (!r.room_full) {
    mySection = r.section; mySections = r.sections;
  }
  return r;
}

async function bootMultiplayer() {
  const r = await joinRoom();
  if (r.room_full) {
    new bootstrap.Modal(document.getElementById('roomFullModal')).show();
    return;
  }
  updateSectionBadge();
  document.getElementById('clearAllBtn').classList.toggle('d-none', !isHost);
  if (isHost) {
    document.getElementById('setupSections').value = mySections;
    setupModalInstance = new bootstrap.Modal(document.getElementById('setupModal'));
    setupModalInstance.show();
  } else if (mySections > 1) {
    document.getElementById('welcomePlayerLabel').textContent = `Player ${mySection} of ${mySections}`;
    welcomeModalInstance = new bootstrap.Modal(document.getElementById('welcomeModal'));
    welcomeModalInstance.show();
  } else {
    refreshState();
  }
}

function retryJoin() {
  const el = bootstrap.Modal.getInstance(document.getElementById('roomFullModal'));
  if (el) el.hide();
  bootMultiplayer();
}

function reopenSetup() {
  if (!isHost) {
    setStatus('Only the host machine (the PC running the server) can change the room size.');
    return;
  }
  const openSetup = () => {
    document.getElementById('setupSections').value = mySections;
    setupModalInstance = bootstrap.Modal.getOrCreateInstance(document.getElementById('setupModal'));
    setupModalInstance.show();
  };
  const settingsModalEl = document.getElementById('settingsModal');
  const settingsInstance = bootstrap.Modal.getInstance(settingsModalEl);
  if (settingsInstance && settingsModalEl.classList.contains('show')) {
    // wait for the fade-out + backdrop cleanup to finish before opening the
    // next modal -- opening immediately after hide() races Bootstrap's own
    // transition and can leave the new modal invisible behind a stale backdrop
    settingsModalEl.addEventListener('hidden.bs.modal', openSetup, {once: true});
    settingsInstance.hide();
  } else {
    openSetup();
  }
}

async function confirmSetup() {
  let sections = parseInt(document.getElementById('setupSections').value, 10) || 1;
  sections = Math.max(1, Math.min(16, sections));
  await joinRoom(sections);  // saves server-side, never errors -- reassigns anyone who no longer fits
  if (setupModalInstance) setupModalInstance.hide();
  updateSectionBadge();
  refreshState();
}

function confirmWelcome() {
  if (welcomeModalInstance) welcomeModalInstance.hide();
  refreshState();
}

function updateSectionBadge() {
  const el = document.getElementById('sectionBadge');
  if (mySections > 1) {
    el.textContent = `You: Player ${mySection}/${mySections}`;
    el.classList.remove('d-none');
  } else {
    el.classList.add('d-none');
    document.getElementById('myProgressBadge').classList.add('d-none');
  }
}

function updateMyProgress(reviewed, total) {
  const el = document.getElementById('myProgressBadge');
  if (mySections <= 1 || total === undefined) {
    el.classList.add('d-none');
    return;
  }
  const pct = total ? Math.round((reviewed / total) * 100) : 0;
  el.textContent = `Your section: ${reviewed}/${total} (${pct}%)`;
  el.classList.remove('d-none');
}

// ---------- resizable main preview (+/- buttons) ----------
function applyPhotoSize(vh) {
  vh = Math.max(30, Math.min(85, vh));
  document.documentElement.style.setProperty('--photo-h', vh + 'vh');
  localStorage.setItem('photoH', vh);
}
function changePhotoSize(delta) {
  const cur = parseInt(localStorage.getItem('photoH'), 10) || 58;
  applyPhotoSize(cur + delta);
}
applyPhotoSize(parseInt(localStorage.getItem('photoH'), 10) || 58);

// ---------- layout toggle: category strip above or below the preview ----------
const LAYOUT_MODES = ['normal', 'swapped', 'side'];  // category/top, preview/top, side-by-side
function applyLayout(mode) {
  if (!LAYOUT_MODES.includes(mode)) mode = 'normal';
  const el = document.getElementById('layoutFlex');
  el.classList.remove('layout-swapped', 'layout-side');
  if (mode === 'swapped') el.classList.add('layout-swapped');
  if (mode === 'side') el.classList.add('layout-side');
  localStorage.setItem('layoutMode', mode);

  // bulk mode's grid needs more room than the side-by-side split gives the
  // preview pane -- keep it off the table in that layout, and back out of
  // it automatically if you were already in bulk mode when you switched
  const bulkBtn = document.getElementById('bulkModeBtn');
  const sideActive = mode === 'side';
  bulkBtn.disabled = sideActive;
  bulkBtn.classList.toggle('d-none', sideActive);
  if (sideActive && typeof bulkMode !== 'undefined' && bulkMode) {
    toggleBulkMode();
  }
}
function toggleLayout() {
  const cur = localStorage.getItem('layoutMode') || 'normal';
  const next = LAYOUT_MODES[(LAYOUT_MODES.indexOf(cur) + 1) % LAYOUT_MODES.length];
  applyLayout(next);
  const labels = {normal: 'Category on top', swapped: 'Preview on top', side: 'Side by side (picture left, categories right)'};
  setStatus(`Layout: ${labels[next]}`);
}
applyLayout(localStorage.getItem('layoutMode') || 'normal');

// ---------- resizable bulk-mode thumbnails (+/- buttons) ----------
function applyBulkThumbSize(w) {
  w = Math.max(60, Math.min(260, w));
  const h = Math.round(w * 0.82);
  document.documentElement.style.setProperty('--bulk-thumb-w', w + 'px');
  document.documentElement.style.setProperty('--bulk-thumb-h', h + 'px');
  localStorage.setItem('bulkThumbW', w);
}
function changeBulkThumbSize(delta) {
  const cur = parseInt(localStorage.getItem('bulkThumbW'), 10) || 110;
  applyBulkThumbSize(cur + delta);
}
applyBulkThumbSize(parseInt(localStorage.getItem('bulkThumbW'), 10) || 110);

// ---------- resizable side-by-side split (drag the vertical handle left/right) ----------
function applySideSplit(pct) {
  pct = Math.max(25, Math.min(75, pct));
  document.documentElement.style.setProperty('--side-split', pct + '%');
  localStorage.setItem('sideSplit', pct);
}

function initSideResize() {
  applySideSplit(parseInt(localStorage.getItem('sideSplit'), 10) || 60);

  const handle = document.getElementById('sideResizeHandle');
  const flexEl = document.getElementById('layoutFlex');
  let dragging = false;

  function pointerX(e) { return e.touches ? e.touches[0].clientX : e.clientX; }

  function onStart(e) {
    dragging = true;
    handle.classList.add('dragging-active');
    e.preventDefault();
  }
  function onMove(e) {
    if (!dragging) return;
    const rect = flexEl.getBoundingClientRect();
    const pct = ((pointerX(e) - rect.left) / rect.width) * 100;
    applySideSplit(pct);
    e.preventDefault();
  }
  function onEnd() {
    dragging = false;
    handle.classList.remove('dragging-active');
  }

  handle.addEventListener('mousedown', onStart);
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onEnd);
  handle.addEventListener('touchstart', onStart, {passive: false});
  window.addEventListener('touchmove', onMove, {passive: false});
  window.addEventListener('touchend', onEnd);
}

// ---------- resizable category strip (drag the handle up/down) ----------
function applyStripSize(thumbH) {
  thumbH = Math.max(50, Math.min(240, thumbH));
  const cardW = Math.round(thumbH * 1.35 + 20);
  document.documentElement.style.setProperty('--cat-thumb-h', thumbH + 'px');
  document.documentElement.style.setProperty('--cat-card-w', cardW + 'px');
  localStorage.setItem('stripThumbH', thumbH);
}

function initStripResize() {
  const saved = parseInt(localStorage.getItem('stripThumbH'), 10);
  applyStripSize(Number.isFinite(saved) ? saved : 90);

  const handle = document.getElementById('stripResizeHandle');
  let dragging = false, startY = 0, startH = 90;

  function pointerY(e) { return e.touches ? e.touches[0].clientY : e.clientY; }

  function onStart(e) {
    dragging = true;
    startY = pointerY(e);
    startH = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--cat-thumb-h'), 10) || 90;
    e.preventDefault();
  }
  function onMove(e) {
    if (!dragging) return;
    const delta = pointerY(e) - startY;
    applyStripSize(startH + delta);
  }
  function onEnd() { dragging = false; }

  handle.addEventListener('mousedown', onStart);
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup', onEnd);
  handle.addEventListener('touchstart', onStart, {passive: false});
  window.addEventListener('touchmove', onMove, {passive: false});
  window.addEventListener('touchend', onEnd);
}

// ---------- live category sync: pick up categories/people created by others ----------
function startCategoryAutoSync() {
  setInterval(async () => {
    if (document.hidden) return;
    const s = await api(`/api/state?${partitionParams()}`);
    const grandPct = s.total ? Math.round((s.reviewed / s.total) * 100) : 0;
    document.getElementById('progressBadge').textContent = `${s.reviewed} / ${s.total} reviewed (${grandPct}%)`;
    updateProgressBar(s.reviewed, s.total);
    updateMyProgress(s.section_reviewed, s.section_total);
    renderStrip(s.categories);
  }, 3000);
}

// ---------- multiplayer progress modal (polls every 2s while open) ----------
const SECTION_COLORS = ['#38bdf8', '#f472b6', '#facc15', '#4ade80', '#a78bfa',
                         '#fb923c', '#f87171', '#2dd4bf', '#60a5fa', '#e879f9',
                         '#fbbf24', '#34d399', '#c084fc', '#fda4af', '#a3e635', '#7dd3fc'];
let mpInterval = null;
let serverLanUrl = null;

async function getServerLanUrl() {
  if (serverLanUrl) return serverLanUrl;
  const r = await api('/api/server_info');
  serverLanUrl = r.lan_url;
  return serverLanUrl;
}

async function copyMpLanUrl() {
  const el = document.getElementById('mpLanUrlInput');
  el.select();
  navigator.clipboard && navigator.clipboard.writeText(el.value);
}

async function saveRoomSizeFromModal() {
  let sections = parseInt(document.getElementById('mpSectionsInput').value, 10) || 1;
  sections = Math.max(1, Math.min(16, sections));
  await joinRoom(sections);
  updateSectionBadge();
  refreshMultiplayer();
}

document.getElementById('multiplayerModal').addEventListener('shown.bs.modal', async () => {
  const url = await getServerLanUrl();
  document.getElementById('mpLanUrlInput').value = url;
  const canvas = document.getElementById('mpQrCanvas');
  if (window.QRCode) {
    QRCode.toCanvas(canvas, url, {width: 140, margin: 1}, (err) => {
      if (err) console.error('QR code generation failed:', err);
    });
  } else {
    console.error('QRCode library did not load -- check network/CDN access');
  }
  refreshMultiplayer();
  mpInterval = setInterval(refreshMultiplayer, 2000);
});
document.getElementById('multiplayerModal').addEventListener('hidden.bs.modal', () => {
  if (mpInterval) clearInterval(mpInterval);
  mpInterval = null;
});

async function refreshMultiplayer() {
  const hostControls = document.getElementById('mpHostControls');
  const sectionsInput = document.getElementById('mpSectionsInput');
  hostControls.classList.toggle('d-none', !isHost);
  if (isHost && document.activeElement !== sectionsInput) {
    sectionsInput.value = mySections;
  }

  const r = await api(`/api/sections_progress?sections=${mySections}`);
  const grandPct = r.grand_total ? Math.round((r.grand_reviewed / r.grand_total) * 100) : 0;
  document.getElementById('mpGrandLabel').textContent = `${r.grand_reviewed} / ${r.grand_total} (${grandPct}%)`;

  const segBar = document.getElementById('mpSegmentedBar');
  const cards = document.getElementById('mpCards');
  segBar.innerHTML = '';
  cards.innerHTML = '';

  for (const s of r.sections) {
    const color = SECTION_COLORS[(s.section - 1) % SECTION_COLORS.length];
    const pct = s.total ? Math.round((s.reviewed / s.total) * 100) : 0;
    const shareOfGrand = r.grand_total ? (s.reviewed / r.grand_total) * 100 : 0;

    const seg = document.createElement('div');
    seg.className = 'seg';
    seg.style.width = shareOfGrand + '%';
    seg.style.background = color;
    seg.title = `Section ${s.section}: ${s.reviewed}/${s.total} (${pct}%)`;
    seg.textContent = shareOfGrand > 8 ? s.reviewed : '';
    segBar.appendChild(seg);

    const youTag = (s.section === mySection)
      ? ' <span class="badge text-bg-info" style="font-size:.6rem;">you</span>' : '';
    const card = document.createElement('div');
    card.className = 'mp-card';
    card.style.borderLeftColor = color;
    card.innerHTML = `
      <div class="fw-bold small">Section ${s.section}${youTag}</div>
      <div class="small text-muted">${s.reviewed} / ${s.total} photos &middot; ${pct}%</div>
      <div class="mp-bar-wrap"><div class="mp-bar-fill" style="width:${pct}%; background:${color};"></div></div>
      <div class="small text-muted mt-1 text-truncate">${s.current ? 'on: ' + s.current : 'all done!'}</div>
    `;
    cards.appendChild(card);
  }

  if (r.sections.length <= 1) {
    const hint = document.createElement('div');
    hint.className = 'text-muted small mt-2';
    hint.innerHTML = isHost
      ? 'Solo mode right now -- open <b>Settings</b> and set "how many people total" to add more players.'
      : 'Solo mode right now -- ask whoever is running the server (Settings on their end) to add more players.';
    cards.appendChild(hint);
  }
}

function partitionParams() {
  return `section=${mySection}&sections=${mySections}`;
}

// ---------- settings ----------
function getCooldownMs() {
  const v = parseInt(localStorage.getItem('cooldownMs'), 10);
  return Number.isFinite(v) ? v : 100;
}
function saveSettings() {
  const v = parseInt(document.getElementById('cooldownInput').value, 10);
  localStorage.setItem('cooldownMs', Number.isFinite(v) ? v : 100);
}
function copyLanUrl() {
  const el = document.getElementById('lanUrlInput');
  el.select();
  navigator.clipboard && navigator.clipboard.writeText(el.value);
}

// ---------- click-cooldown guard ----------
function canAct() {
  const now = performance.now();
  const cd = getCooldownMs();
  if (now < cooldownUntil) return false;
  cooldownUntil = now + cd;
  flashCooldown(cd);
  return true;
}
function flashCooldown(ms) {
  if (ms <= 0) return;
  document.querySelectorAll('.toolbar .btn, .cat-card').forEach(el => el.classList.add('is-cooling'));
  setTimeout(() => {
    document.querySelectorAll('.toolbar .btn, .cat-card').forEach(el => el.classList.remove('is-cooling'));
  }, ms);
}

let connectionFailures = 0;
let hostGoneModalInstance = null;

function onConnectionOk() {
  connectionFailures = 0;
  if (hostGoneModalInstance) hostGoneModalInstance.hide();
}

function onConnectionFail() {
  connectionFailures++;
  if (connectionFailures >= 2) {
    hostGoneModalInstance = bootstrap.Modal.getOrCreateInstance(document.getElementById('hostGoneModal'));
    hostGoneModalInstance.show();
  }
}

async function api(path, opts) {
  let res;
  try {
    res = await fetch(path, opts);
  } catch (err) {
    onConnectionFail();
    throw err;
  }
  if (!res.ok && res.status >= 500) {
    onConnectionFail();
    throw new Error(`server error ${res.status}`);
  }
  onConnectionOk();
  return res.json();
}

function retryConnection() {
  refreshState().catch(() => {});
}
async function apiPost(path, body) {
  return api(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({...body, section: mySection, sections: mySections})
  });
}

function setStatus(text) { document.getElementById('statusBar').textContent = text; }

function updateProgressBar(reviewed, total) {
  const pct = total ? Math.round((reviewed / total) * 100) : 0;
  document.getElementById('progressBarFill').style.width = pct + '%';
  document.getElementById('progressBarPct').textContent = `${reviewed} / ${total} (${pct}%)`;
}

async function refreshState() {
  const s = await api(`/api/state?${partitionParams()}`);
  const grandPct = s.total ? Math.round((s.reviewed / s.total) * 100) : 0;
  document.getElementById('progressBadge').textContent = `${s.reviewed} / ${s.total} reviewed (${grandPct}%)`;
  updateProgressBar(s.reviewed, s.total);
  updateSectionBadge();
  updateMyProgress(s.section_reviewed, s.section_total);
  currentFile = s.current;
  const img = document.getElementById('photo');
  const doneMsg = document.getElementById('doneMsg');
  if (s.current) {
    img.classList.remove('d-none');
    doneMsg.classList.add('d-none');
    img.src = `/api/photo/${encodeURIComponent(s.current)}?size=1600`;
    setStatus(`Now showing: ${s.current}`);
    loadPhotoDetails(s.current);
  } else {
    img.classList.add('d-none');
    doneMsg.classList.remove('d-none');
    setStatus('Done! Progress saved.');
    document.getElementById('photoDetails').innerHTML = '';
  }
  renderStrip(s.categories);
  preloadUpcoming(s.upcoming || []);
  if (bulkMode) loadBulkGrid();
}

function formatBytes(n) {
  if (!n) return null;
  return n > 1024 * 1024 ? (n / 1024 / 1024).toFixed(1) + ' MB' : Math.round(n / 1024) + ' KB';
}

async function loadPhotoDetails(fname) {
  const el = document.getElementById('photoDetails');
  const info = await api(`/api/photo_info/${encodeURIComponent(fname)}`);
  const chips = [];
  if (info.date) chips.push(`<i class="bi bi-calendar3"></i> ${info.date}`);
  if (info.width && info.height) chips.push(`<i class="bi bi-aspect-ratio"></i> ${info.width}&times;${info.height}`);
  const sizeStr = formatBytes(info.size_bytes);
  if (sizeStr) chips.push(`<i class="bi bi-hdd"></i> ${sizeStr}`);
  if (info.camera) chips.push(`<i class="bi bi-camera2"></i> ${info.camera}`);
  if (info.iso) chips.push(`ISO ${info.iso}`);
  if (info.exposure) chips.push(info.exposure);
  if (info.fnumber) chips.push(info.fnumber);
  if (info.focal_length) chips.push(info.focal_length);
  el.innerHTML = chips.map(c => `<span>${c}</span>`).join('');
}

function preloadUpcoming(upcoming) {
  for (const fname of upcoming) {
    const url = `/api/photo/${encodeURIComponent(fname)}?size=1600`;
    if (preloadedUrls.has(url)) continue;
    preloadedUrls.add(url);
    const img = new Image();
    img.src = url;
  }
}

function categoryLabel(name) {
  if (name.startsWith('people_')) return '#' + name.split('_')[1];
  if (name === 'atmosphere') return 'Atmosphere';
  return name;
}

function buildCategoryCard(c) {
  const card = document.createElement('div');
  card.className = 'cat-card';
  card.onclick = (e) => {
    if (e.target.closest('.mini-btns')) return;
    if (bulkMode) bulkAssignToCategory(c.name);
    else assign(c.name);
  };
  card.addEventListener('mouseenter', (e) => showHoverPreview(c.thumb, e));
  card.addEventListener('mousemove', positionHoverPreview);
  card.addEventListener('mouseleave', hideHoverPreview);
  // touchscreens have no hover -- hold the card to preview it instead of tapping straight through
  let touchPreviewTimer = null, touchPreviewShown = false;
  card.addEventListener('touchstart', (e) => {
    touchPreviewShown = false;
    const t = e.touches[0];
    touchPreviewTimer = setTimeout(() => {
      touchPreviewShown = true;
      showHoverPreview(c.thumb, {clientX: t.clientX, clientY: t.clientY});
    }, 400);
  }, {passive: true});
  card.addEventListener('touchmove', () => clearTimeout(touchPreviewTimer));
  card.addEventListener('touchend', (e) => {
    clearTimeout(touchPreviewTimer);
    hideHoverPreview();
    if (touchPreviewShown) e.preventDefault();  // long-press previewed it, don't also fire the tap
  });
  card.addEventListener('dragover', (e) => { e.preventDefault(); card.classList.add('drag-over'); });
  card.addEventListener('dragleave', () => card.classList.remove('drag-over'));
  card.addEventListener('drop', (e) => {
    e.preventDefault(); card.classList.remove('drag-over');
    if (e.dataTransfer.getData('text/plain') === 'current-photo') assign(c.name);
  });

  const thumbHtml = c.thumb
    ? `<img src="/api/photo/${encodeURIComponent(c.thumb)}?size=200" loading="lazy">`
    : `<div class="noimg">?</div>`;

  card.innerHTML = `
    ${thumbHtml}
    <div class="cat-label"></div>
    <div class="mini-btns">
      <button class="btn btn-info" onclick="openFolder('${escapeJs(c.name)}')" title="Open in Explorer"><i class="bi bi-folder2-open"></i></button>
      <button class="btn btn-primary" onclick="renameCategory('${escapeJs(c.name)}')" title="Rename"><i class="bi bi-pencil"></i></button>
      <button class="btn btn-danger" onclick="deleteCategory('${escapeJs(c.name)}')" title="Delete whole category (back to unsorted)"><i class="bi bi-trash"></i></button>
    </div>`;

  const labelEl = card.querySelector('.cat-label');
  labelEl.textContent = `${categoryLabel(c.name)} (${c.count})`;

  return {el: card, labelEl, thumb: c.thumb, count: c.count};
}

let stripState = {};       // category name -> {el, labelEl, thumb, count}
let stripOrderCache = [];  // category names, in display order

function renderStrip(categories) {
  const outer = document.getElementById('stripOuter');
  const select = document.getElementById('bulkCategorySelect');

  select.innerHTML = '';
  for (const c of categories) {
    const opt = document.createElement('option');
    opt.value = c.name; opt.textContent = categoryLabel(c.name);
    select.appendChild(opt);
  }

  if (!categories.length) {
    if (stripOrderCache.length !== 0) {
      outer.innerHTML = '<div class="text-muted small p-2">No categories yet — click a button below to start</div>';
    }
    stripOrderCache = [];
    stripState = {};
    return;
  }

  const newOrder = categories.map(c => c.name);
  const sameShape = newOrder.length === stripOrderCache.length &&
                     newOrder.every((n, i) => n === stripOrderCache[i]);

  if (sameShape) {
    // nothing was added/removed/reordered -- patch only what changed,
    // in place, so nothing flickers
    for (const c of categories) {
      const prev = stripState[c.name];
      if (!prev) continue;
      if (prev.count !== c.count) {
        prev.labelEl.textContent = `${categoryLabel(c.name)} (${c.count})`;
        prev.count = c.count;
      }
      if (prev.thumb !== c.thumb && c.thumb) {
        const imgEl = prev.el.querySelector('img');
        const url = `/api/photo/${encodeURIComponent(c.thumb)}?size=200`;
        if (imgEl) {
          imgEl.src = url;
        } else {
          const noimg = prev.el.querySelector('.noimg');
          if (noimg) noimg.outerHTML = `<img src="${url}" loading="lazy">`;
        }
        prev.thumb = c.thumb;
      }
    }
    return;
  }

  // categories were added/removed/reordered -- rebuild (rare, so any
  // flicker here is acceptable; routine picks stay on the fast path above)
  const scrollPos = outer.scrollLeft;
  outer.innerHTML = '';
  stripState = {};
  stripOrderCache = newOrder;
  for (const c of categories) {
    const card = buildCategoryCard(c);
    outer.appendChild(card.el);
    stripState[c.name] = card;
  }
  outer.scrollLeft = scrollPos;
}

function escapeJs(s) { return s.replace(/'/g, "\\\\'"); }

// ---------- hover preview ----------
function showHoverPreview(thumbFile, e) {
  if (!thumbFile) return;
  const box = document.getElementById('hoverPreview');
  document.getElementById('hoverPreviewImg').src = `/api/photo/${encodeURIComponent(thumbFile)}?size=500`;
  box.style.display = 'block';
  positionHoverPreview(e);
}
function positionHoverPreview(e) {
  const box = document.getElementById('hoverPreview');
  box.style.left = (e.clientX + 20) + 'px';
  box.style.top = (e.clientY + 20) + 'px';
}
function hideHoverPreview() {
  document.getElementById('hoverPreview').style.display = 'none';
}

// ---------- lightbox ----------
function openLightbox() {
  if (!currentFile) return;
  document.getElementById('lightboxImg').src = `/api/photo/${encodeURIComponent(currentFile)}?size=2400`;
  new bootstrap.Modal(document.getElementById('lightboxModal')).show();
}

// ---------- drag and drop of the current photo ----------
document.getElementById('photo').addEventListener('dragstart', (e) => {
  e.dataTransfer.setData('text/plain', 'current-photo');
});

// ---------- actions ----------
async function assign(category) {
  if (!currentFile || !canAct()) return;
  await apiPost('/api/assign', {category});
  await refreshState();
}

async function customCategory() {
  const name = prompt('Category / folder name:');
  if (!name) return;
  if (bulkMode) {
    // bulkAssignToCategory does its own cooldown check -- don't also
    // consume it here, or the second check fails right after the first
    await bulkAssignToCategory(name);
    return;
  }
  if (!canAct()) return;
  await apiPost('/api/assign', {category: name});
  await refreshState();
}

async function skip() {
  if (!canAct()) return;
  await apiPost('/api/skip', {});
  await refreshState();
}

async function undo() {
  if (!canAct()) return;
  await apiPost('/api/undo', {});
  await refreshState();
}

async function renameCategory(oldName) {
  const newName = prompt('Edit the name and press OK:', oldName);
  if (!newName || newName === oldName) return;
  await api('/api/rename', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old: oldName, new: newName})
  });
  await refreshState();
}

async function deleteCategory(category) {
  if (!confirm(`Delete the whole "${category}" category? Every photo in it goes back to unsorted.`)) return;
  await api('/api/delete_category', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({category})
  });
  await refreshState();
}

async function openFolder(category) {
  await api('/api/open_folder', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({category})
  });
}

async function syncFromDisk() {
  const r = await api('/api/sync', {method: 'POST'});
  setStatus(r.changed ? 'Synced -- some photos went back into the queue.' : 'Sync: nothing to reconcile, all good.');
  await refreshState();
}

async function clearAll() {
  if (!canAct()) return;
  if (!confirm('Clear EVERYTHING? This wipes every sorted folder and all progress for the whole event -- for everyone, on every device. Only the copies get deleted, your original photos are safe. This cannot be undone.')) return;
  const r = await api('/api/clear_all', {method: 'POST'});
  if (r.ok === false) {
    setStatus(r.error || 'Clear All is only allowed from the host machine.');
    return;
  }
  await refreshState();
}

// ---------- bulk mode ----------
function toggleBulkMode() {
  bulkMode = !bulkMode;
  document.getElementById('singleView').classList.toggle('d-none', bulkMode);
  document.getElementById('bulkView').classList.toggle('d-none', !bulkMode);
  if (bulkMode) loadBulkGrid();
}

async function loadBulkGrid() {
  const r = await api(`/api/undecided_list?limit=200&${partitionParams()}`);
  const grid = document.getElementById('bulkGrid');
  grid.innerHTML = '';
  bulkSelected.clear();
  updateBulkSelectedCount();
  for (const fname of r.files) {
    const wrap = document.createElement('div');
    wrap.className = 'bulk-thumb';
    wrap.dataset.fname = fname;
    wrap.innerHTML = `<img src="/api/photo/${encodeURIComponent(fname)}?size=220" loading="lazy" onclick="toggleBulkSelect('${escapeJs(fname)}')">`;
    grid.appendChild(wrap);
  }
  if (!r.files.length) {
    grid.innerHTML = '<div class="text-muted small p-2">Nothing left to sort in your section right now.</div>';
  }
}

function toggleBulkSelect(fname) {
  const el = document.querySelector(`.bulk-thumb[data-fname="${CSS.escape(fname)}"]`);
  if (bulkSelected.has(fname)) {
    bulkSelected.delete(fname);
    el && el.classList.remove('selected');
  } else {
    bulkSelected.add(fname);
    el && el.classList.add('selected');
  }
  updateBulkSelectedCount();
}

function updateBulkSelectedCount() {
  document.getElementById('bulkSelectedCount').textContent = `${bulkSelected.size} selected`;
}

function deselectAllBulk() {
  for (const fname of bulkSelected) {
    const el = document.querySelector(`.bulk-thumb[data-fname="${CSS.escape(fname)}"]`);
    el && el.classList.remove('selected');
  }
  bulkSelected.clear();
  updateBulkSelectedCount();
}

// ---------- press-and-drag multi-select (touch and mouse, like Photos/Gallery) ----------
function setupBulkDragSelect() {
  const grid = document.getElementById('bulkGrid');
  const box = document.getElementById('bulkSelectionBox');
  const LONG_PRESS_MS = 220;
  let pressTimer = null;
  let dragging = false;
  let dragMode = 'add';
  let startX = 0, startY = 0;
  let dragStartSelection = new Set();  // snapshot of what was selected before this drag

  function updateBox(curX, curY) {
    const left = Math.min(startX, curX), top = Math.min(startY, curY);
    const right = Math.max(startX, curX), bottom = Math.max(startY, curY);
    box.style.left = left + 'px';
    box.style.top = top + 'px';
    box.style.width = (right - left) + 'px';
    box.style.height = (bottom - top) + 'px';

    // true marquee semantics: whatever the rectangle currently overlaps is
    // selected (or deselected, in remove mode) on top of whatever was
    // already selected before the drag started -- shrinking the box lets
    // go of items it no longer covers, same as Photos/Gallery apps
    document.querySelectorAll('.bulk-thumb').forEach((el) => {
      const r = el.getBoundingClientRect();
      const overlaps = !(r.right < left || r.left > right || r.bottom < top || r.top > bottom);
      const fname = el.dataset.fname;
      const wasSelected = dragStartSelection.has(fname);
      const shouldSelect = dragMode === 'add' ? (wasSelected || overlaps) : (wasSelected && !overlaps);
      if (shouldSelect && !bulkSelected.has(fname)) {
        bulkSelected.add(fname);
        el.classList.add('selected');
      } else if (!shouldSelect && bulkSelected.has(fname)) {
        bulkSelected.delete(fname);
        el.classList.remove('selected');
      }
    });
    updateBulkSelectedCount();
  }

  let activePointerId = null;

  grid.addEventListener('pointerdown', (e) => {
    const thumb = e.target.closest('.bulk-thumb');
    if (!thumb) return;
    startX = e.clientX; startY = e.clientY;
    activePointerId = e.pointerId;
    // keep every event for this finger/cursor routed to the grid even once
    // it strays outside the grid's box -- without this, dragging near the
    // edge hands control back to the browser and it starts scrolling again
    try { grid.setPointerCapture(e.pointerId); } catch (err) {}
    pressTimer = setTimeout(() => {
      dragging = true;
      dragMode = bulkSelected.has(thumb.dataset.fname) ? 'remove' : 'add';
      dragStartSelection = new Set(bulkSelected);
      grid.classList.add('dragging');
      box.style.display = 'block';
      updateBox(startX, startY);
      if (navigator.vibrate) navigator.vibrate(15);  // small haptic tick, phones only
    }, LONG_PRESS_MS);
  });

  grid.addEventListener('pointermove', (e) => {
    if (dragging) {
      e.preventDefault();
      updateBox(e.clientX, e.clientY);
      return;
    }
    if (!pressTimer) return;
    // still inside the long-press window -- block native scroll from
    // hijacking the gesture before our timer gets a chance to fire
    e.preventDefault();
    // moved too far before the long-press fired -- treat as a scroll/flick, cancel
    if (Math.abs(e.clientX - startX) > 8 || Math.abs(e.clientY - startY) > 8) {
      clearTimeout(pressTimer);
      pressTimer = null;
    }
  });

  function endPress() {
    clearTimeout(pressTimer);
    pressTimer = null;
    dragging = false;
    grid.classList.remove('dragging');
    box.style.display = 'none';
    if (activePointerId !== null) {
      try { grid.releasePointerCapture(activePointerId); } catch (err) {}
      activePointerId = null;
    }
  }
  grid.addEventListener('pointerup', endPress);
  grid.addEventListener('pointercancel', endPress);
  grid.addEventListener('pointerleave', () => { if (!dragging) clearTimeout(pressTimer); });
}

async function bulkAssignToCategory(category) {
  if (!canAct()) return;
  if (bulkSelected.size === 0) {
    setStatus('Select at least one photo first, then click a category to file them all.');
    return;
  }
  const r = await api('/api/bulk_assign', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({category, files: Array.from(bulkSelected)})
  });
  setStatus(`Filed ${r.count} photos into ${category}.`);
  await refreshState();
}

async function bulkAssignSelected() {
  const category = document.getElementById('bulkCategorySelect').value;
  if (!category) { setStatus('Pick a category first.'); return; }
  await bulkAssignToCategory(category);
}

// ---------- session timer ----------
function startSessionTimer() {
  const start = Date.now();
  const el = document.getElementById('sessionTimer');
  setInterval(() => {
    const secs = Math.floor((Date.now() - start) / 1000);
    const h = String(Math.floor(secs / 3600)).padStart(2, '0');
    const m = String(Math.floor((secs % 3600) / 60)).padStart(2, '0');
    const s = String(secs % 60).padStart(2, '0');
    el.innerHTML = `<i class="bi bi-stopwatch"></i> ${h}:${m}:${s}`;
  }, 1000);
}

// ---------- pick-folder gate: nothing else starts until a folder is chosen ----------
let pickFolderPoll = null;

async function bootApp() {
  const info = await api('/api/current_folder');
  if (info.folder) {
    bootMultiplayer();
    return;
  }
  bootstrap.Modal.getOrCreateInstance(document.getElementById('pickFolderModal')).show();
  pickFolderPoll = setInterval(async () => {
    const info2 = await api('/api/current_folder');
    if (info2.folder) {
      clearInterval(pickFolderPoll);
      pickFolderPoll = null;
      const m = bootstrap.Modal.getInstance(document.getElementById('pickFolderModal'));
      if (m) m.hide();
      bootMultiplayer();
    }
  }, 2000);
}

async function choosePhotoFolder() {
  const statusEl = document.getElementById('pickFolderStatus');
  statusEl.textContent = 'Opening the folder picker on the host computer...';
  const r = await api('/api/pick_folder', {method: 'POST'});
  if (r.ok) {
    statusEl.textContent = '';
    if (pickFolderPoll) { clearInterval(pickFolderPoll); pickFolderPoll = null; }
    const m = bootstrap.Modal.getInstance(document.getElementById('pickFolderModal'));
    if (m) m.hide();
    bootMultiplayer();
  } else if (r.error === 'cancelled') {
    statusEl.textContent = '';
  } else {
    statusEl.textContent = r.error || 'Could not pick a folder.';
  }
}

// ---------- boot ----------
document.getElementById('cooldownInput').value = getCooldownMs();
document.getElementById('lanUrlInput').value = window.location.origin;
initStripResize();
initSideResize();
setupBulkDragSelect();
bootApp();
startCategoryAutoSync();
startSessionTimer();
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return INDEX_HTML


def main():
    lan_ip = get_lan_ip()
    local_url = f"http://127.0.0.1:{PORT}"
    lan_url = f"http://{lan_ip}:{PORT}"
    threading.Timer(1.0, lambda: webbrowser.open(lan_url)).start()
    print(f"Event Photo Sorter (web) running.")
    print(f"  On this PC:        {local_url}")
    print(f"  On your WiFi (LAN): {lan_url}   <- share this for multiplayer sorting")
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
