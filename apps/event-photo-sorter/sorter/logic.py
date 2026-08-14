"""
Pure logic: categories, the undecided-photo queue, and splitting the photo
list across multiple players ("partitions").
"""
import os

from . import config


def sanitize_name(name):
    invalid = '<>:"/\\|?*'
    return "".join(ch for ch in (name or "").strip() if ch not in invalid)


def categories_of(entry):
    """A progress[fname] value is normally a single category string, but a
    photo filed into multiple categories at once (see /api/assign_multi)
    stores a list instead -- this normalizes either shape to a list."""
    return entry if isinstance(entry, list) else [entry]


def existing_categories(progress):
    cats = []
    if os.path.exists(config.SORTED_DIR):
        cats = [d for d in os.listdir(config.SORTED_DIR)
                if os.path.isdir(os.path.join(config.SORTED_DIR, d))]
    counts = {}
    for entry in progress.values():
        for c in categories_of(entry):
            counts[c] = counts.get(c, 0) + 1
    cats = [c for c in cats if counts.get(c, 0) > 0]
    if config.TRASH_CATEGORY not in cats:
        cats.append(config.TRASH_CATEGORY)  # always shown, even empty

    def sort_key(name):
        if name == config.TRASH_CATEGORY:
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
    for fname, entry in progress.items():
        if cat in categories_of(entry):
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
