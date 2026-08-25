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
    el.textContent += evt.data;
    el.scrollTop = el.scrollHeight;
  };
  ws.onerror = () => {
    el.textContent += "\n[stream error]\n";
  };
  return ws;
}
