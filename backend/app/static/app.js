"use strict";
// Lookout UI. All text goes through textContent / DOM nodes (never parsed as HTML), so API data cannot inject markup.
const app = document.getElementById("app");
const bar = document.getElementById("bar");
let csrf = "", me = null, timers = [];

const PERMISSIONS = {
  "admin.users": "Manage users", "admin.groups": "Manage groups", "admin.settings": "Change settings",
  "admin.backup": "Back up and restore", "admin.llm": "Configure Sensei", "admin.audit": "Read the audit log",
  "inventory.oracle.create": "Add Oracle databases", "inventory.ssh.create": "Add SSH hosts", "inventory.api.create": "Add APIs",
  "view.db_table.create": "Create database views", "view.ssh_health.create": "Create SSH health views",
  "view.ssh.custom": "Run custom SSH commands", "view.api_response.create": "Create API views",
  "view.combined.create": "Create combined views", "view.trend.create": "Create trend views",
  "dashboard.create": "Create dashboards", "link.create": "Add links", "llm.use": "Ask Sensei",
};

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k != null && k !== false) el.append(k.nodeType ? k : document.createTextNode(String(k)));
  return el;
}
const show = (...nodes) => { app.replaceChildren(...nodes); };
const clearTimers = () => { timers.forEach(clearInterval); timers = []; };
const when = v => v ? new Date(v).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "never";
const tags = s => s.split(",").map(x => x.trim()).filter(Boolean);

function toast(msg, bad) {
  const t = h("div", { class: "toast" + (bad ? " bad" : "") }, msg);
  document.getElementById("toasts").append(t);
  setTimeout(() => t.remove(), 4500);
}

async function api(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  if (method !== "GET") headers["X-CSRF-Token"] = csrf;
  const r = await fetch(path, { method, headers, credentials: "same-origin", body: body ? JSON.stringify(body) : undefined });
  const data = r.status === 204 ? null : await r.json().catch(() => null);
  if (!r.ok) {
    const e = (data && data.error) || { code: "ERROR", message: "Request failed" };
    let msg = e.message;
    const errs = e.details && e.details.errors;
    if (errs && errs.length) msg += ": " + errs.map(x => `${x.loc.join(".")} ${x.msg}`).join("; ");
    throw Object.assign(new Error(msg), { code: e.code, status: r.status, details: e.details });
  }
  return data;
}

// ---- building blocks ---------------------------------------------------------------------------------
function field(label, input, hint) {
  return h("label", {}, label, input, hint ? h("span", { class: "hint" }, hint) : null);
}
const text = (o = {}) => h("input", { type: "text", autocomplete: "off", ...o });
const num = (v, o = {}) => h("input", { type: "number", value: v, ...o });
const select = (opts, o = {}) => h("select", o, opts.map(([v, l]) => h("option", { value: v }, l)));

