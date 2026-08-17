/* Dashboard behaviour: fetch three JSON documents, render cards, an SVG line
   chart and a breach feed. Deliberately dependency-free — the whole page is
   ~15 KB, which is the point of a serving layer that pre-aggregates. */

(function () {
  "use strict";

  var CONFIG = window.WEATHER_CONFIG || {};
  var API_BASE = (CONFIG.apiBase || "").replace(/\/+$/, "");
  var REFRESH_MS = (CONFIG.refreshSeconds || 60) * 1000;

  // With no API configured, read the sample documents committed next to the
  // page so it renders locally and in review.
  var ENDPOINTS = API_BASE
    ? {
        latest: API_BASE + "/api/latest",
        timeseries: API_BASE + "/api/timeseries",
        breaches: API_BASE + "/api/breaches",
        advice: API_BASE + "/api/advice",
        ask: API_BASE + "/api/ask",
        feedback: API_BASE + "/api/advice/feedback",
      }
    : {
        latest: "./data/latest.json",
        timeseries: "./data/timeseries_24h.json",
        breaches: "./data/breaches_24h.json",
        advice: "./data/advice.json",
        ask: null,
        feedback: null,
      };

  var SERIES_COLORS = ["--s1", "--s2", "--s3", "--s4", "--s5"];

  var METRICS = {
    temp_c: { label: "Temperature", unit: "°C", decimals: 1 },
    humidity: { label: "Humidity", unit: "%", decimals: 0 },
    wind_kph: { label: "Wind", unit: " km/h", decimals: 1 },
    pm2_5: { label: "PM2.5", unit: " µg/m³", decimals: 1 },
  };

  var state = {
    metric: "temp_c",
    timeseries: null,
    adviceId: null,
    // "welcome" (idle) or "card" (a live advice card is showing). Lets a poll
    // that finds calm weather return a card to the welcome state without
    // stomping a welcome the user is already reading.
    adviceMode: "welcome",
    // Remembered so a follow-up question re-queries the same location the
    // card was built for.
    adviceLocation: null,
  };

  /* ---------------- advice ----------------
     A strictly secondary feature: it is requested only after the weather has
     rendered, every failure is swallowed, and nothing here can throw into the
     weather refresh path. */

  var SESSION_KEY = "weather-advice-session";
  var DISMISSED_KEY = "weather-advice-dismissed";

  function sessionId() {
    try {
      var existing = localStorage.getItem(SESSION_KEY);
      if (existing) return existing;
      var fresh =
        (window.crypto && window.crypto.randomUUID && window.crypto.randomUUID()) ||
        String(Date.now()) + Math.random().toString(16).slice(2);
      localStorage.setItem(SESSION_KEY, fresh);
      return fresh;
    } catch (e) {
      // Private browsing blocks storage; a per-load id still works, it just
      // forgets frequency state between reloads.
      return String(Date.now());
    }
  }

  function dismissed() {
    try {
      return JSON.parse(sessionStorage.getItem(DISMISSED_KEY) || "[]");
    } catch (e) {
      return [];
    }
  }

  function rememberDismissed(id) {
    try {
      var all = dismissed();
      all.push(id);
      sessionStorage.setItem(DISMISSED_KEY, JSON.stringify(all.slice(-20)));
    } catch (e) {
      /* nothing to do */
    }
  }

  function sendFeedback(card, event) {
    if (!ENDPOINTS.feedback) return;
    var body = JSON.stringify({
      event: event,
      session_id: sessionId(),
      recommendation_id: card.recommendation_id,
      trigger_code: card.trigger_code,
      location: card.location,
      weather_snapshot_id: card.weather_snapshot_id,
      generation_method: card.generation_method,
    });
    try {
      // text/plain keeps this a CORS-simple request, so telemetry never costs
      // a preflight round trip. The API parses the body regardless.
      fetch(ENDPOINTS.feedback, {
        method: "POST",
        headers: { "Content-Type": "text/plain;charset=UTF-8" },
        body: body,
        keepalive: true,
      }).catch(function () {});
    } catch (e) {
      /* feedback is best-effort by design */
    }
  }

  var TRIGGER_ICONS = {
    RAIN_EXPECTED: "\u2602",
    HIGH_UV: "\u2600",
    EXTREME_HEAT: "\ud83c\udf21",
    HIGH_WIND: "\ud83c\udf2c",
  };

  // The assistant panel is permanent. When there is nothing to advise it shows
  // a welcome, not nothing — that is the whole point of pinning it. An optional
  // reply is passed after the user asks something in calm weather, so the input
  // never leads to silence.
  function renderWelcome(replyText) {
    var slot = el("advice-slot");
    var wrap = document.createElement("div");
    wrap.className = "assistant-welcome";
    wrap.innerHTML =
      '<div class="assistant-welcome__row">' +
      '<div class="assistant-welcome__icon">🌤️</div>' +
      "<div>" +
      '<p class="assistant-welcome__title">欢迎来到天气看板</p>' +
      '<p class="assistant-welcome__text">今天有什么安排吗？让我帮你看看该准备什么。</p>' +
      "</div></div>" +
      (replyText
        ? '<p class="assistant-welcome__reply">' + escapeHtml(replyText) + "</p>"
        : "");
    wrap.appendChild(questionForm());
    slot.innerHTML = "";
    slot.appendChild(wrap);
    slot.hidden = false;
    state.adviceId = null;
    state.adviceMode = "welcome";
  }

  // The answer to a user's question, from /api/ask: a forecast readout, or an
  // honest "here's what I can answer". Rendered into the same panel, with the
  // input kept so the next question is one tap away.
  function renderAnswer(payload) {
    var slot = el("advice-slot");
    var wrap = document.createElement("div");
    wrap.className = "assistant-welcome";

    var detail = "";
    if (payload.detail && payload.detail.length) {
      detail =
        '<div class="advice__evidence">' +
        payload.detail
          .map(function (d) {
            return (
              '<span class="advice__chip">' +
              escapeHtml(d.label) +
              " " +
              escapeHtml(d.value) +
              "</span>"
            );
          })
          .join("") +
        "</div>";
    }

    wrap.innerHTML =
      '<div class="assistant-welcome__row">' +
      '<div class="assistant-welcome__icon">💬</div>' +
      "<div>" +
      (payload.title
        ? '<p class="assistant-welcome__title">' + escapeHtml(payload.title) + "</p>"
        : "") +
      '<p class="assistant-welcome__text">' +
      escapeHtml(payload.message || "") +
      "</p>" +
      detail +
      "</div></div>" +
      sourcesHtml(payload.sources);
    wrap.appendChild(questionForm());

    slot.innerHTML = "";
    slot.appendChild(wrap);
    slot.hidden = false;
    state.adviceId = null;
    // Treated like the welcome for polling: a calm poll leaves the answer
    // alone, but a fresh rule card (a real hazard) still takes over.
    state.adviceMode = "answer";
  }

  // Kept for the dismiss/mute handlers: a dismissed card returns the panel to
  // its welcome state rather than hiding it.
  function clearAdvice() {
    renderWelcome();
  }

  // The assistant input. A question goes to /api/ask (forecast lookup, or an
  // honest "here's what I can answer"), NOT to /api/advice — that endpoint is
  // for the rule-triggered card, which appears on its own.
  function questionForm() {
    var form = document.createElement("form");
    form.className = "advice__ask";
    form.innerHTML =
      '<input class="advice__ask-input" type="text" maxlength="200" ' +
      'placeholder="例如：明天会下雨吗" ' +
      'aria-label="向天气助手提问">' +
      '<button class="advice__ask-button" type="submit">问</button>';
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var input = form.querySelector(".advice__ask-input");
      var button = form.querySelector(".advice__ask-button");
      var question = (input.value || "").trim();
      if (!question) return;
      // Disable only for the duration of the request, to stop a double-submit;
      // always re-enabled when it settles (see `.finally`).
      input.disabled = true;
      if (button) button.disabled = true;
      askQuestion(question).finally(function () {
        input.disabled = false;
        if (button) button.disabled = false;
      });
    });
    return form;
  }

  // Answer a user question. Online it asks /api/ask; offline it reads the
  // forecast straight out of the loaded sample so the demo still works.
  function askQuestion(question) {
    if (!API_BASE) {
      renderAnswer(offlineForecastAnswer(question));
      return Promise.resolve();
    }
    var loc = state.adviceLocation;
    if (!loc) {
      renderAnswer({ kind: "unknown", message: "还没拿到地点，稍等一下再问。" });
      return Promise.resolve();
    }
    var url =
      ENDPOINTS.ask +
      "?location=" +
      encodeURIComponent(loc) +
      "&q=" +
      encodeURIComponent(question);
    return fetch(url, { cache: "no-store" })
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (payload) {
        renderAnswer(
          payload || { kind: "unknown", message: "网络不太顺，稍等再问一次试试。" }
        );
      })
      .catch(function () {
        renderAnswer({ kind: "unknown", message: "网络不太顺，稍等再问一次试试。" });
      });
  }

  // Offline-only: a plain readout of the loaded forecast. Deliberately dumber
  // than the server (no day parsing, no rain phrasing) so the assistant's real
  // logic lives in one place — the backend — and this is just a sample view.
  function offlineForecastAnswer(question) {
    var loc = (state.latest && (state.latest.locations || [])[0]) || null;
    if (!loc || !(loc.forecast || []).length) {
      return { kind: "unknown", message: "样本模式下暂无预报数据。" };
    }
    var lines = loc.forecast.slice(0, 3).map(function (d) {
      var piece = d.date + "：" + (d.condition_text || "");
      if (d.chance_of_rain != null) piece += "，降水 " + d.chance_of_rain + "%";
      if (d.maxtemp_c != null) piece += "，最高 " + d.maxtemp_c + "°C";
      return piece;
    });
    return {
      kind: "forecast",
      title: loc.name + " · 未来三天（样本）",
      message: lines.join("；") + "。",
    };
  }

  function sourcesHtml(sources) {
    // A template card has no sources and renders exactly as it did in v1.1;
    // the citation row only appears when the copy was actually grounded.
    if (!sources || !sources.length) return "";

    var links = sources
      .map(function (source) {
        // rel="noopener noreferrer" because these are third-party links, and
        // the authority is shown so the reader knows who is being cited before
        // deciding to follow it.
        return (
          '<a class="advice__source" href="' +
          escapeHtml(source.source_url) +
          '" target="_blank" rel="noopener noreferrer">' +
          escapeHtml(source.authority || source.title) +
          "</a>"
        );
      })
      .join("");

    return '<p class="advice__sources"><span>依据：</span>' + links + "</p>";
  }

  function renderAdvice(card) {
    var slot = el("advice-slot");
    var wrapper = document.createElement("div");
    wrapper.className = "advice";
    wrapper.setAttribute("data-severity", card.severity || "info");

    var chips = (card.evidence || [])
      .map(function (item) {
        return (
          '<span class="advice__chip">' +
          escapeHtml(item.label) +
          " " +
          escapeHtml(item.value) +
          "</span>"
        );
      })
      .join("");

    wrapper.innerHTML =
      '<div class="advice__icon">' +
      (TRIGGER_ICONS[card.trigger_code] || "\u2139") +
      "</div>" +
      '<div class="advice__body">' +
      '<p class="advice__title">' +
      escapeHtml(card.title) +
      "</p>" +
      '<p class="advice__message">' +
      escapeHtml(card.message) +
      "</p>" +
      (chips ? '<div class="advice__evidence">' + chips + "</div>" : "") +
      sourcesHtml(card.sources) +
      '<p class="advice__meta">' +
      escapeHtml(card.location) +
      " · 天气数据更新于 " +
      escapeHtml(timeAgoZh(card.weather_observed_at_utc)) +
      "</p>" +
      '<div class="advice__actions"></div>' +
      "</div>";

    if (card.sources && card.sources.length) {
      // A follow-up input is only offered on a grounded card, because only the
      // retrieval path can act on the question. On a template card it would do
      // nothing, and an input that does nothing is worse than none.
      wrapper.querySelector(".advice__body").appendChild(questionForm());
    }

    var actions = wrapper.querySelector(".advice__actions");
    (card.actions || []).forEach(function (action) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "advice__button";
      button.textContent = action.label;
      button.addEventListener("click", function () {
        rememberDismissed(card.recommendation_id);
        sendFeedback(card, action.type === "mute" ? "muted" : "dismissed");
        clearAdvice();
      });
      actions.appendChild(button);
    });

    slot.innerHTML = "";
    slot.appendChild(wrapper);
    slot.hidden = false;
    state.adviceId = card.recommendation_id;
    state.adviceMode = "card";
    sendFeedback(card, "shown");
  }

  // Polls the rule-triggered advice card (not user questions — those go through
  // askQuestion → /api/ask). Called on every refresh for the primary location.
  function refreshAdvice(location) {
    if (!ENDPOINTS.advice || !location) return Promise.resolve();
    state.adviceLocation = location;

    var url = API_BASE
      ? ENDPOINTS.advice +
        "?location=" +
        encodeURIComponent(location) +
        "&session=" +
        encodeURIComponent(sessionId())
      : ENDPOINTS.advice;

    // A card stopped applying (calm weather, dismissed, expired). Only step
    // back to the welcome if a card was actually showing — never stomp a
    // welcome or a Q&A answer the user is reading.
    var showNoCard = function () {
      if (state.adviceMode === "card") renderWelcome();
    };

    return fetch(url, { cache: "no-store" })
      .then(function (response) {
        if (response.status === 204) return { none: true };
        if (!response.ok) return null;
        return response.json();
      })
      .then(function (payload) {
        if (payload === null) return; // transient error: leave the panel as-is
        // Offline the document wraps the card so "no advice" is representable.
        var card = payload.none ? null : payload.card !== undefined ? payload.card : payload;
        if (!card) {
          showNoCard();
          return;
        }
        // Re-rendering an identical card on every poll is what makes this kind
        // of feature feel like a popup. Same id means leave the DOM alone.
        if (card.recommendation_id === state.adviceId) return;
        if (dismissed().indexOf(card.recommendation_id) !== -1) {
          showNoCard();
          return;
        }
        if (parseTime(card.expires_at_utc) && parseTime(card.expires_at_utc) < new Date()) {
          showNoCard();
          return;
        }
        renderAdvice(card);
      })
      .catch(function () {
        // Advice is optional; the weather page has already rendered.
      });
  }

  /* ---------------- helpers ---------------- */

  function el(id) {
    return document.getElementById(id);
  }

  function text(value, fallback) {
    return value === null || value === undefined || value === "" ? fallback || "—" : String(value);
  }

  function num(value, decimals, unit) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    return Number(value).toFixed(decimals === undefined ? 1 : decimals) + (unit || "");
  }

  function parseTime(value) {
    if (!value) return null;
    var d = new Date(value);
    return isNaN(d.getTime()) ? null : d;
  }

  function timeAgo(value) {
    var d = parseTime(value);
    if (!d) return "unknown";
    var seconds = Math.round((Date.now() - d.getTime()) / 1000);
    if (seconds < 0) return "just now";
    if (seconds < 90) return seconds + "s ago";
    var minutes = Math.round(seconds / 60);
    if (minutes < 90) return minutes + "m ago";
    var hours = Math.round(minutes / 60);
    if (hours < 36) return hours + "h ago";
    return Math.round(hours / 24) + "d ago";
  }

  // The advice card is written in Chinese, so its metadata is too — mixing
  // "更新于" with "82s ago" in one line reads as a bug.
  function timeAgoZh(value) {
    var d = parseTime(value);
    if (!d) return "未知";
    var seconds = Math.round((Date.now() - d.getTime()) / 1000);
    if (seconds < 90) return Math.max(seconds, 0) + " 秒前";
    var minutes = Math.round(seconds / 60);
    if (minutes < 90) return minutes + " 分钟前";
    var hours = Math.round(minutes / 60);
    if (hours < 36) return hours + " 小时前";
    return Math.round(hours / 24) + " 天前";
  }

  function clockTime(value) {
    var d = parseTime(value);
    if (!d) return "—";
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // The air quality index is a 1-6 scale; turning it into words is the
  // difference between a number and a dashboard.
  function epaLabel(index) {
    var labels = {
      1: ["Good", "ok"],
      2: ["Moderate", "ok"],
      3: ["Unhealthy for sensitive", "warn"],
      4: ["Unhealthy", "warn"],
      5: ["Very unhealthy", "crit"],
      6: ["Hazardous", "crit"],
    };
    return labels[index] || null;
  }

  function setStatus(state_, message) {
    var wrapper = el("status");
    wrapper.querySelector(".status__dot").setAttribute("data-state", state_);
    wrapper.querySelector(".status__text").textContent = message;
  }

  function fetchJson(url) {
    return fetch(url, { cache: "no-store" }).then(function (response) {
      if (!response.ok) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (body) {
            var error = new Error(body.detail || "HTTP " + response.status);
            error.status = response.status;
            throw error;
          });
      }
      return response.json();
    });
  }

  /* ---------------- location cards ---------------- */

  function renderLocations(payload) {
    var container = el("locations");
    var locations = (payload && payload.locations) || [];

    if (!locations.length) {
      container.innerHTML = '<p class="empty">No observations yet.</p>';
      return;
    }

    container.innerHTML = "";
    locations.forEach(function (loc) {
      var card = document.createElement("article");
      card.className = "card";

      var epa = epaLabel(loc.us_epa_index);
      var head = document.createElement("div");
      head.className = "card__head";
      head.innerHTML =
        '<div class="card__place">' +
        escapeHtml(text(loc.name)) +
        '<span class="card__country">' +
        escapeHtml(text(loc.country, "")) +
        "</span></div>" +
        (epa ? '<span class="pill pill--' + epa[1] + '">' + escapeHtml(epa[0]) + "</span>" : "");

      var temp = document.createElement("div");
      temp.className = "card__temp";
      temp.textContent = num(loc.temp_c, 1, "°C");

      var condition = document.createElement("p");
      condition.className = "card__condition";
      condition.textContent =
        text(loc.condition_text) +
        (loc.feelslike_c !== null && loc.feelslike_c !== undefined
          ? " · feels like " + num(loc.feelslike_c, 0, "°C")
          : "");

      var metrics = document.createElement("dl");
      metrics.className = "metrics";
      [
        ["Humidity", num(loc.humidity, 0, "%")],
        ["Wind", num(loc.wind_kph, 1, " km/h") + " " + text(loc.wind_dir, "")],
        ["Pressure", num(loc.pressure_mb, 0, " mb")],
        ["UV", num(loc.uv, 0, "")],
        ["PM2.5", num(loc.pm2_5, 1, "")],
        ["Readings 24h", text(loc.observation_count_24h, "0")],
      ].forEach(function (pair) {
        var row = document.createElement("div");
        row.className = "metric";
        row.innerHTML =
          "<dt>" + escapeHtml(pair[0]) + "</dt><dd>" + escapeHtml(pair[1].trim()) + "</dd>";
        metrics.appendChild(row);
      });

      var foot = document.createElement("div");
      foot.className = "card__foot";
      foot.textContent = "Observed " + timeAgo(loc.observed_at_utc);

      card.appendChild(head);
      card.appendChild(temp);
      card.appendChild(condition);
      card.appendChild(metrics);
      card.appendChild(foot);
      container.appendChild(card);
    });
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  /* ---------------- chart ---------------- */

  function renderChart() {
    var container = el("chart");
    var legend = el("legend");
    var payload = state.timeseries;
    var series = (payload && payload.series) || [];
    var metric = state.metric;
    var meta = METRICS[metric];

    var prepared = series
      .map(function (s, index) {
        var points = (s.points || [])
          .map(function (p) {
            var t = parseTime(p.t);
            var v = p[metric];
            return t && v !== null && v !== undefined && !isNaN(v)
              ? { t: t.getTime(), v: Number(v) }
              : null;
          })
          .filter(Boolean)
          .sort(function (a, b) {
            return a.t - b.t;
          });
        return { name: s.name || s.location_key, points: points, color: SERIES_COLORS[index % 5] };
      })
      .filter(function (s) {
        return s.points.length > 1;
      });

    if (!prepared.length) {
      container.innerHTML = '<p class="empty">Not enough history to plot yet.</p>';
      legend.innerHTML = "";
      return;
    }

    var W = 900;
    var H = 320;
    var M = { top: 16, right: 16, bottom: 30, left: 48 };
    var innerW = W - M.left - M.right;
    var innerH = H - M.top - M.bottom;

    var allPoints = prepared.reduce(function (acc, s) {
      return acc.concat(s.points);
    }, []);
    var tMin = Math.min.apply(
      null,
      allPoints.map(function (p) {
        return p.t;
      })
    );
    var tMax = Math.max.apply(
      null,
      allPoints.map(function (p) {
        return p.t;
      })
    );
    var vMin = Math.min.apply(
      null,
      allPoints.map(function (p) {
        return p.v;
      })
    );
    var vMax = Math.max.apply(
      null,
      allPoints.map(function (p) {
        return p.v;
      })
    );

    // Never draw a flat line at the very edge of the frame.
    var pad = (vMax - vMin) * 0.12 || 1;
    vMin -= pad;
    vMax += pad;

    function x(t) {
      return tMax === tMin ? M.left : M.left + ((t - tMin) / (tMax - tMin)) * innerW;
    }
    function y(v) {
      return M.top + innerH - ((v - vMin) / (vMax - vMin)) * innerH;
    }

    var ticks = 4;
    var svg = ['<svg viewBox="0 0 ' + W + " " + H + '" preserveAspectRatio="xMidYMid meet">'];

    svg.push('<g class="axis">');
    for (var i = 0; i <= ticks; i++) {
      var value = vMin + ((vMax - vMin) * i) / ticks;
      var yy = y(value);
      svg.push(
        '<line class="gridline" x1="' +
          M.left +
          '" y1="' +
          yy.toFixed(1) +
          '" x2="' +
          (W - M.right) +
          '" y2="' +
          yy.toFixed(1) +
          '" />'
      );
      svg.push(
        '<text x="' +
          (M.left - 8) +
          '" y="' +
          (yy + 4).toFixed(1) +
          '" text-anchor="end">' +
          value.toFixed(meta.decimals) +
          "</text>"
      );
    }
    for (var j = 0; j <= 4; j++) {
      var tt = tMin + ((tMax - tMin) * j) / 4;
      svg.push(
        '<text x="' +
          x(tt).toFixed(1) +
          '" y="' +
          (H - 8) +
          '" text-anchor="middle">' +
          clockTime(new Date(tt).toISOString()) +
          "</text>"
      );
    }
    svg.push("</g>");

    prepared.forEach(function (s) {
      var d = s.points
        .map(function (p, index) {
          return (index ? "L" : "M") + x(p.t).toFixed(1) + " " + y(p.v).toFixed(1);
        })
        .join(" ");
      svg.push(
        '<path class="series" d="' + d + '" stroke="var(' + s.color + ')" />'
      );
    });

    svg.push("</svg>");
    container.innerHTML = svg.join("");
    container.setAttribute(
      "aria-label",
      meta.label +
        " over the last 24 hours for " +
        prepared
          .map(function (s) {
            return s.name;
          })
          .join(", ")
    );

    legend.innerHTML = prepared
      .map(function (s) {
        var last = s.points[s.points.length - 1];
        return (
          '<span class="legend__item"><span class="legend__swatch" style="background:var(' +
          s.color +
          ')"></span>' +
          escapeHtml(s.name) +
          " · " +
          num(last.v, meta.decimals, meta.unit) +
          "</span>"
        );
      })
      .join("");
  }

  /* ---------------- breaches ---------------- */

  function renderBreaches(payload) {
    var container = el("breaches");
    var breaches = (payload && payload.breaches) || [];

    if (!breaches.length) {
      container.innerHTML =
        '<p class="empty">No thresholds crossed in the last 24 hours — everything nominal.</p>';
      return;
    }

    container.innerHTML = "";
    breaches.slice(0, 25).forEach(function (b) {
      var row = document.createElement("div");
      row.className = "breach";
      row.setAttribute("data-severity", b.severity || "warning");
      row.innerHTML =
        '<span class="pill pill--' +
        (b.severity === "critical" ? "crit" : "warn") +
        '">' +
        escapeHtml((b.severity || "warning").toUpperCase()) +
        '</span><span class="breach__msg">' +
        escapeHtml(text(b.message)) +
        '</span><span class="breach__time">' +
        escapeHtml(timeAgo(b.detected_at_utc)) +
        "</span>";
      container.appendChild(row);
    });
  }

  /* ---------------- orchestration ---------------- */

  function refresh() {
    return Promise.all([
      fetchJson(ENDPOINTS.latest).catch(function (e) {
        return { __error: e };
      }),
      fetchJson(ENDPOINTS.timeseries).catch(function (e) {
        return { __error: e };
      }),
      fetchJson(ENDPOINTS.breaches).catch(function (e) {
        return { __error: e };
      }),
    ]).then(function (results) {
      var latest = results[0];
      var timeseries = results[1];
      var breaches = results[2];

      if (latest.__error) {
        // A 503 means the pipeline is up but has not curated yet; that is a
        // different message from "the API is unreachable".
        var pending = latest.__error.status === 503;
        setStatus(pending ? "stale" : "error", pending ? "Awaiting first curation" : "Unreachable");
        el("locations").innerHTML =
          '<p class="empty">' + escapeHtml(latest.__error.message) + "</p>";
      } else {
        // Held so the assistant can answer forecast questions from the same
        // snapshot the cards render from (and locally, without a backend).
        state.latest = latest;
        renderLocations(latest);
        var age = parseTime(latest.generated_at_utc);
        var stale = age && Date.now() - age.getTime() > 3 * 3600 * 1000;
        setStatus(stale ? "stale" : "live", stale ? "Data is stale" : "Live");
        el("generated-at").textContent = "Curated " + timeAgo(latest.generated_at_utc);
      }

      state.timeseries = timeseries.__error ? null : timeseries;
      renderChart();

      if (breaches.__error) {
        el("breaches").innerHTML = '<p class="empty">Breach feed unavailable.</p>';
      } else {
        renderBreaches(breaches);
      }

      el("footer-meta").textContent =
        "Last refreshed " + new Date().toLocaleTimeString() + (API_BASE ? "" : " · sample data");

      // Deliberately last: the weather is on screen before advice is even
      // requested, so a slow or broken advice service cannot delay it.
      if (!latest.__error) {
        var first = (latest.locations || [])[0];
        if (first) refreshAdvice(first.name || first.location_key);
      }
    });
  }

  document.querySelectorAll(".toggle").forEach(function (button) {
    button.addEventListener("click", function () {
      state.metric = button.getAttribute("data-metric");
      document.querySelectorAll(".toggle").forEach(function (other) {
        other.setAttribute("aria-pressed", String(other === button));
      });
      renderChart();
    });
  });

  window.addEventListener("resize", function () {
    // The SVG scales itself; only the legend needs re-flowing, which the
    // browser handles. Kept as a hook for future responsive tick logic.
  });

  // Show the welcome immediately, so the assistant panel is never empty while
  // the first weather fetch is in flight.
  renderWelcome();

  refresh();
  setInterval(refresh, REFRESH_MS);
})();
