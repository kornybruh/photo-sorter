"""
Constants and the "currently active folder" state.

SRC/SORTED_DIR/etc. are None until a folder is picked (or the last one used
is restored from disk). set_active_folder() reassigns them; every other
module just reads these bare module-level names, so nothing else needs to
change when the active folder changes.
"""
import os
import json

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAST_FOLDER_FILE = os.path.join(APP_DIR, "last_folder.json")
TRASH_CATEGORY = "trash"
PORT = 8765

SRC = None
SORTED_DIR = None
PROGRESS_FILE = None
SKIPPED_FILE = None
ROOM_FILE = None
ROTATIONS_FILE = None


def set_active_folder(path):
    global SRC, SORTED_DIR, PROGRESS_FILE, SKIPPED_FILE, ROOM_FILE, ROTATIONS_FILE
    SRC = os.path.abspath(path)
    SORTED_DIR = os.path.join(SRC, "sorted")
    PROGRESS_FILE = os.path.join(SORTED_DIR, "manual_progress.json")
    SKIPPED_FILE = os.path.join(SORTED_DIR, "skipped.json")
    ROOM_FILE = os.path.join(SORTED_DIR, "multiplayer_room.json")
    ROTATIONS_FILE = os.path.join(SORTED_DIR, "rotations.json")
    os.makedirs(SORTED_DIR, exist_ok=True)
    try:
        with open(LAST_FOLDER_FILE, "w", encoding="utf-8") as f:
            json.dump({"path": SRC}, f)
    except Exception:
        pass


def restore_last_folder():
    if os.path.exists(LAST_FOLDER_FILE):
        try:
            with open(LAST_FOLDER_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f).get("path")
            if saved and os.path.isdir(saved):
                set_active_folder(saved)
        except Exception:
            pass