// A modal form. `build` returns the body nodes; `onSubmit` may throw, and its message is shown inside the form.
function modal(title, body, { submit = "Save", danger = false, onSubmit, cancel = "Cancel" } = {}) {
  const err = h("p", { class: "err", role: "alert" });
  const dlg = h("dialog", {});
  const go = h("button", { type: "submit", class: danger ? "danger" : "" }, submit);
  const form = h("form", { onsubmit: async e => {
    e.preventDefault();
    go.disabled = true; err.textContent = "";
    try { if (onSubmit) await onSubmit(); dlg.close(); } catch (x) { err.textContent = x.message; go.disabled = false; }
  } }, h("h2", {}, title), body, err,
  h("div", { class: "foot" }, h("button", { type: "button", class: "ghost", onclick: () => dlg.close() }, cancel), onSubmit ? go : null));
  dlg.append(form);
  dlg.addEventListener("close", () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}
const confirmBox = (title, msg, label, fn) => modal(title, h("p", {}, msg), { submit: label, danger: true, onSubmit: fn });

function pageHead(title, sub, ...actions) {
  return h("div", { class: "page-head" }, h("div", {}, h("h1", {}, title), sub ? h("p", {}, sub) : null), h("div", { class: "actions" }, actions));
}
function empty(title, msg, ...actions) {
  return h("div", { class: "panel empty" }, h("h2", {}, title), h("p", {}, msg), h("div", { class: "actions", style: null }, actions));
}
const statusDot = s => h("span", { class: "dot " + (s || "unknown") }, { up: "Up", down: "Down" }[s] || "Not checked");

// ---- rendering a ResultSet ---------------------------------------------------------------------------
function renderResult(res, renderer) {
  if (!res) return h("p", { class: "mute" }, "No data yet. Choose Run now to fetch it.");
  if (renderer === "raw_json") return h("pre", {}, JSON.stringify(res, null, 2));
  const shown = res.rows.slice(0, 500);  // chart renderers are not built yet: they fall back to the table
  const note = [];
  if (res.meta.cached) note.push(res.meta.stale ? "cached (stale)" : "cached");
  if (res.meta.generated_at) note.push(when(res.meta.generated_at));
  if (res.meta.truncated || res.rows.length > shown.length) note.push("truncated");
  const failed = (res.meta.sources || []).filter(s => s.error).map(s => `${s.name}: ${s.error}`);
  return h("div", {},
    res.rows.length ? h("div", { class: "tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, res.columns.map(c => h("th", {}, c.name)))),
      h("tbody", {}, shown.map(r => h("tr", {}, r.map(v => h("td", {}, v == null ? "" : typeof v === "object" ? JSON.stringify(v) : v)))))))
      : h("p", { class: "mute" }, "The query ran and returned no rows."),
    h("p", { class: "mute" }, `${res.rows.length} rows` + (note.length ? ", " + note.join(", ") : "")),
    failed.length ? h("p", { class: "err" }, "These servers failed: " + failed.join("; ")) : null);
}

function place(el, g) {  // 12-column grid placement; CSSOM assignment is allowed under CSP
  el.style.gridColumn = `${g.x + 1} / span ${g.w}`;
  el.style.gridRow = `${g.y + 1} / span ${g.h}`;
}

// ---- sign in -----------------------------------------------------------------------------------------
function loginPage(msg) {
  const user = text({ id: "u", autocomplete: "username", required: true, autofocus: true });
  const pw = h("input", { id: "p", type: "password", autocomplete: "current-password", required: true });
  const err = h("p", { class: "err", role: "alert" }, msg || "");
  const ticks = h("div", { class: "ticks", "aria-hidden": "true" },
    Array.from({ length: 72 }, (_, i) => h("span", { class: "tick" + (i % 23 === 9 ? " down" : i % 7 === 3 ? "" : " up") })));
  show(h("div", { class: "login-wrap" },
    h("div", { class: "login-art" },
      h("a", { class: "brand", href: "#/" }, h("span", { class: "lamp" }), "Lookout"),
      h("div", {}, h("p", {}, "Know which servers are failing, and why."), ticks)),
    h("div", { class: "login-form" }, h("form", { onsubmit: async e => {
      e.preventDefault();
      try { start(await api("POST", "/api/auth/login", { login: user.value, password: pw.value })); }
      catch (x) { err.textContent = x.status === 429 ? "Too many attempts. Wait a minute and try again." : "That login or password is not right."; }
    } }, h("h1", {}, "Sign in"), h("p", { class: "mute" }, "Use the account your administrator created for you."),
    h("br"), field("Login", user), field("Password", pw), h("button", { type: "submit" }, "Sign in"), err))));
}

function changePasswordPage() {
  const cur = h("input", { type: "password", autocomplete: "current-password", required: true });
  const nw = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: "12" });
  const err = h("p", { class: "err", role: "alert" });
  show(h("div", { class: "login-wrap" }, h("div", { class: "login-art" }, h("a", { class: "brand", href: "#/" }, h("span", { class: "lamp" }), "Lookout"), h("p", {}, "Set your own password to continue.")),
    h("div", { class: "login-form" }, h("form", { onsubmit: async e => {
      e.preventDefault();
      try { await api("POST", "/api/auth/change-password", { current_password: cur.value, new_password: nw.value });
            me.must_change_pw = false; route(); }
      catch (x) { err.textContent = x.message; }
    } }, h("h1", {}, "Choose a new password"), h("p", { class: "mute" }, "Use at least 12 characters."), h("br"),
    field("Temporary password", cur), field("New password", nw), h("button", { type: "submit" }, "Change password"), err))));
}

// ---- overview ----------------------------------------------------------------------------------------
async function homePage() {
  const [sum, heat, top, act] = await Promise.all(["summary", "heatmap", "top-errors", "activity"].map(p => api("GET", "/api/home/" + p)));
  const r = sum.resources, up = r.up || 0, down = r.down || 0, unk = r.unknown || 0, total = up + down + unk;
  if (!total) {
    return show(pageHead("Welcome to Lookout", "Nothing is being watched yet. Three steps get your first error counts on screen."),
      h("div", { class: "panel empty" }, h("h2", {}, "Add a server to begin"), h("p", {}, "Lookout connects to your Oracle databases, SSH hosts and APIs, then shows what is failing."),
        h("a", { class: "btn", href: "#/servers" }, "Add a server"),
        h("ol", { class: "steps" },
          h("li", {}, h("b", {}, "Add servers"), h("span", {}, "Register each Oracle database, SSH host or API once.")),
          h("li", {}, h("b", {}, "Create a view"), h("span", {}, "A view is a saved query, such as today's error counts.")),
          h("li", {}, h("b", {}, "Build a dashboard"), h("span", {}, "Put views side by side and share them.")))));
  }
  const verdict = down ? `${down} of ${total} server${total === 1 ? " is" : "s are"} down` : `All ${total} server${total === 1 ? " is" : "s are"} reachable`;
  show(pageHead("Overview", null),
    h("p", { class: "verdict" + (down ? " bad" : "") }, verdict),
    h("div", { class: "facts" },
      h("div", {}, h("b", {}, up), h("span", {}, "up")), h("div", {}, h("b", {}, down), h("span", {}, "down")),
      h("div", {}, h("b", {}, unk), h("span", {}, "not checked")), h("div", {}, h("b", {}, sum.failed_runs_24h), h("span", {}, "failed runs in 24 hours")),
      h("div", {}, h("b", {}, sum.dashboards), h("span", {}, "dashboards"))),
    h("div", { class: "watch" }, Object.entries(heat).map(([tag, items]) => h("div", { class: "watch-group" }, h("span", {}, tag),
      h("div", { class: "ticks" }, items.map(i => h("button", { type: "button", class: "tick " + i.status,
        title: `${i.name}: ${{ up: "up", down: "down" }[i.status] || "not checked"}${i.last_error ? " (" + i.last_error + ")" : ""}`,
        "aria-label": `${i.name}, ${i.status}`, onclick: () => { location.hash = "#/servers"; } })))))),
    h("div", { class: "legend" }, h("span", { class: "up" }, "Up"), h("span", { class: "down" }, "Down"), h("span", {}, "Not checked")),
    h("div", { class: "grid2" },
      h("section", { class: "panel" }, h("h2", {}, "Top errors today"),
        top.errors.length ? h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Error"), h("th", { class: "r" }, "Count"), h("th", { class: "r" }, "Servers"))),
          h("tbody", {}, top.errors.map(e => h("tr", {}, h("td", {}, e.error), h("td", { class: "r num" }, e.count), h("td", { class: "r num" }, e.servers))))))
          : h("div", { class: "body" }, h("p", { class: "mute" }, "No error counts yet. Run a Daily errors view to fill this in."))),
      h("section", { class: "panel" }, h("h2", {}, "Recent activity"),
        act.length ? h("ul", { class: "feed" }, act.map(a => h("li", {}, `${a.action.replace(/[._]/g, " ")} ${a.target_type}`, h("time", {}, when(a.at)))))
          : h("div", { class: "body" }, h("p", { class: "mute" }, "Nothing has happened yet.")))));
}

