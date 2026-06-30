// Admin frontend logic for Spotify-Partymode.
"use strict";

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 4000;

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
  setInterval(refresh, POLL_MS);
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
  $("#set-poll").value = s.poll_interval_seconds || 5;
  $("#secret-hint").textContent = s.spotify_client_secret_set ? "(stored)" : "(not set)";
  if (!$("#playlist-input").value && s.default_playlist) $("#playlist-input").value = s.default_playlist;
  renderSpotify(s.spotify_connected);
}

$("#settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    spotify_client_id: $("#set-client-id").value.trim(),
    spotify_client_secret: $("#set-client-secret").value.trim(),
    spotify_redirect_uri: $("#set-redirect").value.trim(),
    poll_interval_seconds: parseInt($("#set-poll").value, 10) || 5,
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
}

function renderNowPlaying(track) {
  const el = $("#now-playing");
  if (!track) {
    el.querySelector(".cover").src = "";
    el.querySelector(".title").textContent = "Nothing playing";
    el.querySelector(".sub").textContent = "";
    return;
  }
  el.querySelector(".cover").src = track.image_url || "";
  el.querySelector(".title").textContent = track.name;
  el.querySelector(".sub").textContent = `${track.artist} · ${track.album}`;
}

function renderWishAdmin(wishes) {
  const ul = $("#wish-admin");
  ul.innerHTML = "";
  $("#wish-empty").classList.toggle("hidden", wishes.length > 0);
  const pendingIds = wishes.filter((w) => w.status === "pending").map((w) => w.id);

  wishes.forEach((w) => {
    const li = document.createElement("li");
    const isPending = w.status === "pending";
    const badge = w.status === "queued" ? '<span class="badge queued">next</span>' : "";
    li.innerHTML = `
      <img class="cover" src="${w.image_url || ""}" alt="" />
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

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
