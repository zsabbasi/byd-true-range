"use strict";
// Battery tab: learned usable capacity with its uncertainty and the windows it was learned from.
// All data is written with textContent / DOM APIs only (never as parsed HTML).
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
  function fmt(v, d) { return v === null || v === undefined ? dash : Number(v).toFixed(d === undefined ? 0 : d); }
  function when(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { day: "numeric", month: "short" }) + ", " +
      d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function level(el_, cls) { el_.classList.remove("ok", "warn", "bad"); if (cls) el_.classList.add(cls); }

  const HOW = "Usable capacity = BYD's measured consumption x your km per 1% of battery, over 30–50 km windows that start and end exactly when the battery percent ticks over. Only the middle of the battery (15–92%) is used, and windows never overlap. The percentage compares usable energy with the nominal pack: a new pack may deliver less than nominal, so watch the trend over months, not one number. It trusts BYD's energy figure and the linearity of its battery percent, so confidence is capped near 60% until checked on real drives. An estimate, not a diagnostic.";

  async function load() {
    const box = $("view-battery");
    clear(box);
    box.appendChild(el("div", "muted", "Loading…"));
    let data;
    try {
      const res = await fetch("/api/battery", { cache: "no-store", credentials: "same-origin" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      data = await res.json();
    } catch (e) {
      clear(box);
      box.appendChild(el("div", "stale", "Could not load the battery estimate."));
      return;
    }
    clear(box);
    const b = data.battery;

    const hero = el("section", "hero");
    hero.appendChild(el("div", "label", "Usable battery capacity"));
    const big = el("div", "big small");
    big.appendChild(el("span", null, fmt(b.usable_kwh, 1)));
    big.appendChild(el("span", "unit", " kWh"));
    hero.appendChild(big);
    hero.appendChild(el("div", "sub", "± " + fmt(b.sd_kwh, 1) + " kWh · " + fmt(b.pct_of_nominal, 0) + "% of " + fmt(b.nominal_kwh, 2) + " kWh"));

    const row = el("div", "row");
    const chip = el("span", "chip", b.prior_only ? "Not measured yet" : "Confidence " + b.confidence_pct + "%");
    level(chip, b.prior_only ? "bad" : (b.confidence_pct >= 50 ? "ok" : b.confidence_pct >= 25 ? "warn" : "bad"));
    row.appendChild(chip);
    const detail = b.prior_only
      ? "This is your configured battery size (" + fmt(b.prior_kwh, 1) + " kWh), not a measurement. It is measured after a drive of 30+ km that uses at least 5% of the battery, inside 15–92%, while BYD reports its consumption. "
      : "Measured from " + b.n_samples + (b.n_samples === 1 ? " window" : " windows") + " covering " + fmt(b.km_total, 0) + " km, combined with your configured battery size. ";
    const hi = window.UI.info(detail + HOW, "How this is measured");
    row.appendChild(hi.btn);
    hero.appendChild(row);
    hero.appendChild(hi.tip);

    if (!b.prior_only && b.pct_of_nominal > 105) {
      hero.appendChild(el("div", "note", "Above the nominal size: check battery_kwh in settings.json."));
    } else if (!b.prior_only && b.pct_of_nominal < 80) {
      hero.appendChild(el("div", "note", "Well below the nominal size: check battery_kwh in settings.json."));
    }
    box.appendChild(hero);

    if (data.samples.length) {
      box.appendChild(el("h2", null, "Windows used"));
      for (const s of data.samples.slice().reverse()) {
        const card = el("div", "card");
        const top = el("div", "cardtop");
        top.appendChild(el("span", "when", when(s.when)));
        card.appendChild(top);
        const mid = el("div", "cardmid");
        const c = el("div", "cell");
        c.appendChild(el("span", "v", fmt(s.usable_kwh, 1)));
        c.appendChild(el("span", "u", " kWh ±" + fmt(s.sd_kwh, 1)));
        mid.appendChild(c);
        card.appendChild(mid);
        card.appendChild(el("div", "note", fmt(s.km, 0) + " km, " + s.drop_pct + "% of battery, BYD said " + fmt(s.byd_kwh_per_100, 1) + " kWh/100 km"));
        box.appendChild(card);
      }
    }
  }

  window.loadBattery = load;
})();
