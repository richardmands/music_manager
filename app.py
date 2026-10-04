"""CD Tagger: a local web app for finding and writing metadata for ripped CDs and vinyl.

Run:  python app.py   then open http://127.0.0.1:5173
"""
import io
import json
import os
import re
import subprocess
import sys
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageOps

from flask import Flask, jsonify, request, send_file, send_from_directory

import sources
import tags
import vision

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
DEFAULT_CONFIG = {
    "music_root": os.path.expanduser("~/Music"),
    "discogs_token": "",
    "anthropic_api_key": "",          # optional; otherwise ANTHROPIC_API_KEY env var
    "language": "ja",                 # ja | romaji | en (VGMdb name preference)
    "cover_filename": "cover.jpg",    # saved next to the files when enabled
    "port": 5173,
}

app = Flask(__name__, static_folder="static")
uploads = {}  # id -> {"name", "data"}; photos live in memory for this session
# Reviewed change sets waiting for the user to press SAVE: token -> plan.
# Nothing is written to music files except by /api/save with one of these.
plans = {}
PLAN_TTL = 30 * 60


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8-sig") as fh:
            text = fh.read()
        try:
            cfg.update(json.loads(text))
        except json.JSONDecodeError:
            # Allow Windows paths typed with single backslashes, e.g. "F:\Music".
            cfg.update(json.loads(re.sub(r'\\(?![\\"/bfnrtu])', r"\\\\", text)))
    return cfg


def err(msg, code=400):
    return jsonify({"error": msg}), code


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def get_config():
    cfg = load_config()
    return jsonify({
        "music_root": cfg["music_root"], "language": cfg["language"],
        "has_discogs": bool(cfg["discogs_token"]),
        "has_claude": bool(cfg["anthropic_api_key"] or os.environ.get("ANTHROPIC_API_KEY")),
        "cover_filename": cfg["cover_filename"],
    })


PICK_FOLDER_SCRIPT = r"""
import sys, tkinter as tk
from tkinter import filedialog
root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
path = filedialog.askdirectory(parent=root, initialdir=sys.argv[1], mustexist=True,
                               title="Choose an album folder")
sys.stdout.write(path or "")
"""


@app.post("/api/pick-folder")
def pick_folder():
    """Open the standard Windows folder picker on this PC and return the choice."""
    start = (request.json or {}).get("start") or load_config()["music_root"]
    if not os.path.isdir(start):
        start = os.path.expanduser("~")
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # A separate process so the dialog gets its own UI thread.
    out = subprocess.run([sys.executable, "-c", PICK_FOLDER_SCRIPT, start],
                         capture_output=True, env=env, timeout=600)
    path = out.stdout.decode("utf-8").strip()
    return jsonify({"path": os.path.normpath(path) if path else None})


def find_album_folders(root, max_depth=4):
    """Folders holding music directly, with CD1/CD2-style subfolders folded into their parent."""
    albums = set()
    root_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirs, files in os.walk(root):
        dirs.sort()
        if dirpath.count(os.sep) - root_depth >= max_depth:
            dirs[:] = []
        if any(os.path.splitext(f)[1].lower() in tags.AUDIO_EXT for f in files):
            parent = os.path.dirname(dirpath)
            if tags.DISC_DIR_RE.search(os.path.basename(dirpath)) and parent != root.rstrip("\\/"):
                albums.add(parent)
            else:
                albums.add(dirpath)
    return sorted(albums, key=str.lower)


@app.get("/api/audit")
def audit():
    """Read-only check of the whole library (or one album with ?album=)."""
    root = os.path.abspath(request.args.get("path") or load_config()["music_root"])
    single = request.args.get("album")
    if not os.path.isdir(single or root):
        return err(f"Not a folder: {single or root}")
    folders = [single] if single else find_album_folders(root)

    def one(folder):
        try:
            r = tags.audit_album(folder)
        except Exception as e:
            r = {"files": 0, "problems": [{"level": "major", "text": f"Could not read: {e}"}]}
        return {"path": folder, "name": os.path.relpath(folder, root) if not single else os.path.basename(folder), **r}

    with ThreadPoolExecutor(max_workers=8) as pool:
        albums = list(pool.map(one, folders))
    return jsonify({"root": root, "albums": albums})


@app.post("/api/scan")
def scan():
    path = (request.json or {}).get("path", "")
    if not os.path.isdir(path):
        return err(f"Not a folder: {path}")
    files = tags.scan_folder(path)
    if not files:
        return err("No FLAC or MP3 files in that folder.")
    first = files[0]
    return jsonify({
        "path": path, "files": files,
        "tocs": tags.disc_tocs(files),
        "guess": {"artist": first["albumartist"] or first["artist"], "album": first["album"],
                  "catno": first["catalognumber"], "barcode": first["barcode"]},
        "looks_like_vinyl": sum(1 for f in files if f["side"]) == len(files) and len(files) <= 12,
    })


