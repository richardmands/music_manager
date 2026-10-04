"""Reading and writing tags on FLAC and MP3 files, plus disc TOC estimation."""
import json
import os
import re
import time

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TMED, TPE1,
                         TPE2, TPOS, TPUB, TRCK, TXXX, ID3NoHeaderError)
from mutagen.mp3 import MP3

AUDIO_EXT = {".flac", ".mp3"}
DISC_DIR_RE = re.compile(r"(?:cd|disc|disk|ディスク)\s*[-_ ]?\s*(\d+)", re.I)
# "Side A.flac", "01 - Side B.mp3", "A.flac", "LP1 Side C.flac"
SIDE_RE = re.compile(r"(?:^|[^a-z])side\s*[-_ ]?\s*([a-h])(?![a-z])|^(?:\d+\s*[-_. ]\s*)?([a-h])(?:\s*[-_.]|$)", re.I)
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")


def _num(value):
    """'3/12' -> 3, '' -> None."""
    if not value:
        return None
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def _first(tags, key):
    v = tags.get(key)
    if isinstance(v, list):
        return v[0] if v else ""
    return v or ""


def list_audio(folder):
    """All FLAC/MP3 files in an album folder (including CD1/CD2 subfolders)."""
    found = []
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        # Only go one level deep: album folder + disc subfolders.
        if os.path.relpath(root, folder).count(os.sep) >= 1:
            dirs[:] = []
        for name in sorted(files):
            if os.path.splitext(name)[1].lower() in AUDIO_EXT:
                found.append(os.path.join(root, name))
    return found


def read_file(path, folder):
    ext = os.path.splitext(path)[1].lower()
    info = {"path": path, "name": os.path.relpath(path, folder), "format": ext[1:].upper()}
    tags = {}
    has_cover = False
    if ext == ".flac":
        f = FLAC(path)
        tags = {k.lower(): v for k, v in (f.tags or {}).items()} if f.tags else {}
        tags = {k: _first(tags, k) for k in tags}
        info["length"] = f.info.length
        info["sectors"] = round(f.info.total_samples * 44100 / f.info.sample_rate / 588)
        has_cover = bool(f.pictures)
    else:
        f = MP3(path)
        info["length"] = f.info.length
        info["sectors"] = round(f.info.length * 75)
        id3 = f.tags or {}
        def t(fid):
            fr = id3.get(fid)
            return str(fr.text[0]) if fr and getattr(fr, "text", None) else ""
        tags = {
            "title": t("TIT2"), "artist": t("TPE1"), "album": t("TALB"),
            "albumartist": t("TPE2"), "date": t("TDRC"), "tracknumber": t("TRCK"),
            "discnumber": t("TPOS"), "label": t("TPUB"),
            "catalognumber": t("TXXX:CATALOGNUMBER"), "barcode": t("TXXX:BARCODE"),
            "vinyl_side": t("TXXX:VINYL_SIDE"),
        }
        has_cover = any(k.startswith("APIC") for k in id3.keys())

    disc = _num(tags.get("discnumber"))
    if disc is None:
        m = DISC_DIR_RE.search(os.path.relpath(os.path.dirname(path), folder))
        disc = int(m.group(1)) if m else 1
    track = _num(tags.get("tracknumber"))
    if track is None:
        m = re.match(r"\s*(\d+)", os.path.basename(path))
        track = int(m.group(1)) if m else None

    stem = os.path.splitext(os.path.basename(path))[0]
    m = SIDE_RE.search(stem)
    side = (tags.get("vinyl_side") or (m and (m.group(1) or m.group(2))) or "").upper()

    info.update({
        "disc": disc, "track": track, "side": side,
        "title": tags.get("title", ""), "artist": tags.get("artist", ""),
        "album": tags.get("album", ""), "albumartist": tags.get("albumartist", ""),
        "date": tags.get("date", ""), "label": tags.get("label", ""),
        "catalognumber": tags.get("catalognumber", ""), "barcode": tags.get("barcode", ""),
        "has_cover": has_cover,
    })
    return info


def scan_folder(folder):
    files = [read_file(p, folder) for p in list_audio(folder)]
    files.sort(key=lambda f: (f["disc"], f["track"] if f["track"] is not None else 9999, f["name"]))
    return files


def disc_tocs(files):
    """Estimate each disc's TOC from track lengths (exact for FLAC rips).

    Returns {disc: {"tracks": n, "leadout": x, "offsets": [...]}} using the
    standard 150-sector pregap, suitable for MusicBrainz fuzzy TOC lookup.
    """
    tocs = {}
    for f in files:
        tocs.setdefault(f["disc"], []).append(f["sectors"])
    out = {}
    for disc, sectors in tocs.items():
        offsets, pos = [], 150
        for s in sectors:
            offsets.append(pos)
            pos += s
        out[disc] = {"tracks": len(sectors), "leadout": pos, "offsets": offsets}
    return out


