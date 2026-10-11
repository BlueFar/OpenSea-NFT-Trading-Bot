"""
Clearing out the Info.md folders of candidates that stopped passing.

When a later check fails a rule, the collection drops off the dashboard's candidate list, and its folders
(one per day it passed) are moved out of the data folder. On a Mac they go to the Trash, so they can still be
put back until the Trash is emptied; elsewhere (or if the Trash can't be reached) to data/_removed. If the
collection passes again, the bot writes a fresh folder.

Only a real rule failure clears folders: a check that failed because OpenSea didn't answer leaves them alone.
"""
import os
import shutil
import sys
from typing import Iterable, List, Optional

from ..utils.logging import setup_logger

logger = setup_logger("candidate_files")

REMOVED_DIR = "_removed"
# Reasons that mean the check couldn't be done, not that a rule failed
DATA_PROBLEM_MARKERS = ("api failed", "could not be retrieved", "missing", "unparseable", "unavailable",
                        "no floor snapshot", "not enough data", "insufficient")


def is_data_problem(reason: Optional[str]) -> bool:
    r = (reason or "").lower()
    return any(m in r for m in DATA_PROBLEM_MARKERS)


def default_trash() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    t = os.path.join(os.path.expanduser("~"), ".Trash")
    return t if os.path.isdir(t) else None


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def _free_name(folder: str, name: str) -> str:
    dest, n = os.path.join(folder, name), 2
    while os.path.exists(dest):
        dest = os.path.join(folder, f"{name} {n}")
        n += 1
    return dest


def remove_info_folder(info_path: str, data_root: str, trash: Optional[str] = None) -> Optional[str]:
    """
    Moves the folder holding info_path out of data_root. Returns where it went, or None when there was
    nothing to move or the path isn't a candidate folder inside data_root (never touches anything else).
    """
    if not info_path or os.path.basename(info_path) != "Info.md":
        return None
    folder = os.path.dirname(os.path.abspath(info_path))
    date_dir = os.path.dirname(folder)
    root = os.path.abspath(data_root)
    # Only DATA_ROOT/<date>/<project>/Info.md
    if not os.path.isfile(info_path) or not _inside(folder, root) or os.path.dirname(date_dir) != root:
        return None
    if os.path.basename(date_dir) == REMOVED_DIR:
        return None
    label = f"{os.path.basename(folder)} ({os.path.basename(date_dir)})"
    dest = None
    if trash:
        try:
            dest = _free_name(trash, label)
            shutil.move(folder, dest)
        except OSError as e:
            logger.warning("Could not move %s to the Trash (%s). Using %s instead.", folder, e, REMOVED_DIR)
            dest = None
    if dest is None:
        target = os.path.join(root, REMOVED_DIR, os.path.basename(date_dir))
        os.makedirs(target, exist_ok=True)
        dest = _free_name(target, os.path.basename(folder))
        shutil.move(folder, dest)
        os.utime(dest)  # the 7 days before it's deleted count from now
    try:
        os.rmdir(date_dir)  # only when that day has no folders left
    except OSError:
        pass
    return dest


def remove_info_folders(paths: Iterable[str], data_root: str, trash: Optional[str] = None) -> List[str]:
    moved = []
    for p in dict.fromkeys(paths):
        try:
            dest = remove_info_folder(p, data_root, trash)
        except OSError as e:
            logger.warning("Could not remove candidate folder for %s: %s", p, e)
            continue
        if dest:
            moved.append(dest)
    return moved


def purge_removed(data_root: str, days: float = 7.0, now: Optional[float] = None) -> int:
    """Deletes folders in data/_removed (used when the Trash can't be reached) after `days` days."""
    import time
    base = os.path.join(os.path.abspath(data_root), REMOVED_DIR)
    if not os.path.isdir(base):
        return 0
    cutoff, n = (now or time.time()) - days * 86400, 0
    for day in os.listdir(base):
        day_dir = os.path.join(base, day)
        if not os.path.isdir(day_dir):
            continue
        for name in os.listdir(day_dir):
            p = os.path.join(day_dir, name)
            try:
                if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                    shutil.rmtree(p)
                    n += 1
            except OSError:
                continue
        try:
            os.rmdir(day_dir)
        except OSError:
            pass
    try:
        os.rmdir(base)
    except OSError:
        pass
    return n
