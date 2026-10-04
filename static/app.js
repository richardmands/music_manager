// CD Tagger front end. Plain JS, no build step.
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtLen = s => s == null ? "" : `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const parseLen = s => { const m = /^(\d+):(\d\d)$/.exec((s || "").trim()); return m ? +m[1] * 60 + +m[2] : null; };

const state = {
  config: {},
  scan: null,        // {path, files, tocs, guess, looks_like_vinyl}
  release: null,     // full release (see sources.py)
  photos: [],        // {id, name, label, url}
  cover: null,       // {url} | {upload_id} | null (keep existing)
  pairs: [],         // CD mode: [{file, track, disc, number, total}] as rendered
  discTotal: 1,
  sides: [],         // vinyl mode: [{letter, tracks:[{position,title,artist,length}]}]
};

async function api(path, opts = {}) {
  const res = await fetch(path, opts.body && !(opts.body instanceof FormData)
    ? { method: "POST", headers: { "Content-Type": "application/json" }, ...opts, body: JSON.stringify(opts.body) }
    : opts);
  const data = await res.json().catch(() => ({ error: `HTTP ${res.status}` }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function busy(btn, fn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = "Working…";
  try { return await fn(); }
  catch (e) { showErrors({ error: e.message }); }
  finally { btn.disabled = false; btn.textContent = label; }
}

function showErrors(errors) {
  $("#errors").innerHTML = Object.entries(errors || {}).map(([k, v]) => `<div><b>${esc(k)}:</b> ${esc(v)}</div>`).join("");
}

const lang = () => $("#lang").value;
const pickName = (orig, rom) => (lang() !== "ja" && rom) ? rom : orig;

// ------------------------------------------------------------------ setup

async function init() {
  state.config = await api("/api/config");
  $("#lang").value = state.config.language || "ja";
  $("#coverFilename").textContent = state.config.cover_filename;
  const bits = [];
  bits.push(state.config.has_claude ? "Claude: ready" : "Claude: no API key (photo lookup disabled)");
  bits.push(state.config.has_discogs ? "Discogs: ready" : "Discogs: no token");
  $("#status").textContent = bits.join(" · ");
  if (!state.config.has_discogs) $('.src[value="discogs"]').checked = false;
  if (!state.config.has_claude) {
    $("#identifyBtn").disabled = true;
    $("#claudeHint").textContent = "Set ANTHROPIC_API_KEY or add anthropic_api_key to config.json, then restart.";
  }
  const last = localStorageGet("lastFolder");
  $("#folderPath").value = last || "";
}

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { } }

// ------------------------------------------------------------- 1. folder

$("#browseBtn").onclick = () => busy($("#browseBtn"), async () => {
  // Opens the standard Windows folder picker (it may appear behind the browser).
  const d = await api("/api/pick-folder", { body: { start: $("#folderPath").value.trim() } });
  if (d.path) { $("#folderPath").value = d.path; await scan(); }
});

$("#scanBtn").onclick = () => scan();
$("#folderPath").onkeydown = e => { if (e.key === "Enter") scan(); };

async function scan() {
  const path = $("#folderPath").value.trim().replace(/^"|"$/g, "");
  if (!path) return;
  await busy($("#scanBtn"), async () => {
    const d = await api("/api/scan", { body: { path } });
    state.scan = d;
    localStorageSet("lastFolder", path);
    const discs = new Set(d.files.map(f => f.disc)).size;
    $("#fileInfo").textContent = `${d.files.length} files${discs > 1 ? ` on ${discs} discs` : ""}` +
      (d.looks_like_vinyl ? " · looks like vinyl sides" : "");
    renderFileTable(d.files);
    $("#fileTable").classList.remove("hidden");
    $("#qArtist").value = d.guess.artist || "";
    $("#qAlbum").value = d.guess.album || "";
    $("#qCatno").value = d.guess.catno || "";
    $("#qBarcode").value = d.guess.barcode || "";
    $("#tocBtn").disabled = d.looks_like_vinyl;
    $("#results").innerHTML = ""; showErrors({});
    $("#reviewPanel").classList.add("hidden");
    $("#backupsBtn").classList.remove("hidden");
    $("#backupList").classList.add("hidden");
  });
}

function renderFileTable(files) {
  $("#fileTable tbody").innerHTML = files.map(f => `<tr>
        <td class="file" title="${esc(f.name)}">${esc(f.name)}</td><td class="num">${f.disc}</td>
        <td class="num">${f.track ?? ""}</td><td class="num">${esc(f.side)}</td>
        <td class="num">${fmtLen(f.length)}</td><td>${esc(f.title)}</td><td class="num">${f.has_cover ? "✓" : ""}</td></tr>`).join("");
}

// --------------------------------------------------------------- 2. find

$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.toggle("active", x === t));
  $$(".tabpane").forEach(p => p.classList.toggle("hidden", p.id !== t.dataset.tab));
});

$("#searchBtn").onclick = () => busy($("#searchBtn"), async () => {
  const sources = $$(".src:checked").map(c => c.value);
  const d = await api("/api/search", { body: {
    artist: $("#qArtist").value, album: $("#qAlbum").value,
    catno: $("#qCatno").value, barcode: $("#qBarcode").value, sources, lang: lang(),
  } });
  showErrors(d.errors); renderResults(d.results);
});
$$("#searchTab input").forEach(i => i.onkeydown = e => { if (e.key === "Enter") $("#searchBtn").click(); });

$("#tocBtn").onclick = () => {
  if (!state.scan) return showErrors({ error: "Load an album folder first." });
  busy($("#tocBtn"), async () => {
    const d = await api("/api/toc", { body: { path: state.scan.path } });
    showErrors(d.errors);
    renderResults(d.results, "No disc ID match on MusicBrainz. Try a text or catalog number search, or photos.");
    if (d.results.length) $("#results").insertAdjacentHTML("afterbegin",
      `<div class="muted">Matches by track lengths are approximate; check the track count and lengths before using one.</div>`);
  });
};

function expectedCount() {
  if (!state.scan) return null;
  return state.scan.files.length;
}

function renderResults(results, emptyMsg = "No results.") {
  const box = $("#results");
  if (!results.length) { box.innerHTML = `<div class="muted">${esc(emptyMsg)}</div>`; return; }
  const n = expectedCount();
  box.innerHTML = results.map((r, i) => {
    const meta = [r.date, r.country, r.label, r.catalog, r.format, r.track_count ? `${r.track_count} tracks` : ""]
      .filter(Boolean).map(esc).join(" · ");
    const match = n && r.track_count === n;
    return `<div class="result" data-i="${i}">
      <span class="badge">${esc(r.source)}</span>
      <div><div class="t">${esc(r.title)}</div><div>${esc(r.artist)}</div><div class="meta">${meta}</div></div>
      <div style="text-align:right">${match ? `<div class="badge match">${n} tracks ✓</div>` : ""}
        <a href="${esc(r.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">open ↗</a></div>
    </div>`;
  }).join("");
  box.onclick = async e => {
    const el = e.target.closest(".result");
    if (!el) return;
    $$(".result", box).forEach(x => x.classList.toggle("sel", x === el));
    const r = results[+el.dataset.i];
    try {
      el.style.opacity = .6;
      const full = await api(`/api/release?source=${r.source}&id=${encodeURIComponent(r.id)}&lang=${lang()}`);
      loadRelease(full);
    } catch (err) { showErrors({ [r.source]: err.message }); }
    finally { el.style.opacity = ""; }
  };
}

// photos
const drop = $("#drop");
drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); addPhotos(e.dataTransfer.files); };
$("#photoInput").onchange = e => { addPhotos(e.target.files); e.target.value = ""; };

const PHOTO_LABELS = ["Front cover", "Back cover", "Obi strip", "Spine", "Disc / record label", "Booklet page", "Other"];

async function addPhotos(fileList) {
  const files = [...fileList].filter(f => f.type.startsWith("image/"));
  if (!files.length) return;
  const fd = new FormData();
  files.forEach(f => fd.append("images", f));
  try {
    const d = await api("/api/upload", { method: "POST", body: fd });
    d.uploads.forEach((u, i) => {
      const guess = /back|ura|裏/i.test(u.name) ? "Back cover" : /obi|帯/i.test(u.name) ? "Obi strip"
        : state.photos.length === 0 ? "Front cover" : "Back cover";
      state.photos.push({ ...u, label: guess, url: `/api/upload/${u.id}` });
    });
    renderPhotos();
  } catch (e) { showErrors({ upload: e.message }); }
}

function renderPhotos() {
  $("#photos").innerHTML = state.photos.map((p, i) => `<div class="photo">
      <img src="${p.url}" alt="">
      <button class="x secondary" data-del="${i}" title="Remove">✕</button>
      <select data-i="${i}">${PHOTO_LABELS.map(l => `<option ${l === p.label ? "selected" : ""}>${l}</option>`).join("")}</select>
    </div>`).join("");
  $$("#photos select").forEach(s => s.onchange = () => state.photos[+s.dataset.i].label = s.value);
  $$("#photos [data-del]").forEach(b => b.onclick = () => { state.photos.splice(+b.dataset.del, 1); renderPhotos(); });
}

function ripSummary() {
  if (!state.scan) return "";
  const f = state.scan.files;
  if (state.scan.looks_like_vinyl)
    return `Vinyl rip, one file per side: ` + f.map(x => `side ${x.side || "?"} ${fmtLen(x.length)}`).join(", ");
  const byDisc = {};
  f.forEach(x => (byDisc[x.disc] ||= []).push(fmtLen(x.length)));
  return Object.entries(byDisc).map(([d, l]) => `Disc ${d}: ${l.length} tracks, lengths ${l.join(", ")}`).join("\n");
}

$("#identifyBtn").onclick = () => {
  if (!state.photos.length) return showErrors({ photos: "Add at least one photo." });
  const web = $("#useWeb").checked;
  $("#claudeResult").innerHTML = `<div class="muted">Claude is reading the photos${web ? " and searching the web" : ""}… this can take a minute.</div>`;
  busy($("#identifyBtn"), async () => {
    try {
      const d = await api("/api/identify", { body: {
        images: state.photos.map(p => ({ id: p.id, label: p.label })),
        hints: $("#hints").value, rip_summary: ripSummary(), web,
        sources: $$(".src:checked").map(c => c.value),
      } });
      const r = d.release;
      $("#claudeResult").innerHTML = `<div class="claude-card">
        <div><b>${esc(pickName(r.title, r.title_romanized))}</b> — ${esc(pickName(r.artist, r.artist_romanized))}</div>
        <div class="meta muted">${[r.date, r.label, r.catalog, r.barcode, r.format, `${r.track_count} tracks`, `confidence: ${r.confidence}`].filter(Boolean).map(esc).join(" · ")}</div>
        ${r.notes ? `<div class="notes">${esc(r.notes)}</div>` : ""}
        ${r.sources.length ? `<div class="notes">Sources: ${r.sources.map(u => `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(new URL(u).hostname)}</a>`).join(", ")}</div>` : ""}
        <div class="row"><button id="useClaude">Use this</button>
          ${d.results.length ? `<span class="muted">or pick a database match below (they usually have better data)</span>` : ""}</div>
      </div>`;
      $("#useClaude").onclick = () => loadRelease(r);
      if (r.catalog) $("#qCatno").value = r.catalog;
      if (r.barcode) $("#qBarcode").value = r.barcode;
      showErrors(d.errors);
      renderResults(d.results, "No database matches for that catalog number or barcode.");
    } catch (e) {
      $("#claudeResult").innerHTML = `<div class="err-box">${esc(e.message)}</div>`;
    }
  });
};

$("#lang").onchange = async () => {
  if (state.release?.source === "vgmdb") {
    try { loadRelease(await api(`/api/release?source=vgmdb&id=${state.release.id}&lang=${lang()}`)); }
    catch (e) { showErrors({ vgmdb: e.message }); }
  } else if (state.release) loadRelease(state.release);
};

// ------------------------------------------------------------- 3. review

function flatTracks(rel) {
  return rel.discs.flatMap(d => d.tracks.map(t => ({ ...t, disc: d.number,
    title: pickName(t.title, t.title_romanized) })));
}

function loadRelease(rel) {
  state.release = rel;
  const panel = $("#reviewPanel");
  panel.classList.remove("hidden");
  $("#sourceLine").innerHTML = `From <b>${esc(rel.source)}</b>` +
    (rel.url ? ` · <a href="${esc(rel.url)}" target="_blank" rel="noopener">${esc(rel.url)}</a>` : "");

  const fields = {
    album: pickName(rel.title, rel.title_romanized),
    albumartist: pickName(rel.album_artist || rel.artist, rel.artist_romanized),
    date: rel.date, genre: "", label: rel.label, catalognumber: rel.catalog, barcode: rel.barcode,
  };
  const keepGenre = $('[data-f="genre"]').value;
  for (const [k, v] of Object.entries(fields)) $(`[data-f="${k}"]`).value = v || "";
  $('[data-f="genre"]').value = keepGenre;

  const isVinyl = state.scan?.looks_like_vinyl || (/vinyl|LP|12"|7"/i.test(rel.format || "") &&
    state.scan && state.scan.files.length < rel.track_count);
  $("#mode").value = isVinyl ? "vinyl" : "cd";

  renderCovers();
  renderMap();
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderCovers() {
  const rel = state.release;
  const choices = [{ key: "keep", label: "Keep existing", cover: null }];
  if (rel.cover_url) choices.push({ key: "rel", label: rel.source, cover: { url: rel.cover_url },
    img: `/api/cover-proxy?url=${encodeURIComponent(rel.cover_url)}` });
  state.photos.forEach(p => choices.push({ key: p.id, label: p.label, cover: { upload_id: p.id }, img: p.url }));
  const def = choices.find(c => c.key === "rel") || choices.find(c => c.label === "Front cover") || choices[0];
  state.cover = def.cover;
  $("#coverChoices").innerHTML = choices.map((c, i) => `<div class="cover-choice ${c === def ? "sel" : ""}" data-i="${i}">
      ${c.img ? `<img src="${c.img}" alt="">` : `<div class="none">—</div>`}${esc(c.label)}</div>`).join("");
  $$(".cover-choice").forEach(el => el.onclick = () => {
    $$(".cover-choice").forEach(x => x.classList.toggle("sel", x === el));
    state.cover = choices[+el.dataset.i].cover;
  });
}

$("#mode").onchange = () => renderMap();
$("#sideTitle").onchange = () => renderMap();

function renderMap() {
  const vinyl = $("#mode").value === "vinyl";
  $("#vinylOptions").classList.toggle("hidden", !vinyl);
  vinyl ? renderVinylMap() : renderCdMap();
}

// CD: pair the k-th file disc with the k-th release disc, then track by track.
function cdPairs() {
  const files = state.scan.files;
  const fileDiscs = [...new Set(files.map(f => f.disc))].sort((a, b) => a - b);
  const relDiscs = state.release.discs;
  const pairs = [];
  fileDiscs.forEach((fd, k) => {
    const fs = files.filter(f => f.disc === fd);
    const rd = relDiscs[k];
    const ts = rd ? rd.tracks : [];
    fs.forEach((f, i) => pairs.push({ file: f, track: ts[i] ? { ...ts[i], title: pickName(ts[i].title, ts[i].title_romanized) } : null,
      disc: k + 1, number: i + 1, total: fs.length }));
  });
  return { pairs, discTotal: fileDiscs.length };
}

function renderCdMap() {
  if (!state.scan) return mapWarn("Load an album folder first.");
  const { pairs, discTotal } = cdPairs();
  state.pairs = pairs; state.discTotal = discTotal;  // rows below are rendered in this order
  const relCount = state.release.track_count || flatTracks(state.release).length;
  mapWarn(relCount !== state.scan.files.length
    ? `The release lists ${relCount} tracks but the folder has ${state.scan.files.length} files. Check the matching below.` : "");
  const albumArtist = $('[data-f="albumartist"]').value;
  $("#mapTable thead").innerHTML = `<tr><th>File</th><th>Disc</th><th>#</th><th>Title</th><th>Artist</th><th>Length (file / release)</th></tr>`;
  $("#mapTable tbody").innerHTML = pairs.map((p, i) => {
    const rl = p.track?.length;
    const diff = rl != null ? Math.abs(rl - p.file.length) : null;
    const lenCls = diff == null ? "" : diff > 3 ? "len-bad" : "len-ok";
    return `<tr data-i="${i}">
      <td class="file" title="${esc(p.file.name)}">${esc(p.file.name)}</td>
      <td class="num">${p.disc}</td><td class="num">${p.number}</td>
      <td><input data-k="title" value="${esc(p.track?.title || p.file.title)}"></td>
      <td><input data-k="artist" value="${esc(pickName(p.track?.artist, null) || albumArtist)}"></td>
      <td class="num ${lenCls}">${fmtLen(p.file.length)} / ${fmtLen(rl) || "?"}</td></tr>`;
  }).join("");
}

