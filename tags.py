"""Reading and writing tags on FLAC and MP3 files, plus disc TOC estimation.

Safety rules for anything that changes a file:
- A full snapshot of every file's tags and embedded art is backed up first.
- Changes are made to a temporary copy, the copy's audio data is checked
  byte-for-byte against the original, and only then does it replace the
  original. If anything fails, the original is untouched.
"""
import base64
import glob
import hashlib
import io
import json
import os
import re
import shutil
import time

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, COMM, ID3, TALB, TCON, TDRC, TIT2, TMED, TPE1,
                         TPE2, TPOS, TPUB, TRCK, TXXX, ID3NoHeaderError)
from mutagen.id3 import delete as delete_id3
from mutagen.mp3 import MP3

AUDIO_EXT = {".flac", ".mp3"}
DISC_DIR_RE = re.compile(r"(?:cd|disc|disk|ディスク)\s*[-_ ]?\s*(\d+)", re.I)
# "Side A.flac", "01 - Side B.mp3", "A.flac", "LP1 Side C.flac"
SIDE_RE = re.compile(r"(?:^|[^a-z])side\s*[-_ ]?\s*([a-h])(?![a-z])|^(?:\d+\s*[-_. ]\s*)?([a-h])(?:\s*[-_.]|$)", re.I)
BACKUP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
TMP_SUFFIX = ".cdtagger-tmp"

# Fields the app reads, compares and writes.
FIELDS = ["title", "artist", "album", "albumartist", "date", "genre", "label",
          "catalognumber", "barcode", "tracknumber", "tracktotal", "discnumber",
          "disctotal", "media", "vinyl_side", "comment", "musicbrainz_albumid"]

# MP3 text frames for each field (track/disc totals are packed into TRCK/TPOS).
ID3_TEXT = {"title": (TIT2, "TIT2"), "artist": (TPE1, "TPE1"), "album": (TALB, "TALB"),
            "albumartist": (TPE2, "TPE2"), "date": (TDRC, "TDRC"), "genre": (TCON, "TCON"),
            "label": (TPUB, "TPUB"), "media": (TMED, "TMED")}
ID3_TXXX = {"catalognumber": "CATALOGNUMBER", "barcode": "BARCODE",
            "musicbrainz_albumid": "MusicBrainz Album Id", "vinyl_side": "VINYL_SIDE"}


def _num(value):
    """'3/12' -> 3, '' -> None."""
    if not value:
        return None
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def _split_pair(value):
    """'3/12' -> ('3', '12'); '3' -> ('3', '')."""
    a, _, b = str(value or "").partition("/")
    return a.strip(), b.strip()


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


# ------------------------------------------------------------ snapshots

def _raw_id3_bytes(path):
    """The ID3v2 tag at the start of an MP3, byte for byte (or None)."""
    with open(path, "rb") as fh:
        head = fh.read(10)
        if len(head) < 10 or head[:3] != b"ID3":
            return None
        size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        size += 10 + (10 if head[5] & 0x10 else 0)
        fh.seek(0)
        return fh.read(size)


def _id3_from_raw(raw):
    return ID3(io.BytesIO(raw)) if raw else None


def snapshot(path):
    """Everything needed to put a file's tags and art back exactly."""
    st = os.stat(path)
    snap = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    if path.lower().endswith(".flac"):
        f = FLAC(path)
        snap.update({
            "format": "flac",
            "tags": [[k, v] for k, v in (f.tags or [])],
            "pictures": [base64.b64encode(p.write()).decode() for p in f.pictures],
        })
    else:
        raw = _raw_id3_bytes(path)
        snap.update({"format": "mp3", "id3": base64.b64encode(raw).decode() if raw else None})
    return snap


