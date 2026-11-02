"use strict";
// Lookout UI. All text goes through textContent / DOM nodes (never parsed as HTML), so API data cannot inject markup.
const app = document.getElementById("app");
const bar = document.getElementById("bar");
let csrf = "", me = null, timers = [];

const PERMISSIONS = {
  "admin.users": "Manage users", "admin.groups": "Manage groups", "admin.settings": "Change settings",
  "admin.backup": "Back up and restore", "admin.llm": "Configure Sensei", "admin.audit": "Read the audit log",
  "inventory.oracle.create": "Add Oracle databases", "inventory.ssh.create": "Add SSH hosts", "inventory.api.create": "Add APIs", "inventory.ocp.create": "Add OpenShift clusters",
  "view.db_table.create": "Create database views", "view.ssh_health.create": "Create SSH health views",
  "view.ssh.custom": "Run custom SSH commands", "view.api_response.create": "Create API views", "view.ocp_pods.create": "Create pod views",
  "view.trend.create": "Create trend views",
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
const STAT_TYPES = [["ssh", "Servers"], ["oracle", "Databases"], ["api", "APIs"], ["ocp", "OpenShift"]];

function statBlock(title, c, href) {
  const up = c.up || 0, down = c.down || 0, unk = c.unknown || 0, total = up + down + unk;
  return h("a", { class: "stat" + (down ? " bad" : total ? " ok" : ""), href },
    h("h3", {}, title), h("span", { class: "total" }, total),
    h("div", { class: "split" }, h("span", { class: "up" }, h("b", {}, up), " up"), h("span", { class: "down" }, h("b", {}, down), " down"),
      h("span", {}, h("b", {}, unk), " not checked")));
}

async function homePage() {
  const [sum, heat, act] = await Promise.all(["summary", "heatmap", "activity"].map(p => api("GET", "/api/home/" + p)));
  const r = sum.resources, up = r.up || 0, down = r.down || 0, unk = r.unknown || 0, total = up + down + unk;
  if (!total) {
    return show(pageHead("Welcome to Lookout"),
      h("div", { class: "panel empty" }, h("h2", {}, "Add a connection to begin"), h("p", {}, "Lookout connects to your Oracle databases, SSH hosts, APIs and OpenShift clusters, then shows what is failing."),
        h("a", { class: "btn", href: "#/inventory" }, "Open inventory"),
        h("ol", { class: "steps" },
          h("li", {}, h("b", {}, "Add connections"), h("span", {}, "Register each Oracle database, SSH host, API or cluster once.")),
          h("li", {}, h("b", {}, "Create a view"), h("span", {}, "A view is a saved query, such as today's error counts.")),
          h("li", {}, h("b", {}, "Build a dashboard"), h("span", {}, "Put views side by side and share them.")))));
  }
  const bt = sum.by_type || {};
  const verdict = down ? `${down} of ${total} connection${total === 1 ? " is" : "s are"} down` : `All ${total} connection${total === 1 ? " is" : "s are"} reachable`;
  show(pageHead("Overview"),
    h("p", { class: "verdict" + (down ? " bad" : "") }, verdict),
    h("div", { class: "stats" },
      STAT_TYPES.map(([k, label]) => statBlock(label, bt[k] || {}, "#/inventory")),
      h("a", { class: "stat", href: "#/dashboards" }, h("h3", {}, "Dashboards"), h("span", { class: "total" }, sum.dashboards),
        h("div", { class: "split" }, h("span", {}, h("b", {}, sum.views), " views"))),
      h("a", { class: "stat" + (sum.failed_runs_24h ? " bad" : ""), href: "#/views" }, h("h3", {}, "Failed runs"), h("span", { class: "total" }, sum.failed_runs_24h),
        h("div", { class: "split" }, h("span", {}, "last 24 hours")))),
    h("div", { class: "grid2" },
      h("section", { class: "panel" }, h("h2", {}, "Status by tag"),
        h("div", { class: "body" },
          h("div", { class: "watch" }, Object.entries(heat).map(([tag, items]) => h("div", { class: "watch-group" }, h("span", {}, tag),
            h("div", { class: "ticks" }, items.map(i => h("button", { type: "button", class: "tick " + i.status,
              title: `${i.name}: ${{ up: "up", down: "down" }[i.status] || "not checked"}${i.last_error ? " (" + i.last_error + ")" : ""}`,
              "aria-label": `${i.name}, ${i.status}`, onclick: () => { location.hash = "#/inventory"; } })))))),
          h("div", { class: "legend" }, h("span", { class: "up" }, "Up"), h("span", { class: "down" }, "Down"), h("span", {}, "Not checked")))),
      h("section", { class: "panel" }, h("h2", {}, "Recent activity"),
        act.length ? h("ul", { class: "feed" }, act.map(a => h("li", {}, `${a.action.replace(/[._]/g, " ")} ${a.target_type}`, h("time", {}, when(a.at)))))
          : h("div", { class: "body" }, h("p", { class: "mute" }, "Nothing has happened yet.")))));
}

// ---- servers -----------------------------------------------------------------------------------------
const TYPE_LABEL = { oracle: "Oracle database", ssh: "Linux host", api: "API", ocp: "OpenShift" };

function serverDialog(done, ex) {
  const edit = !!ex, c = (ex && ex.config) || {};
  const hdrText = o => Object.entries(o || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  const keep = edit ? "Leave blank to keep the stored value." : undefined;
  const sec = o => (edit && !Object.values(o).some(Boolean) ? undefined : o);  // blank secret on edit = keep the stored one
  const kind = select([["ssh", "Linux host (SSH)"], ["oracle", "Oracle database"], ["api", "API (REST or SOAP)"], ["ocp", "OpenShift cluster"]]);
  if (edit) { kind.value = ex.type; kind.disabled = true; }
  const name = text({ required: true, maxlength: "128", value: edit ? ex.name : "" }), tagIn = text({ placeholder: "postpaid, prod", value: edit ? ex.tags.join(", ") : "" });
  const llm = h("input", { type: "checkbox", checked: edit && ex.llm_allowed });
  const box = h("div", {});
  let get = () => ({});
  const draw = () => {
    const t = kind.value;
    if (t === "oracle") {
      const host = text({ required: true, value: c.host || "" }), port = num(c.port || 1521), svc = text({ required: true, value: c.service_name || "" }), usr = text({ required: true, value: c.username || "" });
      const pw = h("input", { type: "password", required: !edit, autocomplete: "new-password" });
      box.replaceChildren(h("div", { class: "row2" }, field("Host", host), field("Port", port)), field("Service name", svc),
        h("div", { class: "row2" }, field("Database user", usr), field("Password", pw, keep || "Stored encrypted. You cannot read it back.")));
      get = () => ({ config: { ...c, host: host.value.trim(), port: +port.value, service_name: svc.value.trim(), username: usr.value.trim() }, secret: sec({ password: pw.value }) });
    } else if (t === "ssh") {
      const host = text({ required: true, value: c.host || "" }), port = num(c.port || 22), usr = text({ required: true, value: c.username || "" }), auth = select([["password", "Password"], ["key", "Private key"]]);
      if (c.auth === "key" || c.auth === "password") auth.value = c.auth;
      const cred = h("div", {});
      let secret = () => ({});
      const drawCred = () => {
        if (auth.value === "key") { const k = h("textarea", { required: !edit, spellcheck: "false" }); cred.replaceChildren(field("Private key", k, keep || "Paste the key. Stored encrypted.")); secret = () => sec({ private_key: k.value }); }
        else { const p = h("input", { type: "password", required: !edit, autocomplete: "new-password" }); cred.replaceChildren(field("Password", p, keep)); secret = () => sec({ password: p.value }); }
      };
      auth.addEventListener("change", drawCred); drawCred();
      box.replaceChildren(h("div", { class: "row2" }, field("Host", host), field("Port", port)), h("div", { class: "row2" }, field("User", usr), field("Sign in with", auth)), cred);
      get = () => ({ config: { ...c, host: host.value.trim(), port: +port.value, username: usr.value.trim(), auth: auth.value }, secret: secret() });
    } else if (t === "ocp") {
      const url = text({ required: true, placeholder: "https://api.cluster.example:6443", value: c.api_url || "" });
      const tok = h("input", { type: "password", required: !edit, autocomplete: "off" });
      const tls = h("input", { type: "checkbox", checked: c.verify_tls !== false });
      const nsIn = text({ placeholder: "All namespaces", value: (c.namespaces || []).join(", ") });
      box.replaceChildren(field("API URL", url), field("Service-account token", tok, keep || "A read-only service account is enough. Stored encrypted."),
        field("Namespaces", nsIn, "Leave empty for the whole cluster. List them if the token cannot read cluster-wide."),
        h("label", { class: "check" }, tls, "Verify the TLS certificate"));
      get = () => ({ config: { ...c, api_url: url.value.trim(), namespaces: tags(nsIn.value), verify_tls: tls.checked }, secret: sec({ token: tok.value.trim() }) });
    } else {
      const url = text({ required: true, placeholder: "https://api.example.com", value: c.base_url || "" });
      const proto = select([["rest", "REST"], ["soap", "SOAP"]]); proto.value = c.kind || "rest";
      const am = c.allowed_methods || ["GET"];
      const get_ = h("input", { type: "checkbox", checked: am.includes("GET") }), post_ = h("input", { type: "checkbox", checked: am.includes("POST") });
      const hdrs = h("textarea", { spellcheck: "false", placeholder: "Name: value, one per line" }); hdrs.value = hdrText(c.default_headers);
      const auth = select([["none", "No sign-in"], ["basic", "Username and password"], ["bearer", "Bearer token"], ["api_key", "API key"]]);
      if (c.auth) auth.value = c.auth;
      const keyHdr = text({ value: c.api_key_header || "X-Key" });
      const timeout = num(c.timeout_s || 15, { min: 1, max: 120 });
      const tls = h("input", { type: "checkbox", checked: c.verify_tls !== false });
      const cred = h("div", {});
      let secret = () => null;
      const drawCred = () => {
        const a = auth.value;
        if (a === "basic") { const u = text({ required: !edit }), p = h("input", { type: "password", required: !edit }); cred.replaceChildren(h("div", { class: "row2" }, field("Username", u), field("Password", p)), keep ? h("p", { class: "mute" }, keep) : null); secret = () => sec({ username: u.value, password: p.value }); }
        else if (a === "none") { cred.replaceChildren(); secret = () => null; }
        else { const k = h("input", { type: "password", required: !edit }); cred.replaceChildren(a === "api_key" ? field("Header name", keyHdr) : null, field(a === "bearer" ? "Token" : "API key", k, keep)); secret = () => sec(a === "bearer" ? { token: k.value } : { api_key: k.value }); }
      };
      auth.addEventListener("change", drawCred); drawCred();
      box.replaceChildren(field("Base URL", url), h("div", { class: "row2" }, field("Protocol", proto), field("Timeout (seconds)", timeout)),
        h("label", {}, "Allowed methods"), h("div", { class: "row2" }, h("label", { class: "check" }, get_, "GET"), h("label", { class: "check" }, post_, "POST")),
        field("Default headers", hdrs), field("Authentication", auth), cred, h("label", { class: "check" }, tls, "Verify the TLS certificate"));
      get = () => {
        const methods = [get_.checked ? "GET" : null, post_.checked ? "POST" : null].filter(Boolean);
        if (!methods.length) throw new Error("Allow at least one method, GET or POST.");
        const default_headers = {};
        for (const line of hdrs.value.split("\n").map(x => x.trim()).filter(Boolean)) {
          const i = line.indexOf(":");
          if (i < 1) throw new Error(`Header "${line}" must look like Name: value.`);
          default_headers[line.slice(0, i).trim()] = line.slice(i + 1).trim();
        }
        return { config: { ...c, kind: proto.value, base_url: url.value.trim(), allowed_methods: methods, default_headers, auth: auth.value,
          api_key_header: keyHdr.value.trim() || "X-Key", timeout_s: +timeout.value, verify_tls: tls.checked }, secret: secret() };
      };
    }
  };
  kind.addEventListener("change", draw); draw();
  modal(edit ? `Edit ${ex.name}` : "Add a connection", [field("Type", kind), field("Name", name, "A short name people will recognise, such as POSTPAID-DB1."), box, field("Tags", tagIn, "Separate with commas. Tags group connections on the overview."),
    h("label", { class: "check" }, llm, "Allow Sensei to use this connection")], {
    submit: edit ? "Save changes" : "Add connection",
    onSubmit: async () => {
      const body = { name: name.value.trim(), tags: tags(tagIn.value), llm_allowed: llm.checked, ...get() };
      if (edit) await api("PATCH", "/api/resources/" + ex.id, body); else await api("POST", "/api/resources", { type: kind.value, ...body });
      toast(edit ? "Connection updated" : "Connection added"); done();
    },
  });
}

async function inventoryPage() {
  const all = await api("GET", "/api/resources");
  const q = h("input", { class: "search", type: "search", placeholder: "Search servers", "aria-label": "Search servers" });
  const type = select([["", "All types"], ["ssh", "Linux"], ["oracle", "Oracle DB"], ["api", "API"], ["ocp", "OpenShift"]], { class: "select", "aria-label": "Filter by type" });
  const body = h("tbody", {});
  const where = r => r.config ? (r.config.host ? `${r.config.host}${r.config.port ? ":" + r.config.port : ""}` : r.config.base_url || r.config.api_url || "") : "";
  const draw = () => {
    const rows = all.filter(r => (!type.value || r.type === type.value) && (!q.value || r.name.toLowerCase().includes(q.value.toLowerCase()) || r.tags.some(t => t.includes(q.value.toLowerCase()))));
    body.replaceChildren(...rows.map(r => {
      const test = h("button", { type: "button", class: "ghost small" }, "Test");
      test.addEventListener("click", async () => {
        test.disabled = true; test.textContent = "Testing…";
        try { const t = await api("POST", `/api/resources/${r.id}/test`); toast(t.ok ? `${r.name} is reachable` : `${r.name}: ${t.error || "failed"}`, !t.ok); }
        catch (x) { toast(x.message, true); }
        inventoryPage();
      });
      const del = h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete server", `Delete ${r.name}? Views that use it will lose this source.`, "Delete server",
        async () => { await api("DELETE", "/api/resources/" + r.id); toast("Server deleted"); inventoryPage(); }) }, "Delete");
      const can = r.level === "manage" || r.level === "edit";
      const edit = h("button", { type: "button", class: "ghost small", onclick: () => serverDialog(inventoryPage, r) }, "Edit");
      return h("tr", {}, h("td", {}, h("b", {}, r.name), r.last_error ? h("div", { class: "mute" }, r.last_error) : null), h("td", {}, TYPE_LABEL[r.type] || r.type),
        h("td", { class: "mute" }, where(r)), h("td", {}, r.tags.map(t => h("span", { class: "tag" }, t))), h("td", {}, statusDot(r.status), h("div", { class: "mute" }, r.last_checked_at ? when(r.last_checked_at) : "")),
        h("td", { class: "r" }, h("div", { class: "actions" }, test, can ? edit : null, can ? del : null)));
    }));
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: "6", class: "mute" }, "No servers match.")));
  };
  q.addEventListener("input", draw); type.addEventListener("change", draw);
  show(pageHead("Inventory", null, q, type, h("button", { type: "button", onclick: () => serverDialog(inventoryPage) }, "Add connection")),
    all.length ? h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Name", "Type", "Address", "Tags", "Status", ""].map(x => h("th", {}, x)))), body)))
      : empty("No servers yet", "Add your first Oracle database, SSH host or API. Passwords are stored encrypted and never shown again.",
        h("button", { type: "button", onclick: () => serverDialog(inventoryPage) }, "Add server")));
  draw();
}