// Vinyl: group release tracks into sides, then match each side to a file.
function buildSides() {
  const tracks = flatTracks(state.release);
  const files = state.scan.files;
  const withSide = tracks.filter(t => /^[A-Z]/i.test(t.position || ""));
  let groups = [];
  if (withSide.length >= tracks.length / 2) {
    const map = new Map();
    tracks.forEach(t => {
      const letter = ((t.position || "").match(/^([A-Z])/i)?.[1] || "?").toUpperCase();
      if (!map.has(letter)) map.set(letter, []);
      map.get(letter).push(t);
    });
    groups = [...map.entries()].sort().map(([letter, ts]) => ({ letter, tracks: ts }));
  } else {
    // No printed positions: split the running order so each side's total
    // length best matches the length of its file.
    let i = 0;
    files.forEach((f, k) => {
      const ts = [];
      let sum = 0;
      const remainingFiles = files.length - k - 1;
      while (i < tracks.length - remainingFiles) {
        const t = tracks[i];
        const len = t.length ?? (f.length / Math.max(1, Math.round(tracks.length / files.length)));
        if (ts.length && remainingFiles > 0 && Math.abs(sum + len - f.length) > Math.abs(sum - f.length)) break;
        ts.push(t); sum += len; i++;
      }
      groups.push({ letter: String.fromCharCode(65 + k), tracks: ts });
    });
  }
  // Pair files with sides: by the side letter in the filename, else in order.
  const used = new Set();
  const out = files.map(f => {
    const g = groups.find(g => g.letter === f.side && !used.has(g));
    if (g) used.add(g);
    return { file: f, group: g || null };
  });
  out.forEach(o => {
    if (!o.group) { o.group = groups.find(g => !used.has(g)) || { letter: "?", tracks: [] }; used.add(o.group); }
  });
  return out;
}

