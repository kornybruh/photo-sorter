"""
Load/save the small JSON files that live inside <folder>/sorted/:
manual_progress.json, skipped.json, multiplayer_room.json.

Always reads config.PROGRESS_FILE etc. via the config module (not imported
by name) so these functions pick up whichever folder is currently active,
even if it changes after this module was first imported.
"""
import os
import json

from . import config


def load_progress():
    if os.path.exists(config.PROGRESS_FILE):
        with open(config.PROGRESS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_progress(progress):
    with open(config.PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def load_skipped():
    if os.path.exists(config.SKIPPED_FILE):
        with open(config.SKIPPED_FILE, "r") as f:
            return set(json.load(f))
    return set()


def save_skipped(skipped):
    with open(config.SKIPPED_FILE, "w") as f:
        json.dump(sorted(skipped), f, indent=2)


def load_room():
    if os.path.exists(config.ROOM_FILE):
        try:
            with open(config.ROOM_FILE, "r") as f:
                room = json.load(f)
            room.setdefault("sections", 1)
            room.setdefault("assignments", {})
            room.setdefault("undo_stack", {})
            room.setdefault("last_seen", {})
            return room
        except Exception:
            pass
    return {"sections": 1, "assignments": {}, "undo_stack": {}, "last_seen": {}}


def save_room(room):
    with open(config.ROOM_FILE, "w") as f:
        json.dump(room, f, indent=2)


def load_rotations():
    if os.path.exists(config.ROTATIONS_FILE):
        with open(config.ROTATIONS_FILE, "r") as f:
            return json.load(f)
    return {}


def save_rotations(rotations):
    with open(config.ROTATIONS_FILE, "w") as f:
        json.dump(rotations, f, indent=2)
