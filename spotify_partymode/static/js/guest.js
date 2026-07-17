// Guest frontend logic for Spotify-Partymode.
"use strict";

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 4000;
let pollTimer = null;

// Default cover shown when a track has no artwork (or it fails to load).
const DEFAULT_COVER = "data:image/svg+xml," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">' +
  '<rect width="80" height="80" rx="8" fill="#2a2a2a"/>' +
  '<text x="40" y="52" font-size="34" text-anchor="middle" fill="#1db954">♪</text></svg>'
);
function cover(url) {
  return `<img class="cover" src="${url || DEFAULT_COVER}" onerror="this.src='${DEFAULT_COVER}'" alt="" />`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 2500);
}

// --- auth ---
async function init() {
  const me = await api("/auth/me");
  if (me.guest_name && !me.is_admin) {
    showApp(me.guest_name);
  } else {
    $("#login").classList.remove("hidden");
    updateLoginGate();
    setInterval(updateLoginGate, POLL_MS);
  }
}

// Guests can only join while the party is on; reflect that on the login screen.
async function updateLoginGate() {
  let state;
  try { state = await api("/api/state"); } catch { return; }
  const closed = !state.party_on;
  $("#login-form").classList.toggle("hidden", closed);
  $("#login-closed").classList.toggle("hidden", !closed);
  if (state.session && state.session.name) {
    $("#login-session").textContent = "Party: " + state.session.name;
  }
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = $("#name-input").value.trim();
  if (!name) return;
  try {
    const r = await api("/auth/guest", { method: "POST", body: JSON.stringify({ name }) });
    showApp(r.guest_name);
  } catch (err) {
    toast(err.message);
  }
});

$("#logout").addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST" });
  location.reload();
});

function showApp(name) {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#who-name").textContent = name;
  refresh();
  pollTimer = setInterval(refresh, POLL_MS);
}

// --- state rendering ---
async function refresh() {
  let state;
  try {
    state = await api("/api/state");
  } catch {
    return;
  }
  $("#party-off-banner").classList.toggle("hidden", state.party_on);
  if ($("#session-label")) {
    $("#session-label").textContent = state.session ? state.session.name : "";
  }

  renderNowPlaying(state.current);
  renderTokens(state);
  renderWishes(state.wishes);
  renderUpcoming(state.upcoming);
  loadHistory();
  loadPlayHistory();
  loadBlocks();
}

// --- skip tokens ---
function renderTokens(state) {
  const info = $("#token-info");
  const btn = $("#skip-btn");
  const t = state.tokens || { max: 0, remaining: 0 };
  if (!t.max) {
    // Feature disabled by the admin (0 tokens) -> hide the whole control.
    info.textContent = "";
    btn.classList.add("hidden");
    return;
  }
  btn.classList.remove("hidden");
  info.textContent = `Skip tokens: ${t.remaining}/${t.max} left this hour`;
  btn.disabled = !(state.party_on && state.current && t.remaining > 0);
}

$("#skip-btn").addEventListener("click", async () => {
  try {
    const r = await api("/api/skip", { method: "POST" });
    toast("Skipped");
    if (r.tokens) {
      $("#token-info").textContent = `Skip tokens: ${r.tokens.remaining}/${r.tokens.max} left this hour`;
      $("#skip-btn").disabled = r.tokens.remaining <= 0;
    }
    refresh();
  } catch (err) { toast(err.message); }
});

async function loadPlayHistory() {
  let data;
  try { data = await api("/api/play-history"); } catch { return; }
  const ul = $("#play-history");
  if (!ul) return;
  ul.innerHTML = "";
  $("#play-empty").classList.toggle("hidden", data.history.length > 0);
  for (const h of data.history) {
    const tag = h.source === "wish" ? '<span class="badge queued">party added</span>' : "";
    const li = trackRow(h, `<div class="actions"><span class="hist-time">${fmtTime(h.played_at)}</span>${tag}</div>`);
    ul.appendChild(li);
  }
}

function fmtTime(ts) {
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "";
  }
}

async function loadHistory() {
  let data;
  try {
    data = await api("/api/history");
  } catch {
    return;
  }
  const ul = $("#history");
  if (!ul) return;
  ul.innerHTML = "";
  $("#history-empty").classList.toggle("hidden", data.history.length > 0);
  for (const h of data.history) {
    const tag = h.status === "rejected" ? '<span class="badge">rejected</span>' : "";
    const li = trackRow(h, `<div class="actions"><span class="added-by">${escapeHtml(h.added_by)}</span><span class="hist-time">${fmtTime(h.created_at)}</span>${tag}</div>`);
    ul.appendChild(li);
  }
}