function trackLine(t) {
  return [t.position, t.title, t.length != null ? fmtLen(t.length) : ""].filter(x => x !== "" && x != null).join(" · ");
}

function parseTrackLine(line, albumArtist) {
  const parts = line.split(" · ").map(s => s.trim());
  let position = "", length = null;
  if (parts.length > 1 && parseLen(parts[parts.length - 1]) != null) length = parseLen(parts.pop());
  if (parts.length > 1 && /^[A-Z]?\d*$/i.test(parts[0]) && parts[0].length <= 4) position = parts.shift();
  return { position, title: parts.join(" · "), length, artist: albumArtist };
}

function renderVinylMap() {
  if (!state.scan) return mapWarn("Load an album folder first.");
  const sides = buildSides();
  state.sides = sides;
  const noLens = sides.some(s => s.group.tracks.some(t => t.length == null));
  mapWarn(noLens ? "Some track lengths are unknown, so the .cue start times for those sides can't be calculated." : "");
  $("#mapTable thead").innerHTML = `<tr><th>File</th><th>Side</th><th>Tracks on this side (one per line: position · title · m:ss)</th><th>Length (file / tracks)</th></tr>`;
  $("#mapTable tbody").innerHTML = sides.map((s, i) => {
    const sum = s.group.tracks.reduce((a, t) => a + (t.length || 0), 0);
    const diff = Math.abs(sum - s.file.length);
    const cls = noLens ? "" : diff > 10 ? "len-bad" : "len-ok";
    return `<tr data-i="${i}">
      <td class="file" title="${esc(s.file.name)}">${esc(s.file.name)}</td>
      <td class="num"><input data-k="side" value="${esc(s.group.letter)}" size="2" style="width:3em"></td>
      <td><textarea class="side-tracks" data-k="tracks" rows="${Math.max(3, s.group.tracks.length)}">${esc(s.group.tracks.map(trackLine).join("\n"))}</textarea></td>
      <td class="num ${cls}">${fmtLen(s.file.length)} / ${sum ? fmtLen(sum) : "?"}</td></tr>`;
  }).join("");
}