@app.post("/api/search")
def search():
    body = request.json or {}
    cfg = load_config()
    criteria = {k: (body.get(k) or "").strip() for k in ("artist", "album", "catno", "barcode", "query")}
    if not any(criteria.values()):
        return err("Enter something to search for.")
    results, errors = sources.search_all(body.get("sources") or ["musicbrainz", "discogs"],
                                         cfg, lang=body.get("lang") or cfg["language"], **criteria)
    return jsonify({"results": results, "errors": errors})


@app.post("/api/toc")
def toc_lookup():
    """MusicBrainz lookup by the disc's track layout (works for CD rips, not vinyl)."""
    body = request.json or {}
    files = tags.scan_folder(body.get("path", ""))
    results, errors = [], {}
    for disc, toc in tags.disc_tocs(files).items():
        try:
            for r in sources.mb_toc_lookup(toc):
                r["matched_disc"] = disc
                results.append(r)
        except Exception as e:
            errors[f"musicbrainz disc {disc}"] = str(e)[:300]
    return jsonify({"results": results, "errors": errors})


@app.get("/api/release")
def release():
    cfg = load_config()
    try:
        return jsonify(sources.get_release(request.args["source"], request.args["id"], cfg,
                                           lang=request.args.get("lang") or cfg["language"]))
    except Exception as e:
        return err(f"Could not load release: {e}", 502)


@app.post("/api/upload")
def upload():
    ids = []
    for f in request.files.getlist("images"):
        uid = uuid.uuid4().hex
        uploads[uid] = {"name": f.filename, "data": f.read()}
        ids.append({"id": uid, "name": f.filename})
    return jsonify({"uploads": ids})


@app.get("/api/upload/<uid>")
def get_upload(uid):
    u = uploads.get(uid)
    if not u:
        return err("Not found", 404)
    return send_file(io.BytesIO(u["data"]), download_name=u["name"])


@app.get("/api/cover-proxy")
def cover_proxy():
    """Fetch remote cover art server-side (some sites block hotlinking)."""
    try:
        data, mime = sources.fetch_image(request.args["url"])
    except Exception as e:
        return err(str(e), 502)
    return send_file(io.BytesIO(data), mimetype=mime)


@app.post("/api/identify")
def identify():
    body = request.json or {}
    cfg = load_config()
    images = []
    for item in body.get("images", []):
        u = uploads.get(item.get("id"))
        if u:
            images.append((item.get("label") or u["name"], u["data"]))
    if not images:
        return err("Add at least one photo first.")
    try:
        raw = vision.identify(images, hints=body.get("hints", ""),
                              rip_summary=body.get("rip_summary", ""),
                              use_web=bool(body.get("web")),
                              api_key=cfg["anthropic_api_key"] or None)
    except Exception as e:
        return err(f"Claude lookup failed: {e}", 502)
    found = vision.to_release(raw)
    # Use what Claude read to search the free databases too.
    results, errors = [], {}
    for key, value in (("catno", found["catalog"]), ("barcode", found["barcode"])):
        if value:
            r, e = sources.search_all(body.get("sources") or ["musicbrainz", "discogs"], cfg,
                                      lang=cfg["language"], **{key: value})
            results += [x for x in r if (x["source"], x["id"]) not in {(y["source"], y["id"]) for y in results}]
            errors.update(e)
    return jsonify({"release": found, "results": results, "errors": errors})


def _album_files(folder):
    return {os.path.normcase(os.path.abspath(p)): p for p in tags.list_audio(folder)}


def _new_plan(kind, folder, paths, **extra):
    token = uuid.uuid4().hex
    plans[token] = {"kind": kind, "folder": folder, "created": time.time(),
                    "stats": {p: tags.stat_key(p) for p in paths}, **extra}
    return token