// ---- views -------------------------------------------------------------------------------------------
const VIEW_SOURCE = { db_table: "oracle", ssh_health: "ssh", api_response: "api", ocp_pods: "ocp" };
const VIEW_LABEL = { db_table: "Database query", ssh_health: "Server health", ocp_pods: "OpenShift pods", api_response: "API", trend: "Trend" };
const SOURCE_WORD = { oracle: "databases", ssh: "servers", ocp: "clusters", api: "APIs" };
const HEALTH_CHECKS = [{ id: "cpu_load" }, { id: "mem", warn: 85, crit: 95 }, { id: "swap", warn: 25, crit: 60 }, { id: "iowait", warn: 10, crit: 30 },
  { id: "disk", args: { mount: "/" }, warn: 80, crit: 90 }, { id: "uptime" }];

function viewDialog(done, ex) {
  const edit = !!ex;
  const type = select([["db_table", "Database query"], ["ssh_health", "Server health"], ["ocp_pods", "OpenShift pods"], ...(edit && !["db_table", "ssh_health", "ocp_pods"].includes(ex.type) ? [[ex.type, VIEW_LABEL[ex.type] || ex.type]] : [])]);
  if (edit) { type.value = ex.type; type.disabled = true; }
  const cfg = (edit && ex.config) || {};
  const name = text({ required: true, value: edit ? ex.name : "" }), desc = text({ value: edit ? ex.description || "" : "" });
  const sqlEdit = !edit || (ex.type === "db_table" && cfg.mode === "sql");
  const sql = h("textarea", { spellcheck: "false", placeholder: "SELECT …" }); sql.value = cfg.sql || "";
  const sqlField = field("SQL", sql, "Read-only. Only SELECT statements are allowed.");
  const ns = text({ placeholder: "All namespaces", value: (cfg.namespaces || []).join(", ") });
  const onlyBad = h("input", { type: "checkbox", checked: !!cfg.only_problems });
  const nsBox = h("div", {}, field("Namespaces", ns, "Leave empty for every namespace the cluster connection allows."), h("label", { class: "check" }, onlyBad, "Show only pods that are not healthy"));
  const sched = select([["", "Run only when asked"], ["*/10 * * * *", "Every 10 minutes"], ["0 * * * *", "Every hour"], ["0 6 * * *", "Every day at 06:00"]]);
  if (edit && ex.schedule_cron) {
    if (![...sched.options].some(o => o.value === ex.schedule_cron)) sched.append(h("option", { value: ex.schedule_cron }, ex.schedule_cron));
    sched.value = ex.schedule_cron;
  }
  const ttl = num(edit ? ex.cache_ttl_s : 300, { min: 0, max: 86400 });
  const llm = h("input", { type: "checkbox", checked: edit && ex.llm_allowed });
  const picks = h("div", {}), chosen = new Set(edit ? ex.source_resource_ids : []);
  let checks = [], kind = null;
  const drawSources = async () => {
    kind = VIEW_SOURCE[type.value] || null;
    sqlField.hidden = type.value !== "db_table" || !sqlEdit;
    nsBox.hidden = type.value !== "ocp_pods";
    checks = [];
    if (!kind) return picks.replaceChildren();
    picks.replaceChildren(h("p", { class: "mute" }, "Loading…"));
    try {
      const servers = await api("GET", "/api/resources?type=" + kind);
      checks = servers.map(s => ({ s, c: h("input", { type: "checkbox", checked: chosen.has(s.id) }) }));
      const all = h("button", { type: "button", class: "ghost small", onclick: () => checks.forEach(x => { x.c.checked = true; }) }, "Select all");
      picks.replaceChildren(h("label", {}, `Run on these ${SOURCE_WORD[kind]} `, all),
        servers.length ? h("div", { class: "perms" }, checks.map(x => h("label", { class: "check" }, x.c, x.s.name))) : h("p", { class: "mute" }, `No ${SOURCE_WORD[kind]} yet. Add them in Inventory first.`), h("br"));
    } catch (x) { picks.replaceChildren(h("p", { class: "err" }, x.message)); }
  };
  type.addEventListener("change", drawSources); drawSources();
  modal(edit ? `Edit ${ex.name}` : "New view", [field("Type", type), field("Name", name), field("Description", desc), sqlField, nsBox, picks,
    h("div", { class: "row2" }, field("Refresh", sched), field("Keep result for (seconds)", ttl)),
    h("label", { class: "check" }, llm, "Allow Sensei to use this view")], {
    submit: edit ? "Save changes" : "Create view",
    onSubmit: async () => {
      const t = type.value, ids = checks.filter(x => x.c.checked).map(x => x.s.id);
      if (kind && !ids.length) throw new Error(`Pick at least one of the ${SOURCE_WORD[kind]}.`);
      let config;
      if (t === "db_table") {
        if (sqlEdit && !sql.value.trim()) throw new Error("Write the SQL query.");
        config = sqlEdit ? { ...cfg, mode: "sql", sql: sql.value } : undefined;
      } else if (t === "ssh_health") config = edit ? undefined : { checks: HEALTH_CHECKS };
      else if (t === "ocp_pods") config = { ...cfg, namespaces: tags(ns.value), only_problems: onlyBad.checked };
      const body = { name: name.value.trim(), description: desc.value, schedule_cron: sched.value || null, cache_ttl_s: +ttl.value, llm_allowed: llm.checked };
      if (kind) body.source_resource_ids = ids;
      if (config) body.config = config;
      if (edit) await api("PATCH", "/api/views/" + ex.id, body);
      else await api("POST", "/api/views", { type: t, ...body });
      toast(edit ? "View updated" : "View created"); done();
    },
  });
}