function mapWarn(msg) {
  $("#mapWarn").textContent = msg;
  $("#mapWarn").classList.toggle("hidden", !msg);
}

// --------------------------------------------------------------- writing

function albumTags() {
  const t = {};
  $$("[data-f]").forEach(i => t[i.dataset.f] = i.value.trim());
  if (state.release?.musicbrainz_albumid) t.musicbrainz_albumid = state.release.musicbrainz_albumid;
  return t;
}

function buildWritePlan() {
  const album = albumTags();
  if ($("#mode").value === "cd") {
    const { pairs, discTotal } = state;
    return $$("#mapTable tbody tr").map((tr, i) => {
      const p = pairs[i];
      return { path: p.file.path, tags: { ...album,
        title: $('[data-k="title"]', tr).value.trim(),
        artist: $('[data-k="artist"]', tr).value.trim() || album.albumartist,
        tracknumber: String(p.number), tracktotal: String(p.total),
        discnumber: discTotal > 1 ? String(p.disc) : "", disctotal: discTotal > 1 ? String(discTotal) : "",
      } };
    });
  }
  // Vinyl: one file per side.
  const rows = $$("#mapTable tbody tr");
  const sideCount = rows.length;
  const records = Math.max(1, Math.ceil(sideCount / 2));
  const titleMode = $("#sideTitle").value;
  return rows.map((tr, i) => {
    const s = state.sides[i];
    const letter = $('[data-k="side"]', tr).value.trim().toUpperCase() || String.fromCharCode(65 + i);
    const tracks = $('[data-k="tracks"]', tr).value.split("\n").map(l => l.trim()).filter(Boolean)
      .map(l => parseTrackLine(l, album.albumartist));
    const titles = tracks.map(t => t.title).join(" / ");
    const title = titleMode === "side" ? `Side ${letter}` : titleMode === "list" ? titles : `Side ${letter}: ${titles}`;
    const comment = tracks.map(t => `${t.position ? t.position + ". " : ""}${t.title}${t.length != null ? ` (${fmtLen(t.length)})` : ""}`).join("\n");
    const recordNo = /^[A-Z]$/.test(letter) ? Math.floor((letter.charCodeAt(0) - 65) / 2) + 1 : 1;
    let cue = null;
    if ($("#writeCue").checked && tracks.length && tracks.every(t => t.length != null)) {
      let start = 0;
      cue = tracks.map(t => { const c = { title: t.title, artist: t.artist, start }; start += t.length; return c; });
    }
    return { path: s.file.path, cue, tags: { ...album,
      title, artist: album.albumartist, tracknumber: String(i + 1), tracktotal: String(sideCount),
      discnumber: records > 1 ? String(recordNo) : "", disctotal: records > 1 ? String(records) : "",
      media: "Vinyl", vinyl_side: letter, comment,
    } };
  });
}