def fields_from_snapshot(snap):
    """Normalised {field: value} view of a snapshot, plus has_cover."""
    out = {k: "" for k in FIELDS}
    if snap["format"] == "flac":
        seen = {}
        for k, v in snap["tags"]:
            seen.setdefault(k.lower(), v)
        for k in FIELDS:
            out[k] = seen.get(k, "")
        # Some rippers store "3/12" in TRACKNUMBER.
        for num, tot in (("tracknumber", "tracktotal"), ("discnumber", "disctotal")):
            a, b = _split_pair(out[num])
            out[num] = a
            out[tot] = out[tot] or seen.get("totaltracks" if tot == "tracktotal" else "totaldiscs", "") or b
        out["has_cover"] = bool(snap["pictures"])
        return out
    id3 = _id3_from_raw(base64.b64decode(snap["id3"])) if snap.get("id3") else None
    if id3 is None:
        out["has_cover"] = False
        return out
    def text(fid):
        fr = id3.get(fid)
        return str(fr.text[0]) if fr is not None and getattr(fr, "text", None) else ""
    for k, (_, fid) in ID3_TEXT.items():
        out[k] = text(fid)
    for k, desc in ID3_TXXX.items():
        out[k] = text(f"TXXX:{desc}")
    out["tracknumber"], out["tracktotal"] = _split_pair(text("TRCK"))
    out["discnumber"], out["disctotal"] = _split_pair(text("TPOS"))
    comm = [f for f in id3.getall("COMM") if f.desc == ""]
    out["comment"] = str(comm[0].text[0]) if comm and comm[0].text else ""
    out["has_cover"] = bool(id3.getall("APIC"))
    return out


def embedded_cover(path):
    """(bytes, mime) of the embedded front cover (or first picture), else None."""
    if path.lower().endswith(".flac"):
        pics = FLAC(path).pictures
        pic = next((p for p in pics if p.type == 3), pics[0] if pics else None)
        return (pic.data, pic.mime or "image/jpeg") if pic else None
    try:
        apics = ID3(path).getall("APIC")
    except ID3NoHeaderError:
        return None
    pic = next((p for p in apics if p.type == 3), apics[0] if apics else None)
    return (pic.data, pic.mime or "image/jpeg") if pic else None


def current_fields(path):
    return fields_from_snapshot(snapshot(path))


# ------------------------------------------------------------- reading

def read_file(path, folder):
    ext = os.path.splitext(path)[1].lower()
    info = {"path": path, "name": os.path.relpath(path, folder), "format": ext[1:].upper()}
    if ext == ".flac":
        f = FLAC(path)
        info["length"] = f.info.length
        info["sectors"] = round(f.info.total_samples * 44100 / f.info.sample_rate / 588)
    else:
        f = MP3(path)
        info["length"] = f.info.length
        info["sectors"] = round(f.info.length * 75)
    tags = current_fields(path)

    disc = _num(tags["discnumber"])
    if disc is None:
        m = (DISC_DIR_RE.search(os.path.relpath(os.path.dirname(path), folder))
             or DISC_DIR_RE.search(os.path.basename(os.path.normpath(folder))))
        disc = int(m.group(1)) if m else 1
    track = _num(tags["tracknumber"])
    if track is None:
        m = re.match(r"\s*(\d+)", os.path.basename(path))
        track = int(m.group(1)) if m else None
    stem = os.path.splitext(os.path.basename(path))[0]
    m = SIDE_RE.search(stem)
    side = (tags["vinyl_side"] or (m and (m.group(1) or m.group(2))) or "").upper()

    info.update({k: tags[k] for k in ("title", "artist", "album", "albumartist", "date",
                                      "genre", "label", "catalognumber", "barcode",
                                      "disctotal")})
    info.update({"disc": disc, "track": track, "side": side, "has_cover": tags["has_cover"],
                 "has_tracknumber": bool(tags["tracknumber"]), "discnumber_tag": tags["discnumber"]})
    return info


# ---------------------------------------------------------- library check

GENERIC_TITLE = re.compile(
    r"^(?:track|トラック|audio\s*track|unknown|untitled|no\s*title|title|曲)\s*[-_#.]?\s*\d*$|^\d+$", re.I)
