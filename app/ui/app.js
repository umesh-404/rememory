/* rememory desktop UI logic.
 *
 * Every control here calls a real backend method through pywebview's bridge
 * (window.pywebview.api.*) -- there are no placeholder handlers. Long actions
 * show the overlay, short ones show a toast, and everything reports what
 * actually happened.
 */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

let api = null;              // set once pywebview is ready
let statusTimer = null;
let lastStatus = null;

/* ───────────────────────────── plumbing ───────────────────────────── */

function toast(message, kind = "info") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 240);
  }, kind === "bad" ? 6500 : 3800);
}

function busy(on, text = "Working…") {
  $("#overlayText").textContent = text;
  $("#overlay").classList.toggle("hidden", !on);
}

/** Call a backend method with the overlay up; always surfaces the outcome. */
async function act(method, args = [], busyText = null) {
  if (!api) { toast("Still connecting to rememory…", "bad"); return null; }
  if (busyText) busy(true, busyText);
  try {
    const res = await api[method](...args);
    if (res && typeof res === "object" && "ok" in res && res.message) {
      toast(res.message, res.ok ? "ok" : "bad");
    }
    return res;
  } catch (err) {
    toast(`Something went wrong: ${err}`, "bad");
    return null;
  } finally {
    if (busyText) busy(false);
  }
}

