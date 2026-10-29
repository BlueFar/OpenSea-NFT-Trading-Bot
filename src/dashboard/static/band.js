/* $50–$500 page and Buy List. Uses the helpers app.js shares as window.NFTM. */
(function () {
  "use strict";
  var N = window.NFTM;
  if (!N) return;
  var esc = N.esc, isNum = N.isNum, fmt = N.fmt, money = N.money;
  function $(id) { return document.getElementById(id); }

  function signedUsd(n) {
    if (!isNum(n)) return "–";
    return (n > 0 ? "+" : n < 0 ? "−" : "") + N.usd(Math.abs(n)).replace("−", "");
  }
  function cls(n) { return !isNum(n) ? "" : n < 0 ? "c-bad" : n > 0 ? "c-good" : ""; }
  function osLink(slug, text) {
    return '<a href="https://opensea.io/collection/' + encodeURIComponent(slug) + '" target="_blank" rel="noopener">' + esc(text) + "</a>";
  }
  function days(n) { return isNum(n) ? (n < 1 ? fmt(n * 24, 0) + " h" : fmt(n, 1) + " d") : "–"; }
  function price(n, cur) {
    if (!isNum(n)) return "–";
    var d = n === 0 ? 2 : Math.min(8, Math.max(2, 3 - Math.floor(Math.log10(Math.abs(n)))));
    return '<span class="usd" tabindex="0" data-amt="' + n + '" data-cur="' + esc(cur || "") + '">' + esc(fmt(n, d) + " " + (cur || "")) + "</span>";
  }

  // ---------- Ready checklist and the "+ Buy List" button (also used on the Simulation and Final list pages) ----------
  function readyBox(ready) {
    if (!ready) return "";
    var n = ready.checks.filter(function (c) { return c.ok === true; }).length;
    var pill = ready.ready ? '<span class="pill good">Ready</span>' : '<span class="pill ' + (n >= ready.checks.length - 1 ? "warn" : "neutral") + '">' + n + " of " + ready.checks.length + "</span>";
    return '<details class="ready"><summary>' + pill + '</summary><ul class="ready-list">' + ready.checks.map(function (c) {
      var mark = c.ok === true ? '<span class="rk ok" aria-label="passes">✓</span>' : c.ok === false ? '<span class="rk no" aria-label="fails">✗</span>' : '<span class="rk unk" aria-label="not known yet">?</span>';
      return "<li>" + mark + "<span>" + esc(c.label) + (c.detail ? ' <small class="muted">' + esc(c.detail) + "</small>" : "") + "</span></li>";
    }).join("") + "</ul></details>";
  }
  function buyButton(slug, onList) {
    if (onList) return '<span class="pill neutral on-buy" title="On the Buy List">On Buy List</span>';
    return '<button class="btn ghost buy-add" type="button" data-buy-add="' + esc(slug) + '">+ Buy List</button>';
  }
  window.BuyList = { button: buyButton, readyBox: readyBox };

  function reloadVisible() {
    var page = (location.hash || "#home").slice(1);
    if (page === "band") loadBand();
    if (page === "buy") loadBuy();
    if (window.SimPage) {
      if (page === "sim") window.SimPage.load();
      if (page === "final") window.SimPage.loadFinal();
    }
  }
  document.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-buy-add]"); if (!b) return;
    var slug = b.getAttribute("data-buy-add");
    b.disabled = true;
    N.post("/api/buylist/add", { slug: slug }).then(function (r) {
      var miss = (r.ready && r.ready.failing) || [];
      N.toast(r.ready && r.ready.ready ? "Added to the Buy List. It passes every readiness check." :
        "Added to the Buy List, not ready yet: " + miss.length + (miss.length === 1 ? " check fails" : " checks fail") + ".");
      reloadVisible();
    }, function (err) { N.toast(err.message || "Couldn't add it."); b.disabled = false; });
  });

  // ---------- $50–$500 page ----------
  var band = null, bandBusy = false, view = { chain: null, finalOnly: false, readyOnly: false };
  try { var saved = JSON.parse(localStorage.getItem("bandView") || "null"); if (saved) view = Object.assign(view, saved); } catch (e) {}
  function saveView() { try { localStorage.setItem("bandView", JSON.stringify(view)); } catch (e) {} }

  function readyOf(slug) {
    var s = (band.scorecard || []).filter(function (r) { return r.slug === slug; })[0];
    return s ? s.ready : null;
  }
  function keep(r) {
    if (view.chain && r.chain !== view.chain) return false;
    if (view.finalOnly && !r.final) return false;
    if (view.readyOnly) { var rd = readyOf(r.slug); if (!rd || !rd.ready) return false; }
    return true;
  }
  function nameCell(r, sub) {
    var tags = [N.chainName(r.chain)];
    if (r.final) tags.push("Final list");
    return '<td><div class="band-name">' + N.art(r.slug, r.image) + "<div>" + osLink(r.slug, r.name || r.slug) +
      '<div class="sub">' + esc(tags.filter(Boolean).join(" · ")) + (sub ? " · " + sub : "") + "</div></div></div></td>";
  }
  function stat(label, value, sub, valueCls, tip) {
    return '<div class="card stat"' + (tip ? ' title="' + esc(tip) + '"' : "") + '><span class="stat-label">' + esc(label) + '</span><span class="stat-value num ' + (valueCls || "") + '">' + value +
      '</span><span class="stat-sub">' + sub + "</span></div>";
  }

  function moneyPanel(bids, held, trades) {
    var offers = 0, onTop = 0, cost = 0, value = 0, unvalued = 0, win = 0, loss = 0, nWin = 0, nLoss = 0;
    bids.forEach(function (b) { offers += b.bid_usd; if (b.bid_state === "top" || b.bid_state === "alone") onTop++; });
    held.forEach(function (h) { cost += h.cost_usd; if (isNum(h.value_usd)) value += h.value_usd; else unvalued++; });
    trades.forEach(function (t) { if (t.net_usd > 0) { win += t.net_usd; nWin++; } else if (t.net_usd < 0) { loss += t.net_usd; nLoss++; } });
    var realised = win + loss, tied = realised - cost - offers, total = realised + (value - cost);
    return stat("In open offers", N.usd(offers), bids.length + (bids.length === 1 ? " offer" : " offers") + ", " + onTop + " the best offer. If every offer filled at once, the wallet would need this much.", "", "Our offers waiting to be taken, at today's coin prices") +
      stat("In held items", N.usd(cost), held.length + (held.length === 1 ? " item" : " items") + " bought, worth " + N.usd(value) + " today" + (unvalued ? " (" + unvalued + " with no price yet)" : ""), "", "What we paid. Today's worth is the lower of the last real sale and our usual price under the floor, after fees and gas") +
      stat("Profit from winning sales", signedUsd(win), nWin + (nWin === 1 ? " profitable sale" : " profitable sales") + ", after OpenSea fee, royalty and gas", cls(win)) +
      stat("Profit after losses", signedUsd(realised), nLoss ? signedUsd(loss) + " from " + nLoss + (nLoss === 1 ? " losing sale" : " losing sales") : "No losing sales", cls(realised), "Winning sales minus losing sales (stop-losses)") +
      stat("After money tied up", signedUsd(tied), "Profit after losses, minus the money in held items and in open offers", cls(tied), "What you'd be up or down if the held items were worth nothing and every offer were spent") +
      stat("Overall profit or loss", signedUsd(total), "Profit after losses, plus what held items would bring today, minus what they cost", cls(total));
  }

  function renderBand() {
    var d = band, b = d.band;
    $("bandSub").textContent = "The Simulation's collections where our offer, or what we paid, is $" + fmt(b.min_usd, 0) + "–$" + fmt(b.max_usd, 0) +
      " at the coin price when it was bought. Same market data as the Simulation page (strategy A); nothing extra is checked.";
    var chains = {};
    (d.scorecard || []).forEach(function (r) { if (r.chain) chains[r.chain] = 1; });
    if (view.chain && !chains[view.chain]) view.chain = null;
    $("bandFilters").innerHTML = '<button class="chip" type="button" data-bf="chain" data-v="" aria-pressed="' + (!view.chain) + '">All chains</button>' +
      Object.keys(chains).sort().map(function (c) { return '<button class="chip" type="button" data-bf="chain" data-v="' + esc(c) + '" aria-pressed="' + (view.chain === c) + '">' + esc(N.chainName(c)) + "</button>"; }).join("") +
      '<span class="chip-gap"></span><button class="chip" type="button" data-bf="finalOnly" aria-pressed="' + view.finalOnly + '">Final list only</button>' +
      '<button class="chip" type="button" data-bf="readyOnly" aria-pressed="' + view.readyOnly + '">Ready only</button>';

    var bids = d.bids.filter(keep), held = d.held.filter(keep), trades = d.trades.filter(keep), score = d.scorecard.filter(keep);
    var miss = (d.missing_prices || []).length ? '<div class="banner warn"><span>No dollar price for ' + esc(d.missing_prices.join(", ")) + " right now, so collections priced in it are left out.</span></div>" : "";
    $("bandBanner").innerHTML = miss;
    $("bandMoney").innerHTML = moneyPanel(bids, held, trades);

    $("bandBidsSub").textContent = bids.length + (bids.length === 1 ? " collection" : " collections");
    $("bandBids").innerHTML = bids.length ? '<div class="tbl-scroll"><table class="tbl sim-trades band-tbl"><thead><tr><th>Collection</th><th>Our offer</th><th>Best other</th><th>Floor</th><th title="Days our offer has been up">Bidding</th><th>Outbid</th><th>Ready</th><th></th></tr></thead><tbody>' +
      bids.map(function (r) {
        var st = r.bid_state === "top" || r.bid_state === "alone" ? '<span class="c-good">best offer</span>' : r.bid_state === "capped" ? '<span class="c-warn">at our limit</span>' : "";
        return "<tr>" + nameCell(r, st) + '<td data-l="Our offer"><strong>' + N.usd(r.bid_usd) + '</strong><div class="sub">' + price(r.bid, r.currency) + "</div></td>" +
          '<td data-l="Best other">' + (isNum(r.competitor_top) ? price(r.competitor_top, r.currency) : '<span class="muted">none</span>') + "</td>" +
          '<td data-l="Floor">' + price(r.floor, r.currency) + "</td>" + '<td data-l="Bidding">' + days(r.bid_days) + "</td>" +
          '<td data-l="Outbid">' + fmt(r.times_outbid, 0) + "</td>" + '<td data-l="Ready">' + readyBox(readyOf(r.slug)) + "</td>" +
          '<td data-l="">' + buyButton(r.slug, r.on_buy_list) + "</td></tr>";
      }).join("") + "</tbody></table></div>" : '<p class="chart-note">No open offers in this range.</p>';

    $("bandHeldSub").textContent = held.length + (held.length === 1 ? " item" : " items") + " · a badge after " + fmt(b.long_hold_days, 0) + " days (information only)";
    $("bandHeld").innerHTML = held.length ? '<div class="tbl-scroll"><table class="tbl sim-trades band-tbl"><thead><tr><th>Collection</th><th>Paid</th><th>Held</th><th>Break-even</th><th title="The lower of the last real sale and our usual price under the floor, after fees and gas">Worth today</th><th>Profit or loss</th><th></th></tr></thead><tbody>' +
      held.map(function (r) {
        var stage = r.stop_loss_at ? '<span class="c-bad">stop-loss, step ' + (r.sl_steps + 1) + "</span>" : r.under_since ? '<span class="c-warn">under break-even since ' + esc(N.fmtDay(r.under_since)) + "</span>" : isNum(r.ask) ? "listed at " + price(r.ask, r.currency) : "";
        var basis = r.value_basis === "last_sale" ? "at the last sale" : r.value_basis === "stop_loss" ? "at our stop-loss listing" : "under today's floor";
        return "<tr>" + nameCell(r, stage) + '<td data-l="Paid"><strong>' + N.usd(r.cost_usd) + '</strong><div class="sub">' + price(r.entry_price, r.currency) + "</div></td>" +
          '<td data-l="Held">' + days(r.days_held) + (r.long_hold ? ' <span class="pill warn">long hold</span>' : "") + "</td>" +
          '<td data-l="Break-even">' + price(r.break_even, r.currency) + "</td>" +
          '<td data-l="Worth today">' + N.usd(r.value_usd) + '<div class="sub">' + esc(basis) + "</div></td>" +
          '<td data-l="Profit or loss" class="' + cls(r.net_usd) + '">' + signedUsd(r.net_usd) + "</td>" +
          '<td data-l="">' + buyButton(r.slug, r.on_buy_list) + "</td></tr>";
      }).join("") + "</tbody></table></div>" : '<p class="chart-note">Nothing held in this range.</p>';

    $("bandTradesSub").textContent = trades.length + (trades.length === 1 ? " sale" : " sales");
    $("bandTrades").innerHTML = trades.length ? '<div class="tbl-scroll"><table class="tbl sim-trades band-tbl"><thead><tr><th>Collection</th><th>Paid</th><th>Sold</th><th>Held</th><th>Profit</th></tr></thead><tbody>' +
      trades.map(function (t) {
        var how = t.how === "stoploss" ? "stop-loss, a buyer took our listing" : t.how === "stoploss_offer" ? "stop-loss, took the best offer" : "";
        return "<tr>" + nameCell(t, esc(N.fmtDay(t.exit_at)) + (how ? " · " + esc(how) : "")) +
          '<td data-l="Paid"><strong>' + N.usd(t.cost_usd) + '</strong><div class="sub">' + price(t.entry_price, t.currency) + "</div></td>" +
          '<td data-l="Sold">' + price(t.exit_price, t.currency) + "</td>" + '<td data-l="Held">' + days(t.days_held) + "</td>" +
          '<td data-l="Profit" class="' + cls(t.net_usd) + '">' + signedUsd(t.net_usd) + (isNum(t.roi_pct) ? ' <small>' + (t.roi_pct >= 0 ? "+" : "") + fmt(t.roi_pct, 0) + "%</small>" : "") + "</td></tr>";
      }).join("") + "</tbody></table></div>" : '<p class="chart-note">No sales in this range yet.</p>';

    var nReady = score.filter(function (r) { return r.ready.ready; }).length;
    $("bandScoreSub").textContent = score.length + " collections · " + nReady + " ready";
    $("bandScore").innerHTML = score.length ? '<div class="tbl-scroll"><table class="tbl sim-trades band-tbl"><thead><tr><th>Collection</th><th>Rounds</th><th>Profit</th>' +
      '<th title="Average profit for each day an item was held">% a day held</th><th title="Days of bidding for each offer that filled">Days per fill</th><th>Days to sell</th><th title="From the bot\'s latest rule check">Sales in 7 days</th><th>Ready</th><th></th></tr></thead><tbody>' +
      score.map(function (r) {
        return "<tr>" + nameCell(r, "") + '<td data-l="Rounds">' + r.rounds + (r.rounds ? ' <small class="muted">' + r.wins + " won</small>" : "") + "</td>" +
          '<td data-l="Profit" class="' + cls(r.profit_usd) + '">' + (r.rounds ? signedUsd(r.profit_usd) : "–") + "</td>" +
          '<td data-l="% a day held">' + (isNum(r.pct_per_day) ? fmt(r.pct_per_day, 1) + "%" : "–") + "</td>" +
          '<td data-l="Days per fill">' + (isNum(r.days_per_fill) ? fmt(r.days_per_fill, 1) : r.fills ? "–" : "no fill in " + days(r.bid_days)) + "</td>" +
          '<td data-l="Days to sell">' + days(r.avg_days_to_sell) + "</td>" + '<td data-l="Sales in 7 days">' + (isNum(r.sales_7d) ? r.sales_7d : "–") + "</td>" +
          '<td data-l="Ready">' + readyBox(r.ready) + "</td>" + '<td data-l="">' + buyButton(r.slug, r.on_buy_list) + "</td></tr>";
      }).join("") + "</tbody></table></div>" : '<p class="chart-note">Nothing in this range yet.</p>';
  }

  function loadBand() {
    if (bandBusy) return;
    if (document.querySelector("#page-band details[open]") && band) return; // don't close a checklist being read
    bandBusy = true;
    N.api("/api/band").then(function (r) { band = r; renderBand(); }, function (e) {
      if (!band) $("bandMoney").innerHTML = '<div class="card empty">' + esc(e.message || "Couldn't load this page.") + "</div>";
    }).then(function () { bandBusy = false; });
  }
  $("bandFilters").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-bf]"); if (!b || !band) return;
    var k = b.getAttribute("data-bf");
    if (k === "chain") view.chain = b.getAttribute("data-v") || null; else view[k] = !view[k];
    saveView(); renderBand();
  });

  // ---------- Buy List page ----------
  var buy = null, buyBusy = false;
  function settingsLine(s) {
    if (!s || !isNum(s.min_profit_gap_pct)) return "";
    var ex = s.exit || {};
    return "Offer " + fmt(s.outbid_step_pct, 1) + "% over the best offer, never closer than a " + fmt(s.min_profit_gap_pct, 0) + "% spread to the floor · list " +
      fmt(s.exit_discount_pct, 0) + "% under the floor · stop-loss after " + fmt(ex.stop_loss_hours, 0) + " h under break-even, down to " + fmt(ex.stop_loss_lowest_pct, 0) + "% under the market floor";
  }
  function buyRow(it) {
    var cur = it.currency || "", s = it.suggest || {};
    var now = !s.action ? "Not in the simulation right now, so there's nothing to copy" :
      s.action === "offer" ? "Right now the bot would offer " + N.moneyText(s.price, cur) + (isNum(s.usd) ? " (" + N.usd(s.usd) + ")" : "") :
      "Holding the simulation's item: the bot would list it at " + N.moneyText(s.price, cur) + (isNum(s.usd) ? " (" + N.usd(s.usd) + ")" : "");
    var rec = it.rounds ? it.rounds + (it.rounds === 1 ? " round" : " rounds") + ", " + it.wins + " profitable, " + (isNum(it.realised_usd) ? signedUsd(it.realised_usd) : N.moneyText(it.realised, cur)) : "No completed round yet";
    if (isNum(it.sales_7d)) rec += " · " + it.sales_7d + (it.sales_7d === 1 ? " sale" : " sales") + " in 7 days";
    var warns = (it.warnings || []).map(function (w) { return '<p class="buy-warn c-' + (w.level === "bad" ? "bad" : "warn") + '">' + esc(w.text) + "</p>"; }).join("");
    var level = (it.warnings || []).some(function (w) { return w.level === "bad"; }) ? " bad" : (it.warnings || []).length ? " warn" : "";
    var active = it.enabled && !buy.paused_all;
    return '<div class="fin-row buy-row' + level + '">' + N.art(it.slug, it.image) +
      '<div class="fin-main"><div class="cand-name"><span>' + osLink(it.slug, it.name || it.slug) + "</span></div>" +
      '<div class="cand-meta">' + esc([N.chainName(it.chain), "added " + N.fmtDay(it.added_at), it.ready_at_add ? "ready when added" : "added before it was ready"].filter(Boolean).join(" · ")) + "</div>" +
      (it.note ? '<p class="fin-note">' + esc(it.note) + "</p>" : "") + warns +
      '<p class="fin-state">' + esc(rec) + "</p>" + '<p class="fin-state">' + esc(now) + "</p>" +
      '<p class="buy-settings">' + esc(settingsLine(it.settings)) + "</p>" +
      '<div class="buy-ready">' + readyBox(it.ready) + "</div></div>" +
      '<div class="fin-side"><div class="big-switch"><label for="sw-' + esc(it.slug) + '">' + (active ? "Will trade" : it.enabled ? "On (all paused)" : "Paused") + "</label>" +
      '<button class="switch" id="sw-' + esc(it.slug) + '" type="button" role="switch" aria-checked="' + it.enabled + '" data-buy-enable="' + esc(it.slug) + '"></button></div>' +
      '<button class="btn ghost" type="button" data-buy-remove="' + esc(it.slug) + '" data-name="' + esc(it.name || it.slug) + '">Remove</button></div></div>';
  }
  function renderBuy() {
    var n = buy.items.length, on = buy.tradable.length;
    $("buyTop").innerHTML = '<div class="buy-top-text"><strong>' + (buy.paused_all ? "Everything is paused" : on + " of " + n + (n === 1 ? " collection" : " collections") + " will be traded") +
      '</strong><span class="muted">The trading bot isn\'t built yet, so nothing is bought or sold. When it is, it will only trade the rows switched on here, with the settings saved on each row, and nothing while Pause all is on.</span></div>' +
      '<div class="big-switch"><label for="buyPauseAll">Pause all</label><button class="switch lg" id="buyPauseAll" type="button" role="switch" aria-checked="' + buy.paused_all + '"></button></div>';
    $("buyList").innerHTML = n ? buy.items.map(buyRow).join("") :
      '<div class="empty"><h3>Nothing on the Buy List yet</h3>Use "+ Buy List" on the Simulation, $50–$500 or Final list pages, or add one below.</div>';
  }
  function loadBuy() {
    if (buyBusy) return;
    if (document.querySelector("#page-buy details[open]") && buy) return;
    buyBusy = true;
    N.api("/api/buylist").then(function (r) { buy = r; renderBuy(); }, function (e) {
      if (!buy) $("buyList").innerHTML = '<div class="empty">' + esc(e.message || "Couldn't load the Buy List.") + "</div>";
    }).then(function () { buyBusy = false; });
  }
  $("page-buy").addEventListener("click", function (e) {
    var t = e.target.closest("button"); if (!t || !buy) return;
    if (t.id === "buyPauseAll") {
      var p = t.getAttribute("aria-checked") !== "true";
      t.disabled = true;
      N.post("/api/buylist/pause_all", { paused: p }).then(function () { N.toast(p ? "Paused: the bot will trade nothing." : "Pause all is off."); loadBuy(); },
        function (err) { N.toast(err.message || "Couldn't change that."); }).then(function () { t.disabled = false; });
    } else if (t.hasAttribute("data-buy-enable")) {
      var on = t.getAttribute("aria-checked") !== "true";
      t.disabled = true;
      N.post("/api/buylist/enable", { slug: t.getAttribute("data-buy-enable"), enabled: on }).then(function () { loadBuy(); },
        function (err) { N.toast(err.message || "Couldn't change that."); t.disabled = false; });
    } else if (t.hasAttribute("data-buy-remove")) {
      if (!window.confirm("Take " + t.getAttribute("data-name") + " off the Buy List? It stays in the simulation.")) return;
      t.disabled = true;
      N.post("/api/buylist/remove", { slug: t.getAttribute("data-buy-remove") }).then(function () { N.toast("Removed from the Buy List."); loadBuy(); },
        function (err) { N.toast(err.message || "Couldn't remove it."); t.disabled = false; });
    }
  });
  $("buyAddForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var input = $("buyAddInput"), note = $("buyAddNote"), v = input.value.trim(); if (!v) return;
    var btn = this.querySelector("button"); btn.disabled = true;
    N.post("/api/buylist/add", { slug: v, note: note.value.trim() }).then(function (r) {
      input.value = ""; note.value = "";
      N.toast(r.ready && r.ready.ready ? "Added. It passes every readiness check." : "Added, not ready yet.");
      loadBuy();
    }, function (err) { N.toast(err.message || "Couldn't add it."); }).then(function () { btn.disabled = false; });
  });

  window.BandPage = { load: loadBand, loadBuy: loadBuy };
  var start = (location.hash || "").slice(1);
  if (start === "band") loadBand();
  if (start === "buy") loadBuy();
})();
