// Admin frontend logic for Spotify-Partymode.
"use strict";

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 4000;

const DEFAULT_COVER = "data:image/svg+xml," + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">' +
  '<rect width="80" height="80" rx="8" fill="#2a2a2a"/>' +
  '<text x="40" y="52" font-size="34" text-anchor="middle" fill="#1db954">♪</text></svg>'
);
function coverImg(url) {
  return `<img class="cover" src="${url || DEFAULT_COVER}" onerror="this.src='${DEFAULT_COVER}'" alt="" />`;
}
let viewSessionId = null; // which session's history is being viewed

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

// --- bootstrap: setup -> login -> panel ---
async function init() {
  const status = await api("/auth/status");
  if (!status.setup_complete) {
    location.href = "/setup.html";
    return;
  }
  if (!status.is_admin) {
    $("#login").classList.remove("hidden");
    return;
  }
  await enterPanel();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/auth/admin/login", {
      method: "POST",
      body: JSON.stringify({ username: $("#login-user").value.trim(), password: $("#login-pass").value }),
    });
    $("#login").classList.add("hidden");
    await enterPanel();
  } catch (err) {
    toast(err.message);
  }
});

async function enterPanel() {
  $("#admin").classList.remove("hidden");
  await loadSettings();
  await refresh();
  await loadBlacklist();
  await loadUsers();
  await loadSessions();
  setInterval(refresh, POLL_MS);
}

// --- accounts / users ---
async function loadUsers() {
  const { users } = await api("/api/admin/users");
  const ul = $("#user-list");
  ul.innerHTML = "";
  users.forEach((u) => {
    const li = document.createElement("li");
    li.innerHTML = `<div class="info"><div class="title">${escapeHtml(u.username)}</div></div>`;
    const del = iconBtn("✕", async () => {
      try { await api(`/api/admin/users/${u.id}`, { method: "DELETE" }); loadUsers(); }
      catch (err) { toast(err.message); }
    });
    del.classList.add("ghost");
    const wrap = document.createElement("div");
    wrap.className = "actions";
    wrap.appendChild(del);
    li.appendChild(wrap);
    ul.appendChild(li);
  });
}

$("#user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("#user-name").value.trim();
  const password = $("#user-pass").value;
  if (!username || !password) return toast("Username and password required");
  try {
    await api("/api/admin/users", { method: "POST", body: JSON.stringify({ username, password }) });
    $("#user-name").value = ""; $("#user-pass").value = "";
    toast("Account created");
    loadUsers();
  } catch (err) { toast(err.message); }
});

$("#reg-toggle").addEventListener("change", async (e) => {
  try {
    await api("/api/admin/settings", { method: "POST", body: JSON.stringify({ registration_open: e.target.checked }) });
    setRegLabel(e.target.checked);
    toast(e.target.checked ? "Registration opened" : "Registration closed");
  } catch (err) { toast(err.message); }
});

function setRegLabel(open) {
  $("#reg-label").textContent = open
    ? "Self-registration: OPEN — anyone can create an account."
    : "Self-registration: CLOSED — only admins create accounts.";
}

// --- spotify connection ---
async function renderSpotify(connected) {
  $("#spotify-status").textContent = connected ? "Connected ✓" : "Not connected";
  $("#connect-btn").classList.toggle("hidden", connected);
  $("#disconnect-btn").classList.toggle("hidden", !connected);
}

$("#disconnect-btn").addEventListener("click", async () => {
  try { await api("/auth/spotify/disconnect", { method: "POST" }); renderSpotify(false); toast("Spotify disconnected"); }
  catch (err) { toast(err.message); }
});

// --- party toggle ---
$("#party-toggle").addEventListener("change", async (e) => {
  try {
    await api("/api/admin/party", { method: "POST", body: JSON.stringify({ on: e.target.checked }) });
    toast(e.target.checked ? "Party mode on" : "Party mode off");
  } catch (err) { toast(err.message); }
});

// --- playlist ---
$("#playlist-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const playlist = normalizePlaylist($("#playlist-input").value.trim());
  try { await api("/api/admin/playlist", { method: "POST", body: JSON.stringify({ playlist }) }); toast("Playlist saved"); }
  catch (err) { toast(err.message); }
});

$("#start-btn").addEventListener("click", async () => {
  try { await api("/api/admin/start", { method: "POST" }); toast("Playback started"); }
  catch (err) { toast(err.message); }
});

$("#skip-btn").addEventListener("click", async () => {
  try { await api("/api/admin/skip", { method: "POST" }); toast("Skipped"); }
  catch (err) { toast(err.message); }
});

$("#logout").addEventListener("click", async () => {
  await api("/auth/logout", { method: "POST" });
  location.href = "/";
});

function normalizePlaylist(value) {
  const m = value.match(/playlist[/:]([a-zA-Z0-9]+)/);
  return m ? m[1] : value;
}

