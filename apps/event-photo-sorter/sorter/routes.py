"""
All HTTP endpoints: the page itself, plus the JSON API the frontend
(static/js/app.js) talks to.
"""
import csv
import io
import os
import shutil
import time

from flask import jsonify, request, Response, render_template

from . import config
from .app import app, state_lock
from .network import LOCAL_ADDRESSES, get_lan_ip
from .logic import (
    sanitize_name, existing_categories, category_last_photo, categories_of,
    next_new_person_number, first_undecided, get_partition, partition_slice,
)
from .photos import list_files, fast_open, get_photo_info, photo_datetime
from .storage import (
    load_progress, save_progress, load_skipped, save_skipped,
    load_room, save_room, load_rotations, save_rotations,
)
from PIL import Image

UNDO_HISTORY_LIMIT = 20  # per section -- bounds room file growth, still plenty of steps back


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/server_info")
def api_server_info():
    return jsonify({"lan_url": f"http://{get_lan_ip()}:{config.PORT}"})


@app.route("/api/current_folder")
def api_current_folder():
    return jsonify({"folder": config.SRC})


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
        config.set_active_folder(path)
    return jsonify({"ok": True, "path": config.SRC})


@app.route("/api/state")
def api_state():
    if config.SRC is None:
        return jsonify({"no_folder": True, "current": None, "current_rotation": 0, "duplicate_hint": None,
                         "total": 0, "reviewed": 0, "section_total": 0, "section_reviewed": 0,
                         "upcoming": [], "categories": []})
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
        current_rotation = load_rotations().get(current, 0) if current else 0
        duplicate_hint = _find_duplicate_hint(my_files, idx, progress)

        return jsonify({
            "current": current,
            "current_rotation": current_rotation,
            "duplicate_hint": duplicate_hint,
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
    if config.SRC is None:
        return jsonify({"files": []})
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
    if config.SRC is None:
        return "not found", 404
    fname = os.path.basename(fname)
    path = os.path.join(config.SRC, fname)
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


@app.route("/api/rotate", methods=["POST"])
def api_rotate():
    # records a manual rotation for this filename -- previewed client-side
    # via CSS transform, then baked into the actual pixels only once the
    # photo is filed (see _copy_into_category), so the original in
    # config.SRC is never touched
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    fname = os.path.basename(data.get("file", ""))
    try:
        delta = int(data.get("delta", 0))
    except (TypeError, ValueError):
        delta = 0
    if not fname or delta not in (90, -90):
        return jsonify({"ok": False, "error": "invalid rotate request"}), 400
    with state_lock:
        rotations = load_rotations()
        rotations[fname] = (rotations.get(fname, 0) + delta) % 360
        save_rotations(rotations)
        new_rotation = rotations[fname]
    return jsonify({"ok": True, "rotation": new_rotation})


@app.route("/api/category_photo/<category>/<path:fname>")
def api_category_photo(category, fname):
    # serves from the already-filed copy in sorted/<category>/, not the
    # original in config.SRC -- that copy has any manual rotation already
    # baked into its pixels (see _copy_into_category), so category-strip
    # thumbnails and the gallery modal show the photo the way it was
    # actually filed, without redoing rotation math here
    if config.SRC is None:
        return "not found", 404
    category = sanitize_name(category)
    fname = os.path.basename(fname)
    path = os.path.join(config.SORTED_DIR, category, fname)
    if not os.path.exists(path):
        return "not found", 404
    try:
        size = int(request.args.get("size", 400))
    except ValueError:
        size = 400
    img = fast_open(path, (size, size))
    img.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    buf.seek(0)
    resp = Response(buf.read(), mimetype="image/jpeg")
    # short cache, not "immutable" like /api/photo -- unlike an original,
    # this exact path's bytes can change if the same photo gets undone and
    # refiled into the same category with a different rotation
    resp.headers["Cache-Control"] = "public, max-age=60"
    return resp


@app.route("/api/unfile", methods=["POST"])
def api_unfile():
    # removes one photo from one category -- deletes just that copy and, if
    # it wasn't also filed elsewhere, drops it from progress entirely so it
    # goes straight back into the undecided queue (the original in
    # config.SRC is untouched, same guarantee as every other delete here)
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    fname = os.path.basename(data.get("file", ""))
    if not category or not fname:
        return jsonify({"ok": False, "error": "missing category or file"}), 400
    with state_lock:
        path = os.path.join(config.SORTED_DIR, category, fname)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                return jsonify({"ok": False, "error": str(e)}), 500
        progress = load_progress()
        entry = progress.get(fname)
        if entry is not None:
            remaining = [c for c in categories_of(entry) if c != category]
            if remaining:
                progress[fname] = remaining[0] if len(remaining) == 1 else remaining
            else:
                del progress[fname]
            save_progress(progress)
    return jsonify({"ok": True})


@app.route("/api/category_files")
def api_category_files():
    if config.SRC is None:
        return jsonify({"files": [], "category": ""})
    category = sanitize_name(request.args.get("category", ""))
    cat_dir = os.path.join(config.SORTED_DIR, category)
    if not os.path.isdir(cat_dir):
        return jsonify({"files": [], "category": category})
    files = sorted(f for f in os.listdir(cat_dir) if os.path.isfile(os.path.join(cat_dir, f)))
    return jsonify({"files": files, "category": category})


@app.route("/api/delete_original", methods=["POST"])
def api_delete_original():
    # permanently deletes the actual source photo -- the one deliberate
    # exception to this app's usual "never touch the originals" rule.
    # Requires an explicit confirm client-side; here we also sweep every
    # trace of it (any filed copies, progress/skipped/rotation entries, and
    # its spot in every section's undo history) so nothing is left pointing
    # at a file that no longer exists
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    fname = os.path.basename(data.get("file", ""))
    if not fname:
        return jsonify({"ok": False, "error": "missing file"}), 400
    with state_lock:
        src_path = os.path.join(config.SRC, fname)
        if not os.path.exists(src_path):
            return jsonify({"ok": False, "error": "file not found"}), 404

        progress = load_progress()
        entry = progress.pop(fname, None)
        if entry is not None:
            for category in categories_of(entry):
                copy_path = os.path.join(config.SORTED_DIR, category, fname)
                if os.path.exists(copy_path):
                    try:
                        os.remove(copy_path)
                    except Exception:
                        pass
            save_progress(progress)

        skipped = load_skipped()
        if fname in skipped:
            skipped.discard(fname)
            save_skipped(skipped)

        rotations = load_rotations()
        if fname in rotations:
            del rotations[fname]
            save_rotations(rotations)

        room = load_room()
        for stack in room["undo_stack"].values():
            if fname in stack:
                stack.remove(fname)
        save_room(room)

        try:
            os.remove(src_path)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/photo_info/<path:fname>")
def api_photo_info(fname):
    if config.SRC is None:
        return jsonify({"error": "not found"}), 404
    fname = os.path.basename(fname)
    path = os.path.join(config.SRC, fname)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    return jsonify(get_photo_info(path))


DUPLICATE_HINT_WINDOW_SECS = 2  # burst/retake shots are usually within a second or two of each other


def _find_duplicate_hint(my_files, idx, progress):
    # a *suggestion*, not an automatic decision -- EXIF timestamps can be a
    # second or two off, and consecutive burst shots aren't always true
    # duplicates (someone blinked), so this only ever offers a one-click
    # shortcut, it never files or hides anything on its own
    if idx <= 0 or idx >= len(my_files):
        return None
    current, prev_fname = my_files[idx], my_files[idx - 1]
    prev_entry = progress.get(prev_fname)
    if prev_entry is None:
        return None
    cur_dt = photo_datetime(os.path.join(config.SRC, current))
    prev_dt = photo_datetime(os.path.join(config.SRC, prev_fname))
    if not cur_dt or not prev_dt:
        return None
    if abs((cur_dt - prev_dt).total_seconds()) > DUPLICATE_HINT_WINDOW_SECS:
        return None
    return {"file": prev_fname, "categories": categories_of(prev_entry)}


def _copy_into_category(fname, category):
    cat_dir = os.path.join(config.SORTED_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    dest = os.path.join(cat_dir, fname)
    shutil.copy2(os.path.join(config.SRC, fname), dest)
    degrees = load_rotations().get(fname, 0)
    if degrees:
        # bake any manual rotation into the filed copy -- the original in
        # config.SRC is never touched, only this copy in sorted/<category>/
        img = Image.open(dest).convert("RGB")
        img.rotate(-degrees, expand=True).save(dest, quality=95)


def _set_progress(progress, fname, value):
    # dicts only move a key to the end on *insert*, not on updating an
    # existing key's value -- popping first means a re-filed photo (undo,
    # unfile, then re-assign) lands at the end again, so category_last_photo
    # (which picks "last matching entry" via iteration order) reflects true
    # recency instead of the photo's original, possibly-stale position
    progress.pop(fname, None)
    progress[fname] = value


def _do_assign(fname, category):
    _copy_into_category(fname, category)
    progress = load_progress()
    _set_progress(progress, fname, category)
    save_progress(progress)
    skipped = load_skipped()
    if fname in skipped:
        skipped.discard(fname)
        save_skipped(skipped)


def _push_undo(section, sections, fname):
    room = load_room()
    key = f"{section}:{sections}"
    stack = room["undo_stack"].setdefault(key, [])
    stack.append(fname)
    del stack[:-UNDO_HISTORY_LIMIT]
    save_room(room)


@app.route("/api/assign", methods=["POST"])
def api_assign():
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
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
        # Undo undoes what, or lose anyone's spot in the room. Kept as a
        # capped stack (not just the single most recent pick) so pressing
        # Undo repeatedly walks back through this section's last several
        # decisions instead of falling through to the wrong section's photo
        # after just one step.
        _push_undo(section, sections, fname)
    return jsonify({"ok": True})


@app.route("/api/assign_multi", methods=["POST"])
def api_assign_multi():
    # files the current photo into several categories at once -- e.g. a
    # shot that's both "atmosphere" and a specific person -- by copying it
    # into each category folder and recording the list in progress.json
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    categories = [sanitize_name(c) for c in data.get("categories", [])]
    categories = list(dict.fromkeys(c for c in categories if c))  # de-dupe, keep order
    section, sections = get_partition(data)
    if not categories:
        return jsonify({"ok": False, "error": "no categories selected"}), 400
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
            for category in categories:
                _copy_into_category(fname, category)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        progress = load_progress()
        _set_progress(progress, fname, categories if len(categories) > 1 else categories[0])
        save_progress(progress)
        skipped = load_skipped()
        if fname in skipped:
            skipped.discard(fname)
            save_skipped(skipped)
        _push_undo(section, sections, fname)
    return jsonify({"ok": True})


@app.route("/api/bulk_assign", methods=["POST"])
def api_bulk_assign():
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    files_in = [os.path.basename(f) for f in data.get("files", [])]
    if not category or not files_in:
        return jsonify({"ok": False, "error": "missing category or files"}), 400
    with state_lock:
        cat_dir = os.path.join(config.SORTED_DIR, category)
        os.makedirs(cat_dir, exist_ok=True)
        progress = load_progress()
        count = 0
        for fname in files_in:
            src = os.path.join(config.SRC, fname)
            if not os.path.exists(src):
                continue
            try:
                shutil.copy2(src, os.path.join(cat_dir, fname))
            except Exception:
                continue
            _set_progress(progress, fname, category)
            count += 1
        save_progress(progress)
    return jsonify({"ok": True, "count": count})


@app.route("/api/skip", methods=["POST"])
def api_skip():
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
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


def _restore_from_progress(progress, fname):
    """Removes fname's filed copy/copies and drops it from progress, putting
    it back at the front of the undecided queue. Shared by /api/undo (which
    pops the top of the per-section stack) and /api/undo_specific (which
    targets an exact entry picked from the Undo History list)."""
    entry = progress.pop(fname)
    for category in categories_of(entry):
        path = os.path.join(config.SORTED_DIR, category, fname)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


@app.route("/api/undo", methods=["POST"])
def api_undo():
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    section, sections = get_partition(data)
    with state_lock:
        progress = load_progress()
        room = load_room()
        key = f"{section}:{sections}"
        stack = room["undo_stack"].get(key, [])
        fname = None
        # walk back through this section's own history, skipping anything
        # that's no longer in progress (e.g. its category was already
        # deleted) -- lets repeated Undo presses actually walk back several
        # steps instead of only ever undoing the single most recent pick
        while stack:
            candidate = stack.pop()
            if candidate in progress:
                fname = candidate
                break
        save_room(room)
        if fname is None:
            # fall back to the globally most recent assignment
            if not progress:
                return jsonify({"ok": False, "error": "nothing to undo"}), 400
            fname = list(progress.keys())[-1]
        _restore_from_progress(progress, fname)
        save_progress(progress)
    return jsonify({"ok": True, "restored": fname})


@app.route("/api/undo_history")
def api_undo_history():
    # what the Undo History modal shows: this section's recent picks, most
    # recent first, each with whatever category(ies) it's *currently* filed
    # under -- read straight from progress.json rather than duplicating that
    # in the stack, so it can't drift out of sync with reality
    if config.SRC is None:
        return jsonify({"history": []})
    section, sections = get_partition(request.args)
    with state_lock:
        progress = load_progress()
        room = load_room()
        stack = room["undo_stack"].get(f"{section}:{sections}", [])
        history = [
            {"file": fname, "categories": categories_of(progress[fname])}
            for fname in reversed(stack)
            if fname in progress
        ]
    return jsonify({"history": history[:UNDO_HISTORY_LIMIT]})


@app.route("/api/undo_specific", methods=["POST"])
def api_undo_specific():
    # undoes one exact entry from the Undo History list, not just the most
    # recent pick -- lets you fix a specific misclick a few photos back
    # without having to Undo through everything after it too
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    fname = os.path.basename(data.get("file", ""))
    section, sections = get_partition(data)
    if not fname:
        return jsonify({"ok": False, "error": "missing file"}), 400
    with state_lock:
        progress = load_progress()
        if fname not in progress:
            return jsonify({"ok": False, "error": "already undone"}), 400
        _restore_from_progress(progress, fname)
        save_progress(progress)
        room = load_room()
        stack = room["undo_stack"].get(f"{section}:{sections}", [])
        if fname in stack:
            stack.remove(fname)
        save_room(room)
    return jsonify({"ok": True, "restored": fname})


@app.route("/api/rename", methods=["POST"])
def api_rename():
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    old_name = data.get("old", "")
    new_name = sanitize_name(data.get("new", ""))
    if not new_name or new_name == old_name:
        return jsonify({"ok": False, "error": "invalid name"}), 400
    with state_lock:
        old_dir = os.path.join(config.SORTED_DIR, old_name)
        new_dir = os.path.join(config.SORTED_DIR, new_name)
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
            entry = progress[fname]
            if isinstance(entry, list):
                if old_name in entry:
                    progress[fname] = [new_name if c == old_name else c for c in entry]
            elif entry == old_name:
                progress[fname] = new_name
        save_progress(progress)
    return jsonify({"ok": True})


@app.route("/api/delete_category", methods=["POST"])
def api_delete_category():
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    with state_lock:
        path = os.path.join(config.SORTED_DIR, category)
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        progress = load_progress()
        for fname in list(progress.keys()):
            entry = progress[fname]
            if isinstance(entry, list):
                if category not in entry:
                    continue
                remaining = [c for c in entry if c != category]
                # a photo filed into this category *and* others keeps its
                # other categories -- only the copy in this one is gone
                if remaining:
                    progress[fname] = remaining[0] if len(remaining) == 1 else remaining
                else:
                    del progress[fname]
            elif entry == category:
                del progress[fname]
        save_progress(progress)
    return jsonify({"ok": True})


@app.route("/api/open_folder", methods=["POST"])
def api_open_folder():
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    category = sanitize_name(data.get("category", ""))
    path = os.path.join(config.SORTED_DIR, category)
    os.makedirs(path, exist_ok=True)
    try:
        os.startfile(path)  # only meaningful when opened from the host machine
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/sync", methods=["POST"])
def api_sync():
    if config.SRC is None:
        return jsonify({"ok": True, "changed": False})
    with state_lock:
        progress = load_progress()
        changed = False
        for fname, entry in list(progress.items()):
            cats = categories_of(entry)
            remaining = [c for c in cats if os.path.exists(os.path.join(config.SORTED_DIR, c, fname))]
            if remaining != cats:
                changed = True
                if remaining:
                    progress[fname] = remaining[0] if len(remaining) == 1 else remaining
                else:
                    del progress[fname]
        if changed:
            save_progress(progress)
    return jsonify({"ok": True, "changed": changed})


@app.route("/api/clear_all", methods=["POST"])
def api_clear_all():
    if request.remote_addr not in LOCAL_ADDRESSES:
        return jsonify({"ok": False, "error": "Clear All can only be run from the host machine"}), 403
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    with state_lock:
        if os.path.exists(config.SORTED_DIR):
            for entry in os.listdir(config.SORTED_DIR):
                full = os.path.join(config.SORTED_DIR, entry)
                if os.path.isdir(full):
                    shutil.rmtree(full, ignore_errors=True)
                elif entry != "manual_progress.json":
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        save_progress({})
        save_skipped(set())
        save_rotations({})
        room = load_room()
        room["undo_stack"] = {}
        save_room(room)
    return jsonify({"ok": True})


@app.route("/api/next_person_number")
def api_next_person_number():
    if config.SRC is None:
        return jsonify({"n": 1})
    progress = load_progress()
    return jsonify({"n": next_new_person_number(progress)})


@app.route("/api/join", methods=["POST"])
def api_join():
    # Assigns (or re-fetches) a player/section number for this browser.
    # Only the host machine may set the room size; every other device just
    # gets auto-assigned the next free slot -- no manual picking.
    if config.SRC is None:
        # no folder picked yet -- the background sync loop calls this
        # unconditionally from boot, before the pick-folder gate resolves,
        # so this has to be a harmless no-op rather than touch ROOM_FILE
        # (which is still None at this point)
        return jsonify({"room_full": False, "section": 1, "sections": 1,
                         "is_host": request.remote_addr in LOCAL_ADDRESSES, "no_folder": True})
    data = request.get_json(force=True)
    client_id = str(data.get("client_id") or "")[:64]
    requested_sections = data.get("requested_sections")
    is_actual_host = request.remote_addr in LOCAL_ADDRESSES

    with state_lock:
        room = load_room()

        if requested_sections is not None:
            # the setup/room-size controls are only shown to isHost on the
            # client already; IP-based host detection is fragile across
            # network setups (VPNs, changed DHCP leases, etc.) and blocking
            # on it here just risked locking the real host out of their own
            # controls, so it's not double-enforced server-side
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
            if candidate is None and is_actual_host:
                # the host machine's own browser must never get locked out
                # of its own room by stale assignments left over from a
                # prior session -- grow the room by one slot to fit them
                # instead of refusing, since the host is about to see the
                # setup modal and can shrink it back down anyway
                sections += 1
                room["sections"] = sections
                candidate = sections
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

        # every join is also a heartbeat -- the frontend re-confirms its
        # assignment on a 3s poll (startCategoryAutoSync), so this doubles
        # as "last seen" for the who's-online view without a separate endpoint
        room["last_seen"][client_id] = time.time()

        save_room(room)

    return jsonify({
        "room_full": False,
        "section": assigned,
        "sections": room["sections"],
        "is_host": is_actual_host,
    })


@app.route("/api/kick", methods=["POST"])
def api_kick():
    # frees up a section by removing whoever's assigned to it -- they get
    # auto-reassigned to a new slot (or told the room's full) next time
    # their browser's background sync re-confirms its assignment, no
    # action needed on their end. The Kick button only renders for isHost
    # client-side; not double-enforced here via IP for the same reason
    # room-size changes aren't -- that check is fragile across network
    # setups and risks locking the real host out.
    if config.SRC is None:
        return jsonify({"ok": False, "error": "no folder selected"}), 400
    data = request.get_json(force=True)
    try:
        section = int(data.get("section"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid section"}), 400
    with state_lock:
        room = load_room()
        to_remove = [cid for cid, sec in room["assignments"].items() if sec == section]
        for cid in to_remove:
            del room["assignments"][cid]
        save_room(room)
    return jsonify({"ok": True, "kicked": len(to_remove)})


@app.route("/api/sections_progress")
def api_sections_progress():
    if config.SRC is None:
        return jsonify({"sections": [], "grand_total": 0, "grand_reviewed": 0})
    try:
        sections = max(1, min(16, int(request.args.get("sections", 1))))
    except (TypeError, ValueError):
        sections = 1
    ONLINE_WINDOW_SECS = 8  # background poll is every 3s, so this tolerates ~2 missed beats
    with state_lock:
        files = list_files()
        progress = load_progress()
        skipped = load_skipped()
        room = load_room()
        now = time.time()
        # a section can have more than one stale client_id in "assignments"
        # (e.g. someone rejoined from a new browser) -- take the most
        # recent heartbeat among everyone currently mapped to that section
        section_last_seen = {}
        for cid, sec in room["assignments"].items():
            seen = room["last_seen"].get(cid)
            if seen is not None:
                section_last_seen[sec] = max(section_last_seen.get(sec, 0), seen)
        result = []
        for sec in range(1, sections + 1):
            my_files = partition_slice(files, sec, sections)
            total = len(my_files)
            reviewed = sum(1 for f in my_files if f in progress)
            idx, _ = first_undecided(my_files, progress, skipped)
            current = my_files[idx] if idx < len(my_files) else None
            last_seen = section_last_seen.get(sec)
            online = last_seen is not None and (now - last_seen) < ONLINE_WINDOW_SECS
            result.append({
                "section": sec, "total": total, "reviewed": reviewed, "current": current,
                "online": online,
            })
    return jsonify({
        "sections": result,
        "grand_total": len(files),
        "grand_reviewed": len(progress),
    })


@app.route("/api/export_summary")
def api_export_summary():
    if config.SRC is None:
        return jsonify({"error": "no folder selected"}), 400
    with state_lock:
        progress = load_progress()
        cats, counts = existing_categories(progress)
    total = len(progress)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Category", "Photos", "Percent of sorted"])
    for c in cats:
        n = counts.get(c, 0)
        pct = f"{(n / total * 100):.1f}%" if total else "0.0%"
        writer.writerow([c, n, pct])
    writer.writerow([])
    writer.writerow(["Total sorted", total, ""])

    folder_name = os.path.basename(config.SRC.rstrip(os.sep)) or "event"
    resp = Response(buf.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = f'attachment; filename="{folder_name}_sort_summary.csv"'
    return resp
