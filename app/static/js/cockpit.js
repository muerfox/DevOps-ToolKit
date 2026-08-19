// Small shared helpers used across cockpit pages.

function confirmAction(message) {
  return window.confirm(message || "Are you sure?");
}

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