function fmtWhen(iso) {
  if (!iso) return "never";
  const then = new Date(iso);
  if (isNaN(then)) return "never";
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} h ago`;
  const days = Math.round(hrs / 24);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ───────────────────────────── navigation ───────────────────────────── */

$$(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".nav-item").forEach((b) => b.classList.remove("active"));
    $$(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    if (btn.dataset.tab === "projects") loadProjects();
    if (btn.dataset.tab === "memories") loadMemories();
    if (btn.dataset.tab === "connect") loadConnect();
    if (btn.dataset.tab === "settings") loadSettings();
  });
});

/* ───────────────────────────── overview ───────────────────────────── */

function paintService(key, up, upText, downText) {
  const card = $(`.service[data-svc="${key}"]`);
  if (!card) return;
  card.querySelector(".dot").className = `dot ${up ? "dot-ok" : "dot-bad"}`;
  card.querySelector(".svc-state").textContent = up ? upText : downText;
}

async function refreshStatus(showToast = false) {
  if (!api) return;
  const st = await api.status();
  if (!st) return;
  lastStatus = st;

  paintService("docker", st.services.docker, "Running", "Not running");
  paintService("database", st.services.database, "Running", "Offline");
  paintService("models", st.services.models,
    "Ready", st.services.ollama ? "Models missing" : "Ollama offline");

  const tasks = st.tasks || {};
  const taskKeys = Object.keys(tasks);
  const tasksOk = taskKeys.length > 0 && taskKeys.every((k) => tasks[k]);
  const card = $('.service[data-svc="automation"]');
  card.querySelector(".dot").className =
    `dot ${taskKeys.length === 0 ? "dot-warn" : tasksOk ? "dot-ok" : "dot-warn"}`;
  card.querySelector(".svc-state").textContent =
    taskKeys.length === 0 ? "Manual" : tasksOk ? "Scheduled" : "Partly set up";

  const c = st.collections || {};
  $("#statCode").textContent = (c.code ?? 0).toLocaleString();
  $("#statDocs").textContent = (c.docs ?? 0).toLocaleString();
  $("#statMemory").textContent = (c.memory ?? 0).toLocaleString();
  $("#navMemoryCount").textContent = c.memory ? c.memory.toLocaleString() : "";

  $("#healthPill").querySelector(".dot").className = `dot ${st.healthy ? "dot-ok" : "dot-warn"}`;
  $("#healthText").textContent = st.healthy ? "All systems ready" : "Needs attention";

  const v = st.version || {};
  $("#brandVersion").textContent = v.commit && v.commit !== "unknown" ? v.commit : "local";
  $("#aboutVersion").textContent = v.commit
    ? `${v.commit}${v.date ? ` · ${v.date}` : ""}${v.subject ? ` · ${v.subject}` : ""}`
    : "unknown";
  $("#aboutRoot").textContent = st.root || "—";
  $("#aboutPlatform").textContent = st.platform || "—";

  if (showToast) toast(st.healthy ? "Everything is running." : "Some services need attention.",
    st.healthy ? "ok" : "bad");
}

$("#btnRefresh").addEventListener("click", () => refreshStatus(true));

$("#btnStart").addEventListener("click", async () => {
  await act("start_stack", [], "Starting rememory… (Docker can take a minute)");
  refreshStatus();
});

$("#btnStop").addEventListener("click", async () => {
  await act("stop_stack", [], "Stopping rememory…");
  refreshStatus();
});

$("#btnSyncAll").addEventListener("click", async () => {
  await act("sync_all", [], "Syncing all projects…");
  refreshStatus();
});

$("#btnBackup").addEventListener("click", () => act("backup_now", [], "Backing up memories…"));

$("#btnRepair").addEventListener("click", async () => {
  if (!confirm("Repair re-verifies and rebuilds every component.\n\n"
    + "Your memories, index and settings are NOT touched, and a safety backup "
    + "is taken first. It opens in a new window and may take a few minutes.\n\nContinue?")) return;
  await act("repair", [], "Launching repair…");
});

/* ───────────────────────────── updates ───────────────────────────── */

async function checkUpdate(explicit = false) {
  if (!api) return;
  const info = await api.check_update();
  if (!info) return;
  if (info.available) {
    $("#updateTitle").textContent =
      `Update available — ${info.count} new ${info.count === 1 ? "version" : "versions"}`;
    $("#updateSubtitle").textContent = info.blocked
      ? "Local changes present, so it won't apply automatically."
      : `${info.commit || ""} ${info.subject || ""}`.trim();
    $("#btnApplyUpdate").disabled = !!info.blocked;
    $("#updateBanner").classList.remove("hidden");
    document.body.classList.add("has-banner");
  } else if (explicit) {
    toast(info.message || "You're on the latest version.", "ok");
  }
}

$("#btnCheckUpdate").addEventListener("click", () => checkUpdate(true));

$("#btnDismissUpdate").addEventListener("click", () => {
  $("#updateBanner").classList.add("hidden");
  document.body.classList.remove("has-banner");
});

$("#btnApplyUpdate").addEventListener("click", async () => {
  busy(true, "Updating and restarting rememory…");
  await act("apply_update", []);
  // The process restarts itself; keep the overlay up so the window looks
  // deliberately busy rather than frozen during the handover.
});

/* ───────────────────────────── projects ───────────────────────────── */

async function loadProjects() {
  const box = $("#projectList");
  const res = await api?.projects();
  if (!res || !res.ok) {
    box.innerHTML = `<div class="empty">${esc(res?.message || "Could not load projects.")}</div>`;
    return;
  }
  const list = res.projects || [];
  $("#navProjectCount").textContent = list.length || "";
  if (!list.length) {
    box.innerHTML = `<div class="empty">No projects yet. Add one to give your assistant a knowledge base.</div>`;
    return;
  }
  box.innerHTML = list.map((p) => `
    <div class="item">
      <div class="item-head">
        <span class="dot ${p.exists ? "dot-ok" : "dot-bad"}"></span>
        <div style="min-width:0">
          <div class="item-title">${esc(p.name)}</div>
          <div class="item-sub">${esc(p.root)}${p.exists ? "" : " — folder missing"}</div>
        </div>
        <div class="item-actions">
          <button class="btn btn-sm" data-sync="${esc(p.name)}">Sync</button>
          <button class="btn btn-sm btn-ghost" data-reindex="${esc(p.name)}">Re-index</button>
          <button class="btn btn-sm btn-danger" data-remove="${esc(p.name)}">Remove</button>
        </div>
      </div>
      ${p.description ? `<div class="item-sub" style="margin-top:8px">${esc(p.description)}</div>` : ""}
      <div class="item-meta">
        <span><b>${(p.code || 0).toLocaleString()}</b> code</span>
        <span><b>${(p.docs || 0).toLocaleString()}</b> docs</span>
        <span><b>${(p.memory || 0).toLocaleString()}</b> memories</span>
        <span>indexed <b>${fmtWhen(p.last_indexed)}</b></span>
      </div>
    </div>`).join("");

  box.querySelectorAll("[data-sync]").forEach((b) => b.addEventListener("click", async () => {
    await act("sync_project", [b.dataset.sync], `Syncing ${b.dataset.sync}…`);
    loadProjects();
  }));
  box.querySelectorAll("[data-reindex]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Fully re-index "${b.dataset.reindex}"?\n\nThis rebuilds its code and doc `
      + `chunks from scratch. Stored memories are not affected.`)) return;
    await act("reindex_project", [b.dataset.reindex], `Re-indexing ${b.dataset.reindex}…`);
    loadProjects();
  }));
  box.querySelectorAll("[data-remove]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm(`Remove "${b.dataset.remove}" from rememory?\n\nIts code/doc index is deleted. `
      + `Stored memories are KEPT, and your files are never touched.`)) return;
    await act("remove_project", [b.dataset.remove], "Removing…");
    loadProjects();
    refreshStatus();
  }));
}

