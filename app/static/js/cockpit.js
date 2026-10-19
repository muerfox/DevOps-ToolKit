// Small shared helpers used across cockpit pages.

function confirmAction(message) {
  return window.confirm(message || "Are you sure?");
}

// Used by the SSH server create/settings forms to relabel the secret field
// ("Password" vs "Private key (PEM)") when the auth-type select changes.
function updateSecretLabel(selectEl, labelId) {
  const label = document.getElementById(labelId);
  if (label) label.textContent = selectEl.value === "key" ? "Private key (PEM)" : "Password";
}

// Ansible forms: toggles between "single server" / "group" / "all servers"
// target selects. Both the server and group <select> share name="target_value"
// -- the disabled one is simply omitted from the form submission, so only
// the active choice is ever sent.
function toggleAnsibleTarget(prefix) {
  const type = document.getElementById(prefix + "_target_type").value;
  const serverField = document.getElementById(prefix + "_server_field");
  const groupField = document.getElementById(prefix + "_group_field");
  const serverSelect = document.getElementById(prefix + "_target_value_server");
  const groupSelect = document.getElementById(prefix + "_target_value_group");
  if (!serverField || !groupField || !serverSelect || !groupSelect) return;
  serverField.style.display = type === "server" ? "block" : "none";
  groupField.style.display = type === "group" ? "block" : "none";
  serverSelect.disabled = type !== "server";
  groupSelect.disabled = type !== "group";
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-ansible-target-prefix]").forEach((el) => {
    toggleAnsibleTarget(el.dataset.ansibleTargetPrefix);
  });
  // Reveals the server/remote-path fields on load if the page arrived with
  // ?target=server&deploy_server_id=... pre-selected (the Deploy Center's
  // "+ Deploy a repo to this server" link).
  if (document.getElementById("target")) toggleGitTarget();
});

// Git repo create form: toggles between HTTPS (username/token) and SSH
// (private key/passphrase) field groups.
function toggleGitAuthFields() {
  const type = document.getElementById("auth_type").value;
  const httpsFields = document.getElementById("https_fields");
  const sshFields = document.getElementById("ssh_key_fields");
  if (!httpsFields || !sshFields) return;
  httpsFields.style.display = type === "https" ? "flex" : "none";
  sshFields.style.display = type === "ssh_key" ? "flex" : "none";
}

// Git repo create form: toggles the server/remote-path fields when
// choosing between a local clone and deploying the repo to a server.
function toggleGitTarget() {
  const target = document.getElementById("target").value;
  const serverFields = document.getElementById("server_target_fields");
  if (!serverFields) return;
  serverFields.style.display = target === "server" ? "flex" : "none";
}

// Off-canvas sidebar for narrow (phone-width) screens: hamburger button in
// the topbar toggles it, tapping the backdrop or a nav link closes it.
document.addEventListener("DOMContentLoaded", () => {
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.querySelector(".sidebar-backdrop");
  const toggle = document.querySelector(".sidebar-toggle");
  if (!sidebar || !backdrop || !toggle) return;

  const closeSidebar = () => {
    sidebar.classList.remove("open");
    backdrop.classList.remove("open");
  };
  const openSidebar = () => {
    sidebar.classList.add("open");
    backdrop.classList.add("open");
  };

  toggle.addEventListener("click", () => {
    sidebar.classList.contains("open") ? closeSidebar() : openSidebar();
  });
  backdrop.addEventListener("click", closeSidebar);
  sidebar.querySelectorAll("a").forEach((a) => a.addEventListener("click", closeSidebar));
});

// Attaches a WebSocket log stream to a <pre> element and auto-scrolls it.
function attachLogStream(path, targetId) {
  const el = document.getElementById(targetId);
  if (!el) return null;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${window.location.host}${path}`);
  ws.onmessage = (evt) => {
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.textContent += evt.data;
    if (atBottom) el.scrollTop = el.scrollHeight;
  };
  ws.onerror = () => {
    el.textContent += "\n[stream error]\n";
  };
  return ws;
}

async function fetchJSON(url, opts) {
  const res = await fetch(url, Object.assign({ headers: { Accept: "application/json" } }, opts || {}));
  if (res.redirected && res.url.includes("/login")) {
    window.location = "/login";
    throw new Error("session expired");
  }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function formatBytes(n) {
  if (n === null || n === undefined || isNaN(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  seconds = Math.floor(seconds);
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
  const d = Math.floor(seconds / 86400), h = Math.floor((seconds % 86400) / 3600), m = Math.floor((seconds % 3600) / 60);
  return d ? `${d}d ${h}h` : `${h}h ${String(m).padStart(2, "0")}m`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Renders an inline SVG sparkline (values 0..max) into `el`.
function sparkline(el, values, max, color) {
  if (!el) return;
  const w = 120, h = 28;
  max = max || Math.max(1, ...values);
  const step = values.length > 1 ? w / (values.length - 1) : w;
  const pts = values.map((v, i) => `${(i * step).toFixed(1)},${(h - (Math.min(v, max) / max) * (h - 2) - 1).toFixed(1)}`).join(" ");
  el.innerHTML = `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">` +
    `<polyline fill="none" stroke="${color || "var(--accent)"}" stroke-width="1.5" points="${pts}"/></svg>`;
}

