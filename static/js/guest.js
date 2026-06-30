// Guest frontend logic for Spotify-Partymode.
"use strict";

const $ = (sel) => document.querySelector(sel);
const POLL_MS = 4000;
let pollTimer = null;

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
  if (me.guest_name) {
    showApp(me.guest_name);
  } else {
    $("#login").classList.remove("hidden");
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

  renderNowPlaying(state.current);
  renderWishes(state.wishes);
  renderUpcoming(state.upcoming);
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

function trackRow(t, extraHtml = "") {
  const li = document.createElement("li");
  li.innerHTML = `
    <img class="cover" src="${t.image_url || ""}" alt="" />
    <div class="info">
      <div class="title">${escapeHtml(t.name)}</div>
      <div class="sub">${escapeHtml(t.artist)}${t.album ? " · " + escapeHtml(t.album) : ""}</div>
    </div>
    ${extraHtml}`;
  return li;
}

function renderWishes(wishes) {
  const ul = $("#wish-queue");
  ul.innerHTML = "";
  $("#wish-empty").classList.toggle("hidden", wishes.length > 0);
  for (const w of wishes) {
    const badge = w.status === "queued" ? '<span class="badge queued">next</span>' : "";
    const li = trackRow(w, `<div class="actions"><span class="added-by">${escapeHtml(w.added_by)}</span>${badge}</div>`);
    ul.appendChild(li);
  }
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

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

init();
