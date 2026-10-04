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
  runAudit();
}

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { } }

// ------------------------------------------------------- library check

state.audit = [];

// Albums with missing/generic song titles first, then other major gaps, then minor.
function auditRank(a) {
  const major = a.problems.filter(p => p.level === "major");
  const titles = major.some(p => /song titles/.test(p.text));
  return titles ? 0 : major.length ? 1 : a.problems.length ? 2 : 3;
}

async function runAudit() {
  $("#libSummary").textContent = "Checking your library…";
  $("#libRescan").disabled = true;
  try {
    const d = await api("/api/audit");
    state.audit = d.albums;
    state.auditRoot = d.root;
    renderAudit();
  } catch (e) {
    $("#libSummary").textContent = e.message;
  } finally { $("#libRescan").disabled = false; }
}

function renderAudit() {
  const albums = [...state.audit].sort((a, b) => auditRank(a) - auditRank(b) || a.name.localeCompare(b.name));
  const major = albums.filter(a => auditRank(a) <= 1).length;
  const any = albums.filter(a => a.problems.length).length;
  $("#libSummary").textContent = `${state.auditRoot}: ${albums.length} albums · ${major} need attention · ${any} with any gaps`;
  const f = $("#libFilter").value;
  const shown = albums.filter(a => f === "all" || (f === "any" ? a.problems.length : auditRank(a) <= 1));
  $("#libList").innerHTML = shown.length ? shown.map(a => `<div class="lib-row">
      <div class="lib-name" title="${esc(a.path)}">${esc(a.name)} <span class="muted">· ${a.files} files</span></div>
      <div class="chips">${a.problems.map(p => `<span class="chip ${p.level}">${esc(p.text)}</span>`).join("") ||
        `<span class="chip ok">Looks complete</span>`}</div>
      <button class="secondary" data-open="${esc(a.path)}">Open</button>
    </div>`).join("") : `<div class="muted">Nothing to show with this filter.</div>`;
  $$("[data-open]", $("#libList")).forEach(b => b.onclick = async () => {
    $("#folderPath").value = b.dataset.open;
    await scan();
    $("#folderPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  });
}

async function refreshAuditFor(path) {
  try {
    const d = await api(`/api/audit?album=${encodeURIComponent(path)}`);
    const fresh = d.albums[0];
    const i = state.audit.findIndex(a => a.path === path);
    if (i >= 0) { state.audit[i] = { ...state.audit[i], ...fresh, name: state.audit[i].name }; renderAudit(); }
  } catch { /* the full check can be rerun */ }
}

$("#libFilter").onchange = renderAudit;
$("#libRescan").onclick = runAudit;

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
    $("#chatPrompt").value = ""; $("#chatReply").value = ""; $("#chatResult").innerHTML = "";
    $("#chatCopyBtn").disabled = true; $("#chatCopied").textContent = "";
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

// ----------------------------------------------------- ask an AI chat
// Builds a prompt for the user's own AI chat, and reads the JSON reply back.

const CHAT_FORMAT = `{
  "album_title": "Album title exactly as printed, original script",
  "album_title_romanized": "Hepburn romanization, or null if already Latin script",
  "artist": "Album artist, original script (use the real artist, or Various Artists for compilations)",
  "artist_romanized": "romanization or null",
  "release_date": "YYYY-MM-DD, YYYY-MM or YYYY, or null",
  "label": "Record label or null",
  "catalog_number": "e.g. VICL-60001, or null",
  "barcode": "digits only, or null",
  "media": "CD or Vinyl",
  "discs": [
    {
      "number": 1,
      "tracks": [
        {
          "number": 1,
          "position": "A1 for vinyl, otherwise null",
          "title": "Song title exactly as printed, original script",
          "title_romanized": "romanization or null",
          "artist": "This song's artist (important for compilations), or null if same as album artist",
          "length": "m:ss or null"
        }
      ]
    }
  ],
  "confidence": "high, medium or low",
  "notes": "Anything uncertain, and which edition you matched",
  "sources": ["URLs you used"]
}`;

function buildChatPrompt() {
  const sc = state.scan;
  const files = sc.files;
  const folder = sc.path.split(/[\\/]/).pop();
  const first = files[0];
  const generic = files.filter(f => !f.title || /^(track|トラック|audio\s*track|unknown|untitled)\s*\d*$|^\d+$/i.test(f.title.trim())).length;
  const known = [
    ["Album", first.album], ["Album artist", first.albumartist || first.artist], ["Date", first.date],
    ["Label", first.label], ["Catalog number", first.catalognumber], ["Barcode", first.barcode],
  ].filter(([, v]) => v).map(([k, v]) => `- ${k}: ${v}`);
  let layout;
  if (sc.looks_like_vinyl) {
    layout = "It's a vinyl record, ripped as one file per side:\n" +
      files.map(f => `- Side ${f.side || "?"}: ${fmtLen(f.length)} total`).join("\n");
  } else {
    const byDisc = {};
    files.forEach(f => (byDisc[f.disc] ||= []).push(f));
    layout = Object.entries(byDisc).map(([d, fs]) =>
      `Disc ${d}: ${fs.length} tracks. Exact lengths of my ripped tracks:\n` +
      fs.map((f, i) => `  ${i + 1}. ${fmtLen(f.length)}${f.title && generic < files.length ? `  (currently tagged "${f.title}")` : ""}`).join("\n")
    ).join("\n");
  }
  return `I've ripped a ${sc.looks_like_vinyl ? "vinyl record" : "CD"} (most of my collection is Japanese releases) and need accurate metadata for it. Please identify the exact release and give me its full track list.

What I know:
- Folder name: ${folder}
${known.join("\n")}
${generic ? `- ${generic === files.length ? "All" : generic} of the song titles are missing or placeholders like "Track01", so don't rely on them.\n` : ""}
${layout}

How to find it:
- Search the web: VGMdb, MusicBrainz, Discogs, Amazon.co.jp, Tower Records Japan, HMV Japan, CDJournal and the label's or artist's official site are good sources. A catalog number (like ABCD-12345, printed on the spine or obi) is the most reliable thing to search for.
- If I've attached photos of the packaging, read them first.
- Make sure the track count and track lengths match my rip (within a few seconds). If there are several editions, pick the one that matches.
- Write titles and names exactly as printed, in the original script (kanji/kana). Put a Hepburn romanization in the *_romanized fields when the original isn't in Latin script.
- For compilations, give each song's own artist.
- Don't guess. If you can't confirm something, use null and explain it in "notes".

Reply with ONLY one JSON code block in exactly this format (the values below describe what goes in each field):

\`\`\`json
${CHAT_FORMAT}
\`\`\``;
}

$("#chatMakeBtn").onclick = () => {
  if (!state.scan) return showErrors({ error: "Load an album folder first." });
  $("#chatPrompt").value = buildChatPrompt();
  $("#chatCopyBtn").disabled = false;
  $("#chatCopied").textContent = "";
};

$("#chatCopyBtn").onclick = async () => {
  const box = $("#chatPrompt");
  try { await navigator.clipboard.writeText(box.value); }
  catch { box.select(); document.execCommand("copy"); }
  $("#chatCopied").textContent = "Copied. Paste it into a new chat.";
};

function parseChatReply(text) {
  const fenced = text.match(/```(?:json)?\s*([\s\S]*?)```/i);
  let body = fenced ? fenced[1] : text;
  const a = body.indexOf("{"), b = body.lastIndexOf("}");
  if (a < 0 || b < a) throw new Error("Couldn't find a JSON block in the reply.");
  body = body.slice(a, b + 1).replace(/[“”]/g, '"');
  let r;
  try { r = JSON.parse(body); }
  catch (e) { throw new Error(`The JSON in the reply isn't valid (${e.message}). Ask the chat to resend just the JSON block.`); }
  if (!r.album_title) throw new Error('The reply has no "album_title".');
  if (!Array.isArray(r.discs) || !r.discs.some(d => d.tracks?.length))
    throw new Error('The reply has no track list ("discs" → "tracks").');
  const albumArtist = r.artist || "";
  const discs = r.discs.map((d, i) => ({
    number: d.number || i + 1,
    tracks: (d.tracks || []).map((t, j) => ({
      number: t.number || j + 1, position: t.position || "",
      title: t.title || "", title_romanized: t.title_romanized || "",
      artist: t.artist || albumArtist, length: parseLen(t.length || ""),
    })),
  }));
  return {
    source: "AI chat", id: "", url: (r.sources || [])[0] || "",
    title: r.album_title, title_romanized: r.album_title_romanized || "",
    artist: albumArtist, artist_romanized: r.artist_romanized || "", album_artist: albumArtist,
    date: r.release_date || "", country: "", label: r.label || "", catalog: r.catalog_number || "",
    barcode: String(r.barcode || "").replace(/\D/g, ""), format: r.media || "",
    track_count: discs.reduce((n, d) => n + d.tracks.length, 0), cover_url: "",
    discs, confidence: r.confidence, notes: r.notes || "", sources: r.sources || [],
  };
}

$("#chatUseBtn").onclick = () => {
  const out = $("#chatResult");
  if (!state.scan) { out.innerHTML = `<span class="err-box">Load the album folder first.</span>`; return; }
  try {
    const rel = parseChatReply($("#chatReply").value);
    const n = state.scan.files.length;
    const countNote = state.scan.looks_like_vinyl ? "" :
      rel.track_count === n ? ` · ${n} tracks ✓` : ` · ${rel.track_count} tracks, but the folder has ${n} files`;
    out.innerHTML = `<span class="ok-box">Loaded “${esc(rel.title)}”${esc(countNote)}${rel.confidence ? ` · confidence: ${esc(rel.confidence)}` : ""}. Check it in Review below.</span>` +
      (rel.notes ? `<div class="notes muted">${esc(rel.notes)}</div>` : "");
    loadRelease(rel);
  } catch (e) {
    out.innerHTML = `<span class="err-box">${esc(e.message)}</span>`;
  }
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
  const links = rel.sources?.length ? rel.sources : rel.url ? [rel.url] : [];
  $("#sourceLine").innerHTML = `From <b>${esc(rel.source)}</b>` +
    links.map(u => ` · <a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`).join("");

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
      refreshAuditFor(state.scan.path);
    } catch (e) {
      $("#saveResult").innerHTML = `<div class="err-box">${esc(e.message)}</div>`;
      $("#saveBtn").disabled = true;
    }
  });
  $("#reviewDialog").showModal();
}

$("#cancelBtn").onclick = () => $("#reviewDialog").close();

init();