// Saving is always two steps: "Review changes" asks the server what would
// change (it writes nothing), then only the SAVE button in the review dialog
// writes, using exactly the reviewed change set.

const FIELD_LABELS = {
  title: "Title", artist: "Artist", album: "Album", albumartist: "Album artist", date: "Date",
  genre: "Genre", label: "Label", catalognumber: "Catalog no.", barcode: "Barcode",
  tracknumber: "Track", tracktotal: "Total tracks", discnumber: "Disc", disctotal: "Total discs",
  media: "Media", vinyl_side: "Vinyl side", comment: "Comment", musicbrainz_albumid: "MusicBrainz ID",
};

$("#writeBtn").onclick = () => {
  const files = buildWritePlan();
  if (!files.length) return;
  busy($("#writeBtn"), async () => {
    const d = await api("/api/preview", { body: {
      path: state.scan.path, files, cover: state.cover, save_cover_file: $("#saveCoverFile").checked,
    } });
    openReview(d, "Review changes");
  });
};

$("#backupsBtn").onclick = () => busy($("#backupsBtn"), async () => {
  if (!state.scan) return;
  const d = await api(`/api/backups?path=${encodeURIComponent(state.scan.path)}`);
  const box = $("#backupList");
  box.classList.remove("hidden");
  if (!d.backups.length) { box.innerHTML = `<div class="muted">No saves have been made to this folder yet.</div>`; return; }
  box.innerHTML = `<div class="muted">Each save backs up the tags it replaced. Pick one to see what undoing it would change:</div>` +
    d.backups.map(b => `<div class="backup-row">
      <span>${esc(new Date(b.created * 1000).toLocaleString())} · ${esc(b.reason)} · ${b.count} file(s)</span>
      <button class="secondary" data-b="${esc(b.file)}">Review undo…</button></div>`).join("");
  $$("[data-b]", box).forEach(btn => btn.onclick = () => busy(btn, async () => {
    const r = await api("/api/restore-preview", { body: { path: state.scan.path, backup: btn.dataset.b } });
    openReview(r, `Undo to ${new Date(r.created * 1000).toLocaleString()}`);
  }));
});