// ---- servers -----------------------------------------------------------------------------------------
const TYPE_LABEL = { oracle: "Oracle database", ssh: "SSH host", api: "API" };

function serverDialog(done) {
  const kind = select([["oracle", "Oracle database"], ["ssh", "SSH host"], ["api", "API"]]);
  const name = text({ required: true, maxlength: "128" }), tagIn = text({ placeholder: "postpaid, prod" });
  const llm = h("input", { type: "checkbox" });
  const box = h("div", {});
  let get = () => ({});
  const draw = () => {
    const t = kind.value;
    if (t === "oracle") {
      const host = text({ required: true }), port = num(1521), svc = text({ required: true }), usr = text({ required: true }), pw = h("input", { type: "password", required: true, autocomplete: "new-password" });
      box.replaceChildren(h("div", { class: "row2" }, field("Host", host), field("Port", port)), field("Service name", svc),
        h("div", { class: "row2" }, field("Database user", usr), field("Password", pw, "Stored encrypted. You cannot read it back.")));
      get = () => ({ config: { host: host.value.trim(), port: +port.value, service_name: svc.value.trim(), username: usr.value.trim() }, secret: { password: pw.value } });
    } else if (t === "ssh") {
      const host = text({ required: true }), port = num(22), usr = text({ required: true }), auth = select([["password", "Password"], ["key", "Private key"]]);
      const cred = h("div", {});
      let secret = () => ({});
      const drawCred = () => {
        if (auth.value === "key") { const k = h("textarea", { required: true, spellcheck: "false" }); cred.replaceChildren(field("Private key", k, "Paste the key. Stored encrypted.")); secret = () => ({ private_key: k.value }); }
        else { const p = h("input", { type: "password", required: true, autocomplete: "new-password" }); cred.replaceChildren(field("Password", p)); secret = () => ({ password: p.value }); }
      };
      auth.addEventListener("change", drawCred); drawCred();
      box.replaceChildren(h("div", { class: "row2" }, field("Host", host), field("Port", port)), h("div", { class: "row2" }, field("User", usr), field("Sign in with", auth)), cred);
      get = () => ({ config: { host: host.value.trim(), port: +port.value, username: usr.value.trim(), auth: auth.value }, secret: secret() });
    } else {
      const url = text({ required: true, placeholder: "https://api.example.com" }), auth = select([["none", "No sign-in"], ["basic", "Username and password"], ["bearer", "Bearer token"], ["api_key", "API key"]]);
      const cred = h("div", {});
      let secret = () => null;
      const drawCred = () => {
        const a = auth.value;
        if (a === "basic") { const u = text({ required: true }), p = h("input", { type: "password", required: true }); cred.replaceChildren(h("div", { class: "row2" }, field("Username", u), field("Password", p))); secret = () => ({ username: u.value, password: p.value }); }
        else if (a === "none") { cred.replaceChildren(); secret = () => null; }
        else { const k = h("input", { type: "password", required: true }); cred.replaceChildren(field(a === "bearer" ? "Token" : "API key", k)); secret = () => (a === "bearer" ? { token: k.value } : { api_key: k.value }); }
      };
      auth.addEventListener("change", drawCred); drawCred();
      box.replaceChildren(field("Base URL", url), field("Authentication", auth), cred);
      get = () => ({ config: { kind: "rest", base_url: url.value.trim(), auth: auth.value, allowed_methods: ["GET"] }, secret: secret() });
    }
  };
  kind.addEventListener("change", draw); draw();
  modal("Add a server", [field("Type", kind), field("Name", name, "A short name people will recognise, such as POSTPAID-DB1."), box, field("Tags", tagIn, "Separate with commas. Tags group servers on the overview."),
    h("label", { class: "check" }, llm, "Allow Sensei to use this server")], {
    submit: "Add server",
    onSubmit: async () => { await api("POST", "/api/resources", { type: kind.value, name: name.value.trim(), tags: tags(tagIn.value), llm_allowed: llm.checked, ...get() }); toast("Server added"); done(); },
  });
}

