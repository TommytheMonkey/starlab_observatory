// Starlab: Slack and Google Calendar setup cards, shared by the setup wizard (/setup) and Settings (/rf).
// Each page passes its own api() helper (same-origin fetch with the X-AOBS-RF header). Credentials go to the server
// write-only: nothing secret is ever sent back to the page.
"use strict";
(function () {
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const when = (iso) => iso ? new Date(iso).toLocaleString("en-US", {timeZone: "America/New_York", hour12: true, month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short"}) : "";
  if (!document.getElementById("si-style")) {
    const st = document.createElement("style"); st.id = "si-style";
    st.textContent = `.si-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:8px 0}.si-row input[type=text],.si-row input[type=password]{flex:1;min-width:200px;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:7px;padding:6px 9px;font:inherit}
.si-ok{color:var(--ok)}.si-bad{color:var(--bad)}.si-tbl{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}.si-tbl td{padding:4px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.si-steps{font-size:13.5px;padding-left:20px;margin:6px 0 10px}.si-steps li{margin-bottom:6px}.si-pre{max-height:240px;overflow:auto;background:var(--panel-2);padding:10px;border-radius:8px;font-size:11.5px;white-space:pre}
.si-card label.si-l{display:block;font-size:12.5px;font-weight:600;color:var(--ink-2);margin:8px 0 3px}.si-card select{border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:7px;padding:6px 9px;font:inherit}`;
    document.head.appendChild(st);
  }
  const checks = (c) => c ? `<table class="si-tbl">${c.checks.map((x) => `<tr><td>${x.ok ? "✅" : "❌"}</td><td><b>${esc(x.check)}</b></td><td>${esc(x.detail)}</td></tr>`).join("")}</table>` : "";
  const note = (m) => m ? `<p style="font-size:13px;margin:6px 0" class="${m.bad ? "si-bad" : "si-ok"}">${esc(m.text)}</p>` : "";

  // ---------------------------------------------------------------- Slack
  // withSettings: also show the few Slack settings needed to get going (the wizard has no Slack settings card).
  function mountSlack(el, {api, withSettings = false, onSlackDoc = null}) {
    const S = {ov: null, doc: null, manifest: null, test: null, msg: null, err: null};
    async function load() {
      try { S.ov = await api("/v1/slack"); S.err = null; if (withSettings) S.doc = await api("/v1/config/slack"); }
      catch (e) { S.ov = null; S.err = e.message; }
      render();
    }
    function render() {
      if (!S.ov) { el.innerHTML = `<div class="si-card"><b>Slack</b><div class="banner err">${esc(S.err || "Loading…")}</div></div>`; return; }
      const o = S.ov, c = o.settings, cr = o.credentials, socket = c.mode !== "http";
      const credRow = (k, need) => `<label class="si-l">${esc(cr[k].label)} ${cr[k].set ? `<span class="tag ok">set</span>` : need ? `<span class="tag warn">needed</span>` : `<span class="tag">not needed</span>`}</label>
        <div class="si-row" style="margin:0"><input type="password" autocomplete="off" data-si-cred="${k}" placeholder="${cr[k].set ? "leave empty to keep the saved value" : "paste it here"}">
        ${cr[k].set ? `<button class="btn small" data-si="clear" data-k="${k}">Remove</button>` : ""}</div>`;
      const state = !o.enabled ? `<span class="tag">off</span>` :
        `<span class="tag ${o.running ? "ok" : "warn"}">${o.running ? "connected" : "on, not running"}</span>` +
        (socket && c.commands_enabled ? ` Socket Mode ${o.socket_connected ? "connected" : o.socket_connected === false ? "<b>not connected</b>" : "not started"}` : "") +
        (o.last_post ? ` · last message ${esc(when(o.last_post))}` : "");
      const settings = withSettings ? `<li>Choose how Slack connects and where notifications go, then save:
          <label class="si-l">Connection</label><select data-si-set="mode"><option value="socket" ${socket ? "selected" : ""}>Socket Mode: Starlab connects out to Slack; no public address needed (recommended)</option><option value="http" ${socket ? "" : "selected"}>HTTP: Slack calls a public HTTPS address of this server</option></select>
          ${socket ? "" : `<label class="si-l">Public HTTPS address of this server</label><div class="si-row" style="margin:0"><input type="text" data-si-set="public_base_url" value="${esc(c.public_base_url || "")}" placeholder="https://starlab.example.org"></div>`}
          <label class="si-l">Channel ID for notifications</label><div class="si-row" style="margin:0"><input type="text" data-si-set="channel_id" value="${esc(c.channel_id || "")}" placeholder="C0123456789"></div>
          <div class="muted" style="font-size:12.5px">In Slack: open the channel → its name → the ID at the bottom (C…).</div>
          <label style="font-weight:400;font-size:13.5px;display:block;margin-top:6px"><input type="checkbox" data-si-set="commands_enabled" ${c.commands_enabled ? "checked" : ""}> The <code>/${esc(c.command || "starlab")}</code> slash command</label>
          <div class="si-row"><button class="btn small" data-si="save-settings">Save these</button><span class="muted" style="font-size:12.5px">Which events are posted: Settings → Slack.</span></div></li>` : "";
      el.innerHTML = `<div class="si-card"><div class="h" style="display:flex;gap:8px;align-items:center"><b>Slack</b> ${state}</div>
        <p class="muted" style="font-size:13px;margin:4px 0">Notifications about recordings in a channel, and a <code>/${esc(c.command || "starlab")}</code> command. Starlab works fully without it.</p>
        ${o.last_error ? `<div class="banner err">${esc(o.last_error)}</div>` : ""}
        ${socket && c.commands_enabled && o.socket_mode_available === false ? `<div class="banner warn">Socket Mode needs an optional package that isn't installed. On the Starlab computer run <code>sudo ./install.sh --with-slack</code> in Starlab's source folder, or choose HTTP, or turn off the slash command (notifications work without it).</div>` : ""}
        ${(o.problems || []).length ? `<div class="banner warn">${o.problems.map(esc).join("<br>")}</div>` : ""}
        <ol class="si-steps">${settings}
          <li><button class="btn small" data-si="manifest">Show the Slack app manifest</button> In Slack, <a href="https://api.slack.com/apps?new_app=1" target="_blank" rel="noopener">Create New App</a> → <i>From an app manifest</i> → choose the workspace → paste it.</li>
          <li><i>Install to Workspace</i>, then copy the <b>Bot User OAuth Token</b> (OAuth &amp; Permissions). ${socket ? "Also create an <b>App-Level Token</b> with the scope <code>connections:write</code> (Basic Information → App-Level Tokens)." : "Also copy the <b>Signing Secret</b> (Basic Information)."}</li>
          <li>Paste them here and save, then invite the app to the channel (<code>/invite @${esc((c.app_name || "Starlab").toLowerCase())}</code>).</li>
          <li><b>Test connection</b>, then <b>Turn on</b>.</li></ol>
        ${S.manifest ? `<div class="si-row"><button class="btn small" data-si="copy">Copy manifest</button><a class="btn small" href="/v1/slack/manifest?download=1">Download</a><button class="btn small" data-si="hide">Hide</button></div><div class="si-pre">${esc(JSON.stringify(S.manifest, null, 2))}</div>` : ""}
        ${credRow("bot_token", true)}${credRow(socket ? "app_token" : "signing_secret", c.commands_enabled)}
        <div class="si-row"><button class="btn small primary" data-si="save-creds">Save tokens</button>
          <button class="btn small" data-si="test">Test connection</button><button class="btn small" data-si="test-post">Send a test message</button>
          ${o.enabled ? `<button class="btn small" data-si="apply">${o.running ? "Restart" : "Start"}</button><button class="btn small" data-si="disable">Turn off</button>` : `<button class="btn small primary" data-si="enable">Turn on</button>`}</div>
        ${note(S.msg)}${checks(S.test)}
        <p class="muted" style="font-size:12px;margin:6px 0 0">Tokens are stored in the server's secrets folder, never shown again, logged or exported.</p></div>`;
    }
    async function saveSlackDoc(changes, noteText) {
      const cur = await api("/v1/config/slack");
      await api("/v1/config/slack", {method: "PUT", body: JSON.stringify({data: {...cur.current.data, ...changes}, base_version: cur.current.version, note: noteText})});
      if (onSlackDoc) await onSlackDoc();
    }
    el.addEventListener("click", async (e) => {
      const a = e.target.closest("[data-si]");
      if (!a || !el.contains(a)) return;
      const act = a.dataset.si; S.msg = null;
      try {
        if (act === "manifest") S.manifest = (await api("/v1/slack/manifest")).manifest;
        else if (act === "hide") S.manifest = null;
        else if (act === "copy") { await navigator.clipboard.writeText(JSON.stringify(S.manifest, null, 2)); S.msg = {text: "Manifest copied."}; }
        else if (act === "save-settings") {
          const ch = {};
          el.querySelectorAll("[data-si-set]").forEach((i) => { ch[i.dataset.siSet] = i.type === "checkbox" ? i.checked : i.value.trim() || (i.dataset.siSet === "public_base_url" ? null : ""); });
          await saveSlackDoc(ch, "Slack set up from the integrations card"); S.msg = {text: "Saved."};
        } else if (act === "save-creds" || act === "clear") {
          const b = {};
          if (act === "clear") b[a.dataset.k] = null;
          else el.querySelectorAll("[data-si-cred]").forEach((i) => { if (i.value.trim()) b[i.dataset.siCred] = i.value.trim(); });
          if (!Object.keys(b).length) S.msg = {text: "Nothing to save: paste a token first.", bad: true};
          else { await api("/v1/slack/credentials", {method: "PUT", body: JSON.stringify(b)}); S.msg = {text: act === "clear" ? "Removed." : "Saved. Now test the connection."}; }
        } else if (act === "test" || act === "test-post") {
          S.test = await api("/v1/slack/test", {method: "POST", body: JSON.stringify({post_message: act === "test-post"})});
          S.msg = S.test.ok ? {text: "Everything checks out."} : {text: "Some checks failed; see below.", bad: true};
        } else if (act === "enable" || act === "disable") {
          await saveSlackDoc({enabled: act === "enable"}, act === "enable" ? "Slack turned on" : "Slack turned off");
          const ov = await api("/v1/slack/apply", {method: "POST", body: "{}"});
          S.msg = act === "disable" ? {text: "Slack is off. The tokens are kept; remove them above if you no longer need them."}
            : ov.running ? {text: "Slack is on."} : {text: "Turned on, but it is not running: " + (ov.last_error || "see above"), bad: true};
        } else if (act === "apply") {
          const ov = await api("/v1/slack/apply", {method: "POST", body: "{}"});
          S.msg = ov.running ? {text: "Slack restarted."} : {text: "Not running: " + (ov.last_error || "see above"), bad: true};
        }
        S.ov = await api("/v1/slack");
      } catch (err) { S.msg = {text: err.message, bad: true}; }
      render();
    });
    load();
    return {reload: load};
  }

  // ---------------------------------------------------------------- Google Calendar
  function mountCalendar(el, {api}) {
    const C = {st: null, test: null, msg: null, err: null, id: null};
    async function load() {
      try { C.st = await api("/v1/integrations/calendar"); C.err = null; if (C.id == null) C.id = C.st.calendar_id || ""; }
      catch (e) { C.st = null; C.err = e.message; }
      render();
    }
    function render() {
      if (!C.st) { el.innerHTML = `<div class="si-card"><b>Google Calendar</b><div class="banner err">${esc(C.err || "Loading…")}</div></div>`; return; }
      const s = C.st, k = s.key || {}, on = s.enabled && s.publish;
      const state = !on ? `<span class="tag">off</span>` : s.running ? `<span class="tag ok">on</span>` : `<span class="tag fault">on, not publishing</span>`;
      const last = s.last_sync ? (s.last_sync.error ? `<span class="si-bad">last sync failed ${esc(when(s.last_sync.at))}: ${esc(s.last_sync.error)}</span>` : `last sync ${esc(when(s.last_sync.at))}`) : "";
      const email = k.client_email ? `<code>${esc(k.client_email)}</code> <button class="btn small" data-sc="copy-email">Copy</button>` : "the service account's e-mail address (shown after you upload the key)";
      const keyStep = s.credentials_in_config_file
        ? `<div class="banner info">This Starlab's calendar credentials are set in its configuration file (<code>${esc(s.config_file)}</code>, ${s.auth_mode === "oauth" ? "a Google account sign-in" : "a key file named there"}). They keep working; enter the calendar ID and test.</div>`
        : `<ol class="si-steps">
            <li>In <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">Google Cloud</a>, pick or create a project and <a href="https://console.cloud.google.com/apis/library/calendar-json.googleapis.com" target="_blank" rel="noopener">enable the Google Calendar API</a>.</li>
            <li><a href="https://console.cloud.google.com/iam-admin/serviceaccounts" target="_blank" rel="noopener">Service accounts</a> → <i>Create service account</i> (no roles needed) → open it → <i>Keys</i> → <i>Add key</i> → <i>Create new key</i> → <i>JSON</i>. Your browser downloads a key file.</li>
            <li>Upload that file here:
              <div class="si-row">${k.present ? `<span class="tag ok">key uploaded</span>` : `<span class="tag warn">needed</span>`}
                <input type="file" accept=".json,application/json" data-sc-file style="max-width:260px">
                ${k.present ? `<button class="btn small" data-sc="remove-key">Remove key</button>` : ""}</div>
              ${k.error ? `<div class="banner err">${esc(k.error)}</div>` : ""}
              <div class="muted" style="font-size:12.5px">Stored in the server's secrets folder (readable only by Starlab). The key is never shown again or exported.</div></li>
            <li>In <a href="https://calendar.google.com/calendar/r/settings" target="_blank" rel="noopener">Google Calendar</a>, open the calendar's <i>Settings and sharing</i> → <i>Share with specific people</i> → add ${email} with <b>Make changes to events</b>.</li>
          </ol>`;
      el.innerHTML = `<div class="si-card"><div class="h" style="display:flex;gap:8px;align-items:center"><b>Google Calendar</b> ${state} <span class="muted" style="font-size:12.5px">${last}</span></div>
        <p class="muted" style="font-size:13px;margin:4px 0">Puts every booked recording on a Google calendar, kept up to date as plans change.</p>
        ${s.client_error ? `<div class="banner err">${esc(s.client_error)}</div>` : ""}
        ${keyStep}
        <label class="si-l">Calendar ID</label>
        <div class="si-row" style="margin:0"><input type="text" data-sc-id value="${esc(C.id)}" placeholder="…@group.calendar.google.com, or your Gmail address for your main calendar"></div>
        <div class="muted" style="font-size:12.5px">In the calendar's <i>Settings and sharing</i> → <i>Integrate calendar</i> → <i>Calendar ID</i>.</div>
        <div class="si-row"><button class="btn small" data-sc="test">Test connection</button>
          ${on ? `<button class="btn small" data-sc="enable">Save calendar ID</button><button class="btn small" data-sc="disable">Turn off</button>` : `<button class="btn small primary" data-sc="enable">Turn on</button>`}</div>
        <div class="muted" style="font-size:12.5px">Test connection adds a test event (dated 1 January 2000) and removes it again. Turn on tests first and takes effect at once.</div>
        ${note(C.msg)}${checks(C.test)}</div>`;
    }
    el.addEventListener("change", async (e) => {
      const f = e.target.closest("[data-sc-file]");
      if (!f || !f.files.length) return;
      C.msg = null;
      try {
        const text = await f.files[0].text();
        C.st = await api("/v1/integrations/calendar/key", {method: "PUT", body: JSON.stringify({key_json: text})});
        C.msg = {text: `Key saved for ${C.st.key.client_email}. Share the calendar with it next.`};
      } catch (err) { C.msg = {text: err.message, bad: true}; }
      render();
    });
    el.addEventListener("input", (e) => { if (e.target.matches("[data-sc-id]")) C.id = e.target.value; });
    el.addEventListener("click", async (e) => {
      const a = e.target.closest("[data-sc]");
      if (!a || !el.contains(a)) return;
      const act = a.dataset.sc; C.msg = null;
      try {
        if (act === "copy-email") { await navigator.clipboard.writeText(C.st.key.client_email); C.msg = {text: "E-mail address copied."}; }
        else if (act === "remove-key") {
          if (!a.dataset.armed) { a.dataset.armed = "1"; a.textContent = "Click again to remove"; return; }
          C.st = await api("/v1/integrations/calendar/key", {method: "DELETE"}); C.test = null; C.msg = {text: "Key removed."};
        } else if (act === "test") {
          a.disabled = true; a.textContent = "Testing…";
          C.test = await api("/v1/integrations/calendar/test", {method: "POST", body: JSON.stringify({calendar_id: C.id})});
          C.msg = C.test.ok ? {text: "Starlab can add events to this calendar."} : {text: "The test failed; see below.", bad: true};
        } else if (act === "enable") {
          a.disabled = true; a.textContent = "Testing…";
          const r = await api("/v1/integrations/calendar/enable", {method: "POST", body: JSON.stringify({calendar_id: C.id})});
          C.test = r.test; C.st = r.status;
          C.msg = r.ok ? {text: "Google Calendar is on. Booked recordings appear within a few minutes."} : {text: "Not turned on: the test failed; see below.", bad: true};
        } else if (act === "disable") {
          const r = await api("/v1/integrations/calendar/disable", {method: "POST", body: "{}"}); C.st = r.status;
          C.msg = {text: "Google Calendar is off. Events already on the calendar stay there."};
        }
        if (act !== "enable" && act !== "disable" && act !== "remove-key") C.st = await api("/v1/integrations/calendar");
      } catch (err) { C.msg = {text: err.message, bad: true}; }
      render();
    });
    load();
    return {reload: load};
  }

  window.StarlabIntegrations = {mountSlack, mountCalendar};
})();
