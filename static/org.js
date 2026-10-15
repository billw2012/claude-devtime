// Organisation picker, shared by both pages. One account can be signed into several organisations; the server shows one
// at a time. The choice is remembered in localStorage and sent as org=<id> with every API call (see withOrg).
const ORG = { id: "", list: [] };
try { ORG.id = localStorage.getItem("devtime-org") || ""; } catch (e) { /* private mode */ }

function withOrg(url) {
  if (!ORG.id || !url.startsWith("/api/") || /^\/api\/(orgs|version)/.test(url)) return url;
  return url + (url.includes("?") ? "&" : "?") + "org=" + encodeURIComponent(ORG.id);
}

// Fills <select id="org"> (hidden unless there is more than one org); changing it reloads the page.
async function initOrgs() {
  const sel = document.getElementById("org");
  try {
    const r = await fetch("/api/orgs", { cache: "no-store" });
    ORG.list = r.ok ? await r.json() : [];
  } catch (e) { ORG.list = []; }
  if (!ORG.list.some((o) => o.id === ORG.id)) ORG.id = ORG.list.length ? ORG.list[0].id : "";  // the most recently active
  if (!sel || ORG.list.length < 2) return;
  sel.replaceChildren(...ORG.list.map((o) => {
    const opt = document.createElement("option");
    opt.value = o.id;
    opt.textContent = (o.name || o.id) + ` · ${o.sessions} sessions`;
    return opt;
  }));
  sel.value = ORG.id;
  sel.hidden = false;
  sel.addEventListener("change", () => {
    try { localStorage.setItem("devtime-org", sel.value); } catch (e) { /* private mode */ }
    location.reload();
  });
}