VARIOUS = re.compile(r"^(?:various(?: artists)?|va|v\.a\.|オムニバス)$", re.I)


def audit_album(folder):
    """Read-only check of one album folder. Returns problems, worst first.

    level "major": song titles/artists/album/track numbers/cover missing or generic.
    level "minor": nice-to-have fields such as date, album artist, genre.
    """
    files = scan_folder(folder)
    n = len(files)
    problems = []

    def check(level, label, bad):
        k = sum(1 for f in files if bad(f))
        if k:
            problems.append({"level": level, "text": label + (f" ({k} of {n})" if k != n else "")})

    if not files:
        return {"files": 0, "problems": []}
    check("major", "Missing song titles", lambda f: not f["title"].strip())
    check("major", "Generic song titles like “Track01”",
          lambda f: f["title"].strip() and GENERIC_TITLE.match(f["title"].strip()))
    check("major", "Missing artist", lambda f: not f["artist"].strip())
    check("major", "Song artists are “Various Artists”", lambda f: VARIOUS.match(f["artist"].strip()))
    check("major", "Missing album name", lambda f: not f["album"].strip())
    check("major", "Missing track numbers", lambda f: not f["has_tracknumber"])
    check("major", "No cover art", lambda f: not f["has_cover"])
    if len({f["album"] for f in files if f["album"]}) > 1:
        problems.append({"level": "major", "text": "Files disagree on the album name"})
    check("minor", "No date", lambda f: not f["date"])
    check("minor", "No album artist", lambda f: not f["albumartist"])
    check("minor", "No genre", lambda f: not f["genre"])
    check("minor", "No catalog number", lambda f: not f["catalognumber"])
    return {"files": n, "problems": problems}


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


# -------------------------------------------------------------- backups

