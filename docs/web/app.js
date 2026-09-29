async function loadJson(path, fallback) {
  try {
    const r = await fetch(path, { cache: "no-store" });
    if (!r.ok) throw new Error(String(r.status));
    return await r.json();
  } catch {
    return fallback;
  }
}

function sshClass(status) {
  if (!status || status === "unchecked") return "mute";
  if (status === "ok") return "ok";
  if (status === "ok_socks" || status === "ok_proxy") return "warn";
  return "bad";
}

function sshLabel(status) {
  const map = {
    ok: "ok",
    ok_socks: "ok (socks)",
    ok_proxy: "ok (proxy)",
    auth_fail: "auth fail",
    tcp_closed: "tcp closed",
    no_secret: "no secret",
    fail: "fail",
    unchecked: "—",
  };
  return map[status] || status || "—";
}

function cdnLabel(s) {
  const v = (s.cdn || "").toLowerCase();
  if (v) return s.cdn;
  if (s.domain_modes) return String(s.domain_modes).replace(/[\[\]\s]/g, "") || "—";
  return "—";
}

function cdnClass(label) {
  const v = String(label || "").toLowerCase();
  if (v === "cdn") return "ok";
  if (v.includes("≠") || v === "mixed") return "warn";
  if (v === "direct") return "mute";
  return "mute";
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function domainRows(s) {
  if (Array.isArray(s.domain_rows) && s.domain_rows.length) return s.domain_rows;
  return (s.domains || []).map((d) => ({ domain: d, mode: "—" }));
}

function domainsFull(s) {
  const rows = domainRows(s);
  if (!rows.length) return s.subdomain ? `${esc(s.subdomain)}.*` : "—";
  return rows.map((r) => {
    const mode = r.mode || "—";
    const cls = mode === "cdn" ? "ok" : mode === "direct" ? "mute" : "warn";
    return `<div class="dom-line"><code>${esc(r.domain)}</code> <span class="ssh-badge ${cls}">${esc(mode)}</span></div>`;
  }).join("");
}

function renderFleet(fleet, ssh) {
  const byId = Object.fromEntries((ssh.servers || []).map((x) => [x.id, x]));
  const tbody = document.querySelector("#fleet-table tbody");
  const rows = (fleet.servers || []).slice().sort((a, b) => a.id.localeCompare(b.id, "en", { numeric: true }));

  const html = rows.map((s) => {
    const st = byId[s.id] || {};
    const sshStatus = st.ssh || "unchecked";
    const cls = sshClass(sshStatus);
    const cdn = cdnLabel(s);
    const note = (s.status_note || st.detail || "").replace(/</g, "&lt;");
    const cfTitle = (s.cf_detail || "").replace(/"/g, "&quot;");
    const domQ = domainRows(s).map((r) => `${r.domain} ${r.mode}`).join(" ");
    return `<tr data-q="${[s.id, s.host, s.location, s.state, sshStatus, cdn, domQ, note].join(" ").toLowerCase()}">
      <td><strong>${esc(s.id)}</strong></td>
      <td><code>${esc(s.host || "")}</code></td>
      <td>${esc(s.location || "—")}</td>
      <td>${esc(s.state || "—")}</td>
      <td><span class="ssh-badge ${cls}" title="${esc(st.detail || "")}">${esc(sshLabel(sshStatus))}</span></td>
      <td><span class="ssh-badge ${cdnClass(cdn)}" title="${cfTitle}">${esc(cdn)}</span></td>
      <td>${esc(s.ssh_port || 22)}</td>
      <td>${esc(s.auth_method || "—")}</td>
      <td>${domainsFull(s)}</td>
      <td>${note}</td>
    </tr>`;
  }).join("");
  tbody.innerHTML = html;

  const counts = { ok: 0, proxy: 0, fail: 0, unchecked: 0 };
  const cdnCounts = { cdn: 0, direct: 0, mixed: 0, other: 0 };
  rows.forEach((s) => {
    const st = (byId[s.id] || {}).ssh || "unchecked";
    if (st === "ok") counts.ok++;
    else if (st === "ok_socks" || st === "ok_proxy") counts.proxy++;
    else if (st === "unchecked") counts.unchecked++;
    else counts.fail++;
    const c = cdnLabel(s);
    if (c === "cdn") cdnCounts.cdn++;
    else if (c === "direct") cdnCounts.direct++;
    else if (c === "mixed") cdnCounts.mixed++;
    else cdnCounts.other++;
  });
  document.getElementById("fleet-summary").textContent =
    `Всего ${rows.length}: SSH ok ${counts.ok}, через proxy ${counts.proxy}, fail ${counts.fail}, не проверено ${counts.unchecked}. CDN ${cdnCounts.cdn}, direct ${cdnCounts.direct}, mixed ${cdnCounts.mixed}, н/д ${cdnCounts.other}.`;
  document.getElementById("meta-count").textContent = `серверов: ${rows.length}`;
  document.getElementById("meta-updated").textContent =
    `данные: ${fleet.updated || "—"}${ssh.checked_at ? " · ssh: " + ssh.checked_at : ""}`;

  const filter = document.getElementById("filter");
  filter.addEventListener("input", () => {
    const q = filter.value.trim().toLowerCase();
    tbody.querySelectorAll("tr").forEach((tr) => {
      tr.style.display = !q || tr.dataset.q.includes(q) ? "" : "none";
    });
  });
}

function renderChangelog(data) {
  const el = document.getElementById("changelog-list");
  const items = (data.entries || []).slice().sort((a, b) => (a.date < b.date ? 1 : -1));
  el.innerHTML = items.map((e) => `
    <article class="tl-item">
      <time>${e.date}</time>
      <strong>${e.title}</strong>
      <div>${e.body}</div>
    </article>`).join("") || "<p class='lede'>Пока пусто.</p>";
}

function setupNav() {
  const links = [...document.querySelectorAll("#nav a")];
  const sections = links.map((a) => document.querySelector(a.getAttribute("href"))).filter(Boolean);
  const io = new IntersectionObserver((entries) => {
    entries.forEach((en) => {
      if (!en.isIntersecting) return;
      const id = "#" + en.target.id;
      links.forEach((a) => a.classList.toggle("active", a.getAttribute("href") === id));
    });
  }, { rootMargin: "-40% 0px -55% 0px", threshold: 0 });
  sections.forEach((s) => io.observe(s));
}

(async function main() {
  setupNav();
  const [fleet, ssh, changelog] = await Promise.all([
    loadJson("data/fleet.json", { servers: [], updated: null }),
    loadJson("data/ssh-status.json", { servers: [], checked_at: null }),
    loadJson("data/changelog.json", { entries: [] }),
  ]);
  renderFleet(fleet, ssh);
  renderChangelog(changelog);
})();