async function serversPage() {
  const all = await api("GET", "/api/resources");
  const q = h("input", { class: "search", type: "search", placeholder: "Search servers", "aria-label": "Search servers" });
  const type = select([["", "All types"], ["oracle", "Oracle"], ["ssh", "SSH"], ["api", "API"]], { class: "select", "aria-label": "Filter by type" });
  const body = h("tbody", {});
  const where = r => r.config ? (r.config.host ? `${r.config.host}${r.config.port ? ":" + r.config.port : ""}` : r.config.base_url || "") : "";
  const draw = () => {
    const rows = all.filter(r => (!type.value || r.type === type.value) && (!q.value || r.name.toLowerCase().includes(q.value.toLowerCase()) || r.tags.some(t => t.includes(q.value.toLowerCase()))));
    body.replaceChildren(...rows.map(r => {
      const test = h("button", { type: "button", class: "ghost small" }, "Test");
      test.addEventListener("click", async () => {
        test.disabled = true; test.textContent = "Testing…";
        try { const t = await api("POST", `/api/resources/${r.id}/test`); toast(t.ok ? `${r.name} is reachable` : `${r.name}: ${t.error || "failed"}`, !t.ok); }
        catch (x) { toast(x.message, true); }
        serversPage();
      });
      const del = h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete server", `Delete ${r.name}? Views that use it will lose this source.`, "Delete server",
        async () => { await api("DELETE", "/api/resources/" + r.id); toast("Server deleted"); serversPage(); }) }, "Delete");
      const can = r.level === "manage" || r.level === "edit";
      return h("tr", {}, h("td", {}, h("b", {}, r.name), r.last_error ? h("div", { class: "mute" }, r.last_error) : null), h("td", {}, TYPE_LABEL[r.type] || r.type),
        h("td", { class: "mute" }, where(r)), h("td", {}, r.tags.map(t => h("span", { class: "tag" }, t))), h("td", {}, statusDot(r.status), h("div", { class: "mute" }, r.last_checked_at ? when(r.last_checked_at) : "")),
        h("td", { class: "r" }, h("div", { class: "actions" }, test, can ? del : null)));
    }));
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: "6", class: "mute" }, "No servers match.")));
  };
  q.addEventListener("input", draw); type.addEventListener("change", draw);
  show(pageHead("Servers", "Every database, host and API Lookout can reach.", q, type, h("button", { type: "button", onclick: () => serverDialog(serversPage) }, "Add server")),
    all.length ? h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Name", "Type", "Address", "Tags", "Status", ""].map(x => h("th", {}, x)))), body)))
      : empty("No servers yet", "Add your first Oracle database, SSH host or API. Passwords are stored encrypted and never shown again.",
        h("button", { type: "button", onclick: () => serverDialog(serversPage) }, "Add server")));
  draw();
}

// ---- views -------------------------------------------------------------------------------------------
function resultDialog(v, res) {
  modal(v.name, renderResult(res, v.renderer), { cancel: "Close" });
}

function viewDialog(done) {
  Promise.all([api("GET", "/api/view-templates"), api("GET", "/api/resources?type=oracle")]).then(([templates, servers]) => {
    if (!servers.length) return toast("Add an Oracle server first. Views run against servers.", true);
    const tpl = select([...templates.map((t, i) => [String(i), t.name + ": " + t.description]), ["custom", "Custom SQL query"]]);
    const name = text({ required: true }), sql = h("textarea", { spellcheck: "false", placeholder: "SELECT …" }), sqlField = field("SQL", sql, "Read-only. Only SELECT statements are allowed.");
    const checks = servers.map(s => ({ s, c: h("input", { type: "checkbox" }) }));
    const sched = select([["", "Run only when asked"], ["*/10 * * * *", "Every 10 minutes"], ["0 * * * *", "Every hour"], ["0 6 * * *", "Every day at 06:00"]]);
    const ttl = num(300, { min: 0, max: 86400 });
    const sync = () => { sqlField.hidden = tpl.value !== "custom"; if (tpl.value !== "custom") name.value = name.value || templates[+tpl.value].name; };
    tpl.addEventListener("change", sync); sync();
    const all = h("button", { type: "button", class: "ghost small", onclick: () => checks.forEach(x => { x.c.checked = true; }) }, "Select all");
    modal("New view", [field("Start from", tpl), field("Name", name), sqlField,
      h("label", {}, "Run on these servers ", all), h("div", { class: "perms" }, checks.map(x => h("label", { class: "check" }, x.c, x.s.name))),
      h("br"), h("div", { class: "row2" }, field("Refresh", sched), field("Keep result for (seconds)", ttl))], {
      submit: "Create view",
      onSubmit: async () => {
        const ids = checks.filter(x => x.c.checked).map(x => x.s.id);
        if (!ids.length) throw new Error("Pick at least one server.");
        const base = tpl.value === "custom" ? { type: "db_table", description: "", config: { mode: "sql", sql: sql.value } } : templates[+tpl.value];
        await api("POST", "/api/views", { type: base.type, name: name.value.trim(), description: base.description, config: base.config, source_resource_ids: ids,
          schedule_cron: sched.value || null, cache_ttl_s: +ttl.value });
        toast("View created"); done();
      },
    });
  }).catch(x => toast(x.message, true));
}