function diffCell(v, cls) {
  if (v === "" || v == null) return `<span class="empty">(empty)</span>`;
  return `<span class="${cls}">${esc(v).replace(/\n/g, "<br>")}</span>`;
}

function openReview(d, heading) {
  const changed = d.files.filter(f => f.changes.length || f.cover);
  const unchanged = d.files.length - changed.length;
  const nothing = !changed.length && !d.side_files.length;
  const coverWord = { add: "Add cover art", replace: "Replace embedded cover art", restore: "Put back the original cover art" };

  $("#reviewTitle").textContent = heading;
  $("#reviewBody").innerHTML = `
    <p class="review-lead">Nothing has been saved yet. Check the changes below, then press <b>SAVE</b>.</p>
    <p>${changed.length} file(s) will change${unchanged ? `, ${unchanged} already match` : ""}.
      ${d.missing?.length ? `<br><span class="muted">Not in this folder any more (skipped): ${d.missing.map(esc).join(", ")}</span>` : ""}</p>
    ${d.has_cover ? `<div class="review-cover"><img src="/api/plan/${d.token}/cover" alt=""><span>New cover art</span></div>` : ""}
    ${d.side_files.length ? `<p>Other files: ${d.side_files.map(f =>
      `${f.action === "replace" ? "<b>replace</b>" : "create"} ${esc(f.name)}`).join(", ")}
      ${d.side_files.some(f => f.action === "replace") ? `<span class="muted">(the old one is kept as .bak)</span>` : ""}</p>` : ""}
    ${changed.map(f => `<div class="review-file">
      <div class="review-name">${esc(f.name)}</div>
      <table class="diff">${f.changes.map(c => `<tr><th>${esc(FIELD_LABELS[c.field] || c.field)}</th>
        <td>${diffCell(c.old, "old")}</td><td class="arrow">→</td><td>${diffCell(c.new, "new")}</td></tr>`).join("")}
        ${f.cover ? `<tr><th>Cover</th><td colspan="3">${coverWord[f.cover]}</td></tr>` : ""}</table>
    </div>`).join("")}`;
  $("#saveBtn").disabled = nothing;
  $("#saveBtn").textContent = nothing ? "Nothing to save" : `SAVE ${changed.length || ""} file(s)`.replace("  ", " ");
  $("#saveResult").innerHTML = "";
  $("#cancelBtn").textContent = "Cancel";
  $("#saveBtn").classList.remove("hidden");
  $("#saveBtn").onclick = () => busy($("#saveBtn"), async () => {
    try {
      const r = await api("/api/save", { body: { token: d.token } });
      $("#saveBtn").classList.add("hidden");
      $("#cancelBtn").textContent = "Close";
      $("#saveResult").innerHTML = (r.failed.length
        ? `<div class="err-box">Problems (those files were left as they were):<br>${r.failed.map(esc).join("<br>")}</div>` : "") +
        `<div class="ok-box">Saved ${r.saved.length} file(s)${r.side_files.length ? ` and ${r.side_files.map(esc).join(", ")}` : ""}.
         <br><span class="muted">The previous tags were backed up and can be put back with “Undo / backups”.
         In MediaMonkey, rescan the folder to pick up the changes.</span></div>`;
      const fresh = await api("/api/scan", { body: { path: state.scan.path } });
      state.scan = { ...state.scan, files: fresh.files };
      renderFileTable(fresh.files);
      $("#backupList").classList.add("hidden");
    } catch (e) {
      $("#saveResult").innerHTML = `<div class="err-box">${esc(e.message)}</div>`;
      $("#saveBtn").disabled = true;
    }
  });
  $("#reviewDialog").showModal();
}

$("#cancelBtn").onclick = () => $("#reviewDialog").close();

init();
