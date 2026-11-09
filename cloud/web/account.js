"use strict";
// Account page: who is signed in, how it is stored, and the sign-in form. The password is typed here and goes to this app's
// own server on the same computer, and from there to BYD: never anywhere else, and the server refuses it from a tunnel.
// All data is written with textContent / DOM APIs only (never as parsed HTML). The password is never logged or kept.
(function () {
  const $ = (id) => document.getElementById(id);
  const dash = "—";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

  async function getJSON(url, ms = 10000) {
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), ms);
    try {
      const res = await fetch(url, { cache: "no-store", credentials: "same-origin", signal: ctl.signal });
      if (!res.ok) throw new Error("HTTP " + res.status);
      return await res.json();
    } finally { clearTimeout(to); }
  }

  // POST JSON with the header the server requires (it refuses a request a web page in your browser could forge).
  async function postJSON(url, body, ms = 40000) {
    const ctl = new AbortController();
    const to = setTimeout(() => ctl.abort(), ms);
    try {
      const res = await fetch(url, {
        method: "POST", cache: "no-store", credentials: "same-origin", signal: ctl.signal,
        headers: { "Content-Type": "application/json", "X-BYD-App": "1" }, body: JSON.stringify(body),
      });
      let data = {};
      try { data = await res.json(); } catch (e) { /* not JSON */ }
      return { status: res.status, data };
    } finally { clearTimeout(to); }
  }

  const WARN_WHY = "A dedicated sub-account can be revoked without touching your main account, and a leaked sub-account login can do far less damage. BYD may also sign your phone app out when the same account logs in somewhere else. In the BYD app, share your car with a second BYD account and sign in here with that second account. This app only reads data, but a login is a login.";
  const WHERE_PW = "Your password is typed on this computer and sent to BYD over HTTPS. It goes nowhere else. It is stored encrypted on this computer so the app can stay signed in and sign itself back in when BYD's session expires. The app refuses to accept a password from a tunnel, a proxy or the network. Sign out deletes it.";
  const WHY_REMOTE = "A password typed into a web page passes through the network and the machine running this server, and whoever runs the server could capture it. So the server only accepts a BYD password typed on its own computer, never over the network (that includes your phone, a tunnel or a proxy). If this is someone else's server, you cannot give it your BYD login, and it reads one BYD account only. To track your own car: run your own copy (it is open source, see the README), or ask the host to sign in with a sub-account you share from your BYD app. The host can sign in on the server itself, or over an SSH tunnel: ssh -L 8765:localhost:8765 user@server, then open http://localhost:8765.";
  const REGION_HELP = "BYD runs one server per region and an account only exists on its own. If BYD says 'wrong email or password' with correct details, try another region. Confirmed so far: Pakistan uses Singapore / Asia-Pacific. The other regions are listed because their servers exist, not because every country has been tested.";

  // Says exactly why this page counts as remote, so the owner knows what to change.
  function remoteWhy(a) {
    if (a.remote_reason === "host") return "You opened this app as \"" + (a.host || "?") + "\". Open http://localhost:8765 on the computer that runs it instead.";
    if (a.remote_reason === "proxy") return "This request came through a tunnel or proxy (such as ngrok or Tailscale). Open http://localhost:8765 on the computer that runs the app instead.";
    return "This request came from another device (a phone or another computer). Use the computer that runs the app, or an SSH tunnel to its localhost.";
  }

  function poller(a) {
    const p = a.poller || {}, st = p.state;
    if (!a.signed_in) return { text: "Not signed in", cls: "warn" };
    if (st === "ok") return { text: "Signed in, reading your car", cls: "ok" };
    if (st === "auth-failed") return { text: "BYD rejected this sign-in" + (p.detail ? ": " + p.detail : "") + ". It will not retry by itself; sign in again below.", cls: "bad" };
    if (st === "error") return { text: "Signed in, but BYD cannot be reached right now. Retrying.", cls: "warn" };
    if (st === "no-vehicle") return { text: "Signed in, but this BYD account has no car", cls: "warn" };
    if (st === "starting") return { text: "Signed in, starting to read your car…", cls: "warn" };
    return { text: "Signed in", cls: "ok" };
  }

  function renderStatus(a) {
    const box = $("acct-status");
    clear(box);
    const card = el("section", "hero");
    card.appendChild(el("div", "label", "Account"));
    const pl = poller(a);
    const row = el("div", "row");
    const rejected = a.signed_in && a.poller && a.poller.state === "auth-failed";
    row.appendChild(el("span", "chip " + pl.cls, rejected ? "Rejected by BYD" : a.signed_in ? "Signed in" : "Not signed in"));
    card.appendChild(row);
    card.appendChild(el("div", rejected ? "note errmsg" : "sub", pl.text));
    if (a.signed_in) {
      const bits = [a.user, a.region ? "region " + a.region.toUpperCase() : null, a.country].filter(Boolean);
      card.appendChild(el("div", "note", bits.join(" · ")));
      if (a.store) {
        const how = el("div", "note", "Stored encrypted on the server. ");
        const hi = window.UI.info(a.store.protection + (a.store.key_source ? ". Key: " + a.store.key_source : "") + ". It protects a copied or backed-up data folder; it cannot protect against someone who controls the server itself.", "How the sign-in is stored");
        how.appendChild(hi.btn);
        card.appendChild(how);
        card.appendChild(hi.tip);
      }
    } else if (a.store && a.store.present && !a.store.readable) {
      card.appendChild(el("div", "note", "A stored sign-in exists but cannot be read: " + (a.store.reason || "unknown reason") + "."));
    }
    if (a.signed_in) {
      const out = el("button", "pill", "Sign out");
      out.type = "button";
      let armed = null;
      out.addEventListener("click", async () => {
        if (!armed) {
          out.textContent = a.local ? "Tap again to sign out and delete the stored login" : "Tap again to sign out. You can only sign back in on the computer that runs the app";
          armed = setTimeout(() => { armed = null; out.textContent = "Sign out"; }, 5000);
          return;
        }
        clearTimeout(armed);
        out.disabled = true;
        const r = await postJSON("/api/logout", {});
        if (r.status === 200) { $("view-account").dataset.built = ""; loadAccount(); }
        else { out.disabled = false; out.textContent = (r.data && r.data.message) || "Could not sign out"; }
      });
      card.appendChild(out);
    }
    box.appendChild(card);
  }

  function field(labelText, input) {
    const wrap = el("label", "field");
    wrap.appendChild(el("span", "flabel", labelText));
    wrap.appendChild(input);
    return wrap;
  }

  const BATTERY_HELP = "Your car's battery size is the starting point for every range figure, and the Battery tab then measures the real usable energy from your drives. BYD's cloud does not tell this app which size you have, so you choose it. Standard Range (49.92 kWh) is the usual one in Pakistan, as reported by owners here; check yours in the car's specifications or paperwork.";

  // Battery size: chosen by the owner, never silently assumed. In Pakistan (by time zone) Standard Range is preselected, and said to be.
  function buildBattery(a) {
    const b = a.battery || { kwh: 49.92, set: false, options: [] };
    const box = el("section", "hero");
    box.appendChild(el("div", "label", "Battery size"));
    const head = el("div", "note", b.set ? "Set to " + b.kwh + " kWh." : "Not set yet: the app is assuming " + b.kwh + " kWh (Standard Range). ");
    const bi = window.UI.info(BATTERY_HELP, "About the battery size");
    head.appendChild(bi.btn);
    box.appendChild(head);
    box.appendChild(bi.tip);

    const sel = el("select"); sel.name = "battery";
    const opts = (b.options || []).map((o) => ({ value: String(o.kwh), text: o.label + " (" + o.kwh + " kWh)" }));
    opts.push({ value: "other", text: "Other size…" });
    const known = opts.some((o) => o.value === String(b.kwh));
    if (!b.set) opts.unshift({ value: "", text: "Choose…" });
    for (const o of opts) { const op = el("option", null, o.text); op.value = o.value; sel.appendChild(op); }
    const other = el("input"); other.type = "number"; other.step = "0.01"; other.min = "20"; other.max = "150"; other.placeholder = "kWh";
    other.hidden = true;
    const note = el("div", "note", "");
    const save = el("button", "primary", "Save battery size"); save.type = "button";
    const msg = el("div", "formmsg"); msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite");

    let tz = "";
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { /* none */ }
    if (b.set) sel.value = known ? String(b.kwh) : "other";
    else if (tz === "Asia/Karachi" && (b.options || []).length) {
      sel.value = String(b.options[0].kwh);
      note.textContent = "Preselected from your time zone: owners in Pakistan report Standard Range is the usual one. Check yours, then save.";
    } else sel.value = "";
    if (sel.value === "other") { other.hidden = false; other.value = b.kwh; }

    function value() {
      if (sel.value === "other") return other.value === "" ? NaN : Number(other.value);
      return sel.value === "" ? NaN : Number(sel.value);
    }
    function refresh() { const v = value(); save.disabled = !(v >= 20 && v <= 150); }
    sel.addEventListener("change", () => { other.hidden = sel.value !== "other"; note.textContent = ""; refresh(); });
    other.addEventListener("input", refresh);
    refresh();
    save.addEventListener("click", async () => {
      const v = value();
      if (!(v >= 20 && v <= 150)) return;
      save.disabled = true;
      msg.className = "formmsg"; msg.textContent = "Saving…";
      let r;
      try { r = await postJSON("/api/settings", { battery_kwh: v }, 15000); } catch (e) { r = { status: 0, data: { message: "No answer from the app." } }; }
      if (r.status === 200 && r.data && r.data.ok) {
        msg.className = "formmsg okmsg";
        msg.textContent = "Saved: " + r.data.battery_kwh + " kWh. Range figures now use it.";
        $("view-account").dataset.built = "";
        setTimeout(loadAccount, 1200);
      } else {
        msg.className = "formmsg errmsg";
        msg.textContent = (r.data && r.data.message) || "Could not save (" + (r.status || "no answer") + ").";
        refresh();
      }
    });

    box.appendChild(field("Pack", sel));
    box.appendChild(other);
    box.appendChild(note);
    box.appendChild(save);
    box.appendChild(msg);
    return box;
  }

  function buildForm(a, onDone) {
    const form = el("form", "signin");
    form.setAttribute("autocomplete", "on");
    form.noValidate = true;

    // The warning is not a tooltip: it is the first thing on the form and must be seen.
    const warn = el("div", "warnbox");
    warn.setAttribute("role", "note");
    warn.appendChild(el("div", "warnhead", "Never use your primary BYD login here."));
    warn.appendChild(el("div", null, "Use a dedicated sub-account: share your car with a second BYD account and sign in with that one."));
    const wi = window.UI.info(WARN_WHY, "Why a sub-account");
    warn.appendChild(wi.btn);
    warn.appendChild(wi.tip);
    form.appendChild(warn);

    const privacy = el("div", "note", "Your password goes to BYD and nowhere else. ");
    const pi = window.UI.info(WHERE_PW, "Where your password goes");
    privacy.appendChild(pi.btn);
    form.appendChild(privacy);
    form.appendChild(pi.tip);

    if (!a.local) {
      const remote = el("div", "stale");
      const line = el("div", null, "Sign in is turned off on this device: this server does not take a BYD password over the network. ");
      const ri2 = window.UI.info(WHY_REMOTE, "Why you cannot sign in here");
      line.appendChild(ri2.btn);
      remote.appendChild(line);
      remote.appendChild(ri2.tip);
      remote.appendChild(el("div", "note", remoteWhy(a)));
      form.appendChild(remote);
    }

    const user = el("input"); user.type = "text"; user.name = "username"; user.autocomplete = "username"; user.placeholder = "email or phone";
    user.setAttribute("autocapitalize", "off"); user.spellcheck = false; user.maxLength = 128;
    const pw = el("input"); pw.type = "password"; pw.name = "password"; pw.autocomplete = "current-password"; pw.maxLength = 256; pw.spellcheck = false;
    const region = el("select"); region.name = "region";
    for (const r of a.regions) {
      const o = el("option", null, r.label + " (" + r.id + ")");
      o.value = r.id;
      region.appendChild(o);
    }
    // Pakistan is the default: country PK and its confirmed server (Singapore / Asia-Pacific) are filled in and the server address is shown.
    const countrySel = el("select"); countrySel.name = "country-choice";
    const pk = el("option", null, "Pakistan (PK)"); pk.value = "PK"; countrySel.appendChild(pk);
    const oth = el("option", null, "Other country…"); oth.value = "other"; countrySel.appendChild(oth);
    const country = el("input"); country.type = "text"; country.name = "country"; country.maxLength = 2; country.placeholder = "two letters, e.g. AE";
    country.setAttribute("autocapitalize", "characters"); country.spellcheck = false;
    country.hidden = true;
    const regionNote = el("div", "note", "");
    const serverLine = el("div", "note", "");
    const confirmedPK = (a.confirmed && a.confirmed.PK) || "sg";
    function countryCode() { return countrySel.value === "other" ? country.value.trim().toUpperCase() : "PK"; }
    function showServer() {
      serverLine.textContent = "BYD server: https://dilinkappoversea-" + region.value + ".byd.auto";
    }
    function applyCountry() {
      const other = countrySel.value === "other";
      country.hidden = !other;
      region.disabled = !other;                       // Pakistan: the confirmed server, not editable
      regionNote.textContent = "";
      if (!other) { region.value = confirmedPK; regionNote.textContent = "Confirmed for Pakistan: Singapore / Asia-Pacific."; }
      else if (a.confirmed && a.confirmed[countryCode()]) { region.value = a.confirmed[countryCode()]; regionNote.textContent = "Confirmed for " + countryCode() + "."; }
      showServer();
      refresh();
    }
    countrySel.addEventListener("change", applyCountry);
    country.addEventListener("input", applyCountry);
    region.addEventListener("change", showServer);

    form.appendChild(field("BYD account", user));
    form.appendChild(field("Password", pw));
    form.appendChild(field("Country", countrySel));
    form.appendChild(country);
    const rrow = field("Region", region);
    const ri = window.UI.info(REGION_HELP, "About regions");
    rrow.appendChild(ri.btn);
    form.appendChild(rrow);
    form.appendChild(ri.tip);
    form.appendChild(regionNote);
    form.appendChild(serverLine);

    const ack = el("input"); ack.type = "checkbox"; ack.name = "ack"; ack.id = "ack";
    const ackLabel = el("label", "check");
    ackLabel.appendChild(ack);
    ackLabel.appendChild(el("span", null, " This is a dedicated sub-account, not my primary BYD login."));
    form.appendChild(ackLabel);

    const msg = el("div", "formmsg");
    msg.setAttribute("role", "status");
    msg.setAttribute("aria-live", "polite");
    const go = el("button", "primary", "Sign in to BYD");
    go.type = "submit";
    const hint = el("div", "note", "");
    form.appendChild(go);
    form.appendChild(hint);
    form.appendChild(msg);

    // The button says why it is disabled. Browsers can autofill a field without telling the page, so the state is also re-checked
    // on every kind of edit event and twice a second while the form is on screen.
    let busy = false;
    function refresh() {
      if (busy) return;
      const missing = [];
      if (!user.value.trim()) missing.push("BYD account");
      if (!pw.value) missing.push("password");
      if (!/^[A-Za-z]{2}$/.test(countryCode())) missing.push("two-letter country code");
      if (!ack.checked) missing.push("the sub-account confirmation");
      go.disabled = !a.local || missing.length > 0;
      if (!a.local) hint.textContent = remoteWhy(a);
      else hint.textContent = missing.length ? "Still needed: " + missing.join(", ") + "." : "";
    }
    for (const ev of ["input", "change", "keyup", "blur"]) form.addEventListener(ev, refresh, true);
    const watch = setInterval(() => { if (!form.isConnected) clearInterval(watch); else refresh(); }, 500);
    region.value = confirmedPK;
    applyCountry();

    form.addEventListener("submit", async (ev) => {
      ev.preventDefault();
      if (go.disabled) return;
      go.disabled = true;
      busy = true;
      hint.textContent = "";
      msg.className = "formmsg";
      msg.textContent = "Signing in to BYD… this can take up to 25 seconds.";
      const body = {
        username: user.value.trim(), password: pw.value, region: region.value, country: countryCode(),
        acknowledged: ack.checked, time_zone: (Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"),
      };
      pw.value = "";                                      // the password does not stay in the page
      let r;
      try { r = await postJSON("/api/login", body); } catch (e) { r = { status: 0, data: { message: "No answer from the app. Is it running?" } }; }
      body.password = "";
      const d = r.data || {};
      if (r.status === 200 && d.ok) {
        msg.className = "formmsg okmsg";
        const v = (d.vehicles || []).map((x) => (x.model || "car") + " …" + (x.vin_tail || "")).join(", ");
        msg.textContent = "Signed in" + (v ? ": " + v : "") + ". The app is now reading your car.";
        setTimeout(onDone, 1500);
        return;
      }
      msg.className = "formmsg errmsg";
      if (r.status === 401 && d.error === "rejected") {
        msg.textContent = "BYD rejected the sign-in" + (d.code ? " (code " + d.code + ")" : "") + ": " + (d.message || "") + " " + (d.hint || "");
      } else {
        msg.textContent = d.message || ("Sign in failed (" + (r.status || "no answer") + ").");
      }
      busy = false;
      refresh();
    });
    return form;
  }

  async function loadAccount() {
    const box = $("view-account");
    let a;
    try { a = await getJSON("/api/account"); } catch (e) {
      if (!box.dataset.built) { clear(box); box.appendChild(el("div", "stale", "Could not load the account.")); }
      window.UI.refreshLater("account", 15000, "#/account", loadAccount);
      return;
    }
    const wasSignedIn = box.dataset.signed === "1";
    if (!box.dataset.built || wasSignedIn !== a.signed_in) {
      clear(box);
      box.appendChild(el("h1", "tripname", "BYD account"));
      const st = el("div"); st.id = "acct-status";
      box.appendChild(st);
      box.appendChild(buildBattery(a));
      box.appendChild(el("h2", null, a.signed_in ? "Replace the sign-in" : "Sign in to BYD"));
      box.appendChild(buildForm(a, () => { box.dataset.built = ""; location.hash = "#/"; }));
      box.dataset.built = "1";
      box.dataset.signed = a.signed_in ? "1" : "0";
    }
    renderStatus(a);
    // keep the status line live (the poller reports in as it signs in, retries, or is rejected) without touching the form
    window.UI.refreshLater("account", a.poller && a.poller.state === "starting" ? 3000 : 10000, "#/account", loadAccount);
  }

  window.loadAccount = loadAccount;
})();
