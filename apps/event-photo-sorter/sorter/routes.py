"""
All HTTP endpoints: the page itself, plus the JSON API the frontend
(static/js/app.js) talks to.
"""
import io
import os
import shutil

from flask import jsonify, request, Response, render_template

from . import config
from .app import app, state_lock
from .network import LOCAL_ADDRESSES, get_lan_ip
from .logic import (
    sanitize_name, existing_categories, category_last_photo,
    next_new_person_number, first_undecided, get_partition, partition_slice,
)
from .photos import list_files, fast_open, get_photo_info
from .storage import (
    load_progress, save_progress, load_skipped, save_skipped,
    load_room, save_room,
)
from PIL import Image


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


@app.route("/api/photo_info/<path:fname>")
def api_photo_info(fname):
    if config.SRC is None:
        return jsonify({"error": "not found"}), 404
    fname = os.path.basename(fname)
    path = os.path.join(config.SRC, fname)
    if not os.path.exists(path):
        return jsonify({"error": "not found"}), 404
    return jsonify(get_photo_info(path))


def _do_assign(fname, category):
    cat_dir = os.path.join(config.SORTED_DIR, category)
    os.makedirs(cat_dir, exist_ok=True)
    shutil.copy2(os.path.join(config.SRC, fname), os.path.join(cat_dir, fname))
    progress = load_progress()
    progress[fname] = category
    save_progress(progress)
    skipped = load_skipped()
    if fname in skipped:
        skipped.discard(fname)
        save_skipped(skipped)


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
        # Undo undoes what, or lose anyone's spot in the room
        room = load_room()
        room["last_assigned"][f"{section}:{sections}"] = fname
        save_room(room)
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
            progress[fname] = category
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
        fname = room["last_assigned"].pop(key, None)
        save_room(room)
        if not fname or fname not in progress:
            # fall back to the globally most recent assignment
            if not progress:
                return jsonify({"ok": False, "error": "nothing to undo"}), 400
            fname = list(progress.keys())[-1]
        category = progress.pop(fname)
        path = os.path.join(config.SORTED_DIR, category, fname)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass
        save_progress(progress)
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
            if progress[fname] == old_name:
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
            if progress[fname] == category:
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
        for fname, cat in list(progress.items()):
            if not os.path.exists(os.path.join(config.SORTED_DIR, cat, fname)):
                del progress[fname]
                changed = True
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
        room = load_room()
        room["last_assigned"] = {}
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