async function viewsPage() {
  const list = await api("GET", "/api/views");
  const rows = list.map(v => {
    const run = h("button", { type: "button", class: "small" }, "Run");
    run.addEventListener("click", async () => {
      run.disabled = true; run.textContent = "Running…";
      try { resultDialog(v, await api("POST", `/api/views/${v.id}/run?fresh=true`, { params: {} })); } catch (x) { toast(x.message, true); }
      run.disabled = false; run.textContent = "Run";
    });
    const last = h("button", { type: "button", class: "ghost small", onclick: async () => {
      try { resultDialog(v, await api("GET", `/api/views/${v.id}/result`)); } catch (x) { toast(x.status === 404 ? "This view has not run yet." : x.message, x.status !== 404); } } }, "Last result");
    const del = h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete view", `Delete ${v.name}?`, "Delete view",
      async () => { await api("DELETE", "/api/views/" + v.id); toast("View deleted"); viewsPage(); }) }, "Delete");
    return h("tr", {}, h("td", {}, h("b", {}, v.name), v.description ? h("div", { class: "mute" }, v.description) : null), h("td", {}, v.type.replace("_", " ")),
      h("td", { class: "num" }, v.source_resource_ids.length), h("td", { class: "mute" }, v.schedule_cron || "On request"),
      h("td", { class: "r" }, h("div", { class: "actions" }, run, last, v.level === "manage" ? del : null)));
  });
  show(pageHead("Views", "A view is a saved query that runs on one or more servers.", h("button", { type: "button", onclick: () => viewDialog(viewsPage) }, "New view")),
    list.length ? h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Name", "Type", "Servers", "Refresh", ""].map(x => h("th", {}, x)))), h("tbody", {}, rows))))
      : empty("No views yet", "Create a Daily errors view to count today's failed requests on every Oracle server.", h("button", { type: "button", onclick: () => viewDialog(viewsPage) }, "New view")));
}

// ---- dashboards --------------------------------------------------------------------------------------
async function dashboardsPage() {
  const list = await api("GET", "/api/dashboards");
  const create = () => api("GET", "/api/views").then(views => {
    if (!views.length) return toast("Create a view first. Dashboards show views.", true);
    const name = text({ required: true }), desc = text();
    const checks = views.map(v => ({ v, c: h("input", { type: "checkbox" }) }));
    modal("New dashboard", [field("Name", name), field("Description", desc), h("label", {}, "Show these views"),
      h("div", { class: "perms" }, checks.map(x => h("label", { class: "check" }, x.c, x.v.name)))], {
      submit: "Create dashboard",
      onSubmit: async () => {
        const picked = checks.filter(x => x.c.checked).map(x => x.v);
        const widgets = picked.map((v, i) => ({ view_id: v.id, title: v.name, renderer: v.renderer || "table", grid: { x: (i % 2) * 6, y: Math.floor(i / 2) * 5, w: 6, h: 5 } }));
        const d = await api("POST", "/api/dashboards", { name: name.value.trim(), description: desc.value, widgets });
        location.hash = "#/dashboards/" + d.id;
      },
    });
  }).catch(x => toast(x.message, true));
  show(pageHead("Dashboards", "Views laid out side by side.", h("button", { type: "button", onclick: create }, "New dashboard")),
    list.length ? h("div", { class: "cards" }, list.map(d => h("a", { class: "card", href: "#/dashboards/" + d.id }, h("h3", {}, d.name), h("p", {}, d.description || "No description"))))
      : empty("No dashboards yet", "A dashboard puts several views on one page that you can share.", h("button", { type: "button", onclick: create }, "New dashboard")));
}

async function dashboardPage(id) {
  const d = await api("GET", "/api/dashboards/" + id);
  const grid = h("div", { class: "grid" });
  for (const w of d.widgets) {
    const body = h("div", {}, h("p", { class: "mute" }, "Loading…"));
    const head = h("div", { class: "widget-head" }, h("h3", {}, w.no_access ? "No access" : (w.title || "Widget")));
    const box = h("section", { class: "widget" }, head, body);
    place(box, w.grid);
    grid.append(box);
    if (w.no_access) { body.replaceChildren(h("p", { class: "mute" }, "You do not have access to this data.")); continue; }
    const load = async run => {
      try { body.replaceChildren(renderResult(await api("GET", `/api/dashboards/${id}/widgets/${w.id}/data${run ? "?run=1" : ""}`), w.renderer)); }
      catch (x) { body.replaceChildren(h("p", { class: x.status === 404 ? "mute" : "err" }, x.status === 404 ? "No result yet. Choose Run now." : x.message)); }
    };
    head.append(h("button", { type: "button", class: "ghost small", onclick: () => load(true) }, "Run now"));
    load(false);
    if (w.refresh_s >= 30) timers.push(setInterval(() => load(false), w.refresh_s * 1000));
  }
  const manage = d.level === "manage";
  const share = () => api("POST", `/api/dashboards/${id}/share-link`, {}).then(s => {
    const link = location.origin + s.path;
    modal("Share link", [h("p", {}, "Anyone with this link can see the last stored results, read-only, until ", h("b", {}, when(s.expires_at)), ". It is shown once."), h("div", { class: "secret" }, link)],
      { cancel: "Close" });
  }).catch(x => toast(x.message, true));
  show(pageHead(d.name, d.description || null, h("a", { class: "btn ghost", href: "#/dashboards" }, "All dashboards"),
    manage ? h("button", { type: "button", class: "ghost", onclick: share }, "Share") : null,
    manage ? h("button", { type: "button", class: "danger", onclick: () => confirmBox("Delete dashboard", `Delete ${d.name}? The views stay.`, "Delete dashboard",
      async () => { await api("DELETE", "/api/dashboards/" + id); location.hash = "#/dashboards"; }) }, "Delete") : null),
    d.widgets.length ? grid : empty("This dashboard is empty", "Widgets are added when a dashboard is created."));
}

