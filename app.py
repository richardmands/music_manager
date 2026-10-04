"""CD Tagger: a local web app for finding and writing metadata for ripped CDs and vinyl.

Run:  python app.py   then open http://127.0.0.1:5173
"""
import io
import json
import os
import uuid
import webbrowser

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


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
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


@app.get("/api/browse")
def browse():
    path = request.args.get("path") or load_config()["music_root"]
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return err(f"Not a folder: {path}")
    dirs = []
    try:
        for e in sorted(os.scandir(path), key=lambda e: e.name.lower()):
            if e.is_dir() and not e.name.startswith("."):
                dirs.append(e.name)
    except PermissionError:
        pass
    has_audio = any(os.path.splitext(n)[1].lower() in tags.AUDIO_EXT for n in os.listdir(path))
    parent = os.path.dirname(path)
    return jsonify({"path": path, "parent": parent if parent != path else None,
                    "dirs": dirs, "has_audio": has_audio})


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


@app.post("/api/write")
def write():
    body = request.json or {}
    folder = os.path.abspath(body.get("path", ""))
    allowed = {os.path.abspath(p) for p in tags.list_audio(folder)}
    items = body.get("files", [])
    for it in items:
        if os.path.abspath(it["path"]) not in allowed:
            return err(f"Refusing to write outside the album folder: {it['path']}")

    cover = None
    c = body.get("cover") or {}
    try:
        if c.get("upload_id") and c["upload_id"] in uploads:
            data = uploads[c["upload_id"]]["data"]
            cover = (cover_jpeg(data), "image/jpeg")
        elif c.get("url"):
            data, mime = sources.fetch_image(c["url"])
            cover = (cover_jpeg(data), "image/jpeg")
    except Exception as e:
        return err(f"Could not get cover art: {e}", 502)

    backup_path = tags.backup(folder, [it["path"] for it in items])
    written, cues = 0, []
    for it in items:
        tags.write_file(it["path"], it["tags"], cover)
        written += 1
        if it.get("cue"):
            cues.append(tags.write_cue(it["path"], it["tags"].get("album", ""),
                                       it["tags"].get("albumartist", ""), it["cue"]))
    cover_file = None
    if cover and body.get("save_cover_file"):
        cover_file = os.path.join(folder, load_config()["cover_filename"])
        with open(cover_file, "wb") as fh:
            fh.write(cover[0])
    return jsonify({"written": written, "backup": backup_path, "cues": cues, "cover_file": cover_file})


def cover_jpeg(data, max_edge=1200):
    """Normalise cover art to a reasonably sized JPEG."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((max_edge, max_edge))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue()


if __name__ == "__main__":
    import sys
    port = load_config()["port"]
    if "--no-browser" not in sys.argv:
        webbrowser.open(f"http://127.0.0.1:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