function renderNowPlaying(track) {
  const el = $("#now-playing");
  const img = el.querySelector(".cover");
  img.onerror = () => { img.src = DEFAULT_COVER; };
  if (!track) {
    img.src = DEFAULT_COVER;
    el.querySelector(".title").textContent = "Nothing playing";
    el.querySelector(".sub").textContent = "";
    return;
  }
  img.src = track.image_url || DEFAULT_COVER;
  el.querySelector(".title").textContent = track.name;
  el.querySelector(".sub").textContent =
    (track.blacklisted ? "Blacklisted, will be skipped — " : "") + `${track.artist} · ${track.album}`;
}

function trackRow(t, extraHtml = "") {
  const li = document.createElement("li");
  const blocked = t.blacklisted
    ? '<div class="sub blacklisted">Blacklisted, will be skipped</div>'
    : "";
  li.innerHTML = `
    ${cover(t.image_url)}
    <div class="info">
      <div class="title">${escapeHtml(t.name)}</div>
      <div class="sub">${escapeHtml(t.artist)}${t.album ? " · " + escapeHtml(t.album) : ""}</div>
      ${blocked}
    </div>
    ${extraHtml}`;
  return li;
}

function renderWishes(wishes) {
  const ul = $("#wish-queue");
  ul.innerHTML = "";
  $("#wish-empty").classList.toggle("hidden", wishes.length > 0);
  for (const w of wishes) {
    const badge = badgeFor(w);
    const li = trackRow(w, `<div class="actions"><span class="added-by">${escapeHtml(w.added_by)}</span>${badge}</div>`);
    ul.appendChild(li);
  }
}

function badgeFor(w) {
  if (w.is_current) return '<span class="badge current">now playing</span>';
  if (w.status === "queued") return '<span class="badge queued">next</span>';
  return "";
}

function renderUpcoming(list) {
  const ul = $("#upcoming");
  ul.innerHTML = "";
  for (const t of list) ul.appendChild(trackRow(t));
}

// --- search & add ---
$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#search-input").value.trim();
  if (!q) return;
  try {
    const { results } = await api(`/api/search?q=${encodeURIComponent(q)}`);
    renderResults(results);
  } catch (err) {
    toast(err.message);
  }
});

function renderResults(results) {
  const ul = $("#search-results");
  ul.innerHTML = "";
  for (const t of results) {
    const li = trackRow(t, '<div class="actions"><button class="btn primary icon">+</button></div>');
    li.querySelector("button").addEventListener("click", () => addWish(t));
    ul.appendChild(li);
  }
}

async function addWish(t) {
  try {
    await api("/api/wish", { method: "POST", body: JSON.stringify(t) });
    toast(`Added "${t.name}"`);
    refresh();
  } catch (err) {
    toast(err.message);
  }
}

// --- personal blocks ---
$("#block-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const kind = $("#block-kind").value;
  const q = $("#block-search").value.trim();
  if (!q) return;
  try {
    const { results } = await api(`/api/search?type=${kind}&q=${encodeURIComponent(q)}`);
    renderBlockResults(kind, results);
  } catch (err) { toast(err.message); }
});

function renderBlockResults(kind, results) {
  const ul = $("#block-results");
  ul.innerHTML = "";
  for (const r of results) {
    const li = document.createElement("li");
    const sub = kind === "artist" ? "Artist" : escapeHtml(r.artist || "");
    li.innerHTML = `
      ${cover(r.image_url)}
      <div class="info">
        <div class="title">${escapeHtml(r.name)}</div>
        <div class="sub">${sub}</div>
      </div>
      <div class="actions"><button class="btn ghost small">Block</button></div>`;
    li.querySelector("button").addEventListener("click", () => blockItem(kind, r));
    ul.appendChild(li);
  }
}

async function blockItem(kind, r) {
  try {
    await api("/api/block", { method: "POST", body: JSON.stringify({ kind, spotify_id: r.id, name: r.name }) });
    toast(`Blocked "${r.name}"`);
    $("#block-search").value = "";
    $("#block-results").innerHTML = "";
    loadBlocks();
    refresh();
  } catch (err) { toast(err.message); }
}

async function loadBlocks() {
  let data;
  try { data = await api("/api/blocks"); } catch { return; }
  const ul = $("#block-list");
  ul.innerHTML = "";
  $("#block-empty").classList.toggle("hidden", data.blocks.length > 0);
  for (const b of data.blocks) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div class="info">
        <div class="title">${escapeHtml(b.name)}</div>
        <div class="sub">${escapeHtml(b.kind)}</div>
      </div>
      <div class="actions"><button class="btn icon small ghost">✕</button></div>`;
    li.querySelector("button").addEventListener("click", async () => {
      try { await api(`/api/block/${b.id}`, { method: "DELETE" }); loadBlocks(); refresh(); }
      catch (err) { toast(err.message); }
    });
    ul.appendChild(li);
  }
  const lim = data.limits || {};
  $("#block-limits").textContent =
    `Artists: ${lim.artists_used || 0}/${lim.artists_max || 0}  ·  Tracks: ${lim.tracks_used || 0}/${lim.tracks_max || 0}`;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