@app.post("/api/preview")
def preview():
    """Work out exactly what SAVE would change. Writes nothing."""
    body = request.json or {}
    folder = os.path.abspath(body.get("path", ""))
    allowed = _album_files(folder)
    items = []
    for it in body.get("files", []):
        real = allowed.get(os.path.normcase(os.path.abspath(it["path"])))
        if not real:
            return err(f"Not a music file in this album folder: {it['path']}")
        clean = {k: str(v if v is not None else "").strip()
                 for k, v in it["tags"].items() if k in tags.FIELDS}
        items.append({"path": real, "tags": clean, "cue": it.get("cue")})

    cover = None
    c = body.get("cover") or {}
    try:
        if c.get("upload_id") and c["upload_id"] in uploads:
            cover = (cover_jpeg(uploads[c["upload_id"]]["data"]), "image/jpeg")
        elif c.get("url"):
            cover = (cover_jpeg(sources.fetch_image(c["url"])[0]), "image/jpeg")
    except Exception as e:
        return err(f"Could not get cover art: {e}", 502)

    files, side_files = [], []
    for it in items:
        old = tags.current_fields(it["path"])
        changes = [{"field": k, "old": old.get(k, ""), "new": v}
                   for k, v in it["tags"].items() if (old.get(k) or "") != v]
        files.append({"name": os.path.relpath(it["path"], folder), "changes": changes,
                      "cover": ("replace" if old["has_cover"] else "add") if cover else None})
        if it.get("cue"):
            text = tags.cue_text(it["path"], it["tags"].get("album", ""),
                                 it["tags"].get("albumartist", ""), it["cue"])
            side_files.append({"dest": tags.cue_path(it["path"]), "data": text.encode("utf-8-sig")})
    if cover and body.get("save_cover_file"):
        side_files.append({"dest": os.path.join(folder, load_config()["cover_filename"]),
                           "data": cover[0]})

    token = _new_plan("tags", folder, [it["path"] for it in items], items=items, cover=cover,
                      side_files=side_files)
    return jsonify({
        "token": token, "kind": "tags", "files": files, "has_cover": bool(cover),
        "side_files": [{"name": os.path.relpath(f["dest"], folder),
                        "action": "replace" if os.path.exists(f["dest"]) else "create"}
                       for f in side_files],
    })


@app.get("/api/plan/<token>/cover")
def plan_cover(token):
    plan = plans.get(token)
    if not plan or not plan.get("cover"):
        return err("Not found", 404)
    return send_file(io.BytesIO(plan["cover"][0]), mimetype=plan["cover"][1])


@app.get("/api/backups")
def backups():
    return jsonify({"backups": tags.list_backups(request.args.get("path", ""))})


@app.post("/api/restore-preview")
def restore_preview():
    """Show what undoing to a backup would change. Writes nothing."""
    body = request.json or {}
    folder = os.path.abspath(body.get("path", ""))
    try:
        data = tags.load_backup(body.get("backup", ""))
    except (OSError, ValueError):
        return err("Backup not found.")
    allowed = _album_files(folder)
    snaps, files, missing = {}, [], []
    for path, snap in data["files"].items():
        real = allowed.get(os.path.normcase(path))
        if not real:
            missing.append(os.path.basename(path))
            continue
        old, new = tags.current_fields(real), tags.fields_from_snapshot(snap)
        changes = [{"field": k, "old": old.get(k, ""), "new": new.get(k, "")}
                   for k in tags.FIELDS if (old.get(k) or "") != (new.get(k) or "")]
        files.append({"name": os.path.relpath(real, folder), "changes": changes,
                      "cover": "restore" if old["has_cover"] or new["has_cover"] else None})
        snaps[real] = snap
    token = _new_plan("restore", folder, list(snaps), snaps=snaps, backup=body.get("backup"))
    return jsonify({"token": token, "kind": "restore", "files": files, "missing": missing,
                    "side_files": [], "has_cover": False, "created": data["created"]})


@app.post("/api/save")
def save():
    """Apply a reviewed plan, exactly as it was previewed."""
    token = (request.json or {}).get("token", "")
    plan = plans.pop(token, None)
    if not plan:
        return err("This review has expired or was already saved. Review the changes again.")
    if time.time() - plan["created"] > PLAN_TTL:
        return err("This review is more than 30 minutes old. Review the changes again.")
    changed = [p for p, st in plan["stats"].items()
               if not os.path.exists(p) or tags.stat_key(p) != st]
    if changed:
        return err("These files changed since you reviewed them (maybe in Mp3tag or MediaMonkey), "
                   "so nothing was saved. Load the folder and review again: "
                   + ", ".join(os.path.basename(p) for p in changed))

    paths = list(plan["stats"])
    backup_path = tags.backup(plan["folder"], paths,
                              "before undo" if plan["kind"] == "restore" else "before save")
    saved, failed = [], []
    for p in paths:
        try:
            if plan["kind"] == "restore":
                tags.restore_file(p, plan["snaps"][p])
            else:
                it = next(i for i in plan["items"] if i["path"] == p)
                tags.write_file(p, it["tags"], plan["cover"])
            saved.append(os.path.basename(p))
        except Exception as e:
            failed.append(f"{os.path.basename(p)}: {e}")
    extra = []
    for f in plan.get("side_files", []):
        try:
            tags.write_side_file(f["dest"], f["data"])
            extra.append(os.path.basename(f["dest"]))
        except Exception as e:
            failed.append(f"{os.path.basename(f['dest'])}: {e}")
    return jsonify({"saved": saved, "failed": failed, "side_files": extra,
                    "backup": os.path.basename(backup_path)})


def cover_jpeg(data, max_edge=1200):
    """Normalise cover art to a reasonably sized JPEG."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


if __name__ == "__main__":
    port = load_config()["port"]
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
