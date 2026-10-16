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
  } catch (e) { /* server unreachable: the page reports it */ }
}
