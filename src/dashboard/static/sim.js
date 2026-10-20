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
                       start: "c-muted", remove: "c-muted", trial: "c-warn", kept: "c-good", missed: "c-warn",
                       stepdown: "c-warn", stepup: "c-good", grace: "c-warn", under: "c-warn",
                       stoploss: "c-bad", undercut: "c-warn", recovered: "c-good", cooloff: "c-muted" };
  var data = null, busy = false;

  // Offers sit 0.1% apart, so prices here show at least 4 significant digits (with the dollar value on hover)
  function price(n, cur) {
    if (!isNum(n)) return "–";
    var d = n === 0 ? 2 : Math.min(8, Math.max(2, 3 - Math.floor(Math.log10(Math.abs(n)))));
    return '<span class="usd" tabindex="0" data-amt="' + n + '" data-cur="' + esc(cur || "") + '">' + esc(fmt(n, d) + " " + (cur || "")) + "</span>";
  }
  function ordinal(n) { return n === 1 ? "" : n === 2 ? "2nd " : n === 3 ? "3rd " : n + "th "; }
  function isSale(t) { return t.how === "sold" || t.how === "stoploss" || t.how === "stoploss_offer"; }
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

  function bOn() { return !!(data && data.strategy_b && data.strategy_b.enabled); }
  function bTag() { return '<span class="tag-b" title="Strategy B: the same collections, with a limit that steps down while our offer gets no fills">B</span>'; }
  function limitOf(b, fallback) { return isNum(b && b.limit_pct) ? b.limit_pct : fallback; }

  // How often the bot looks at this collection: sooner where someone outbid us lately, less often where it's quiet
  function cadence(p) {
    var s = p.check_every;
    if (!isNum(s) || p.status === "stopped") return "";
    var why = s < data.interval_seconds ? ", outbid lately" : s > data.interval_seconds ? ", quiet" : "";
    return "every " + hours(s) + why;
  }

  function missedText(p) {
    var n = (p.missed_fills || 0) + (p.missed_above_limit || 0);
    if (!n) return "";
    return "missed " + n + (n === 1 ? " fill" : " fills") + (p.missed_fills ? " (" + p.missed_fills + " within our limit)" : "");
  }

  // Strategy B's own offer or item on this collection, one line under strategy A's numbers
  function bLine(p) {
    var b = p.b, cur = p.currency || "";
    if (!bOn() || !b || b.status === "removed") return "";
    var txt;
    if (b.entry_price != null) {
      txt = "bought for " + price(b.entry_price, cur) + (isNum(b.ask) ? ", listed at " + price(b.ask, cur) : "") +
        " · " + profitHtml(b.now_net, b.now_roi_pct, cur) + " at today's floor";
    } else if (b.status === "stopped") txt = "stopped";
    else if (b.bid_state === "paused") txt = "no offer: the spread is too thin even at this limit";
    else if (isNum(b.bid)) {
      txt = "offer " + price(b.bid, cur) + (b.bid_state === "capped" ? ", at its limit" :
        b.bid_state === "top" || b.bid_state === "alone" ? ", the best offer" : "");
    } else txt = "places its offer at the next check";
    var extra = missedText(b);
    return '<p class="sim-b">' + bTag() + fmt(limitOf(b, data.min_profit_gap_pct), 0) + "% limit · " + txt +
      (extra ? " · " + esc(extra) : "") + "</p>";
  }

  // Track record: does our offer get taken, does the item sell quickly, does it make money
  var VERDICTS = { proven: ["Proven", "good"], not_yet: ["Not proven yet", "neutral"], slow: ["Slow", "warn"], losing: ["Losing", "bad"] };
  var VERDICT_RANK = { proven: 0, not_yet: 1, slow: 2, losing: 3 };
  function verdictPill(rec) {
    if (!rec) return "";
    var v = VERDICTS[rec.verdict] || [rec.verdict, "neutral"];
    return '<span class="pill ' + v[1] + '" title="' + esc(rec.why) + '">' + esc(v[0]) + "</span>";
  }
  function days(n) { return isNum(n) ? (n < 1 ? fmt(n * 24, 0) + " h" : fmt(n, 1) + " d") : "–"; }

  function renderRecord(d) {
    var box = $("simRecord"), pos = (d.positions || []).filter(function (p) { return p.record; });
    if (!pos.length) { box.hidden = true; return; }
    pos.sort(function (x, y) {
      var a = x.record, b = y.record;
      return (VERDICT_RANK[a.verdict] - VERDICT_RANK[b.verdict]) || ((b.roi_per_day || 0) - (a.roi_per_day || 0)) ||
        ((b.fills_per_week || 0) - (a.fills_per_week || 0)) || String(x.name || x.slug).localeCompare(String(y.name || y.slug));
    });
    var counts = {};
    pos.forEach(function (p) { counts[p.record.verdict] = (counts[p.record.verdict] || 0) + 1; });
    var sub = ["proven", "not_yet", "slow", "losing"].filter(function (k) { return counts[k]; })
      .map(function (k) { return counts[k] + " " + VERDICTS[k][0].toLowerCase(); }).join(" · ");
    box.hidden = false;
    box.innerHTML = '<div class="section-title"><h2>Track record</h2><span>' + esc(sub) + "</span></div>" +
      '<p class="chart-note" style="margin:0 0 10px">Proven: ' + "2 or more sales, none at a loss, and in profit overall. Losing: down overall (items held valued at today's floor) after a losing trade, or with an item held 3 days or more below what it cost. " +
      "Slow: no fill in 14 days of bidding. These are strategy A's results" + (bOn() ? "; B's verdict is shown beside it." : ".") + "</p>" +
      '<div class="tbl-scroll"><table class="tbl sim-trades sim-record-tbl"><thead><tr><th>Collection</th><th>Verdict</th>' +
      '<th title="Fills for each week our offer was up">Fills a week</th><th title="Average time from buying to selling">Days to sell</th>' +
      '<th title="Average profit a sale, after fees and gas">Profit a sale</th><th title="Sales so far, plus items held valued at today\'s floor">Profit so far</th></tr></thead><tbody>' +
      pos.map(function (p) {
        var r = p.record, b = p.b && p.b.record;
        function cell(label, html) { return '<td data-l="' + label + '"><span class="cv">' + html + "</span></td>"; }
        return "<tr><td>" + osLink(p.slug, p.name || p.slug) + '<div class="sub">' + esc(r.why) + (r.underwater && r.why.indexOf("down") < 0 ? " · item held below break-even" : "") + "</div></td>" +
          cell("Verdict", verdictPill(r) + (bOn() && b ? '<span class="sub nowrap">' + bTag() + esc((VERDICTS[b.verdict] || [b.verdict])[0]) + "</span>" : "")) +
          cell("Fills a week", (isNum(r.fills_per_week) ? fmt(r.fills_per_week, 1) : "–") + '<span class="sub nowrap">' + r.fills + (r.fills === 1 ? " fill" : " fills") + " in " + days(r.bid_days) + "</span>") +
          cell("Days to sell", days(r.days_to_sell)) +
          cell("Profit a sale", (isNum(r.roi_per_trade) ? (r.roi_per_trade >= 0 ? "+" : "") + fmt(r.roi_per_trade, 0) + "%" : "–") +
            (isNum(r.roi_per_day) ? '<span class="sub nowrap">' + fmt(r.roi_per_day, 1) + "% a day held</span>" : "")) +
          cell("Profit so far", isNum(r.total_net) ? profitHtml(r.total_net, null, r.currency) : "–") + "</tr>";
      }).join("") + "</tbody></table></div>";
  }

  // A Final list pick that stopped passing gets one simulated round (7 days at most) to show a profit
  function trialText(p) {
    if (p.trial_state === "on") return "stopped passing: on trial until its round ends" +
      (p.trial_started_at ? " (by " + N.fmtDay(p.trial_started_at + 7 * 86400) + ")" : "");
    if (p.trial_state === "kept") return "stopped passing, kept after a profitable trial";
    return "";
  }

  function statusPill(p) {
    if (p.status === "stopped") return '<span class="pill neutral">Stopped</span>';
    if (p.status === "holding" && p.stop_loss_at) return '<span class="pill bad">Stop-loss · listed</span>';
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
    if ((data.final || []).indexOf(p.slug) >= 0) meta.push("Final list");
    else if (p.source === "candidate") meta.push("Candidate");
    if (p.leaving) meta.push("off the Candidates list: leaves once the item sells");
    else if (p.dropped_at && p.status === "bidding") meta.push("off the Candidates list: offer kept until " + N.fmtTime(p.dropped_at + (data.candidate_grace_hours || 0) * 3600) + " in case it passes again");
    var ex = data.exit || {};
    if (p.status === "holding" && p.stop_loss_at) meta.push("stop-loss since " + N.fmtTime(p.stop_loss_at) + (isNum(p.market_floor) ? ", market floor " + fmt(p.market_floor, 4) + " " + cur : ""));
    else if (p.status === "holding" && p.under_since) meta.push("under break-even since " + N.fmtTime(p.under_since) + ": stop-loss at " + N.fmtTime(p.under_since + (ex.stop_loss_hours || 0) * 3600) + " if it stays there");
    else if (p.status === "bidding" && p.cool_off_until && p.cool_off_until * 1000 > Date.now()) meta.push("cool-off after a stop-loss: bidding again " + N.fmtTime(p.cool_off_until));
    var trial = trialText(p);
    if (trial) meta.push(trial);
    if ((p.round || 1) > 1) meta.push("round " + p.round);
    if (p.times_outbid) meta.push("outbid " + p.times_outbid + (p.times_outbid === 1 ? " time" : " times"));
    meta.push(missedText(p));
    var btns = (p.status === "stopped"
      ? '<button class="btn" type="button" data-act="start" data-slug="' + esc(p.slug) + '">Start</button>'
      : '<button class="btn ghost" type="button" data-act="stop" data-slug="' + esc(p.slug) + '">Stop</button>') +
      '<button class="btn ghost" type="button" data-act="remove" data-slug="' + esc(p.slug) + '" data-name="' + esc(p.name || p.slug) + '">Remove</button>';
    return '<div class="card sim-card">' +
      '<div class="sim-head">' + N.art(p.slug, p.image) + '<div style="min-width:0;flex:1"><div class="cand-name"><span>' + osLink(p.slug, p.name || p.slug) + "</span></div>" +
      '<div class="cand-meta">' + esc(meta.filter(Boolean).join(" · ")) + "</div></div>" + statusPill(p) + "</div>" +
      '<dl class="kv" style="margin:0">' + kv + "</dl>" + bLine(p) +
      chart((data.charts || {})[p.slug], cur) +
      (p.note ? '<p class="sim-note c-bad">' + esc(p.note) + "</p>" : "") +
      (lastEvent ? '<p class="sim-last"><span class="muted">' + esc(N.ago(lastEvent.ts)) + ":</span> " + esc(lastEvent.message) + "</p>" : "") +
      '<div class="cand-foot"><span>' + (p.last_tick_at ? "Checked " + esc(N.ago(p.last_tick_at)) : "Not checked yet") +
      (cadence(p) ? " · " + esc(cadence(p)) : "") + "</span>" +
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

  function totalText(items) {
    var t = sumUsd(items, "net"), miss = Object.keys(t.missing), txt = t.usd != null ? N.usd(t.usd) : "";
    miss.forEach(function (c) { txt += (txt ? " + " : "") + N.moneyText(t.missing[c], c); });
    return { text: txt || "–", usd: t.usd, any: t.usd != null || miss.length > 0 };
  }

  // One strategy's results: offers held on top, fills, sales, profit (held items valued at today's floor)
  function results(rows, trades) {
    var bidding = rows.filter(function (p) { return p.status === "bidding"; });
    var holding = rows.filter(function (p) { return p.status === "holding" || (p.status === "stopped" && p.entry_price != null); });
    var held = holding.map(function (p) { return { net: p.now_net, currency: p.currency }; });
    return {
      bidding: bidding.length, holding: holding.length,
      onTop: bidding.filter(function (p) { return p.bid_state === "top" || p.bid_state === "alone"; }).length,
      filled: trades.length + holding.length,
      sold: trades.filter(isSale).length,
      missed: rows.reduce(function (n, p) { return n + (p.missed_fills || 0) + (p.missed_above_limit || 0); }, 0),
      total: totalText(trades.concat(held))
    };
  }

  // Strategy A against strategy B, side by side
  function renderCompare(d) {
    var box = $("simCompare");
    if (!bOn()) { box.hidden = true; return; }
    var pos = d.positions || [], b = d.strategy_b;
    var a = results(pos, d.trades || []);
    var bb = results(pos.map(function (p) { return p.b; }).filter(function (x) { return x && x.status !== "removed"; }), d.trades_b || []);
    function row(name, rule, r) {
      return "<tr><td><strong>" + name + '</strong><div class="sub">' + esc(rule) + '</div></td><td data-l="Best offer held">' + r.onTop + " of " + r.bidding +
        '</td><td data-l="Filled">' + r.filled + '</td><td data-l="Sold">' + r.sold + '</td><td data-l="Missed fills">' + r.missed +
        '</td><td data-l="Profit so far" class="' + (isNum(r.total.usd) && r.total.usd < 0 ? "c-bad" : r.total.any ? "c-good" : "") + '">' + esc(r.total.text) + "</td></tr>";
    }
    box.hidden = false;
    box.innerHTML = '<div class="section-title"><h2>Two strategies</h2><span>Same collections, same market data</span></div>' +
      '<div class="tbl-scroll"><table class="tbl sim-trades"><thead><tr><th>Strategy</th><th>Best offer held</th><th>Filled</th><th>Sold</th>' +
      '<th title="Sales where a seller took someone else\'s higher collection offer, under the floor">Missed fills</th><th>Profit so far</th></tr></thead><tbody>' +
      row("Strategy A", "fixed " + fmt(d.min_profit_gap_pct, 0) + "% limit", a) +
      row("Strategy B", "limit steps down " + fmt(b.step_pct, 0) + "% a day with no fill, to " + fmt(Math.min(b.lowest_gap_pct, d.min_profit_gap_pct), 0) + "%", bb) +
      "</tbody></table></div>" +
      '<p class="chart-note" style="margin:8px 0 0">Profit counts items still held as if sold ' + fmt(d.exit_discount_pct, 0) + "% under today's floor. The Stats above are strategy A.</p>";
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
    var sold = trades.filter(isSale);
    var under = holding.filter(function (p) { return isNum(p.now_net) && p.now_net < 0; }).length;
    // Results count bought items that haven't sold as if sold under today's floor, so an item waiting under
    // break-even (or in a stop-loss) shows its loss before it sells.
    var held = holding.map(function (p) { return { net: p.now_net, currency: p.currency }; });
    var all = totalText(trades.concat(held)), realised = totalText(trades), open = totalText(held);
    var profitSub = !holding.length ? "after OpenSea fee, royalty and gas" :
      (trades.length ? realised.text + " closed, " : "") + open.text + " on " + holding.length +
      (holding.length === 1 ? " item" : " items") + " still held, valued at today's floor";
    $("simStats").innerHTML =
      '<div class="card stat"><span class="stat-label">Best offer held</span><span class="stat-value num">' + onTop + '</span><span class="stat-sub">of ' + bidding.length + " collections bidding</span></div>" +
      '<div class="card stat"><span class="stat-label">Offers filled</span><span class="stat-value num">' + (trades.length + holding.length) + '</span><span class="stat-sub">' + holding.length + " waiting to sell</span></div>" +
      '<div class="card stat"><span class="stat-label">Sold</span><span class="stat-value num">' + sold.length + '</span><span class="stat-sub">' +
        (under ? under + (under === 1 ? " item" : " items") + " held below break-even" : sold.length ? "never below break-even" : "no round trip yet") + "</span></div>" +
      '<div class="card stat"><span class="stat-label">Profit so far' + (bOn() ? " (A)" : "") + '</span><span class="stat-value num ' + (isNum(all.usd) && all.usd < 0 ? "c-bad" : all.any ? "c-good" : "") + '">' + esc(all.text) + '</span><span class="stat-sub">' + esc(profitSub) + "</span></div>";

    renderCompare(d);
    renderRecord(d);

    // Rules in plain words, from the current settings
    if ($("simRules").contains(document.activeElement)) return renderRest();
    var ex = d.exit || {};
    var grace = d.candidate_grace_hours ? " keeps its offer for " + fmt(d.candidate_grace_hours, 0) + " hours in case it passes again (most do), then leaves (after selling anything bought)" : " leaves straight away (after selling anything bought)";
    var scope = d.include_candidates ? "every collection on the Candidates page, the Final list and the ones added here" : "the collections below";
    $("simRules").innerHTML = '<div class="section-title"><h2>How it plays</h2><form class="sim-every" id="simEvery"><label for="simEveryIn">Check every</label> ' +
      '<input id="simEveryIn" type="number" min="1" max="1440" step="1" value="' + fmt(d.interval_seconds / 60, 0).replace(/,/g, "") + '"> minutes ' +
      '<button class="btn ghost" type="submit">Save</button></form></div>' +
      '<p class="chart-note" style="margin:0 0 10px">Simulating ' + scope + ". A candidate that stops passing the bot's rules" + grace + ". Losing the verified tick, a listings jump or a big 1-day floor drop removes it at once. A Final list pick gets one more round to show a profit. Checking more often answers outbids sooner but slows the candidate scan, which shares the same OpenSea allowance.</p>" +
      '<ol class="sim-steps">' +
      "<li><strong>Bid.</strong> One offer per collection, " + fmt(d.outbid_step_pct, 1) + "% above the best real offer, so it's the best offer at the smallest price. It follows other bidders up and down.</li>" +
      "<li><strong>Stop at the limit.</strong> We never offer so much that the floor is less than " + fmt(d.min_profit_gap_pct, 0) + "% above our offer (royalty included). If someone bids higher, we stay at that limit.</li>" +
      "<li><strong>Fill.</strong> When a real seller sells one for our offer or less, they would have taken ours instead, so we count it as bought at our offer.</li>" +
      "<li><strong>Sell.</strong> We list it " + fmt(d.exit_discount_pct, 0) + "% under the floor and count it sold when a real buyer pays that or more for a floor item." +
      (d.repeat ? " Then we bid again." : "") + "</li>" +
      (ex.lower_after_days ? "<li><strong>Not selling.</strong> Unsold after " + fmt(ex.lower_after_days, 0) + " days, the listing goes " + fmt(ex.lower_step_pct, 0) +
        "% lower each day, never below break-even.</li>" : "") +
      "<li><strong>Stop-loss.</strong> The market floor is the " + ordinal(ex.market_floor_rank) + "cheapest listing, one per seller, so one panic listing can't set it. Once it has stayed under break-even for " +
        fmt(ex.stop_loss_hours, 0) + " hours, we sell at a loss: listed " + fmt(ex.stop_loss_start_pct, 0) + "% under the market floor, " + fmt(ex.lower_step_pct, 0) +
        "% lower each day (or when a cheaper listing sells first), never more than " + fmt(ex.stop_loss_lowest_pct, 0) + "% under. The best offer is taken once it reaches " +
        fmt(ex.accept_offer_pct, 0) + "% of our listing; lower offers are lowballs and are refused. Back above break-even for " + fmt(ex.recover_hours, 0) +
        " hours, it returns to normal selling. After a stop-loss the collection gets no new offer for " + fmt(ex.cool_off_days, 0) + " days.</li>" +
      "<li><strong>Missed fills.</strong> A sale under the floor and above our offer is usually a seller taking someone else's higher collection offer. We count those, and whether our limit would have covered that price.</li>" +
      "<li><strong>How often.</strong> A collection where someone outbid us in the last day is checked every " + esc(hours(d.contested_interval_seconds)) +
      ", one where we've held the best offer unchallenged for a day every " + esc(hours(d.quiet_interval_seconds)) + ", the rest every " + esc(hours(d.interval_seconds)) + ".</li>" +
      (bOn() ? "<li><strong>Strategy B.</strong> A second set of offers on the same collections, judged on the same sales. When its offer has sat at the limit for " +
        fmt(d.strategy_b.every_hours, 0) + " hours with no fill, the limit drops " + fmt(d.strategy_b.step_pct, 0) + "% (" + fmt(d.min_profit_gap_pct, 0) + "% to " +
        fmt(d.min_profit_gap_pct - d.strategy_b.step_pct, 0) + "% and so on, never below " + fmt(Math.min(d.strategy_b.lowest_gap_pct, d.min_profit_gap_pct), 0) +
        "%), so it can outbid others. After a sale it steps back up " + fmt(d.strategy_b.step_pct, 0) + "%. Buttons on a card apply to both.</li>" : "") +
      "</ol>";
    renderRest();
  }

  function renderRest() {
    var d = data, pos = d.positions || [], trades = d.trades || [], events = d.events || [];

    // Cards
    $("simGrid").innerHTML = pos.length ? pos.map(function (p) { return card(p, p.last_event); }).join("") :
      '<div class="card empty"><h3>No collections yet</h3>Add one below, or list them under <code>simulation: collections</code> in config/config.yaml.</div>';

    // Trades, both strategies, newest first
    var tradesB = bOn() ? d.trades_b || [] : [];
    var nA = trades.length;
    trades = trades.concat(tradesB).sort(function (x, y) { return (y.exit_at || 0) - (x.exit_at || 0); });
    $("simTradesSub").textContent = trades.length ? nA + (nA === 1 ? " round trip" : " round trips") + (bOn() ? " (A), " + tradesB.length + " (B)" : "") : "Bought and sold";
    $("simTrades").innerHTML = trades.length ? '<div class="tbl-scroll"><table class="tbl sim-trades"><thead><tr><th>Collection</th><th>Bought</th><th>Sold</th><th>Held</th><th>Profit</th></tr></thead><tbody>' +
      trades.map(function (t) {
        return "<tr><td>" + (t.strategy === "B" ? bTag() : "") + osLink(t.slug, t.name) + '<div class="sub">' + esc(N.fmtDay(t.exit_at)) +
          (t.how === "removed" ? " · removed, valued at the floor" : t.how === "stoploss" ? " · stop-loss, a buyer took our listing" : t.how === "stoploss_offer" ? " · stop-loss, took the best offer" : "") + "</div></td><td>" + price(t.entry_price, t.currency) +
          "</td><td>" + price(t.exit_price, t.currency) + "</td><td>" + esc(hours(t.hold_seconds)) + "</td><td>" + profitHtml(t.net, t.roi_pct, t.currency) + "</td></tr>";
      }).join("") + "</tbody></table></div>" :
      '<p class="chart-note" style="margin-top:0">None yet. A round trip needs a seller to hit our offer, then a buyer to take our listing.</p>';

    // Activity
    var now = new Date();
    $("simFeed").innerHTML = events.length ? events.slice(0, 30).map(function (e) {
      var t = N.fmtDay(e.ts) === N.fmtDay(now) ? N.fmtTime(e.ts) : N.fmtDay(e.ts).split(",")[0];
      return "<li><time>" + esc(t) + '</time><span class="dot ' + (EVENT_COLORS[e.kind] || "c-muted") + '"></span><span>' + (e.strategy === "B" ? bTag() : "") + "<strong>" + esc(e.name) + "</strong>: " + esc(e.message) + "</span></li>";
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

  $("page-sim").addEventListener("submit", function (e) {
    if (e.target.id !== "simEvery") return;
    e.preventDefault();
    var v = parseFloat($("simEveryIn").value);
    N.post("/api/simulation/settings", { interval_minutes: v }).then(function () {
      N.toast("Saved. The bot checks every " + fmt(v, 0) + " minutes from its next round."); document.activeElement.blur(); load();
    }, function (err) { N.toast(err.message || "Couldn't save."); });
  });

  // ---------- Final list ----------
  var finBusy = false, finData = null;
  function finProfit(label, r, cur) {
    var held = r.entry_price != null;
    var total = (isNum(r.realised_net) ? r.realised_net : 0) + (held && isNum(r.now_net) ? r.now_net : 0);
    var has = isNum(r.realised_net) || (held && isNum(r.now_net));
    var sub = [];
    if (r.trades) sub.push(r.trades + (r.trades === 1 ? " sale" : " sales") + (isNum(r.realised_net) ? " " + (r.realised_net >= 0 ? "+" : "−") + N.moneyText(Math.abs(r.realised_net), cur) : ""));
    if (held) sub.push("1 held, " + (isNum(r.now_net) ? (r.now_net >= 0 ? "+" : "−") + N.moneyText(Math.abs(r.now_net), cur) : "–") + " at today's floor");
    if (!has && isNum(r.est_net)) sub.push("if it fills: " + (r.est_net >= 0 ? "+" : "−") + N.moneyText(Math.abs(r.est_net), cur) + " (" + fmt(r.est_roi_pct, 0) + "%)");
    return '<div class="fin-profit"><span class="muted">' + label + "</span><strong>" + (has ? profitHtml(total, null, cur) : "–") + "</strong>" +
      '<span class="fin-sub">' + esc(sub.join(" · ") || "no fills yet") + "</span></div>";
  }
  function finRow(f) {
    var cur = f.currency || "", held = f.entry_price != null, withB = finData && finData.strategy_b && f.b && f.b.status;
    var stateLine = !f.status ? "Not in the simulation yet" : held ? "Bought for " + N.moneyText(f.entry_price, cur) + (isNum(f.ask) ? ", listed at " + N.moneyText(f.ask, cur) : "") :
      isNum(f.bid) ? "Our offer " + N.moneyText(f.bid, cur) + (isNum(f.competitor_top) ? " · best other " + N.moneyText(f.competitor_top, cur) : "") + (isNum(f.floor) ? " · floor " + N.moneyText(f.floor, cur) : "") : "Waiting for the first check";
    if (missedText(f)) stateLine += " · " + missedText(f);
    var rec = f.record, recLine = rec ? '<p class="fin-record">' + verdictPill(rec) + " " + esc(rec.why) +
      (finData && finData.strategy_b && f.b && f.b.record ? ' <span class="muted">· ' + bTag() + esc((VERDICTS[f.b.record.verdict] || [f.b.record.verdict])[0]) + "</span>" : "") + "</p>" : "";
    return '<div class="fin-row">' + N.art(f.slug, f.image) +
      '<div class="fin-main"><div class="cand-name"><span>' + osLink(f.slug, f.name || f.slug) + "</span></div>" +
      '<div class="cand-meta">' + esc([N.chainName(f.chain), f.added_by === "claude" ? "added by Claude" : "added by you"].filter(Boolean).join(" · ")) + "</div>" +
      (f.note ? '<p class="fin-note">' + esc(f.note) + "</p>" : "") +
      (trialText(f) ? '<p class="fin-trial ' + (f.trial_state === "on" ? "c-warn" : "c-good") + '">' + esc(N.cap(trialText(f))) + "</p>" : "") +
      '<p class="fin-state">' + esc(stateLine) + "</p>" + recLine +
      (f.last_event ? '<p class="sim-last"><span class="muted">' + esc(N.ago(f.last_event.ts)) + ":</span> " + esc(f.last_event.message) + "</p>" : "") + "</div>" +
      '<div class="fin-side">' + (f.status ? statusPill(f) : '<span class="pill neutral">Not simulated</span>') +
      finProfit(withB ? "Strategy A profit" : "Simulation profit", f, cur) +
      (withB ? finProfit(bTag() + "Strategy B profit (" + fmt(limitOf(f.b, finData.min_profit_gap_pct), 0) + "% limit)", f.b, cur) : "") +
      '<button class="btn ghost" type="button" data-fin-remove="' + esc(f.slug) + '" data-name="' + esc(f.name || f.slug) + '">Remove</button></div></div>';
  }
  function loadFinal() {
    if (finBusy) return;
    finBusy = true;
    N.api("/api/final").then(function (r) {
      finData = r;
      $("finList").innerHTML = r.items.length ? r.items.map(finRow).join("") :
        '<div class="empty"><h3>Nothing on the Final list</h3>Add a collection below, or ask Claude to check one.</div>';
    }, function (e) { $("finList").innerHTML = '<div class="empty">' + esc(e.message || "Couldn't load the Final list.") + "</div>"; })
      .then(function () { finBusy = false; });
  }
  $("finList").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-fin-remove]"); if (!b) return;
    if (!window.confirm("Take " + b.getAttribute("data-name") + " off the Final list? It stays in the simulation.")) return;
    b.disabled = true;
    N.post("/api/final/remove", { slug: b.getAttribute("data-fin-remove") }).then(function () { N.toast("Removed from the Final list."); loadFinal(); },
      function (err) { N.toast(err.message || "Couldn't remove it."); b.disabled = false; });
  });
  $("finAddForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var input = $("finAddInput"), note = $("finAddNote"), v = input.value.trim(); if (!v) return;
    var btn = this.querySelector("button"); btn.disabled = true;
    N.post("/api/final/add", { slug: v, note: note.value.trim() }).then(function () {
      input.value = ""; note.value = ""; N.toast("Added to the Final list and the simulation."); loadFinal();
    }, function (err) { N.toast(err.message || "Couldn't add it."); }).then(function () { btn.disabled = false; });
  });

  window.SimPage = { load: load, loadFinal: loadFinal };
  if ((location.hash || "").slice(1) === "final") loadFinal();
  if ((location.hash || "").slice(1) === "sim") load();
})();