// --- settings ---
async function loadSettings() {
  const s = await api("/api/admin/settings");
  $("#set-client-id").value = s.spotify_client_id || "";
  $("#set-redirect").value = s.spotify_redirect_uri || "";
  $("#set-poll").value = s.poll_interval_seconds || 4;
  $("#set-lead").value = s.insert_lead_seconds || 20;
  $("#secret-hint").textContent = s.spotify_client_secret_set ? "(stored)" : "(not set)";
  if (!$("#playlist-input").value && s.default_playlist) $("#playlist-input").value = s.default_playlist;
  $("#reg-toggle").checked = !!s.registration_open;
  setRegLabel(!!s.registration_open);
  renderSpotify(s.spotify_connected);
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    spotify_client_id: $("#set-client-id").value.trim(),
    spotify_client_secret: $("#set-client-secret").value.trim(),
    spotify_redirect_uri: $("#set-redirect").value.trim(),
    poll_interval_seconds: parseInt($("#set-poll").value, 10) || 4,
    insert_lead_seconds: parseInt($("#set-lead").value, 10) || 20,
  };
  try {
    await api("/api/admin/settings", { method: "POST", body: JSON.stringify(body) });
    $("#set-client-secret").value = "";
    toast("Settings saved");
    loadSettings();
  } catch (err) { toast(err.message); }
});

// --- state + wish queue ---
async function refresh() {
  let state;
  try { state = await api("/api/state"); } catch { return; }
  $("#party-toggle").checked = state.party_on;
  renderNowPlaying(state.current);
  renderWishAdmin(state.wishes);
  if ($("#session-status")) {
    $("#session-status").textContent = state.session ? ("Active: " + state.session.name) : "No active session";
  }
  loadHistory();
  loadPlayHistory();
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

function renderWishAdmin(wishes) {
  const ul = $("#wish-admin");
  ul.innerHTML = "";
  $("#wish-empty").classList.toggle("hidden", wishes.length > 0);
  const pendingIds = wishes.filter((w) => w.status === "pending").map((w) => w.id);

  wishes.forEach((w) => {
    const li = document.createElement("li");
    const isPending = w.status === "pending";
    const badge = w.is_current
      ? '<span class="badge current">now playing</span>'
      : (w.status === "queued" ? '<span class="badge queued">next</span>' : "");
    li.innerHTML = `
      ${coverImg(w.image_url)}
      <div class="info">
        <div class="title">${escapeHtml(w.name)}</div>
        <div class="sub">${escapeHtml(w.artist)} · <span class="added-by">${escapeHtml(w.added_by)}</span></div>
      </div>
      <div class="actions">${badge}</div>`;
    const actions = li.querySelector(".actions");
    if (isPending) {
      const up = iconBtn("↑", () => move(pendingIds, w.id, -1));
      const down = iconBtn("↓", () => move(pendingIds, w.id, +1));
      const reject = iconBtn("✕", () => rejectWish(w.id));
      reject.classList.add("ghost");
      actions.append(up, down, reject);
    }
    ul.appendChild(li);
  });
}

function iconBtn(label, onClick) {
  const b = document.createElement("button");
  b.className = "btn icon small";
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

async function move(ids, id, delta) {
  const i = ids.indexOf(id);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= ids.length) return;
  [ids[i], ids[j]] = [ids[j], ids[i]];
  try { await api("/api/admin/reorder", { method: "POST", body: JSON.stringify({ ids }) }); refresh(); }
  catch (err) { toast(err.message); }
}

async function rejectWish(id) {
  try { await api("/api/admin/reject", { method: "POST", body: JSON.stringify({ wish_id: id }) }); refresh(); }
  catch (err) { toast(err.message); }
}

// --- blacklist ---
$("#blacklist-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { kind: $("#bl-kind").value, spotify_id: $("#bl-id").value.trim(), name: $("#bl-name").value.trim() };
  if (!body.spotify_id || !body.name) return toast("ID and name required");
  try {
    await api("/api/admin/blacklist", { method: "POST", body: JSON.stringify(body) });
    $("#bl-id").value = ""; $("#bl-name").value = "";
    loadBlacklist();
  } catch (err) { toast(err.message); }
});

async function loadBlacklist() {
  const { items } = await api("/api/admin/blacklist");
  const ul = $("#blacklist");
  ul.innerHTML = "";
  items.forEach((it) => {
    const li = document.createElement("li");
    li.innerHTML = `<div class="info"><div class="title">${escapeHtml(it.name)}</div>
      <div class="sub">${it.kind} · ${escapeHtml(it.spotify_id)}</div></div>`;
    const del = iconBtn("✕", async () => { await api(`/api/admin/blacklist/${it.id}`, { method: "DELETE" }); loadBlacklist(); });
    del.classList.add("ghost");
    const wrap = document.createElement("div");
    wrap.className = "actions";
    wrap.appendChild(del);
    li.appendChild(wrap);
    ul.appendChild(li);
  });
}

// --- sessions + history ---
function fmtTime(ts) {
  try { return new Date(ts * 1000).toLocaleString(); } catch { return ""; }
}

let currentSessionId = null;