// ---- links -------------------------------------------------------------------------------------------
async function linksPage() {
  const tiles = await api("GET", "/api/links");
  const copy = async (id, fld, btn) => {
    try { const s = await api("POST", `/api/links/${id}/reveal`); await navigator.clipboard.writeText(s[fld]); btn.textContent = "Copied"; }
    catch (x) { btn.textContent = "Failed"; }
    setTimeout(() => { btn.textContent = "Copy " + fld; }, 2000);
  };
  const open = async (t, panel) => {
    const r = await api("POST", `/api/links/${t.id}/launch`);
    panel.replaceChildren();
    if (r.credential_hint.needs_credential) {
      const u = text({ placeholder: "username" }), p = h("input", { type: "password", placeholder: "password" });
      panel.append(h("p", { class: "mute" }, "Save your login for this site. It is stored encrypted."), u, h("br"), p, h("br"),
        h("button", { type: "button", onclick: async () => { await api("PUT", `/api/links/${t.id}/my-credential`, { username: u.value, password: p.value }); linksPage(); } }, "Save login"));
      return;
    }
    window.open(r.url, "_blank", "noopener,noreferrer");
    if (r.credential_hint.has_credential) {
      const bu = h("button", { type: "button", class: "ghost small" }, "Copy username"), bp = h("button", { type: "button", class: "ghost small" }, "Copy password");
      bu.addEventListener("click", () => copy(t.id, "username", bu)); bp.addEventListener("click", () => copy(t.id, "password", bp));
      panel.append(bu, " ", bp);
    }
  };
  const add = () => {
    const name = text({ required: true }), url = h("input", { type: "url", required: true, placeholder: "https://" }), tg = text();
    const mode = select([["none", "No saved login"], ["shared", "One shared login for everyone"], ["per_user", "Each person saves their own"]]);
    const cred = h("div", {}); let secret = () => undefined;
    const draw = () => {
      if (mode.value === "shared") { const u = text({ required: true }), p = h("input", { type: "password", required: true }); cred.replaceChildren(h("div", { class: "row2" }, field("Username", u), field("Password", p))); secret = () => ({ username: u.value, password: p.value }); }
      else { cred.replaceChildren(); secret = () => undefined; }
    };
    mode.addEventListener("change", draw);
    modal("Add a link", [field("Name", name), field("Address", url), field("Tags", tg, "Separate with commas."), field("Login", mode), cred], {
      submit: "Add link",
      onSubmit: async () => { await api("POST", "/api/links", { name: name.value.trim(), url: url.value.trim(), tags: tags(tg.value), credential_mode: mode.value, credential: secret() }); toast("Link added"); linksPage(); },
    });
  };
  show(pageHead("Links", "Other panels and tools, one click away.", h("button", { type: "button", onclick: add }, "Add link")),
    tiles.length ? h("div", { class: "cards" }, tiles.map(t => {
      const panel = h("div", {});
      return h("div", { class: "card" }, h("h3", {}, t.name), h("p", {}, t.tags.map(x => h("span", { class: "tag" }, x))),
        h("div", { class: "actions" }, h("button", { type: "button", onclick: () => open(t, panel).catch(x => panel.replaceChildren(h("p", { class: "err" }, x.message))) }, "Open"),
          t.can_manage ? h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete link", `Delete ${t.name}?`, "Delete link", async () => { await api("DELETE", "/api/links/" + t.id); linksPage(); }) }, "Delete") : null), panel);
    })) : empty("No links yet", "Add the other panels your team uses, with saved logins if you like.", h("button", { type: "button", onclick: add }, "Add link")));
}

// ---- users (admin) -----------------------------------------------------------------------------------
function tempPasswordDialog(login, pw) {
  modal("Temporary password", [h("p", {}, `Give this to ${login}. They must change it at first sign-in. It is shown once.`), h("div", { class: "secret" }, pw)], { cancel: "Close" });
}

async function usersPage() {
  const users = await api("GET", "/api/admin/users");
  const add = () => {
    const login = text({ required: true, pattern: "[A-Za-z0-9._@\\-]+" }), nm = text(), adm = h("input", { type: "checkbox" });
    modal("Add a user", [field("Login", login, "Letters, digits and . _ @ -"), field("Display name", nm), h("label", { class: "check" }, adm, "Administrator (can do everything)")], {
      submit: "Create user",
      onSubmit: async () => { const u = await api("POST", "/api/admin/users", { login: login.value.trim(), display_name: nm.value, is_admin: adm.checked }); usersPage(); setTimeout(() => tempPasswordDialog(u.login, u.temp_password), 0); },
    });
  };
  const perms = async u => {
    const cur = new Set((await api("GET", `/api/admin/users/${u.id}/permissions`)).permissions.map(p => p.permission));
    const boxes = Object.entries(PERMISSIONS).map(([k, l]) => ({ k, c: h("input", { type: "checkbox", checked: cur.has(k) }), l }));
    modal(`What ${u.display_name || u.login} can do`, [u.is_admin ? h("p", { class: "mute" }, "Administrators can already do everything.") : null,
      h("div", { class: "perms" }, boxes.map(b => h("label", { class: "check" }, b.c, b.l)))], {
      submit: "Save permissions",
      onSubmit: async () => { const keep = [...cur].filter(k => !(k in PERMISSIONS)); // keep scoped grants this screen does not edit
        await api("PUT", `/api/admin/users/${u.id}/permissions`, { permissions: [...boxes.filter(b => b.c.checked).map(b => b.k), ...keep].map(permission => ({ permission })) }); toast("Permissions saved"); },
    });
  };
  const rows = users.map(u => h("tr", {}, h("td", {}, h("b", {}, u.display_name || u.login), h("div", { class: "mute" }, u.login)),
    h("td", {}, u.is_admin ? "Administrator" : "User"), h("td", {}, h("span", { class: "dot " + (u.is_active ? "up" : "unknown") }, u.is_active ? (u.locked ? "Locked" : "Active") : "Deactivated")),
    h("td", { class: "mute" }, u.last_login ? when(u.last_login) : "Never"),
    h("td", { class: "r" }, h("div", { class: "actions" },
      h("button", { type: "button", class: "ghost small", onclick: () => perms(u).catch(x => toast(x.message, true)) }, "Permissions"),
      h("button", { type: "button", class: "ghost small", onclick: () => confirmBox("Reset password", `Create a new temporary password for ${u.login}? Their sessions end now.`, "Reset password",
        async () => { const r = await api("POST", `/api/admin/users/${u.id}/reset-password`); setTimeout(() => tempPasswordDialog(u.login, r.temp_password), 0); }) }, "Reset password"),
      u.id === me.id ? null : h("button", { type: "button", class: "ghost small", onclick: async () => { try { await api("PATCH", `/api/admin/users/${u.id}`, { is_active: !u.is_active }); usersPage(); } catch (x) { toast(x.message, true); } } }, u.is_active ? "Deactivate" : "Activate")))));
  show(pageHead("Users", "Who can sign in, and what they can do.", h("button", { type: "button", onclick: add }, "Add user")),
    h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Person", "Role", "Status", "Last sign-in", ""].map(x => h("th", {}, x)))), h("tbody", {}, rows)))));
}

let senseiConv = null;

async function senseiPage() {
  const log = h("div", { class: "chat-log", "aria-live": "polite" });
  const input = h("textarea", { rows: 2, placeholder: "Ask about your servers, e.g. \"Summarise today's errors\"", maxlength: 4000 });
  const send = h("button", { type: "submit" }, "Ask");
  const bubble = (who, ...kids) => { const b = h("div", { class: "msg " + who }, ...kids); log.append(b); log.scrollTop = log.scrollHeight; return b; };
  const sourcesLine = list => h("p", { class: "mute src" }, "Used: " + list.map(s => s.name).join(", "));

  async function ask(textValue) {
    if (!senseiConv) senseiConv = (await api("POST", "/api/llm/conversations")).id;
    bubble("user", textValue);
    const status = bubble("bot mute", "Thinking\u2026");
    const r = await fetch(`/api/llm/conversations/${senseiConv}/messages`, { method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf }, body: JSON.stringify({ text: textValue }) });
    if (!r.ok) { const e = await r.json().catch(() => null); status.remove(); throw new Error((e && e.error && e.error.message) || "Request failed"); }
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "", answer = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const block = buf.slice(0, i); buf = buf.slice(i + 2);
        const m = /^event: (\w+)\ndata: (.*)$/s.exec(block);
        if (!m) continue;
        const data = JSON.parse(m[2]);
        if (m[1] === "tool_call") status.textContent = "Checking " + data.name.replace(/_/g, " ") + "\u2026";
        else if (m[1] === "token") { status.remove(); answer = bubble("bot", data.text); }
        else if (m[1] === "sources" && answer) { if (data.length) answer.append(sourcesLine(data)); }
        else if (m[1] === "error") { status.remove(); throw new Error(data.message); }
      }
    }
  }

  const form = h("form", { class: "chat-form", onsubmit: async e => {
    e.preventDefault();
    const t = input.value.trim();
    if (!t) return;
    input.value = ""; send.disabled = true;
    try { await ask(t); } catch (x) { toast(x.message, true); } finally { send.disabled = false; input.focus(); }
  } }, input, send);
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });

  const actions = [h("button", { type: "button", class: "ghost", onclick: () => { senseiConv = null; log.replaceChildren(); } }, "New chat")];
  if (me.is_admin) actions.push(h("button", { type: "button", class: "ghost", onclick: () => senseiSettings().catch(x => toast(x.message, true)) }, "Configure"));
  show(pageHead("Sensei", "Ask questions about your servers in plain language. Sensei can only read views you can already use.", ...actions),
    h("div", { class: "panel chat" }, log, form));
  if (!senseiConv) bubble("bot mute", "Ask a question to begin. Sensei shows which views it used for each answer.");
}