$("#btnShowAdd").addEventListener("click", () => {
  $("#addForm").classList.toggle("hidden");
  if (!$("#addForm").classList.contains("hidden")) $("#inpName").focus();
});
$("#btnCancelAdd").addEventListener("click", () => $("#addForm").classList.add("hidden"));

$("#btnPick").addEventListener("click", async () => {
  const res = await api?.pick_folder();
  if (res?.ok && res.path) {
    $("#inpRoot").value = res.path;
    if (!$("#inpName").value) {
      // Suggest a slug from the folder name -- one less thing to type.
      const leaf = res.path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || "";
      $("#inpName").value = leaf.toLowerCase().replace(/[^a-z0-9-]+/g, "-")
        .replace(/^-+|-+$/g, "").slice(0, 50);
    }
  }
});

$("#btnCreate").addEventListener("click", async () => {
  const name = $("#inpName").value.trim();
  const root = $("#inpRoot").value.trim();
  const desc = $("#inpDesc").value.trim();
  if (!root) { toast("Choose the project folder first.", "bad"); return; }
  const res = await act("add_project", [name, root, desc],
    "Creating the knowledge base and indexing…");
  if (res?.ok) {
    $("#addForm").classList.add("hidden");
    ["#inpName", "#inpRoot", "#inpDesc"].forEach((s) => ($(s).value = ""));
    loadProjects();
    refreshStatus();
  }
});

/* ───────────────────────────── memories ───────────────────────────── */

let searchDebounce = null;

