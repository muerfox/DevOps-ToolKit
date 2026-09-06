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
    el.textContent += evt.data;
    el.scrollTop = el.scrollHeight;
  };
  ws.onerror = () => {
    el.textContent += "\n[stream error]\n";
  };
  return ws;
}