async function senseiSettings() {
  const c = await api("GET", "/api/admin/llm");
  const groups = await api("GET", "/api/admin/groups").catch(() => []);
  const on = h("input", { type: "checkbox" }); on.checked = c.enabled;
  const url = text({ value: c.base_url }), model = text({ value: c.model });
  const key = h("input", { type: "password", autocomplete: "new-password", placeholder: c.has_api_key ? "Saved. Leave blank to keep." : "Optional" });
  const mode = select([["native", "Native tool calls"], ["json", "JSON fallback"]]); mode.value = c.tool_mode;
  const prompt = h("textarea", {}, c.system_prompt);
  const rate = num(c.rate_limit_per_10min, { min: 1 }), calls = num(c.max_tool_calls, { min: 1, max: 20 });
  const rows = num(c.max_rows, { min: 1, max: 200 }), chars = num(c.max_chars, { min: 500, max: 50000 });
  const picks = groups.map(g => { const cb = h("input", { type: "checkbox", value: g.id }); cb.checked = c.enabled_group_ids.includes(g.id); return [g, cb]; });
  const result = h("p", { class: "mute" });
  const test = h("button", { type: "button", class: "ghost small", onclick: async () => {
    result.textContent = "Testing\u2026";
    try { const r = await api("POST", "/api/admin/llm/test"); result.textContent = r.ok ? `Connected. Tool calls ${r.tool_calls_supported ? "work" : "were not used: try the JSON fallback"}.` : "Failed: " + r.error; }
    catch (x) { result.textContent = x.message; }
  } }, "Test saved settings");
  modal("Configure Sensei", h("div", {},
    h("label", { class: "check" }, on, "Sensei is on"),
    field("Model endpoint (OpenAI-compatible)", url), field("Model", model), field("API key", key),
    field("Tool calling", mode), field("Extra instructions", prompt),
    field("Messages per user per 10 minutes", rate), field("Tool calls per question", calls),
    field("Rows sent to the model", rows), field("Characters per tool result", chars),
    h("fieldset", {}, h("legend", {}, "Groups allowed to use Sensei (admins always can)"),
      ...picks.map(([g, cb]) => h("label", { class: "check" }, cb, g.name))),
    test, result), {
    onSubmit: async () => {
      const body = { enabled: on.checked, base_url: url.value.trim(), model: model.value.trim(), temperature: c.temperature,
        max_tokens: c.max_tokens, timeout_s: c.timeout_s, system_prompt: prompt.value, tool_mode: mode.value,
        enabled_group_ids: picks.filter(([, cb]) => cb.checked).map(([g]) => g.id),
        rate_limit_per_10min: +rate.value, max_tool_calls: +calls.value, max_rows: +rows.value, max_chars: +chars.value };
      if (key.value) body.api_key = key.value;
      await api("PUT", "/api/admin/llm", body);
      toast("Sensei settings saved");
    } });
}