async function loadMemories(query = "") {
  const box = $("#memoryList");
  box.innerHTML = `<div class="empty">Loading…</div>`;
  const res = await api?.memories(query, 30);
  if (!res || !res.ok) {
    box.innerHTML = `<div class="empty">${esc(res?.message || "Could not load memories.")}</div>`;
    return;
  }
  const list = res.memories || [];
  if (!list.length) {
    box.innerHTML = `<div class="empty">${query
      ? "Nothing matched that search."
      : "No memories yet. Your assistant stores decisions and findings here as you work."}</div>`;
    return;
  }
  box.innerHTML = list.map((m, i) => {
    const type = m.memory_type || "note";
    return `
    <div class="item">
      <div class="item-head clickable" data-toggle="${i}">
        <span class="badge badge-${esc(type)}">${esc(type)}</span>
        <div style="min-width:0">
          <div class="item-title">${esc(m.title || "(untitled)")}</div>
          <div class="item-sub">${esc(m.project || "")} · ${fmtWhen(m.created_at)}${
            m.score != null ? ` · match ${m.score}` : ""}</div>
        </div>
        <div class="item-actions">
          <button class="btn btn-sm btn-danger" data-del="${esc(m.id)}">Delete</button>
        </div>
      </div>
      ${(m.tags || []).length
        ? `<div class="item-meta">${m.tags.map((t) => `<span class="badge">${esc(t)}</span>`).join("")}</div>`
        : ""}
      <div class="memory-body hidden" id="mem-${i}">${esc(m.content || "")}</div>
    </div>`;
  }).join("");

  box.querySelectorAll("[data-toggle]").forEach((head) => head.addEventListener("click", (ev) => {
    if (ev.target.closest("button")) return;   // don't expand when Delete is hit
    $(`#mem-${head.dataset.toggle}`).classList.toggle("hidden");
  }));
  box.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this memory permanently?\n\nThis cannot be undone.")) return;
    await act("delete_memory", [b.dataset.del], "Deleting…");
    loadMemories($("#inpSearch").value.trim());
    refreshStatus();
  }));
}

$("#inpSearch").addEventListener("input", (ev) => {
  clearTimeout(searchDebounce);
  const q = ev.target.value.trim();
  searchDebounce = setTimeout(() => loadMemories(q), 380);
});

/* ───────────────────────────── connect ───────────────────────────── */

async function loadConnect() {
  const res = await api?.connection_config();
  if (!res?.ok) return;
  $("#cliSnippet").textContent = res.cli;
  $("#jsonSnippet").textContent = res.json;
}

$$("[data-copy]").forEach((btn) => btn.addEventListener("click", async () => {
  const text = $(`#${btn.dataset.copy}`).textContent;
  try {
    await navigator.clipboard.writeText(text);
    toast("Copied to clipboard.", "ok");
  } catch {
    // Clipboard API can be blocked in a webview; fall back to a selection.
    const range = document.createRange();
    range.selectNodeContents($(`#${btn.dataset.copy}`));
    const sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(range);
    toast("Select-and-copy the highlighted text (Ctrl+C).", "info");
  }
}));

/* ───────────────────────────── settings ───────────────────────────── */

async function loadSettings() {
  const res = await api?.settings();
  if (!res?.ok) return;
  $("#swAutostart").checked = !!res.settings.launch_at_login;
  $("#swAutoUpdate").checked = res.settings.auto_update !== false;
}

$("#swAutostart").addEventListener("change", async (ev) => {
  const res = await act("set_setting", ["launch_at_login", ev.target.checked]);
  if (!res?.ok) ev.target.checked = !ev.target.checked;   // revert on failure
});
$("#swAutoUpdate").addEventListener("change", (ev) =>
  act("set_setting", ["auto_update", ev.target.checked]));

$$("[data-open]").forEach((b) => b.addEventListener("click", () => act("open_path", [b.dataset.open])));
$("#btnRepo").addEventListener("click", () =>
  act("open_url", ["https://github.com/umesh-404/rememory"]));
$("#btnRestart").addEventListener("click", async () => {
  busy(true, "Restarting rememory…");
  await act("restart_app", []);
});

/* ───────────────────────────── boot ───────────────────────────── */

function boot() {
  api = window.pywebview.api;
  refreshStatus();
  loadProjects();
  checkUpdate(false);
  statusTimer = setInterval(refreshStatus, 6000);
}

if (window.pywebview?.api) boot();
else window.addEventListener("pywebviewready", boot);

// Follow the OS theme so the app matches the rest of the desktop.
const applyTheme = (e) =>
  document.documentElement.dataset.theme = e.matches ? "light" : "dark";
const mq = window.matchMedia("(prefers-color-scheme: light)");
applyTheme(mq);
mq.addEventListener("change", applyTheme);
