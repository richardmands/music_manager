"""Identify a release from photos of its cover / back / obi using Claude.

Claude reads the photos (Japanese text included) and reports what it found
through a strict `submit_release` tool. With web search on, it also looks the
release up online to confirm and complete the track list.
"""
import base64
import io

import anthropic
from PIL import Image, ImageOps

MODEL = "claude-opus-5-5"
MAX_EDGE = 2000  # long edge in px; enough for small print on a back cover

_nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}

SUBMIT_TOOL = {
    "name": "submit_release",
    "description": "Submit the identified release metadata. Call this exactly once, when done.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["album_title", "album_title_romanized", "artist", "artist_romanized",
                     "release_date", "label", "catalog_number", "barcode", "media",
                     "discs", "cover_image_url", "confidence", "notes", "sources"],
        "properties": {
            "album_title": {"type": "string", "description": "Exactly as printed, original script"},
            "album_title_romanized": _nullable_str,
            "artist": {"type": "string", "description": "Album artist, original script"},
            "artist_romanized": _nullable_str,
            "release_date": {**_nullable_str, "description": "YYYY, YYYY-MM or YYYY-MM-DD"},
            "label": _nullable_str,
            "catalog_number": {**_nullable_str, "description": "e.g. VICL-60001, SRCL-1234"},
            "barcode": {**_nullable_str, "description": "Digits only (JAN/EAN/UPC)"},
            "media": {"type": "string", "enum": ["CD", "Vinyl", "Cassette", "Other"]},
            "discs": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["number", "tracks"],
                    "properties": {
                        "number": {"type": "integer"},
                        "tracks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["number", "position", "title", "title_romanized",
                                             "artist", "length"],
                                "properties": {
                                    "number": {"type": "integer"},
                                    "position": {**_nullable_str,
                                                 "description": "Printed position, e.g. 'A1' on vinyl"},
                                    "title": {"type": "string"},
                                    "title_romanized": _nullable_str,
                                    "artist": {**_nullable_str,
                                               "description": "Only if different from album artist"},
                                    "length": {**_nullable_str, "description": "m:ss if printed/known"},
                                },
                            },
                        },
                    },
                },
            },
            "cover_image_url": {**_nullable_str,
                                "description": "Direct link to a front cover image file found online"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": "string", "description": "Anything uncertain or unreadable"},
            "sources": {"type": "array", "items": {"type": "string"},
                        "description": "URLs used to confirm, if any"},
        },
    },
}

SYSTEM = """You identify music releases (mostly Japanese CDs, also vinyl records) from photos \
of their packaging: front cover, back cover, obi strip, spine, booklet, disc label or record label.

Rules:
- Transcribe titles and names exactly as printed, in the original script (kanji/kana/hangul/latin). \
Put a Hepburn romanization in the *_romanized fields when the original is not Latin script; \
use null when it already is.
- Japanese catalog numbers look like ABCD-12345 and are usually on the spine or obi. \
Barcodes on Japanese releases are 13-digit JANs starting 45 or 49; give digits only.
- Only list tracks you can actually read or have confirmed from a reliable source. Never invent \
tracks to match a count. If the track list is not visible and you could not confirm it, \
return an empty discs array and say so in notes.
- For vinyl, keep the printed side positions (A1, A2, B1...) in `position`, and put every record \
in its own disc entry.
- When you are finished, call submit_release exactly once."""

WEB_ADDENDUM = """
You also have web search. Use it to confirm the release and complete or correct the track list \
(VGMdb, MusicBrainz, Discogs, the label's or artist's official site, Amazon.co.jp, CDJournal, \
Tower Records Japan and HMV Japan are good sources). Searching the catalog number on its own \
is usually the fastest way in. List the URLs you relied on in `sources`.
Also find this exact edition's front cover online and put a direct link to the image file
(not a web page) in `cover_image_url`, e.g. the Amazon.co.jp product image, Cover Art Archive,
Discogs, Tower Records or HMV Japan. Use null if you can't find one."""


def prepare_image(data):
    """Fix phone rotation, shrink and re-encode as JPEG. Returns base64."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    img = img.convert("RGB")
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return base64.standard_b64encode(buf.getvalue()).decode()


def identify(images, hints="", rip_summary="", use_web=False, api_key=None):
    """images: list of (label, bytes). Returns the submit_release dict."""
    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    content = []
    for label, data in images:
        content.append({"type": "text", "text": f"Photo: {label}"})
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": "image/jpeg",
                                   "data": prepare_image(data)}})
    prompt = "Identify this release and report its metadata."
    if rip_summary:
        prompt += f"\n\nThe user's ripped files, for matching the track list:\n{rip_summary}"
    if hints:
        prompt += f"\n\nNotes from the user: {hints}"
    content.append({"type": "text", "text": prompt})
    messages = [{"role": "user", "content": content}]

    tools = [SUBMIT_TOOL]
    if use_web:
        tools.append({"type": "web_search_20260209", "name": "web_search", "max_uses": 8})

    for _ in range(8):
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            system=SYSTEM + (WEB_ADDENDUM if use_web else ""),
            tools=tools,
            output_config={"effort": "high" if use_web else "medium"},
            # If a safety classifier declines, retry on a fallback model automatically.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude declined to process these images.")
        for block in resp.content:
            if block.type == "tool_use" and block.name == "submit_release":
                return dict(block.input)
        messages.append({"role": "assistant", "content": resp.content})
        if resp.stop_reason == "pause_turn":
            continue  # long-running web search; let it resume
        if resp.stop_reason == "max_tokens":
            raise RuntimeError("Claude's response was cut off (max_tokens).")
        messages.append({"role": "user", "content": "Please call submit_release now with what you have."})
    raise RuntimeError("Claude did not return a result.")


def to_release(r):
    """Convert submit_release output to the shared release shape (see sources.py)."""
    discs = [{
        "number": d["number"],
        "tracks": [{
            "number": t["number"], "position": t.get("position") or "",
            "title": t["title"], "title_romanized": t.get("title_romanized") or "",
            "artist": t.get("artist") or r["artist"],
            "length": _len(t.get("length")),
        } for t in d["tracks"]],
    } for d in r.get("discs", [])]
    return {
        "source": "claude", "id": "", "url": (r.get("sources") or [""])[0],
        "title": r["album_title"], "title_romanized": r.get("album_title_romanized") or "",
        "artist": r["artist"], "artist_romanized": r.get("artist_romanized") or "",
        "album_artist": r["artist"], "date": r.get("release_date") or "", "country": "",
        "label": r.get("label") or "", "catalog": r.get("catalog_number") or "",
        "barcode": r.get("barcode") or "", "format": r.get("media", ""),
        "track_count": sum(len(d["tracks"]) for d in discs),
        "cover_url": r.get("cover_image_url") or "",
        "discs": discs, "confidence": r.get("confidence"), "notes": r.get("notes", ""),
        "sources": r.get("sources", []),
    }


def _len(s):
    if not s:
        return None
    try:
        m, _, sec = s.partition(":")
        return int(m) * 60 + int(sec) if sec else None
    except ValueError:
        return None
