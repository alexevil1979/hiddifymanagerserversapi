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

function domainModes(s) {
  if (!s.domain_modes) return "—";
  return s.domain_modes.replace(/[\[\]]/g, "");
}

function renderFleet(fleet, ssh) {
  const byId = Object.fromEntries((ssh.servers || []).map((x) => [x.id, x]));
  const tbody = document.querySelector("#fleet-table tbody");
  const rows = (fleet.servers || []).slice().sort((a, b) => a.id.localeCompare(b.id, "en", { numeric: true }));

  const html = rows.map((s) => {
    const st = byId[s.id] || {};
    const sshStatus = st.ssh || "unchecked";
    const cls = sshClass(sshStatus);
    const note = (s.status_note || st.detail || "").replace(/</g, "&lt;");
    return `<tr data-q="${[s.id, s.host, s.location, s.state, sshStatus, note].join(" ").toLowerCase()}">
      <td><strong>${s.id}</strong></td>
      <td><code>${s.host || ""}</code></td>
      <td>${s.location || "—"}</td>
      <td>${s.state || "—"}</td>
      <td><span class="ssh-badge ${cls}" title="${(st.detail || "").replace(/"/g, "&quot;")}">${sshLabel(sshStatus)}</span></td>
      <td>${s.ssh_port || 22}</td>
      <td>${s.auth_method || "—"}</td>
      <td>${domainModes(s)}</td>
      <td>${note}</td>
    </tr>`;
  }).join("");
  tbody.innerHTML = html;

  const counts = { ok: 0, proxy: 0, fail: 0, unchecked: 0 };
  rows.forEach((s) => {
    const st = (byId[s.id] || {}).ssh || "unchecked";
    if (st === "ok") counts.ok++;
    else if (st === "ok_socks" || st === "ok_proxy") counts.proxy++;
    else if (st === "unchecked") counts.unchecked++;
    else counts.fail++;
  });
  document.getElementById("fleet-summary").textContent =
    `Всего ${rows.length}: SSH ok ${counts.ok}, через proxy ${counts.proxy}, fail ${counts.fail}, не проверено ${counts.unchecked}.`;
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
