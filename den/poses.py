"""The pose library: saved poses, each a pose map den drew from a photo, kept under a name.

A saved pose is four files in POSES_DIR: <name>.png (the map), <name>.source.<ext> (the photo
it was drawn from, byte for byte), <name>.keypoints.json (the skeleton's joints in OpenPose
format, as the detector found them) and <name>.json (description, size, when, where the photo
came from); the .json is written last, so it marks a complete one. A request uses a saved pose as
pose:NAME, as a reference or a pose guide image, and a model finds the names with list_poses
(ADR 0003). Only the broker draws and saves one (POST /pose), since drawing takes the GPU;
listing, renaming and deleting happen here.
"""

import base64
import difflib
import json
import os
import re
import struct
import time
import zlib
from pathlib import Path

from den.core import DenError

POSES_DIR = Path(
    os.environ.get("DEN_POSES")
    or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "den/poses"
)
NAME_MAX = 80
DESCRIPTION_MAX = 300
# Lower-case words joined by hyphens: no dot or slash, so pose:NAME never reads as pose:PATH.
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Common ratios, long side first, for the aspect a table of contents shows.
_ASPECTS = [(1, 1), (5, 4), (4, 3), (3, 2), (16, 9), (2, 1)]


def is_name(value):
    return isinstance(value, str) and len(value) <= NAME_MAX and _NAME.fullmatch(value) is not None


def check_name(name):
    if not is_name(name):
        raise DenError(
            f"a pose name is lower-case words joined by hyphens, at most {NAME_MAX} characters "
            f"(e.g. look-back-hand-on-hip), got {name!r}"
        )


def check_description(description):
    """The description, on one line; it's required, since it's all a model picks a pose by."""
    text = " ".join(str(description or "").split())
    if not text:
        raise DenError("a saved pose needs a description: one line on the pose and the framing")
    if len(text) > DESCRIPTION_MAX:
        raise DenError(f"keep the description to one line, at most {DESCRIPTION_MAX} characters")
    return text


def _map(name):
    return POSES_DIR / f"{name}.png"


def _meta(name):
    return POSES_DIR / f"{name}.json"


def _keypoints(name):
    return POSES_DIR / f"{name}.keypoints.json"


def _sources(name):
    # The name is checked, so it holds no glob characters.
    return sorted(POSES_DIR.glob(f"{name}.source.*"))


def exists(name):
    return _meta(name).is_file()


def names():
    if not POSES_DIR.is_dir():
        return []
    return sorted(p.name[: -len(".json")] for p in POSES_DIR.glob("*.json") if is_name(p.name[: -len(".json")]))