def backup(folder, paths, reason):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = re.sub(r"[^\w\-]+", "_", os.path.basename(folder.rstrip("\\/")))[:60]
    dest = os.path.join(BACKUP_DIR, f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.json")
    data = {"version": 2, "folder": os.path.abspath(folder), "created": time.time(),
            "reason": reason, "files": {os.path.abspath(p): snapshot(p) for p in paths}}
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    return dest


def list_backups(folder):
    folder = os.path.abspath(folder)
    out = []
    for p in sorted(glob.glob(os.path.join(BACKUP_DIR, "*.json")), reverse=True):
        try:
            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if data.get("version") == 2 and os.path.normcase(data.get("folder", "")) == os.path.normcase(folder):
            out.append({"file": os.path.basename(p), "created": data["created"],
                        "reason": data.get("reason", ""), "count": len(data["files"])})
    return out


def load_backup(name):
    path = os.path.join(BACKUP_DIR, os.path.basename(name))
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------- safe writing

def _is_flac(path):
    if path.endswith(TMP_SUFFIX):
        path = path[:-len(TMP_SUFFIX)]
    return path.lower().endswith(".flac")


def _audio_hash(path):
    """SHA-1 of just the audio data (tags and art excluded)."""
    with open(path, "rb") as fh:
        data_start, data_end = 0, os.path.getsize(path)
        if _is_flac(path):
            head = fh.read(10)
            # Skip an ID3 tag some tools prepend to FLAC files.
            if head[:3] == b"ID3":
                data_start = 10 + ((head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9])
            fh.seek(data_start)
            if fh.read(4) != b"fLaC":
                raise ValueError("not a FLAC stream")
            last = False
            while not last:
                hdr = fh.read(4)
                last = bool(hdr[0] & 0x80)
                fh.seek(int.from_bytes(hdr[1:4], "big"), 1)
            data_start = fh.tell()
        else:
            raw = _raw_id3_bytes(path)
            data_start = len(raw) if raw else 0
            fh.seek(-128, 2)
            if fh.read(3) == b"TAG":
                data_end -= 128
        h = hashlib.sha1()
        fh.seek(data_start)
        remaining = data_end - data_start
        while remaining > 0:
            chunk = fh.read(min(1 << 20, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
        return h.hexdigest()


def _safely(path, change):
    """Run change(tmp_path) on a copy, verify the audio is identical, then swap in."""
    tmp = path + TMP_SUFFIX
    shutil.copy2(path, tmp)
    try:
        change(tmp)
        before, after = _audio_hash(path), _audio_hash(tmp)
        if before != after:
            raise RuntimeError("audio data changed during tag write; original left untouched")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _write_tags(path, t, cover):
    if _is_flac(path):
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
    # Only fields present in `t` are touched; everything else is left alone.
    for key, (cls, fid) in ID3_TEXT.items():
        if key in t:
            id3.delall(fid)
            if t[key]:
                id3.add(cls(encoding=1, text=str(t[key])))
    for num, tot, cls, fid in (("tracknumber", "tracktotal", TRCK, "TRCK"),
                               ("discnumber", "disctotal", TPOS, "TPOS")):
        if num in t or tot in t:
            old_n, old_t = _split_pair(id3[fid].text[0] if fid in id3 else "")
            n = t.get(num, old_n)
            total = t.get(tot, old_t)
            id3.delall(fid)
            if n:
                id3.add(cls(encoding=1, text=f"{n}/{total}" if total else str(n)))
    for key, desc in ID3_TXXX.items():
        if key in t:
            id3.delall(f"TXXX:{desc}")
            if t[key]:
                id3.add(TXXX(encoding=1, desc=desc, text=str(t[key])))
    if "comment" in t:
        for fr in [f for f in id3.getall("COMM") if f.desc == ""]:
            id3.delall(fr.HashKey)
        if t["comment"]:
            id3.add(COMM(encoding=1, lang="eng", desc="", text=t["comment"]))
    if cover:
        id3.delall("APIC")
        id3.add(APIC(encoding=1, mime=cover[1], type=3, desc="Front Cover", data=cover[0]))
    # ID3v2.3 with UTF-16 text: safest for MediaMonkey, Mp3tag and Windows Explorer.
    id3.save(path, v2_version=3)


def write_file(path, t, cover=None):
    """Write tag dict `t` (keys from FIELDS) to one file. Empty values remove the tag.

    Only keys present in `t` are changed. cover: (bytes, mime) or None to keep
    existing art.
    """
    _safely(path, lambda tmp: _write_tags(tmp, t, cover))


def _restore_tags(path, snap):
    if snap["format"] == "flac":
        f = FLAC(path)
        if f.tags is None:
            f.add_tags()
        f.tags.clear()
        for k, v in snap["tags"]:
            f.tags.append((k, v))
        f.clear_pictures()
        for b in snap["pictures"]:
            f.add_picture(Picture(base64.b64decode(b)))
        f.save()
        return
    delete_id3(path, delete_v1=False, delete_v2=True)
    if snap.get("id3"):
        raw = base64.b64decode(snap["id3"])
        id3 = _id3_from_raw(raw)
        id3.save(path, v2_version=3 if id3.version[1] <= 3 else 4)


def restore_file(path, snap):
    """Put a file's tags and art back to a snapshot taken by backup()."""
    _safely(path, lambda tmp: _restore_tags(tmp, snap))


def stat_key(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime_ns


# ------------------------------------------------------------ cue sheets

def _cue_escape(s):
    return (s or "").replace('"', "'")


def cue_text(path, album, performer, tracks):
    """Cue sheet for a vinyl side file.

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
    return "\r\n".join(lines) + "\r\n"


def cue_path(path):
    return os.path.splitext(path)[0] + ".cue"


def write_side_file(dest, data):
    """Write a cover.jpg / .cue next to the music, keeping any old one as .bak."""
    if os.path.exists(dest):
        shutil.copy2(dest, dest + ".bak")
    tmp = dest + TMP_SUFFIX
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)
