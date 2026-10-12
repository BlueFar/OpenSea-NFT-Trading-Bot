/* Simulation page: paper trading run by the bot. Uses the helpers app.js shares as window.NFTM. */
(function () {
  "use strict";
  var N = window.NFTM;
  if (!N) return;
  var esc = N.esc, isNum = N.isNum, fmt = N.fmt, money = N.money;

  function $(id) { return document.getElementById(id); }
  var EVENT_COLORS = { placed: "c-accent", raised: "c-accent", lowered: "c-muted", top: "c-good", capped: "c-warn",
                       limit: "c-muted", paused: "c-warn", filled: "c-good", listed: "c-accent", repriced: "c-muted",
                       sold: "c-good", ignored: "c-muted", problem: "c-bad", added: "c-muted", stop: "c-muted",
                       start: "c-muted", remove: "c-muted" };
  var data = null, busy = false;

  // Offers sit 0.1% apart, so prices here show at least 4 significant digits (with the dollar value on hover)
  function price(n, cur) {
    if (!isNum(n)) return "–";
    var d = n === 0 ? 2 : Math.min(8, Math.max(2, 3 - Math.floor(Math.log10(Math.abs(n)))));
    return '<span class="usd" tabindex="0" data-amt="' + n + '" data-cur="' + esc(cur || "") + '">' + esc(fmt(n, d) + " " + (cur || "")) + "</span>";
  }
  function pct(n, d) { return isNum(n) ? fmt(n, d == null ? 0 : d) + "%" : "–"; }
  function hours(sec) {
    if (!isNum(sec) || sec <= 0) return "–";
    var h = sec / 3600;
    return h < 1 ? Math.max(1, Math.round(h * 60)) + " min" : h < 48 ? fmt(h, 1) + " h" : fmt(h / 24, 1) + " days";
  }
  function profitHtml(net, roi, cur) {
    if (!isNum(net)) return "–";
    return '<span class="' + (net >= 0 ? "c-good" : "c-bad") + '">' + (net >= 0 ? "+" : "−") + money(Math.abs(net), cur) +
      "</span>" + (isNum(roi) ? " <small>" + (roi >= 0 ? "+" : "") + fmt(roi, 0) + "%</small>" : "");
  }
  function osLink(slug, text) {
    return '<a href="https://opensea.io/collection/' + encodeURIComponent(slug) + '" target="_blank" rel="noopener">' + esc(text) + "</a>";
  }

  function statusPill(p) {
    if (p.status === "stopped") return '<span class="pill neutral">Stopped</span>';
    if (p.status === "holding") return p.ask ? '<span class="pill good">Bought · listed</span>' : '<span class="pill good">Bought</span>';
    if (!isNum(p.bid) && p.bid_state !== "paused") return '<span class="pill neutral">Starting</span>';
    switch (p.bid_state) {
      case "top": return '<span class="pill good">Best offer</span>';
      case "alone": return '<span class="pill good">Only offer</span>';
      case "capped": return '<span class="pill warn" title="The best real offer is above our limit">Outbid · at our limit</span>';
      case "paused": return '<span class="pill bad">No offer</span>';
      case "no_floor": return '<span class="pill neutral">No floor price</span>';
      default: return '<span class="pill neutral">Starting</span>';
    }
  }

  // Our offer, the best real offer, the floor and our listing over the last 3 days
  function chart(rows, cur) {
    if (!rows || rows.length < 2) return '<div class="sim-chart empty-chart">The chart fills in as the bot checks.</div>';
    var W = 300, H = 74, P = 3, vals = [];
    rows.forEach(function (r) { for (var i = 1; i < 5; i++) if (isNum(r[i])) vals.push(r[i]); });
    if (!vals.length) return "";
    var lo = Math.min.apply(null, vals), hi = Math.max.apply(null, vals);
    if (hi - lo < hi * 0.02) { lo -= hi * 0.01; hi += hi * 0.01; }
    var t0 = rows[0][0], t1 = rows[rows.length - 1][0] || t0 + 1;
    function x(t) { return P + (W - 2 * P) * (t - t0) / Math.max(1, t1 - t0); }
    function y(v) { return P + (H - 2 * P) * (1 - (v - lo) / (hi - lo)); }
    function line(i, cls) {
      var d = "", pen = false;
      rows.forEach(function (r) {
        if (isNum(r[i])) { d += (pen ? "L" : "M") + x(r[0]).toFixed(1) + "," + y(r[i]).toFixed(1); pen = true; } else pen = false;
      });
      return d ? '<path class="' + cls + '" d="' + d + '"/>' : "";
    }
    return '<div class="sim-chart"><svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="none" role="img" aria-label="Floor, best offer and our price over the last 3 days">' +
      line(3, "l-floor") + line(1, "l-bid") + line(2, "l-top") + line(4, "l-ask") + "</svg>" +
      '<div class="sim-legend"><span class="k-floor">Floor</span><span class="k-top">Best other offer</span><span class="k-bid">Our offer</span>' +
      (rows.some(function (r) { return isNum(r[4]); }) ? '<span class="k-ask">Our listing</span>' : "") + "</div></div>";
  }

  function card(p, lastEvent) {
    var cur = p.currency || "", gapMin = data.min_profit_gap_pct;
    var kv;
    if (p.entry_price != null) {
      kv = "<div><dt>Bought for</dt><dd>" + price(p.entry_price, cur) + "</dd></div>" +
        "<div><dt>Listed at</dt><dd>" + (isNum(p.ask) ? price(p.ask, cur) : "–") + "</dd></div>" +
        "<div><dt>Floor</dt><dd>" + money(p.floor, cur) + "</dd></div>" +
        '<div><dt title="If our listing sells, after OpenSea fee, royalty and gas">Profit at our price</dt><dd>' + profitHtml(p.ask_net, p.ask_roi_pct, cur) + "</dd></div>" +
        '<div><dt title="Selling ' + fmt(data.exit_discount_pct, 0) + '% under today\'s floor">At today\'s floor</dt><dd>' + profitHtml(p.now_net, p.now_roi_pct, cur) + "</dd></div>" +
        '<div><dt>Break-even</dt><dd>' + price(p.break_even, cur) + "</dd></div>";
    } else {
      var gapCls = isNum(p.gap_pct) ? (p.gap_pct >= gapMin - 0.05 ? "c-good" : "c-bad") : "";
      kv = "<div><dt>Our offer</dt><dd>" + (isNum(p.bid) ? price(p.bid, cur) : "–") + "</dd></div>" +
        "<div><dt>Best other offer</dt><dd>" + (isNum(p.competitor_top) ? price(p.competitor_top, cur) : '<span class="muted">none</span>') + "</dd></div>" +
        "<div><dt>Floor</dt><dd>" + money(p.floor, cur) + "</dd></div>" +
        '<div><dt title="How far the floor sits above our offer, royalty included. We stop raising below ' + fmt(gapMin, 0) + '%.">Spread at our offer</dt><dd class="' + gapCls + '">' + pct(p.gap_pct, 1) + "</dd></div>" +
        '<div><dt title="The highest offer that keeps a ' + fmt(gapMin, 0) + '% spread">Our limit</dt><dd>' + (isNum(p.max_bid) ? price(p.max_bid, cur) : "–") + "</dd></div>" +
        '<div><dt title="If it fills and sells ' + fmt(data.exit_discount_pct, 0) + '% under today\'s floor">Profit if it fills</dt><dd>' + profitHtml(p.est_net, p.est_roi_pct, cur) + "</dd></div>";
    }
    var meta = [N.chainName(p.chain)];
    if ((p.round || 1) > 1) meta.push("round " + p.round);
    if (p.times_outbid) meta.push("outbid " + p.times_outbid + (p.times_outbid === 1 ? " time" : " times"));
    var btns = (p.status === "stopped"
      ? '<button class="btn" type="button" data-act="start" data-slug="' + esc(p.slug) + '">Start</button>'
      : '<button class="btn ghost" type="button" data-act="stop" data-slug="' + esc(p.slug) + '">Stop</button>') +
      '<button class="btn ghost" type="button" data-act="remove" data-slug="' + esc(p.slug) + '" data-name="' + esc(p.name || p.slug) + '">Remove</button>';
    return '<div class="card sim-card">' +
      '<div class="sim-head">' + N.art(p.slug, p.image) + '<div style="min-width:0;flex:1"><div class="cand-name"><span>' + osLink(p.slug, p.name || p.slug) + "</span></div>" +
      '<div class="cand-meta">' + esc(meta.filter(Boolean).join(" · ")) + "</div></div>" + statusPill(p) + "</div>" +
      '<dl class="kv" style="margin:0">' + kv + "</dl>" +
      chart((data.charts || {})[p.slug], cur) +
      (p.note ? '<p class="sim-note c-bad">' + esc(p.note) + "</p>" : "") +
      (lastEvent ? '<p class="sim-last"><span class="muted">' + esc(N.ago(lastEvent.ts)) + ":</span> " + esc(lastEvent.message) + "</p>" : "") +
      '<div class="cand-foot"><span>' + (p.last_tick_at ? "Checked " + esc(N.ago(p.last_tick_at)) : "Not checked yet") + "</span>" +
      '<span class="sim-btns">' + btns + "</span></div></div>";
  }

  function sumUsd(items, field) {
    var usd = 0, missing = {}, any = false;
    items.forEach(function (t) {
      var v = t[field]; if (!isNum(v)) return;
      var u = N.toUsd(v, t.currency);
      if (u == null) missing[t.currency || "?"] = (missing[t.currency || "?"] || 0) + v; else { usd += u; any = true; }
    });
    return { usd: any ? usd : null, missing: missing };
  }

  function render() {
    var d = data, pos = d.positions || [], trades = d.trades || [], events = d.events || [];

    // Banner
    var banner = "";
    if (!d.enabled) banner = '<div class="banner warn"><span>The simulation is switched off. Set <code>simulation: enabled: true</code> in config/config.yaml.</span></div>';
    else if (d.bot !== "running" && d.bot !== "paused") banner = '<div class="banner warn"><span>The simulation runs inside the bot, which is ' + esc(d.bot) + ". Nothing is being tracked until it runs again (turn it on from Home).</span></div>";
    else if (pos.length && !pos.some(function (p) { return p.last_tick_at; })) banner = '<div class="banner"><span>The bot makes its first check within ' + fmt(d.interval_seconds / 60, 0) + " minutes.</span></div>";
    $("simBanner").innerHTML = banner;

    // Stats
    var bidding = pos.filter(function (p) { return p.status === "bidding"; });
    var onTop = bidding.filter(function (p) { return p.bid_state === "top" || p.bid_state === "alone"; }).length;
    var holding = pos.filter(function (p) { return p.status === "holding" || (p.status === "stopped" && p.entry_price != null); });
    var sold = trades.filter(function (t) { return t.how !== "removed"; });
    var under = holding.filter(function (p) { return isNum(p.now_net) && p.now_net < 0; }).length;
    // Results count bought items that haven't sold as if sold under today's floor: listings never go below
    // break-even, so a losing round shows up as an item that doesn't sell, not as a losing sale.
    var held = holding.map(function (p) { return { net: p.now_net, currency: p.currency }; });
    function totalText(items) {
      var t = sumUsd(items, "net"), miss = Object.keys(t.missing), txt = t.usd != null ? N.usd(t.usd) : "";
      miss.forEach(function (c) { txt += (txt ? " + " : "") + N.moneyText(t.missing[c], c); });
      return { text: txt || "–", usd: t.usd, any: t.usd != null || miss.length > 0 };
    }
    var all = totalText(trades.concat(held)), realised = totalText(trades), open = totalText(held);
    var profitSub = !holding.length ? "after OpenSea fee, royalty and gas" :
      (trades.length ? realised.text + " closed, " : "") + open.text + " on " + holding.length +
      (holding.length === 1 ? " item" : " items") + " still held, valued at today's floor";
    $("simStats").innerHTML =
      '<div class="card stat"><span class="stat-label">Best offer held</span><span class="stat-value num">' + onTop + '</span><span class="stat-sub">of ' + bidding.length + " collections bidding</span></div>" +
      '<div class="card stat"><span class="stat-label">Offers filled</span><span class="stat-value num">' + (trades.length + holding.length) + '</span><span class="stat-sub">' + holding.length + " waiting to sell</span></div>" +
      '<div class="card stat"><span class="stat-label">Sold</span><span class="stat-value num">' + sold.length + '</span><span class="stat-sub">' +
        (under ? under + (under === 1 ? " item" : " items") + " held below break-even" : sold.length ? "never below break-even" : "no round trip yet") + "</span></div>" +
      '<div class="card stat"><span class="stat-label">Profit so far</span><span class="stat-value num ' + (isNum(all.usd) && all.usd < 0 ? "c-bad" : all.any ? "c-good" : "") + '">' + esc(all.text) + '</span><span class="stat-sub">' + esc(profitSub) + "</span></div>";

    // Rules in plain words, from the current settings
    $("simRules").innerHTML = '<div class="section-title"><h2>How it plays</h2><span>Checks every ' + fmt(d.interval_seconds / 60, 0) + " minutes</span></div>" +
      '<ol class="sim-steps">' +
      "<li><strong>Bid.</strong> One offer per collection, " + fmt(d.outbid_step_pct, 1) + "% above the best real offer, so it's the best offer at the smallest price. It follows other bidders up and down.</li>" +
      "<li><strong>Stop at the limit.</strong> We never offer so much that the floor is less than " + fmt(d.min_profit_gap_pct, 0) + "% above our offer (royalty included). If someone bids higher, we stay at that limit.</li>" +
      "<li><strong>Fill.</strong> When a real seller sells one for our offer or less, they would have taken ours instead, so we count it as bought at our offer.</li>" +
      "<li><strong>Sell.</strong> We list it " + fmt(d.exit_discount_pct, 0) + "% under the floor and count it sold when a real buyer pays that or more for a floor item." +
      (d.repeat ? " Then we bid again." : "") + "</li>" +
      "<li><strong>If the floor falls.</strong> We never list below break-even, so the item waits until the floor recovers. Profit so far counts it as if sold under today's floor, so a loss still shows.</li></ol>";

    // Cards
    $("simGrid").innerHTML = pos.length ? pos.map(function (p) { return card(p, p.last_event); }).join("") :
      '<div class="card empty"><h3>No collections yet</h3>Add one below, or list them under <code>simulation: collections</code> in config/config.yaml.</div>';

    // Trades
    $("simTradesSub").textContent = trades.length ? trades.length + (trades.length === 1 ? " round trip" : " round trips") : "Bought and sold";
    $("simTrades").innerHTML = trades.length ? '<div class="tbl-scroll"><table class="tbl sim-trades"><thead><tr><th>Collection</th><th>Bought</th><th>Sold</th><th>Held</th><th>Profit</th></tr></thead><tbody>' +
      trades.map(function (t) {
        return "<tr><td>" + osLink(t.slug, t.name) + '<div class="sub">' + esc(N.fmtDay(t.exit_at)) +
          (t.how === "removed" ? " · removed, valued at the floor" : "") + "</div></td><td>" + price(t.entry_price, t.currency) +
          "</td><td>" + price(t.exit_price, t.currency) + "</td><td>" + esc(hours(t.hold_seconds)) + "</td><td>" + profitHtml(t.net, t.roi_pct, t.currency) + "</td></tr>";
      }).join("") + "</tbody></table></div>" :
      '<p class="chart-note" style="margin-top:0">None yet. A round trip needs a seller to hit our offer, then a buyer to take our listing.</p>';

    // Activity
    var now = new Date();
    $("simFeed").innerHTML = events.length ? events.slice(0, 30).map(function (e) {
      var t = N.fmtDay(e.ts) === N.fmtDay(now) ? N.fmtTime(e.ts) : N.fmtDay(e.ts).split(",")[0];
      return "<li><time>" + esc(t) + '</time><span class="dot ' + (EVENT_COLORS[e.kind] || "c-muted") + '"></span><span><strong>' + esc(e.name) + "</strong>: " + esc(e.message) + "</span></li>";
    }).join("") : '<li style="grid-template-columns:1fr" class="muted">Nothing yet.</li>';
  }

  function load() {
    if (busy) return;
    busy = true;
    N.api("/api/simulation").then(function (r) { data = r; render(); }, function (e) {
      if (!data) $("simGrid").innerHTML = '<div class="card empty">' + esc(e.message || "Couldn't load the simulation.") + "</div>";
    }).then(function () { busy = false; });
  }

  $("simGrid").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-act]"); if (!b) return;
    var act = b.getAttribute("data-act"), slug = b.getAttribute("data-slug");
    if (act === "remove" && !window.confirm("Remove " + (b.getAttribute("data-name") || slug) + " from the simulation? Its trades stay in the history, and an item it bought is counted as sold under today's floor.")) return;
    b.disabled = true;
    N.post("/api/simulation/" + act, { slug: slug }).then(function () {
      N.toast(act === "stop" ? "Stopped." : act === "start" ? "Started. The bot picks it up at its next check." : "Removed.");
      load();
    }, function (err) { N.toast(err.message || "Couldn't do that."); b.disabled = false; });
  });
  $("simAddForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var input = $("simAddInput"), v = input.value.trim(); if (!v) return;
    var btn = this.querySelector("button"); btn.disabled = true;
    N.post("/api/simulation/add", { slug: v }).then(function () {
      input.value = ""; N.toast("Added. The bot places its offer at the next check."); load();
    }, function (err) { N.toast(err.message || "Couldn't add it."); }).then(function () { btn.disabled = false; });
  });

  window.SimPage = { load: load };
  if ((location.hash || "").slice(1) === "sim") load();
})();