async function sharedPage(token) {
  const d = await api("GET", "/api/shared/" + encodeURIComponent(token));
  const grid = h("div", { class: "grid" });
  for (const w of d.widgets) {
    const box = h("section", { class: "widget" }, h("div", { class: "widget-head" }, h("h3", {}, w.title || "Widget")), renderResult(w.result, w.renderer));
    place(box, w.grid);
    grid.append(box);
  }
  show(pageHead(d.name, "Shared, read-only. Showing the last stored results."), grid);
}

// ---- routing / startup -------------------------------------------------------------------------------
const PAGES = { "": homePage, servers: serversPage, views: viewsPage, dashboards: dashboardsPage, links: linksPage, users: usersPage, sensei: senseiPage };

function drawNav(section) {
  const items = [["", "Overview"], ["servers", "Servers"], ["views", "Views"], ["dashboards", "Dashboards"], ["links", "Links"]];
  const nodes = items.map(([k, l]) => h("a", { href: "#/" + k, "aria-current": k === section ? "page" : null }, l));
  nodes.push(h("span", { class: "sep" }, "Assistant"), h("a", { href: "#/sensei", "aria-current": section === "sensei" ? "page" : null }, "Sensei"));
  if (me.is_admin) nodes.push(h("span", { class: "sep" }, "Admin"), h("a", { href: "#/users", "aria-current": section === "users" ? "page" : null }, "Users"));
  document.getElementById("nav").replaceChildren(...nodes);
}

async function route() {
  clearTimers();
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (parts[0] === "shared" && parts[1]) { bar.hidden = true; return await sharedPage(parts[1]); }
    if (!me) { bar.hidden = true; return loginPage(); }
    if (me.must_change_pw) { bar.hidden = true; return changePasswordPage(); }
    bar.hidden = false;
    drawNav(parts[0]);
    window.scrollTo(0, 0);
    if (parts[0] === "dashboards" && parts[1]) return await dashboardPage(parts[1]);
    return await (PAGES[parts[0]] || homePage)();
  } catch (x) {
    if (x.status === 401) { me = null; bar.hidden = true; return loginPage("Your session ended. Sign in again."); }
    show(pageHead("Something went wrong"), h("p", { class: "err", role: "alert" }, x.status === 404 ? "That page was not found, or the link has expired." : x.status === 403 ? "You do not have permission to see this." : x.message));
  }
}

function start(r) {
  me = r.user; csrf = r.csrf_token;
  document.getElementById("who").textContent = me.display_name || me.login;
  route();
}

document.getElementById("logout").addEventListener("click", async () => {
  try { await api("POST", "/api/auth/logout"); } catch (x) { /* already signed out */ }
  me = null; location.hash = "#/"; route();
});
window.addEventListener("hashchange", route);
api("GET", "/api/auth/me").then(start, () => route());