def _raw_tags(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        f = FLAC(path)
        return {k: list(v) for k, v in (f.tags or {}).items()} if f.tags else {}
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        return {}
    return {k: str(v) for k, v in id3.items() if not k.startswith("APIC")}


def backup(folder, paths):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = re.sub(r"[^\w\-]+", "_", os.path.basename(folder.rstrip("\\/")))[:60]
    dest = os.path.join(BACKUP_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump({p: _raw_tags(p) for p in paths}, fh, ensure_ascii=False, indent=1)
    return dest


def write_file(path, t, cover=None):
    """Write tag dict `t` to one file. Empty values remove the tag.

    t keys: title artist album albumartist date tracknumber tracktotal
            discnumber disctotal label catalognumber barcode genre
            musicbrainz_albumid comment media vinyl_side
    cover: (bytes, mime) or None to leave existing art alone.
    """
    ext = os.path.splitext(path)[1].lower()
    if ext == ".flac":
        f = FLAC(path)
        if f.tags is None:
            f.add_tags()
        for key, value in t.items():
            if value in (None, ""):
                if key in f.tags:
                    del f.tags[key]
            else:
                f.tags[key] = str(value)
        if cover:
            f.clear_pictures()
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, cover[1], "Front Cover", cover[0]
            f.add_picture(pic)
        f.save()
        return

    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()

    def put(frame_cls, fid, value, **kw):
        id3.delall(fid)
        if value not in (None, ""):
            id3.add(frame_cls(encoding=1, text=str(value), **kw))

    put(TIT2, "TIT2", t.get("title"))
    put(TPE1, "TPE1", t.get("artist"))
    put(TALB, "TALB", t.get("album"))
    put(TPE2, "TPE2", t.get("albumartist"))
    put(TDRC, "TDRC", t.get("date"))
    put(TPUB, "TPUB", t.get("label"))
    put(TCON, "TCON", t.get("genre"))
    put(TMED, "TMED", t.get("media"))
    if "comment" in t:
        id3.delall("COMM")
        if t["comment"]:
            id3.add(COMM(encoding=1, lang="eng", desc="", text=t["comment"]))
    trk = t.get("tracknumber")
    if trk and t.get("tracktotal"):
        trk = f"{trk}/{t['tracktotal']}"
    put(TRCK, "TRCK", trk)
    pos = t.get("discnumber")
    if pos and t.get("disctotal"):
        pos = f"{pos}/{t['disctotal']}"
    put(TPOS, "TPOS", pos)
    for key, desc in (("catalognumber", "CATALOGNUMBER"), ("barcode", "BARCODE"),
                      ("musicbrainz_albumid", "MusicBrainz Album Id"),
                      ("vinyl_side", "VINYL_SIDE")):
        id3.delall(f"TXXX:{desc}")
        if t.get(key):
            id3.add(TXXX(encoding=1, desc=desc, text=str(t[key])))
    if cover:
        id3.delall("APIC")
        id3.add(APIC(encoding=1, mime=cover[1], type=3, desc="Front Cover", data=cover[0]))
    # ID3v2.3 with UTF-16 text: safest for MediaMonkey, Mp3tag and Windows Explorer.
    id3.save(path, v2_version=3)


def _cue_escape(s):
    return (s or "").replace('"', "'")


def write_cue(path, album, performer, tracks):
    """Write `<file>.cue` next to a vinyl side file.

    tracks: [{"title", "artist", "start"}] with start in seconds. Start times
    come from the release's listed track lengths, so they are estimates.
    """
    lines = [f'PERFORMER "{_cue_escape(performer)}"', f'TITLE "{_cue_escape(album)}"',
             f'FILE "{_cue_escape(os.path.basename(path))}" WAVE']
    for i, t in enumerate(tracks, 1):
        frames = round(float(t.get("start") or 0) * 75)
        mm, rem = divmod(frames, 75 * 60)
        ss, ff = divmod(rem, 75)
        lines += [f"  TRACK {i:02d} AUDIO", f'    TITLE "{_cue_escape(t.get("title"))}"',
                  f'    PERFORMER "{_cue_escape(t.get("artist") or performer)}"',
                  f"    INDEX 01 {mm:02d}:{ss:02d}:{ff:02d}"]
    dest = os.path.splitext(path)[0] + ".cue"
    # UTF-8 with BOM so Windows players read Japanese text correctly.
    with open(dest, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return dest