const openTab = (href, ...kids) => h("a", { href, target: "_blank", rel: "noopener" }, ...kids);

async function viewsPage() {
  const [list, resources] = await Promise.all([api("GET", "/api/views"), api("GET", "/api/resources")]);
  const names = new Map(resources.map(r => [r.id, r.name]));
  const q = h("input", { class: "search", type: "search", placeholder: "Search views", "aria-label": "Search views" });
  const kind = select([["", "All types"], ...Object.entries(VIEW_LABEL)], { class: "select", "aria-label": "Filter by type" });
  const body = h("tbody", {});
  const chips = ids => { const shown = ids.slice(0, 6).map(i => h("span", { class: "tag" }, names.get(i) || "hidden")); return ids.length > 6 ? [...shown, h("span", { class: "mute" }, `+${ids.length - 6} more`)] : shown; };
  const draw = () => {
    const rows = list.filter(v => (!kind.value || v.type === kind.value) && (!q.value || v.name.toLowerCase().includes(q.value.toLowerCase())));
    body.replaceChildren(...rows.map(v => h("tr", {},
      h("td", {}, openTab((v.type === "api_response" ? "#/api/" : "#/views/") + v.id, h("b", {}, v.name)), v.description ? h("div", { class: "mute" }, v.description) : null), h("td", {}, VIEW_LABEL[v.type] || v.type),
      h("td", {}, chips(v.source_resource_ids)), h("td", { class: "mute" }, v.schedule_cron || "On request"),
      h("td", { class: "r" }, h("div", { class: "actions" },
        v.level === "manage" || v.level === "edit" ? h("button", { type: "button", class: "ghost small", onclick: () => viewDialog(viewsPage, v) }, "Edit") : null,
        v.level === "manage" ? h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete view", `Delete ${v.name}?`, "Delete view",
          async () => { await api("DELETE", "/api/views/" + v.id); toast("View deleted"); viewsPage(); }) }, "Delete") : null)))));
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: "5", class: "mute" }, "No views match.")));
  };
  q.addEventListener("input", draw); kind.addEventListener("change", draw);
  const add = () => viewDialog(viewsPage);
  show(pageHead("Views", null, q, kind, h("button", { type: "button", onclick: add }, "New view")),
    list.length ? h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Name", "Type", "Runs on", "Refresh", ""].map(x => h("th", {}, x)))), body)))
      : empty("No views yet", "", h("button", { type: "button", onclick: add }, "New view")));
  draw();
}

// One view in its own tab: the result in pages of 50 rows, however large it is.
async function viewPage(id) {
  const v = await api("GET", "/api/views/" + id);
  const vars = (v.config && v.config.params) || [];
  const inputs = vars.map(p => ({ p, el: p.type === "enum" ? select((p.enum || []).map(x => [x, x])) : h("input", { type: p.type === "number" ? "number" : p.type === "date" ? "date" : "text", value: p.default == null ? "" : p.default }) }));
  const params = () => Object.fromEntries(inputs.filter(i => i.el.value !== "").map(i => [i.p.name, i.el.value]));
  const q = () => "params=" + encodeURIComponent(JSON.stringify(params()));
  const body = h("div", {}), note = h("p", { class: "mute" }), pager = h("div", { class: "pager" });
  const runBtn = h("button", { type: "button" }, vars.length ? "Run" : "Run now");
  let runId = null;

  const showPage = r => {
    runId = r.meta.run_id;
    const first = r.total_rows ? (r.page - 1) * r.size + 1 : 0, last = (r.page - 1) * r.size + r.rows.length;
    const notes = [`${first.toLocaleString()}-${last.toLocaleString()} of ${r.total_rows.toLocaleString()} rows`];
    if (r.meta.generated_at) notes.push((r.meta.cached ? "stored " : "ran ") + when(r.meta.generated_at));
    if (r.meta.truncated) notes.push("cut off at the row limit");
    note.textContent = notes.join(", ");
    const failed = (r.meta.sources || []).filter(s => s.error).map(s => `${s.name}: ${s.error}`);
    body.replaceChildren(r.total_rows ? h("div", { class: "tablewrap tall" }, h("table", {}, h("thead", {}, h("tr", {}, r.columns.map(c => h("th", {}, c.name)))),
      h("tbody", {}, r.rows.map(row => h("tr", {}, row.map(x => h("td", {}, x == null ? "" : typeof x === "object" ? JSON.stringify(x) : x)))))))
      : h("p", { class: "mute" }, "The query ran and returned no rows."), failed.length ? h("p", { class: "err" }, "These failed: " + failed.join("; ")) : null);
    const go = async p => {
      try { showPage(await api("GET", `/api/views/${id}/rows?page=${p}&run_id=${runId}`)); }
      catch (x) { toast(x.message, true); }
    };
    const jump = h("input", { type: "number", class: "pagejump", min: "1", max: String(r.pages), value: r.page, "aria-label": "Page number" });
    jump.addEventListener("change", () => go(Math.min(r.pages, Math.max(1, Math.floor(+jump.value) || 1))));
    const btn = (label, p, off) => h("button", { type: "button", class: "ghost small", disabled: off, onclick: () => go(p) }, label);
    pager.replaceChildren(...(r.pages > 1 ? [btn("First", 1, r.page === 1), btn("Previous", r.page - 1, r.page === 1), h("span", {}, "Page "), jump, h("span", {}, ` of ${r.pages.toLocaleString()}`),
      btn("Next", r.page + 1, r.page === r.pages), btn("Last", r.pages, r.page === r.pages)] : []));
  };

  const run = async fresh => {
    runBtn.disabled = true;
    body.replaceChildren(h("p", { class: "mute" }, "Running…")); note.textContent = ""; pager.replaceChildren();
    try {
      let r;
      try { r = await api("GET", `/api/views/${id}/rows?page=1&fresh=${fresh}&${q()}`); }
      catch (x) {  // view-only access: show the stored result instead of running
        if (x.status !== 403) throw x;
        const last = await api("GET", `/api/views/${id}/result?${q()}`);
        r = await api("GET", `/api/views/${id}/rows?page=1&run_id=${last.meta.run_id}`);
      }
      showPage(r);
    } catch (x) { body.replaceChildren(h("p", { class: x.status === 404 ? "mute" : "err" }, x.status === 404 ? "No result yet." : x.message)); }
    finally { runBtn.disabled = false; }
  };
  runBtn.addEventListener("click", () => run(true));
  const canEdit = v.level === "manage" || v.level === "edit";
  show(pageHead(v.name, v.description || null, h("a", { class: "btn ghost", href: "#/views" }, "All views"),
      canEdit ? h("button", { type: "button", class: "ghost", onclick: () => viewDialog(() => viewPage(id), v) }, "Edit") : null, vars.length ? null : runBtn),
    vars.length ? h("div", { class: "panel" }, h("div", { class: "body" }, h("div", { class: "row2" }, inputs.map(i => field(i.p.label || i.p.name, i.el))), runBtn)) : null,
    note, body, pager);
  if (!vars.some(p => p.required && p.default == null)) run(false);
  else body.replaceChildren(h("p", { class: "mute" }, "Fill in the values and choose Run."));
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
  show(pageHead("Dashboards", null, h("button", { type: "button", onclick: create }, "New dashboard")),
    list.length ? h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Name", "Description"].map(x => h("th", {}, x)))),
        h("tbody", {}, list.map(d => h("tr", {}, h("td", {}, openTab("#/dashboards/" + d.id, h("b", {}, d.name))), h("td", { class: "mute" }, d.description || ""))))))) 
      : empty("No dashboards yet", "A dashboard puts several views on one page that you can share.", h("button", { type: "button", onclick: create }, "New dashboard")));
}

