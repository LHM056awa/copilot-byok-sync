#!/usr/bin/env python3
"""Deploy the workspace .vscode/tasks.json to the user-level VS Code tasks.

Merge semantics (never clobbers unrelated user tasks):

* same ``label``  -> the workspace definition replaces the user one in place
                     (keeping its position in the user's array)
* new ``label``    -> appended to the user's tasks array
* everything else in the user-level file (other tasks, ``version``,
  ``dependencies``, ...) is preserved untouched

Safety: no backup file is created.  The write is atomic (temp file +
``os.replace``).  If the user-level file cannot be parsed, or its structure
is not the expected ``{"tasks": [...]}`` object, the script aborts without
touching it.  When everything is already up to date, the file is not
rewritten at all.

Usage (Windows):

    python deploy_user_tasks.py
    python deploy_user_tasks.py --user-dir "C:\\path\\to\\Code\\User"
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from typing import Any, Dict, List, Tuple

DEFAULT_USER_DIR = os.path.join(os.environ.get("APPDATA", ""), "Code", "User")


def _load_json(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def merge_tasks(src_tasks: List[Dict[str, Any]], dst: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int, int]:
    """Merge *src_tasks* into *dst*['tasks'] by label, in place.

    Returns ``(tasks, updated_in_place, appended)``.
    """
    if "tasks" not in dst:
        dst["tasks"] = []
    tasks = dst["tasks"]
    if not isinstance(tasks, list):
        raise ValueError("'tasks' in the user-level file is not a JSON array")

    label_to_index: Dict[str, int] = {}
    for i, entry in enumerate(tasks):
        if isinstance(entry, dict) and isinstance(entry.get("label"), str):
            label_to_index.setdefault(entry["label"], i)

    updated = appended = 0
    for new_task in src_tasks:
        label = new_task.get("label") if isinstance(new_task, dict) else None
        if isinstance(label, str) and label in label_to_index:
            idx = label_to_index[label]
            if tasks[idx] != new_task:
                tasks[idx] = new_task
                updated += 1
        else:
            tasks.append(new_task)
            appended += 1
            if isinstance(label, str):
                label_to_index[label] = len(tasks) - 1
    return tasks, updated, appended


def atomic_write(path: str, text: str) -> None:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tasks-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    ap = argparse.ArgumentParser(description="Merge workspace tasks into the user-level VS Code tasks.json")
    ap.add_argument("--user-dir", default=DEFAULT_USER_DIR,
                    help="directory that should contain tasks.json (default: %APPDATA%/Code/User)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    src_path = os.path.join(here, ".vscode", "tasks.json")
    user_dir = args.user_dir
    dst_path = os.path.join(user_dir, "tasks.json")

    if not os.path.isfile(src_path):
        print(f"error: workspace source not found: {src_path}")
        print("This script must run from the repository root (next to .vscode\\tasks.json).")
        return 2
    if not user_dir or not os.path.isdir(user_dir):
        print(f"error: user-level directory not found: {user_dir or '<APPDATA unset>'}")
        return 2

    src = _load_json(src_path)
    src_tasks = src.get("tasks", [])
    if not isinstance(src_tasks, list):
        print(f"error: {src_path} has no 'tasks' array")
        return 2

    if os.path.isfile(dst_path):
        try:
            dst = _load_json(dst_path)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"error: {dst_path} is not valid JSON ({exc}); leaving it untouched")
            return 2
        if not isinstance(dst, dict):
            print(f"error: {dst_path} is not a JSON object; leaving it untouched")
            return 2
        try:
            tasks, updated, appended = merge_tasks(src_tasks, dst)
        except ValueError as exc:
            print(f"error: {exc}; leaving {dst_path} untouched")
            return 2
    else:
        dst = copy.deepcopy(src)
        tasks = dst.get("tasks", [])
        updated, appended = 0, len(src_tasks)
        print(f"note: {dst_path} did not exist; creating it from the workspace file")

    text = json.dumps(dst, indent=4) + "\n"
    if updated == 0 and appended == 0 and os.path.isfile(dst_path):
        with open(dst_path, encoding="utf-8") as fh:
            if fh.read() == text:
                labels = [t.get("label", "<no label>") for t in tasks if isinstance(t, dict)]
                print(f"OK: {dst_path} already up to date (nothing written)")
                print("    labels: " + ", ".join(labels))
                return 0

    atomic_write(dst_path, text)
    kept = len(tasks) - updated - appended
    labels = [t.get("label", "<no label>") for t in tasks if isinstance(t, dict)]
    print(f"OK: updated {dst_path}")
    print(f"    updated in place: {updated}   appended: {appended}   kept as-is: {kept}")
    print("    labels: " + ", ".join(labels))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
