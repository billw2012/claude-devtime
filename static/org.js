// Organisation pill, shared by both pages. Each organisation has its own devtime server (see docs/reference.md), so
// the page only shows which one this is: the name from /api/version in <span id="orgName">.
async function initOrgs() {
  const label = document.getElementById("orgName");
  if (!label) return;
  try {
    const r = await fetch("/api/version", { cache: "no-store" });
    const v = r.ok ? await r.json() : {};
    const name = v.orgName || (v.org ? "org " + String(v.org).slice(0, 8) : "");
    if (name) {
      label.textContent = name;
      label.title = "Organisation" + (v.org ? " " + v.org : "");
      label.hidden = false;
    }
    if (v.scanning) waitForScan();
  } catch (e) { /* server unreachable: the page reports it */ }
}

// A new server scans the whole transcript history first (minutes on a long one): say so, and reload when it is done.
function waitForScan() {
  const note = document.createElement("div");
  note.textContent = "First scan of your Claude Code history in progress - the dashboard fills in when it is done.";
  note.setAttribute("role", "status");
  note.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:1000;padding:10px 16px;text-align:center;" +
    "font:14px system-ui,sans-serif;background:#2b5fb4;color:#fff";
  document.body.appendChild(note);
  const poll = async () => {
    try {
      const r = await fetch("/api/version", { cache: "no-store" });
      if (r.ok && !(await r.json()).scanning) return location.reload();
    } catch (e) { /* restarting: keep waiting */ }
    setTimeout(poll, 3000);
  };
  setTimeout(poll, 3000);
}
