"use strict";

// Declarative field map: key -> { label, group, type }. Mirrors _KNOWN_KEYS in config.py.
const FIELDS = [
  { key: "music_path", label: "Music folder", group: "essential", type: "text" },
  { key: "lastfm_api_key", label: "Last.fm API key", group: "essential", type: "text" },
  { key: "db_path", label: "Ledger database path", group: "advanced", type: "text" },
  { key: "genre_min_weight", label: "Genre min weight", group: "advanced", type: "text" },
  { key: "genre_max_count", label: "Genre max count (blank = no cap)", group: "advanced", type: "text" },
  { key: "genre_use_album_tags", label: "Use album tags for genre", group: "advanced", type: "checkbox" },
  { key: "lastfm_rate_per_sec", label: "Last.fm requests/sec", group: "advanced", type: "text" },
  { key: "genre_stage_limit", label: "Genre stage limit", group: "advanced", type: "text" },
  { key: "musicbrainz_rate_per_sec", label: "MusicBrainz requests/sec", group: "advanced", type: "text" },
  { key: "musicbrainz_contact", label: "MusicBrainz contact (email or URL)", group: "advanced", type: "text" },
  { key: "album_stage_limit", label: "Album stage limit", group: "advanced", type: "text" },
];

const csrfToken = document.querySelector('meta[name="csrf-token"]').content;

function postJSON(url, body) {
  return fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-TagMend-CSRF": csrfToken },
    body: JSON.stringify(body),
  });
}

function renderField(field, value) {
  const wrap = document.createElement("div");
  const label = document.createElement("label");
  label.textContent = field.label;
  label.htmlFor = field.key;

  let input;
  if (field.type === "checkbox") {
    wrap.className = "field checkbox";
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = String(value).toLowerCase() === "true";
    wrap.append(input, label);
  } else {
    wrap.className = "field";
    input = document.createElement("input");
    input.type = "text";
    input.value = value == null ? "" : value;
    wrap.append(label, input);
  }
  input.id = field.key;
  input.name = field.key;
  return wrap;
}

function collectPayload() {
  const payload = {};
  for (const field of FIELDS) {
    const input = document.getElementById(field.key);
    if (field.type === "checkbox") {
      payload[field.key] = input.checked ? "true" : "false";
    } else {
      payload[field.key] = input.value;
    }
  }
  return payload;
}

function setResult(node, ok, message) {
  node.textContent = message;
  node.className = "result " + (ok ? "ok" : "err");
}

async function load() {
  const seed = await (await fetch("/api/seed")).json();
  const values = seed.values || {};
  const groups = { essential: document.getElementById("essential"), advanced: document.getElementById("advanced") };
  for (const field of FIELDS) {
    groups[field.group].append(renderField(field, values[field.key]));
  }
}

async function save(event) {
  event.preventDefault();
  const node = document.getElementById("save-result");
  try {
    const resp = await postJSON("/api/save", collectPayload());
    const data = await resp.json();
    setResult(node, resp.ok && data.ok, resp.ok && data.ok ? "Saved." : data.error || "Save failed.");
  } catch (err) {
    setResult(node, false, String(err));
  }
}

async function testKey() {
  const node = document.getElementById("test-result");
  node.textContent = "Testing…";
  node.className = "result";
  try {
    const resp = await postJSON("/api/test", { lastfm_api_key: document.getElementById("lastfm_api_key").value });
    const data = await resp.json();
    setResult(node, data.ok, data.ok ? "Key works." : data.error || "Key failed.");
  } catch (err) {
    setResult(node, false, String(err));
  }
}

document.getElementById("settings-form").addEventListener("submit", save);
document.getElementById("test-key").addEventListener("click", testKey);
load();
