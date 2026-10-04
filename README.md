# CD Tagger

A small local web app for finding metadata for ripped CDs and vinyl, especially
Japanese releases, and writing it straight into FLAC and MP3 files. Mp3tag and
MediaMonkey pick up the result.

## What it does

**Library check** runs when the app opens. It reads (never writes) every album
under your `music_root` and lists the ones that need attention: missing or
generic song titles like “Track01” first, then missing artists, album names,
track numbers or cover art. Smaller gaps (date, album artist, genre, catalog
number) can be shown with the filter. **Open** loads that album below.

1. **Load an album folder.** Shows its FLAC/MP3 files, current tags, and
   whether they look like vinyl sides (`Side A.flac`, `Side B.flac`, …).
   `CD1`/`CD2` subfolders are treated as discs.
2. **Find metadata**, either way:
   - **Search databases.** Search MusicBrainz and Discogs by artist and album,
     **catalog number** (e.g. `VICL-60001`, the most reliable for Japanese CDs)
     or barcode. **Look up by track lengths** matches a CD rip's track layout
     against MusicBrainz disc IDs.
   - **From photos.** Drop photos of the front, back, obi or spine. Claude reads
     them, including Japanese text, catalog number and barcode, and can also
     search the web to confirm and complete the track list. What it reads is
     then used to search the databases too, and you can use either result.
   - **Ask an AI chat.** No API key needed. *Create instructions* writes a
     prompt describing the album (track count, exact track lengths, current
     tags) and the exact JSON format to reply with. Copy it into a new
     claude.ai chat with web search on (attach cover photos if you like),
     paste the reply back into the app, and press *Use this reply*.
3. **Review and SAVE.** Edit any field and choose cover art (from the
   database or one of your photos). Then see below.

### Nothing is ever saved automatically

Your files are only changed when you press **SAVE**:

1. **Review changes…** shows, for every file, each field's old and new value,
   whether the cover art changes, and any `cover.jpg` / `.cue` files that would
   be created or replaced. At this point nothing has been written.
2. **SAVE** in that dialog writes exactly what you reviewed. If any file was
   changed by another program after you reviewed it, nothing is saved.

When you press SAVE:

- The current tags *and embedded cover art* of every affected file are backed
  up to `backups/` first.
- Each file is changed as a temporary copy. The copy's audio data is checked
  byte-for-byte against the original, and only then does it replace the
  original. If the check fails, the original is left untouched.
- An existing `cover.jpg` or `.cue` that gets replaced is kept as `.bak`.

**Undo / backups** (next to the folder's file list) lists every save made to
that folder. You can review and SAVE an undo the same way, and undo puts the
tags and art back exactly as they were.

### Choosing a folder

**Browse…** opens the normal Windows folder picker. It can sometimes appear
behind the browser window. You can also paste a folder path and press Load.

### Vinyl (one file per side)

Switch **Mode** to *Vinyl* (it is chosen automatically for side-named files).
Tracks are grouped into sides using their printed positions (A1, A2, B1…).
If a source has no positions, tracks are split so each side's total length
matches its file. Each side file gets:

- `TITLE`: `Side A: Song 1 / Song 2 / …` (or just the songs, or just `Side A`)
- `TRACKNUMBER`/`TRACKTOTAL` = side number/total sides, `DISCNUMBER` = record number
- `COMMENT`: the side's full track list with times
- `MEDIA` = Vinyl, `VINYL_SIDE` = A/B/…
- optionally a `.cue` sheet next to the file, with track starts estimated from
  the listed lengths, so players such as foobar2000 can skip between songs

### Tags written

| Field | FLAC (Vorbis comment) | MP3 (ID3v2.3, UTF-16) |
|---|---|---|
| Title / Artist / Album / Album artist | TITLE, ARTIST, ALBUM, ALBUMARTIST | TIT2, TPE1, TALB, TPE2 |
| Date / Genre / Label | DATE, GENRE, LABEL | TDRC, TCON, TPUB |
| Track / Disc | TRACKNUMBER, TRACKTOTAL, DISCNUMBER, DISCTOTAL | TRCK `n/total`, TPOS `n/total` |
| Catalog no. / Barcode | CATALOGNUMBER, BARCODE | TXXX:CATALOGNUMBER, TXXX:BARCODE |
| MusicBrainz release | MUSICBRAINZ_ALBUMID | TXXX:MusicBrainz Album Id |
| Vinyl | MEDIA, VINYL_SIDE, COMMENT | TMED, TXXX:VINYL_SIDE, COMM |
| Cover | embedded front cover | APIC front cover |

These match Mp3tag's and MediaMonkey's default field mappings. After writing,
rescan the folder in MediaMonkey (or press F5 in Mp3tag).

## Setup

Needs Python 3.10+.

```bash
pip install -r requirements.txt
```

Copy `config.example.json` to `config.json` and edit it:

- `music_root`: where the folder browser starts
- `discogs_token`: optional, free from https://www.discogs.com/settings/developers
- `anthropic_api_key`: for photo lookup (or set the `ANTHROPIC_API_KEY`
  environment variable instead). Get one at https://console.anthropic.com.
- `language`: `ja`, `romaji` or `en`. Which name to prefer when a source has
  several (also switchable in the app).

Then double-click `run.bat`, or:

```bash
python app.py
```

It opens http://127.0.0.1:5173 and only listens on your own machine.

## Costs and limits

- MusicBrainz and Discogs are free. MusicBrainz allows about one request per
  second, which the app respects.
- Photo lookup uses Claude Opus 5.5 and costs a few cents per album, more with
  web search on (it may run up to 8 searches).
- VGMdb is the best database for anime, game and idol CDs. It has no official
  API, and the unofficial vgmdb.info mirror the app can use is often offline,
  so that source is off by default. With web search on, photo lookup can still
  find VGMdb pages.
- Track-length lookup only works for CD rips. It's exact with FLAC, approximate
  with MP3.
- The `.cue` start times for vinyl are estimates based on the listed track
  lengths, not detected from the audio.