function dashboardEditDialog(d, done) {
  if (d.widgets.some(w => w.no_access)) return toast("This dashboard has widgets you cannot access, so editing it here would remove them.", true);
  api("GET", "/api/views").then(views => {
    const name = text({ required: true, value: d.name }), desc = text({ value: d.description || "" });
    const have = new Map(d.widgets.map(w => [w.view_id, w]));
    const checks = views.map(v => ({ v, c: h("input", { type: "checkbox", checked: have.has(v.id) }) }));
    modal(`Edit ${d.name}`, [field("Name", name), field("Description", desc), h("label", {}, "Show these views"),
      h("div", { class: "perms" }, checks.map(x => h("label", { class: "check" }, x.c, x.v.name)))], {
      submit: "Save changes",
      onSubmit: async () => {
        const picked = checks.filter(x => x.c.checked).map(x => x.v);
        const kept = picked.filter(v => have.has(v.id)).map(v => { const w = have.get(v.id); return { view_id: v.id, title: w.title, renderer: w.renderer, grid: w.grid, options: w.options, params: w.params, refresh_s: w.refresh_s }; });
        let y = kept.reduce((m, w) => Math.max(m, w.grid.y + w.grid.h), 0);
        const added = picked.filter(v => !have.has(v.id)).map((v, i) => ({ view_id: v.id, title: v.name, renderer: v.renderer || "table", grid: { x: (i % 2) * 6, y: y + Math.floor(i / 2) * 5, w: 6, h: 5 } }));
        await api("PATCH", "/api/dashboards/" + d.id, { name: name.value.trim(), description: desc.value, widgets: [...kept, ...added] });
        toast("Dashboard updated"); done();
      },
    });
  }).catch(x => toast(x.message, true));
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
    manage || d.level === "edit" ? h("button", { type: "button", class: "ghost", onclick: () => dashboardEditDialog(d, () => dashboardPage(id)) }, "Edit") : null,
    manage ? h("button", { type: "button", class: "ghost", onclick: share }, "Share") : null,
    manage ? h("button", { type: "button", class: "danger", onclick: () => confirmBox("Delete dashboard", `Delete ${d.name}? The views stay.`, "Delete dashboard",
      async () => { await api("DELETE", "/api/dashboards/" + id); location.hash = "#/dashboards"; }) }, "Delete") : null),
    d.widgets.length ? grid : empty("This dashboard is empty", "Choose Edit to add views to it."));
}