async function loadSessions() {
  let data;
  try { data = await api("/api/admin/sessions"); } catch { return; }
  const sel = $("#session-select");
  const cur = data.sessions.find((s) => s.is_current);
  currentSessionId = cur ? cur.id : null;
  if (viewSessionId === null) viewSessionId = currentSessionId;
  sel.innerHTML = "";
  data.sessions.forEach((s) => {
    const opt = document.createElement("option");
    opt.value = s.id;
    const when = new Date(s.started_at * 1000).toLocaleDateString();
    opt.textContent = `${s.name}${s.is_current ? " (active)" : ""} · ${when}`;
    if (s.id === viewSessionId) opt.selected = true;
    sel.appendChild(opt);
  });
  if (data.sessions.length === 0) {
    sel.innerHTML = '<option>No sessions yet</option>';
  }
  updateClearButtons();
  loadHistory();
  loadPlayHistory();
}

function updateClearButtons() {
  const isCurrent = viewSessionId === currentSessionId && currentSessionId !== null;
  $("#clear-history").classList.toggle("hidden", !isCurrent);
  $("#clear-play-history").classList.toggle("hidden", !isCurrent);
}

$("#session-select").addEventListener("change", (e) => {
  viewSessionId = parseInt(e.target.value, 10) || null;
  updateClearButtons();
  loadHistory();
  loadPlayHistory();
});

$("#session-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const r = await api("/api/admin/sessions/start", { method: "POST", body: JSON.stringify({ name: $("#session-name").value.trim() }) });
    $("#session-name").value = "";
    viewSessionId = r.session_id;
    toast("Party started");
    await loadSessions();
    refresh();
  } catch (err) { toast(err.message); }
});

$("#end-session").addEventListener("click", async () => {
  if (!confirm("End the current party session?")) return;
  try { await api("/api/admin/sessions/end", { method: "POST" }); toast("Party ended"); await loadSessions(); refresh(); }
  catch (err) { toast(err.message); }
});

async function loadHistory() {
  if (!viewSessionId) { $("#history").innerHTML = ""; $("#history-empty").classList.remove("hidden"); return; }
  let data;
  try { data = await api(`/api/admin/sessions/${viewSessionId}/history`); } catch { return; }
  const ul = $("#history");
  ul.innerHTML = "";
  $("#history-empty").classList.toggle("hidden", data.history.length > 0);
  data.history.forEach((h) => {
    const li = document.createElement("li");
    const tag = h.status === "rejected" ? '<span class="badge">rejected</span>' : "";
    li.innerHTML = `
      ${coverImg(h.image_url)}
      <div class="info">
        <div class="title">${escapeHtml(h.name)}</div>
        <div class="sub"><span class="added-by">${escapeHtml(h.added_by)}</span> · <span class="hist-time">${fmtTime(h.created_at)}</span> ${tag}</div>
      </div>`;
    const del = iconBtn("✕", async () => {
      try { await api(`/api/admin/history/${h.id}`, { method: "DELETE" }); loadHistory(); }
      catch (err) { toast(err.message); }
    });
    del.classList.add("ghost");
    const wrap = document.createElement("div");
    wrap.className = "actions";
    wrap.appendChild(del);
    li.appendChild(wrap);
    ul.appendChild(li);
  });
}

async function loadPlayHistory() {
  if (!viewSessionId) { $("#play-history").innerHTML = ""; $("#play-empty").classList.remove("hidden"); return; }
  let data;
  try { data = await api(`/api/admin/sessions/${viewSessionId}/play-history`); } catch { return; }
  const ul = $("#play-history");
  ul.innerHTML = "";
  $("#play-empty").classList.toggle("hidden", data.history.length > 0);
  data.history.forEach((h) => {
    const li = document.createElement("li");
    const tag = h.source === "wish" ? '<span class="badge queued">party added</span>' : "";
    const by = h.added_by ? ` · <span class="added-by">${escapeHtml(h.added_by)}</span>` : "";
    li.innerHTML = `
      ${coverImg(h.image_url)}
      <div class="info">
        <div class="title">${escapeHtml(h.name)}</div>
        <div class="sub"><span class="hist-time">${fmtTime(h.played_at)}</span>${by} ${tag}</div>
      </div>`;
    const del = iconBtn("✕", async () => {
      try { await api(`/api/admin/play-history/${h.id}`, { method: "DELETE" }); loadPlayHistory(); }
      catch (err) { toast(err.message); }
    });
    del.classList.add("ghost");
    const wrap = document.createElement("div");
    wrap.className = "actions";
    wrap.appendChild(del);
    li.appendChild(wrap);
    ul.appendChild(li);
  });
}

$("#clear-history").addEventListener("click", async () => {
  if (!confirm("Clear the wish history for this session?")) return;
  try { await api("/api/admin/history/clear", { method: "POST" }); toast("Wish history cleared"); loadHistory(); }
  catch (err) { toast(err.message); }
});

$("#clear-play-history").addEventListener("click", async () => {
  if (!confirm("Clear the play history for this session?")) return;
  try { await api("/api/admin/play-history/clear", { method: "POST" }); toast("Play history cleared"); loadPlayHistory(); }
  catch (err) { toast(err.message); }
});

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