def get(name):
    """A saved pose: name, description, width, height, saved, from, map and source paths."""
    check_name(name)
    if not (exists(name) and _map(name).is_file()):
        close = difflib.get_close_matches(name, names(), n=3, cutoff=0.5)
        hint = f"; did you mean {', '.join(close)}?" if close else ""
        raise DenError(f"no saved pose {name!r}{hint} (list_poses or `den pose list` shows them)")
    try:
        meta = json.loads(_meta(name).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise DenError(f"cannot read {_meta(name)}: {e}") from e
    sources = _sources(name)
    keypoints = _keypoints(name)
    return {
        **meta,
        "name": name,
        "map": str(_map(name)),
        "source": str(sources[0]) if sources else None,
        "keypoints": str(keypoints) if keypoints.is_file() else None,
    }


def entries():
    return [get(name) for name in names()]


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
INLINE_MAX_BYTES = 4 << 20


def inline(path):
    """{"base64", "mime"} of an image to show a model, or None when it's missing, an unknown
    type or too big. list_poses shows a pose this way, here or from a remote broker."""
    mime = IMAGE_TYPES.get(Path(path).suffix.lower()) if path else None
    try:
        data = Path(path).read_bytes() if mime else b""
    except OSError:
        return None
    if not data or len(data) > INLINE_MAX_BYTES:
        return None
    return {"base64": base64.b64encode(data).decode(), "mime": mime}


def show(name, files=True):
    """One saved pose for a model to look at: its details, then small copies of the skeleton and
    the photo (list_poses with a name; the broker's GET /poses?name=, without the files)."""
    entry = get(name)
    return {
        "details": details(entry, files) + "\nBelow: the skeleton, then the photo.",
        "images": [i for i in (inline(entry["map"]), inline(entry["source"])) if i],
    }


def aspect(width, height):
    """"portrait 2:3", "landscape 16:9" or "square", to the nearest common ratio."""
    ratio = max(width, height) / min(width, height)
    long_side, short_side = min(_ASPECTS, key=lambda r: abs(r[0] / r[1] - ratio))
    if long_side == short_side:
        return "square"
    if height > width:
        return f"portrait {short_side}:{long_side}"
    return f"landscape {long_side}:{short_side}"


def line(entry):
    w, h = entry["width"], entry["height"]
    return f"- {entry['name']}: {entry['description']} ({aspect(w, h)}, {w}x{h})"


# Where a pose's files are, for a model that has only the name: its stand-in photo is what a
# place image is best edited from (the den-image skill), and the keypoints are the joints as data.
FILES_NOTE = (
    "Each pose's files are in {dir}: NAME.png (the skeleton), NAME.source.<ext> (the photo it was "
    "drawn from), NAME.keypoints.json (the joints in OpenPose format). list_poses with a name shows "
    "one pose with its paths and a preview."
)


def toc(files=True):
    """The table of contents a model keeps: one line per saved pose, and where the files are.
    files=False leaves the files out, for a client on another machine."""
    lines = [line(entry) for entry in entries()]
    if not lines:
        return "The pose library is empty."
    return "\n".join(lines) + ("\n" + FILES_NOTE.format(dir=POSES_DIR) if files else "")


def details(entry, files=True):
    """One pose in full: what it is, its size and aspect, and where each of its files is
    (files=False: without them, for a client on another machine)."""
    w, h = entry["width"], entry["height"]
    lines = [
        f"{entry['name']}: {entry['description']}",
        f"size: {w}x{h} ({aspect(w, h)}); saved {entry.get('saved', '?')}",
    ]
    if files:
        lines += [
            f"skeleton: {entry['map']}",
            f"photo: {entry['source'] or '(missing)'}",
            f"keypoints: {entry['keypoints'] or '(not recorded; den pose refresh draws them)'}",
        ]
    return "\n".join(lines)


def check_taken(name, replace=False):
    if exists(name) and not replace:
        raise DenError(f"there is already a saved pose {name!r}; pass replace to overwrite it")


def check_new(name, description, replace=False):
    """Refuse a save before any drawing: a bad name or description, or a name that's taken."""
    check_name(name)
    check_description(description)
    check_taken(name, replace)


def png_size(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise DenError("the pose map isn't a PNG")
    return struct.unpack(">II", data[16:24])


def is_blank(data):
    """Whether a PNG map has nothing drawn on it: every pixel zero, as SDPose leaves the canvas
    when it finds no person.

    PNG filters predict each byte from its neighbours, so an all-zero image filters to all
    zeros and, working forward from the first pixel, only an all-zero image does: checking the
    decompressed rows past each row's filter byte is enough, without undoing the filters.
    """
    width, height = png_size(data)
    bit_depth, colour, interlace = data[24], data[25], data[28]
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(colour)
    if bit_depth != 8 or channels is None or interlace:
        return False  # not what ComfyUI writes; don't refuse what can't be read cheaply
    idat, pos = b"", 8
    while pos < len(data):
        length, kind = struct.unpack(">I4s", data[pos : pos + 8])
        if kind == b"IDAT":
            idat += data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    raw = zlib.decompress(idat)
    row = width * channels + 1
    return not any(raw[y * row + 1 : (y + 1) * row].strip(b"\0") for y in range(height))


def save(name, description, photo, map_data, replace=False, keypoints=None, origin=None):
    """Keep a drawn map, a copy of its photo and its keypoints under name; returns the saved pose.

    The photo may be the pose's own stored one (a refresh): it's read before anything is removed,
    and the pose keeps the path it originally came from. origin names a photo sent as bytes,
    which the photo path (a temporary file) doesn't."""
    check_new(name, description, replace)
    description = check_description(description)
    photo = Path(photo)
    width, height = png_size(map_data)
    origin = origin or str(photo)
    if photo.resolve().parent == POSES_DIR.resolve() and exists(name):
        origin = get(name).get("from", origin)
    try:
        photo_data = photo.read_bytes()
        POSES_DIR.mkdir(parents=True, exist_ok=True)
        _meta(name).unlink(missing_ok=True)  # incomplete until the new .json is written
        for old in _sources(name):
            old.unlink()
        (POSES_DIR / f"{name}.source{photo.suffix.lower() or '.img'}").write_bytes(photo_data)
        _map(name).write_bytes(map_data)
        if keypoints is not None:
            _keypoints(name).write_text(json.dumps(keypoints) + "\n")
        else:
            _keypoints(name).unlink(missing_ok=True)
        meta = {
            "description": description,
            "width": width,
            "height": height,
            "saved": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "from": origin,
        }
        _meta(name).write_text(json.dumps(meta, indent=2) + "\n")
    except OSError as e:
        raise DenError(f"cannot save pose {name!r} in {POSES_DIR}: {e}") from e
    return get(name)


def _files(name):
    return [_map(name), _meta(name), _keypoints(name), *_sources(name)]


def remove(name):
    get(name)  # fails loudly, with close names, when there is none
    for path in _files(name):
        path.unlink(missing_ok=True)


def rename(old, new, replace=False):
    get(old)
    check_name(new)
    if new == old:
        return get(new)
    if exists(new):
        if not replace:
            raise DenError(f"there is already a saved pose {new!r}; pass --replace to overwrite it")
        remove(new)
    # The .json moves last, so an interrupted rename leaves no complete-looking half.
    files = [_map(old), *_sources(old), *([_keypoints(old)] if _keypoints(old).is_file() else []), _meta(old)]
    moves = [(p, POSES_DIR / (new + p.name[len(old) :])) for p in files]
    for src, dst in moves:
        src.rename(dst)
    return get(new)