// ---- response viewer: formatted and colour-coded -----------------------------------------------------
// A tokenizer turns the body into DOM nodes, so it is never parsed as HTML and XML entities are never expanded.
const HTTP_TEXT = { 200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 301: "Moved Permanently", 302: "Found", 304: "Not Modified",
  400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found", 405: "Method Not Allowed", 409: "Conflict", 415: "Unsupported Media Type",
  422: "Unprocessable Entity", 429: "Too Many Requests", 500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout" };
const MAX_PRETTY_LINES = 3000, MAX_FIELD_ROWS = 2000;
const XML_TOKEN = /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>|<![^>]*>|<[^>]*>|[^<]+/g;
const piece = (cls, t) => [cls, t];
const bodyKind = t => { const x = t.trimStart(); return x.startsWith("<") ? "xml" : /^[\[{]/.test(x) ? "json" : "text"; };

function xmlTagPieces(tok) {
  const m = /^<(\/?)([^\s\/>]+)([\s\S]*?)(\/?)>$/.exec(tok);
  if (!m) return [piece("", tok)];
  const [, close, name, rest, self] = m, i = name.indexOf(":");
  const out = [piece("pun", close ? "</" : "<")];
  if (i > 0) out.push(piece("ns", name.slice(0, i + 1)), piece("tag", name.slice(i + 1))); else out.push(piece("tag", name));
  for (const a of rest.matchAll(/\s+([^\s=\/>]+)(?:(\s*=\s*)("[^"]*"|'[^']*'|[^\s"'>]+))?/g)) {
    out.push(piece("", " "), piece("attr", a[1]));
    if (a[3] !== undefined) out.push(piece("pun", "="), piece("val", a[3]));
  }
  out.push(piece("pun", self ? "/>" : ">"));
  return out;
}

function xmlLines(src) {
  const kindOf = t => t.startsWith("<!--") ? "com" : t.startsWith("<![CDATA[") ? "text" : t.startsWith("<?") ? "pi" : t.startsWith("<!") ? "doc"
    : t.startsWith("</") ? "close" : t.startsWith("<") ? (t.endsWith("/>") ? "self" : "open") : "text";
  const items = (src.match(XML_TOKEN) || []).map(t => ({ t, k: kindOf(t) })).filter(x => x.k !== "text" || x.t.trim());
  const lines = [];
  let depth = 0;
  for (let i = 0; i < items.length; i++) {
    const { t, k } = items[i];
    if (k === "open" && items[i + 1] && items[i + 1].k === "text" && items[i + 2] && items[i + 2].k === "close") {  // <a>text</a> stays on one line
      lines.push({ depth, pieces: [...xmlTagPieces(t), piece("txt", items[i + 1].t.trim()), ...xmlTagPieces(items[i + 2].t)] });
      i += 2;
      continue;
    }
    if (k === "close") depth = Math.max(0, depth - 1);
    lines.push({ depth, pieces: k === "text" ? [piece("txt", t.trim())] : k === "com" || k === "pi" || k === "doc" ? [piece(k, t.trim())] : xmlTagPieces(t) });
    if (k === "open") depth++;
  }
  return lines;
}

function jsonLines(src) {
  let text = src;
  try { text = JSON.stringify(JSON.parse(src), null, 2); } catch (x) { /* not valid JSON: shown as received */ }
  return text.split("\n").map(line => {
    const pieces = [];
    let last = 0;
    for (const m of line.matchAll(/("(?:\\.|[^"\\])*")(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],]/g)) {
      if (m.index > last) pieces.push(piece("", line.slice(last, m.index)));
      if (m[1] !== undefined && m[2]) pieces.push(piece("key", m[1]), piece("pun", m[2]));
      else pieces.push(piece(m[1] !== undefined ? "str" : /^[{}\[\],]$/.test(m[0]) ? "pun" : /^(true|false|null)$/.test(m[0]) ? "kw" : "num", m[0]));
      last = m.index + m[0].length;
    }
    if (last < line.length) pieces.push(piece("", line.slice(last)));
    return { depth: 0, pieces };
  });
}

function codeBlock(lines) {
  const box = h("div", { class: "code" });
  for (const l of lines.slice(0, MAX_PRETTY_LINES)) {
    const row = h("div", { class: "cl" }, l.pieces.map(([cls, t]) => cls ? h("span", { class: "t-" + cls }, t) : t));
    if (l.depth) row.style.paddingLeft = `calc(54px + ${l.depth * 1.6}ch)`;
    box.append(row);
  }
  if (lines.length > MAX_PRETTY_LINES) box.append(h("div", { class: "cl mute" }, `… ${lines.length - MAX_PRETTY_LINES} more lines. Choose Raw to see everything.`));
  return box;
}

// Every value in the body as path / attributes / text, the quickest way to read a long XML or JSON reply.
function fieldRows(src, kind) {
  const rows = [];
  if (kind === "json") {
    let data;
    try { data = JSON.parse(src); } catch (x) { return rows; }
    const walk = (v, path) => {
      if (v && typeof v === "object") for (const [k, x] of Object.entries(v)) walk(x, Array.isArray(v) ? `${path}[${k}]` : path ? `${path}.${k}` : k);
      else rows.push([path || "value", "", v === null ? "null" : String(v)]);
    };
    walk(data, "");
    return rows;
  }
  if (kind !== "xml") return rows;
  const attrsOf = tok => [...tok.matchAll(/\s([^\s=\/>]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g)].map(a => `${a[1]}=${a[2] !== undefined ? a[2] : a[3]}`).join("   ");
  const stack = [];
  for (const t of src.match(XML_TOKEN) || []) {
    if (t.startsWith("<![CDATA[")) { if (stack.length) stack[stack.length - 1].text += t.slice(9, -3); continue; }
    if (t.startsWith("<!") || t.startsWith("<?")) continue;
    if (!t.startsWith("<")) { if (stack.length) stack[stack.length - 1].text += t.trim(); continue; }
    const m = /^<(\/?)([^\s\/>]+)/.exec(t);
    if (!m) continue;
    const name = m[2].includes(":") ? m[2].split(":")[1] : m[2];
    if (m[1]) {
      const el = stack.pop();
      if (el && !el.kids && (el.text || el.attrs)) rows.push([el.path, el.attrs, el.text]);
    } else {
      if (stack.length) stack[stack.length - 1].kids = true;
      const path = [...stack.map(e => e.name), name].join("/");
      if (t.endsWith("/>")) { const a = attrsOf(t); if (a) rows.push([path, a, ""]); }
      else stack.push({ name, path, attrs: attrsOf(t), text: "", kids: false });
    }
  }
  return rows;
}

function fieldsTable(rows) {
  const withAttrs = rows.some(r => r[1]);
  return h("div", { class: "tablewrap tall" }, h("table", {}, h("thead", {}, h("tr", {}, ["Path", withAttrs ? "Attributes" : null, "Value"].filter(Boolean).map(x => h("th", {}, x)))),
    h("tbody", {}, rows.map(r => h("tr", {}, h("td", {}, h("code", {}, r[0])), withAttrs ? h("td", {}, h("code", {}, r[1])) : null, h("td", {}, r[2]))))));
}

function responseView({ status, text, ms, truncated }) {
  const kind = bodyKind(text);
  const lines = kind === "xml" ? xmlLines(text) : kind === "json" ? jsonLines(text) : text.split("\n").map(l => ({ depth: 0, pieces: [piece("", l)] }));
  const fields = fieldRows(text, kind).slice(0, MAX_FIELD_ROWS);
  const views = { Pretty: () => codeBlock(lines), Raw: () => h("pre", { class: "code raw" }, text), ...(fields.length ? { Fields: () => fieldsTable(fields) } : {}) };
  const body = h("div", {}), tabs = h("div", { class: "tabs", role: "tablist" });
  let active = "Pretty";
  const draw = () => { body.replaceChildren(views[active]()); [...tabs.children].forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === active))); };
  for (const name of Object.keys(views)) tabs.append(h("button", { type: "button", role: "tab", "data-tab": name, onclick: () => { active = name; draw(); } }, name));
  const copy = h("button", { type: "button", class: "ghost small" }, "Copy");
  copy.addEventListener("click", async () => {
    const shown = active === "Pretty" ? lines.map(l => "  ".repeat(l.depth) + l.pieces.map(p => p[1]).join("")).join("\n") : text;
    try { await navigator.clipboard.writeText(shown); copy.textContent = "Copied"; setTimeout(() => { copy.textContent = "Copy"; }, 1500); } catch (x) { toast("Could not copy", true); }
  });
  const bytes = new Blob([text]).size;
  draw();
  return h("div", {},
    h("div", { class: "resp-head" }, h("span", { class: "badge " + (status < 400 ? "ok" : "bad") }, `${status} ${HTTP_TEXT[status] || ""}`.trim()), h("span", { class: "mute" }, `${ms} ms`),
      h("span", { class: "mute" }, bytes >= 1024 ? (bytes / 1024).toFixed(1) + " KB" : bytes + " B"), kind !== "text" ? h("span", { class: "tag" }, kind.toUpperCase()) : null,
      h("span", { class: "spacer" }), tabs, copy),
    body, truncated ? h("p", { class: "mute" }, "The body was cut off at 256 KB.") : null);
}

// ---- API: saved REST and SOAP requests --------------------------------------------------------------
const NAME_IN_TEMPLATE = /\{\{\s*(\w+)\s*\}\}/g;
const namesIn = (...texts) => [...new Set(texts.flatMap(t => [...String(t || "").matchAll(NAME_IN_TEMPLATE)].map(m => m[1])))];
const isSoap = conn => !!conn && !!conn.config && conn.config.kind === "soap";

function parseLines(value) {  // "Name: value" per line
  const out = {};
  for (const line of value.split("\n").map(x => x.trim()).filter(Boolean)) {
    const i = line.indexOf(":");
    if (i < 1) throw new Error(`"${line}" must look like Name: value.`);
    out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

function methodBadge(v, conn) {
  const soap = isSoap(conn);
  return h("span", { class: "badge " + (soap ? "soap" : (v.config.method || "GET").toLowerCase()) }, soap ? "SOAP" : v.config.method || "GET");
}

function apiRequestDialog(done, ex, apis) {
  const edit = !!ex, c = (ex && ex.config) || {};
  if (!apis.length) return toast("Add an API connection in Inventory first.", true);
  const conn = select(apis.map(a => [a.id, `${a.name} (${isSoap(a) ? "SOAP" : "REST"})`]));
  if (edit && ex.source_resource_ids[0]) conn.value = ex.source_resource_ids[0];
  const cur = () => apis.find(a => a.id === conn.value);
  const name = text({ required: true, value: edit ? ex.name : "" }), desc = text({ value: edit ? ex.description || "" : "" });
  const method = select([["GET", "GET"], ["POST", "POST"]]); method.value = c.method || "GET";
  const path = text({ placeholder: "/customers/{{id}}", value: c.path || "" });
  const query = text({ placeholder: "status={{status}}&page=1", value: c.query_template || "" });
  const hdrs = h("textarea", { spellcheck: "false", placeholder: "Name: value, one per line" });
  hdrs.value = Object.entries(c.headers || {}).map(([k, x]) => `${k}: ${x}`).join("\n");
  const fmt = select([["json", "JSON"], ["form", "Form fields"]]); fmt.value = c.body_format || "json";
  const body = h("textarea", { spellcheck: "false", placeholder: '{"name": "{{name}}"}' });
  body.value = c.body_template != null ? JSON.stringify(c.body_template, null, 2) : "";
  const env = h("textarea", { spellcheck: "false", placeholder: "<soapenv:Envelope>…</soapenv:Envelope>" }); env.value = (c.soap && c.soap.envelope_template) || "";
  const output = select([["raw", "Raw response (status and body)"], ["table", "Table of fields"]]); output.value = edit ? c.output || "table" : "raw";
  const llm = h("input", { type: "checkbox", checked: edit && ex.llm_allowed });

  const methodField = field("Method", method);
  const bodyBox = h("div", {}, h("div", { class: "row2" }, field("Body format", fmt)), field("Body", body, "A JSON object. Use {{name}} where a value is typed in at run time."));
  const soapBox = h("div", {}, field("Envelope (XML)", env, "Use {{name}} where a value is typed in at run time."));

  // Parameters are the {{names}} found in the request; the admin describes each one.
  const state = new Map((c.variables || []).map(v => [v.name, { label: v.label || "", type: v.type || "string", required: v.required !== false, default: v.default == null ? "" : String(v.default), pattern: v.pattern || "" }]));
  const paramBox = h("div", {});
  const current = () => namesIn(path.value, query.value, hdrs.value, isSoap(cur()) ? env.value : body.value);
  const drawParams = () => {
    const inHeader = new Set(namesIn(hdrs.value));
    const names = current();
    paramBox.replaceChildren(...(names.length ? [h("label", {}, "Parameters the user is asked for")] : []), ...names.map(n => {
      if (!state.has(n)) state.set(n, { label: "", type: "string", required: true, default: "", pattern: "" });
      const st = state.get(n);
      const label = text({ value: st.label, placeholder: n }), type = select([["string", "Text"], ["number", "Number"], ["boolean", "Yes / no"], ["date", "Date"]]);
      type.value = st.type;
      const def = text({ value: st.default }), req = h("input", { type: "checkbox", checked: st.required }), pat = text({ value: st.pattern, placeholder: inHeader.has(n) ? "Required for a header value, e.g. ^[A-Za-z0-9-]+$" : "Optional, e.g. ^[0-9]+$" });
      label.addEventListener("input", () => { st.label = label.value; }); type.addEventListener("change", () => { st.type = type.value; });
      def.addEventListener("input", () => { st.default = def.value; }); req.addEventListener("change", () => { st.required = req.checked; });
      pat.addEventListener("input", () => { st.pattern = pat.value; });
      return h("div", { class: "param" }, h("code", {}, `{{${n}}}`), h("div", { class: "row2" }, field("Label", label), field("Type", type)),
        h("div", { class: "row2" }, field("Default value", def), h("label", { class: "check" }, req, "Required")), field("Allowed format", pat));
    }));
  };
  const sync = () => { const soap = isSoap(cur()); hdrs.placeholder = soap ? "Content-Type: application/soap+xml;charset=utf-8" : "Name: value, one per line"; methodField.hidden = soap; bodyBox.hidden = soap || method.value !== "POST"; soapBox.hidden = !soap; drawParams(); };
  for (const el of [path, query, hdrs, body, env]) el.addEventListener("input", drawParams);
  conn.addEventListener("change", sync); method.addEventListener("change", sync); sync();

  modal(edit ? `Edit ${ex.name}` : "New API request", [field("Connection", conn, "Its address and sign-in are set in Inventory."), field("Name", name), field("Description", desc), methodField,
    field("Path", path, "Added to the connection's base URL."), field("Query", query), field("Headers", hdrs), bodyBox, soapBox, paramBox, field("Show the result as", output),
    h("label", { class: "check" }, llm, "Allow Sensei to send this request when someone asks it to")], {
    submit: edit ? "Save changes" : "Create request",
    onSubmit: async () => {
      const k = cur(), soap = isSoap(k), m = soap ? "POST" : method.value, allowed = k.config.allowed_methods || [];
      if (!allowed.includes(m)) throw new Error(`${k.name} does not allow ${m}. Change it in Inventory first.`);
      const cfg = { output: output.value, method: m, path: path.value.trim(), query_template: query.value.trim(), headers: parseLines(hdrs.value) };
      if (c.mapping) cfg.mapping = c.mapping;  // field mapping is kept when editing, even though this form does not edit it
      if (soap) {
        if (!env.value.trim()) throw new Error("A SOAP request needs the envelope.");
        cfg.soap = { ...(c.soap || {}), envelope_template: env.value };  // an action saved earlier is kept
      } else if (m === "POST" && body.value.trim()) {
        try { cfg.body_template = JSON.parse(body.value); } catch (x) { throw new Error("The body is not valid JSON."); }
        cfg.body_format = fmt.value;
      }
      const inHeader = new Set(namesIn(hdrs.value));
      cfg.variables = current().map(n => {
        const st = state.get(n);
        if (!st.required && st.default === "") throw new Error(`{{${n}}} is optional, so it needs a default value.`);
        if (inHeader.has(n) && !st.pattern.trim()) throw new Error(`{{${n}}} is used in a header, so it needs an allowed format.`);
        return { name: n, label: st.label || n, type: st.type, required: st.required, default: st.default === "" ? null : st.default, pattern: st.pattern.trim() || null };
      });
      const payload = { name: name.value.trim(), description: desc.value, config: cfg, source_resource_ids: [k.id], llm_allowed: llm.checked };
      if (edit) await api("PATCH", "/api/views/" + ex.id, payload);
      else await api("POST", "/api/views", { type: "api_response", cache_ttl_s: 0, schedule_cron: null, ...payload });
      toast(edit ? "Request updated" : "Request created"); done();
    },
  });
}

async function apiPage() {
  const [all, conns, types] = await Promise.all([api("GET", "/api/views?type=api_response"), api("GET", "/api/resources?type=api"), api("GET", "/api/view-types")]);
  const list = all.filter(v => v.type === "api_response");
  const canCreate = !!(types.find(t => t.key === "api_response") || {}).can_create;
  const byId = new Map(conns.map(r => [r.id, r]));
  const q = h("input", { class: "search", type: "search", placeholder: "Search requests", "aria-label": "Search requests" });
  const body = h("tbody", {});
  const draw = () => {
    const rows = list.filter(v => !q.value || v.name.toLowerCase().includes(q.value.toLowerCase()));
    body.replaceChildren(...rows.map(v => {
      const k = byId.get(v.source_resource_ids[0]);
      return h("tr", {}, h("td", {}, openTab("#/api/" + v.id, h("b", {}, v.name)), v.description ? h("div", { class: "mute" }, v.description) : null),
        h("td", {}, methodBadge(v, k)), h("td", {}, k ? k.name : h("span", { class: "mute" }, "hidden")), h("td", {}, h("code", {}, (v.config && v.config.path) || "/")),
        h("td", { class: "r" }, h("div", { class: "actions" }, openTab("#/api/" + v.id, h("span", { class: "btn small" }, "Open")),
          v.level === "manage" || v.level === "edit" ? h("button", { type: "button", class: "ghost small", onclick: () => apiRequestDialog(apiPage, v, conns) }, "Edit") : null,
          v.level === "manage" ? h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete request", `Delete ${v.name}?`, "Delete request",
            async () => { await api("DELETE", "/api/views/" + v.id); toast("Request deleted"); apiPage(); }) }, "Delete") : null)));
    }));
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: "5", class: "mute" }, "No requests match.")));
  };
  q.addEventListener("input", draw);
  const add = () => apiRequestDialog(apiPage, null, conns);
  show(pageHead("API", null, q, canCreate ? h("button", { type: "button", onclick: add }, "New request") : null),
    list.length ? h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Request", "Method", "Connection", "Path", ""].map(x => h("th", {}, x)))), body)))
      : empty("No API requests yet", canCreate ? "" : "Ask an administrator to add one.", canCreate ? h("button", { type: "button", onclick: add }, "New request") : null));
  draw();
}

