import os


def list_dir(path):
    """Return {path, parent, entries: [{name, path, is_dir}]} for `path`.

    Only directories are listed (this browser exists purely to pick a root
    folder), sorted case-insensitively. Raises the same exceptions as
    os.scandir on invalid/inaccessible paths - callers handle those.
    """
    path = os.path.normpath(path)
    if not os.path.isdir(path):
        raise NotADirectoryError(path)

    entries = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                if entry.is_dir():
                    entries.append({"name": entry.name, "path": entry.path, "is_dir": True})
            except OSError:
                continue
    entries.sort(key=lambda e: e["name"].lower())

    parent = os.path.dirname(path)
    if parent == path:
        parent = None

    return {"path": path, "parent": parent, "entries": entries}


def list_drives():
    """Windows drive roots (C:\\, D:\\, ...) to seed the browser when no path is given."""
    drives = []
    if os.name == "nt":
        import string

        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.exists(drive):
                drives.append({"name": drive, "path": drive, "is_dir": True})
    else:
        drives.append({"name": "/", "path": "/", "is_dir": True})
    return drives
