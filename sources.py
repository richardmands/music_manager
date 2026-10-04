"""Free metadata sources: MusicBrainz, VGMdb (via vgmdb.info) and Discogs.

Every source returns releases in one shape:
  summary: source id url title artist date country label catalog barcode
           track_count format
  full:    summary + album_artist cover_url discs=[{number, tracks=[{number,
           title, artist, length, position}]}]
           (length in seconds or None; position is the printed position such
            as "A1" on vinyl, "" when unknown)
"""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

UA = "CDTagger/0.1 (personal music library tagger)"
session = requests.Session()
session.headers["User-Agent"] = UA

_mb_lock = threading.Lock()
_mb_last = [0.0]


def _get(url, params=None, headers=None, timeout=20):
    r = session.get(url, params=params, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _mb_get(path, params):
    # MusicBrainz allows ~1 request/second.
    with _mb_lock:
        wait = 1.05 - (time.time() - _mb_last[0])
        if wait > 0:
            time.sleep(wait)
        _mb_last[0] = time.time()
    params = dict(params, fmt="json")
    return _get(f"https://musicbrainz.org/ws/2/{path}", params)


def _parse_length(s):
    """'3:45' -> 225."""
    if not s:
        return None
    try:
        parts = [int(p) for p in str(s).split(":")]
    except ValueError:
        return None
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def norm_catno(s):
    return re.sub(r"[\s\-‐－]", "", (s or "")).upper()


# ---------------------------------------------------------------- MusicBrainz

def _mb_credit(credits):
    return "".join(c.get("name", "") + c.get("joinphrase", "") for c in credits or [])


def _mb_summary(r):
    li = r.get("label-info") or []
    media = r.get("media") or []
    return {
        "source": "musicbrainz", "id": r["id"],
        "url": f"https://musicbrainz.org/release/{r['id']}",
        "title": r.get("title", ""),
        "artist": _mb_credit(r.get("artist-credit")),
        "date": r.get("date", ""), "country": r.get("country", ""),
        "label": ", ".join(sorted({(l.get("label") or {}).get("name", "") for l in li} - {""})),
        "catalog": ", ".join(sorted({l.get("catalog-number") or "" for l in li} - {""})),
        "barcode": r.get("barcode") or "",
        "track_count": r.get("track-count") or sum(m.get("track-count", 0) for m in media),
        "format": " + ".join(f"{m.get('format') or '?'}" for m in media),
    }


def mb_search(artist="", album="", catno="", barcode="", query=""):
    if catno:
        q = f'catno:"{catno}"'
        # MusicBrainz is inconsistent about hyphens in catalog numbers.
        if "-" in catno:
            q += f' OR catno:"{catno.replace("-", "")}"'
    elif barcode:
        q = f"barcode:{barcode}"
    elif artist or album:
        parts = []
        if album:
            parts.append(f'release:"{album.replace(chr(34), "")}"')
        if artist:
            parts.append(f'artist:"{artist.replace(chr(34), "")}"')
        q = " AND ".join(parts)
    else:
        q = query
    data = _mb_get("release", {"query": q, "limit": 25})
    return [_mb_summary(r) for r in data.get("releases", [])]


def mb_toc_lookup(toc):
    """Fuzzy TOC lookup. toc = {"tracks", "leadout", "offsets"}."""
    toc_str = " ".join(str(x) for x in [1, toc["tracks"], toc["leadout"], *toc["offsets"]])
    data = _mb_get("discid/-", {"toc": toc_str, "inc": "artist-credits labels", "cdstubs": "no"})
    return [_mb_summary(r) for r in data.get("releases", [])]


def mb_release(release_id, lang=None):
    r = _mb_get(f"release/{release_id}", {"inc": "artist-credits labels recordings"})
    out = _mb_summary(r)
    album_artist = out["artist"]
    discs = []
    for m in r.get("media", []):
        tracks = []
        for t in m.get("tracks", []):
            tracks.append({
                "number": t.get("position"),
                "position": t.get("number") or "",
                "title": t.get("title", ""),
                "artist": _mb_credit(t.get("artist-credit")) or album_artist,
                "length": round(t["length"] / 1000) if t.get("length") else None,
            })
        discs.append({"number": m.get("position", len(discs) + 1), "tracks": tracks})
    caa = r.get("cover-art-archive") or {}
    out.update({
        "album_artist": album_artist, "discs": discs,
        "cover_url": f"https://coverartarchive.org/release/{release_id}/front-1200" if caa.get("front") else "",
        "musicbrainz_albumid": release_id,
    })
    return out


# ---------------------------------------------------------------------- VGMdb
# VGMdb is the best database for Japanese anime, game and idol CDs. vgmdb.info
# is an unofficial JSON mirror of it; it is often offline (vgmdb.net itself sits
# behind a bot check), so this source is off by default and fails fast.

VGM = "https://vgmdb.info"
VGM_LANG_KEYS = {
    "ja": ["ja", "Japanese", "jp"],
    "romaji": ["ja-latn", "Romaji", "en", "English"],
    "en": ["en", "English", "ja-latn", "Romaji"],
}


def _pick(names, lang):
    if isinstance(names, str):
        return names
    names = names or {}
    for key in VGM_LANG_KEYS.get(lang, VGM_LANG_KEYS["ja"]) + list(names.keys()):
        if names.get(key):
            return names[key]
    return ""


def vgmdb_search(artist="", album="", catno="", barcode="", query="", lang="ja"):
    q = catno or barcode or " ".join(x for x in (album, artist) if x) or query
    data = _get(f"{VGM}/search/albums", {"q": q, "format": "json"}, timeout=8)
    albums = (data.get("results") or {}).get("albums") or []
    out = []
    for a in albums[:25]:
        aid = a.get("link", "").split("/")[-1]
        out.append({
            "source": "vgmdb", "id": aid, "url": f"https://vgmdb.net/album/{aid}",
            "title": _pick(a.get("titles"), lang), "artist": "",
            "date": a.get("release_date", ""), "country": "", "label": "",
            "catalog": a.get("catalog", ""), "barcode": "", "track_count": None, "format": "",
        })
    return out


def vgmdb_release(album_id, lang="ja"):
    a = _get(f"{VGM}/album/{album_id}", {"format": "json"}, timeout=8)
    people = a.get("performers") or a.get("composers") or []
    artist = ", ".join(_pick(p.get("names"), lang) for p in people[:4])
    label = a.get("label") or a.get("publisher") or {}
    if isinstance(label, list):
        label = label[0] if label else {}
    discs = []
    for i, d in enumerate(a.get("discs") or [], 1):
        tracks = [{
            "number": j, "position": "",
            "title": _pick(t.get("names"), lang),
            "artist": "",
            "length": _parse_length(t.get("track_length")),
        } for j, t in enumerate(d.get("tracks") or [], 1)]
        discs.append({"number": i, "tracks": tracks})
    return {
        "source": "vgmdb", "id": str(album_id), "url": f"https://vgmdb.net/album/{album_id}",
        "title": _pick(a.get("names"), lang), "artist": artist, "album_artist": artist,
        "date": (a.get("release_date") or "").replace(".", "-"), "country": "",
        "label": _pick(label.get("names"), lang) if isinstance(label, dict) else "",
        "catalog": a.get("catalog", ""), "barcode": a.get("barcode") or "",
        "track_count": sum(len(d["tracks"]) for d in discs), "format": a.get("media_format", ""),
        "cover_url": a.get("picture_full") or a.get("picture_small") or "",
        "discs": discs,
    }


# -------------------------------------------------------------------- Discogs

def _discogs_headers(token):
    return {"Authorization": f"Discogs token={token}"}


def discogs_search(token, artist="", album="", catno="", barcode="", query=""):
    params = {"type": "release", "per_page": 25}
    if catno:
        params["catno"] = catno
    elif barcode:
        params["barcode"] = barcode
    elif artist or album:
        params.update({"artist": artist, "release_title": album})
    else:
        params["q"] = query
    data = _get("https://api.discogs.com/database/search", params, _discogs_headers(token))
    out = []
    for r in data.get("results", []):
        title = r.get("title", "")
        artist_name, _, album_name = title.partition(" - ")
        out.append({
            "source": "discogs", "id": str(r["id"]),
            "url": f"https://www.discogs.com/release/{r['id']}",
            "title": album_name or title, "artist": artist_name if album_name else "",
            "date": str(r.get("year") or ""), "country": r.get("country", ""),
            "label": ", ".join((r.get("label") or [])[:2]), "catalog": r.get("catno", ""),
            "barcode": ", ".join((r.get("barcode") or [])[:1]), "track_count": None,
            "format": ", ".join(r.get("format") or []),
        })
    return out


def _discogs_artist(artists):
    s = "".join(re.sub(r" \(\d+\)$", "", a.get("anv") or a.get("name", "")) +
                (f" {a['join']} " if a.get("join") and a["join"] != "," else (", " if a.get("join") == "," else ""))
                for a in artists or [])
    return s.strip()


def discogs_release(token, release_id, lang=None):
    r = _get(f"https://api.discogs.com/releases/{release_id}", headers=_discogs_headers(token))
    album_artist = _discogs_artist(r.get("artists"))
    discs = {}
    for t in r.get("tracklist", []):
        if t.get("type_") not in (None, "track"):
            continue
        pos = t.get("position", "")
        m = re.match(r"(?:CD)?(\d+)[-.](\d+)$", pos)
        disc = int(m.group(1)) if m else 1
        tracks = discs.setdefault(disc, [])
        tracks.append({
            "number": len(tracks) + 1, "position": pos, "title": t.get("title", ""),
            "artist": _discogs_artist(t.get("artists")) or album_artist,
            "length": _parse_length(t.get("duration")),
        })
    labels = r.get("labels") or []
    barcodes = [i["value"] for i in r.get("identifiers", []) if i.get("type") == "Barcode"]
    images = r.get("images") or []
    primary = next((i for i in images if i.get("type") == "primary"), images[0] if images else {})
    return {
        "source": "discogs", "id": str(release_id), "url": r.get("uri", ""),
        "title": r.get("title", ""), "artist": album_artist, "album_artist": album_artist,
        "date": r.get("released") or str(r.get("year") or ""), "country": r.get("country", ""),
        "label": ", ".join(sorted({l.get("name", "") for l in labels})),
        "catalog": ", ".join(sorted({l.get("catno", "") for l in labels})),
        "barcode": re.sub(r"\D", "", barcodes[0]) if barcodes else "",
        "track_count": sum(len(v) for v in discs.values()),
        "format": ", ".join(f.get("name", "") for f in r.get("formats", [])),
        "cover_url": primary.get("uri", ""),
        "discs": [{"number": k, "tracks": v} for k, v in sorted(discs.items())],
    }


# --------------------------------------------------------------------- facade

def search_all(sources, config, lang="ja", **criteria):
    """Search several sources in parallel. Returns (results, errors)."""
    jobs = {}
    if "musicbrainz" in sources:
        jobs["musicbrainz"] = lambda: mb_search(**criteria)
    if "vgmdb" in sources:
        jobs["vgmdb"] = lambda: vgmdb_search(lang=lang, **criteria)
    if "discogs" in sources:
        token = config.get("discogs_token")
        if token:
            jobs["discogs"] = lambda: discogs_search(token, **criteria)
    results, errors = [], {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {name: pool.submit(fn) for name, fn in jobs.items()}
        for name, fut in futures.items():
            try:
                results.extend(fut.result())
            except Exception as e:  # one bad source shouldn't sink the others
                errors[name] = str(e)[:300]
    if "discogs" in sources and not config.get("discogs_token"):
        errors["discogs"] = "No Discogs token set in config.json (free at discogs.com/settings/developers)"
    want = norm_catno(criteria.get("catno"))
    if want:  # exact catalog-number matches first
        results.sort(key=lambda r: want not in norm_catno(r.get("catalog")))
    return results, errors


def get_release(source, release_id, config, lang="ja"):
    if source == "musicbrainz":
        return mb_release(release_id)
    if source == "vgmdb":
        return vgmdb_release(release_id, lang)
    if source == "discogs":
        return discogs_release(config.get("discogs_token"), release_id)
    raise ValueError(f"unknown source {source}")


def fetch_image(url):
    r = session.get(url, timeout=30)
    r.raise_for_status()
    mime = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    return r.content, mime