function copyText(text, btn) {
  const done = () => {
    if (!btn) return;
    const old = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = old), 1200);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done);
  } else {
    const ta = document.createElement("textarea");
    ta.value = text;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
    done();
  }
}

// --- toasts ------------------------------------------------------------

function toast(message, level) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = `toast ${level || "info"}`;
  el.dataset.level = level || "info";
  el.innerHTML = `<span>${escapeHtml(message)}</span><button type="button" aria-label="Dismiss">&times;</button>`;
  el.querySelector("button").onclick = () => el.remove();
  box.appendChild(el);
  scheduleToastDismiss(el);
}

function scheduleToastDismiss(el) {
  if (el.dataset.level === "error") return; // errors stay until dismissed
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 300);
  }, 5000);
}

document.querySelectorAll("#toasts .toast").forEach(scheduleToastDismiss);

// --- mobile sidebar ------------------------------------------------------------

function toggleSidebar(force) {
  const open = force === undefined ? !document.body.classList.contains("sidebar-open") : force;
  document.body.classList.toggle("sidebar-open", open);
}

// --- command palette (Ctrl/Cmd+K) ------------------------------------------------------------

const Palette = {
  items: null,
  selected: 0,
  async load() {
    if (this.items) return this.items;
    try {
      this.items = (await fetchJSON("/api/search-index")).items;
    } catch (e) {
      this.items = [];
    }
    return this.items;
  },
  async open() {
    const el = document.getElementById("palette");
    if (!el) return;
    el.hidden = false;
    const input = document.getElementById("palette-input");
    input.value = "";
    input.focus();
    await this.load();
    this.render("");
  },
  close() {
    const el = document.getElementById("palette");
    if (el) el.hidden = true;
  },
  score(item, q) {
    if (!q) return 1;
    const hay = `${item.title} ${item.kind} ${item.subtitle || ""} ${item.keywords || ""}`.toLowerCase();
    const title = item.title.toLowerCase();
    if (title.startsWith(q)) return 3;
    if (title.includes(q)) return 2;
    return q.split(/\s+/).every((t) => hay.includes(t)) ? 1 : 0;
  },
  render(q) {
    q = q.trim().toLowerCase();
    const list = document.getElementById("palette-results");
    const matches = (this.items || [])
      .map((it) => [this.score(it, q), it])
      .filter(([s]) => s > 0)
      .sort((a, b) => b[0] - a[0])
      .slice(0, 12)
      .map(([, it]) => it);
    this.matches = matches;
    this.selected = 0;
    list.innerHTML = matches.length
      ? matches.map((it, i) =>
          `<li class="${i === 0 ? "sel" : ""}" data-i="${i}"><span class="kind">${escapeHtml(it.kind)}</span>` +
          `<span class="title">${escapeHtml(it.title)}</span>` +
          (it.subtitle ? `<span class="muted sub">${escapeHtml(it.subtitle)}</span>` : "") + `</li>`).join("")
      : `<li class="empty muted">No matches</li>`;
    list.querySelectorAll("li[data-i]").forEach((li) => {
      li.onmouseenter = () => this.highlight(+li.dataset.i);
      li.onclick = () => this.go(+li.dataset.i);
    });
  },
  highlight(i) {
    const lis = document.querySelectorAll("#palette-results li[data-i]");
    if (!lis.length) return;
    this.selected = (i + lis.length) % lis.length;
    lis.forEach((li, j) => li.classList.toggle("sel", j === this.selected));
    lis[this.selected].scrollIntoView({ block: "nearest" });
  },
  go(i) {
    const it = (this.matches || [])[i];
    if (it) window.location = it.url;
  },
};

document.addEventListener("keydown", (e) => {
  const pal = document.getElementById("palette");
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    pal && pal.hidden ? Palette.open() : Palette.close();
    return;
  }
  if (!pal || pal.hidden) return;
  if (e.key === "Escape") Palette.close();
  else if (e.key === "ArrowDown") { e.preventDefault(); Palette.highlight(Palette.selected + 1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); Palette.highlight(Palette.selected - 1); }
  else if (e.key === "Enter") { e.preventDefault(); Palette.go(Palette.selected); }
});

document.addEventListener("input", (e) => {
  if (e.target && e.target.id === "palette-input") Palette.render(e.target.value);
});

document.addEventListener("click", (e) => {
  const pal = document.getElementById("palette");
  if (pal && !pal.hidden && e.target === pal) Palette.close();
});