// One request in its own tab: ask for the parameters, send, show what came back. Nothing is sent until Send is chosen.
async function apiRequestPage(id) {
  const [v, conns] = await Promise.all([api("GET", "/api/views/" + id), api("GET", "/api/resources?type=api")]);
  if (v.type !== "api_response") return show(pageHead("API"), h("p", { class: "err" }, "That is not an API request."));
  const conn = conns.find(r => r.id === v.source_resource_ids[0]), c = v.config, vars = c.variables || [];
  const method = isSoap(conn) ? "POST" : c.method || "GET";
  const where = (conn ? conn.config.base_url.replace(/\/$/, "") : "(connection hidden)") + (c.path || "") + (c.query_template ? "?" + c.query_template : "");
  const inputs = vars.map(p => {
    let el;
    if (p.type === "boolean") { el = select([["true", "Yes"], ["false", "No"]]); if (p.default != null) el.value = String(p.default); }
    else if (p.type === "enum") { el = select((p.enum || []).map(x => [x, x])); if (p.default != null) el.value = String(p.default); }
    else el = h("input", { type: p.type === "number" ? "number" : p.type === "date" ? "date" : "text", value: p.default == null ? "" : p.default, placeholder: p.pattern ? "Format: " + p.pattern : "" });
    return { p, el };
  });
  const out = h("div", {}), sendBtn = h("button", { type: "button" }, "Send");

  const show_ = res => {
    const bits = [], ms = res.meta.duration_ms;
    if (c.output === "raw") {
      const [status, text] = res.rows[0];
      out.replaceChildren(responseView({ status, text, ms, truncated: res.meta.truncated }));
    } else {
      out.replaceChildren(h("div", { class: "resp-head" }, h("span", { class: "badge ok" }, "OK"), h("span", { class: "mute" }, `${ms} ms`)), renderResult(res, v.renderer));
    }
  };
  sendBtn.addEventListener("click", async () => {
    const missing = inputs.filter(i => i.p.required && i.el.value === "").map(i => i.p.label || i.p.name);
    if (missing.length) return out.replaceChildren(h("p", { class: "err" }, "Fill in: " + missing.join(", ")));
    sendBtn.disabled = true; out.replaceChildren(h("p", { class: "mute" }, "Sending…"));
    try { show_(await api("POST", `/api/views/${id}/run?fresh=true`, { params: Object.fromEntries(inputs.filter(i => i.el.value !== "").map(i => [i.p.name, i.p.type === "number" ? +i.el.value : i.el.value])) })); }
    catch (x) {
      const bad = x.details && x.details.invalid ? Object.entries(x.details.invalid).map(([k, m]) => `${k} ${m}`).join("; ") : "";
      out.replaceChildren(h("p", { class: "err" }, bad ? "Not sent: " + bad : x.message));
    } finally { sendBtn.disabled = false; }
  });
  const canEdit = v.level === "manage" || v.level === "edit";
  show(pageHead(v.name, v.description || null, h("a", { class: "btn ghost", href: "#/api" }, "All requests"),
      canEdit ? h("button", { type: "button", class: "ghost", onclick: () => apiRequestDialog(() => apiRequestPage(id), v, conns) }, "Edit") : null),
    h("div", { class: "panel" }, h("div", { class: "body" }, h("div", { class: "reqline" }, methodBadge(v, conn), h("code", {}, where)),
      vars.length ? h("div", { class: "row2" }, inputs.map(i => field((i.p.label || i.p.name) + (i.p.required ? " *" : ""), i.el))) : h("p", { class: "mute" }, "This request has no parameters."), sendBtn)),
    h("div", { class: "panel" }, h("h2", {}, "Response"), h("div", { class: "body" }, out)));
  out.replaceChildren(h("p", { class: "mute" }, "Choose Send to run the request."));
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
  show(pageHead("Links", null, h("button", { type: "button", onclick: add }, "Add link")),
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

// Group checkboxes for a user dialog; `save(userId)` writes only the memberships that changed.
function groupPicker(groups, userId) {
  const boxes = groups.map(g => ({ g, c: h("input", { type: "checkbox", checked: !!userId && g.members.includes(userId) }) }));
  const node = groups.length ? h("div", {}, h("label", {}, "Groups"), h("div", { class: "perms" }, boxes.map(b => h("label", { class: "check" }, b.c, b.g.name)))) : null;
  const save = async id => {
    for (const { g, c } of boxes) {
      const has = g.members.includes(id);
      if (c.checked !== has) await api("PUT", `/api/admin/groups/${g.id}/members`, { user_ids: c.checked ? [...g.members, id] : g.members.filter(x => x !== id) });
    }
  };
  return { node, save };
}

async function usersPage() {
  const [users, groups] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/groups")]);
  const gname = new Map(groups.map(g => [g.id, g.name]));
  const add = () => {
    const login = text({ required: true, pattern: "[A-Za-z0-9._@\\-]+" }), nm = text(), adm = h("input", { type: "checkbox" });
    const picker = groupPicker(groups, null);
    modal("Add a user", [field("Login", login, "Letters, digits and . _ @ -"), field("Display name", nm), h("label", { class: "check" }, adm, "Administrator (can do everything)"), picker.node], {
      submit: "Create user",
      onSubmit: async () => {
        const u = await api("POST", "/api/admin/users", { login: login.value.trim(), display_name: nm.value, is_admin: adm.checked });
        try { await picker.save(u.id); } catch (x) { toast("The user was created, but adding them to groups failed: " + x.message, true); }
        usersPage(); setTimeout(() => tempPasswordDialog(u.login, u.temp_password), 0);
      },
    });
  };
  const editUser = u => {
    const nm = text({ value: u.display_name || "" }), adm = h("input", { type: "checkbox", checked: u.is_admin, disabled: u.id === me.id });
    const picker = groupPicker(groups, u.id);
    modal(`Edit ${u.login}`, [field("Display name", nm), h("label", { class: "check" }, adm, "Administrator (can do everything)"),
      u.id === me.id ? h("p", { class: "mute" }, "You cannot remove your own administrator role.") : null, picker.node], {
      submit: "Save changes",
      onSubmit: async () => { const body = { display_name: nm.value }; if (u.id !== me.id) body.is_admin = adm.checked; await api("PATCH", `/api/admin/users/${u.id}`, body); await picker.save(u.id); toast("User updated"); usersPage(); },
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
    h("td", {}, u.is_admin ? "Administrator" : "User"), h("td", {}, (u.groups || []).map(g => h("span", { class: "tag" }, gname.get(g) || "group"))),
    h("td", {}, h("span", { class: "dot " + (u.is_active ? "up" : "unknown") }, u.is_active ? (u.locked ? "Locked" : "Active") : "Deactivated")),
    h("td", { class: "mute" }, u.last_login ? when(u.last_login) : "Never"),
    h("td", { class: "r" }, h("div", { class: "actions" },
      h("button", { type: "button", class: "ghost small", onclick: () => editUser(u) }, "Edit"),
      h("button", { type: "button", class: "ghost small", onclick: () => perms(u).catch(x => toast(x.message, true)) }, "Permissions"),
      h("button", { type: "button", class: "ghost small", onclick: () => confirmBox("Reset password", `Create a new temporary password for ${u.login}? Their sessions end now.`, "Reset password",
        async () => { const r = await api("POST", `/api/admin/users/${u.id}/reset-password`); setTimeout(() => tempPasswordDialog(u.login, r.temp_password), 0); }) }, "Reset password"),
      u.id === me.id ? null : h("button", { type: "button", class: "ghost small", onclick: async () => { try { await api("PATCH", `/api/admin/users/${u.id}`, { is_active: !u.is_active }); usersPage(); } catch (x) { toast(x.message, true); } } }, u.is_active ? "Deactivate" : "Activate"),
      u.id === me.id ? null : h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete user", `Permanently delete ${u.login}? Their sessions, group memberships and Sensei chats are removed. This cannot be undone.`, "Delete user",
        async () => { await api("DELETE", `/api/admin/users/${u.id}`); toast("User deleted"); usersPage(); }) }, "Delete")))));
  show(pageHead("Users", null, h("button", { type: "button", onclick: add }, "Add user")),
    h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Person", "Role", "Groups", "Status", "Last sign-in", ""].map(x => h("th", {}, x)))), h("tbody", {}, rows)))));
}

// ---- groups (admin) ----------------------------------------------------------------------------------
async function groupsPage() {
  const [groups, users] = await Promise.all([api("GET", "/api/admin/groups"), api("GET", "/api/admin/users")]);
  const uname = new Map(users.map(u => [u.id, u.display_name || u.login]));
  const edit = g => {
    const nm = text({ required: true, maxlength: "128", value: g ? g.name : "" }), desc = text({ value: g ? g.description || "" : "" });
    modal(g ? `Edit ${g.name}` : "New group", [field("Name", nm), field("Description", desc)], {
      submit: g ? "Save changes" : "Create group",
      onSubmit: async () => {
        const body = { name: nm.value.trim(), description: desc.value };
        if (g) await api("PATCH", `/api/admin/groups/${g.id}`, body); else await api("POST", "/api/admin/groups", body);
        toast(g ? "Group updated" : "Group created"); groupsPage();
      },
    });
  };
  const members = g => {
    const boxes = users.map(u => ({ u, c: h("input", { type: "checkbox", checked: g.members.includes(u.id) }) }));
    modal(`Members of ${g.name}`, [h("div", { class: "perms" }, boxes.map(b => h("label", { class: "check" }, b.c, b.u.display_name || b.u.login)))], {
      submit: "Save members",
      onSubmit: async () => { await api("PUT", `/api/admin/groups/${g.id}/members`, { user_ids: boxes.filter(b => b.c.checked).map(b => b.u.id) }); toast("Members saved"); groupsPage(); },
    });
  };
  const perms = async g => {
    const cur = new Set((await api("GET", `/api/admin/groups/${g.id}/permissions`)).permissions.map(p => p.permission));
    const boxes = Object.entries(PERMISSIONS).map(([k, l]) => ({ k, c: h("input", { type: "checkbox", checked: cur.has(k) }), l }));
    modal(`What ${g.name} can do`, [h("div", { class: "perms" }, boxes.map(b => h("label", { class: "check" }, b.c, b.l)))], {
      submit: "Save permissions",
      onSubmit: async () => { const keep = [...cur].filter(k => !(k in PERMISSIONS));  // keep scoped grants this screen does not edit
        await api("PUT", `/api/admin/groups/${g.id}/permissions`, { permissions: [...boxes.filter(b => b.c.checked).map(b => b.k), ...keep].map(permission => ({ permission })) }); toast("Permissions saved"); },
    });
  };
  const rows = groups.map(g => h("tr", {},
    h("td", {}, h("b", {}, g.name), g.description ? h("div", { class: "mute" }, g.description) : null),
    h("td", {}, g.members.length ? [...g.members.slice(0, 6).map(m => h("span", { class: "tag" }, uname.get(m) || "user")), g.members.length > 6 ? h("span", { class: "mute" }, `+${g.members.length - 6} more`) : null] : h("span", { class: "mute" }, "No members")),
    h("td", { class: "r" }, h("div", { class: "actions" },
      h("button", { type: "button", class: "ghost small", onclick: () => edit(g) }, "Edit"),
      h("button", { type: "button", class: "ghost small", onclick: () => members(g) }, "Members"),
      h("button", { type: "button", class: "ghost small", onclick: () => perms(g).catch(x => toast(x.message, true)) }, "Permissions"),
      h("button", { type: "button", class: "danger small", onclick: () => confirmBox("Delete group", `Delete ${g.name}? Its members lose what the group gave them.`, "Delete group",
        async () => { await api("DELETE", `/api/admin/groups/${g.id}`); toast("Group deleted"); groupsPage(); }) }, "Delete")))));
  show(pageHead("Groups", null, h("button", { type: "button", onclick: () => edit(null) }, "New group")),
    groups.length ? h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Group", "Members", ""].map(x => h("th", {}, x)))), h("tbody", {}, rows))))
      : empty("No groups yet", "", h("button", { type: "button", onclick: () => edit(null) }, "New group")));
}

// ---- policy (admin): who can see, use, edit and manage each connection, view and dashboard -------------
const POLICY_KINDS = { resources: ["Connections", "/api/resources"], views: ["Views", "/api/views"], dashboards: ["Dashboards", "/api/dashboards"] };
const ACCESS_LEVELS = [["view", "View"], ["use", "Use"], ["edit", "Edit"], ["manage", "Manage"]];

async function accessDialog(base, obj, users, groups) {
  const entries = (await api("GET", `${base}/${obj.id}/acl`)).entries;
  const list = h("div", { class: "acl" });
  const state = entries.map(e => ({ who: `${e.principal_type}:${e.principal_id}`, level: e.level }));
  const whoSelect = value => {
    const sel = h("select", { "aria-label": "User or group" },
      h("optgroup", { label: "Users" }, users.map(u => h("option", { value: "user:" + u.id }, u.display_name || u.login))),
      h("optgroup", { label: "Groups" }, groups.map(g => h("option", { value: "group:" + g.id }, g.name))));
    sel.value = value;
    return sel;
  };
  const draw = () => {
    list.replaceChildren(...(state.length ? state.map((row, i) => {
      const who = whoSelect(row.who), level = select(ACCESS_LEVELS, { "aria-label": "Access level" });
      level.value = row.level;
      who.addEventListener("change", () => { row.who = who.value; }); level.addEventListener("change", () => { row.level = level.value; });
      return h("div", { class: "acl-row" }, who, level, h("button", { type: "button", class: "danger small", onclick: () => { state.splice(i, 1); draw(); } }, "Remove"));
    }) : [h("p", { class: "mute" }, "Only the owner and administrators have access.")]));
  };
  draw();
  const add = h("button", { type: "button", class: "ghost small", onclick: () => {
    const first = users[0] ? "user:" + users[0].id : groups[0] ? "group:" + groups[0].id : null;
    if (!first) return toast("There are no users or groups yet.", true);
    state.push({ who: first, level: "view" }); draw();
  } }, "Add access");
  modal(`Access to ${obj.name}`, [list, add], {
    submit: "Save access",
    onSubmit: async () => {
      const seen = new Set();
      for (const r of state) { if (seen.has(r.who)) throw new Error("Each user or group can be listed only once."); seen.add(r.who); }
      await api("PUT", `${base}/${obj.id}/acl`, { entries: state.map(r => { const [t, id] = r.who.split(":"); return { principal_type: t, principal_id: id, level: r.level }; }) });
      toast("Access saved");
    },
  });
}

async function policyPage() {
  const [users, groups] = await Promise.all([api("GET", "/api/admin/users"), api("GET", "/api/admin/groups")]);
  const kind = select(Object.entries(POLICY_KINDS).map(([k, [label]]) => [k, label]), { class: "select", "aria-label": "What to share" });
  const q = h("input", { class: "search", type: "search", placeholder: "Search", "aria-label": "Search" });
  const body = h("tbody", {});
  let items = [];
  const draw = () => {
    const base = POLICY_KINDS[kind.value][1];
    const rows = items.filter(o => !q.value || o.name.toLowerCase().includes(q.value.toLowerCase()));
    body.replaceChildren(...rows.map(o => h("tr", {}, h("td", {}, h("b", {}, o.name)), h("td", { class: "mute" }, (o.type && (TYPE_LABEL[o.type] || VIEW_LABEL[o.type] || o.type)) || ""),
      h("td", { class: "r" }, h("button", { type: "button", class: "ghost small", onclick: () => accessDialog(base, o, users, groups).catch(x => toast(x.message, true)) }, "Access")))));
    if (!rows.length) body.append(h("tr", {}, h("td", { colspan: "3", class: "mute" }, "Nothing here yet.")));
  };
  const load = async () => { items = await api("GET", POLICY_KINDS[kind.value][1]); draw(); };
  kind.addEventListener("change", () => load().catch(x => toast(x.message, true))); q.addEventListener("input", draw);
  show(pageHead("Policy", null, kind, q),
    h("div", { class: "panel" }, h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Name", "Type", ""].map(x => h("th", {}, x)))), body))));
  await load();
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
  show(pageHead("Sensei", null, ...actions),
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
// ---- servers: live Linux health ---------------------------------------------------------------------
const RANK = { down: 0, crit: 1, warn: 2, ok: 3 };
const HEALTH_WORD = { ok: "Healthy", warn: "Warning", crit: "Critical" };
const upDays = d => d == null ? "n/a" : d >= 1 ? `${Math.floor(d)}d ${Math.floor((d % 1) * 24)}h` : `${Math.floor(d * 24)}h`;

function meter(label, value, text, st) {
  const fill = h("span", { class: st || "ok" });
  fill.style.width = Math.min(100, Math.max(0, value || 0)) + "%";
  return h("div", { class: "metric" }, h("div", { class: "mhead" }, h("span", {}, label), h("b", { class: "num " + (st || "") }, text)), h("div", { class: "bar" }, fill));
}
const fact = (label, text) => h("div", { class: "metric" }, h("div", { class: "mhead" }, h("span", {}, label), h("b", { class: "num" }, text)));
const serverState = s => s.status === "down" ? "down" : s.health;

function serverCard(s) {
  const st = serverState(s), rows = [];
  if (st === "down") rows.push(h("p", { class: "err" }, s.error || "Unreachable"));
  else {
    if (s.cpu) rows.push(meter("Load", s.cpu.per_core * 100, `${s.cpu.load1.toFixed(2)} on ${s.cpu.cores} cores`, s.cpu.status));
    if (s.mem) rows.push(meter("Memory", s.mem.used_pct, `${s.mem.used_pct}%`, s.mem.status));
    if (s.swap) rows.push(s.swap.total_mb ? meter("Swap", s.swap.used_pct, `${s.swap.used_pct}%`, s.swap.status) : fact("Swap", "none"));
    for (const d of s.disks.slice(0, 2)) rows.push(meter("Disk " + d.mount, d.used_pct, `${d.used_pct}%`, d.status));
    if (s.iowait) rows.push(meter("I/O wait", s.iowait.pct, `${s.iowait.pct}%`, s.iowait.status));
    rows.push(fact("Uptime", upDays(s.uptime_days)));
  }
  return h("div", { class: "card " + st },
    h("div", { class: "chead" }, h("h3", {}, s.name), st === "down" ? h("span", { class: "dot down" }, "Down") : h("span", { class: "dot " + (st === "ok" ? "up" : st) }, HEALTH_WORD[st])),
    s.tags.length ? h("div", {}, s.tags.map(t => h("span", { class: "tag" }, t))) : null, ...rows,
    h("p", { class: "mute small" }, "Checked " + when(s.checked_at)));
}

async function serversPage() {
  const grid = h("div", { class: "cards health" }, h("p", { class: "mute" }, "Checking servers…"));
  const stats = h("div", { class: "stats" });
  const q = h("input", { class: "search", type: "search", placeholder: "Search servers", "aria-label": "Search servers" });
  const only = select([["", "All servers"], ["problem", "Problems only"]], { class: "select", "aria-label": "Filter" });
  const refresh = h("button", { type: "button", class: "ghost" }, "Refresh");
  let data = [], loaded = false;
  const draw = () => {
    const n = k => data.filter(s => serverState(s) === k).length;
    stats.replaceChildren(...[["Servers", data.length, ""], ["Healthy", n("ok"), "ok"], ["Warning", n("warn"), n("warn") ? "warn" : ""],
      ["Critical", n("crit"), n("crit") ? "bad" : ""], ["Down", n("down"), n("down") ? "bad" : ""]].map(([l, v, c]) =>
      h("div", { class: "stat " + c }, h("h3", {}, l), h("span", { class: "total" }, v))));
    const rows = data.filter(s => (!only.value || serverState(s) !== "ok") && (!q.value || s.name.toLowerCase().includes(q.value.toLowerCase()) || s.tags.some(t => t.includes(q.value.toLowerCase()))))
      .sort((a, b) => RANK[serverState(a)] - RANK[serverState(b)] || a.name.localeCompare(b.name));
    grid.replaceChildren(...(rows.length ? rows.map(serverCard) : [h("p", { class: "mute" }, loaded ? "No servers match." : "Checking servers…")]));
  };
  const load = async fresh => {
    refresh.disabled = true;
    try { data = (await api("GET", "/api/servers/health" + (fresh ? "?fresh=true" : ""))).servers; loaded = true; draw(); }
    finally { refresh.disabled = false; }
  };
  refresh.addEventListener("click", () => load(true).catch(x => toast(x.message, true)));
  q.addEventListener("input", draw); only.addEventListener("change", draw);
  show(pageHead("Servers", null, q, only, refresh), stats, grid);
  await load(false);
  if (!data.length) return show(pageHead("Servers"), empty("No Linux servers yet", "Add a Linux host in Inventory.", h("a", { class: "btn", href: "#/inventory" }, "Open inventory")));
  timers.push(setInterval(() => load(false).catch(() => {}), 60000));
}

// ---- pods: OpenShift ---------------------------------------------------------------------------------
const POD_DOT = { ok: "up", warn: "warn", crit: "down" };

function podTable(pods) {
  return h("div", { class: "tablewrap" }, h("table", {}, h("thead", {}, h("tr", {}, ["Pod", "State", "Ready", "Restarts", "Age", "Node"].map(x => h("th", {}, x)))),
    h("tbody", {}, pods.map(p => h("tr", {}, h("td", {}, p.name), h("td", {}, h("span", { class: "dot " + POD_DOT[p.status] }, p.state)), h("td", { class: "num" }, p.ready),
      h("td", { class: "num" }, p.restarts), h("td", { class: "mute" }, p.age), h("td", { class: "mute" }, p.node || "-"))))));
}

function namespaceBlock(c, n) {
  const list = h("div", {});
  const draw = pods => list.replaceChildren(pods.length ? podTable(pods) : h("p", { class: "mute" }, "Every pod here is running normally."));
  draw(n.problems);
  let all = false;
  const toggle = h("button", { type: "button", class: "ghost small" }, `All pods (${n.total})`);
  toggle.addEventListener("click", async () => {
    all = !all;
    toggle.disabled = true;
    try {
      if (all) {
        const r = await api("GET", `/api/pods?cluster=${encodeURIComponent(c.id)}&namespace=${encodeURIComponent(n.name)}&all=true`);
        draw(r.clusters[0].namespaces.find(x => x.name === n.name).pods);
      } else draw(n.problems);
      toggle.textContent = all ? "Only problems" : `All pods (${n.total})`;
    } catch (x) { all = !all; toast(x.message, true); }
    toggle.disabled = false;
  });
  return h("section", { class: "ns" + (n.crit ? " bad" : n.warn ? " warn" : "") },
    h("div", { class: "chead" }, h("h3", {}, n.name), toggle),
    h("div", { class: "pills" }, h("span", { class: "pill ok" }, `${n.ok} running`), n.warn ? h("span", { class: "pill warn" }, `${n.warn} warning`) : null, n.crit ? h("span", { class: "pill crit" }, `${n.crit} failing`) : null),
    list);
}

async function podsPage() {
  const wrap = h("div", {});
  const q = h("input", { class: "search", type: "search", placeholder: "Search namespaces", "aria-label": "Search namespaces" });
  const refresh = h("button", { type: "button", class: "ghost" }, "Refresh");
  let clusters = [];
  const draw = () => {
    const f = q.value.toLowerCase();
    wrap.replaceChildren(...clusters.map(c => h("section", { class: "cluster" },
      h("div", { class: "chead" }, h("h2", {}, c.name), statusDot(c.status),
        c.status === "up" ? h("div", { class: "pills" }, h("span", { class: "pill ok" }, `${c.totals.ok} running`), c.totals.warn ? h("span", { class: "pill warn" }, `${c.totals.warn} warning`) : null,
          c.totals.crit ? h("span", { class: "pill crit" }, `${c.totals.crit} failing`) : null) : null),
      c.status === "down" ? h("p", { class: "err" }, c.error || "Unreachable")
        : h("div", { class: "nsgrid" }, c.namespaces.filter(n => !f || n.name.includes(f)).sort((a, b) => b.crit - a.crit || b.warn - a.warn || a.name.localeCompare(b.name)).map(n => namespaceBlock(c, n))),
      c.truncated ? h("p", { class: "mute small" }, "Showing the first 5000 pods.") : null)));
  };
  const load = async fresh => {
    refresh.disabled = true;
    try { clusters = (await api("GET", "/api/pods" + (fresh ? "?fresh=true" : ""))).clusters; draw(); }
    finally { refresh.disabled = false; }
  };
  refresh.addEventListener("click", () => load(true).catch(x => toast(x.message, true)));
  q.addEventListener("input", draw);
  show(pageHead("Pods", null, q, refresh), h("p", { class: "mute" }, "Checking clusters…"));
  await load(false);
  if (!clusters.length) return show(pageHead("Pods"), empty("No OpenShift clusters yet", "Add one in Inventory.", h("a", { class: "btn", href: "#/inventory" }, "Open inventory")));
  show(pageHead("Pods", null, q, refresh), wrap);
  timers.push(setInterval(() => load(false).catch(() => {}), 60000));
}

const PAGES = { "": homePage, dashboards: dashboardsPage, servers: serversPage,
  pods: podsPage, views: viewsPage, api: apiPage,
  links: linksPage, sensei: senseiPage, inventory: inventoryPage, users: usersPage, groups: groupsPage, policy: policyPage };

// Drawer: pinned = docked beside the page (the burger can still fold it); unpinned or narrow screen = overlay.
const shell = document.getElementById("shell"), narrow = window.matchMedia("(max-width:820px)");
let pinned = true, navOpen = true;
try { pinned = localStorage.getItem("lookout.pin") !== "0"; } catch (x) { /* storage blocked: stay pinned */ }

function layoutNav() {
  const overlay = !pinned || narrow.matches;
  shell.classList.toggle("overlay", overlay);
  shell.classList.toggle("open", overlay && navOpen);
  shell.classList.toggle("closed", !overlay && !navOpen);
  document.getElementById("scrim").hidden = !(overlay && navOpen) || bar.hidden;
  document.getElementById("burger").setAttribute("aria-expanded", String(navOpen));
  const pin = document.getElementById("pin");
  pin.setAttribute("aria-pressed", String(pinned));
  pin.textContent = pinned ? "Pinned" : "Pin";
}
function setNavOpen(v) { navOpen = v; layoutNav(); }
document.getElementById("burger").addEventListener("click", () => setNavOpen(!navOpen));
document.getElementById("scrim").addEventListener("click", () => setNavOpen(false));
document.getElementById("pin").addEventListener("click", () => {
  pinned = !pinned; navOpen = pinned;
  try { localStorage.setItem("lookout.pin", pinned ? "1" : "0"); } catch (x) { /* not persisted */ }
  layoutNav();
});
narrow.addEventListener("change", () => { navOpen = pinned && !narrow.matches; layoutNav(); });
navOpen = pinned && !narrow.matches;

function drawNav(section) {
  const link = (k, l) => h("a", { href: "#/" + k, "aria-current": k === section ? "page" : null }, l);
  const items = [["", "Overview"], ["dashboards", "Dashboards"], ["servers", "Servers"], ["pods", "Pods"], ["views", "Views"], ["api", "API"],
    ["links", "Links"], ["sensei", "Sensei"], ["inventory", "Inventory"]];
  const nodes = items.map(([k, l]) => link(k, l));
  if (me.is_admin) nodes.push(h("span", { class: "sep" }, "Admin"), link("users", "Users"), link("groups", "Groups"), link("policy", "Policy"));
  document.getElementById("nav").replaceChildren(...nodes);
  if (shell.classList.contains("overlay")) setNavOpen(false);  // picking a page closes the overlay drawer
}

function chrome(on) { bar.hidden = !on; document.getElementById("topbar").hidden = !on; layoutNav(); }

async function route() {
  clearTimers();
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  try {
    if (parts[0] === "shared" && parts[1]) { chrome(false); return await sharedPage(parts[1]); }
    if (!me) { chrome(false); return loginPage(); }
    if (me.must_change_pw) { chrome(false); return changePasswordPage(); }
    chrome(true);
    drawNav(parts[0]);
    window.scrollTo(0, 0);
    if (parts[0] === "dashboards" && parts[1]) return await dashboardPage(parts[1]);
    if (parts[0] === "views" && parts[1]) return await viewPage(parts[1]);
    if (parts[0] === "api" && parts[1]) return await apiRequestPage(parts[1]);
    return await (PAGES[parts[0]] || homePage)();
  } catch (x) {
    if (x.status === 401) { me = null; chrome(false); return loginPage("Your session ended. Sign in again."); }
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
