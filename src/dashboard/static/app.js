/* NFT Monitor dashboard. Plain JS, no build step; talks to the local server's /api endpoints. */
(function () {
  "use strict";

  // ---------- Rule and label definitions ----------
  var RULES = [
    { key: "verification", name: "Blue tick", desc: "Verified on OpenSea", kind: "bool" },
    { key: "project_age", name: "Collection age", desc: "Days since it was created on OpenSea", unit: "days", kind: "min", prefix: "More than" },
    { key: "listed_items", name: "Listed for sale", desc: "Share of the supply that's listed", unit: "%", kind: "max", prefix: "Under" },
    { key: "trading_frequency", name: "Trading pace", desc: "Sales per day over 7 full days, with some listings bought at about the floor price (not rare items), and some sellers accepting offers", unit: "per day", kind: "max", prefix: "At most" },
    { key: "floor_change_1d", name: "Floor move in 1 day", desc: "Up or down", unit: "%", kind: "max", prefix: "Under" },
    { key: "floor_change_7d", name: "Floor move in 7 days", desc: "Up or down", unit: "%", kind: "max", prefix: "Under" },
    { key: "offer_to_floor", name: "Spread over top offer", desc: "How far the floor sits above the top offer, royalty included", unit: "%", kind: "min", prefix: "At least" },
    { key: "net_profit", name: "Net profit", desc: "Return after OpenSea fee, royalty and gas", unit: "%", kind: "min", prefix: "At least" }
  ];
  var RULE_BY_KEY = {};
  RULES.forEach(function (r) { RULE_BY_KEY[r.key] = r; });

  var FUNNEL_LABELS = {
    verification: "No blue tick", project_age: "Too new", listed_items: "Too many listed",
    trading_frequency: "Trading pace (too fast, too slow, or no floor or offer sales)", floor_history_1d: "Waiting for floor history (1 day)",
    floor_history_7d: "Waiting for floor history (7 days)", floor_change_1d: "Floor moved (1 day)",
    floor_change_7d: "Floor moved (7 days)", offer_to_floor: "Spread too small", net_profit: "Profit too small",
    floor_price: "No floor price", total_supply: "Supply missing", collection_fetch: "Couldn't load details",
    unknown: "Other", PASS: "Passed every rule"
  };
  var EVENT_COLORS = { candidate: "c-good", offline: "c-warn", error: "c-bad", online: "c-accent", start: "c-accent",
                       restart: "c-accent", stop: "c-muted", settings: "c-muted" };

  var state = { overview: null, settings: null, draft: null, candDays: 7, near: null, tz: undefined, offline: false,
                prices: {}, aliases: {}, candItems: null };

  // ---------- Helpers ----------
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function isNum(n) { return typeof n === "number" && isFinite(n); }
  function fmt(n, d) { return isNum(n) ? n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) : "–"; }
  function int(n) { return isNum(n) ? Math.round(n).toLocaleString("en-US") : "–"; }
  function moneyText(n, cur) {
    if (!isNum(n)) return "–";
    var a = Math.abs(n), d = a === 0 ? 2 : a < 0.01 ? 5 : a < 0.1 ? 4 : a < 10 ? 3 : 2;
    return fmt(n, d) + " " + (cur || "");
  }
  // A coin amount that shows its dollar value on hover (or tap on a phone).
  // then = { rate, at }: the coin's dollar price when the candidate was found.
  function money(n, cur, then) {
    if (!isNum(n)) return "–";
    var attrs = ' data-amt="' + n + '" data-cur="' + esc(cur || "") + '"';
    if (then && isNum(then.rate)) attrs += ' data-then="' + then.rate + '" data-at="' + esc(then.at || "") + '"';
    return '<span class="usd" tabindex="0"' + attrs + ">" + esc(moneyText(n, cur)) + "</span>";
  }
  function coinKey(cur) {
    var c = String(cur || "").trim().toUpperCase();
    return state.aliases[c] || c;
  }
  function usdRate(cur) {
    var p = state.prices[coinKey(cur)];
    return p && isNum(p.usd) ? p.usd : null;
  }
  function foundRate(d, offer) {
    var r = offer ? (isNum(d.offer_usd_rate) ? d.offer_usd_rate : d.usd_rate) : d.usd_rate;
    return d.found_at && isNum(r) ? { rate: r, at: d.found_at } : null;
  }
  function toUsd(n, cur) { var r = usdRate(cur); return isNum(n) && r != null ? n * r : null; }
  function usd(n) {
    if (!isNum(n)) return "–";
    var a = Math.abs(n), d = a >= 1000 ? 0 : a >= 1 ? 2 : a >= 0.01 ? 3 : 5;
    return (n < 0 ? "−$" : "$") + fmt(a, d);
  }
  function signed(n, d) { return isNum(n) ? (n > 0 ? "+" : n < 0 ? "−" : "") + fmt(Math.abs(n), d) : "–"; }
  function cap(s) { s = String(s || ""); return s.charAt(0).toUpperCase() + s.slice(1); }

  function toDate(v) {
    if (v == null || v === "") return null;
    if (typeof v === "number") return new Date(v * 1000);
    var d = new Date(v);
    return isNaN(d.getTime()) ? null : d;
  }
  function fmtTime(v) {
    var d = toDate(v); if (!d) return "";
    try { return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: state.tz }); }
    catch (e) { return d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); }
  }
  function fmtDay(v, withTime) {
    var d = toDate(v); if (!d) return "";
    var o = { weekday: "short", day: "numeric", month: "short", timeZone: state.tz };
    if (withTime) { o.hour = "2-digit"; o.minute = "2-digit"; }
    try { return d.toLocaleString("en-GB", o); } catch (e) { delete o.timeZone; return d.toLocaleString("en-GB", o); }
  }
  function shortDay(v) {
    var d = toDate(v); if (!d) return "";
    var o = { day: "numeric", month: "short", timeZone: state.tz };
    try { return d.toLocaleDateString("en-GB", o); } catch (e) { delete o.timeZone; return d.toLocaleDateString("en-GB", o); }
  }
  function ago(v) {
    var d = toDate(v); if (!d) return "";
    var s = Math.max(0, (Date.now() - d.getTime()) / 1000);
    if (s < 90) return "just now";
    if (s < 3600) return Math.round(s / 60) + " minutes ago";
    if (s < 5400) return "1 hour ago";
    if (s < 86400 * 2) return Math.round(s / 3600) + " hours ago";
    return Math.round(s / 86400) + " days ago";
  }
  function sameDay(a, b) { return a && b && fmtDay(a) === fmtDay(b); }
  function chainName(id) {
    var s = state.settings;
    if (s) for (var i = 0; i < s.chains.length; i++) if (s.chains[i].id === id) return s.chains[i].name;
    return id ? cap(String(id).replace(/_/g, " ")) : "";
  }

  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (body) {
        if (state.offline) { state.offline = false; }
        if (!r.ok) { var e = new Error(body.error || ("Request failed (" + r.status + ")")); e.body = body; e.status = r.status; throw e; }
        return body;
      });
    }, function (err) { state.offline = true; throw err; });
  }
  function post(path, data) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data || {}) });
  }

  function hash(str) { var h = 2166136261; for (var i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }
  function art(slug, image) {
    if (image && /^https:\/\//.test(image)) {
      return '<span class="art" aria-hidden="true"><img src="' + esc(image) + '" alt="" loading="lazy" referrerpolicy="no-referrer" onerror="this.remove()"></span>';
    }
    var h = hash(slug || "x"), hue = h % 360, hue2 = (hue + 40 + (h >> 9) % 80) % 360, cells = "";
    for (var i = 0; i < 9; i++) {
      var on = (h >> (i + 3)) & 1, x = (i % 3) * 16 + 2, y = Math.floor(i / 3) * 16 + 2;
      if (on) cells += '<rect x="' + x + '" y="' + y + '" width="12" height="12" rx="' + ((h >> i) & 1 ? 6 : 3) + '" fill="hsl(' + hue2 + ' 70% 88% / .9)"/>';
    }
    return '<span class="art" aria-hidden="true"><svg viewBox="0 0 50 50"><rect width="50" height="50" fill="hsl(' + hue + ' 55% 42%)"/>' + cells + "</svg></span>";
  }
  var TICK = '<svg class="tick" viewBox="0 0 24 24" aria-label="Verified"><path fill="currentColor" d="M12 1.5l2.6 1.9 3.2-.1 1 3 2.6 1.9-1 3.1 1 3.1-2.6 1.9-1 3-3.2-.1L12 22.5l-2.6-1.9-3.2.1-1-3L2.6 15.8l1-3.1-1-3.1 2.6-1.9 1-3 3.2.1z"/><path d="M8 12.2l2.7 2.7L16.2 9.4" fill="none" stroke="var(--surface)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  var I_OK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
  var I_NO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg>';
  var I_WAIT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><circle cx="12" cy="12" r="8"/><path d="M12 8v4l2.5 2"/></svg>';
  var I_OFF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.2" stroke-linecap="round"><path d="M6 12h12"/></svg>';
  var I_CLOCK = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
  var I_WARN = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3l9.5 17H2.5z"/><path d="M12 10v4M12 17.5v.01"/></svg>';

  function nameLine(name, verified) {
    return '<div class="cand-name"><span>' + esc(name) + "</span>" + (verified ? TICK : "") + "</div>";
  }
  function unitText(rule, v) {
    if (!rule) return "";
    if (rule.unit === "%") return fmt(v, 1) + "%";
    if (rule.unit === "days") return fmt(v, 0) + " days";
    if (rule.unit === "per day") return fmt(v, 2) + "/day";
    if (rule.unit === "sales" || rule.unit === "floor sales" || rule.unit === "offer sales") return int(v) + (v === 1 ? " sale" : " sales");
    return String(v);
  }
  var WEEK_SALES = { key: "trading_frequency", name: "Trading pace: sales this week", unit: "sales", kind: "min", prefix: "At least" };
  var FLOOR_SALES = { key: "trading_frequency", name: "Trading pace: sales at floor price", unit: "floor sales", kind: "min", prefix: "At least" };
  var OFFER_SALES = { key: "trading_frequency", name: "Trading pace: sales to offers", unit: "offer sales", kind: "min", prefix: "At least" };
  // A trading-pace rejection for too few sales (or too few at floor price) is counted per week, not per day
  function ruleFor(key, unit) {
    if (key === "trading_frequency" && unit === "sales in 7 days") return WEEK_SALES;
    if (key === "trading_frequency" && unit === "floor sales in 7 days") return FLOOR_SALES;
    if (key === "trading_frequency" && unit === "offer sales in 14 days") return OFFER_SALES;
    return RULE_BY_KEY[key];
  }
  function paceSetting(name) {
    var r = state.settings && state.settings.rules && state.settings.rules.trading_frequency;
    return r && isNum(r[name]) ? r[name] : 0;
  }
  function minWeekSales() { return paceSetting("min_sales_7d"); }
  function rareText() { var p = paceSetting("rare_item_pct"); return p > 0 ? ", not one of the rarest " + fmt(p, 0) + "%" : ""; }
  function floorBand() { return fmt(paceSetting("floor_sale_min_pct") || 90, 0) + "–" + fmt(paceSetting("floor_sale_max_pct") || 115, 0) + "% of the floor"; }

  // ---------- Navigation ----------
  function go(page) {
    if (!$("page-" + page)) page = "home";
    document.querySelectorAll(".page").forEach(function (p) { p.classList.toggle("active", p.id === "page-" + page); });
    document.querySelectorAll(".nav button").forEach(function (b) {
      if (b.getAttribute("data-page") === page) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
    });
    try { if (location.hash !== "#" + page) history.replaceState(null, "", "#" + page); } catch (e) {}
    window.scrollTo(0, 0);
    if (page === "candidates") loadCandidates();
    if (page === "near") loadNear();
    if (page === "settings" && !isDirty()) loadSettings();
  }
  document.querySelectorAll(".nav button").forEach(function (b) { b.addEventListener("click", function () { go(b.getAttribute("data-page")); }); });

  var toastTimer = null;
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.classList.add("show");
    clearTimeout(toastTimer); toastTimer = setTimeout(function () { t.classList.remove("show"); }, 3600);
  }

  // ---------- Dollar values (hover, focus or tap) ----------
  function loadPrices() {
    return api("/api/prices").then(function (r) {
      state.prices = r.prices || {}; state.aliases = r.aliases || {};
      if (state.candItems && $("page-candidates").classList.contains("active")) renderCandidates();
      if (state.draft && $("page-settings").classList.contains("active") && !$("page-settings").contains(document.activeElement)) renderSettings();
    }, function () {});
  }
  var tip = document.createElement("div");
  tip.className = "usd-tip"; tip.setAttribute("role", "tooltip"); tip.hidden = true;
  document.body.appendChild(tip);
  var tipFor = null;
  function tipHtml(el) {
    var n = parseFloat(el.getAttribute("data-amt")), cur = el.getAttribute("data-cur"), p = state.prices[coinKey(cur)];
    var lines = [];
    if (p && isNum(p.usd)) {
      lines.push('<strong>' + usd(n * p.usd) + "</strong> now");
    } else {
      lines.push("No dollar price for " + esc(cur || "this coin") + " yet");
    }
    var then = parseFloat(el.getAttribute("data-then"));
    if (isNum(then)) {
      var at = el.getAttribute("data-at");
      lines.push('<strong>' + usd(n * then) + "</strong> when found" + (at ? " (" + esc(fmtDay(at)) + ")" : ""));
    }
    if (p && isNum(p.usd) && p.source !== "Stablecoin") {
      lines.push('<span class="usd-src">1 ' + esc(coinKey(cur)) + " = " + usd(p.usd) + " · " + esc(p.source) + "</span>");
    }
    return lines.join("<br>");
  }
  function showTip(el) {
    tipFor = el; tip.innerHTML = tipHtml(el); tip.hidden = false;
    var r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    var top = r.top - h - 8; if (top < 8) top = r.bottom + 8;
    tip.style.left = left + "px"; tip.style.top = top + "px";
  }
  function hideTip() { tip.hidden = true; tipFor = null; }
  var canHover = window.matchMedia && window.matchMedia("(hover: hover)").matches;
  document.addEventListener("mouseover", function (e) {
    if (!canHover) return;
    var el = e.target.closest && e.target.closest(".usd");
    if (el) { if (el !== tipFor) showTip(el); } else if (tipFor) hideTip();
  });
  document.addEventListener("focusin", function (e) {
    // On touch screens the tap handler below shows it; a tap also focuses, which would toggle it twice
    if (canHover && e.target.classList && e.target.classList.contains("usd")) showTip(e.target);
  });
  document.addEventListener("focusout", function (e) { if (e.target === tipFor) hideTip(); });
  // On a phone a tap shows the dollar value instead of opening the card
  document.addEventListener("click", function (e) {
    var el = e.target.closest && e.target.closest(".usd");
    if (el && !canHover) { e.preventDefault(); e.stopPropagation(); if (el === tipFor) hideTip(); else showTip(el); return; }
    if (!canHover && tipFor) hideTip();
  }, true);
  window.addEventListener("scroll", hideTip, true);

  // ---------- Home ----------
  function greeting() {
    var h;
    try { h = parseInt(new Date().toLocaleString("en-GB", { hour: "2-digit", hour12: false, timeZone: state.tz }), 10); }
    catch (e) { h = new Date().getHours(); }
    return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
  }

  function renderStatus(o) {
    var st = o.state, title, sub, cls, icon, on = o.desired_run || st === "running" || st === "paused";
    var live = '<span class="dot live" style="width:12px;height:12px"></span>';
    if (st === "running") {
      cls = "st-running"; icon = live; title = "Running" + (o.dry_run ? " (dry run)" : "");
      sub = (o.started_at ? "Checking collections since " + fmtDay(o.started_at, true) : "Checking collections") +
        (o.last_check_at ? " · last check " + ago(o.last_check_at) : "");
    } else if (st === "paused") {
      cls = "st-paused"; title = "Paused: no internet";
      icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M2 8.8a15 15 0 0 1 20 0M5.5 12.4a10 10 0 0 1 13 0M9 16a5 5 0 0 1 6 0"/><path d="M3 3l18 18"/></svg>';
      sub = "Lost connection" + (o.offline_since ? " at " + fmtTime(o.offline_since) : "") + ". The bot carries on by itself when the internet is back.";
    } else if (st === "starting") {
      cls = "st-paused"; icon = '<span class="spinner"></span>'; title = "Starting…";
      sub = "The bot is meant to be on. The dashboard starts it within a few seconds.";
    } else {
      cls = "st-stopped"; title = "Stopped";
      icon = '<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>';
      sub = "It stays off, even after a restart, until you turn it on here.";
    }
    var dry = !on ? '<label class="dry"><input type="checkbox" id="dryRun"> Dry run (no Info.md files)</label>' : "";
    $("statusCard").innerHTML =
      '<div class="status-icon ' + cls + '">' + icon + "</div>" +
      '<div class="status-text"><h2>' + esc(title) + "</h2><p>" + esc(sub) + "</p></div>" +
      '<div class="status-actions">' + dry + '<div class="big-switch"><label for="runSwitch">' + (on ? "On" : "Off") + "</label>" +
      '<button class="switch lg" type="button" id="runSwitch" role="switch" aria-checked="' + on + '" aria-label="Bot on or off"></button></div></div>';
    $("runSwitch").onclick = function () {
      var btn = this; btn.disabled = true;
      var req = on ? post("/api/bot/stop") : post("/api/bot/start", { dry_run: !!($("dryRun") && $("dryRun").checked) });
      req.then(function (r) {
        toast(on ? "Bot stopped. It stays off until you turn it on." : (r.message || "Bot started."));
      }, function (e) { toast(e.message || "Couldn't change the bot."); }).then(function () { setTimeout(loadOverview, 600); });
    };

    var side = st === "paused" ? '<strong class="c-warn"><span class="dot"></span>Paused · no internet</strong><span class="muted">Resumes by itself</span>' :
      st === "stopped" ? '<strong class="c-muted"><span class="dot"></span>Stopped</strong><span class="muted">Turn on from Home</span>' :
      st === "starting" ? '<strong class="c-warn"><span class="dot"></span>Starting…</strong><span class="muted">One moment</span>' :
      '<strong class="c-good"><span class="dot live"></span>Running</strong><span class="muted">' + esc(o.last_check_at ? "Last check " + ago(o.last_check_at) : "Getting started") + "</span>";
    $("sideStatus").innerHTML = side;
  }

  function renderRing(fh) {
    var days = Math.min(7, Math.floor(fh.days || 0)), C = 2 * Math.PI * 22, off = C * (1 - days / 7);
    $("ringBox").innerHTML =
      '<svg class="ring" viewBox="0 0 54 54" role="img" aria-label="' + days + ' of 7 days"><circle class="track" cx="27" cy="27" r="22"/>' +
      '<circle class="fill" cx="27" cy="27" r="22" stroke-dasharray="' + C.toFixed(2) + '" stroke-dashoffset="' + off.toFixed(2) + '" transform="rotate(-90 27 27)"/>' +
      '<text x="27" y="32" text-anchor="middle">' + days + "/7</text></svg>" +
      '<div><div style="font-weight:650">' + (fh.ready ? "Ready" : "Day " + days + " of 7") + '</div><div class="stat-sub">' +
      (fh.ready ? fmt(fh.days, 0) + " days of floor prices saved" : "7-day price check ready from " + esc(fmtDay(fh.ready_on + "T12:00:00"))) + "</div></div>";
  }

  function renderBanner(o) {
    var b = "";
    if (o.state === "paused") {
      b = '<div class="banner warn">' + I_WARN + "<div><strong>No internet" + (o.offline_since ? " since " + fmtTime(o.offline_since) : "") +
        ".</strong> Nothing is being marked as failed while the connection is down. After a power cut, the Mac and the bot both start again by themselves.</div></div>";
    } else if (!o.floor_history.ready && o.rules_enabled.floor_change_7d) {
      b = '<div class="banner">' + I_CLOCK + "<div><strong>First week: building floor history.</strong> The 7-day price check needs a week of saved floor prices, so candidates can't appear before " +
        esc(fmtDay(o.floor_history.ready_on + "T12:00:00")) + ". Everything else is being checked already.</div></div>";
    }
    if (!o.autostart_installed) {
      b += '<div class="banner warn" style="margin-top:10px">' + I_WARN + '<div><strong>Auto-start isn\'t set up on this Mac.</strong> After a power cut the bot won\'t come back by itself. Run <span class="cmd">python bot.py install-autostart</span> once in Terminal.</div></div>';
    }
    $("homeBanner").innerHTML = b;
  }

  function renderFunnel(o) {
    var f = o.funnel || {}, keys = Object.keys(f), total = 0;
    keys.forEach(function (k) { total += f[k]; });
    $("funnelSub").textContent = "Last 7 days · " + int(total) + " checks";
    if (!total) { $("funnel").innerHTML = '<div class="empty">No checks yet. Turn the bot on to start.</div>'; return; }
    var rows = keys.filter(function (k) { return k !== "PASS"; }).sort(function (a, b) { return f[b] - f[a]; });
    rows.push("PASS");
    var max = Math.max.apply(null, keys.map(function (k) { return f[k]; }));
    $("funnel").innerHTML = rows.map(function (k) {
      var n = f[k] || 0, label = FUNNEL_LABELS[k] || k;
      if (k === "offer_to_floor") label = "Spread under " + o.limits.offer_to_floor + "%";
      if (k === "net_profit") label = "Profit under " + o.limits.net_profit + "%";
      var cls = k === "PASS" ? "pass" : k.indexOf("floor_history") === 0 ? "waiting" : "";
      var w = Math.max(n / max * 100, n ? 0.6 : 0);
      return '<div class="f-row ' + (cls === "pass" ? "pass" : "") + '"><span class="f-label" title="' + esc(label) + '">' + esc(label) +
        '</span><span class="f-track"><span class="f-bar ' + cls + '" style="width:' + w.toFixed(2) + '%"></span></span><span class="f-count">' + int(n) + "</span></div>";
    }).join("");
  }

  function renderFeed(o) {
    var ev = o.events || [];
    if (!ev.length) { $("feed").innerHTML = '<li style="grid-template-columns:1fr" class="muted">Nothing yet.</li>'; return; }
    var now = new Date();
    $("feed").innerHTML = ev.slice(0, 12).map(function (e) {
      var t = sameDay(e.ts, now) ? fmtTime(e.ts) : fmtDay(e.ts).split(",")[0];
      var msg = esc(e.message);
      if (e.kind === "candidate") msg = "<strong>" + msg + "</strong>";
      return "<li><time>" + esc(t) + '</time><span class="dot ' + (EVENT_COLORS[e.kind] || "c-muted") + '"></span><span>' + msg + "</span></li>";
    }).join("");
  }

  function renderOverview(o) {
    state.overview = o; state.tz = o.timezone || undefined;
    $("h-home").textContent = greeting();
    $("homeSub").textContent = "Here's what the bot has been doing. Times are " + (o.timezone || "local") + " time.";
    renderStatus(o); renderBanner(o); renderRing(o.floor_history); renderFunnel(o); renderFeed(o);
    $("statChecked").textContent = int(o.checked_today);
    $("statCheckedSub").textContent = "out of " + int(o.universe) + " known, on " + o.chains_enabled + " chains" +
      (o.skipped_unverified ? " · " + int(o.skipped_unverified) + " skipped (no blue tick)" : "");
    $("statShort").textContent = int(o.shortlisted);
    $("statCand").textContent = int(o.candidates_7d);
    $("badgeCand").textContent = o.candidates_7d;
  }

  function loadOverview() {
    loadChains();
    return api("/api/overview").then(renderOverview, function (e) {
      if (state.offline) {
        $("sideStatus").innerHTML = '<strong class="c-bad"><span class="dot"></span>Dashboard offline</strong><span class="muted">Is it still running?</span>';
      } else { toast(e.message); }
    });
  }

  // ---------- Most active chains ----------
  var chainsLoaded = 0;
  function chainRank(id) { var r = state.chainRanks && state.chainRanks[id]; return isNum(r) ? r : null; }
  function loadChains(force) {
    if (!force && Date.now() - chainsLoaded < 5 * 60 * 1000) return Promise.resolve();
    chainsLoaded = Date.now();
    return api("/api/chains").then(function (r) {
      state.chains = r; state.chainRanks = {};
      (r.chains || []).forEach(function (c) { if (isNum(c.rank)) state.chainRanks[c.id] = c.rank; });
      renderChains();
      if (state.candItems && $("page-candidates").classList.contains("active")) renderCandidates();
    }, function () {
      chainsLoaded = 0;
      if (!state.chains && $("chainsBody")) $("chainsBody").innerHTML = '<p class="chart-note" style="margin-top:0">Couldn\'t load the chain list. Trying again shortly.</p>';
    });
  }
  function bigUsd(n) {
    if (!isNum(n)) return "–";
    if (n >= 1e6) return "$" + fmt(n / 1e6, n >= 1e7 ? 1 : 2) + "M";
    if (n >= 1e4) return "$" + fmt(n / 1e3, 0) + "K";
    return "$" + fmt(n, 0);
  }
  function trendCell(t) {
    if (!isNum(t)) return '<span class="muted">–</span>';
    var pct = t - 100, cls = pct >= 10 ? "c-good" : pct <= -10 ? "c-bad" : "muted";
    var arrow = pct >= 10 ? "↑" : pct <= -10 ? "↓" : "→";
    return '<span class="' + cls + '">' + arrow + " " + (pct > 0 ? "+" : pct < 0 ? "−" : "") + fmt(Math.abs(pct), 0) + "%</span>";
  }
  function renderChains() {
    var r = state.chains, box = $("chainsBody");
    if (!box || !r) return;
    var rows = (r.chains || []).filter(function (c) { return c.at; });
    if (!rows.length) {
      box.innerHTML = '<p class="chart-note" style="margin-top:0">The bot fills this in a few minutes after it starts, then every ' +
        fmt(r.interval_hours || 6, 0) + " hours.</p>";
      $("chainsSub").textContent = "NFT trading on OpenSea";
      return;
    }
    var showAll = !!state.chainsAll, anyLlama = rows.some(function (c) { return isNum(c.llama_day_usd); });
    var list = showAll ? rows : rows.slice(0, 10);
    $("chainsSub").textContent = r.updated_at ? "Updated " + ago(r.updated_at) : "";
    box.innerHTML = '<div class="tbl-scroll"><table class="tbl chains"><thead><tr><th>#</th><th>Chain</th><th>24h</th><th>7 days</th>' +
      '<th title="Last 24 hours compared with the daily average of the last 7 days"><span class="lg">Today vs week</span><span class="sm">Trend</span></th><th class="hide-sm" title="How many of its top ' + int(r.per_chain) +
      ' collections sold something in the last 24 hours">Busy today</th>' + (anyLlama ? '<th class="hide-sm" title="All NFT marketplaces, from DefiLlama">All markets 24h</th>' : "") +
      "</tr></thead><tbody>" + list.map(function (c) {
        var native = c.native && !isNum(c.day_usd) ? moneyText(c.native.day, c.native.symbol) : "";
        var flags = (c.enabled ? "" : ' <span class="pill neutral" title="The bot doesn\'t scan this chain (Settings)">not scanned</span>') +
          (c.stale ? ' <span class="pill warn" title="OpenSea didn\'t answer for this chain lately">old</span>' : "") +
          (c.partial || c.unpriced ? ' <span class="muted" title="' + (c.unpriced ? "Some volume has no dollar price yet" : "Some collections didn\'t answer") + '">*</span>' : "");
        return '<tr class="' + (c.enabled ? "" : "dim") + '"><td class="muted">' + (isNum(c.rank) ? c.rank : "") + "</td>" +
          '<td class="chain-name"><a href="https://opensea.io/collections/chain/' + encodeURIComponent(c.id) + '" target="_blank" rel="noopener">' + esc(c.name) + "</a>" + flags + "</td>" +
          '<td class="num-cell">' + (native ? esc(native) : isNum(c.day_usd) && c.day_usd > 0 ? bigUsd(c.day_usd) : '<span class="muted">quiet</span>') + "</td>" +
          '<td class="num-cell">' + (isNum(c.week_usd) ? bigUsd(c.week_usd) : "–") + "</td>" +
          '<td class="num-cell">' + trendCell(c.trend) + "</td>" +
          '<td class="num-cell hide-sm">' + (isNum(c.active) ? int(c.active) + " of " + int(c.collections) : "–") + "</td>" +
          (anyLlama ? '<td class="num-cell hide-sm">' + (isNum(c.llama_day_usd) ? bigUsd(c.llama_day_usd) : '<span class="muted">–</span>') + "</td>" : "") + "</tr>";
      }).join("") + "</tbody></table></div>" +
      (rows.length > 10 ? '<button class="btn ghost" type="button" id="chainsMore" style="margin-top:8px">' + (showAll ? "Show top 10" : "Show all " + rows.length + " chains") + "</button>" : "") +
      '<p class="chart-note">Adds up the ' + int(r.per_chain) + " biggest collections on each chain by OpenSea volume, so real totals are a bit higher, " +
      "but chains compare fairly. Refreshed every " + fmt(r.interval_hours || 6, 0) + " hours." +
      (anyLlama ? " All markets includes Blur, Magic Eden and others, from DefiLlama, which covers only a few chains." : "") + "</p>";
    var more = $("chainsMore");
    if (more) more.addEventListener("click", function () { state.chainsAll = !state.chainsAll; renderChains(); });
  }

  // ---------- Candidates ----------
  // A candidate whose latest check (after it was found) failed: it no longer passes today
  function stoppedPassing(item, latest) {
    return !!(latest && latest.pass === false && latest.at && toDate(latest.at) > toDate(item.created_at));
  }
  function stoppedReason(latest) {
    var label = (RULE_BY_KEY[latest.rule] || {}).name || FUNNEL_LABELS[latest.rule] || "A rule failed";
    var why = String(latest.reason || "").replace(/^[a-z_0-9]+:\s*/, "").split(";")[0].trim();
    return why && why.toLowerCase() !== label.toLowerCase() ? label + ": " + why : label;
  }
  function candCard(item, latest) {
    var d = item.details || {}, cur = d.currency || "", then = foundRate(d), offThen = foundRate(d, true);
    var nRules = RULES.filter(function (r) { var x = (d.rules || {})[r.key]; return x && x.enabled; }).length;
    var stopped = stoppedPassing(item, latest), checked = latest && latest.last_checked;
    return '<div class="card cand" role="button" tabindex="0" data-open="' + esc(item.slug) + '" data-date="' + esc(item.date_str) + '">' +
      '<div class="cand-head">' + art(item.slug, d.image) + '<div style="min-width:0">' + nameLine(d.name || item.slug, d.verified) +
      '<div class="cand-meta">' + esc(chainName(d.chain)) + (chainRank(d.chain) ? ' <span class="chain-rank" title="Its rank in Most active chains on Home">#' + chainRank(d.chain) + " chain</span>" : "") +
      (isNum(d.supply) ? " · " + int(d.supply) + " items" : "") + "</div></div></div>" +
      '<dl class="kv" style="margin:0">' +
      '<div><dt>Spread over offer</dt><dd class="c-good">' + fmt(d.spread, 1) + "%</dd></div>" +
      "<div><dt>Est. profit</dt><dd>" + money(d.net, cur, then) + " <small>" + fmt(d.roi, 0) + "%</small></dd></div>" +
      "<div><dt>Floor</dt><dd>" + money(d.floor, cur, then) + "</dd></div>" +
      "<div><dt>Top offer</dt><dd>" + money(d.offer, d.offer_currency || cur, offThen) + "</dd></div></dl>" +
      (stopped ? '<p class="cand-stopped">' + esc(stoppedReason(latest)) + "</p>" : "") +
      '<div class="cand-foot"><span>Found ' + esc(fmtDay(d.found_at || item.created_at, true)) +
      (checked ? '<br><span class="muted">Last checked ' + esc(ago(checked)) + "</span>" : "") + "</span>" +
      (stopped ? '<span class="pill bad">Stopped passing</span>'
               : '<span class="pill good">Passed ' + (nRules ? "all " + nRules + " rules" : "") + "</span>") + "</div></div>";
  }

  // Sorting and filters only change what's shown here, never the bot's rules.
  var SORTS = [
    ["latest", "Latest found"],
    ["spread", "Highest spread over offer"],
    ["profit", "Highest profit in $"],
    ["roi", "Highest return %"],
    ["cheap", "Cheapest to buy in $"],
    ["sales_hi", "Most sales this week"],
    ["sales_lo", "Fewest sales this week"],
    ["listed", "Fewest listed for sale"],
    ["name", "Name A–Z"],
    ["chain", "Blockchain"],
    ["chainrank", "Most active chain first"]
  ];
  var FILTERS = [
    ["spread", "Min spread over offer", "%", "min"],
    ["minbuy", "Min buy price", "$", "min"],
    ["buy", "Max buy price", "$", "max"],
    ["profit", "Min profit", "$", "min"],
    ["roi", "Min return", "%", "min"],
    ["listed", "Max listed for sale", "%", "max"],
    ["sales", "Min sales this week", "", "min"]
  ];
  var VIEW_KEY = "nftmonitor.candidates.view";
  function loadView() {
    var v = null;
    try { v = JSON.parse(localStorage.getItem(VIEW_KEY) || "null"); } catch (e) {}
    v = v && typeof v === "object" ? v : {};
    return { sort: SORTS.some(function (x) { return x[0] === v.sort; }) ? v.sort : "latest",
             f: v.f && typeof v.f === "object" ? v.f : {}, chains: Array.isArray(v.chains) ? v.chains : [], open: !!v.open,
             stopped: !!v.stopped };
  }
  var view = loadView();
  function saveView() { try { localStorage.setItem(VIEW_KEY, JSON.stringify(view)); } catch (e) {} }

  // The numbers each sort and filter uses, in dollars where it matters
  function metrics(item) {
    var d = item.details || {}, cur = d.currency || "";
    var buy = isNum(d.entry) ? d.entry : d.offer;
    return {
      spread: d.spread, roi: d.roi, listed: d.listed_pct, sales: d.sales_7d,
      profit: toUsd(d.net, cur), buy: toUsd(buy, cur), minbuy: toUsd(buy, cur),
      name: String(d.name || item.slug).toLowerCase(), chain: chainName(d.chain).toLowerCase(),
      chainrank: chainRank(d.chain)
    };
  }
  function activeFilters() {
    var n = FILTERS.filter(function (f) { return isNum(view.f[f[0]]); }).length;
    return n + (view.chains.length ? 1 : 0);
  }
  function passes(m, item) {
    for (var i = 0; i < FILTERS.length; i++) {
      var k = FILTERS[i][0], lim = view.f[k];
      if (!isNum(lim)) continue;
      var v = m[k];
      if (!isNum(v)) return false;  // unknown value (e.g. no dollar price yet) can't meet the filter
      if (FILTERS[i][3] === "min" ? v < lim : v > lim) return false;
    }
    if (view.chains.length && view.chains.indexOf((item.details || {}).chain) < 0) return false;
    return true;
  }
  function byNum(key, dir) {
    return function (a, b) {
      var x = a.m[key], y = b.m[key];
      if (!isNum(x) && !isNum(y)) return a.i - b.i;
      if (!isNum(x)) return 1;
      if (!isNum(y)) return -1;
      return (x - y) * dir || a.i - b.i;
    };
  }
  function sorter(key) {
    switch (key) {
      case "spread": return byNum("spread", -1);
      case "profit": return byNum("profit", -1);
      case "roi": return byNum("roi", -1);
      case "cheap": return byNum("buy", 1);
      case "sales_hi": return byNum("sales", -1);
      case "sales_lo": return byNum("sales", 1);
      case "listed": return byNum("listed", 1);
      case "name": return function (a, b) { return a.m.name.localeCompare(b.m.name) || a.i - b.i; };
      case "chain": return function (a, b) { return a.m.chain.localeCompare(b.m.chain) || a.i - b.i; };
      case "chainrank": return byNum("chainrank", 1);
      default: return function (a, b) { return a.i - b.i; };
    }
  }

  function renderCandTools(items) {
    var present = {};
    items.forEach(function (it) { var c = (it.details || {}).chain; if (c) present[c] = true; });
    view.chains.forEach(function (c) { present[c] = true; });
    var chains = Object.keys(present).sort(function (a, b) { return chainName(a).localeCompare(chainName(b)); });
    var n = activeFilters();
    $("candTools").innerHTML =
      '<div class="cand-bar">' +
      '<label class="field" for="candSort">Sort by <select id="candSort">' + SORTS.map(function (x) {
        return '<option value="' + x[0] + '"' + (x[0] === view.sort ? " selected" : "") + ">" + esc(x[1]) + "</option>";
      }).join("") + "</select></label>" +
      '<button class="btn" type="button" id="candFilterBtn" aria-expanded="' + view.open + '" aria-controls="candFilters">' +
      '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4 6h16M7 12h10M10 18h4"/></svg>Filters' +
      (n ? ' <span class="count">' + n + "</span>" : "") + "</button>" +
      '<span class="muted" id="candCount"></span>' +
      '<button class="btn ghost" type="button" id="candStopped" aria-pressed="false" hidden></button>' +
      (n ? '<button class="btn ghost" type="button" id="candClear">Clear filters</button>' : "") + "</div>" +
      '<div class="card cand-filters" id="candFilters"' + (view.open ? "" : " hidden") + ">" +
      '<div class="filter-grid">' + FILTERS.map(function (f) {
        var v = view.f[f[0]];
        return '<label class="field" for="cf-' + f[0] + '"><span>' + esc(f[1]) + "</span><span class=\"in\">" + (f[2] === "$" ? "$" : "") +
          '<input id="cf-' + f[0] + '" type="number" step="any" min="0" inputmode="decimal" placeholder="Any" value="' + (isNum(v) ? v : "") + '">' +
          (f[2] === "%" ? "%" : "") + "</span></label>";
      }).join("") + "</div>" +
      (chains.length ? '<div class="filter-chains"><span class="muted">Chains</span><div class="chips" role="group" aria-label="Chains">' + chains.map(function (c) {
        return '<button class="chip" type="button" data-chain="' + esc(c) + '" aria-pressed="' + (view.chains.indexOf(c) >= 0) + '">' + esc(chainName(c)) + "</button>";
      }).join("") + "</div></div>" : "") +
      '<p class="chart-note">Only changes what\'s shown here. Dollar amounts use today\'s prices; leave a box empty for no limit.</p></div>';
  }

  function renderCandidates() {
    var items = state.candItems || [], latest = state.candLatest || {};
    if (!$("candTools").firstChild || !$("candTools").contains(document.activeElement)) renderCandTools(items);
    var rows = items.map(function (it, i) { return { it: it, i: i, m: metrics(it) }; });
    var nStopped = items.filter(function (it) { return stoppedPassing(it, latest[it.slug]); }).length;
    var shown = rows.filter(function (r) {
      return (view.stopped || !stoppedPassing(r.it, latest[r.it.slug])) && passes(r.m, r.it);
    }).sort(sorter(view.sort));
    if (view.sort === "chain") {
      var html = "", last = null;
      shown.forEach(function (r) {
        var c = chainName((r.it.details || {}).chain) || "Other";
        if (c !== last) { html += '<h3 class="grid-group">' + esc(c) + "</h3>"; last = c; }
        html += candCard(r.it, latest[r.it.slug]);
      });
      $("candGrid").innerHTML = html;
    } else {
      $("candGrid").innerHTML = shown.map(function (r) { return candCard(r.it, latest[r.it.slug]); }).join("");
    }
    if (!items.length) {
      $("candGrid").innerHTML = '<div class="card empty" style="grid-column:1/-1"><h3>No candidates in this period</h3>Check Near misses to see which rule is holding collections back.</div>';
    } else if (!shown.length) {
      $("candGrid").innerHTML = '<div class="card empty" style="grid-column:1/-1"><h3>No candidates match these filters</h3>' +
        (activeFilters() ? "Loosen a filter or clear them to see all " + items.length + "." : "All of them stopped passing on a later check.") + "</div>";
    }
    $("candCount").textContent = items.length ? (shown.length === items.length ? items.length + " shown" : shown.length + " of " + items.length + " shown") : "";
    var sb = $("candStopped");
    if (sb) {
      sb.hidden = !nStopped;
      sb.setAttribute("aria-pressed", String(view.stopped));
      sb.textContent = (view.stopped ? "Hide " : "Show ") + nStopped + " that stopped passing";
    }
  }
  function loadCandidates() {
    api("/api/candidates?days=" + state.candDays).then(function (r) {
      // One card per collection: its newest pass in the period
      var seen = {};
      state.candItems = (r.items || []).filter(function (it) { if (seen[it.slug]) return false; seen[it.slug] = true; return true; });
      state.candLatest = r.latest || {};
      renderCandidates();
    }, function (e) { $("candGrid").innerHTML = '<div class="card empty" style="grid-column:1/-1">' + esc(e.message) + "</div>"; });
  }
  $("rangeChips").addEventListener("click", function (e) {
    var b = e.target.closest("[data-days]"); if (!b) return;
    state.candDays = parseInt(b.getAttribute("data-days"), 10);
    document.querySelectorAll("#rangeChips .chip").forEach(function (c) { c.setAttribute("aria-pressed", String(c === b)); });
    loadCandidates();
  });
  $("candTools").addEventListener("change", function (e) {
    if (e.target.id === "candSort") { view.sort = e.target.value; saveView(); renderCandidates(); }
  });
  $("candTools").addEventListener("input", function (e) {
    var id = e.target.id; if (id.indexOf("cf-") !== 0) return;
    var v = parseFloat(e.target.value), k = id.slice(3);
    if (isNaN(v)) delete view.f[k]; else view.f[k] = v;
    saveView(); renderCandidates();
    var btn = $("candFilterBtn"), n = activeFilters();
    if (btn) { var c = btn.querySelector(".count"); if (n && !c) { c = document.createElement("span"); c.className = "count"; btn.appendChild(document.createTextNode(" ")); btn.appendChild(c); } if (c) { if (n) c.textContent = n; else c.remove(); } }
  });
  $("candTools").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b) return;
    if (b.id === "candFilterBtn") { view.open = !view.open; }
    else if (b.id === "candClear") { view.f = {}; view.chains = []; }
    else if (b.id === "candStopped") { view.stopped = !view.stopped; }
    else if (b.hasAttribute("data-chain")) {
      var c = b.getAttribute("data-chain"), i = view.chains.indexOf(c);
      if (i >= 0) view.chains.splice(i, 1); else view.chains.push(c);
    } else return;
    saveView(); renderCandTools(state.candItems || []); renderCandidates();
  });

  // ---------- Near misses ----------
  function gauge(d, rule) {
    var a = d.value, v = d.limit, top = Math.max(a, v) * 1.18 || 1;
    return '<div class="gauge"><span class="g-fill" style="width:' + (a / top * 100).toFixed(1) + '%"></span><span class="g-mark" style="left:' + (v / top * 100).toFixed(1) + '%"></span></div>' +
      '<div class="gauge-labels"><span>Now ' + unitText(rule, a) + "</span><span>Limit " + unitText(rule, v) + "</span></div>";
  }
  function shortBy(d, rule) {
    var diff = Math.abs(d.value - d.limit);
    var whole = rule.unit === "days" || rule.unit === "sales" || rule.unit === "floor sales" || rule.unit === "offer sales";
    var u = rule.unit === "%" ? " points" : rule.unit === "days" ? " days" : rule.unit === "sales" ? " sales" :
      rule.unit === "floor sales" ? (diff === 1 ? " floor sale" : " floor sales") :
      rule.unit === "offer sales" ? (diff === 1 ? " offer sale" : " offer sales") : " sales/day";
    return fmt(diff, whole ? 0 : rule.unit === "per day" ? 2 : 1) + u + (d.kind === "max" ? " over" : " short");
  }
  function nmRow(r) {
    var d = r.details || {}, rule = ruleFor(r.reject_filter || d.rule, d.unit) || { name: r.reject_filter, unit: "" };
    var also = (d.failed_rules || []).filter(function (k) { return k !== rule.key; });
    var note = d.early_exit ? '<div class="nm-off">Stopped at this rule, so later rules weren\'t checked.</div>' :
      also.length ? '<div class="nm-off">Also fails ' + esc(also.map(function (k) { return (RULE_BY_KEY[k] || { name: k }).name.toLowerCase(); }).join(", ")) + "</div>" : "";
    return '<button class="nm" type="button" data-open="' + esc(r.slug) + '" data-date="' + esc(r.date_str) + '"><div class="nm-who">' + art(r.slug, d.image) +
      '<div style="min-width:0">' + nameLine(d.name || r.slug, d.verified) + '<div class="cand-meta">' + esc(chainName(d.chain)) + " · " + esc(fmtDay(r.created_at)) + "</div></div></div>" +
      '<div class="nm-rule"><strong>' + esc(rule.name) + "</strong>" + gauge(d, rule) + note + "</div>" +
      '<span class="pill warn">' + esc(shortBy(d, rule)) + "</span></button>";
  }
  function waitRow(r) {
    var d = r.details || {}, oh = state.overview;
    var label = (r.reject_filter || "").slice(-2) === "1d" ? "1-day" : "7-day";
    return '<button class="nm" type="button" data-open="' + esc(r.slug) + '" data-date="' + esc(r.date_str) + '"><div class="nm-who">' + art(r.slug, d.image) +
      '<div style="min-width:0">' + nameLine(d.name || r.slug, d.verified) + '<div class="cand-meta">' + esc(chainName(d.chain)) + "</div></div></div>" +
      '<div class="nm-rule"><strong>Needs the ' + label + ' floor price</strong><span class="muted">Passed ' + (d.checked || []).length + " rules before this one</span></div>" +
      '<span class="pill neutral">' + (oh && oh.floor_history.ready_on ? "Ready " + esc(fmtDay(oh.floor_history.ready_on + "T12:00:00")) : "Next check") + "</span></button>";
  }
  function loadNear() {
    api("/api/near-misses").then(function (r) {
      state.near = r;
      $("badgeNear").textContent = r.near.length;
      $("nmList").innerHTML = r.near.length ? r.near.map(nmRow).join("") :
        '<div class="empty"><h3>Nothing close right now</h3>No collection missed a rule by a small margin in the last 7 days.</div>';
      $("waitList").innerHTML = r.waiting.length ? r.waiting.map(waitRow).join("") : '<div class="empty">Nothing waiting.</div>';
    }, function (e) { $("nmList").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; });
  }

  // ---------- Rule checklist (drawer and Check page) ----------
  function ruleRow(key, x, d) {
    var rule = ruleFor(key, x.unit) || { name: key, unit: "" }, res = x.result, on = x.enabled !== false;
    var wouldFail = /would have been (FAIL|DATA_INSUFFICIENT)/.test(x.note || x.notes || "");
    var ico = !on ? '<span class="ico off">' + I_OFF + "</span>" : res === "PASS" ? '<span class="ico ok">' + I_OK + "</span>" :
      res === "DATA_INSUFFICIENT" ? '<span class="ico wait">' + I_WAIT + "</span>" : res === "OBSERVE" ? '<span class="ico off">' + I_OFF + "</span>" :
      '<span class="ico no">' + I_NO + "</span>";
    var val = x.value, shown;
    if (key === "verification") shown = String(val) === "verified" || (res === "PASS" && !val) ? "Verified" : "No blue tick";
    else if (isNum(val)) shown = unitText(rule, val);
    else if (res === "DATA_INSUFFICIENT") shown = "Waiting";
    else shown = val == null || val === "None" ? "–" : String(val);
    var why;
    if (key === "verification") why = "Needs the OpenSea blue tick";
    else if (rule === FLOOR_SALES && x.limit != null) why = "At least " + unitText(rule, x.limit) + " in 7 days where a buyer bought a listing at " + floorBand() + rareText();
    else if (rule === OFFER_SALES && x.limit != null) why = "At least " + unitText(rule, x.limit) + " in 14 days where a seller accepted a collection offer";
    else if (x.limit != null) why = (rule.prefix || "Limit") + " " + unitText(rule, x.limit).replace("/day", " a day") + (rule === WEEK_SALES ? " in the last 7 days" : "");
    else why = rule.desc || "";
    if (key === "trading_frequency" && rule === RULE_BY_KEY.trading_frequency && x.limit != null) {
      var extras = [];
      if (minWeekSales() > 0) extras.push("at least " + unitText(WEEK_SALES, minWeekSales()) + " in 7 days");
      if (paceSetting("min_floor_sales_7d") > 0) extras.push(paceSetting("min_floor_sales_7d") + " at floor price");
      if (paceSetting("min_offer_sales_14d") > 0) extras.push(unitText(OFFER_SALES, paceSetting("min_offer_sales_14d")) + " to offers in 14 days");
      if (extras.length) why += ", " + extras.join(", ");
    }
    if (res === "DATA_INSUFFICIENT" && on) why = (key.indexOf("floor_change") === 0 ? "No saved floor price from back then yet" : "Not enough data");
    var whyHtml = esc(why), then = d && foundRate(d);
    if (key === "floor_change_1d" && d && isNum(d.floor_1d)) whyHtml += " · floor " + money(d.floor_1d, d.currency, then) + " → " + money(d.floor, d.currency, then);
    if (key === "floor_change_7d" && d && isNum(d.floor_7d)) whyHtml += " · floor " + money(d.floor_7d, d.currency, then) + " → " + money(d.floor, d.currency, then);
    if (!on) whyHtml += esc(" · switched off" + (wouldFail ? ", would have failed" : ""));
    return "<li>" + ico + '<div style="min-width:0"><div class="rule-name">' + esc(rule.name) + '</div><div class="rule-why">' + whyHtml + "</div></div>" +
      '<span class="rule-val">' + esc(shown) + "</span></li>";
  }
  function checklistFromDetails(d) {
    if (d.rules) return '<ul class="checklist">' + RULES.map(function (r) { return d.rules[r.key] ? ruleRow(r.key, d.rules[r.key], d) : ""; }).join("") + "</ul>";
    // Early exit: only the rules checked before it stopped
    var rows = (d.checked || []).map(function (k) { return ruleRow(k, { result: "PASS", enabled: true, limit: null, value: "" }, d); });
    if (d.rule) {
      var rule = RULE_BY_KEY[d.rule];
      if (rule) rows.push(ruleRow(d.rule, { result: d.rule.indexOf("floor_history") === 0 ? "DATA_INSUFFICIENT" : "FAIL", enabled: true, limit: d.limit, value: d.value, unit: d.unit }, d));
      else rows.push('<li><span class="ico wait">' + I_WAIT + '</span><div><div class="rule-name">' + esc(FUNNEL_LABELS[d.rule] || d.rule) + '</div><div class="rule-why">' + esc(d.reason || "") + "</div></div><span></span></li>");
    }
    return '<ul class="checklist">' + rows.join("") + '</ul><p class="chart-note" style="padding:0 20px 14px;margin:0">The bot stops at the first rule a collection fails, so later rules weren\'t checked.</p>';
  }

  // ---------- Drawer ----------
  var drawer = $("drawer"), overlay = $("overlay"), lastFocus = null;
  function closeBtn() {
    return '<button class="icon-btn" type="button" id="drawerClose" aria-label="Close"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 6l12 12M18 6L6 18"/></svg></button>';
  }
  function showDrawer(html) {
    drawer.innerHTML = html;
    drawer.classList.add("open"); overlay.classList.add("open"); drawer.setAttribute("aria-hidden", "false");
    if (!lastFocus) lastFocus = document.activeElement;
    var c = $("drawerClose"); if (c) { c.onclick = closeDrawer; c.focus(); }
  }
  function closeDrawer() {
    drawer.classList.remove("open"); overlay.classList.remove("open"); drawer.setAttribute("aria-hidden", "true");
    if (lastFocus && lastFocus.focus) lastFocus.focus(); lastFocus = null;
  }
  overlay.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeDrawer(); hideTip(); return; }
    var t = e.target;
    if ((e.key === "Enter" || e.key === " ") && t.getAttribute && t.getAttribute("role") === "button" && t.hasAttribute("data-open")) {
      e.preventDefault(); openDrawer(t.getAttribute("data-open"), t.getAttribute("data-date"));
    }
  });
  document.addEventListener("click", function (e) {
    var b = e.target.closest("[data-open]"); if (b) openDrawer(b.getAttribute("data-open"), b.getAttribute("data-date"));
  });

  // Each of last week's sales against the floor at the time: floor buys vs accepted offers vs rare items
  var PAID_LABELS = {
    floor: ["good", "Floor price"], below: ["warn", "Below floor"], above: ["neutral", "Above floor"], skipped: ["neutral", "Not compared"],
    offer: ["warn", "Accepted offer"], rare: ["neutral", "Rare item"]
  };
  function buyersPaid(d) {
    var fs = d.floor_sales;
    if (!fs || !fs.rows || !fs.rows.length) return "";
    var band = fmt(fs.min_pct, 0) + "–" + fmt(fs.max_pct, 0) + "%";
    var head = fs.count + " of " + fs.total + " at floor price";
    var rows = fs.rows.map(function (r) {
      var l = PAID_LABELS[r.label] || PAID_LABELS.skipped;
      var pct = isNum(r.pct) ? fmt(r.pct, 0) + "%" : esc(r.note || "");
      if (r.label === "rare" && isNum(r.rank)) l = [l[0], "Rare #" + int(r.rank)];
      else if (r.label === "below" && r.how === "offer") l = [l[0], "Accepted offer"];
      return '<tr><td class="paid-day">' + esc(shortDay(r.ts)) + '</td><td class="paid-amt">' + (isNum(r.price) ? money(r.price, r.currency) : "–") + "</td>" +
        '<td class="paid-pct">' + pct + '</td><td><span class="pill ' + l[0] + '">' + l[1] + "</span></td></tr>";
    }).join("");
    var all = fs.sale_rows || fs.total;
    var more = all > fs.rows.length ? '<p class="chart-note">Showing the latest ' + fs.rows.length + " of " + all + " sales.</p>" : "";
    return '<div class="card panel"><div class="section-title"><h2>What buyers paid</h2><span>' + esc(head) + "</span></div>" +
      '<table class="tbl paid"><thead><tr><th>Day</th><th>Paid</th><th>Of floor</th><th></th></tr></thead><tbody>' + rows + "</tbody></table>" + more +
      '<p class="chart-note">A floor-price sale is a listing bought at ' + band + " of the floor at the time" +
      (fs.rare_pct > 0 ? ", and not one of the rarest " + fmt(fs.rare_pct, 0) + "% of items" : "") +
      ". Accepted offers don't count, because they say nothing about whether your listing near the floor will sell.</p></div>";
  }

  // The sales where a seller accepted an offer in the last 14 days: which ones counted, and why
  var OFFER_LABELS = { confirmed: ["good", "Collection offer"], price: ["neutral", "Judged by price"],
                       trait: ["neutral", "Trait offer"], item: ["neutral", "One-item offer"] };
  function offerSales(d) {
    var os = d.offer_sales;
    if (!os || !isNum(os.count)) return "";
    var days = int(os.days || 14), covered = int(os.covered_days || days), rows = Array.isArray(os.rows) ? os.rows : null;
    var head = int(os.count) + (os.count === 1 ? " sale" : " sales") + " counted" + (isNum(os.needed) && os.needed > 0 ? " · needs " + int(os.needed) : "");
    var body = (rows || []).map(function (r) {
      var l = r.how === "other" ? (OFFER_LABELS[r.offer_type] || OFFER_LABELS.item) : (OFFER_LABELS[r.how] || OFFER_LABELS.price);
      var url = typeof r.url === "string" && r.url.indexOf("https://opensea.io/") === 0 ? r.url : "";
      var qty = isNum(r.qty) && r.qty > 1 ? ' <span class="sub">×' + int(r.qty) + "</span>" : "";
      return '<tr' + (r.how === "other" ? ' class="not-counted"' : "") + '><td class="paid-day">' + esc(shortDay(r.ts)) + '</td><td class="paid-amt">' +
        (isNum(r.price) ? money(r.price, r.currency) : "–") + qty + '</td><td class="paid-pct">' + (isNum(r.pct) ? fmt(r.pct, 0) + "%" : "") + "</td>" +
        '<td><span class="pill ' + l[0] + '">' + l[1] + "</span></td>" +
        '<td class="paid-link">' + (url ? '<a href="' + esc(url) + '" target="_blank" rel="noopener" aria-label="Open this item on OpenSea">View</a>' : "") + "</td></tr>";
    }).join("");
    var table;
    if (!rows) table = '<p class="chart-note" style="margin-top:0">The list of sales wasn\'t saved for this check. It appears after the next check.</p>';
    else if (rows.length) table = '<table class="tbl paid"><thead><tr><th>Day</th><th>Paid</th><th>Of floor</th><th></th><th></th></tr></thead><tbody>' + body + "</tbody></table>";
    else table = '<p class="chart-note" style="margin-top:0">No seller accepted an offer in the last ' + covered + " days.</p>";
    var notes = [];
    if (rows && isNum(os.row_count) && os.row_count > rows.length) notes.push("Showing " + rows.length + " of " + int(os.row_count) + " sales, counted ones first.");
    if (covered < days) notes.push("Sales from " + (covered + 1) + "-" + days + " days ago couldn't be downloaded this time, so only the last " + covered + " days are listed.");
    if (os.other_offers) notes.push(int(os.other_offers) + (os.other_offers === 1 ? " offer was" : " offers were") +
      " on a trait or one item, so " + (os.other_offers === 1 ? "it doesn't" : "they don't") + " count.");
    return '<div class="card panel"><div class="section-title"><h2>Sales to offers, last ' + covered + " days</h2><span>" + esc(head) + "</span></div>" + table +
      '<p class="chart-note">A seller accepted a collection offer, the kind of bid you place. "Judged by price" means OpenSea had no record of the order, ' +
      "but it sold well under the floor, which only an offer does. ×N means one sale of several items. " + notes.join(" ") + " View opens the item on OpenSea.</p></div>";
  }

  function drawerBody(d, extra, skipRules) {
    var cur = d.currency || "", then = foundRate(d), offThen = foundRate(d, true), lim = (state.overview && state.overview.limits) || {};
    var html = "";
    if (isNum(d.spread) || isNum(d.net)) {
      html += '<div class="hero-nums"><div class="card"><div class="l">Spread over top offer</div><div class="v ' + (d.spread >= (lim.offer_to_floor || 0) ? "c-good" : "c-warn") + '">' + fmt(d.spread, 1) +
        '%</div><div class="l">needs ' + esc(lim.offer_to_floor) + "%</div></div>" +
        '<div class="card"><div class="l">Estimated profit</div><div class="v">' + money(d.net, cur, then) + '</div><div class="l">' + fmt(d.roi, 1) + "% return</div></div></div>";
    }
    if (isNum(d.entry) && isNum(d.exit)) {
      html += '<div class="card panel"><div class="section-title"><h2>Trade plan</h2><span>Estimate, not a guarantee</span></div><table class="tbl"><tbody>' +
        "<tr><td>Buy with a bid " + fmt(d.bid_premium_pct, 1) + '% above top offer <span class="sub">(' + money(d.offer, d.offer_currency || cur, offThen) + ")</span></td><td>" + money(d.entry, cur, then) + "</td></tr>" +
        "<tr><td>Sell " + fmt(d.sell_discount_pct, 1) + '% under floor <span class="sub">(' + money(d.floor, cur, then) + ")</span></td><td>" + money(d.exit, cur, then) + "</td></tr>" +
        "<tr><td>OpenSea fee " + fmt(d.fee_pct, 1) + '% <span class="sub">(' + esc(d.fee_source || "") + ")</span></td><td>−" + money(d.fee_amount, cur, then) + "</td></tr>" +
        "<tr><td>Creator royalty " + fmt(d.royalty_pct, 1) + "%</td><td>−" + money(d.royalty_amount, cur, then) + "</td></tr>" +
        "<tr><td>Gas estimate</td><td>−" + money(d.gas, cur, then) + "</td></tr>" +
        '<tr class="total"><td>Net profit</td><td>' + money(d.net, cur, then) + "</td></tr></tbody></table>" +
        (d.economics_note ? '<p class="chart-note">' + esc(d.economics_note) + "</p>" : "") + "</div>";
    }
    var sd = d.sales_daily || [];
    if (sd.length) {
      var max = Math.max.apply(null, sd.map(function (x) { return x[1]; }).concat([1]));
      html += '<div class="card panel"><div class="section-title"><h2>Sales, last 7 days</h2><span>' + int(d.sales_7d) + " sales · " + fmt(d.pace, 2) + "/day</span></div>" +
        '<div class="mini-bars">' + sd.slice(-7).map(function (x) {
          var day = toDate(x[0] + "T12:00:00"), lbl = day ? day.toLocaleDateString("en-GB", { weekday: "short" }) : x[0];
          return '<div><span class="num" style="color:var(--fg);font-weight:600">' + x[1] + '</span><i class="' + (x[1] ? "" : "zero") + '" style="height:' + (x[1] / max * 46 + 2).toFixed(0) + 'px"></i>' + esc(lbl) + "</div>";
        }).join("") + "</div></div>";
    }
    html += buyersPaid(d) + offerSales(d);
    if (!skipRules) html += '<div class="card" style="overflow:hidden"><div class="section-title" style="padding:16px 20px 0;margin:0"><h2>Rules</h2></div>' + checklistFromDetails(d) + "</div>";
    if ((d.data_notes || []).length) html += '<div class="card panel"><div class="section-title"><h2>Data notes</h2></div><ul class="notes">' + d.data_notes.map(function (n) { return "<li>" + esc(n) + "</li>"; }).join("") + "</ul></div>";
    return html + (extra || "");
  }

  function openDrawer(slug, date) {
    showDrawer('<div class="drawer-head"><div style="flex:1"><h2 id="drawerTitle">' + esc(slug) + "</h2></div>" + closeBtn() + '</div><div class="drawer-body"><div class="loading">Loading…</div></div>');
    api("/api/candidate?slug=" + encodeURIComponent(slug) + (date ? "&date=" + encodeURIComponent(date) : "")).then(function (r) {
      var d = r.details || {}, url = d.opensea_url || ("https://opensea.io/collection/" + encodeURIComponent(slug));
      var pill = r.is_pass ? '<span class="pill good">Candidate</span>' :
        (r.reject_filter || "").indexOf("floor_history") === 0 ? '<span class="pill neutral">Waiting for floor history</span>' :
        '<span class="pill bad">Not a candidate</span>';
      var extra = "";
      if (d.info_md) {
        extra += '<div class="path"><code>' + esc(d.info_md) + '</code><button class="btn" type="button" id="copyPath" style="padding:4px 9px;font-size:12px">Copy</button></div>';
      }
      extra += '<div style="display:flex;gap:8px;flex-wrap:wrap"><a class="btn primary" href="' + esc(url) + '" target="_blank" rel="noopener">Open on OpenSea</a>' +
        (r.info_md_content ? '<button class="btn" type="button" id="openInfo">Show Info.md</button>' : "") + "</div>" +
        (r.info_md_content ? '<pre class="md" id="infoMd" hidden>' + esc(r.info_md_content) + "</pre>" : "");
      showDrawer('<div class="drawer-head">' + art(slug, d.image) + '<div style="min-width:0"><h2 id="drawerTitle">' + esc(d.name || slug) + (d.verified ? TICK : "") + "</h2>" +
        '<div class="cand-meta">' + esc(chainName(d.chain)) + (isNum(d.supply) ? " · " + int(d.supply) + " items" : "") + (isNum(d.owners) ? " · " + int(d.owners) + " owners" : "") + "</div></div>" + closeBtn() + "</div>" +
        '<div class="drawer-body"><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">' + pill +
        '<span class="muted" style="font-size:12.5px">' + (r.is_pass ? "Found " : "Checked ") + esc(fmtDay(d.found_at || r.created_at, true)) + "</span></div>" +
        drawerBody(d, extra) + "</div>");
      var cp = $("copyPath");
      if (cp) cp.onclick = function () {
        try { navigator.clipboard.writeText(d.info_md).then(function () { toast("Path copied"); }, function () { toast(d.info_md); }); } catch (e) { toast(d.info_md); }
      };
      var oi = $("openInfo");
      if (oi) oi.onclick = function () { var m = $("infoMd"); m.hidden = !m.hidden; oi.textContent = m.hidden ? "Show Info.md" : "Hide Info.md"; };
    }, function (e) {
      showDrawer('<div class="drawer-head"><div style="flex:1"><h2 id="drawerTitle">' + esc(slug) + "</h2></div>" + closeBtn() + '</div><div class="drawer-body"><div class="empty">' + esc(e.message) + "</div></div>");
    });
  }

  // ---------- Check page ----------
  $("checkForm").addEventListener("submit", function (e) {
    e.preventDefault();
    var q = $("checkInput").value.trim(); if (!q) return;
    var slug = q.toLowerCase().replace(/^.*opensea\.io\/(?:[a-z-]+\/)?collection\//, "").replace(/[\/?#].*$/, "").replace(/\s+/g, "-");
    var out = $("checkOut");
    out.innerHTML = '<div class="card steps"><div><span class="spinner"></span>Checking ' + esc(slug) + " on OpenSea. This takes up to a minute…</div></div>";
    api("/api/inspect?slug=" + encodeURIComponent(slug)).then(function (r) {
      var d = r.details || { rules: {} };
      if (!r.details) { Object.keys(r.criteria || {}).forEach(function (k) { var c = r.criteria[k]; d.rules[k] = { result: c.result, enabled: c.enabled, value: c.actual_value, limit: c.threshold, note: c.notes }; }); }
      var failed = RULES.filter(function (x) { var c = d.rules[x.key]; return c && c.enabled && c.result !== "PASS"; });
      var waiting = failed.filter(function (x) { return d.rules[x.key].result === "DATA_INSUFFICIENT"; });
      var headline = r.is_overall_pass ? "Would be a candidate" : failed.length === waiting.length ? "Not yet: waiting for floor history" : "Not a candidate";
      var pill = r.is_overall_pass ? '<span class="pill good">Passes all ' + RULES.filter(function (x) { return d.rules[x.key] && d.rules[x.key].enabled; }).length + " rules</span>" :
        '<span class="pill ' + (failed.length === waiting.length ? "neutral" : "bad") + '">Fails ' + failed.length + " rule" + (failed.length === 1 ? "" : "s") + "</span>";
      out.innerHTML = '<div class="card" style="overflow:hidden"><div class="verdict">' + art(slug, d.image) +
        '<div style="flex:1 1 200px;min-width:0"><h2>' + headline + '</h2><div class="cand-meta">' + esc(d.name || slug) + (d.chain ? " · " + esc(chainName(d.chain)) : "") + " · checked just now</div></div>" + pill +
        "</div>" + checklistFromDetails(d) + "</div>" +
        (r.details ? '<div style="display:flex;flex-direction:column;gap:16px">' + drawerBody(d, "", true) + "</div>" : "");
    }, function (e) {
      out.innerHTML = '<div class="card empty"><h3>Couldn\'t check that</h3>' + esc(e.message) + "</div>";
    });
  });

  // ---------- Settings ----------
  function clone(o) { return JSON.parse(JSON.stringify(o)); }
  function isDirty() { return !!(state.settings && state.draft && JSON.stringify(state.draft) !== JSON.stringify(state.settings)); }
  function sw(id, on, label) { return '<button class="switch" type="button" role="switch" id="' + id + '" aria-checked="' + on + '" aria-label="' + esc(label) + '"></button>'; }

  function gasUsd(c) { var u = toUsd(c.gas, c.coin); return u == null ? "" : "≈ " + usd(u); }
  function renderSettings() {
    var s = state.draft; if (!s) return;
    $("ruleRows").innerHTML = RULES.map(function (r) {
      var cfg = s.rules[r.key];
      var dis = cfg.enabled ? "" : " disabled";
      var field = r.kind === "bool" ? '<span class="field">Required</span>' :
        '<label class="field" for="v-' + r.key + '">' + r.prefix + ' <input id="v-' + r.key + '" type="number" step="any" min="0" value="' + esc(cfg.value) + '"' + dis + "> " + (r.unit === "per day" ? "a day" : r.unit) + "</label>";
      if (r.key === "trading_frequency") {
        field = '<div class="field-stack">' + field + '<label class="field" for="v2-trading_frequency">At least <input id="v2-trading_frequency" type="number" step="1" min="0" value="' +
          esc(cfg.min_sales_7d) + '"' + dis + "> sales in 7 days</label>" +
          '<label class="field" for="v3-trading_frequency" title="Paid ' + fmt(cfg.floor_sale_min_pct || 90, 0) + "–" + fmt(cfg.floor_sale_max_pct || 115, 0) +
          '% of the floor at the time. 0 turns this off.">At least <input id="v3-trading_frequency" type="number" step="1" min="0" value="' +
          esc(cfg.min_floor_sales_7d) + '"' + dis + "> at floor price</label>" +
          '<label class="field" for="v5-trading_frequency" title="A floor-price sale of an item this rare doesn\'t count. 0 turns this off.">Skip the rarest <input id="v5-trading_frequency" type="number" step="any" min="0" max="100" value="' +
          esc(cfg.rare_item_pct) + '"' + dis + ">% of items</label>" +
          '<label class="field" for="v4-trading_frequency" title="A seller accepted a collection offer, the kind of bid you place. 0 turns this off.">At least <input id="v4-trading_frequency" type="number" step="1" min="0" value="' +
          esc(cfg.min_offer_sales_14d) + '"' + dis + "> offer sales in 14 days</label></div>";
      }
      return '<div class="set-row' + (cfg.enabled ? "" : " off") + '">' + sw("on-" + r.key, cfg.enabled, r.name) +
        '<div style="min-width:0"><div class="label">' + r.name + '</div><div class="desc">' + r.desc + "</div>" +
        (cfg.enabled ? "" : '<div class="off-note">Off: still measured, but it won\'t reject collections</div>') + "</div>" + field + "</div>";
    }).join("");

    var tr = s.trade;
    $("tradeRows").innerHTML = [
      ["bid", "Bid above the top offer", "How much higher than the current top offer your bid goes", "%"],
      ["sell", "Sell below the floor", "Discount under the floor price for a quick sale", "%"],
      ["fee", "OpenSea fee", "Used when OpenSea doesn't list its fee for a collection", "%"]
    ].map(function (f) {
      return '<div class="set-row plain"><div style="min-width:0"><div class="label">' + f[1] + '</div><div class="desc">' + f[2] + "</div></div>" +
        '<label class="field" for="t-' + f[0] + '"><input id="t-' + f[0] + '" type="number" step="any" min="0" value="' + esc(tr[f[0]]) + '"> ' + f[3] + "</label></div>";
    }).join("");

    var on = s.chains.filter(function (c) { return c.enabled; }).length;
    $("chainTools").innerHTML = '<button class="btn" type="button" id="chAll">Select all</button><button class="btn" type="button" id="chNone">Select none</button>' +
      '<span class="muted">' + on + " of " + s.chains.length + " chains on</span>";
    var groups = { eth: "Priced in ETH", native: "Priced in the chain's own coin", stable: "Priced in stablecoins" }, last = null, rows = "";
    s.chains.forEach(function (c, i) {
      if (c.kind !== last) { rows += '<div class="chain-group">' + groups[c.kind] + "</div>"; last = c.kind; }
      rows += '<div class="chain-row' + (c.enabled ? "" : " off") + '">' + sw("ch-" + i, c.enabled, c.name) +
        '<div style="min-width:0"><div class="label">' + esc(c.name) + '</div><div class="desc">Gas paid in ' + esc(c.coin) + "</div></div>" +
        '<label class="field" for="g-' + i + '">Gas <input id="g-' + i + '" type="number" step="any" min="0" value="' + esc(c.gas) + '"> ' + esc(c.coin) +
        ' <span class="gas-usd" id="gu-' + i + '">' + gasUsd(c) + "</span></label></div>";
    });
    $("chainRows").innerHTML = rows;

    var rt = s.runtime;
    var auto = s.autostart.installed ? '<span class="pill good">Set up</span>' : '<span class="pill warn">Not set up</span>';
    $("macRows").innerHTML =
      '<div class="set-row plain"><div style="min-width:0"><div class="label">Start when the Mac turns on</div><div class="desc">' +
      (s.autostart.installed ? "After a power cut the dashboard comes back and the bot carries on where it stopped. If you turned the bot off here, it stays off." :
        'Run <span class="cmd">python bot.py install-autostart</span> once in Terminal to set this up.') + "</div></div>" + auto + "</div>" +
      [["skip_unverified", "Skip collections without a blue tick", "Saves API calls. Only used while the blue tick rule is on.", s.skip_unverified],
       ["wait_for_internet", "Wait for the internet", "Pauses while offline instead of marking collections as failed.", rt.wait_for_internet],
       ["notify_new_candidates", "Mac notification for new candidates", "Shows a banner in the top-right corner of the screen.", rt.notify_new_candidates],
       ["notification_sound", "Play a sound with notifications", "", rt.notification_sound]].map(function (f) {
        return '<div class="set-row">' + sw("m-" + f[0], f[3], f[1]) + '<div style="min-width:0"><div class="label">' + f[1] + '</div><div class="desc">' + f[2] + "</div></div><span></span></div>";
      }).join("");
    $("saveBar").hidden = !isDirty();
  }

  function loadSettings() {
    return api("/api/settings").then(function (s) {
      state.settings = s;
      if (!isDirty()) { state.draft = clone(s); renderSettings(); }
    }, function (e) { toast(e.message); });
  }

  $("page-settings").addEventListener("click", function (e) {
    var b = e.target.closest("button"); if (!b || !state.draft) return;
    var s = state.draft, id = b.id;
    if (id.indexOf("on-") === 0) { var k = id.slice(3); s.rules[k].enabled = !s.rules[k].enabled; }
    else if (id.indexOf("ch-") === 0) { var c = s.chains[+id.slice(3)]; c.enabled = !c.enabled; }
    else if (id === "chAll" || id === "chNone") { s.chains.forEach(function (c) { c.enabled = id === "chAll"; }); }
    else if (id === "m-skip_unverified") { s.skip_unverified = !s.skip_unverified; }
    else if (id.indexOf("m-") === 0) { var mk = id.slice(2); s.runtime[mk] = !s.runtime[mk]; }
    else return;
    renderSettings();
  });
  $("page-settings").addEventListener("input", function (e) {
    var el = e.target, v = parseFloat(el.value), s = state.draft;
    if (!s || isNaN(v)) return;
    if (el.id.indexOf("v-") === 0) s.rules[el.id.slice(2)].value = v;
    else if (el.id === "v2-trading_frequency") s.rules.trading_frequency.min_sales_7d = Math.max(0, Math.round(v));
    else if (el.id === "v3-trading_frequency") s.rules.trading_frequency.min_floor_sales_7d = Math.max(0, Math.round(v));
    else if (el.id === "v4-trading_frequency") s.rules.trading_frequency.min_offer_sales_14d = Math.max(0, Math.round(v));
    else if (el.id === "v5-trading_frequency") s.rules.trading_frequency.rare_item_pct = Math.min(100, Math.max(0, v));
    else if (el.id.indexOf("t-") === 0) s.trade[el.id.slice(2)] = v;
    else if (el.id.indexOf("g-") === 0) { s.chains[+el.id.slice(2)].gas = v; var gu = $("gu-" + el.id.slice(2)); if (gu) gu.textContent = gasUsd(s.chains[+el.id.slice(2)]); }
    $("saveBar").hidden = !isDirty();
  });
  $("btnDiscard").addEventListener("click", function () { state.draft = clone(state.settings); renderSettings(); toast("Changes discarded"); });
  $("btnSave").addEventListener("click", function () {
    var s = state.draft;
    if (!s.chains.some(function (c) { return c.enabled; })) { toast("Switch on at least one chain."); return; }
    var btn = this; btn.disabled = true;
    post("/api/settings", { rules: s.rules, trade: s.trade, chains: s.chains.map(function (c) { return { id: c.id, enabled: c.enabled, gas: c.gas }; }),
                            skip_unverified: s.skip_unverified, runtime: s.runtime })
      .then(function (r) {
        state.settings = r.settings; state.draft = clone(r.settings); renderSettings();
        toast("Saved. The bot uses these from its next check.");
        loadOverview();
      }, function (e) { toast(e.message || "Couldn't save."); })
      .then(function () { btn.disabled = false; });
  });
  window.addEventListener("beforeunload", function (e) { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });

  // ---------- Start ----------
  function refresh() {
    if (document.hidden) return;
    loadOverview();
    var page = (location.hash || "#home").slice(1);
    if (page === "candidates") loadCandidates();
    if (page === "near") loadNear();
  }
  loadPrices();
  setInterval(loadPrices, 5 * 60 * 1000);
  loadSettings().then(function () {
    loadOverview().then(function () { loadNear(); });
    var start = (location.hash || "").replace("#", "");
    if (start) go(start);
  });
  window.addEventListener("hashchange", function () {
    var page = (location.hash || "#home").slice(1);
    if (!$("page-" + page) || !$("page-" + page).classList.contains("active")) go(page);
  });
  setInterval(refresh, 10000);
  document.addEventListener("visibilitychange", refresh);
})();
