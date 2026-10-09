// Miners dashboard client. Split out of templates/miners.html so the browser
// can cache it: the document was 373KB and re-downloaded in full on every
// navigation. Served by app.py's /js/miners.js route (NOT the /asset mount)
// so it carries an mtime-keyed ETag and revalidates instead of going stale
// after a deploy. See docs/sn79/performance/page_load_audit.md.
// Session expired mid-use -> the auth middleware answers /api/* with 401.
// Bounce to the login page instead of leaving fetch() calls silently failing.
(function () {
  var originalFetch = window.fetch;
  window.fetch = function () {
    return originalFetch.apply(this, arguments).then(function (response) {
      if (response.status === 401) {
        window.location.href =
          "/login?next=" + encodeURIComponent(window.location.pathname);
      }
      return response;
    });
  };
})();

// ---- Conditional fetching ------------------------------------------------
//
// Every JSON endpoint the dashboard polls carries a weak ETag, but fetch() was
// never sending If-None-Match, so an unchanged 13KB payload was re-sent on every
// poll. (Verified: the parser-loaded /js/miners.js revalidated and got a 304;
// the fetch()-loaded /api/* calls did not.) So do the validation explicitly
// rather than depending on browser cache heuristics.
//
// A 304 is the cheapest possible diff: ~150 bytes of headers and no body.
//
// The response *text* is cached rather than the parsed object, and re-parsed on
// each 304. Handing back the same object every time would let one caller's
// mutation corrupt what every later caller sees; re-parsing 60KB costs well
// under a millisecond and keeps the semantics identical to a real fetch.
// Survives a reload. Without this, every navigation starts with an empty
// validator set and re-downloads every payload — measured at 59KB for a plain
// F5 versus ~4KB when the tags carry over. sessionStorage rather than
// localStorage so it is per-tab and disappears with it; a stale tag is harmless
// either way, since the server answers 200 with the new body when it differs.
var ETAG_STORE_KEY = "etagCache.v1";
// Bounds are on the *decoded* text, which is several times the gzipped wire
// size: the metagraph identity half is ~103KB of JSON that travels as ~20KB.
// Typical sessionStorage quota is 5-10MB per origin, so ~1MB total is safe.
var ETAG_STORE_MAX_ENTRY = 512 * 1024;
var ETAG_STORE_MAX_TOTAL = 1024 * 1024;
var etagCache = (function () {
  try {
    return JSON.parse(sessionStorage.getItem(ETAG_STORE_KEY)) || {};
  } catch (e) {
    return {};
  }
})();
var etagStoreTimer = null;

function persistEtagCache() {
  if (etagStoreTimer) return; // coalesce a burst of loads into one write
  etagStoreTimer = setTimeout(function () {
    etagStoreTimer = null;
    try {
      var out = {};
      var total = 0;
      for (var k in etagCache) {
        if (!Object.prototype.hasOwnProperty.call(etagCache, k)) continue;
        var n = etagCache[k].text.length;
        if (n > ETAG_STORE_MAX_ENTRY || total + n > ETAG_STORE_MAX_TOTAL) continue;
        total += n;
        out[k] = etagCache[k];
      }
      sessionStorage.setItem(ETAG_STORE_KEY, JSON.stringify(out));
    } catch (e) {
      // Quota exceeded or storage disabled: the in-memory cache still works.
      try {
        sessionStorage.removeItem(ETAG_STORE_KEY);
      } catch (e2) {}
    }
  }, 1000);
}

function conditionalFetch(url) {
  var prior = etagCache[url];
  // cache: "no-store" so the browser does not layer its own revalidation on top
  // of ours — we own the validator, and two of them disagreeing is a bug source.
  var init = { cache: "no-store", headers: {} };
  if (prior) init.headers["If-None-Match"] = prior.etag;
  return fetch(url, init).then(function (r) {
    if (r.status === 304 && prior) return JSON.parse(prior.text);
    if (!r.ok) throw new Error("HTTP " + r.status);
    var etag = r.headers.get("ETag");
    return r.text().then(function (text) {
      if (etag) {
        etagCache[url] = { etag: etag, text: text };
        persistEtagCache();
      } else delete etagCache[url];
      return JSON.parse(text);
    });
  });
}

function cachedEtagFor(url) {
  return etagCache[url] ? etagCache[url].etag : null;
}

function dropEtag(url) {
  delete etagCache[url];
}

// Data from backend: Bittensor Metagraph (netuid 88, finney)
// Structure: incentives[hotkey]=decimal, hotkeyToUid[hotkey]=uid, hotkeyToColdkey[hotkey]=coldkey
let minersArray = [];
let filteredMiners = [];
let subnetTotalEmission = 0;
let subnetEmissions = {};
let subnetAlphaPrices = {};
let currentBlock = 0;
let chart = null;
const DEFAULT_COLDKEYS = [
  "5F7YRN7pXvyqBY1wxJ3TBC2TfprxGXzWHfhDuspt6tyfsjJL",
  "5Eci5qYC6LNzte2zvqEnbytAZbDaCg1eEk21UGdKeBnyh5nj",
  "5GQe7rXYeFgvUcd4rADKdFsLU9V3WMyMk1R2q5TzAJd9Zz1m",
  "5FRtchUXj3U5QLNdZyuRcJChghV9Lye2zYMnY2AybWkaDVqY",
  "5FRi4yz2XEECLbRT4Lttr7aP4o7929woWpStUt9G9CawDVF1",
  "5CfQ6895NGPG6SYxixLrnaY2a4P8rnknR3dGmAJhZFeTc8Zo",
  "5EWgdZT1HZVqrE3rfdqyx9g73N6KipkBTDTfVCekQdVrVSnJ",
  "5CkTVgUpsyuWK5QJCBx3BU5a9RZivZtD2MyNqBdXgyNMVg47",
  "5GREyxixYB8EJVHV1A9fQDh734tghXtt41A2MQG7VebqfQNe",
  "5Fnwngtt7bm5Nm8zTrXeELgG88M1cDBkJzaewdDghkLfgnXW",
  "5DUV5M1JFF3bpBpsZQ1ytNzsBJhczKXuAX8cAAQCVDWFJota",
  "5HpPMn4pKbKmhW8Xo9U9LHwTfjQpvCLWxk3GpezJKwuoSGs5",
  "5GBNUndCzsce7kH33GoF6oYt43RLpod8EPrpSRdwQF9MyLUX",
  "5G3uod81PRbexLN4GJQ2QpEuzMiN9jTffRZgmrKwyTcZeJo1",
  "5Eqm7JVh7C4NfHc6s5DS5WrFSfgsLDXuvopih6YntR574F6D",
  "5En5ViQDeCo9hnZ6vG2EhCSLgWkJzuDS2a5h7Bbz1rMongNJ",
  "5FehJALv2VEEFumf315e15A9mmw4QBUEviQ5Vd7r3BVxSX95",
  "5Hovmb4Ga8z3tP4UAnjF6fwmKvqYprL82soxUXLK5WQ2HqT6",
  "5FU5hJEBaCmpdyi3DJBjQUNBTVK9hz9hcAYKjprVMNfBc4YT",
  "5EnxBJEc4xf5hQKt7DMYsZgH7kAq6Kw2xok1xRkduDZpzn4E",
  "5FsLjX4Aa8W8p59T6ugrWCWBgk95aSEjHkALxnx7SZjuxpuf",
  "5CrAkrjfZ6CfjPFPsRxuyPPjXz6vCX5pGEnBMZQ8KU5vyPpB",
  "5FUAWyfAtJLfnChB2yHLrne5QnF4cHr66k5GaMNyUqhwcEgK",
  "5H6shai13saCzg5p7Sx1XDsBQZGzTY8oYmC7u6kAaQBeyPnD",
  "5HC3xg4W3bsgsu6Bqgjf4ii8mWcvJuU37JWkaiTtxvzKex8u",
  "5HLBnZZEbWZ3XRzTjWt4F4ggtaU3EdcmMLZvpsqsqeMjcxdD",
  "5F6nKMEs2yBNMehUYqNWyM2UCCsn3xqJhU6nBoem2KHSU1e5",
  "5CAeBzEQYeCGCr8PNtveEfQ1qzDca4XnxT6ta4RihwhHNzx2",
  "5EkU3zAmve5rgTteAPVTXLPzs9Q95tAu1f3PspazvaR2VRtw",
  "5FToeoW4tZ3LjgH9joXGvtkvbdXPV9RzjDy7MwzZABLmZKqQ",
  "5EqmAnSFH86bRu6q6zajfrHzYPYSqFkTw9pEXx18MeF1AV9x",
  "5EUc1vYbaRGrxtTPBVLroC1YSTbM7Py8o9pYfzEcwSmurokW",
  "5EC73L1MzPeU5M2Mn7fUCZKP9xrWMn3sjy9z9DAdWoCshWUP",
  "5G9Ar2RodCujAwmfGw5RL7tWr1bDWdiLxHBFGetHi3wLhmKB",
  "5C7QAeTfivvG3WLvzTSPYY3gjSCn8wDWeaVJUz6KBtmLxXhT",
  "5FhZznriKks7KqHLcoL4Ut3jdM2zJp6RSVsRPMmPYHZuGAAx",
  "5CXzmtm8mwVv2wXmaMSqwA7BkgSAJX2ZyewrgeywkX5VajN9",
  "5Dr9T92NAdoi4GL5n138hkchAxXx7Q2pqbF1gVKNLgECp7hH",
  "5D8EogQFrjcx81WwAQNg3xpEryTuzZBHYpU1B7999KH5csW9",
  "5Chr4b1cEwPTKeWKkdMpm9wnNzmvVCbbqvXFTGuiUxoffBwi",
  "5Ci19i9cwpSdgcAg8VQuuRN29TH6cxqYqJuDRwEyMbaCrcr5",
  "5EHKVSarB9U2G17aEMAz7uXx4pCJMQuWNYhVLcdNbBneuq5z",
  "5F9BHbXuaThK26eNaVWt6LDXpQxBH6Gzz9NGfpt64a3QQkSZ",
  "5FRffJzovvSiA9B2vFc6arVTEJd5dgZX14Ug6826mA3c2Eir",
  "5FRHjn6FTKovikhTDk7vE73Z2qM7bp6VeW6ALBQ2gs2bNerG",
  "5DFGHSTxSJpaXhDoDjx77gswYFXZtVCVR6gA7wqM5BdQPZED",
  "5Dc9w3rC6WBaWYGJhJhby3QrncXRy7cgBwDPGm2qbRMLXeX4",
  "5FTbBm7iJNWDovG1fyHFfU9LwhgV56aSbf6Cxb529Cbwkoat",
  "5E6xNwBPFSwYkySuieS3Y7vF8SGBW7GfoF3XfGZgQzkvrVoA",
  "5DD45SAecKmhpyn9TykNoZWnc7mFgBrhvwmYNC6XSN2Ron6R",
  "5DUxybWJH5tofLXFP1TjE7rQ1av97z8sTCShZYXdQEZNPwcg",
  "5Fnwngtt7bm5Nm8zTrXeELgG88M1cDBkJzaewdDghkLfgnXW",
  "5EvxmWMeWahszPbfUhNKvkc5yjV6L2SiXUtyLs96zqWmy3QC",
  "5HYhmNU4S7T7uV9nRrMAtp9R1uAeVi5gSzujb8yCvuZPkYKP",
  "5GjHjhtEXgkmGaSgGogYs2SbMJiUvAmdGtYSacSJzRahSfRX",
  "5GgmeU2DDbvCahdg2S5X91UVU4zdFLUxEBEbw11g72i45SxY",
  "5CCb9PjiicAQjC4NKmPjU7yCRqcBSNpz1FMiytYiyhyuDHgu",
  "5Hb9gA7B21k1RJHwQ49CwQdAzMvm3hra69vQZDxBCnLiCfQY",
  "5EUAiFpZiYcJFZVhXbp1c4GzukxeHaoYWmZb946Eemz6ST9q",
  "5DvSXzA1boJ3CagbBWsdZzn1auGQamNyTX3T3X9R7GXrcjjX",
  "5DcXKMJjYhjdNqfkyARk65q1uLg4ALHixdoXdTV3DbTiBLon",
  "5DNo893avTVVEGKNJ9REMbLUWaJdXhrnaFk8njXKPSqU5GM9",
  "5EcLapzXkpmNXPDistJuWsF9MpevmHeot8oSVZnpBFUW3c5r",
  "5DPDALvARJN8Bsba3Yy7mx3LzKx3kVeLDs6jfCZ9iWVABonW",
  "5FHovibREhLYEFPSyZW9sAZdiQ4RBpRXVoFrunqtnb8fw27v",
  "5DAwDVmueCsMUVa5ko9H1Mpgoa36jQW44jUqCX7yEi4tGqaa",
  "5HVnS4HmuPaepAnNzA2aXUf96SPDXceV3PugqG7Stsh8eErG",
  "5CnwHCF9JChi5JC2bJzC3KtsDRWBEyrdcGZ3KUVbTxjvEdpW",
  "5HeRKBo2NLScWGn5sd98ThmpJSAqTHjo21ae2S8simH5w1rJ",
  "5DX4KnN1Js3ufjWKcMMqzjGwHjKgQn9Js7jmC2xxaeFupX5o",
];
let registeredColdkeys = [];
let currentPage = 1;
let rowsPerPage = 100;
let sortColumn = "rank";
let sortDirection = "asc";
let coldkeyColors = {}; // Cache colors for each coldkey
let agentColors = {}; // Cache colors for each agent name
let highlightedColdkeys = DEFAULT_COLDKEYS.slice(); // Track which coldkeys are highlighted
let currentParamsUid = null;
let currentParamsData = null;
let currentStatusUid = null;
let currentHistoryUid = null;
let historyCharts = [];
let myMinerUids = [];
let feeRegimeData = null;
let feeRegimeCharts = [];
let investing88MinerContext = null;
let investing88Charts = [];
let investing88DaysMap = {};
let investing88ScoresMap = {};
let investing88PrevScoresMap = {}; // previous-day scores for deltas
let minerAgentInfo = {};
let uidMetrics = {};
let uidMetricDeltas = {};
var bookVolumeLimit = 500000;
var bookVolumeWarnRatio = 0.98;
var DISPLAY_PARAM_KEYS = ["max_quantity", "min_quantity", "taker_ratio"];
var SERVER_NICKNAMES = {
  "65.109.34.53": "Server_1",
  "65.109.87.169": "Server3",
  "178.16.143.193": "Server4",
  "144.91.111.126": "Server5",
  "65.109.65.81": "Server6",
  "84.32.34.89": "server_14",
    "65.109.69.29": "Server_2",
    "88.216.183.132": "Server_7",  // added 2026-10-06
    "84.32.131.131": "Server_9",  // added 2026-10-09
};
var COLDKEY_NICKNAMES = {
  "5HdegbzgYQ26ZfsqBLx7o54uwFQzLRPwkBe2S6JM2aNVpCj5": "b005",
  "5EAHcQJ61VRi28m2wNpQAUpXXuPr4EaqFpDPbqBqAoEBd77q": "b006",
  "5FHBj9FW8pysyZEUS8wj3fNUz1AsL6jUf8e9ZaR7ZZeSWrNz": "b007",
  "5H9EEaLUHwpYJ49MYbZjPVvrVv6439N9HXhvSBSfrZMSYzZ2": "w004",
  "5CQ2zbACHJGtmu5JqDodY4PkrqfUP5oXgXgX6Ks79gGjvkJy": "w003",
  "5DLysBUFyR8v75vef54DATeVBy9sMEx5JdeLvikXvbGYvx6N": "w002",
  "5HjZiymx6XonNczu9vgtkbcuBbPeTKZ1Pso6jQrw7D1DcfC2": "w001",
  "5GREyxixYB8EJVHV1A9fQDh734tghXtt41A2MQG7VebqfQNe": "b001",
  "5DD45SAecKmhpyn9TykNoZWnc7mFgBrhvwmYNC6XSN2Ron6R": "b002",
  "5Fnwngtt7bm5Nm8zTrXeELgG88M1cDBkJzaewdDghkLfgnXW": "b003",
  "5G96Vc7YqepyNNZi6dWqzpzQJtQxcZFdysC7Cp15ZgrTfhXf": "w005",
  "5CnwHCF9JChi5JC2bJzC3KtsDRWBEyrdcGZ3KUVbTxjvEdpW": "k001",
  "5HeRKBo2NLScWGn5sd98ThmpJSAqTHjo21ae2S8simH5w1rJ": "k002",
};
function getColdkeyNickname(ck) {
  return ck && COLDKEY_NICKNAMES[ck] ? COLDKEY_NICKNAMES[ck] : null;
}
function shortColdkey(ck) {
  if (!ck) return "N/A";
  return ck.substring(0, 8) + "..." + ck.substring(ck.length - 6);
}
// Nickname if we have one for this coldkey, else the truncated address.
function formatColdkeyLabel(ck) {
  if (!ck) return "N/A";
  return getColdkeyNickname(ck) || shortColdkey(ck);
}
// Longer form for tooltips: "nickname (partial address)" or a longer
// truncated address when there's no nickname.
function formatColdkeyTooltip(ck) {
  if (!ck) return "N/A";
  var nick = getColdkeyNickname(ck);
  return nick
    ? nick + " (" + ck.substring(0, 10) + "...)"
    : ck.substring(0, 16) + "...";
}
var AGENT_PARAM_SELECT_OPTIONS = {
  RegimeFeeHybridAgent: {
    mode_override: ["auto", "BOTH", "MAKER_ONLY", "TAKER_ONLY"],
  },
};

var THEME_KEY = "sn88-dashboard-theme";
var THEME_USER_SET_KEY = "sn88-dashboard-theme-user-set";
function getStoredTheme() {
  try {
    var t = localStorage.getItem(THEME_KEY);
    var explicit = localStorage.getItem(THEME_USER_SET_KEY) === "1";
    if (t === "light" && explicit) return "light";
    return "dark";
  } catch (e) {
    return "dark";
  }
}
function setTheme(theme) {
  var isDark = theme === "dark";
  document.documentElement.classList.toggle("theme-dark", isDark);
  var lightBtn = document.getElementById("themeLight"),
    darkBtn = document.getElementById("themeDark");
  if (lightBtn) {
    lightBtn.classList.toggle("active", !isDark);
    lightBtn.setAttribute("aria-pressed", !isDark);
  }
  if (darkBtn) {
    darkBtn.classList.toggle("active", isDark);
    darkBtn.setAttribute("aria-pressed", isDark);
  }
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch (e) {}
  agentColors = {};
  if (minersArray.length > 0) renderTable();
  // Chart.js bakes colors in at construction time, so the sparkline has to
  // be rebuilt for the new surface rather than just restyled by CSS.
  if (alphaPriceData) renderAlphaPriceChart(alphaPriceData);
}
document.addEventListener("DOMContentLoaded", function () {
  setTheme(getStoredTheme());
  document.getElementById("themeLight") &&
    document
      .getElementById("themeLight")
      .addEventListener("click", function () {
        try {
          localStorage.setItem(THEME_USER_SET_KEY, "1");
        } catch (e) {}
        setTheme("light");
      });
  document.getElementById("themeDark") &&
    document
      .getElementById("themeDark")
      .addEventListener("click", function () {
        try {
          localStorage.setItem(THEME_USER_SET_KEY, "1");
        } catch (e) {}
        setTheme("dark");
      });
  var alphaRangeWrap = document.getElementById("alphaPriceRanges");
  if (alphaRangeWrap)
    alphaRangeWrap.addEventListener("click", function (e) {
      var btn = e.target.closest("button[data-range]");
      if (!btn) return;
      Array.prototype.forEach.call(
        alphaRangeWrap.querySelectorAll("button[data-range]"),
        function (b) {
          b.classList.toggle("active", b === btn);
        }
      );
      loadAlphaPrice(btn.getAttribute("data-range"));
    });
  loadColdkeys();
  loadData();
  loadRegBotActiveRuns();
  loadBatchActiveRuns();
  loadUnstakeActiveRuns();
  loadColdkeysSummary(false);
  startBackgroundPolling();
  var currentNetuid = getSubnetFromInput();
  [71, 79, 88].forEach(function (n) {
    if (n !== currentNetuid) fetchSubnetEmissionBackground(n);
  });
});

// ---- Background polling and change notifications ---------------------------
//
// Every one of these costs something on the server — the RegBot active scan in
// particular used to be a 690ms SSH fan-out across 12 boxes, and it ran at this
// cadence per open tab whether or not anyone was looking. Three rules:
//
//   1. Nothing polls while the tab is hidden.
//   2. When the /api/events stream is up, the server says when something
//      changed, so the timers drop to a slow safety cadence. The stream is an
//      optimisation, never the only path — if it dies, the timers carry on and
//      the page keeps working exactly as it did before it existed.
//   3. This scheduler is the only place background work is scheduled. Adding a
//      bare setInterval elsewhere re-introduces the problem.
var backgroundPolls = [];
var pollScheduler = null;
var eventStream = null;
var eventStreamUp = false;

function pollWhenVisible(fn, fastMs, slowMs) {
  backgroundPolls.push({
    fn: fn,
    fast: fastMs,
    // Endpoints the server can't notify about keep their original cadence.
    slow: slowMs || fastMs,
    last: Date.now(),
  });
}

function runDuePolls() {
  if (document.visibilityState !== "visible") return;
  var now = Date.now();
  for (var i = 0; i < backgroundPolls.length; i++) {
    var p = backgroundPolls[i];
    var due = eventStreamUp ? p.slow : p.fast;
    if (now - p.last >= due) {
      p.last = now;
      try {
        p.fn();
      } catch (e) {
        console.warn("background poll failed:", e);
      }
    }
  }
}

function startBackgroundPolling() {
  //                                     fast (no stream)   slow (stream up)
  pollWhenVisible(loadData, 5 * 60 * 1000, 5 * 60 * 1000);
  pollWhenVisible(loadRegBotActiveRuns, 20000, 120000);
  pollWhenVisible(loadBatchActiveRuns, 15000, 120000);
  pollWhenVisible(loadUnstakeActiveRuns, 20000, 120000);
  pollWhenVisible(function () {
    loadColdkeysSummary(false);
  }, 5 * 60 * 1000);

  pollScheduler = setInterval(runDuePolls, 5000);

  // Coming back to a tab that has been hidden for a while should show current
  // data immediately rather than whatever was on screen when it was backgrounded.
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible") return;
    loadRegBotActiveRuns();
    loadBatchActiveRuns();
    loadUnstakeActiveRuns();
    if (Date.now() - lastDataLoadAt > 60000) loadData();
  });

  startEventStream();
}

// Topic -> what to re-fetch, and the URL whose ETag tells us we already have it.
var EVENT_TOPICS = {
  regbot_active: { url: "/api/regbot/active", load: loadRegBotActiveRuns },
  batch_active: { url: "/api/regbot/batch/active", load: loadBatchActiveRuns },
  unstake_active: { url: "/api/unstake/active", load: loadUnstakeActiveRuns },
  uid_metrics: { url: "/api/uid-metrics", load: loadUidMetrics },
};

var topicInFlight = {};

function onServerChange(topic, etag) {
  // Per-netuid topic, so it can't live in the flat EVENT_TOPICS map above.
  if (topic.indexOf("metagraph:") === 0) {
    var n = parseInt(topic.slice("metagraph:".length), 10);
    if (!isNaN(n)) onMetagraphChanged(n);
    return;
  }
  var entry = EVENT_TOPICS[topic];
  if (!entry) return;
  // Two endpoints publish from inside their own GET handler, so a notification
  // can arrive before the fetch that caused it has stored its ETag. Without
  // this guard that race turns one change into two or three fetches.
  if (topicInFlight[topic]) return;
  // Two endpoints publish from inside their own GET handler, so the tab that
  // just fetched is told about its own result. Skipping a notification whose
  // ETag we already hold stops that becoming a fetch loop, and stops other tabs
  // re-fetching something they happen to be current on.
  if (etag && cachedEtagFor(entry.url) === etag) return;
  if (document.visibilityState !== "visible") return;
  topicInFlight[topic] = true;
  Promise.resolve(entry.load()).catch(function () {}).then(function () {
    topicInFlight[topic] = false;
  });
}

function startEventStream() {
  if (!window.EventSource) return;
  try {
    eventStream = new EventSource("/api/events");
  } catch (e) {
    return;
  }
  eventStream.addEventListener("open", function () {
    eventStreamUp = true;
  });
  eventStream.addEventListener("change", function (e) {
    var msg;
    try {
      msg = JSON.parse(e.data);
    } catch (err) {
      return;
    }
    onServerChange(msg.topic, msg.etag);
  });
  eventStream.onerror = function () {
    // EventSource reconnects on its own; until it does, the fast timers are the
    // floor. Nothing here needs to retry, and nothing may throw.
    eventStreamUp = false;
  };
}

function loadColdkeys() {
  var netuid = getSubnetFromInput();
  var stored = localStorage.getItem("registeredColdkeys");
  if (stored !== null) {
    try {
      registeredColdkeys = JSON.parse(stored);
    } catch (e) {
      registeredColdkeys = [];
    }
  } else {
    registeredColdkeys = [];
  }
  // Merge my default coldkeys on the subnets the dashboard tracks (79 & 88).
  // A coldkey with no neuron on the viewed subnet just matches nothing.
  if (netuid === 88 || netuid === 79) {
    DEFAULT_COLDKEYS.forEach(function (ck) {
      if (registeredColdkeys.indexOf(ck) < 0) registeredColdkeys.push(ck);
    });
  }
  // The New Coldkeys are NOT default "my miners" — drop them from the
  // registered set even if a previous session cached them (they used to
  // be merged in via DEFAULT_COLDKEYS before that was fixed). They
  // remain viewable via the "New Coldkeys Only" toggle.
  registeredColdkeys = registeredColdkeys.filter(function (ck) {
    return NEW_COLDKEYS.indexOf(ck) < 0;
  });
  saveColdkeys();
  displayColdkeys();
}
function saveColdkeys() {
  localStorage.setItem(
    "registeredColdkeys",
    JSON.stringify(registeredColdkeys),
  );
}
function displayColdkeys() {
  var el = document.getElementById("coldkeyBadges");
  if (registeredColdkeys.length === 0) {
    el.innerHTML =
      '<span style="color:#6c757d;font-size:0.9rem">No coldkeys registered</span>';
    return;
  }
  el.innerHTML = registeredColdkeys
    .map(function (ck) {
      return (
        '<div class="coldkey-badge" title="' +
        ck +
        '">' +
        formatColdkeyLabel(ck) +
        ' <span class="remove-btn" onclick="removeColdkey(\'' +
        ck.replace(/'/g, "\\'") +
        "')\">×</span></div>"
      );
    })
    .join("");
}
function addColdkey() {
  var input = document.getElementById("coldkeyInput");
  var ck = input.value.trim();
  if (ck.length < 40) {
    alert("Coldkey must be at least 40 characters");
    return;
  }
  if (registeredColdkeys.indexOf(ck) >= 0) {
    alert("Already registered");
    return;
  }
  registeredColdkeys.push(ck);
  saveColdkeys();
  displayColdkeys();
  input.value = "";
  updateStatistics();
  filterTable();
  renderChart();
}
function removeColdkey(ck) {
  registeredColdkeys = registeredColdkeys.filter(function (c) {
    return c !== ck;
  });
  saveColdkeys();
  displayColdkeys();
  updateStatistics();
  filterTable();
  renderChart();
}
function isMinerOwned(m) {
  return m.coldkey && registeredColdkeys.indexOf(m.coldkey) >= 0;
}

// The 3 newest ("testing") coldkeys — the View listbox's "Testing
// Coldkeys" option focuses the leaderboard + stat cards on just their
// miners. Subnet 79 only: updateTableColumns() shows/hides that option
// based on netuid.
var NEW_COLDKEYS = [
  "5HdegbzgYQ26ZfsqBLx7o54uwFQzLRPwkBe2S6JM2aNVpCj5",
  "5EAHcQJ61VRi28m2wNpQAUpXXuPr4EaqFpDPbqBqAoEBd77q",
  "5H9EEaLUHwpYJ49MYbZjPVvrVv6439N9HXhvSBSfrZMSYzZ2",
  "5FHBj9FW8pysyZEUS8wj3fNUz1AsL6jUf8e9ZaR7ZZeSWrNz",
  "5CQ2zbACHJGtmu5JqDodY4PkrqfUP5oXgXgX6Ks79gGjvkJy",
  "5DLysBUFyR8v75vef54DATeVBy9sMEx5JdeLvikXvbGYvx6N",
  "5G96Vc7YqepyNNZi6dWqzpzQJtQxcZFdysC7Cp15ZgrTfhXf",
  "5HjZiymx6XonNczu9vgtkbcuBbPeTKZ1Pso6jQrw7D1DcfC2",
];
function isNewColdkeyMiner(m) {
  return m.coldkey && NEW_COLDKEYS.indexOf(m.coldkey) >= 0;
}
function newColdkeysFeatureActive() {
  return getSubnetFromInput() === 79;
}
// "mine" (default) | "all" | "testing" — "testing" only valid on SN79.
function getMinerViewMode() {
  var el = document.getElementById("minerViewMode");
  var v = el ? el.value : "mine";
  if (v === "testing" && !newColdkeysFeatureActive()) return "mine";
  return v;
}
function newColdkeysOnly() {
  return getMinerViewMode() === "testing";
}
// "testing" -> ONLY the testing-coldkey miners.
// "mine" -> everything EXCEPT the testing-coldkey miners.
// "all" (or non-SN79 subnets) -> no-op, shows everyone.
function applyNewColdkeyFilter(miners) {
  if (!newColdkeysFeatureActive()) return miners;
  var mode = getMinerViewMode();
  if (mode === "testing") {
    return miners.filter(isNewColdkeyMiner);
  }
  if (mode === "mine") {
    return miners.filter(function (m) {
      return !isNewColdkeyMiner(m);
    });
  }
  return miners;
}
// "My Miners" / "My TAO/Day" / "My TAO/Epoch" stat cards: same view mode
// as the leaderboard — "testing" -> the testing-coldkey miners' stats,
// otherwise ("mine"/"all") -> the default-registered coldkeys (which
// already exclude the testing coldkeys).
function myStatsMiners() {
  if (newColdkeysOnly()) {
    return minersArray.filter(isNewColdkeyMiner);
  }
  return minersArray.filter(isMinerOwned);
}
function applyColdkeyViewFilters() {
  updateStatistics();
  filterTable();
  renderChart();
}

// Vibrant color palette for registered coldkeys
var registeredColorPalette = [
  "hsl(350, 85%, 55%)", // Vibrant Red
  "hsl(120, 75%, 45%)", // Vibrant Green
  "hsl(210, 90%, 55%)", // Vibrant Blue
  "hsl(280, 80%, 60%)", // Vibrant Purple
  "hsl(30, 90%, 55%)", // Vibrant Orange
  "hsl(180, 75%, 45%)", // Vibrant Cyan
  "hsl(50, 90%, 50%)", // Vibrant Yellow
  "hsl(320, 85%, 55%)", // Vibrant Magenta
  "hsl(90, 70%, 45%)", // Vibrant Lime
  "hsl(240, 85%, 60%)", // Vibrant Indigo
];

// Distinct color palette for non-registered coldkeys
var coldkeyColorPalette = [
  "hsl(0, 70%, 60%)", // Red
  "hsl(30, 75%, 55%)", // Orange
  "hsl(60, 70%, 50%)", // Yellow
  "hsl(90, 65%, 45%)", // Yellow-Green
  "hsl(120, 70%, 45%)", // Green
  "hsl(150, 65%, 45%)", // Green-Cyan
  "hsl(180, 70%, 50%)", // Cyan
  "hsl(210, 75%, 55%)", // Blue
  "hsl(240, 70%, 60%)", // Blue-Purple
  "hsl(270, 75%, 60%)", // Purple
  "hsl(300, 70%, 60%)", // Magenta
  "hsl(330, 75%, 55%)", // Pink
  "hsl(15, 65%, 55%)", // Red-Orange
  "hsl(45, 75%, 50%)", // Gold
  "hsl(75, 60%, 45%)", // Lime
  "hsl(105, 65%, 45%)", // Spring Green
  "hsl(135, 60%, 45%)", // Sea Green
  "hsl(165, 65%, 50%)", // Turquoise
  "hsl(195, 70%, 55%)", // Sky Blue
  "hsl(225, 70%, 58%)", // Royal Blue
  "hsl(255, 65%, 60%)", // Violet
  "hsl(285, 70%, 58%)", // Orchid
  "hsl(315, 70%, 58%)", // Hot Pink
  "hsl(345, 75%, 55%)", // Crimson
];

// Blue serial palette for maker agents (light theme)
var agentColorPaletteMakerLight = [
  "hsl(210, 72%, 38%)",
  "hsl(215, 75%, 42%)",
  "hsl(220, 78%, 46%)",
  "hsl(205, 68%, 36%)",
  "hsl(225, 80%, 48%)",
  "hsl(200, 65%, 34%)",
  "hsl(218, 76%, 44%)",
  "hsl(212, 73%, 40%)",
  "hsl(208, 70%, 37%)",
  "hsl(222, 79%, 47%)",
  "hsl(203, 66%, 35%)",
  "hsl(216, 74%, 43%)",
];

// Yellow serial palette for taker agents (light theme)
var agentColorPaletteTakerLight = [
  "hsl(42, 78%, 38%)",
  "hsl(45, 82%, 42%)",
  "hsl(48, 86%, 46%)",
  "hsl(40, 74%, 36%)",
  "hsl(50, 88%, 48%)",
  "hsl(38, 70%, 34%)",
  "hsl(46, 84%, 44%)",
  "hsl(52, 90%, 50%)",
  "hsl(44, 80%, 40%)",
  "hsl(47, 85%, 45%)",
  "hsl(41, 76%, 37%)",
  "hsl(49, 87%, 47%)",
];

// Blue serial palette for maker agents (dark theme)
var agentColorPaletteMakerDark = [
  "hsl(210, 82%, 72%)",
  "hsl(215, 85%, 74%)",
  "hsl(220, 88%, 76%)",
  "hsl(205, 78%, 70%)",
  "hsl(225, 90%, 78%)",
  "hsl(200, 75%, 68%)",
  "hsl(218, 84%, 73%)",
  "hsl(212, 83%, 72%)",
  "hsl(208, 80%, 71%)",
  "hsl(222, 87%, 77%)",
  "hsl(203, 76%, 69%)",
  "hsl(216, 86%, 75%)",
];

// Yellow serial palette for taker agents (dark theme)
var agentColorPaletteTakerDark = [
  "hsl(42, 88%, 72%)",
  "hsl(45, 90%, 74%)",
  "hsl(48, 92%, 76%)",
  "hsl(40, 84%, 70%)",
  "hsl(50, 94%, 78%)",
  "hsl(38, 82%, 68%)",
  "hsl(46, 89%, 73%)",
  "hsl(52, 95%, 79%)",
  "hsl(44, 87%, 71%)",
  "hsl(47, 91%, 75%)",
  "hsl(41, 85%, 69%)",
  "hsl(49, 93%, 77%)",
];
// Pink serial palette for other agents (light theme)
var agentColorPaletteOtherLight = [
  "hsl(330, 72%, 42%)",
  "hsl(335, 75%, 46%)",
  "hsl(340, 78%, 48%)",
  "hsl(325, 68%, 40%)",
  "hsl(345, 80%, 50%)",
  "hsl(320, 65%, 38%)",
  "hsl(338, 76%, 47%)",
  "hsl(332, 73%, 44%)",
  "hsl(328, 70%, 41%)",
  "hsl(342, 79%, 49%)",
  "hsl(322, 66%, 39%)",
  "hsl(336, 74%, 45%)",
];

// Pink serial palette for other agents (dark theme)
var agentColorPaletteOtherDark = [
  "hsl(330, 82%, 74%)",
  "hsl(335, 85%, 76%)",
  "hsl(340, 88%, 78%)",
  "hsl(325, 78%, 72%)",
  "hsl(345, 90%, 80%)",
  "hsl(320, 75%, 70%)",
  "hsl(338, 84%, 75%)",
  "hsl(332, 83%, 74%)",
  "hsl(328, 80%, 73%)",
  "hsl(342, 87%, 79%)",
  "hsl(322, 76%, 71%)",
  "hsl(336, 86%, 77%)",
];

// Generate consistent color for a coldkey using hash
function getColdkeyColor(coldkey) {
  if (!coldkey) return "rgba(128,128,128,0.6)";
  if (coldkeyColors[coldkey]) return coldkeyColors[coldkey];

  // Check if this is a registered coldkey - use vibrant palette
  var isRegistered = registeredColdkeys.indexOf(coldkey) >= 0;
  if (isRegistered) {
    var index = registeredColdkeys.indexOf(coldkey);
    var color =
      registeredColorPalette[index % registeredColorPalette.length];
    coldkeyColors[coldkey] = color;
    return color;
  }

  // For non-registered coldkeys, use distinct color palette
  var hash = 0;
  for (var i = 0; i < coldkey.length; i++) {
    hash = coldkey.charCodeAt(i) + ((hash << 5) - hash);
    hash = hash & hash;
  }

  var paletteIndex = Math.abs(hash) % coldkeyColorPalette.length;
  var color = coldkeyColorPalette[paletteIndex];
  coldkeyColors[coldkey] = color;
  return color;
}

function isDarkTheme() {
  return document.documentElement.classList.contains("theme-dark");
}

function getAgentSide(agentName) {
  if (!agentName) return "other";
  if (/Taker/i.test(agentName)) return "taker";
  if (/Maker/i.test(agentName)) return "maker";
  if (/^UltimateMT/i.test(agentName)) return "taker";
  if (/^UltimateM/i.test(agentName)) return "maker";
  if (/^RandomM/i.test(agentName)) return "maker";
  return "other";
}

function getAgentColorPalette(side) {
  var isDark = isDarkTheme();
  if (side === "taker") {
    return isDark
      ? agentColorPaletteTakerDark
      : agentColorPaletteTakerLight;
  }
  if (side === "maker") {
    return isDark
      ? agentColorPaletteMakerDark
      : agentColorPaletteMakerLight;
  }
  return isDark
    ? agentColorPaletteOtherDark
    : agentColorPaletteOtherLight;
}

function hashAgentName(agentName) {
  var hash = 0;
  for (var i = 0; i < agentName.length; i++) {
    hash = agentName.charCodeAt(i) + ((hash << 5) - hash);
    hash = hash & hash;
  }
  return Math.abs(hash);
}

function getAgentColor(agentName, params) {
  if (!agentName) return isDarkTheme() ? "#e2e8f0" : "#6c757d";
  var side = getAgentSide(agentName);
  var cacheKey =
    agentName + ":" + side + (isDarkTheme() ? ":dark" : ":light");
  if (agentColors[cacheKey]) return agentColors[cacheKey];
  var palette = getAgentColorPalette(side);
  var paletteIndex = hashAgentName(agentName) % palette.length;
  var color = palette[paletteIndex];
  agentColors[cacheKey] = color;
  return color;
}

function getAgentBlockStyle(agentName, params) {
  var color = getAgentColor(agentName, params);
  if (color.indexOf("hsl(") === 0) {
    var bgAlpha = isDarkTheme() ? 0.42 : 0.22;
    var borderAlpha = isDarkTheme() ? 0.88 : 0.45;
    var borderWidth = isDarkTheme() ? "1.5px" : "1px";
    return (
      "background:" +
      hslToRgba(color, bgAlpha) +
      "; border:" +
      borderWidth +
      " solid " +
      hslToRgba(color, borderAlpha) +
      "; color:" +
      color +
      ";"
    );
  }
  return (
    "background:rgba(108,117,125,0.15); border:1px solid rgba(108,117,125,0.35); color:" +
    color +
    ";"
  );
}

// Convert HSL to RGBA for transparency
function hslToRgba(hsl, alpha) {
  var match = hsl.match(/hsl\((\d+),\s*(\d+)%,\s*(\d+)%\)/);
  if (!match) return hsl;

  var h = parseInt(match[1]) / 360;
  var s = parseInt(match[2]) / 100;
  var l = parseInt(match[3]) / 100;

  var r, g, b;
  if (s === 0) {
    r = g = b = l;
  } else {
    var hue2rgb = function (p, q, t) {
      if (t < 0) t += 1;
      if (t > 1) t -= 1;
      if (t < 1 / 6) return p + (q - p) * 6 * t;
      if (t < 1 / 2) return q;
      if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
      return p;
    };
    var q = l < 0.5 ? l * (1 + s) : l + s - l * s;
    var p = 2 * l - q;
    r = hue2rgb(p, q, h + 1 / 3);
    g = hue2rgb(p, q, h);
    b = hue2rgb(p, q, h - 1 / 3);
  }

  return (
    "rgba(" +
    Math.round(r * 255) +
    "," +
    Math.round(g * 255) +
    "," +
    Math.round(b * 255) +
    "," +
    alpha +
    ")"
  );
}

function getSubnetFromInput() {
  var el = document.getElementById("subnetInput");
  if (!el) return 88;
  var n = parseInt(el.value, 10);
  if (isNaN(n) || n < 0 || n > 255) return 88;
  return n;
}
function applySubnet() {
  loadColdkeys();
  loadData(true);
  // The three active bars are SN79-only and each hides itself when the subnet is
  // something else. That hide used to be driven by updateTableColumns() on every
  // render; it belongs here instead — once, on the user action that changes it.
  loadRegBotActiveRuns();
  loadBatchActiveRuns();
  loadUnstakeActiveRuns();
}
var lastDataLoadAt = 0;

// Each panel loader already swallows its own fetch errors, but they now share one
// Promise.all with the table render — so one of them throwing inside a .then()
// would take the whole table down with it. It didn't before, when they were
// fire-and-forget, and it must not now.
function settled(p) {
  return Promise.resolve(p).catch(function (err) {
    console.warn("panel loader failed:", err);
    return null;
  });
}

// ---- Metagraph: identity + state --------------------------------------
//
// Identity (hotkey <-> uid <-> coldkey, validator flags, IPs) only changes when
// something registers or deregisters. State (emission, incentive, age, immunity)
// changes every block. Fetched separately so identity answers 304 nearly always
// and only the ~5KB state half actually moves; merged back into the single shape
// processMetagraph() has always consumed, so nothing downstream changes.
function metagraphIdentityUrl(netuid) {
  return "/api/metagraph-identity?netuid=" + netuid;
}
function metagraphStateUrl(netuid) {
  return "/api/metagraph-state?netuid=" + netuid;
}

function mergeMetagraph(identity, state) {
  var order = identity.order || [];
  var cols = state.cols || {};
  function toMap(name, cast) {
    var arr = cols[name] || {};
    var out = {};
    for (var i = 0; i < order.length; i++)
      out[order[i]] = cast ? cast(arr[i]) : arr[i];
    return out;
  }
  return {
    hotkeyToUid: identity.hotkeyToUid || {},
    uidToHotkey: identity.uidToHotkey || {},
    hotkeyToColdkey: identity.hotkeyToColdkey || {},
    coldkeyToHotkeys: identity.coldkeyToHotkeys || {},
    isValidator: identity.isValidator || {},
    uidToIp: identity.uidToIp || {},
    immunityPeriod: identity.immunityPeriod,
    totalNeurons: identity.totalNeurons,
    error: identity.error,
    emissions: toMap("emissions"),
    incentives: toMap("incentives"),
    minerAge: toMap("minerAge"),
    isImmune: toMap("isImmune", Boolean),
    netuid: state.netuid != null ? state.netuid : identity.netuid,
    currentBlock: state.currentBlock,
    alphaPriceTao: state.alphaPriceTao,
    fetched_at: state.fetched_at,
  };
}

function loadMetagraph(netuid) {
  var identityUrl = metagraphIdentityUrl(netuid);
  var stateUrl = metagraphStateUrl(netuid);
  return Promise.all([
    conditionalFetch(identityUrl),
    conditionalFetch(stateUrl),
  ])
    .then(function (halves) {
      var identity = halves[0];
      var state = halves[1];
      // State's arrays are positional against identity's `order`. If a refresh
      // landed between the two requests, a stale identity would map every value
      // onto the wrong hotkey — and a length check would not catch it, because
      // one deregistration plus one registration keeps the count the same. The
      // server stamps state with the identity ETag it was built from, so a
      // mismatch is detectable; refetch identity unconditionally and merge that.
      if (
        state.identity_etag &&
        cachedEtagFor(identityUrl) &&
        state.identity_etag !== cachedEtagFor(identityUrl)
      ) {
        dropEtag(identityUrl);
        return conditionalFetch(identityUrl).then(function (fresh) {
          return mergeMetagraph(fresh, state);
        });
      }
      return mergeMetagraph(identity, state);
    });
}

function hideLoadingOverlay() {
  var el = document.getElementById("loadingOverlay");
  if (el) el.classList.remove("active");
}

// ---- Forced refresh ---------------------------------------------------------
//
// The Refresh button used to await /api/metagraph-data?refresh=1 before painting
// anything, which froze the page for ~3s per click. Measured on the server:
//
//   0.8s  importing bittensor into a fresh subprocess
//   2.1s  syncing the metagraph from finney
//
// None of that has to happen before the user sees a table. _metagraph_refresh_worker
// keeps the server's copy under 90s old, so the warm halves are a good answer
// immediately; the chain re-read runs behind the paint and the table updates again
// when it lands. Same reasoning as the reg-cost timeout that used to blank the page:
// one slow upstream must never hold the render hostage.
var metagraphRereadInFlight = false;

function setRefreshBusy(busy) {
  // The button is the only visible sign that a re-read is still running. The page
  // stays fully interactive throughout — this is a progress indicator, not a lock.
  var btn = document.getElementById("refreshBtn");
  if (!btn) return;
  btn.disabled = busy;
  btn.classList.toggle("refreshing", busy);
}

function forceMetagraphReread(netuid, onFresh) {
  if (metagraphRereadInFlight) return; // double-click, or a poll landing on top
  metagraphRereadInFlight = true;
  setRefreshBusy(true);
  // The response body is the old combined shape and is ignored: what matters is
  // the side effect on the server's cache, which the halves below then read.
  fetch("/api/metagraph-data?netuid=" + netuid + "&refresh=1", {
    cache: "no-store",
  })
    .then(function () {
      return loadMetagraph(netuid);
    })
    .then(function (fresh) {
      // Dropped if the user changed subnet while the chain was being read —
      // rendering netuid 79's metagraph into an 88 table would be worse than
      // showing nothing new.
      if (getSubnetFromInput() !== netuid) return;
      onFresh(fresh);
    })
    .catch(function (err) {
      console.warn("Forced metagraph re-read failed:", err);
    })
    .then(function () {
      metagraphRereadInFlight = false;
      setRefreshBusy(false);
    });
}

// Everything that has to run after the miner rows change. Module scope because
// both loadData() and the metagraph push handler repaint through it, and two
// copies of this list would drift.
function repaintTable() {
  updateStatistics();
  renderChart();
  filterTable();
  updateHighlightInfo();
}

// ---- Metagraph pushes -------------------------------------------------------
//
// The server publishes `metagraph:<netuid>` the moment a refresh lands a snapshot
// whose ETag actually changed — roughly every METAGRAPH_REFRESH_SECONDS. This
// handler used to be a comment saying loadData owned it, while loadData polled on
// a flat 5-minute timer: the table could sit 5 minutes stale with the "there is
// new data" notification arriving and being dropped on the floor.
//
// Subscribing makes it fresher AND cheaper. loadMetagraph() goes through
// conditionalFetch, so a re-read the client already has costs a 304 with no body,
// and the fetches now happen when the data changes rather than on a clock.
var metagraphPushInFlight = false;

function onMetagraphChanged(netuid) {
  // The server warms three netuids; only the one on screen matters. Merging
  // another subnet's metagraph into this table is the hazard the subnet-switch
  // path already guards against.
  if (netuid !== getSubnetFromInput()) return;
  if (metagraphPushInFlight) return;
  if (document.visibilityState !== "visible") return;
  // A forced re-read is already fetching newer data and will repaint itself.
  if (metagraphRereadInFlight) return;
  metagraphPushInFlight = true;
  loadMetagraph(netuid)
    .then(function (fresh) {
      if (getSubnetFromInput() !== netuid) return;
      processMetagraph(fresh);
      repaintTable();
      lastDataLoadAt = Date.now();
    })
    .catch(function (err) {
      console.warn("Metagraph push refresh failed:", err);
    })
    .then(function () {
      metagraphPushInFlight = false;
    });
}

function loadData(forceRefresh) {
  document.getElementById("loadingOverlay").classList.add("active");
  setRefreshBusy(true);
  lastDataLoadAt = Date.now();
  var netuid = getSubnetFromInput();

  loadMetagraph(netuid)
    .then(function (metagraphData) {
      processMetagraph(metagraphData);
      var afterLoad = repaintTable;

      // Paint the table the moment the metagraph is in, and again as soon as the
      // two feeds that add columns to it arrive. Everything else on this page
      // owns its own panel and repaints itself.
      //
      // The old shape awaited *every* loader before painting anything. Observed
      // 2026-08-12 with the chain endpoint refusing connections: /api/reg-cost
      // sat for its full 120s timeout and the page showed nothing at all, even
      // though the metagraph had already come back from the on-disk snapshot.
      // One slow upstream must not be able to hold the table hostage.
      afterLoad();
      hideLoadingOverlay();

      // Now that something is on screen, go ask the chain for a newer answer.
      if (forceRefresh) {
        forceMetagraphReread(netuid, function (fresh) {
          processMetagraph(fresh);
          afterLoad();
        });
      }

      // Below the metagraph nothing depends on anything else: /api/uid-metrics is
      // called with no uids, reg-cost and alpha-price take only a netuid. These
      // used to be chained, which bought nothing and cost three extra round trips
      // (~400ms same-host, ~750ms from another continent).
      var tableFeeds = [];
      var panels = [];
      var subnet = getSubnetFromInput();
      if (subnet === 79) {
        if (forceRefresh) loadRegBotBalances();
        tableFeeds = [settled(loadMinerAgentInfo()), settled(loadUidMetrics())];
        panels = [
          settled(loadFeeRegimePanel(forceRefresh)),
          settled(loadSimId()),
          settled(loadSubnetRegCost(forceRefresh)),
          settled(loadAlphaPrice()),
        ];
      } else if (subnet === 88) {
        hideFeeRegimePanel();
        hideSimId();
        minerAgentInfo = {};
        uidMetrics = {};
        uidMetricDeltas = {};
        tableFeeds = [settled(loadInvesting88Days(forceRefresh))];
        panels = [
          settled(loadSubnetRegCost(forceRefresh)),
          settled(loadAlphaPrice()),
        ];
      } else {
        hideFeeRegimePanel();
        hideSimId();
        minerAgentInfo = {};
        uidMetrics = {};
        uidMetricDeltas = {};
        investing88DaysMap = {};
        investing88ScoresMap = {};
        investing88PrevScoresMap = {};
        panels = [
          settled(loadSubnetRegCost(forceRefresh)),
          settled(loadAlphaPrice()),
        ];
      }

      // Repaint on the table feeds only; the panels must not gate the table.
      var painted = Promise.all(tableFeeds).then(afterLoad);

      // ...and the panels must not gate the *button* either. On a forced refresh
      // `/api/fee-regime?refresh=1` and `/api/reg-cost?refresh=1` are real upstream
      // calls — 2.2s of TAOS, 2.1s of chain — and each panel repaints itself the
      // moment its own answer lands, showing its previous value until then. Waiting
      // for them here left Refresh disabled for ~3s after the page was already
      // current. Every panel is `settled()`, so none of these can reject.
      Promise.all(panels);
      return painted;
    })
    .catch(function (err) {
      console.error(err);
      alert("Failed to load data: " + err.message);
    })
    .finally(function () {
      hideLoadingOverlay();
      // A forced re-read outlives this promise chain by design; it owns the
      // button until it finishes.
      if (!metagraphRereadInFlight) setRefreshBusy(false);
    });
}

function formatFeePct(value) {
  if (value == null || isNaN(value)) return "—";
  var pct = value * 100;
  return (pct >= 0 ? "+" : "") + pct.toFixed(4) + "%";
}

function hideFeeRegimePanel() {
  var panel = document.getElementById("feeRegimePanel");
  if (panel)
    panel.classList.remove(
      "visible",
      "regime-maker-rebate",
      "regime-taker-rebate",
      "regime-both-pay",
    );
}

function destroyFeeRegimeCharts() {
  feeRegimeCharts.forEach(function (c) {
    if (c) c.destroy();
  });
  feeRegimeCharts = [];
}

function renderFeeRegimeSummary(data) {
  var panel = document.getElementById("feeRegimePanel");
  if (!panel || getSubnetFromInput() !== 79) return;
  feeRegimeData = data;
  panel.classList.add("visible");
  panel.classList.remove(
    "regime-maker-rebate",
    "regime-taker-rebate",
    "regime-both-pay",
  );
  var regimeClass =
    {
      "MAKER-REBATE": "regime-maker-rebate",
      "TAKER-REBATE": "regime-taker-rebate",
      BOTH_PAY: "regime-both-pay",
    }[data.regime] || "regime-both-pay";
  panel.classList.add(regimeClass);

  document.getElementById("feeRegimeBadge").textContent =
    data.regime_label || data.regime || "DIS";
  var cheaper = data.cheaper_side || "";
  var gap = formatFeePct(data.median_gap);
  document.getElementById("feeRegimeDesc").textContent =
    (data.regime_hint || "") +
    (cheaper
      ? " · " + cheaper + " cheaper by " + gap.replace("+", "")
      : "");

  var mk = data.maker || {};
  var tk = data.taker || {};
  document.getElementById("feeRegimeStats").innerHTML =
    "<span>Maker median: " +
    formatFeePct(mk.median) +
    "</span>" +
    "<span>Taker median: " +
    formatFeePct(tk.median) +
    "</span>" +
    "<span>" +
    (tk.rebate_books || 0) +
    "/" +
    (data.book_count || 0) +
    " taker rebated</span>" +
    "<span>" +
    (mk.rebate_books || 0) +
    "/" +
    (data.book_count || 0) +
    " maker rebated</span>";

  var updatedEl = document.getElementById("feeRegimeUpdated");
  if (updatedEl && data.updated_at) {
    updatedEl.textContent =
      "Updated " + new Date(data.updated_at).toLocaleTimeString();
  }
}

function renderFeeRegimeHistoryCharts(
  globalPoints,
  bookPoints,
  liveBooks,
) {
  destroyFeeRegimeCharts();
  var chartsWrap = document.querySelector(
    "#feeRegimePanel .fee-regime-charts",
  );
  var emptyEl = document.getElementById("feeRegimeEmpty");
  globalPoints = globalPoints || [];
  bookPoints = bookPoints || [];
  var hasBooks =
    bookPoints.length > 0 ||
    (liveBooks && Object.keys(liveBooks).length > 0);
  if (!globalPoints.length && !hasBooks) {
    if (chartsWrap) chartsWrap.style.display = "none";
    if (emptyEl) emptyEl.style.display = "";
    return;
  }
  if (chartsWrap) chartsWrap.style.display = "";
  if (emptyEl) emptyEl.style.display = "none";

  var isDark = document.documentElement.classList.contains("theme-dark");
  var gridColor = isDark ? "rgba(148,163,184,0.2)" : "rgba(0,0,0,0.08)";
  var textColor = isDark ? "#cbd5e1" : "#495057";

  function buildLineChartOptions(showLegend, xTitle) {
    var options = {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          display: !!showLegend,
          labels: { color: textColor, boxWidth: 12, padding: 10 },
        },
        tooltip: {
          callbacks: {
            title: function (items) {
              if (!items.length) return "";
              var label = items[0].label;
              return xTitle ? xTitle + " " + label : label;
            },
            label: function (context) {
              var seriesLabel = context.dataset.label || "";
              var val = context.parsed.y;
              if (val == null || isNaN(val)) {
                return seriesLabel ? seriesLabel + ": —" : "—";
              }
              return (
                (seriesLabel ? seriesLabel + ": " : "") +
                formatFeePct(val)
              );
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            maxRotation: 0,
            minRotation: 0,
            color: textColor,
            maxTicksLimit: 16,
            autoSkip: true,
          },
          grid: { color: gridColor },
        },
        y: {
          ticks: {
            color: textColor,
            callback: function (v) {
              return formatFeePct(v);
            },
          },
          grid: { color: gridColor },
        },
      },
    };
    if (xTitle) {
      options.scales.x.title = {
        display: true,
        text: xTitle,
        color: textColor,
        font: { size: 11 },
      };
    }
    return options;
  }

  function makeDataset(label, data, color, pointCount) {
    return {
      label: label,
      data: data,
      borderColor: color,
      backgroundColor: color + "33",
      fill: false,
      tension: 0.15,
      pointRadius: pointCount > 40 ? 0 : 1.5,
      spanGaps: true,
    };
  }

  var medianCtx = document.getElementById("feeRegimeMedianChart");
  if (medianCtx && globalPoints.length) {
    var medianLabels = globalPoints.map(function (p) {
      return formatFeeRegimeLabel(p.snapshot_at);
    });
    feeRegimeCharts.push(
      new Chart(medianCtx, {
        type: "line",
        data: {
          labels: medianLabels,
          datasets: [
            makeDataset(
              "Maker",
              globalPoints.map(function (p) {
                return p.maker_median;
              }),
              "#fd7e14",
              globalPoints.length,
            ),
            makeDataset(
              "Taker",
              globalPoints.map(function (p) {
                return p.taker_median;
              }),
              "#0d6efd",
              globalPoints.length,
            ),
          ],
        },
        options: buildLineChartOptions(true),
      }),
    );
  }

  var booksPayload = null;
  var booksSnapshotAt = null;
  if (bookPoints.length) {
    var latestBookPoint = bookPoints[bookPoints.length - 1];
    booksPayload = latestBookPoint.books;
    booksSnapshotAt = latestBookPoint.snapshot_at;
  } else if (liveBooks) {
    booksPayload = liveBooks;
    booksSnapshotAt = feeRegimeData && feeRegimeData.updated_at;
  }

  var booksCtx = document.getElementById("feeRegimeBooksChart");
  var booksSublabel = document.getElementById("feeRegimeBooksSublabel");
  if (booksCtx && booksPayload && Object.keys(booksPayload).length) {
    var bookIds = Object.keys(booksPayload).sort(function (a, b) {
      var na = parseInt(a, 10);
      var nb = parseInt(b, 10);
      if (!isNaN(na) && !isNaN(nb)) return na - nb;
      return String(a).localeCompare(String(b));
    });
    var makerSeries = bookIds.map(function (id) {
      var row = booksPayload[id] || {};
      return row.maker != null ? row.maker : null;
    });
    var takerSeries = bookIds.map(function (id) {
      var row = booksPayload[id] || {};
      return row.taker != null ? row.taker : null;
    });
    if (booksSublabel) {
      booksSublabel.textContent = booksSnapshotAt
        ? "Latest snapshot · " + formatHistoryLabel(booksSnapshotAt)
        : "";
    }
    feeRegimeCharts.push(
      new Chart(booksCtx, {
        type: "line",
        data: {
          labels: bookIds,
          datasets: [
            makeDataset("Maker", makerSeries, "#fd7e14", bookIds.length),
            makeDataset("Taker", takerSeries, "#0d6efd", bookIds.length),
          ],
        },
        options: buildLineChartOptions(true, "Book"),
      }),
    );
  } else if (booksSublabel) {
    booksSublabel.textContent = "";
  }
}

function renderFeeRegimeDetails(data) {
  var body = document.getElementById("feeRegimeDetailsBody");
  if (!body || !data) return;
  var mk = data.maker || {};
  var tk = data.taker || {};
  var html =
    "<div><strong>Maker</strong> range " +
    formatFeePct(mk.min) +
    " to " +
    formatFeePct(mk.max) +
    " · <strong>Taker</strong> range " +
    formatFeePct(tk.min) +
    " to " +
    formatFeePct(tk.max) +
    "</div>";
  if (data.books) {
    var takerBooks = Object.keys(data.books)
      .map(function (bookId) {
        return { bookId: bookId, taker: data.books[bookId].taker };
      })
      .filter(function (row) {
        return row.taker != null;
      })
      .sort(function (a, b) {
        return b.taker - a.taker;
      })
      .slice(0, 12);
    if (takerBooks.length) {
      html +=
        '<div style="margin-top:8px"><strong>Most expensive taker books</strong></div><div class="fee-regime-books">';
      takerBooks.forEach(function (row) {
        html +=
          "<span>book " +
          row.bookId +
          ": " +
          formatFeePct(row.taker) +
          "</span>";
      });
      html += "</div>";
    }
  }
  body.innerHTML = html;
}

function toggleFeeRegimeDetails() {
  var details = document.getElementById("feeRegimeDetails");
  var btn = document.getElementById("feeRegimeToggleBtn");
  if (!details || !btn) return;
  var show = details.style.display === "none";
  details.style.display = show ? "" : "none";
  btn.textContent = show ? "Hide books" : "Books";
  if (show && feeRegimeData && !feeRegimeData.books) {
    fetch("/api/fee-regime?books=1")
      .then(function (r) {
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data) {
          feeRegimeData = data;
          renderFeeRegimeDetails(data);
        }
      })
      .catch(function () {});
  } else if (show) {
    renderFeeRegimeDetails(feeRegimeData);
  }
}

function loadFeeRegimePanel(forceRefresh) {
  if (getSubnetFromInput() !== 79) {
    hideFeeRegimePanel();
    return Promise.resolve();
  }
  var days =
    parseInt(document.getElementById("feeRegimeDaysSelect").value, 10) ||
    7;
  // Don't request books=1 here: the charts use history (global_points +
  // book_points), and the plain summary is cached/lighter/reliable.
  // books=1 is fetched lazily only when the "Books" details are opened.
  var summaryParams = [];
  if (forceRefresh) summaryParams.push("refresh=1");
  var summaryQs = summaryParams.join("&");
  var summaryUrl = "/api/fee-regime" + (summaryQs ? "?" + summaryQs : "");
  // book_points=latest: the books chart only ever plots the newest snapshot
  // (see renderFeeRegimeHistoryCharts), and shipping one per point instead made
  // this a ~754KB response for ~10KB of used data.
  var historyUrl =
    "/api/fee-regime/history?days=" + days + "&book_points=latest";
  // Neither fetch rejects: a failed live summary must NOT wipe the
  // history charts (they come from a separate, always-available source).
  return Promise.all([
    conditionalFetch(summaryUrl).catch(function () {
      return null;
    }),
    conditionalFetch(historyUrl).catch(function () {
      return { global_points: [], book_points: [] };
    }),
  ]).then(function (results) {
    var summary = results[0];
    var history = results[1] || { global_points: [], book_points: [] };
    if (summary && !summary.error) {
      renderFeeRegimeSummary(summary);
    } else if (getSubnetFromInput() === 79) {
      // Live regime unavailable — keep the panel and history charts.
      var panel = document.getElementById("feeRegimePanel");
      if (panel) panel.classList.add("visible");
      var badge = document.getElementById("feeRegimeBadge");
      if (badge) badge.textContent = "—";
      var desc = document.getElementById("feeRegimeDesc");
      if (desc)
        desc.textContent =
          "Live fee regime unavailable — showing recent history";
    }
    renderFeeRegimeHistoryCharts(
      history.global_points || [],
      history.book_points || [],
      (summary && summary.books) || null,
    );
  });
}

function reloadFeeRegimePanel() {
  loadFeeRegimePanel(false);
}

function loadMinerAgentInfo() {
  return conditionalFetch("/api/miner-params")
    .then(function (data) {
      minerAgentInfo = data.agents || {};
      myMinerUids = data.uids || [];
    })
    .catch(function () {
      minerAgentInfo = {};
      myMinerUids = [];
    });
}

function hasTrackedHistory(uid) {
  return myMinerUids.indexOf(Number(uid)) >= 0;
}

// The server sends columns plus ready-made deltas instead of a full duplicate
// of the previous epoch's metrics (which was half the payload, and existed to
// draw arrows on three fields). Rehydrate into the per-UID object shape the rest
// of the file expects, so only this function knows about the wire format.
function rehydrateUidMetrics(data) {
  var uids = data.uids || [];
  var fields = data.fields || [];
  var cols = data.cols || {};
  var metrics = {};
  for (var i = 0; i < uids.length; i++) {
    var row = {};
    for (var f = 0; f < fields.length; f++) {
      var name = fields[f];
      var col = cols[name];
      if (col) row[name] = col[i];
    }
    metrics[String(uids[i])] = row;
  }
  var deltas = {};
  var dcols = data.deltas || {};
  for (var k in dcols) {
    if (!Object.prototype.hasOwnProperty.call(dcols, k)) continue;
    for (var j = 0; j < uids.length; j++) {
      var v = dcols[k][j];
      if (v == null) continue;
      var key = String(uids[j]);
      (deltas[key] || (deltas[key] = {}))[k] = v;
    }
  }
  return { metrics: metrics, deltas: deltas };
}

function loadUidMetrics(refresh) {
  var url = "/api/uid-metrics" + (refresh ? "?refresh=1" : "");
  return conditionalFetch(url)
    .then(function (data) {
      // `metrics` means the server was asked for shape=full; nothing in the UI
      // does that, but honour it rather than render an empty table if it happens.
      var hydrated = data.metrics
        ? { metrics: data.metrics, deltas: {} }
        : rehydrateUidMetrics(data);
      uidMetrics = hydrated.metrics;
      uidMetricDeltas = hydrated.deltas;
      if (data.book_volume_limit != null)
        bookVolumeLimit = data.book_volume_limit;
      if (data.book_volume_warn_ratio != null)
        bookVolumeWarnRatio = data.book_volume_warn_ratio;
    })
    .catch(function (err) {
      console.warn("Failed to load UID metrics:", err);
      uidMetrics = {};
      uidMetricDeltas = {};
    });
}

function getUidMetrics(uid) {
  return uidMetrics[String(uid)] || uidMetrics[uid] || null;
}
function getMetricDelta(uid, key) {
  // Computed server-side against the previous epoch snapshot; see
  // rehydrateUidMetrics. Only real_pnl, unrl_pnl and volume carry one.
  var row = uidMetricDeltas[String(uid)] || uidMetricDeltas[uid];
  if (!row || row[key] == null) return null;
  return Number(row[key]);
}
function renderDeltaHtml(delta) {
  if (delta == null || delta === 0) return "";
  var rounded = Math.round(delta);
  if (rounded === 0) return "";
  var sign = rounded > 0 ? "+" : "";
  var color = rounded > 0 ? "#22c55e" : "#ef4444";
  return (
    '<span style="font-size:0.7rem;color:' +
    color +
    ';margin-left:3px">' +
    sign +
    formatCompactNumber(rounded, 1) +
    "</span>"
  );
}
function formatCompactNumber(value, smallDecimals) {
  if (value == null || Number.isNaN(Number(value))) return "n/a";
  var v = Number(value);
  if (Math.abs(v) >= 1000000) return (v / 1e6).toFixed(2) + "M";
  if (Math.abs(v) >= 1000) return (v / 1e3).toFixed(1) + "K";
  smallDecimals = smallDecimals == null ? 4 : smallDecimals;
  return String(Number(v.toFixed(smallDecimals)));
}

function formatMetricValue(value) {
  return formatCompactNumber(value, 4);
}

function getMetricValue(uid, key) {
  var metrics = getUidMetrics(uid);
  if (!metrics || metrics[key] == null) return null;
  return Number(metrics[key]);
}

function getBookVolumeWarnThreshold() {
  return bookVolumeLimit * bookVolumeWarnRatio;
}

function getHotBookCount(uid) {
  var metrics = getUidMetrics(uid);
  if (!metrics || metrics.hot_book_count == null) return 0;
  return Number(metrics.hot_book_count) || 0;
}

function getHotBookWarnStyle(hotCount, bookCount) {
  if (!hotCount || hotCount <= 0) return "";
  var share =
    bookCount > 0 ? hotCount / bookCount : Math.min(hotCount / 8, 1);
  var intensity = Math.min(
    0.85,
    0.28 + share * 0.45 + Math.min(hotCount, 8) * 0.04,
  );
  return (
    "background:rgba(220,53,69," + intensity.toFixed(2) + "); color:#fff;"
  );
}

function getHotBookRowAccent(hotCount, bookCount) {
  if (!hotCount || hotCount <= 0) return "";
  var share =
    bookCount > 0 ? hotCount / bookCount : Math.min(hotCount / 8, 1);
  var intensity = Math.min(
    0.95,
    0.45 + share * 0.4 + Math.min(hotCount, 8) * 0.05,
  );
  return (
    "box-shadow:inset 4px 0 0 rgba(220,53,69," +
    intensity.toFixed(2) +
    ");"
  );
}

function getBookVolumeCellStyle(volume) {
  if (volume == null || Number.isNaN(Number(volume))) return "";
  var v = Number(volume);
  var warnAt = getBookVolumeWarnThreshold();
  if (v < warnAt) return "";
  var span = Math.max(bookVolumeLimit - warnAt, 1);
  var progress = Math.min(1, (v - warnAt) / span);
  var intensity = Math.min(0.9, 0.35 + progress * 0.5);
  return (
    "background:rgba(220,53,69," +
    intensity.toFixed(2) +
    "); color:#fff; font-weight:700;"
  );
}

function renderMetricCell(uid, key) {
  var val = getMetricValue(uid, key);
  if (val == null) return '<span class="coldkey-text">-</span>';
  return (
    '<span class="metric-cell">' +
    escapeHtml(formatMetricValue(val)) +
    "</span>"
  );
}

function renderPnlMetricCell(uid, key) {
  var val = getMetricValue(uid, key);
  if (val == null) return '<span class="coldkey-text">-</span>';
  var cls = val < 0 ? " metric-pnl-neg" : "";
  return (
    '<span class="metric-cell' +
    cls +
    '">' +
    escapeHtml(formatMetricValue(val)) +
    renderDeltaHtml(getMetricDelta(uid, key)) +
    "</span>"
  );
}

function formatPnlValueHtml(value) {
  if (value == null || Number.isNaN(Number(value)))
    return escapeHtml("-");
  var num = Number(value);
  var cls = num < 0 ? " metric-pnl-neg" : "";
  return (
    '<span class="metric-cell' +
    cls +
    '">' +
    escapeHtml(formatMetricValue(num)) +
    "</span>"
  );
}

function renderPnlBooksCell(uid) {
  var metrics = getUidMetrics(uid);
  if (!metrics) return '<span class="metric-empty">-</span>';
  var pos = metrics.pnl_books_pos;
  var neg = metrics.pnl_books_neg;
  if (pos == null && neg == null)
    return '<span class="metric-empty">-</span>';
  pos = pos == null ? 0 : Number(pos);
  neg = neg == null ? 0 : Number(neg);
  return (
    '<span class="metric-cell pnl-books-cell">' +
    '<span class="pnl-books-pos">' +
    pos +
    "+</span> / " +
    '<span class="pnl-books-neg">' +
    neg +
    "-</span></span>"
  );
}

function renderVolumeCell(uid) {
  var val = getMetricValue(uid, "volume");
  var metrics = getUidMetrics(uid) || {};
  var hotCount = getHotBookCount(uid);
  var warnStyle = getHotBookWarnStyle(hotCount, metrics.book_count || 0);
  var title =
    hotCount > 0
      ? hotCount +
        " book(s) at or above " +
        Math.round(bookVolumeWarnRatio * 100) +
        "% of " +
        formatCompactNumber(bookVolumeLimit, 0) +
        " limit"
      : "";
  if (val == null) return '<span class="coldkey-text">-</span>';
  var warnIcon =
    hotCount > 0
      ? ' <i class="bi bi-exclamation-triangle-fill" style="font-size:0.7rem"></i>'
      : "";
  return (
    '<span class="metric-cell volume-warn-cell"' +
    (warnStyle ? ' style="' + warnStyle + '"' : "") +
    (title ? ' title="' + escapeHtml(title) + '"' : "") +
    ">" +
    escapeHtml(formatMetricValue(val)) +
    renderDeltaHtml(getMetricDelta(uid, "volume")) +
    warnIcon +
    "</span>"
  );
}

function renderMkTkVolumeCell(uid) {
  var metrics = getUidMetrics(uid);
  if (!metrics || (metrics.vol_mkr == null && metrics.vol_tkr == null)) {
    return '<span class="coldkey-text">-</span>';
  }
  return (
    '<span class="metric-cell">' +
    escapeHtml(formatMetricValue(metrics.vol_mkr)) +
    " / " +
    escapeHtml(formatMetricValue(metrics.vol_tkr)) +
    "</span>"
  );
}

function serverNameFromIp(ip) {
  if (!ip) return "";
  return SERVER_NICKNAMES[ip] || ip;
}

function getDisplayParamKeys(agentName) {
  var keys = DISPLAY_PARAM_KEYS.slice();
  if (agentName && /Eater/i.test(agentName)) {
    keys.push("eat_quantity");
  }
  return keys;
}

// agent_index identifies the active agent by its agent_id field, not by
// list position. Fall back to positional for older files without agent_id.
function selectActiveAgent(agents, agentIndex) {
  if (!Array.isArray(agents)) return null;
  for (var i = 0; i < agents.length; i++) {
    if (agents[i] && agents[i].agent_id === agentIndex) return agents[i];
  }
  if (agentIndex >= 0 && agentIndex < agents.length)
    return agents[agentIndex];
  return null;
}

function extractAgentInfoFromData(data) {
  if (!data || data.agent_index == null || !data.agents) return null;
  var agent = selectActiveAgent(data.agents, data.agent_index);
  if (!agent) return null;
  var params = {};
  getDisplayParamKeys(agent.name || "").forEach(function (key) {
    if (agent.params && agent.params[key] !== undefined)
      params[key] = agent.params[key];
  });
  var minerIp = data.miner_ip || "";
  return {
    name: agent.name || "",
    params: params,
    miner_ip: minerIp,
    server: serverNameFromIp(minerIp),
  };
}

function formatParamValue(value) {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number") {
    if (Number.isInteger(value)) return String(value);
    var rounded = Number(value.toFixed(4));
    return String(rounded);
  }
  return String(value);
}

function formatAgentParamsSummary(params, agentName) {
  if (!params) return "";
  return getDisplayParamKeys(agentName)
    .filter(function (key) {
      return params[key] !== undefined;
    })
    .map(function (key) {
      return (
        '<span class="agent-param-item">' +
        escapeHtml(key) +
        ': <span class="agent-param-value">' +
        escapeHtml(formatParamValue(params[key])) +
        "</span></span>"
      );
    })
    .join("");
}

function getMinerAgentInfo(uid) {
  var info = minerAgentInfo[String(uid)] || minerAgentInfo[uid];
  if (!info) return null;
  if (typeof info === "string")
    return { name: info, params: {}, miner_ip: "", server: "" };
  return info;
}

function getMinerAxonIp(uid) {
  var miner = minersArray.find(function (m) {
    return m.uid === uid;
  });
  return miner && miner.axonIp ? miner.axonIp : "";
}

function getServerInfoForUid(uid) {
  var info = getMinerAgentInfo(uid);
  var ip = getMinerAxonIp(uid) || (info && info.miner_ip) || "";
  if (!ip) return { server: "", miner_ip: "" };
  return { server: serverNameFromIp(ip), miner_ip: ip };
}

function getMinerServerName(uid) {
  return getServerInfoForUid(uid).server;
}

function renderServerCell(m) {
  var serverInfo = getServerInfoForUid(m.uid);
  if (!serverInfo.server && !serverInfo.miner_ip) {
    return '<span class="coldkey-text">-</span>';
  }
  var label = escapeHtml(serverInfo.server || serverInfo.miner_ip);
  var ipLine =
    serverInfo.miner_ip &&
    serverInfo.server &&
    serverInfo.server !== serverInfo.miner_ip
      ? '<span class="server-cell-ip">' +
        escapeHtml(serverInfo.miner_ip) +
        "</span>"
      : "";
  return (
    '<span class="server-cell-label" title="' +
    escapeHtml(serverInfo.miner_ip || serverInfo.server) +
    '">' +
    label +
    "</span>" +
    ipLine
  );
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// creg79.py and unstake_env.py colour their output, so log lines pulled
// into the UI carry raw ANSI escapes ("[31m0.12τ[0m") that
// render as literal "[31m" noise in a tooltip or table cell.
function stripAnsi(text) {
  // eslint-disable-next-line no-control-regex
  return String(text == null ? "" : text).replace(/\[[0-9;]*m/g, "");
}

function setMinerParamsStatus(message, type) {
  var el = document.getElementById("minerParamsStatus");
  el.textContent = message || "";
  el.className = "params-status" + (type ? " " + type : "");
}

function showNotifyAlert(title, message, type, durationMs) {
  var container = document.getElementById("notifyToastContainer");
  if (!container) return;
  type = type || "success";
  durationMs = durationMs == null ? 5000 : durationMs;
  var iconClass =
    type === "error"
      ? "bi-exclamation-triangle-fill"
      : "bi-check-circle-fill";
  var toastEl = document.createElement("div");
  toastEl.className = "toast notify-toast notify-toast-" + type;
  toastEl.setAttribute("role", "alert");
  toastEl.setAttribute("aria-live", "assertive");
  toastEl.setAttribute("aria-atomic", "true");
  toastEl.innerHTML =
    '<div class="toast-header">' +
    '<i class="bi ' +
    iconClass +
    ' me-2"></i>' +
    '<strong class="me-auto">' +
    escapeHtml(title || (type === "error" ? "Error" : "Success")) +
    "</strong>" +
    '<button type="button" class="btn-close" data-bs-dismiss="toast" aria-label="Close"></button>' +
    "</div>" +
    '<div class="toast-body">' +
    escapeHtml(message || "") +
    "</div>";
  container.appendChild(toastEl);
  var toast = bootstrap.Toast.getOrCreateInstance(toastEl, {
    delay: durationMs,
    autohide: durationMs > 0,
  });
  toastEl.addEventListener("hidden.bs.toast", function () {
    toastEl.remove();
  });
  toast.show();
}

function getSelectedAgentIndex() {
  var el = document.getElementById("paramAgentIndex");
  if (!el) {
    return currentParamsData && currentParamsData.agent_index != null
      ? currentParamsData.agent_index
      : 0;
  }
  return parseInt(el.value, 10);
}

var AGENT_STYLE_GROUPS = [
  {
    label: "Taker",
    test: function (n) {
      return /taker/i.test(n);
    },
  },
  {
    label: "Maker",
    test: function (n) {
      return /maker/i.test(n) || /^RandomM[A-Z]/i.test(n);
    },
  },
  {
    label: "Hybrid",
    test: function (n) {
      return /hybrid/i.test(n);
    },
  },
  {
    label: "Ultimate",
    test: function (n) {
      return /ultimate/i.test(n);
    },
  },
  {
    label: "Futures",
    test: function (n) {
      return /futures/i.test(n);
    },
  },
  {
    label: "Profit",
    test: function (n) {
      return /profit/i.test(n);
    },
  },
  {
    label: "Other",
    test: function (n) {
      return true;
    },
  },
];

function classifyAgent(name) {
  for (var i = 0; i < AGENT_STYLE_GROUPS.length; i++) {
    if (AGENT_STYLE_GROUPS[i].test(name))
      return AGENT_STYLE_GROUPS[i].label;
  }
  return "Other";
}

function buildAgentListHtml(data, selectedIndex) {
  var agents = data.agents || [];
  var grouped = {};
  var order = [];
  agents.forEach(function (agent, idx) {
    var name = agent && agent.name ? agent.name : "Agent " + idx;
    // The control file selects agents by agent_id, not list position.
    var aid = agent && agent.agent_id != null ? agent.agent_id : idx;
    var group = classifyAgent(name);
    if (!grouped[group]) {
      grouped[group] = [];
      order.push(group);
    }
    grouped[group].push({ idx: aid, name: name });
  });
  var html = '<div class="agent-list-box" id="agentListBox">';
  order.forEach(function (group) {
    html +=
      '<div class="agent-list-group-label">' +
      escapeHtml(group) +
      "</div>";
    grouped[group].forEach(function (item) {
      var sel = item.idx === selectedIndex ? " selected" : "";
      html +=
        '<div class="agent-list-item' +
        sel +
        '" data-idx="' +
        item.idx +
        '" onclick="selectAgentOption(' +
        item.idx +
        ')">' +
        highlightAgentName(item.name) +
        "</div>";
    });
  });
  html += "</div>";
  return html;
}

function selectAgentOption(idx) {
  var hidden = document.getElementById("paramAgentIndex");
  if (hidden) hidden.value = idx;
  var box = document.getElementById("agentListBox");
  if (box) {
    box.querySelectorAll(".agent-list-item").forEach(function (el) {
      el.classList.toggle(
        "selected",
        parseInt(el.dataset.idx, 10) === idx,
      );
    });
  }
  onParamAgentIndexChange();
}

function buildAgentSelectHtml(data, selectedIndex) {
  return (data.agents || [])
    .map(function (agent, idx) {
      var name = agent && agent.name ? agent.name : "Agent " + idx;
      return (
        '<option value="' +
        idx +
        '"' +
        (idx === selectedIndex ? " selected" : "") +
        ">" +
        escapeHtml(name) +
        " (" +
        idx +
        ")</option>"
      );
    })
    .join("");
}

function getParamSelectOptions(agentName, paramKey, currentValue) {
  var agentOptions = AGENT_PARAM_SELECT_OPTIONS[agentName];
  if (!agentOptions || !agentOptions[paramKey]) return null;
  var options = agentOptions[paramKey].slice();
  var current = currentValue == null ? "" : String(currentValue);
  if (current && options.indexOf(current) < 0) options.unshift(current);
  return options;
}

function buildParamInputHtml(agent, key, value) {
  var selectOptions = getParamSelectOptions(agent.name, key, value);
  if (selectOptions) {
    var current = value == null ? "" : String(value);
    return (
      '<select data-param-key="' +
      escapeHtml(key) +
      '" data-param-type="string">' +
      selectOptions
        .map(function (opt) {
          return (
            '<option value="' +
            escapeHtml(opt) +
            '"' +
            (current === opt ? " selected" : "") +
            ">" +
            escapeHtml(opt) +
            "</option>"
          );
        })
        .join("") +
      "</select>"
    );
  }
  var type = typeof value;
  if (type === "boolean") {
    return (
      '<select data-param-key="' +
      escapeHtml(key) +
      '" data-param-type="boolean">' +
      '<option value="true"' +
      (value ? " selected" : "") +
      ">true</option>" +
      '<option value="false"' +
      (!value ? " selected" : "") +
      ">false</option></select>"
    );
  }
  if (type === "number") {
    return (
      '<input type="number" step="any" data-param-key="' +
      escapeHtml(key) +
      '" data-param-type="number" value="' +
      escapeHtml(value) +
      '">'
    );
  }
  return (
    '<input type="text" data-param-key="' +
    escapeHtml(key) +
    '" data-param-type="string" value="' +
    escapeHtml(value) +
    '">'
  );
}

function renderMinerParamsFormBody(data, agentIndex) {
  var formWrap = document.getElementById("minerParamsFormWrap");
  var formBody = document.getElementById("minerParamsFormBody");
  var confirmBtn = document.getElementById("minerParamsConfirmBtn");
  // agentIndex is an agent_id, not a list position.
  var agent = data.agents
    ? selectActiveAgent(data.agents, agentIndex)
    : null;

  if (!agent || !agent.params) {
    formWrap.style.display = "none";
    confirmBtn.style.display = "none";
    return;
  }

  formWrap.style.display = "block";
  confirmBtn.style.display = "inline-block";
  var keys = Object.keys(agent.params);
  formBody.innerHTML = keys
    .map(function (key) {
      var value = agent.params[key];
      var inputHtml = buildParamInputHtml(agent, key, value);
      return (
        "<tr><td>" +
        escapeHtml(key) +
        "</td><td>" +
        inputHtml +
        "</td></tr>"
      );
    })
    .join("");
}

function onParamAgentIndexChange() {
  if (!currentParamsData) return;
  renderMinerParamsFormBody(currentParamsData, getSelectedAgentIndex());
}

function renderMinerParamsForm(data, uid) {
  currentParamsUid = uid;
  currentParamsData = data;
  var metaEl = document.getElementById("minerParamsMeta");
  var emptyEl = document.getElementById("minerParamsEmpty");
  var confirmBtn = document.getElementById("minerParamsConfirmBtn");

  metaEl.style.display = "grid";
  emptyEl.style.display = "none";
  setMinerParamsStatus("", "");

  var agentIndex = data.agent_index != null ? data.agent_index : 0;

  var syncIp = getMinerAxonIp(uid) || data.miner_ip || "N/A";
  metaEl.innerHTML =
    '<div class="params-meta-item"><label>UID</label><div class="meta-value">' +
    uid +
    "</div></div>" +
    '<div class="params-meta-item"><label>Sync IP</label><div class="meta-value">' +
    escapeHtml(syncIp) +
    "</div></div>" +
    '<div class="params-meta-item"><label>Poll Seconds</label><div class="meta-value">' +
    (data.poll_seconds != null ? data.poll_seconds : 60) +
    "</div></div>" +
    '<div class="params-meta-item" style="grid-column:1/-1"><label>Agent</label>' +
    '<input type="hidden" id="paramAgentIndex" value="' +
    agentIndex +
    '">' +
    buildAgentListHtml(data, agentIndex) +
    "</div>";

  if (!data.agents || !data.agents.length) {
    document.getElementById("minerParamsFormWrap").style.display = "none";
    confirmBtn.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.textContent = "No agents found in params file.";
    loadAgentHistory(uid);
    return;
  }

  renderMinerParamsFormBody(data, agentIndex);
  loadAgentHistory(uid);
}

function collectMinerParamsPayload() {
  var params = {};
  document.querySelectorAll("[data-param-key]").forEach(function (input) {
    var key = input.getAttribute("data-param-key");
    var type = input.getAttribute("data-param-type");
    var raw = input.value;
    if (type === "boolean") params[key] = raw === "true";
    else if (type === "number")
      params[key] = raw.includes(".")
        ? parseFloat(raw)
        : parseInt(raw, 10);
    else params[key] = raw;
  });
  var syncIp = getMinerAxonIp(currentParamsUid);
  return {
    agent_index: getSelectedAgentIndex(),
    params: params,
    sync_ip: syncIp || undefined,
  };
}

function confirmMinerParams() {
  if (currentParamsUid == null) return;
  var confirmBtn = document.getElementById("minerParamsConfirmBtn");
  var pendingClose = false;
  confirmBtn.disabled = true;
  setMinerParamsStatus("Saving and syncing...", "");
  fetch("/api/miner-params/" + currentParamsUid, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(collectMinerParamsPayload()),
  })
    .then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) {
          var detail = body.detail;
          if (Array.isArray(detail)) {
            detail = detail
              .map(function (d) {
                return d.msg || JSON.stringify(d);
              })
              .join(", ");
          }
          throw new Error(detail || "HTTP " + r.status);
        }
        return body;
      });
    })
    .then(function (result) {
      currentParamsData = result.data;
      var info = extractAgentInfoFromData(result.data);
      if (info) minerAgentInfo[String(currentParamsUid)] = info;
      renderTable();
      loadAgentHistory(currentParamsUid);
      if (result.rsynced) {
        pendingClose = true;
        var syncMsg =
          "Saved and synced to " +
          result.miner_ip +
          ":" +
          result.remote_path;

        showNotifyAlert(
          "Params synced",
          "UID " +
            currentParamsUid +
            " saved and synced to " +
            result.miner_ip,
          "success",
          5000,
        );
        confirmBtn.disabled = true;
        setTimeout(function () {
          bootstrap.Modal.getOrCreateInstance(
            document.getElementById("minerParamsModal"),
          ).hide();
          currentParamsUid = null;
          currentParamsData = null;
          setMinerParamsStatus("", "");
          confirmBtn.disabled = false;
        }, 3000);
      } else {
        renderMinerParamsForm(result.data, currentParamsUid);
        var rsyncErr =
          "Saved locally, but rsync failed: " +
          (result.rsync_message || "unknown error");
        setMinerParamsStatus(rsyncErr, "error");
        showNotifyAlert("Sync failed", rsyncErr, "error", 7000);
      }
    })
    .catch(function (err) {
      setMinerParamsStatus("Save failed: " + err.message, "error");
      showNotifyAlert("Save failed", err.message, "error", 7000);
    })
    .finally(function () {
      if (!pendingClose) confirmBtn.disabled = false;
    });
}

function formatAgentHistoryTime(entry) {
  if (!entry) return "—";
  if (
    entry.switched_at &&
    entry.switched_at_tz === "Asia/Tokyo" &&
    entry.switched_at.indexOf("T") < 0
  ) {
    return entry.switched_at + " JST";
  }
  var iso = entry.switched_at_utc_iso || entry.switched_at;
  if (iso && iso.indexOf("T") >= 0) {
    var d = new Date(iso);
    if (!isNaN(d.getTime())) {
      return formatInstantInTokyo(d).title;
    }
  }
  return "—";
}

function formatAgentHistorySource(source) {
  if (source === "params_update") return "params";
  if (source === "auto_volume") return "Auto (hot books)";
  if (source === "manual") return "Manual";
  return source || "—";
}

function renderAgentHistoryBody(entries, loading) {
  var body = document.getElementById("minerAgentHistoryBody");
  if (!body) return;
  if (loading) {
    body.innerHTML =
      '<div class="params-empty" style="display:block">Loading history…</div>';
    return;
  }
  if (!entries || !entries.length) {
    body.innerHTML =
      '<div class="params-empty" style="display:block;color:#6c757d;font-size:0.82rem">No agent switches recorded yet.</div>';
    return;
  }
  var rows = entries
    .map(function (entry) {
      var syncBadge =
        entry.rsynced === false
          ? ' <span style="color:#dc3545;font-size:0.7rem">sync failed</span>'
          : "";
      var changeCell;
      if (entry.source === "params_update") {
        var agentName = escapeHtml(
          entry.new_agent || "#" + entry.new_agent_index,
        );
        var paramsData =
          entry.params || (entry.extra && entry.extra.params);
        var extra = paramsData
          ? Object.entries(paramsData)
              .map(function (kv) {
                return (
                  escapeHtml(kv[0]) + ": " + escapeHtml(String(kv[1]))
                );
              })
              .join(" · ")
          : "";
        changeCell =
          agentName +
          (extra
            ? ' <span style="color:#94a3b8;font-size:0.75rem">(' +
              extra +
              ")</span>"
            : "") +
          syncBadge;
      } else {
        var prev = escapeHtml(
          entry.previous_agent || "#" + entry.previous_agent_index,
        );
        var next = escapeHtml(
          entry.new_agent || "#" + entry.new_agent_index,
        );
        changeCell =
          prev +
          ' <span class="agent-history-arrow">→</span> ' +
          next +
          syncBadge;
      }
      return (
        "<tr>" +
        '<td style="white-space:nowrap">' +
        escapeHtml(formatAgentHistoryTime(entry)) +
        "</td>" +
        '<td><span class="agent-history-source">' +
        escapeHtml(formatAgentHistorySource(entry.source)) +
        "</span></td>" +
        "<td>" +
        changeCell +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
  body.innerHTML =
    '<table class="agent-history-table"><thead><tr>' +
    "<th>When</th><th>Source</th><th>Change</th></tr></thead><tbody>" +
    rows +
    "</tbody></table>";
}

function loadAgentHistory(uid) {
  renderAgentHistoryBody(null, true);
  return fetch("/api/agent-history/" + uid + "?limit=30")
    .then(function (r) {
      return r.ok ? r.json() : { entries: [] };
    })
    .then(function (data) {
      renderAgentHistoryBody(data.entries || []);
      return data;
    })
    .catch(function () {
      renderAgentHistoryBody([]);
    });
}

function showMinerParamsEmpty(uid, message) {
  currentParamsUid = null;
  currentParamsData = null;
  document.getElementById("minerParamsMeta").style.display = "none";
  document.getElementById("minerParamsFormWrap").style.display = "none";
  document.getElementById("minerParamsConfirmBtn").style.display = "none";
  var emptyEl = document.getElementById("minerParamsEmpty");
  emptyEl.style.display = "block";
  emptyEl.textContent = message;
  setMinerParamsStatus("", "");
}

function showMinerParams(uid) {
  var modalEl = document.getElementById("minerParamsModal");
  var titleEl = document.getElementById("minerParamsModalTitle");
  titleEl.textContent = "Miner Params — UID " + uid;
  showMinerParamsEmpty(uid, "Loading...");
  bootstrap.Modal.getOrCreateInstance(modalEl).show();
  loadAgentHistory(uid);
  fetch("/api/miner-params/" + uid)
    .then(function (r) {
      if (r.status === 404) {
        showMinerParamsEmpty(
          uid,
          "No params file found for UID " +
            uid +
            ". Add 79Miners/params/run_miner_" +
            uid +
            ".json to enable params for this miner.",
        );
        return null;
      }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (data) renderMinerParamsForm(data, uid);
    })
    .catch(function (err) {
      showMinerParamsEmpty(uid, "Failed to load params: " + err.message);
    });
}

function formatStatusValue(value) {
  return formatCompactNumber(value, 4);
}

function simIdMetaHtml(data) {
  return data.sim_id ? " · sim " + escapeHtml(data.sim_id) : "";
}

function renderUidStatusBody(data) {
  var bodyEl = document.getElementById("uidStatusBody");
  if (!data.active) {
    bodyEl.innerHTML =
      '<div class="uid-status-empty">No TAOS metrics reported for UID ' +
      data.uid +
      " (inactive or never scored)" +
      simIdMetaHtml(data) +
      ".</div>";
    return;
  }
  var summaryRows = (data.summary || [])
    .map(function (item) {
      var isPnl =
        /pnl/i.test(item.label || "") || /pnl/i.test(item.gauge || "");
      var valueHtml = isPnl
        ? formatPnlValueHtml(item.value)
        : escapeHtml(formatStatusValue(item.value));
      return (
        "<tr><td>" +
        escapeHtml(item.label) +
        "</td><td>" +
        valueHtml +
        "</td></tr>"
      );
    })
    .join("");
  var html =
    '<div class="uid-status-meta">TAOS metrics for subnet 79 · cached up to 60s' +
    simIdMetaHtml(data) +
    "</div>" +
    '<div class="uid-status-section">' +
    "<h6>Summary</h6>" +
    '<div class="params-form-wrap"><table class="params-form-table"><thead><tr><th>Metric</th><th>Value</th></tr></thead><tbody>' +
    summaryRows +
    "</tbody></table></div></div>";
  if (data.books && data.books.length) {
    var stats = data.book_stats || {};
    html +=
      '<div class="uid-status-section"><h6>Per-book breakdown</h6>' +
      '<div class="uid-status-meta">' +
      data.books.length +
      " books · " +
      (stats.without_weighted_kappa || 0) +
      " without weighted_kappa · " +
      "pnl books: " +
      (stats.positive_pnl_books || 0) +
      "+ / " +
      (stats.negative_pnl_books || 0) +
      "- · " +
      "total vol " +
      formatStatusValue(stats.total_daily_volume) +
      " · " +
      "total pnl " +
      formatStatusValue(stats.total_realized_pnl) +
      (stats.pnl_volume_ratio_pct != null
        ? " (" + Number(stats.pnl_volume_ratio_pct).toFixed(4) + "%)"
        : "") +
      '</div><div class="params-form-wrap"><table class="params-form-table"><thead><tr>' +
      "<th>Book</th><th>w_kappa</th><th>kappa</th><th>vol_day</th><th>real_pnl</th><th>mkr_fee%</th><th>tkr_fee%</th><th>act_f</th>" +
      "</tr></thead><tbody>" +
      data.books
        .map(function (book) {
          var volStyle = getBookVolumeCellStyle(book.daily_volume);
          var volTitle = volStyle
            ? "Book volume near " +
              formatCompactNumber(bookVolumeLimit, 0) +
              " limit"
            : "";
          return (
            "<tr><td>" +
            escapeHtml(book.book_id) +
            "</td>" +
            "<td>" +
            escapeHtml(formatStatusValue(book.weighted_kappa)) +
            "</td>" +
            "<td>" +
            escapeHtml(formatStatusValue(book.kappa)) +
            "</td>" +
            "<td" +
            (volStyle ? ' style="' + volStyle + '"' : "") +
            (volTitle ? ' title="' + escapeHtml(volTitle) + '"' : "") +
            ">" +
            escapeHtml(formatStatusValue(book.daily_volume)) +
            "</td>" +
            "<td>" +
            formatPnlValueHtml(book.realized_pnl) +
            "</td>" +
            "<td>" +
            escapeHtml(
              formatStatusValue((book.fees_maker_rate || 0) * 100),
            ) +
            "</td>" +
            "<td>" +
            escapeHtml(
              formatStatusValue((book.fees_taker_rate || 0) * 100),
            ) +
            "</td>" +
            "<td>" +
            escapeHtml(formatStatusValue(book.activity_factor)) +
            "</td></tr>"
          );
        })
        .join("") +
      "</tbody></table></div></div>";
  }
  bodyEl.innerHTML = html;
}

function loadUidStatus(uid, refresh) {
  var bodyEl = document.getElementById("uidStatusBody");
  bodyEl.innerHTML =
    '<div class="uid-status-empty">Loading TAOS metrics...</div>';
  var url =
    "/api/uid-status/" + uid + "?books=1" + (refresh ? "&refresh=1" : "");
  return fetch(url)
    .then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) {
          var detail = body.detail;
          if (Array.isArray(detail)) {
            detail = detail
              .map(function (d) {
                return d.msg || JSON.stringify(d);
              })
              .join(", ");
          }
          throw new Error(detail || "HTTP " + r.status);
        }
        return body;
      });
    })
    .then(function (data) {
      renderUidStatusBody(data);
      return data;
    })
    .catch(function (err) {
      bodyEl.innerHTML =
        '<div class="uid-status-empty">Failed to load UID status: ' +
        escapeHtml(err.message) +
        "</div>";
    });
}

function showUidStatus(uid) {
  if (getSubnetFromInput() !== 79) return;
  currentStatusUid = uid;
  document.getElementById("uidStatusModalTitle").textContent =
    "UID Status — UID " + uid;
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("uidStatusModal"),
  ).show();
  loadUidStatus(uid, false);
}

function refreshUidStatus() {
  if (currentStatusUid == null) return;
  loadUidStatus(currentStatusUid, true);
}

function formatSn88Money(value) {
  if (value == null || isNaN(Number(value))) return "—";
  return formatCompactNumber(Number(value), 2);
}

function formatSn88Pct(value, digits) {
  if (value == null || isNaN(Number(value))) return "—";
  digits = digits == null ? 2 : digits;
  return Number(value).toFixed(digits) + "%";
}

function formatSn88Number(value, digits) {
  if (value == null || isNaN(Number(value))) return "—";
  digits = digits == null ? 4 : digits;
  return Number(value).toFixed(digits);
}

function applyInvesting88Days() {
  if (getSubnetFromInput() !== 88 || !minersArray.length) return;
  minersArray.forEach(function (m) {
    var row = investing88DaysMap[String(m.uid)];
    m.days = row != null ? Number(row.days) : null;
  });
}

function loadInvesting88Days(forceRefresh) {
  if (getSubnetFromInput() !== 88) return Promise.resolve();
  var url = "/api/investing88/days";
  var needsRefresh =
    forceRefresh || Object.keys(investing88ScoresMap).length === 0;
  if (needsRefresh) url += "?refresh=1";
  return fetch(url)
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      investing88DaysMap = data.days || {};
      investing88ScoresMap = data.scores || {};
      investing88PrevScoresMap = data.prev_scores || {};
      applyInvesting88Days();
      return data;
    })
    .catch(function (err) {
      console.warn("Failed to load Investing88 days:", err);
      investing88DaysMap = {};
      investing88ScoresMap = {};
      investing88PrevScoresMap = {};
    });
}

function getSn88Score(uid, field) {
  var s = investing88ScoresMap[String(uid)];
  if (!s) return null;
  var v = s[field];
  return v == null || isNaN(Number(v)) ? null : Number(v);
}

// ---- SN88 per-field delta: day-over-day (today vs previous day) ----
// Both maps come from the backend (/api/investing88/days): current scores
// and the previous day's dated cache snapshot.
function getSn88Delta(uid, field) {
  var c = investing88ScoresMap[String(uid)];
  var p = investing88PrevScoresMap[String(uid)];
  if (!c || !p) return null;
  var cv = c[field],
    pv = p[field];
  if (cv == null || pv == null) return null;
  cv = Number(cv);
  pv = Number(pv);
  if (isNaN(cv) || isNaN(pv)) return null;
  return cv - pv;
}
function formatSn88DeltaMag(v, fmt) {
  switch (fmt) {
    case "int":
      return String(Math.round(v));
    case "clip":
      return v >= 1e6
        ? (v / 1e6).toFixed(2) + "M"
        : v >= 1e3
          ? (v / 1e3).toFixed(2) + "K"
          : v.toFixed(2);
    case "num4":
      return v.toFixed(4);
    case "num2":
      return v.toFixed(2);
    case "pct4":
      return v.toFixed(4) + "%";
    case "pct2":
    case "pct_red":
    case "pct_green":
      return v.toFixed(2) + "%";
    case "money":
      return formatSn88Money(v);
    default:
      return String(v);
  }
}
function renderSn88DeltaBadge(uid, field, fmt) {
  var d = getSn88Delta(uid, field);
  if (d == null || Math.abs(d) < 1e-9) return "";
  var up = d > 0;
  return (
    '<span class="sn88-delta ' +
    (up ? "sn88-delta-up" : "sn88-delta-down") +
    '">' +
    (up ? "▲" : "▼") +
    formatSn88DeltaMag(Math.abs(d), fmt) +
    "</span>"
  );
}

function renderSn88AssetCell(uid) {
  var asset = getSn88Score(uid, "asset");
  if (asset == null) return '<span class="coldkey-text">—</span>';
  var sym = asset === 0 ? "τ" : asset === 1 ? "$" : String(asset);
  return '<span class="metric-cell">' + sym + "</span>";
}

function renderSn88ScoreCell(uid, field, fmt) {
  var val = getSn88Score(uid, field);
  if (val == null) return '<span class="coldkey-text">—</span>';
  var str,
    cls = "metric-cell";
  if (fmt === "clip") {
    var n = Number(val);
    str =
      n >= 1e6
        ? (n / 1e6).toFixed(2) + "M"
        : n >= 1e3
          ? (n / 1e3).toFixed(2) + "K"
          : n.toFixed(2);
  } else if (fmt === "pct_red") {
    str = formatSn88Pct(val, 2);
    cls = "metric-cell text-danger";
  } else if (fmt === "pct_green") {
    str = formatSn88Pct(val, 2);
    cls = "metric-cell text-success";
  } else {
    str =
      fmt === "int"
        ? String(Math.round(Number(val)))
        : fmt === "pct2"
          ? formatSn88Pct(val, 2)
          : fmt === "pct4"
            ? formatSn88Pct(val, 4)
            : fmt === "money"
              ? formatSn88Money(val)
              : fmt === "num4"
                ? formatSn88Number(val, 4)
                : fmt === "num2"
                  ? formatSn88Number(val, 2)
                  : String(val);
  }
  return (
    '<span class="' +
    cls +
    '">' +
    escapeHtml(str) +
    "</span>" +
    renderSn88DeltaBadge(uid, field, fmt)
  );
}

function renderInvesting88Allocations(allocations) {
  var entries = Object.keys(allocations || {})
    .map(function (symbol) {
      return { symbol: symbol, weight: allocations[symbol] };
    })
    .sort(function (a, b) {
      return (b.weight || 0) - (a.weight || 0);
    });
  if (!entries.length)
    return '<div class="uid-status-empty">No allocation data</div>';
  return (
    '<div class="sn88-allocation-grid">' +
    entries
      .map(function (item) {
        return (
          '<div class="sn88-allocation-item">' +
          escapeHtml(item.symbol) +
          " · " +
          escapeHtml(formatSn88Pct((item.weight || 0) * 100, 2)) +
          "</div>"
        );
      })
      .join("") +
    "</div>"
  );
}

function renderInvesting88Miner(data) {
  var bodyEl = document.getElementById("investing88MinerBody");
  var linkEl = document.getElementById("investing88DashboardLink");
  if (!bodyEl) return;
  if (!data || !data.found) {
    bodyEl.innerHTML =
      '<div class="uid-status-empty">' +
      escapeHtml(
        (data && data.error) ||
          "Miner not found on Investing88 dashboard",
      ) +
      "</div>";
    if (linkEl) linkEl.style.display = "none";
    return;
  }
  var score = data.score || {};
  if (linkEl) {
    linkEl.href = data.dashboard_url || "#";
    linkEl.style.display = data.dashboard_url ? "" : "none";
  }
  var meta =
    "Score date " +
    escapeHtml(score.date || "—") +
    " · Rank #" +
    escapeHtml(String(score.rank != null ? score.rank : "—")) +
    " · " +
    escapeHtml(score.asset_label || "Asset —") +
    (score.days != null
      ? " · " + escapeHtml(String(score.days)) + " days live"
      : "");
  if (data.cache_updated_at_tokyo) {
    meta +=
      " · cached " + escapeHtml(data.cache_updated_at_tokyo) + " JST";
  } else if (data.fetched_at) {
    meta +=
      " · live " + escapeHtml(new Date(data.fetched_at).toLocaleString());
  }
  if (data.days_info && data.days_info.cash != null) {
    meta +=
      " · cash " +
      escapeHtml(formatSn88Pct(Number(data.days_info.cash) * 100, 2));
  }
  var scoreRows = [
    ["Score", formatSn88Number(score.score, 2)],
    ["APY", formatSn88Pct(score.apy, 2)],
    ["Value", formatSn88Money(score.value)],
    ["Swap", formatSn88Money(score.swap)],
    ["Daily %", formatSn88Pct(score.daily_percent, 4)],
    ["Yield %", formatSn88Pct(score.yield_percent, 2)],
    ["Risk %", formatSn88Pct(score.risk_percent, 2)],
    ["Odds %", formatSn88Pct(score.odds_percent, 2)],
    ["LSR", formatSn88Number(score.lsr, 4)],
    ["MAR", formatSn88Number(score.mar, 2)],
    [
      "Last rebalance (days ago)",
      score.last != null ? String(score.last) : "—",
    ],
  ];
  var html =
    '<div class="uid-status-meta">' +
    meta +
    "</div>" +
    '<div class="uid-status-section"><h6>Score summary</h6><div class="params-form-wrap"><table class="params-form-table"><tbody>' +
    scoreRows
      .map(function (row) {
        return (
          "<tr><td>" +
          escapeHtml(row[0]) +
          "</td><td>" +
          escapeHtml(row[1]) +
          "</td></tr>"
        );
      })
      .join("") +
    "</tbody></table></div></div>";

  var latest = data.latest_strategy;
  if (latest) {
    html +=
      '<div class="uid-status-section"><h6>Latest strategy · ' +
      escapeHtml(latest.date || "") +
      (latest.time ? " " + escapeHtml(latest.time) : "") +
      "</h6>" +
      '<div class="uid-status-meta">' +
      escapeHtml(latest.asset_label || "") +
      (latest.value != null
        ? " · value " + escapeHtml(formatSn88Money(latest.value))
        : "") +
      "</div>" +
      renderInvesting88Allocations(latest.allocations) +
      "</div>";
  }

  if (data.holdings && data.holdings.length) {
    html +=
      '<div class="uid-status-section"><h6>Holdings · ' +
      escapeHtml(data.holdings[0].date || score.date || "") +
      "</h6>" +
      '<div class="params-form-wrap"><table class="params-form-table"><thead><tr>' +
      "<th>Symbol</th><th>Close</th><th>Qty</th><th>Value</th><th>Weight</th>" +
      "</tr></thead><tbody>" +
      data.holdings
        .map(function (row) {
          return (
            "<tr><td>" +
            escapeHtml(row.symbol) +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Money(row.close)) +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Number(row.quantity, 2)) +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Money(row.value)) +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Pct((row.weight || 0) * 100, 2)) +
            "</td></tr>"
          );
        })
        .join("") +
      "</tbody></table></div></div>";
  }

  if (data.pnl_history && data.pnl_history.length) {
    html +=
      '<div class="uid-status-section"><h6>PNL history</h6>' +
      '<div class="history-chart-wrap"><canvas id="investing88PnlChart"></canvas></div>' +
      '<div class="params-form-wrap" style="margin-top:10px"><table class="params-form-table"><thead><tr>' +
      "<th>Date</th><th>Open</th><th>High</th><th>Low</th><th>Close</th>" +
      '</tr></thead><tbody id="investing88PnlTableBody"></tbody></table></div></div>';
  }

  if (data.strategy_history && data.strategy_history.length > 1) {
    html +=
      '<div class="uid-status-section"><h6>Strategy history</h6><div class="params-form-wrap"><table class="params-form-table"><thead><tr>' +
      "<th>Date</th><th>Time</th><th>Asset</th><th>Value</th><th>Top holdings</th>" +
      "</tr></thead><tbody>" +
      data.strategy_history
        .map(function (row) {
          var top = Object.keys(row.allocations || {})
            .map(function (symbol) {
              return { symbol: symbol, weight: row.allocations[symbol] };
            })
            .sort(function (a, b) {
              return (b.weight || 0) - (a.weight || 0);
            })
            .slice(0, 5)
            .map(function (item) {
              return (
                item.symbol +
                " " +
                formatSn88Pct((item.weight || 0) * 100, 1)
              );
            })
            .join(", ");
          return (
            "<tr><td>" +
            escapeHtml(row.date || "—") +
            "</td>" +
            "<td>" +
            escapeHtml(row.time || "—") +
            "</td>" +
            "<td>" +
            escapeHtml(row.asset_label || "—") +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Money(row.value)) +
            "</td>" +
            "<td>" +
            escapeHtml(top || "—") +
            "</td></tr>"
          );
        })
        .join("") +
      "</tbody></table></div></div>";
  }

  if (data.errors && data.errors.length) {
    html +=
      '<div class="uid-status-meta">Partial data: ' +
      escapeHtml(data.errors.join("; ")) +
      "</div>";
  }
  bodyEl.innerHTML = html;
  if (data.pnl_history && data.pnl_history.length) {
    var tableBody = document.getElementById("investing88PnlTableBody");
    if (tableBody) {
      tableBody.innerHTML = data.pnl_history
        .map(function (row) {
          return (
            "<tr><td>" +
            escapeHtml(row.date || "—") +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Money(row.open)) +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Money(row.high)) +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Money(row.low)) +
            "</td>" +
            "<td>" +
            escapeHtml(formatSn88Money(row.close)) +
            "</td></tr>"
          );
        })
        .join("");
    }
    renderInvesting88PnlChart(data.pnl_history);
  }
}

function destroyInvesting88Charts() {
  investing88Charts.forEach(function (c) {
    if (c) c.destroy();
  });
  investing88Charts = [];
}

function renderInvesting88PnlChart(pnlHistory) {
  destroyInvesting88Charts();
  var ctx = document.getElementById("investing88PnlChart");
  if (!ctx || !pnlHistory || !pnlHistory.length) return;

  var sorted = pnlHistory.slice().sort(function (a, b) {
    return String(a.date || "").localeCompare(String(b.date || ""));
  });
  var labels = sorted.map(function (row) {
    return row.date || "—";
  });
  var isDark = document.documentElement.classList.contains("theme-dark");
  var gridColor = isDark ? "rgba(148,163,184,0.2)" : "rgba(0,0,0,0.08)";
  var textColor = isDark ? "#cbd5e1" : "#495057";

  function moneyTick(v) {
    if (v == null || isNaN(v)) return "—";
    return formatSn88Money(v);
  }

  investing88Charts.push(
    new Chart(ctx, {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Close",
            data: sorted.map(function (row) {
              return row.close;
            }),
            borderColor: "#198754",
            backgroundColor: "rgba(25,135,84,0.15)",
            fill: true,
            tension: 0.2,
            pointRadius: sorted.length > 30 ? 0 : 2,
            spanGaps: true,
            order: 1,
          },
          {
            label: "High",
            data: sorted.map(function (row) {
              return row.high;
            }),
            borderColor: isDark ? "#86efac" : "#28a745",
            backgroundColor: "transparent",
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            borderDash: [4, 3],
            spanGaps: true,
            order: 2,
          },
          {
            label: "Low",
            data: sorted.map(function (row) {
              return row.low;
            }),
            borderColor: isDark ? "#fca5a5" : "#dc3545",
            backgroundColor: "transparent",
            fill: false,
            tension: 0.2,
            pointRadius: 0,
            borderDash: [4, 3],
            spanGaps: true,
            order: 3,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: {
            display: true,
            labels: { color: textColor, boxWidth: 12, padding: 10 },
          },
          tooltip: {
            callbacks: {
              label: function (context) {
                var row = sorted[context.dataIndex] || {};
                if (context.dataset.label === "Close") {
                  return [
                    "Close: " + moneyTick(row.close),
                    "Open: " + moneyTick(row.open),
                    "High: " + moneyTick(row.high),
                    "Low: " + moneyTick(row.low),
                  ];
                }
                return (
                  context.dataset.label +
                  ": " +
                  moneyTick(context.parsed.y)
                );
              },
            },
          },
        },
        scales: {
          x: {
            ticks: {
              maxRotation: 45,
              minRotation: 45,
              color: textColor,
              maxTicksLimit: 14,
            },
            grid: { color: gridColor },
          },
          y: {
            ticks: {
              color: textColor,
              callback: function (v) {
                return formatCompactNumber(v, 1);
              },
            },
            grid: { color: gridColor },
          },
        },
      },
    }),
  );
}

function loadInvesting88Miner(uid, rank, hotkey, refresh) {
  destroyInvesting88Charts();
  var bodyEl = document.getElementById("investing88MinerBody");
  if (bodyEl)
    bodyEl.innerHTML =
      '<div class="uid-status-empty">Loading Investing88 data...</div>';
  var params = [];
  if (hotkey) params.push("hotkey=" + encodeURIComponent(hotkey));
  if (rank != null) params.push("rank=" + encodeURIComponent(rank));
  if (refresh) params.push("refresh=1");
  var url =
    "/api/investing88/miner/" +
    uid +
    (params.length ? "?" + params.join("&") : "");
  return fetch(url)
    .then(function (r) {
      return r.json().then(function (body) {
        if (!r.ok) {
          var detail = body.detail;
          if (Array.isArray(detail)) {
            detail = detail
              .map(function (d) {
                return d.msg || JSON.stringify(d);
              })
              .join(", ");
          }
          throw new Error(detail || "HTTP " + r.status);
        }
        return body;
      });
    })
    .then(function (data) {
      renderInvesting88Miner(data);
    })
    .catch(function (err) {
      if (bodyEl) {
        bodyEl.innerHTML =
          '<div class="uid-status-empty">Failed to load Investing88 data: ' +
          escapeHtml(err.message) +
          "</div>";
      }
    });
}

function showInvesting88Miner(uid, rank, hotkey) {
  if (getSubnetFromInput() !== 88) return;
  investing88MinerContext = { uid: uid, rank: rank, hotkey: hotkey };
  document.getElementById("investing88MinerModalTitle").textContent =
    "Investing88 Miner — UID " +
    uid +
    (hotkey ? " · " + hotkey.substring(0, 8) + "..." : "");
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("investing88MinerModal"),
  ).show();
  loadInvesting88Miner(uid, rank, hotkey, false);
}

function refreshInvesting88Miner() {
  if (!investing88MinerContext) return;
  loadInvesting88Miner(
    investing88MinerContext.uid,
    investing88MinerContext.rank,
    investing88MinerContext.hotkey,
    true,
  );
}

function destroyHistoryCharts() {
  historyCharts.forEach(function (c) {
    if (c) c.destroy();
  });
  historyCharts = [];
}

function formatHistoryLabel(iso) {
  if (!iso) return "";
  var d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  var mo = d.getMonth() + 1;
  var dy = d.getDate();
  var hh = String(d.getHours()).padStart(2, "0");
  var mm = String(d.getMinutes()).padStart(2, "0");
  return mo + "/" + dy + " " + hh + ":" + mm;
}

function formatFeeRegimeLabel(iso) {
  if (!iso) return "";
  var d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  var mo = d.getMonth() + 1;
  var dy = d.getDate();
  var hh = String(d.getHours()).padStart(2, "0");
  var mm = String(d.getMinutes()).padStart(2, "0");
  return mo + "/" + dy + " " + hh + ":" + mm;
}

function renderHistoryCharts(points) {
  destroyHistoryCharts();
  var chartsWrap = document.getElementById("minerHistoryCharts");
  var emptyEl = document.getElementById("minerHistoryEmpty");
  var infoEl = document.getElementById("historyPointInfo");
  if (!points || points.length === 0) {
    chartsWrap.style.display = "none";
    emptyEl.style.display = "";
    if (infoEl) infoEl.textContent = "";
    return;
  }
  chartsWrap.style.display = "";
  emptyEl.style.display = "none";
  if (infoEl) infoEl.textContent = points.length + " snapshots";
  var labels = points.map(function (p) {
    return formatHistoryLabel(p.snapshot_at);
  });
  var isDark = document.documentElement.classList.contains("theme-dark");
  var gridColor = isDark ? "rgba(148,163,184,0.2)" : "rgba(0,0,0,0.08)";
  var textColor = isDark ? "#cbd5e1" : "#495057";
  var commonOptions = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: false } },
    scales: {
      x: {
        ticks: {
          maxRotation: 45,
          minRotation: 45,
          color: textColor,
          maxTicksLimit: 12,
        },
        grid: { color: gridColor },
      },
      y: {
        ticks: { color: textColor },
        grid: { color: gridColor },
      },
    },
  };
  var specs = [
    {
      id: "historyKappaChart",
      key: "kappa",
      color: "#0d6efd",
      label: "Kappa",
    },
    {
      id: "historyPnlChart",
      key: "real_pnl",
      color: "#198754",
      label: "Realized PNL",
    },
  ];
  specs.forEach(function (spec) {
    var ctx = document.getElementById(spec.id);
    if (!ctx) return;
    historyCharts.push(
      new Chart(ctx, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: spec.label,
              data: points.map(function (p) {
                return p[spec.key];
              }),
              borderColor: spec.color,
              backgroundColor: spec.color + "33",
              fill: true,
              tension: 0.2,
              pointRadius: points.length > 40 ? 0 : 2,
              spanGaps: true,
            },
          ],
        },
        options: commonOptions,
      }),
    );
  });
  var volCtx = document.getElementById("historyVolumeChart");
  if (volCtx) {
    var pr = points.length > 40 ? 0 : 2;
    historyCharts.push(
      new Chart(volCtx, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            {
              label: "Total",
              data: points.map(function (p) {
                return p.volume;
              }),
              borderColor: "#3b82f6",
              backgroundColor: "#3b82f633",
              fill: false,
              borderWidth: 2,
              tension: 0.2,
              pointRadius: pr,
              spanGaps: true,
            },
            {
              label: "Maker",
              data: points.map(function (p) {
                return p.vol_mkr;
              }),
              borderColor: "#c2773d",
              backgroundColor: "#c2773d33",
              fill: true,
              borderWidth: 2,
              tension: 0.2,
              pointRadius: pr,
              spanGaps: true,
            },
            {
              label: "Taker",
              data: points.map(function (p) {
                return p.vol_tkr;
              }),
              borderColor: "#eab308",
              backgroundColor: "#eab30833",
              fill: true,
              borderWidth: 2,
              tension: 0.2,
              pointRadius: pr,
              spanGaps: true,
            },
          ],
        },
        options: Object.assign({}, commonOptions, {
          plugins: {
            legend: {
              display: true,
              labels: { color: textColor, boxWidth: 12 },
            },
          },
        }),
      }),
    );
  }
}

function loadMinerHistory(uid) {
  var days =
    parseInt(document.getElementById("historyDaysSelect").value, 10) || 7;
  var bodyEl = document.getElementById("minerHistoryBody");
  var emptyEl = document.getElementById("minerHistoryEmpty");
  var chartsWrap = document.getElementById("minerHistoryCharts");
  chartsWrap.style.display = "none";
  emptyEl.style.display = "";
  emptyEl.textContent = "Loading history...";
  return fetch("/api/miner-history/" + uid + "?days=" + days)
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      renderHistoryCharts(data.points || []);
      return data;
    })
    .catch(function (err) {
      destroyHistoryCharts();
      chartsWrap.style.display = "none";
      emptyEl.style.display = "";
      emptyEl.textContent = "Failed to load history: " + err.message;
    });
}

function showMinerHistory(uid) {
  if (getSubnetFromInput() !== 79) return;
  currentHistoryUid = uid;
  document.getElementById("minerHistoryModalTitle").textContent =
    "Performance History — UID " + uid;
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("minerHistoryModal"),
  ).show();
  loadMinerHistory(uid);
}

function reloadMinerHistory() {
  if (currentHistoryUid == null) return;
  loadMinerHistory(currentHistoryUid);
}

document
  .getElementById("minerHistoryModal")
  .addEventListener("hidden.bs.modal", function () {
    destroyHistoryCharts();
    currentHistoryUid = null;
  });

document
  .getElementById("investing88MinerModal")
  .addEventListener("hidden.bs.modal", function () {
    destroyInvesting88Charts();
    investing88MinerContext = null;
  });

function processMetagraph(data) {
  // API: /api/metagraph-data (Bittensor Metagraph). Incentive = emission share %.
  var netuid = data.netuid != null ? data.netuid : getSubnetFromInput();
  var subnetInput = document.getElementById("subnetInput");
  if (
    subnetInput &&
    (subnetInput.value === "" ||
      parseInt(subnetInput.value, 10) !== netuid)
  )
    subnetInput.value = netuid;
  var chartTitleEl = document.getElementById("chartTitle");
  if (chartTitleEl)
    chartTitleEl.textContent =
      "Miner Incentive Distribution — Subnet " + netuid;
  var tableTitleEl = document.getElementById("tableTitle");
  if (tableTitleEl)
    tableTitleEl.textContent = "Miner Leaderboard — Subnet " + netuid;
  updateTableColumns();
  minersArray = [];
  var emissions = data.emissions || {};
  var hotkeyToUid = data.hotkeyToUid || {};
  var hotkeyToColdkey = data.hotkeyToColdkey || {};
  var isValidator = data.isValidator || {};
  var isImmune = data.isImmune || {};
  var minerAge = data.minerAge || {};
  var uidToIp = data.uidToIp || {};
  var totalEmission = 0;
  var minerEmissions = [];
  Object.keys(emissions).forEach(function (hotkey) {
    if (isValidator[hotkey]) return;
    var uid = hotkeyToUid[hotkey];
    if (uid === undefined) return;
    var em = emissions[hotkey];
    if (typeof em !== "number" || em < 0) em = 0;
    totalEmission += em;
    minerEmissions.push({
      uid: uid,
      hotkey: hotkey,
      coldkey: hotkeyToColdkey[hotkey] || null,
      emission: em,
      rank: 0,
      immune: isImmune[hotkey] || false,
      age: minerAge[hotkey] || 0,
      axonIp: uidToIp[String(uid)] || uidToIp[uid] || "",
    });
  });
  subnetTotalEmission = totalEmission;
  subnetEmissions[netuid] = totalEmission;
  if (data.alphaPriceTao != null)
    subnetAlphaPrices[netuid] = data.alphaPriceTao;
  if (data.currentBlock) currentBlock = data.currentBlock;
  var totalEmissionSafe = totalEmission > 0 ? totalEmission : 1;
  minerEmissions.forEach(function (m) {
    m.incentive = (m.emission / totalEmissionSafe) * 100;
  });
  minerEmissions.sort(function (a, b) {
    return b.incentive - a.incentive;
  });
  minerEmissions.forEach(function (m, i) {
    m.rank = i + 1;
  });
  minersArray = minerEmissions;
  filteredMiners = minersArray.slice();
}

// Count my miners by their agent side (maker / taker / other) and show
// the badge in the leaderboard header. Agent info only exists for SN79.
function updateAgentSideCounts(myMiners) {
  var el = document.getElementById("agentSideCounts");
  if (!el) return;
  var netuid = getSubnetFromInput();
  var counts = { maker: 0, taker: 0, other: 0 };
  myMiners.forEach(function (m) {
    var info = getMinerAgentInfo(m.uid);
    if (!info || !info.name) return;
    counts[getAgentSide(info.name)]++;
  });
  var hasData = counts.maker + counts.taker + counts.other > 0;
  if (netuid !== 79 || !hasData) {
    el.style.display = "none";
    return;
  }
  el.style.display = "flex";
  document.getElementById("makerAgentCount").textContent = counts.maker;
  document.getElementById("takerAgentCount").textContent = counts.taker;
  document.getElementById("otherAgentCount").textContent = counts.other;
}

function updateStatistics() {
  if (minersArray.length === 0) return;
  var total = minersArray.length;
  var sum = minersArray.reduce(function (s, m) {
    return s + m.incentive;
  }, 0);
  var top = Math.max.apply(
    null,
    minersArray.map(function (m) {
      return m.incentive;
    }),
  );
  var myMiners = myStatsMiners();
  var myCount = myMiners.length;
  var myTotal = myMiners.reduce(function (s, m) {
    return s + m.incentive;
  }, 0);
  var myAvg = myCount > 0 ? myTotal / myCount : 0;
  updateAgentSideCounts(myMiners);
  var immuneCount = minersArray.filter(function (m) {
    return m.immune;
  }).length;
  var showingNew = newColdkeysOnly();
  var myMinersLabelEl = document.getElementById("myMinersLabel");
  if (myMinersLabelEl)
    myMinersLabelEl.textContent = showingNew
      ? "My Miners (Testing)"
      : "My Miners";
  var myTaoPerDayLabelEl = document.getElementById("myTaoPerDayLabel");
  if (myTaoPerDayLabelEl)
    myTaoPerDayLabelEl.textContent = showingNew
      ? "My TAO/Day (Testing)"
      : "My TAO/Day";
  document.getElementById("totalMiners").textContent = total;
  document.getElementById("avgIncentive").textContent =
    (sum / total).toFixed(4) + "%";
  document.getElementById("topIncentive").textContent =
    top.toFixed(4) + "%";
  document.getElementById("immuneMinersCount").textContent = immuneCount;
  document.getElementById("myMinersCount").textContent = myCount;
  document.getElementById("myMinersAvgIncentive").textContent =
    myCount > 0
      ? "total " +
        myTotal.toFixed(4) +
        "% · avg " +
        myAvg.toFixed(4) +
        "%"
      : "";
  var netuid = getSubnetFromInput();
  var price = subnetAlphaPrices[netuid];
  var myTaoEl = document.getElementById("myTaoPerDay");
  if (myTaoEl) {
    if (price != null && myCount > 0) {
      var myDailyTao = myMiners.reduce(function (s, m) {
        return s + m.emission * price * 20;
      }, 0);
      myTaoEl.textContent = "τ " + myDailyTao.toFixed(4);
    } else {
      myTaoEl.textContent = price == null ? "…" : "-";
    }
  }
  var myEpochEl = document.getElementById("myTaoPerEpoch");
  var myEpochLabel = document.getElementById("myTaoEpochLabel");
  if (myEpochEl) {
    if (price != null && myCount > 0) {
      var myEpochTao = myMiners.reduce(function (s, m) {
        return s + m.emission * price;
      }, 0);
      var epochNum =
        currentBlock > 0 ? Math.floor(currentBlock / 360) : null;
      if (myEpochLabel && epochNum != null) {
        myEpochLabel.textContent =
          "My TAO/Epoch #" + epochNum + (showingNew ? " (Testing)" : "");
      }
      myEpochEl.textContent = "τ " + myEpochTao.toFixed(4);
    } else {
      myEpochEl.textContent = price == null ? "…" : "-";
    }
  }
  var myValueEl = document.getElementById("myMinersValue");
  if (myValueEl) {
    var totalValue = 0;
    var hasAnyValue = false;
    myMiners.forEach(function (m) {
      var v = getSn88Score(m.uid, "value");
      if (v != null) {
        totalValue += v;
        hasAnyValue = true;
      }
    });
    myValueEl.textContent = hasAnyValue
      ? formatSn88Money(totalValue)
      : "—";
  }
}

function renderSubnetEmissions() {
  var el = document.getElementById("myEmissionTotal");
  if (!el) return;
  var n = getSubnetFromInput();
  var em = subnetEmissions[n];
  var price = subnetAlphaPrices[n];
  var isDark = document.documentElement.classList.contains("theme-dark");
  var dim = isDark ? "#64748b" : "#adb5bd";
  if (em == null) {
    el.innerHTML = '<span style="color:' + dim + '">…</span>';
    return;
  }
  var epochTao = price != null ? em * price : null;
  var dailyTao = epochTao != null ? epochTao * 20 : null;
  var epochStr =
    epochTao != null ? "τ" + epochTao.toFixed(4) : "α" + em.toFixed(4);
  var dailyStr =
    dailyTao != null
      ? '<div style="font-size:0.72rem;color:' +
        dim +
        ';margin-top:2px">/day τ' +
        dailyTao.toFixed(2) +
        "</div>"
      : "";
  el.innerHTML =
    '<div style="font-size:1.4rem;font-weight:700;color:#17a2b8">' +
    epochStr +
    "</div>" +
    dailyStr;
}

// The header shows total non-validator emission for each of 71/79/88. This used
// to pull the *whole* metagraph per subnet — 144KB for 71 plus 156KB for 88 on
// every page load — and sum it client-side, for two numbers. The server already
// holds those metagraphs warm, so it does the sum and ships ~100 bytes.
function fetchSubnetEmissionBackground(netuid) {
  conditionalFetch("/api/subnet-emission?netuid=" + netuid)
    .then(function (data) {
      if (!data) return;
      subnetEmissions[netuid] = data.emission || 0;
      if (data.alphaPriceTao != null)
        subnetAlphaPrices[netuid] = data.alphaPriceTao;
      renderSubnetEmissions();
    })
    .catch(function () {});
}

function updateHighlightInfo() {
  var highlightInfoEl = document.getElementById("highlightInfo");
  var highlightCountEl = document.getElementById("highlightCount");

  if (highlightedColdkeys.length === 0) {
    highlightInfoEl.style.display = "none";
    return;
  }

  // Count miners in highlighted coldkeys
  var count = minersArray.filter(function (m) {
    return highlightedColdkeys.indexOf(m.coldkey) >= 0;
  }).length;

  highlightCountEl.textContent = count;
  highlightInfoEl.style.display = "block";
}

function clearHighlights() {
  highlightedColdkeys = [];
  renderChart();
  updateHighlightInfo();
}

function renderChart() {
  if (minersArray.length === 0) return;
  var ctx = document.getElementById("incentiveChart");
  if (chart) chart.destroy();

  // The incentive chart always shows every coldkey, including the New
  // Coldkeys — unaffected by the "New Coldkeys Only" toggle (that only
  // filters the leaderboard table below). Since NEW_COLDKEYS isn't part
  // of registeredColdkeys/highlightedColdkeys, they render with the
  // ordinary hash-based color like any other non-owned coldkey — no
  // special highlight.
  var chartMiners = minersArray;
  var coldkeyGroups = {};
  chartMiners.forEach(function (m) {
    var netuid = getSubnetFromInput();
    // Filter out miners with incentive > 4%
    // if (netuid!= 88 && m.incentive > 3) return;

    var ck = m.coldkey || "unknown";
    if (!coldkeyGroups[ck]) coldkeyGroups[ck] = [];
    coldkeyGroups[ck].push({ x: m.rank, y: m.incentive, miner: m });
  });

  // Create dataset for each coldkey (split by immune status for different point styles)
  var MY_CK_FIXED = {
    "5GREyxixYB8EJVHV1A9fQDh734tghXtt41A2MQG7VebqfQNe": "#3b82f6",
    "5DD45SAecKmhpyn9TykNoZWnc7mFgBrhvwmYNC6XSN2Ron6R": "#ef4444",
    "5Fnwngtt7bm5Nm8zTrXeELgG88M1cDBkJzaewdDghkLfgnXW": "#facc15",
  };
  var myColdkeyColors = {};
  // Spread registered coldkeys across the hue wheel via the golden angle
  // so each gets a distinct, vibrant color (instead of all defaulting to
  // white). The three keys in MY_CK_FIXED keep their pinned colors.
  var GOLDEN_ANGLE = 137.508;
  registeredColdkeys.forEach(function (ck, i) {
    if (MY_CK_FIXED[ck]) {
      myColdkeyColors[ck] = MY_CK_FIXED[ck];
      return;
    }
    var hue = Math.round((i * GOLDEN_ANGLE) % 360);
    var light = 52 + (i % 3) * 9; // cycle 52% / 61% / 70% for extra contrast
    myColdkeyColors[ck] = "hsl(" + hue + ", 75%, " + light + "%)";
  });

  var datasets = [];
  var coldkeyToIndex = {}; // Map coldkey to dataset index
  // Sort: registered first, then highlighted, then others
  var sortedColdkeys = Object.keys(coldkeyGroups).sort(function (a, b) {
    var aReg =
      registeredColdkeys.indexOf(a) >= 0
        ? 0
        : highlightedColdkeys.indexOf(a) >= 0
          ? 1
          : 2;
    var bReg =
      registeredColdkeys.indexOf(b) >= 0
        ? 0
        : highlightedColdkeys.indexOf(b) >= 0
          ? 1
          : 2;
    return aReg - bReg;
  });
  sortedColdkeys.forEach(function (coldkey) {
    var miners = coldkeyGroups[coldkey];
    var isRegistered = registeredColdkeys.indexOf(coldkey) >= 0;
    var isHighlighted = highlightedColdkeys.indexOf(coldkey) >= 0;
    var baseColor = isRegistered
      ? myColdkeyColors[coldkey]
      : isHighlighted
        ? "#facc15"
        : getColdkeyColor(coldkey);
    var bgColor = isRegistered
      ? baseColor
      : isHighlighted
        ? "#facc15"
        : hslToRgba(baseColor, 0.7);
    var borderColor = isRegistered
      ? "#ffffff"
      : isHighlighted
        ? "#ffffff"
        : hslToRgba(baseColor, 0.95);

    // Split miners into immune and non-immune
    var immuneMiners = miners.filter(function (p) {
      return p.miner.immune;
    });
    var nonImmuneMiners = miners.filter(function (p) {
      return !p.miner.immune;
    });

    // Calculate sizes
    var baseRadius = isHighlighted ? 7 : isRegistered ? 5 : 3;
    var immuneRadius = Math.round(baseRadius * 1.2);
    var baseHoverRadius = isHighlighted ? 10 : isRegistered ? 8 : 6;
    var immuneHoverRadius = Math.round(baseHoverRadius * 1.2);

    // Generate label
    var label =
      coldkey === "unknown"
        ? "Unknown Coldkey"
        : formatColdkeyLabel(coldkey);
    label += " (" + miners.length + ")";
    if (isHighlighted) label = "💡 " + label;
    else if (isRegistered) label = "⭐ " + label;

    coldkeyToIndex[coldkey] = datasets.length;

    // Add non-immune miners dataset (circles)
    if (nonImmuneMiners.length > 0) {
      datasets.push({
        label: label,
        data: nonImmuneMiners,
        backgroundColor: bgColor,
        borderColor: borderColor,
        borderWidth: isHighlighted ? 3 : isRegistered ? 1.5 : 1,
        pointRadius: baseRadius,
        pointHoverRadius: baseHoverRadius,
        pointStyle: "circle",
        coldkey: coldkey,
        isImmune: false,
      });
    }

    // Add immune miners dataset (rectangles)
    if (immuneMiners.length > 0) {
      datasets.push({
        label: nonImmuneMiners.length > 0 ? "" : label,
        data: immuneMiners,
        backgroundColor: bgColor,
        borderColor: borderColor,
        borderWidth: isHighlighted ? 3 : isRegistered ? 1.5 : 1,
        pointRadius: immuneRadius,
        pointHoverRadius: immuneHoverRadius,
        pointStyle: "rectRot",
        coldkey: coldkey,
        isImmune: true,
      });
    }
  });

  // Sort datasets: registered coldkeys first, then by total number of miners per coldkey
  datasets.sort(function (a, b) {
    // Check if coldkeys are registered (use label or coldkey property)
    var aIsRegistered = registeredColdkeys.indexOf(a.coldkey) >= 0;
    var bIsRegistered = registeredColdkeys.indexOf(b.coldkey) >= 0;
    if (aIsRegistered && !bIsRegistered) return -1;
    if (!aIsRegistered && bIsRegistered) return 1;

    // Count total miners for each coldkey
    var aTotal = datasets
      .filter(function (d) {
        return d.coldkey === a.coldkey;
      })
      .reduce(function (sum, d) {
        return sum + d.data.length;
      }, 0);
    var bTotal = datasets
      .filter(function (d) {
        return d.coldkey === b.coldkey;
      })
      .reduce(function (sum, d) {
        return sum + d.data.length;
      }, 0);
    return bTotal - aTotal;
  });

  chart = new Chart(ctx, {
    type: "scatter",
    data: { datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          display: true,
          position: "top",
          labels: {
            usePointStyle: true,
            padding: 10,
            font: { size: 10 },
            filter: function (legendItem, chartData) {
              return (
                chartData.datasets[legendItem.datasetIndex].label !== ""
              );
            },
            generateLabels: function (chart) {
              var defaults =
                Chart.defaults.plugins.legend.labels.generateLabels(
                  chart,
                );
              defaults.forEach(function (item) {
                var ds = chart.data.datasets[item.datasetIndex];
                if (!ds) return;
                var makerCount = 0,
                  takerCount = 0;
                (ds.data || []).forEach(function (p) {
                  try {
                    var n = getMinerAgentName(p.miner.uid);
                    if (/taker/i.test(n)) takerCount++;
                    else if (/maker/i.test(n) || /^RandomM[A-Z]/i.test(n))
                      makerCount++;
                  } catch (e) {}
                });
                if (makerCount + takerCount > 0) {
                  item.fillStyle =
                    takerCount > makerCount
                      ? "rgba(180,83,9,0.9)"
                      : "rgba(59,130,246,0.9)";
                }
              });
              return defaults.filter(function (item) {
                var ds = chart.data.datasets[item.datasetIndex];
                return ds && ds.label !== "";
              });
            },
          },
          onClick: function (e, legendItem, legend) {
            // Custom click handler - toggle highlight instead of hiding
            var index = legendItem.datasetIndex;
            var dataset = chart.data.datasets[index];
            var coldkey = dataset.coldkey;

            // Toggle highlight
            var idx = highlightedColdkeys.indexOf(coldkey);
            if (idx >= 0) {
              highlightedColdkeys.splice(idx, 1);
            } else {
              highlightedColdkeys.push(coldkey);
            }

            // Re-render chart with updated highlights
            renderChart();
            updateHighlightInfo();
          },
        },
        tooltip: {
          callbacks: {
            label: function (ctx) {
              var m = ctx.raw.miner;
              if (!m) return "Incentive: " + ctx.raw.y.toFixed(4) + "%";
              var lines = [
                "Rank #" + m.rank,
                "UID: " + m.uid,
                "Incentive: " + m.incentive.toFixed(4) + "%",
                "Hotkey: " + (m.hotkey || "").substring(0, 16) + "...",
                "Coldkey: " + formatColdkeyTooltip(m.coldkey),
              ];
              if (m.immune)
                lines.push(
                  "🛡️ Immune (" + m.age.toLocaleString() + " blocks)",
                );
              if (isMinerOwned(m)) lines.push("⭐ Your Miner");
              return lines;
            },
          },
        },
      },
      scales: {
        x: {
          title: { display: true, text: "Rank" },
          min: 0,
          max:
            getSubnetFromInput() === 79 ? minersArray.length : undefined,
          reverse: true,
          grid: { color: "rgba(0,0,0,0.06)" },
        },
        y: {
          beginAtZero: true,
          title: { display: true, text: "Incentive (%)" },
          grid: { color: "rgba(0,0,0,0.06)" },
        },
      },
    },
  });
}

function getTableColspan() {
  var n = getSubnetFromInput();
  if (n === 79) return 14;
  if (n === 88) return 16;
  return 5;
}

function updateTableColumns() {
  var netuid = getSubnetFromInput();
  var showSn79Columns = netuid === 79;
  var showSn88Columns = netuid === 88;
  if (!showSn79Columns) hideFeeRegimePanel();
  // The active-run bars used to be re-fetched from here. This is a column-layout
  // function that runs on every render, so it fired all three loaders — including
  // the 690ms RegBot SSH fan-out — an extra two times on every page load. The bars
  // are owned by startBackgroundPolling(); switching subnets is handled by their
  // own getSubnetFromInput() guards.
  var sn79Headers = [
    "serverColumnHeader",
    "regDateColumnHeader",
    "agentColumnHeader",
    "kappaColumnHeader",
    "pnlBooksColumnHeader",
    "realPnlColumnHeader",
    "unrlPnlColumnHeader",
    "volumeColumnHeader",
    "mkTkVolumeColumnHeader",
  ];
  sn79Headers.forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.style.display = showSn79Columns ? "" : "none";
  });
  var sn88Headers = [
    "sn88AssetColumnHeader",
    "sn88DaysColumnHeader",
    "sn88LastColumnHeader",
    "sn88ValueColumnHeader",
    "sn88ScoreColumnHeader",
    "sn88LsrColumnHeader",
    "sn88MarColumnHeader",
    "sn88RiskPctColumnHeader",
    "sn88OddsPctColumnHeader",
    "sn88DailyPctColumnHeader",
    "sn88YieldPctColumnHeader",
  ];
  sn88Headers.forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.style.display = showSn88Columns ? "" : "none";
  });
  var myValueCard = document.getElementById("myValueCard");
  if (myValueCard)
    myValueCard.style.display = showSn88Columns ? "" : "none";

  // "Testing Coldkeys" view option: subnet 79 only.
  var testingOption = document.getElementById("testingColdkeysOption");
  if (testingOption) testingOption.hidden = !showSn79Columns;
  if (!showSn79Columns) {
    var viewSelect = document.getElementById("minerViewMode");
    if (viewSelect && viewSelect.value === "testing") {
      viewSelect.value = "mine";
    }
  }
}

function getMinerAgentName(uid) {
  var info = getMinerAgentInfo(uid);
  return info ? info.name : "";
}

var AGENT_KEYWORD_COLORS = [
  { kw: "Harvester", color: "#a855f7" },
  { kw: "Ultimate", color: "#f97316" },
  { kw: "Futures", color: "#3b82f6" },
  { kw: "Adaptive", color: "#8b5cf6" },
  { kw: "Hybrid", color: "#10b981" },
  { kw: "Regime", color: "#14b8a6" },
  { kw: "Profit", color: "#22c55e" },
  { kw: "Gamma", color: "#f59e0b" },
  { kw: "Delta", color: "#06b6d4" },
  { kw: "Beta", color: "#84cc16" },
  { kw: "Taker", color: "#f43f5e" },
  { kw: "Maker", color: "#38bdf8" },
  { kw: "Toxicity", color: "#fb923c" },
  { kw: "Cross", color: "#e879f9" },
  { kw: "Eater", color: "#ff6b6b" },
  { kw: "Tune", color: "#94a3b8" },
  { kw: "DCA", color: "#34d399" },
  { kw: "OBB", color: "#fbbf24" },
];

function highlightAgentName(name) {
  if (name === "FreeAllAgent") return escapeHtml(name);
  var result = escapeHtml(name);
  AGENT_KEYWORD_COLORS.forEach(function (entry) {
    var escaped = escapeHtml(entry.kw);
    var re = new RegExp("(" + escaped + ")", "gi");
    result = result.replace(
      re,
      '<span style="color:' + entry.color + ';font-weight:700">$1</span>',
    );
  });
  return result;
}

function renderAgentLabel(uid) {
  var info = getMinerAgentInfo(uid);
  if (!info || !info.name) return "";
  var agentColor = getAgentColor(info.name, info.params);
  var paramsHtml = formatAgentParamsSummary(info.params, info.name);
  return (
    '<span class="agent-name-block agent-name-clickable" style="' +
    getAgentBlockStyle(info.name, info.params) +
    '" title="Click to edit params: ' +
    escapeHtml(info.name) +
    '" onclick="showMinerParams(' +
    uid +
    ')">' +
    '<span class="agent-name-label" style="color:' +
    agentColor +
    '">' +
    highlightAgentName(info.name) +
    "</span>" +
    (paramsHtml
      ? '<span class="agent-params-summary" style="color:' +
        agentColor +
        '">' +
        paramsHtml +
        "</span>"
      : "") +
    "</span>"
  );
}

function isTokyoRegDateIso(iso) {
  return (
    !!iso && (iso.indexOf("+09:00") >= 0 || iso.indexOf("+0900") >= 0)
  );
}

function splitRegDateString(regDate) {
  if (!regDate || regDate.indexOf(" ") < 0) return null;
  var parts = regDate.split(" ");
  return {
    datePart: parts[0],
    timePart: parts[1] ? parts[1].slice(0, 5) : "",
    title: regDate + " JST",
  };
}

function formatInstantInTokyo(date) {
  return {
    datePart: date.toLocaleDateString("en-CA", {
      timeZone: "Asia/Tokyo",
    }),
    timePart: date.toLocaleTimeString("en-GB", {
      timeZone: "Asia/Tokyo",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }),
    title:
      date
        .toLocaleString("sv-SE", {
          timeZone: "Asia/Tokyo",
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        })
        .replace("T", " ") + " JST",
  };
}

function parseRegDateInstant(metrics) {
  if (!metrics) return null;
  var iso = metrics.reg_date_utc_iso || metrics.reg_date_iso;
  if (iso) {
    var fromIso = new Date(iso);
    if (!isNaN(fromIso.getTime())) return fromIso;
  }
  if (metrics.reg_date && metrics.reg_date.indexOf(" ") >= 0) {
    var suffix =
      metrics.reg_date_tz === "Asia/Tokyo" || isTokyoRegDateIso(iso)
        ? "+09:00"
        : "Z";
    var fromLegacy = new Date(
      metrics.reg_date.replace(" ", "T") + suffix,
    );
    if (!isNaN(fromLegacy.getTime())) return fromLegacy;
  }
  return null;
}

function formatRegDateDisplay(metrics) {
  if (!metrics) return null;
  if (
    metrics.reg_date &&
    (metrics.reg_date_tz === "Asia/Tokyo" ||
      isTokyoRegDateIso(metrics.reg_date_iso))
  ) {
    return splitRegDateString(metrics.reg_date);
  }
  var instant = parseRegDateInstant(metrics);
  return instant ? formatInstantInTokyo(instant) : null;
}

function getRegDate(uid) {
  var formatted = formatRegDateDisplay(getUidMetrics(uid));
  return formatted ? formatted.datePart + " " + formatted.timePart : "";
}

function getRegDateSortValue(uid) {
  var m = getUidMetrics(uid);
  if (!m) return "";
  var instant = parseRegDateInstant(m);
  return instant
    ? instant.toISOString()
    : m.reg_date_utc_iso || m.reg_date_iso || m.reg_date || "";
}

function formatRegCostTao(tao) {
  if (tao == null || tao === "") return null;
  var n = Number(tao);
  if (!isFinite(n)) return null;
  if (n >= 1) return n.toFixed(3) + " TAO";
  if (n >= 0.01) return n.toFixed(4) + " TAO";
  return n.toFixed(6) + " TAO";
}

function renderSubnetRegCost(data, netuid) {
  var wrap = document.getElementById("subnetRegCost");
  var valueEl = document.getElementById("subnetRegCostValue");
  var labelEl = document.getElementById("subnetRegCostLabel");
  if (!wrap || !valueEl) return;
  netuid = netuid != null ? netuid : getSubnetFromInput();
  wrap.style.display = "";
  if (labelEl) labelEl.textContent = "SN" + netuid + " Reg cost";
  var formatted = data ? formatRegCostTao(data.tao) : null;
  valueEl.textContent = formatted || "—";
  if (data && data.block != null) {
    var title =
      "Subnet " + netuid + " registration cost at block " + data.block;
    if (data.rao != null)
      title += " · " + Number(data.rao).toLocaleString() + " rao";
    if (data.updated_at)
      title +=
        " · updated " + new Date(data.updated_at).toLocaleTimeString();
    wrap.title = title;
  } else {
    wrap.title = "";
  }
}

function loadSubnetRegCost(refresh) {
  var netuid = getSubnetFromInput();
  var url =
    "/api/reg-cost?netuid=" + netuid + (refresh ? "&refresh=1" : "");
  return fetch(url)
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (getSubnetFromInput() !== netuid) return;
      renderSubnetRegCost(data, netuid);
    })
    .catch(function (err) {
      console.warn("Failed to load subnet reg cost:", err);
      if (getSubnetFromInput() === netuid)
        renderSubnetRegCost(null, netuid);
    });
}

// ---- Alpha price sparkline (header) ----------------------------------
var alphaPriceChart = null;
var alphaPriceRange = "7d";
var alphaPriceData = null; // kept so a theme switch can repaint the chart

function formatAlphaPrice(v) {
  if (v == null) return "—";
  var n = Number(v);
  if (!isFinite(n)) return "—";
  if (n >= 1) return n.toFixed(4);
  if (n >= 0.01) return n.toFixed(5);
  return n.toFixed(6);
}

function alphaPriceDirection(changePct) {
  if (changePct == null || !isFinite(changePct)) return "flat";
  // Below this the delta renders as "0.00%", so calling it up or down
  // would contradict the number printed next to the caret.
  if (changePct > 0.005) return "up";
  if (changePct < -0.005) return "down";
  return "flat";
}

function alphaPriceLineColor(direction, isDark) {
  if (direction === "up") return "#0ca30c";
  if (direction === "down") return "#d03b3b";
  return isDark ? "#94a3b8" : "#6c757d";
}

function alphaPriceRgba(hex, alpha) {
  var n = parseInt(hex.slice(1), 16);
  return (
    "rgba(" +
    ((n >> 16) & 255) +
    "," +
    ((n >> 8) & 255) +
    "," +
    (n & 255) +
    "," +
    alpha +
    ")"
  );
}

function renderAlphaPriceChart(data) {
  var canvas = document.getElementById("alphaPriceChart");
  if (!canvas || typeof Chart === "undefined") return;
  if (alphaPriceChart) {
    alphaPriceChart.destroy();
    alphaPriceChart = null;
  }
  if (!data || !data.points || data.points.length < 2) return;

  var isDark = document.documentElement.classList.contains("theme-dark");
  // Prefer the change the chip is actually displaying (live headline vs window
  // start); falls back to the series' own figure before renderAlphaPrice has
  // run or when no live price is available.
  var line = alphaPriceLineColor(
    alphaPriceDirection(
      data._effective_change_pct != null
        ? data._effective_change_pct
        : data.change_pct
    ),
    isDark
  );
  var ctx = canvas.getContext("2d");
  var fill = ctx.createLinearGradient(0, 0, 0, 34);
  fill.addColorStop(0, alphaPriceRgba(line, 0.26));
  fill.addColorStop(1, alphaPriceRgba(line, 0.02));

  alphaPriceChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: data.points.map(function (p) {
        return p[0];
      }),
      datasets: [
        {
          data: data.points.map(function (p) {
            return p[1];
          }),
          borderColor: line,
          backgroundColor: fill,
          borderWidth: 2,
          fill: true,
          // No smoothing: a curve through hourly closes draws prices that
          // never traded, including false intermediate highs and lows.
          tension: 0,
          pointRadius: 0,
          pointHoverRadius: 3.5,
          pointHoverBackgroundColor: line,
          // 2px ring in the surface color so the hovered point stays
          // legible where it sits on top of the filled area.
          pointHoverBorderColor: isDark ? "#1e293b" : "#ffffff",
          pointHoverBorderWidth: 2,
          // Hit target far bigger than the mark — the sparkline is 34px tall.
          pointHitRadius: 14,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      layout: { padding: { top: 3, bottom: 3 } },
      // Nearest-x rather than nearest-point: hovering anywhere in the
      // column reads that hour, which is the crosshair behaviour.
      interaction: { mode: "index", intersect: false },
      scales: {
        x: { display: false },
        y: { display: false, grace: "6%" },
      },
      plugins: {
        // One series, already named by the chip's label.
        legend: { display: false },
        tooltip: {
          displayColors: false,
          backgroundColor: isDark
            ? "rgba(15,23,42,0.95)"
            : "rgba(33,37,41,0.95)",
          titleFont: { size: 11, weight: "normal" },
          bodyFont: { size: 12, family: "monospace" },
          padding: 8,
          callbacks: {
            title: function (items) {
              return items.length ? items[0].label : "";
            },
            label: function (item) {
              return formatAlphaPrice(item.parsed.y) + " τ";
            },
          },
        },
      },
    },
  });
}

function formatAlphaAge(hours) {
  if (hours == null || !isFinite(hours)) return "";
  if (hours < 1) return Math.max(1, Math.round(hours * 60)) + "m";
  if (hours < 48) return Math.round(hours) + "h";
  return Math.round(hours / 24) + "d";
}

function renderAlphaPriceAge(card, data) {
  var ageEl = document.getElementById("alphaPriceAge");
  var stale = !!data.stale;
  card.classList.toggle("is-stale", stale);
  if (!ageEl) return;
  if (!stale) {
    ageEl.style.display = "none";
    return;
  }
  ageEl.style.display = "";
  // Says "history", not "price": with the headline coming live off the chain,
  // an overdue feed no longer means the number is wrong — only that the line
  // under it stops earlier than it should.
  ageEl.innerHTML =
    '<i class="bi bi-exclamation-triangle-fill"></i>history ' +
    formatAlphaAge(data.age_hours) +
    " behind";
  ageEl.title =
    "The daily price history has not advanced since " +
    data.latest_candle +
    " UTC, which is past its usual once-a-day drop. The headline price is " +
    "unaffected — it is read live from the chain.";
}

// The sparkline's own end date. Shown whenever the headline is live, because
// that is exactly when the two can disagree and look broken.
function renderAlphaHistEnd(data, headlineIsLive) {
  var el = document.getElementById("alphaPriceHistEnd");
  if (!el) return;
  if (!headlineIsLive || !data.latest_candle) {
    el.style.display = "none";
    return;
  }
  el.style.display = "";
  // "08-11 23:00" from "2026-08-11 23:00" — the year is noise at this size.
  el.textContent = "→ " + String(data.latest_candle).slice(5);
  el.title =
    "The sparkline is a once-a-day feed and ends at " +
    data.latest_candle +
    " UTC. The price above it is live from the chain, so the two differ by " +
    "however much alpha has moved since.";
}

function renderAlphaPrice(data, netuid) {
  var card = document.getElementById("alphaPriceCard");
  var labelEl = document.getElementById("alphaPriceLabel");
  var valueEl = document.getElementById("alphaPriceValue");
  var deltaEl = document.getElementById("alphaPriceDelta");
  if (!card || !valueEl || !deltaEl) return;

  if (!data) {
    card.style.display = "none";
    if (alphaPriceChart) {
      alphaPriceChart.destroy();
      alphaPriceChart = null;
    }
    return;
  }

  card.style.display = "";
  if (labelEl) labelEl.textContent = "SN" + netuid + " α";

  // Headline comes from the chain, not from the series. The history feed ships
  // one batch a day, so its newest close is normally yesterday's — showing it
  // here meant the header was permanently ~a day behind the real price (3.2%
  // out when this was measured). processMetagraph() has already stored the live
  // price by the time this runs, and it refreshes on the same 5-minute cycle,
  // so the headline now moves on its own. The series still drives the shape of
  // the line and the high/low, which is what it is actually good for.
  var live = subnetAlphaPrices[netuid];
  var headlineIsLive = live != null && isFinite(Number(live));
  var shown = headlineIsLive ? Number(live) : data.current;
  valueEl.textContent = formatAlphaPrice(shown) + " τ";

  // Measure the change against whatever is on display, so the two agree. With a
  // live headline this reads "window start → now"; falling back to the series'
  // own change_pct would end the comparison at yesterday while the number above
  // it says now.
  var changePct = data.change_pct;
  if (headlineIsLive && data.first) changePct = ((shown - data.first) / data.first) * 100;
  // Stashed so the theme-repaint path (which re-renders from alphaPriceData)
  // colours the line by the same number the chip is showing.
  data._effective_change_pct = changePct;

  var dir = alphaPriceDirection(changePct);
  deltaEl.className = "alpha-price-delta " + dir;
  var icon =
    dir === "up"
      ? "bi-caret-up-fill"
      : dir === "down"
        ? "bi-caret-down-fill"
        : "bi-dash";
  var pct =
    changePct == null
      ? "—"
      : (changePct >= 0 ? "+" : "") + Number(changePct).toFixed(2) + "%";
  deltaEl.innerHTML = '<i class="bi ' + icon + '"></i>' + pct;

  renderAlphaPriceAge(card, data);
  renderAlphaHistEnd(data, headlineIsLive);

  // Two sources, named as such. The gap between them is normal and permanent,
  // so the tooltip has to explain it rather than leave it looking like drift.
  var title =
    (data.name ? data.name + " " : "") + "alpha price · " + data.range;
  title += headlineIsLive
    ? "\nprice " + formatAlphaPrice(shown) + " τ — live from chain"
    : "\nprice " + formatAlphaPrice(shown) + " τ — last close (no live price)";
  title +=
    "\nhistory: high " +
    formatAlphaPrice(data.high) +
    " τ · low " +
    formatAlphaPrice(data.low) +
    " τ";
  if (data.period_start && data.period_end)
    title += "\n" + data.period_start + " → " + data.period_end;
  if (data.latest_candle)
    title +=
      "\nnewest candle " +
      data.latest_candle +
      " UTC" +
      (data.age_hours != null
        ? " (" + formatAlphaAge(data.age_hours) + " behind; feed updates daily)"
        : "");
  // Deliberately labelled as the file, not the data: this is when the collector
  // last rewrote it, which keeps moving even while the series is frozen.
  if (data.updated_at)
    title +=
      "\nsource file rewritten " + new Date(data.updated_at).toLocaleString();
  card.title = title;

  renderAlphaPriceChart(data);
}

function loadAlphaPrice(range) {
  if (range) alphaPriceRange = range;
  var netuid = getSubnetFromInput();
  var url =
    "/api/alpha-price?netuid=" +
    netuid +
    "&range=" +
    encodeURIComponent(alphaPriceRange);
  return fetch(url)
    .then(function (r) {
      // 404 just means this subnet has no history file — hide the chip
      // rather than showing an error for it.
      if (r.status === 404) return null;
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      if (getSubnetFromInput() !== netuid) return;
      alphaPriceData = data;
      renderAlphaPrice(data, netuid);
    })
    .catch(function (err) {
      console.warn("Failed to load alpha price:", err);
      if (getSubnetFromInput() === netuid) {
        alphaPriceData = null;
        renderAlphaPrice(null, netuid);
      }
    });
}

function renderSimId(data) {
  var wrap = document.getElementById("subnetSimId");
  var valueEl = document.getElementById("subnetSimIdValue");
  if (!wrap || !valueEl) return;
  wrap.style.display = "";
  if (data && data.sim_id) {
    valueEl.textContent = data.sim_id;
    wrap.title = "Live TAOS simulation epoch for subnet 79";
  } else {
    valueEl.textContent = "—";
    wrap.title = "Could not determine current simulation";
  }
}

function hideSimId() {
  var wrap = document.getElementById("subnetSimId");
  if (wrap) wrap.style.display = "none";
}

function loadSimId() {
  return fetch("/api/sim-id")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      renderSimId(data);
    })
    .catch(function (err) {
      console.warn("Failed to load sim id:", err);
      renderSimId(null);
    });
}

function loadRegBotBalances() {
  // Fire-and-forget: refreshes the server-side balance/stake cache
  // file so it's current by the time Add Miner / Unstake / Multi-
  // Server Register are opened, without blocking the rest of the page
  // load on 8 servers' worth of chain queries.
  fetch("/api/regbot/balance/refresh", { method: "POST" }).catch(
    function (err) {
      console.warn("Failed to refresh RegBot balances:", err);
    },
  );
}

// The server used to ship this per UID alongside reg_block and the current
// block it was already sending. Same number, one subtraction.
function metricsBlocksSinceRegistration(metrics) {
  if (!metrics) return null;
  if (metrics.blocks_since_registration != null)
    return Number(metrics.blocks_since_registration);
  if (metrics.reg_block != null && currentBlock)
    return Math.max(0, currentBlock - Number(metrics.reg_block));
  return null;
}

function renderRegDateCell(uid) {
  var metrics = getUidMetrics(uid) || {};
  var formatted = formatRegDateDisplay(metrics);
  if (!formatted) return '<span class="metric-empty">—</span>';
  var title = formatted.title;
  var since = metricsBlocksSinceRegistration(metrics);
  if (since != null) {
    title += " · " + since.toLocaleString() + " blocks ago";
  }
  var html =
    '<span class="reg-date-cell" title="' +
    escapeHtml(title) +
    '">' +
    '<span class="reg-date-day">' +
    escapeHtml(formatted.datePart) +
    "</span>";
  if (formatted.timePart) {
    html +=
      '<span class="reg-date-time">' +
      escapeHtml(formatted.timePart) +
      " JST</span>";
  }
  html += "</span>";
  return html;
}

function getMetricSortValue(uid, key) {
  var val = getMetricValue(uid, key);
  return val == null ? -Infinity : val;
}

function getSortValue(m, column) {
  if (column === "coldkey") return (m.coldkey || "").toLowerCase();
  if (column === "agent") return getMinerAgentName(m.uid).toLowerCase();
  if (column === "server") return getMinerServerName(m.uid).toLowerCase();
  if (column === "reg_date") return getRegDateSortValue(m.uid);
  if (column === "hotkey") return (m.hotkey || "").toLowerCase();
  if (column === "immune") return m.immune ? 1 : 0;
  if (column === "kappa") return getMetricSortValue(m.uid, "kappa");
  if (column === "pnl_books_pos")
    return getMetricSortValue(m.uid, "pnl_books_pos");
  if (column === "real_pnl") return getMetricSortValue(m.uid, "real_pnl");
  if (column === "unrl_pnl") return getMetricSortValue(m.uid, "unrl_pnl");
  if (column === "volume") return getMetricSortValue(m.uid, "volume");
  if (column === "vol_mkr") return getMetricSortValue(m.uid, "vol_mkr");
  if (column === "hot_books") return getHotBookCount(m.uid);
  if (column === "days") return m.days != null ? m.days : -Infinity;
  if (column === "sn88_last") {
    var v88 = getSn88Score(m.uid, "last");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_value") {
    var v88 = getSn88Score(m.uid, "value");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_score") {
    var v88 = getSn88Score(m.uid, "score");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_lsr") {
    var v88 = getSn88Score(m.uid, "lsr");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_mar") {
    var v88 = getSn88Score(m.uid, "mar");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_risk_percent") {
    var v88 = getSn88Score(m.uid, "risk_percent");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_odds_percent") {
    var v88 = getSn88Score(m.uid, "odds_percent");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_daily_percent") {
    var v88 = getSn88Score(m.uid, "daily_percent");
    return v88 == null ? -Infinity : v88;
  }
  if (column === "sn88_yield_percent") {
    var v88 = getSn88Score(m.uid, "yield_percent");
    return v88 == null ? -Infinity : v88;
  }
  return m[column];
}

function renderTable() {
  updateTableColumns();
  var tbody = document.getElementById("minersTableBody");
  var colspan = getTableColspan();
  if (filteredMiners.length === 0) {
    tbody.innerHTML =
      '<tr><td colspan="' +
      colspan +
      '" style="text-align:center;padding:40px">No miners</td></tr>';
    return;
  }
  var sorted = filteredMiners.slice().sort(function (a, b) {
    var va = getSortValue(a, sortColumn);
    var vb = getSortValue(b, sortColumn);
    if (va < vb) return sortDirection === "asc" ? -1 : 1;
    if (va > vb) return sortDirection === "asc" ? 1 : -1;
    return 0;
  });
  var start = (currentPage - 1) * rowsPerPage;
  var pageData = sorted.slice(start, start + rowsPerPage);
  var netuid = getSubnetFromInput();
  var coldkeyCounts = {};
  minersArray.forEach(function (x) {
    if (x.coldkey)
      coldkeyCounts[x.coldkey] = (coldkeyCounts[x.coldkey] || 0) + 1;
  });
  tbody.innerHTML = pageData
    .map(function (m) {
      var owned = isMinerOwned(m);
      var hotkeyEsc = (m.hotkey || "")
        .replace(/\\/g, "\\\\")
        .replace(/'/g, "\\'");
      var coldkeyEsc = (m.coldkey || "N/A")
        .replace(/\\/g, "\\\\")
        .replace(/'/g, "\\'");
      var coldkeyShort = formatColdkeyLabel(m.coldkey);
      var coldkeyNick = getColdkeyNickname(m.coldkey);
      var coldkeyTitle =
        (coldkeyNick ? coldkeyNick + " · " : "") + coldkeyEsc;
      var coldkeyColor = getColdkeyColor(m.coldkey);
      var ckCount =
        m.coldkey && coldkeyCounts[m.coldkey] > 1
          ? ' <span class="ck-count-badge">' +
            coldkeyCounts[m.coldkey] +
            "</span>"
          : "";
      var coldkeyDisplay =
        '<span class="coldkey-color-dot" style="background:' +
        coldkeyColor +
        '"></span><span class="coldkey-text" title="' +
        coldkeyTitle +
        '">' +
        coldkeyShort +
        "</span>" +
        ckCount +
        (m.coldkey
          ? ' <i class="bi bi-clipboard copy-btn" onclick="copyToClipboard(\'' +
            coldkeyEsc +
            "', this)\"></i>"
          : "") +
        (netuid === 88
          ? ' <i class="bi bi-info-circle sn88-miner-info-btn" title="Investing88 miner info" onclick="event.stopPropagation(); showInvesting88Miner(' +
            m.uid +
            "," +
            m.rank +
            ",'" +
            hotkeyEsc +
            "')\"></i>"
          : "");
      var agentCell = netuid === 79 ? renderAgentLabel(m.uid) : "";
      var immuneIcon = m.immune
        ? ' <i class="bi bi-shield-fill immune-icon" title="Immune (' +
          m.age.toLocaleString() +
          ' blocks)"></i>'
        : "";
      var uidDisplay =
        netuid === 79
          ? '<span class="uid-status-link" title="View TAOS metrics" onclick="showUidStatus(' +
            m.uid +
            ')">' +
            m.uid +
            "</span>"
          : String(m.uid);
      var historyIcon =
        netuid === 79 && hasTrackedHistory(m.uid)
          ? ' <i class="bi bi-graph-up history-icon" title="Performance history" onclick="event.stopPropagation(); showMinerHistory(' +
            m.uid +
            ')"></i>'
          : "";
      var hotBookCount = netuid === 79 ? getHotBookCount(m.uid) : 0;
      var hotBookMetrics = getUidMetrics(m.uid) || {};
      var rowStyle = "";
      if (netuid === 79) {
        var agentName79 = getMinerAgentName(m.uid);
        var isDark =
          document.documentElement.classList.contains("theme-dark");
        if (/taker/i.test(agentName79)) {
          rowStyle = isDark
            ? "background:rgba(120,53,15,0.35)"
            : "background:rgba(180,100,40,0.12)";
        } else if (
          /maker/i.test(agentName79) ||
          /^RandomM[A-Z]/i.test(agentName79)
        ) {
          rowStyle = isDark
            ? "background:rgba(30,64,120,0.45)"
            : "background:rgba(59,130,246,0.1)";
        } else {
          rowStyle = isDark
            ? "background:rgba(255,255,255,0.04)"
            : "background:rgba(255,255,255,0.8)";
        }
        if (hotBookCount > 0)
          rowStyle = getHotBookRowAccent(
            hotBookCount,
            hotBookMetrics.book_count || 0,
          );
      } else if (hotBookCount > 0) {
        rowStyle = getHotBookRowAccent(
          hotBookCount,
          hotBookMetrics.book_count || 0,
        );
      }
      var rowAccent = rowStyle ? ' style="' + rowStyle + '"' : "";

      return (
        '<tr class="' +
        (owned ? "owned-row" : "") +
        '"' +
        rowAccent +
        ' title="Coldkey: ' +
        coldkeyEsc +
        (hotBookCount > 0
          ? " · " + hotBookCount + " book(s) near 500K limit"
          : "") +
        '">' +
        '<td class="rank-cell">#' +
        m.rank +
        "</td>" +
        '<td class="uid-cell">' +
        uidDisplay +
        historyIcon +
        immuneIcon +
        "</td>" +
        '<td class="hotkey-cell" title="' +
        hotkeyEsc +
        '">' +
        (m.hotkey
          ? m.hotkey.substring(0, 6) + "…" + m.hotkey.slice(-6)
          : "") +
        ' <i class="bi bi-clipboard copy-btn" onclick="copyToClipboard(\'' +
        hotkeyEsc +
        "', this)\"></i></td>" +
        '<td class="incentive-cell ' +
        (owned ? "owned" : "") +
        '">' +
        m.incentive.toFixed(4) +
        "%</td>" +
        "<td>" +
        coldkeyDisplay +
        "</td>" +
        (netuid === 88
          ? "<td>" + renderSn88AssetCell(m.uid) + "</td>"
          : "") +
        (netuid === 88
          ? '<td><span class="metric-cell">' +
            (m.days != null ? m.days : "—") +
            "</span></td>"
          : "") +
        (netuid === 79 ? "<td>" + renderServerCell(m) + "</td>" : "") +
        (netuid === 79
          ? "<td>" + renderRegDateCell(m.uid) + "</td>"
          : "") +
        (netuid === 79 ? "<td>" + agentCell + "</td>" : "") +
        (netuid === 79
          ? "<td>" + renderMetricCell(m.uid, "kappa") + "</td>"
          : "") +
        (netuid === 79
          ? "<td>" + renderPnlBooksCell(m.uid) + "</td>"
          : "") +
        (netuid === 79
          ? "<td>" + renderPnlMetricCell(m.uid, "real_pnl") + "</td>"
          : "") +
        (netuid === 79
          ? "<td>" + renderPnlMetricCell(m.uid, "unrl_pnl") + "</td>"
          : "") +
        (netuid === 79
          ? "<td>" + renderVolumeCell(m.uid) + "</td>"
          : "") +
        (netuid === 79
          ? "<td>" + renderMkTkVolumeCell(m.uid) + "</td>"
          : "") +
        (netuid === 88
          ? "<td>" + renderSn88ScoreCell(m.uid, "last", "int") + "</td>"
          : "") +
        (netuid === 88
          ? "<td>" + renderSn88ScoreCell(m.uid, "value", "clip") + "</td>"
          : "") +
        (netuid === 88
          ? "<td>" + renderSn88ScoreCell(m.uid, "score", "num2") + "</td>"
          : "") +
        (netuid === 88
          ? "<td>" + renderSn88ScoreCell(m.uid, "lsr", "num4") + "</td>"
          : "") +
        (netuid === 88
          ? "<td>" + renderSn88ScoreCell(m.uid, "mar", "num2") + "</td>"
          : "") +
        (netuid === 88
          ? "<td>" +
            renderSn88ScoreCell(m.uid, "risk_percent", "pct_red") +
            "</td>"
          : "") +
        (netuid === 88
          ? "<td>" +
            renderSn88ScoreCell(m.uid, "odds_percent", "pct_green") +
            "</td>"
          : "") +
        (netuid === 88
          ? "<td>" +
            renderSn88ScoreCell(m.uid, "daily_percent", "pct_green") +
            "</td>"
          : "") +
        (netuid === 88
          ? "<td>" +
            renderSn88ScoreCell(m.uid, "yield_percent", "pct2") +
            "</td>"
          : "") +
        "</tr>"
      );
    })
    .join("");
  var totalRows = sorted.length;
  var totalPages = Math.ceil(totalRows / rowsPerPage);
  document.getElementById("paginationInfo").textContent =
    "Showing " +
    (start + 1) +
    "-" +
    Math.min(start + rowsPerPage, totalRows) +
    " of " +
    totalRows;
  var btns =
    '<button onclick="changePage(' +
    (currentPage - 1) +
    ')" ' +
    (currentPage === 1 ? "disabled" : "") +
    ">Prev</button>";
  for (var i = 1; i <= Math.min(5, totalPages); i++) {
    var p = currentPage <= 3 ? i : currentPage - 3 + i;
    if (p <= totalPages)
      btns +=
        '<button onclick="changePage(' +
        p +
        ')" class="' +
        (p === currentPage ? "active" : "") +
        '">' +
        p +
        "</button>";
  }
  btns +=
    '<button onclick="changePage(' +
    (currentPage + 1) +
    ')" ' +
    (currentPage === totalPages ? "disabled" : "") +
    ">Next</button>";
  document.getElementById("paginationButtons").innerHTML = btns;
}

function sortTable(col) {
  if (sortColumn === col)
    sortDirection = sortDirection === "asc" ? "desc" : "asc";
  else {
    sortColumn = col;
    sortDirection = "asc";
  }
  currentPage = 1;
  renderTable();
}
function filterTable() {
  var q = document.getElementById("searchBox").value.toLowerCase();
  var showOnlyMine = getMinerViewMode() === "mine";

  filteredMiners = applyNewColdkeyFilter(minersArray).filter(
    function (m) {
      // Apply "My Miners Only" filter
      if (showOnlyMine && !isMinerOwned(m)) return false;

      // Apply search filter
      if (!q) return true;
      return (
        String(m.uid).indexOf(q) >= 0 ||
        (m.hotkey || "").toLowerCase().indexOf(q) >= 0 ||
        (m.coldkey || "").toLowerCase().indexOf(q) >= 0 ||
        (getColdkeyNickname(m.coldkey) || "").toLowerCase().indexOf(q) >=
          0 ||
        getMinerAgentName(m.uid).toLowerCase().indexOf(q) >= 0 ||
        getMinerServerName(m.uid).toLowerCase().indexOf(q) >= 0 ||
        (m.axonIp || "").toLowerCase().indexOf(q) >= 0 ||
        ((getMinerAgentInfo(m.uid) || {}).miner_ip || "")
          .toLowerCase()
          .indexOf(q) >= 0
      );
    },
  );

  currentPage = 1;
  renderTable();
}
function changePage(p) {
  var totalPages = Math.ceil(filteredMiners.length / rowsPerPage);
  if (p < 1 || p > totalPages) return;
  currentPage = p;
  renderTable();
}
function copyToClipboard(text, element) {
  if (!text) {
    showCopyTooltip(element, "Nothing to copy", "error");
    return;
  }

  if (!navigator.clipboard) {
    // Fallback for browsers without clipboard API
    var textArea = document.createElement("textarea");
    textArea.value = text;
    textArea.style.position = "fixed";
    textArea.style.left = "-999999px";
    document.body.appendChild(textArea);
    textArea.select();
    try {
      document.execCommand("copy");
      document.body.removeChild(textArea);
      showCopyTooltip(element, "Copied!", "success");
    } catch (err) {
      document.body.removeChild(textArea);
      showCopyTooltip(element, "Copy failed", "error");
    }
    return;
  }

  navigator.clipboard
    .writeText(text)
    .then(function () {
      showCopyTooltip(element, "Copied!", "success");
    })
    .catch(function (err) {
      console.error("Failed to copy:", err);
      showCopyTooltip(element, "Copy failed", "error");
    });
}

function showCopyTooltip(element, message, status) {
  // Change icon based on status
  var originalClass = element.className;
  if (status === "success") {
    element.className = "bi bi-check-circle-fill copy-btn";
    element.style.color = "#28a745";
  } else {
    element.className = "bi bi-x-circle-fill copy-btn";
    element.style.color = "#dc3545";
  }

  // Create and show tooltip
  var tooltip = document.createElement("div");
  tooltip.className = "copy-tooltip";
  tooltip.textContent = message;
  tooltip.style.cssText =
    "position:absolute;background:#000;color:#fff;padding:4px 8px;border-radius:4px;font-size:0.75rem;white-space:nowrap;z-index:10000;pointer-events:none;";

  var rect = element.getBoundingClientRect();
  tooltip.style.left = rect.left + rect.width / 2 + "px";
  tooltip.style.top = rect.top - 30 + "px";
  tooltip.style.transform = "translateX(-50%)";

  if (status === "success") {
    tooltip.style.background = "#28a745";
  } else {
    tooltip.style.background = "#dc3545";
  }

  document.body.appendChild(tooltip);

  // Reset after 1.5 seconds
  setTimeout(function () {
    element.className = originalClass;
    element.style.color = "";
    if (tooltip.parentNode) {
      document.body.removeChild(tooltip);
    }
  }, 1500);
}
function exportCSV() {
  if (minersArray.length === 0) {
    alert("No data");
    return;
  }
  var csv =
    "Rank,UID,Hotkey,Coldkey,Incentive,Immune,Age (blocks),Status\n";
  minersArray
    .slice()
    .sort(function (a, b) {
      return a.rank - b.rank;
    })
    .forEach(function (m) {
      var h = (m.hotkey || "").replace(/"/g, '""');
      var c = (m.coldkey || "N/A").replace(/"/g, '""');
      csv +=
        m.rank +
        "," +
        m.uid +
        ',"' +
        h +
        '","' +
        c +
        '",' +
        m.incentive.toFixed(6) +
        "," +
        (m.immune ? "Yes" : "No") +
        "," +
        m.age +
        "," +
        (isMinerOwned(m) ? "My Miner" : "Other") +
        "\n";
    });
  var blob = new Blob([csv], { type: "text/csv" });
  var a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download =
    "miners_incentive_" + new Date().toISOString().slice(0, 10) + ".csv";
  a.click();
  URL.revokeObjectURL(a.href);
}

// ---- Axon port band (creg79.py --port-prefix) ----
// Mirrors regbot.py's PORT_PREFIX_RE so a bad pattern is caught before a
// round trip; the server still re-validates (it's what feeds the shell).
var PORT_PREFIX_RE = /^[0-9X]{1,4}(,[0-9X]{1,4})*$/;
var PORT_PREFIX_HINT =
  "Port band must be digits with X as a wildcard digit, e.g. 79XX or " +
  "809X — comma-separate for multiple bands (809X,808X).";

function isValidPortPrefix(value) {
  return PORT_PREFIX_RE.test(value);
}

function defaultPortPrefixFor(serverName) {
  if (!regbotPresetsCache || !serverName) return "";
  var match = regbotPresetsCache.servers.filter(function (s) {
    return s.name === serverName;
  })[0];
  return (match && match.port_prefix) || "";
}

// Re-fills the band whenever the selected server changes. Overwriting is
// intended: the band is a property of the server, so a value left over
// from a previously-selected server is always wrong for the new one.
function applyAddMinerDefaultPortPrefix() {
  var el = document.getElementById("addMinerPortPrefix");
  if (el)
    el.value = defaultPortPrefixFor(
      document.getElementById("addMinerServer").value,
    );
}

// ---- Add Miner (RegBot) ----
var regbotPresetsCache = null;
var addMinerLogTimer = null;
var addMinerCurrentRun = null; // { server, run_id }
var addMinerLiveRegCost = null; // { tao, block } from the last fetch
var addMinerServerBalance = null; // { wallet, tao } from the last fetch
var addMinerLastKnownStatus = null; // "RUNNING" | "FINISHED" — tracked so we
// only notify on the transition into FINISHED, not on every poll
var addMinerKillPending = null; // { server, run_id } awaiting kill confirmation

function loadRegBotPresets() {
  if (regbotPresetsCache) return Promise.resolve(regbotPresetsCache);
  return fetch("/api/regbot/presets")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      regbotPresetsCache = data;
      return data;
    });
}

function populateAddMinerForm(data) {
  var serverSel = document.getElementById("addMinerServer");
  serverSel.innerHTML = data.servers
    .map(function (s) {
      var balText =
        s.balance_tao != null
          ? " — " + formatRegCostTao(s.balance_tao)
          : "";
      return (
        '<option value="' +
        s.name +
        '">' +
        s.name +
        " (" +
        s.wallet +
        ")" +
        balText +
        "</option>"
      );
    })
    .join("");

  var agentSel = document.getElementById("addMinerDispatchAgent");
  agentSel.innerHTML = data.dispatch_agents
    .map(function (a, i) {
      return (
        '<option value="' +
        i +
        '"' +
        (a.name === "WarriorAgent" ? " selected" : "") +
        ">" +
        escapeHtml(a.label) +
        "</option>"
      );
    })
    .join("");
}

function updateAddMinerEstimate() {
  var reglimit = parseFloat(
    document.getElementById("addMinerReglimit").value,
  );
  var maxCount = parseInt(
    document.getElementById("addMinerMaxCount").value,
    10,
  );
  var el = document.getElementById("addMinerEstimate");
  if (isNaN(reglimit) || isNaN(maxCount)) {
    el.textContent = "";
    return;
  }
  var serverSel = document.getElementById("addMinerServer");
  var serverLabel = serverSel.value
    ? serverSel.options[serverSel.selectedIndex].text
    : "the selected server";
  el.textContent =
    "Max spend: τ " +
    (reglimit * maxCount).toFixed(4) +
    " · up to " +
    maxCount +
    " hotkey(s) on " +
    serverLabel;
  renderRegCostWarning();
  renderServerBalanceWarning();
}

function resetAddMinerModal() {
  document.getElementById("addMinerFormSection").style.display = "";
  document.getElementById("addMinerStatusSection").style.display = "none";
  document.getElementById("addMinerRefreshLogBtn").style.display = "none";
  document.getElementById("addMinerKillBtn").style.display = "none";
  document.getElementById("addMinerSubmitBtn").style.display = "";
  document.getElementById("addMinerSubmitBtn").disabled = false;
  document.getElementById("addMinerFormError").style.display = "none";
  document.getElementById("addMinerLog").textContent = "";
  document.getElementById("addMinerSyncStatus").style.display = "none";
  document.getElementById("addMinerPortPrefix").value = "";
  addMinerCurrentRun = null;
  addMinerPendingParams = null;
  addMinerServerBalance = null;
  addMinerLastKnownStatus = null;
  addMinerKillPending = null;
  if (addMinerLogTimer) {
    clearInterval(addMinerLogTimer);
    addMinerLogTimer = null;
  }
}

function renderRegCostWarning() {
  var el = document.getElementById("addMinerLiveRegCost");
  if (!addMinerLiveRegCost) return;
  var reglimitEl = document.getElementById("addMinerReglimit");
  var current = parseFloat(reglimitEl.value);
  var underLimit = !isNaN(current) && current < addMinerLiveRegCost.tao;
  el.innerHTML =
    "Current reg cost: <strong>τ " +
    addMinerLiveRegCost.tao.toFixed(4) +
    "</strong> (block " +
    addMinerLiveRegCost.block +
    ")" +
    (underLimit
      ? ' <span class="text-danger">— your Reg Limit is below this; registration will not go through until price drops or you raise it</span>'
      : "");
}

function renderServerBalanceWarning() {
  var el = document.getElementById("addMinerServerBalance");
  if (!addMinerServerBalance) return;
  var reglimit = parseFloat(
    document.getElementById("addMinerReglimit").value,
  );
  var maxCount = parseInt(
    document.getElementById("addMinerMaxCount").value,
    10,
  );
  var maxSpend =
    !isNaN(reglimit) && !isNaN(maxCount) ? reglimit * maxCount : null;
  var insufficient =
    maxSpend != null && addMinerServerBalance.tao < maxSpend;
  el.innerHTML =
    "Wallet balance (" +
    addMinerServerBalance.wallet +
    "): <strong>τ " +
    addMinerServerBalance.tao.toFixed(4) +
    "</strong>" +
    (insufficient
      ? ' <span class="text-danger">— not enough to cover the max spend above</span>'
      : "");
}

function loadRegBotServerBalance() {
  var server = document.getElementById("addMinerServer").value;
  var el = document.getElementById("addMinerServerBalance");
  if (!server) {
    el.textContent = "";
    addMinerServerBalance = null;
    return;
  }
  el.textContent = "Loading wallet balance…";
  fetch("/api/regbot/balance?server=" + encodeURIComponent(server))
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (data) {
      addMinerServerBalance = { wallet: data.wallet, tao: data.tao };
      renderServerBalanceWarning();
    })
    .catch(function (err) {
      addMinerServerBalance = null;
      el.textContent = "Could not load wallet balance: " + err.message;
    });
}

function loadRegBotLiveRegCost() {
  var el = document.getElementById("addMinerLiveRegCost");
  el.textContent = "Loading current reg cost…";
  fetch("/api/reg-cost?netuid=79&refresh=1")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      addMinerLiveRegCost = { tao: data.tao, block: data.block };
      renderRegCostWarning();
    })
    .catch(function (err) {
      el.textContent = "Could not load current reg cost: " + err.message;
    });
}

function openAddMinerModal() {
  resetAddMinerModal();
  loadRegBotPresets()
    .then(function (data) {
      populateAddMinerForm(data);
      updateAddMinerEstimate();
      loadRegBotServerBalance();
      applyAddMinerDefaultPortPrefix();
    })
    .catch(function (err) {
      document.getElementById("addMinerFormError").style.display = "";
      document.getElementById("addMinerFormError").textContent =
        "Failed to load servers/agents: " + err.message;
    });
  loadRegBotLiveRegCost();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("addMinerModal"),
  ).show();
}

var addMinerPendingParams = null;

function submitAddMiner() {
  var server = document.getElementById("addMinerServer").value;
  var agentSel = document.getElementById("addMinerDispatchAgent");
  var agentIndex = agentSel.value;
  var agentEntry =
    agentIndex !== "" && regbotPresetsCache
      ? regbotPresetsCache.dispatch_agents[parseInt(agentIndex, 10)]
      : null;
  var reglimit = parseFloat(
    document.getElementById("addMinerReglimit").value,
  );
  var maxCount = parseInt(
    document.getElementById("addMinerMaxCount").value,
    10,
  );
  var errEl = document.getElementById("addMinerFormError");
  errEl.style.display = "none";

  if (!server || !agentEntry || isNaN(reglimit) || isNaN(maxCount)) {
    errEl.style.display = "";
    errEl.textContent = "Please fill in all fields.";
    return;
  }

  var portPrefix = document
    .getElementById("addMinerPortPrefix")
    .value.trim();
  if (portPrefix && !isValidPortPrefix(portPrefix)) {
    errEl.style.display = "";
    errEl.textContent = PORT_PREFIX_HINT;
    return;
  }

  var serverSel = document.getElementById("addMinerServer");
  var serverLabel = serverSel.options[serverSel.selectedIndex].text;
  addMinerPendingParams = {
    server: server,
    dispatchAgent: agentEntry.name,
    dispatchAgentId: agentEntry.agent_id,
    dispatchAgentLabel: agentEntry.label,
    reglimit: reglimit,
    maxCount: maxCount,
    portPrefix: portPrefix || null,
    serverLabel: serverLabel,
  };

  document.getElementById("regbotConfirmDetails").innerHTML =
    "<div><strong>Server:</strong> " +
    serverLabel +
    "</div>" +
    "<div><strong>Dispatch Agent:</strong> " +
    escapeHtml(agentEntry.label) +
    "</div>" +
    "<div><strong>Reg Limit:</strong> τ " +
    reglimit +
    " per hotkey</div>" +
    "<div><strong>Max Count:</strong> " +
    maxCount +
    " hotkey(s)</div>" +
    "<div><strong>Port Band:</strong> " +
    (portPrefix ? escapeHtml(portPrefix) : "server default") +
    "</div>" +
    "<div class='mt-2'><strong>Max spend: τ " +
    (reglimit * maxCount).toFixed(4) +
    "</strong></div>" +
    (addMinerServerBalance
      ? "<div>Wallet balance: τ " +
        addMinerServerBalance.tao.toFixed(4) +
        "</div>"
      : "");

  // Only one modal on screen at a time — stacking a second Bootstrap
  // modal on top of this one causes backdrop/z-index glitches. Hide
  // this one first; the confirm modal's "hidden" handler reopens it
  // (to the form on cancel, or to the status panel on confirm).
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("addMinerModal"),
  ).hide();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("regbotConfirmModal"),
  ).show();
}

// Fires whether the confirm modal closed via Cancel/X (addMinerPendingParams
// still set) or via Confirm (confirmAddMinerSubmit already cleared it).
document
  .getElementById("regbotConfirmModal")
  .addEventListener("hidden.bs.modal", function () {
    bootstrap.Modal.getOrCreateInstance(
      document.getElementById("addMinerModal"),
    ).show();
  });

function confirmAddMinerSubmit() {
  if (!addMinerPendingParams) return;
  var server = addMinerPendingParams.server;
  var dispatchAgent = addMinerPendingParams.dispatchAgent;
  var dispatchAgentId = addMinerPendingParams.dispatchAgentId;
  var reglimit = addMinerPendingParams.reglimit;
  var maxCount = addMinerPendingParams.maxCount;
  var portPrefix = addMinerPendingParams.portPrefix;
  addMinerPendingParams = null;

  var confirmModalEl = document.getElementById("regbotConfirmModal");
  var confirmModal = bootstrap.Modal.getInstance(confirmModalEl);
  if (confirmModal) confirmModal.hide();

  var errEl = document.getElementById("addMinerFormError");
  var submitBtn = document.getElementById("addMinerSubmitBtn");
  submitBtn.disabled = true;

  // Switch to the status view immediately (not inside the fetch's
  // .then) — the confirm modal's hide -> add-miner modal's reopen
  // happens on Bootstrap's own ~300ms transition timer, which can land
  // before this request finishes; reopening should show "Starting…",
  // not a stale, still-editable form.
  document.getElementById("addMinerFormSection").style.display = "none";
  document.getElementById("addMinerStatusSection").style.display = "";
  document.getElementById("addMinerStatusBadge").textContent =
    "Starting…";
  document.getElementById("addMinerStatusBadge").className =
    "badge bg-secondary";
  document.getElementById("addMinerStatusSummary").textContent = "";
  document.getElementById("addMinerLog").textContent = "";

  fetch("/api/regbot/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      server: server,
      dispatch_agent: dispatchAgent,
      dispatch_agent_id: dispatchAgentId,
      reglimit: reglimit,
      max_count: maxCount,
      port_prefix: portPrefix,
    }),
  })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (data) {
      addMinerCurrentRun = { server: data.server, run_id: data.run_id };
      addMinerLastKnownStatus = "RUNNING";
      document.getElementById("addMinerRefreshLogBtn").style.display = "";
      document.getElementById("addMinerKillBtn").style.display = "";
      submitBtn.style.display = "none";
      document.getElementById("addMinerStatusSummary").textContent =
        " " +
        data.server +
        " (" +
        data.wallet +
        ") · agent " +
        data.dispatch_agent +
        (data.dispatch_agent_id != null
          ? " (id " + data.dispatch_agent_id + ")"
          : "") +
        " · reglimit τ" +
        data.reglimit +
        " · max_count " +
        data.max_count +
        " · run " +
        data.run_id;
      refreshAddMinerLog();
      addMinerLogTimer = setInterval(refreshAddMinerLog, 4000);
    })
    .catch(function (err) {
      // Couldn't even start — revert to the form so the user can retry.
      submitBtn.disabled = false;
      document.getElementById("addMinerFormSection").style.display = "";
      document.getElementById("addMinerStatusSection").style.display =
        "none";
      errEl.style.display = "";
      errEl.textContent = "Failed to start: " + err.message;
    });
}

function refreshAddMinerLog() {
  if (!addMinerCurrentRun) return;
  fetch(
    "/api/regbot/log?server=" +
      encodeURIComponent(addMinerCurrentRun.server) +
      "&run_id=" +
      encodeURIComponent(addMinerCurrentRun.run_id),
  )
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (data) {
      var badge = document.getElementById("addMinerStatusBadge");
      badge.textContent = data.status;
      badge.className =
        "badge " +
        (data.status === "RUNNING" ? "bg-primary" : "bg-success");
      document.getElementById("addMinerLog").textContent =
        data.log || "(no output yet)";
      document.getElementById("addMinerKillBtn").style.display =
        data.status === "RUNNING" ? "" : "none";

      var syncEl = document.getElementById("addMinerSyncStatus");
      if (data.status === "RUNNING") {
        syncEl.style.display = "none";
      } else if (data.synced_uids && data.synced_uids.length) {
        syncEl.style.display = "";
        syncEl.className = "mb-2 text-success";
        syncEl.innerHTML =
          '<i class="bi bi-check-circle"></i> Params synced to this dashboard for UID(s): ' +
          data.synced_uids.join(", ") +
          " — visible in 79Miners/params/";
      } else if (data.sync_error) {
        syncEl.style.display = "";
        syncEl.className = "mb-2 text-warning";
        syncEl.innerHTML =
          '<i class="bi bi-exclamation-triangle"></i> Could not sync params file(s): ' +
          data.sync_error +
          " (click Refresh Log to retry)";
      } else {
        syncEl.style.display = "";
        syncEl.className = "mb-2 text-muted";
        syncEl.innerHTML =
          "No newly-registered UID detected in the log yet.";
      }

      if (
        data.status !== "RUNNING" &&
        addMinerLastKnownStatus === "RUNNING"
      ) {
        notifyRegBotFinished(addMinerCurrentRun, data);
      }
      addMinerLastKnownStatus = data.status;

      if (data.status !== "RUNNING" && addMinerLogTimer) {
        clearInterval(addMinerLogTimer);
        addMinerLogTimer = null;
        loadRegBotActiveRuns(true); // a run just finished — drop its pill promptly
      }
    })
    .catch(function (err) {
      document.getElementById("addMinerLog").textContent +=
        "\n[log fetch error: " + err.message + "]";
    });
}

function notifyRegBotFinished(run, data) {
  if (!run) return;
  var title = "RegBot finished — " + run.server;
  var body;
  var type = "success";
  if (data.synced_uids && data.synced_uids.length) {
    body =
      "Registered UID(s): " +
      data.synced_uids.join(", ") +
      " (" +
      run.run_id +
      ")";
  } else if (data.sync_error) {
    body =
      "Finished, but params sync failed: " +
      data.sync_error +
      " (" +
      run.run_id +
      ")";
    type = "error";
  } else {
    body = run.run_id + " has stopped (no new UID detected).";
  }
  showNotifyAlert(title, body, type, 10000);

  if (typeof Notification === "undefined") return;
  if (Notification.permission === "granted") {
    new Notification(title, { body: body });
  } else if (Notification.permission !== "denied") {
    Notification.requestPermission().then(function (perm) {
      if (perm === "granted") new Notification(title, { body: body });
    });
  }
}

function killAddMinerRun() {
  if (!addMinerCurrentRun) return;
  addMinerKillPending = addMinerCurrentRun;
  document.getElementById("regbotKillConfirmDetails").innerHTML =
    "<div><strong>Server:</strong> " +
    addMinerKillPending.server +
    "</div>" +
    "<div><strong>Run:</strong> " +
    escapeHtml(addMinerKillPending.run_id) +
    "</div>";

  // Same single-modal-at-a-time discipline as the registration confirm
  // flow: hide this modal first, the kill-confirm modal's "hidden"
  // handler reopens it (cancel or confirm — either way).
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("addMinerModal"),
  ).hide();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("regbotKillConfirmModal"),
  ).show();
}

document
  .getElementById("regbotKillConfirmModal")
  .addEventListener("hidden.bs.modal", function () {
    bootstrap.Modal.getOrCreateInstance(
      document.getElementById("addMinerModal"),
    ).show();
  });

function confirmKillAddMinerRun() {
  if (!addMinerKillPending) return;
  var run = addMinerKillPending;
  addMinerKillPending = null;

  var killConfirmModalEl = document.getElementById(
    "regbotKillConfirmModal",
  );
  var killConfirmModal = bootstrap.Modal.getInstance(killConfirmModalEl);
  if (killConfirmModal) killConfirmModal.hide();

  // Set the loading state synchronously (not inside the fetch's .then)
  // so it's already showing by the time the reopened add-miner modal
  // becomes visible, same reasoning as confirmAddMinerSubmit().
  var killBtn = document.getElementById("addMinerKillBtn");
  killBtn.disabled = true;
  killBtn.innerHTML =
    '<span class="spinner-border spinner-border-sm" role="status" ' +
    'aria-hidden="true"></span> Killing…';

  fetch("/api/regbot/kill", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ server: run.server, run_id: run.run_id }),
  })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function () {
      refreshAddMinerLog();
      loadRegBotActiveRuns(true); // the run is gone — rescan rather than wait 20s
    })
    .catch(function (err) {
      showNotifyAlert(
        "Failed to kill session",
        err.message,
        "error",
        8000,
      );
    })
    .finally(function () {
      killBtn.disabled = false;
      killBtn.innerHTML =
        '<i class="bi bi-stop-circle"></i> Kill Session';
    });
}

// "unstake_transfer" and "transfer" both move TAO off the coldkey and so
// both need a destination; "unstake" alone does not.
function modeNeedsDestination(mode) {
  return mode === "unstake_transfer" || mode === "transfer";
}

function modeLabel(mode) {
  if (mode === "unstake_transfer") return "Unstake & Transfer";
  if (mode === "transfer") return "Transfer only";
  return "Unstake only";
}

// ---- Unstake & Transfer ----
var unstakePresetsCache = null;
var unstakePendingRun = null; // { server, destination }
var unstakeLogTimer = null;
var unstakeCurrentRun = null; // { run_id, startedAt }
var unstakeLastKnownStatus = null; // "RUNNING" | "FINISHED"

function loadUnstakePresets() {
  if (unstakePresetsCache) return Promise.resolve(unstakePresetsCache);
  return fetch("/api/unstake/presets")
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (data) {
      unstakePresetsCache = data;
      return data;
    });
}

function populateUnstakeForm(data) {
  var sel = document.getElementById("unstakeServer");
  sel.innerHTML = data.servers
    .map(function (s) {
      var balText =
        s.balance_tao != null
          ? " — " + formatRegCostTao(s.balance_tao) + " free"
          : "";
      var stakedText =
        s.staked_tao != null
          ? " / " + formatRegCostTao(s.staked_tao) + " staked"
          : "";
      return (
        '<option value="' +
        s.name +
        '">' +
        s.name +
        " (" +
        s.wallet +
        ")" +
        balText +
        stakedText +
        "</option>"
      );
    })
    .join("");
  applyUnstakeDefaultDestination();
  updateUnstakeRunEnabled();
}

function resetUnstakeModal() {
  document.getElementById("unstakeFormSection").style.display = "";
  document.getElementById("unstakeResultSection").style.display = "none";
  document.getElementById("unstakeFormError").style.display = "none";
  document.getElementById("unstakePreviewWrap").style.display = "none";
  document.getElementById("unstakePreviewOutput").textContent = "";
  document.getElementById("unstakeMode").value = "unstake";
  document.getElementById("unstakeDestination").value = "";
  document.getElementById("unstakeAmount").value = "";
  document.getElementById("unstakeRunBtn").style.display = "";
  document.getElementById("unstakeProgressWrap").style.display = "none";
  document.getElementById("unstakeResultSummary").textContent = "";
  unstakePendingRun = null;
  unstakeCurrentRun = null;
  unstakeLastKnownStatus = null;
  if (unstakeLogTimer) {
    clearInterval(unstakeLogTimer);
    unstakeLogTimer = null;
  }
  applyUnstakeModeVisibility();
  updateUnstakeRunEnabled();
}

function openUnstakeModal() {
  resetUnstakeModal();
  loadUnstakePresets()
    .then(populateUnstakeForm)
    .catch(function (err) {
      var errEl = document.getElementById("unstakeFormError");
      errEl.style.display = "";
      errEl.textContent = "Failed to load servers: " + err.message;
    });
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("unstakeModal"),
  ).show();
}

// Each coldkey sweeps to its own destination by default (see
// DEFAULT_DESTINATIONS in unstake.py). Overwriting on server change is
// intended — a destination left over from another coldkey would send
// that wallet's TAO to the wrong place.
function applyUnstakeDefaultDestination() {
  var el = document.getElementById("unstakeDestination");
  if (!el || !unstakePresetsCache) return;
  var server = document.getElementById("unstakeServer").value;
  var match = unstakePresetsCache.servers.filter(function (s) {
    return s.name === server;
  })[0];
  el.value = (match && match.default_destination) || "";
}

function applyUnstakeModeVisibility() {
  var mode = document.getElementById("unstakeMode").value;
  var isTransfer = modeNeedsDestination(mode);
  document.getElementById("unstakeDestinationWrap").style.display =
    isTransfer ? "" : "none";
  // Amount is transfer-only: with unstake_transfer the point is to sweep
  // whatever the unstake just freed, which isn't known up front.
  document.getElementById("unstakeAmountWrap").style.display =
    mode === "transfer" ? "" : "none";
  var hint;
  if (mode === "transfer") {
    hint =
      "Sends TAO that is ALREADY free on this coldkey — no unstaking " +
      "happens. Leave Amount blank to send the whole free balance " +
      "(minus a small fee reserve). Real, irreversible TAO movement — " +
      "use Preview to see what would happen first.";
  } else if (mode === "unstake_transfer") {
    hint =
      "Unstakes every hotkey's stake above the dust threshold on " +
      "netuid 79 for this coldkey, then sweeps the resulting free " +
      "balance to this address. Real, irreversible TAO movement — " +
      "use Preview to see what would happen first.";
  } else {
    hint =
      "Unstakes every hotkey's stake above the dust threshold on " +
      "netuid 79 for this coldkey. The freed TAO stays as free " +
      "balance on this same coldkey — nothing is sent anywhere. " +
      "Real, irreversible on-chain action — use Preview to see what " +
      "would happen first.";
  }
  document.getElementById("unstakeModeHint").textContent = hint;
}

// Blank = sweep everything (unstake_env.py's own default). Anything
// present must be a positive number.
function unstakeAmountValue() {
  var raw = (document.getElementById("unstakeAmount").value || "").trim();
  if (!raw) return null;
  var n = parseFloat(raw);
  return isNaN(n) ? NaN : n;
}

function onUnstakeModeChanged() {
  applyUnstakeModeVisibility();
  onUnstakeInputsChanged();
}

function updateUnstakeRunEnabled() {
  // Run is enabled as soon as the form is complete for the selected
  // mode — a server, and (for unstake_transfer) a destination. Preview
  // is optional, not a gate: use it to sanity-check first if you want,
  // but it's not required before Run.
  var server = document.getElementById("unstakeServer").value;
  var mode = document.getElementById("unstakeMode").value;
  var destination = document
    .getElementById("unstakeDestination")
    .value.trim();
  var ready = !!server && (mode === "unstake" || !!destination);
  document.getElementById("unstakeRunBtn").disabled = !ready;
}

function onUnstakeInputsChanged() {
  updateUnstakeRunEnabled();
}

function previewUnstake() {
  var server = document.getElementById("unstakeServer").value;
  var mode = document.getElementById("unstakeMode").value;
  var destination = document
    .getElementById("unstakeDestination")
    .value.trim();
  var errEl = document.getElementById("unstakeFormError");
  errEl.style.display = "none";

  if (!server || (modeNeedsDestination(mode) && !destination)) {
    errEl.style.display = "";
    errEl.textContent = modeNeedsDestination(mode)
      ? "Please select a server and enter a destination address."
      : "Please select a server.";
    return;
  }

  var btn = document.getElementById("unstakePreviewBtn");
  btn.disabled = true;
  btn.innerHTML =
    '<span class="spinner-border spinner-border-sm" role="status" ' +
    'aria-hidden="true"></span> Previewing…';

  fetch("/api/unstake/preview", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      server: server,
      mode: mode,
      destination: modeNeedsDestination(mode) ? destination : null,
      amount: mode === "transfer" ? unstakeAmountValue() : null,
    }),
  })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (data) {
      document.getElementById("unstakePreviewWrap").style.display = "";
      document.getElementById("unstakePreviewOutput").textContent =
        data.output;
    })
    .catch(function (err) {
      errEl.style.display = "";
      errEl.textContent = "Preview failed: " + err.message;
    })
    .finally(function () {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-eye"></i> Preview (Dry Run)';
    });
}

function submitUnstake() {
  var server = document.getElementById("unstakeServer").value;
  var mode = document.getElementById("unstakeMode").value;
  var destination = document
    .getElementById("unstakeDestination")
    .value.trim();

  var serverSel = document.getElementById("unstakeServer");
  var serverLabel = serverSel.options[serverSel.selectedIndex].text;
  var isTransfer = modeNeedsDestination(mode);
  var amount = mode === "transfer" ? unstakeAmountValue() : null;
  var errEl = document.getElementById("unstakeFormError");
  errEl.style.display = "none";
  if (amount !== null && (isNaN(amount) || amount <= 0)) {
    errEl.style.display = "";
    errEl.textContent =
      "Amount must be a positive number of TAO (or blank to send everything).";
    return;
  }
  unstakePendingRun = {
    server: server,
    mode: mode,
    destination: isTransfer ? destination : null,
    amount: amount,
  };

  document.getElementById("unstakeConfirmDetails").innerHTML =
    "<div><strong>Server:</strong> " +
    escapeHtml(serverLabel) +
    "</div>" +
    "<div><strong>Action:</strong> " +
    escapeHtml(modeLabel(mode)) +
    "</div>" +
    (isTransfer
      ? "<div><strong>Destination:</strong> " +
        escapeHtml(destination) +
        "</div>"
      : "") +
    (mode === "transfer"
      ? "<div><strong>Amount:</strong> " +
        (amount === null
          ? "whole free balance"
          : "τ " + escapeHtml(String(amount))) +
        "</div>"
      : "");

  // Same single-modal-at-a-time discipline as the RegBot confirm flows.
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("unstakeModal"),
  ).hide();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("unstakeConfirmModal"),
  ).show();
}

document
  .getElementById("unstakeConfirmModal")
  .addEventListener("hidden.bs.modal", function () {
    bootstrap.Modal.getOrCreateInstance(
      document.getElementById("unstakeModal"),
    ).show();
  });

function confirmUnstakeRun() {
  if (!unstakePendingRun) return;
  var run = unstakePendingRun;
  unstakePendingRun = null;

  var confirmModalEl = document.getElementById("unstakeConfirmModal");
  var confirmModal = bootstrap.Modal.getInstance(confirmModalEl);
  if (confirmModal) confirmModal.hide();

  // Switch to the result view synchronously, same reasoning as
  // confirmAddMinerSubmit() — the reopened modal shouldn't show a
  // stale, still-editable form while the request is in flight.
  document.getElementById("unstakeFormSection").style.display = "none";
  document.getElementById("unstakeResultSection").style.display = "";
  document.getElementById("unstakeResultBadge").textContent = "Starting…";
  document.getElementById("unstakeResultBadge").className =
    "badge bg-secondary";
  document.getElementById("unstakeResultSummary").textContent = "";
  document.getElementById("unstakeProgressWrap").style.display = "none";
  document.getElementById("unstakeResultOutput").textContent = "";
  document.getElementById("unstakeRunBtn").style.display = "none";

  fetch("/api/unstake/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      server: run.server,
      mode: run.mode,
      destination: run.destination,
      amount: run.amount,
    }),
  })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (data) {
      unstakeCurrentRun = { run_id: data.run_id, startedAt: Date.now() };
      unstakeLastKnownStatus = "RUNNING";
      refreshUnstakeLog();
      unstakeLogTimer = setInterval(refreshUnstakeLog, 4000);
      loadUnstakeActiveRuns(); // show its pill immediately, not after up to 20s
    })
    .catch(function (err) {
      var badge = document.getElementById("unstakeResultBadge");
      badge.textContent = "ERROR";
      badge.className = "badge bg-danger";
      document.getElementById("unstakeResultOutput").textContent =
        err.message;
      showNotifyAlert("Unstake failed", err.message, "error", 10000);
    });
}

function formatElapsed(ms) {
  var secs = Math.round(ms / 1000);
  return secs >= 60
    ? Math.floor(secs / 60) + "m " + (secs % 60) + "s"
    : secs + "s";
}

function refreshUnstakeLog() {
  if (!unstakeCurrentRun) return;
  fetch(
    "/api/unstake/log?run_id=" +
      encodeURIComponent(unstakeCurrentRun.run_id),
  )
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (data) {
      var badge = document.getElementById("unstakeResultBadge");
      var progressWrap = document.getElementById("unstakeProgressWrap");
      var summaryEl = document.getElementById("unstakeResultSummary");
      var elapsedText = formatElapsed(
        Date.now() - unstakeCurrentRun.startedAt,
      );

      if (data.status === "RUNNING") {
        badge.textContent = "RUNNING";
        badge.className = "badge bg-primary";
        progressWrap.style.display = "";
        summaryEl.textContent =
          "Elapsed: " +
          elapsedText +
          " — hotkeys are unstaked one at a time on-chain, this can take a while.";
      } else {
        progressWrap.style.display = "none";
        var label = data.timed_out
          ? "TIMEOUT"
          : data.success
            ? "SUCCESS"
            : "FAILED";
        badge.textContent = label;
        badge.className =
          "badge " + (data.success ? "bg-success" : "bg-danger");
        summaryEl.textContent = "Finished after " + elapsedText;
      }
      document.getElementById("unstakeResultOutput").textContent =
        data.output || "(no output yet)";

      if (
        data.status !== "RUNNING" &&
        unstakeLastKnownStatus === "RUNNING"
      ) {
        showNotifyAlert(
          modeLabel(data.mode) +
            (data.success
              ? " complete"
              : data.timed_out
                ? " timed out"
                : " failed"),
          modeNeedsDestination(data.mode)
            ? data.server + " → " + data.destination
            : data.server,
          data.success ? "success" : "error",
          10000,
        );
      }
      unstakeLastKnownStatus = data.status;

      if (data.status !== "RUNNING" && unstakeLogTimer) {
        clearInterval(unstakeLogTimer);
        unstakeLogTimer = null;
        loadUnstakeActiveRuns(); // this run just finished — drop its pill promptly
      }
    })
    .catch(function (err) {
      document.getElementById("unstakeResultOutput").textContent +=
        "\n[log fetch error: " + err.message + "]";
    });
}

// ---- Multi-Server Register (strict round-robin batch) ----
var multiRegisterPollTimer = null;
var multiRegisterActiveBatchId = null;
var multiRegisterPendingStart = null; // { server_configs, dispatch_agent, dispatch_agent_id }

var MULTI_REGISTER_STATUS_ICON = {
  PENDING: '<i class="bi bi-circle text-muted"></i>',
  RUNNING: '<i class="bi bi-arrow-repeat text-primary"></i>',
  DONE: '<i class="bi bi-check-circle-fill text-success"></i>',
  CANCELLED: '<i class="bi bi-x-circle text-danger"></i>',
  LAUNCH_FAILED:
    '<i class="bi bi-exclamation-triangle-fill text-danger"></i>',
};

function resetMultiRegisterModal() {
  document.getElementById("multiRegisterFormSection").style.display = "";
  document.getElementById("multiRegisterProgressSection").style.display =
    "none";
  document.getElementById("multiRegisterFormError").style.display =
    "none";
  document.getElementById("multiRegisterEstimate").textContent = "";
  document.getElementById("multiRegisterStartBtn").style.display = "";
  document.getElementById("multiRegisterCancelBtn").style.display =
    "none";
  document.getElementById("multiRegisterNewBatchBtn").style.display =
    "none";
  document.getElementById("multiRegisterActiveNote").style.display =
    "none";
  multiRegisterPendingStart = null;
  multiRegisterActiveBatchId = null;
  if (multiRegisterPollTimer) {
    clearInterval(multiRegisterPollTimer);
    multiRegisterPollTimer = null;
  }
}

function openMultiRegisterModal() {
  resetMultiRegisterModal();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiRegisterModal"),
  ).show();
  loadRegBotPresets()
    .then(populateMultiRegisterForm)
    .catch(function (err) {
      var errEl = document.getElementById("multiRegisterFormError");
      errEl.style.display = "";
      errEl.textContent = "Failed to load servers: " + err.message;
    });
  // Several batches can run at once, so opening this modal must NOT
  // hijack the view with whichever one happens to be running — that
  // would make it impossible to start another. Show the form, and list
  // anything already running as chips you can click to watch.
  loadMultiRegisterActiveNote();
}

function loadMultiRegisterActiveNote() {
  var note = document.getElementById("multiRegisterActiveNote");
  return fetch("/api/regbot/batch/active")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      var running = data.batches || (data.batch ? [data.batch] : []);
      if (!running.length) {
        note.style.display = "none";
        return running;
      }
      note.style.display = "";
      note.innerHTML =
        '<i class="bi bi-broadcast"></i> <strong>' +
        running.length +
        " batch" +
        (running.length === 1 ? "" : "es") +
        " already running</strong> — starting another runs them at the " +
        "same time, including on shared servers. " +
        running
          .map(function (b) {
            var done = b.queue.filter(function (i) {
              return i.status === "DONE";
            }).length;
            return (
              '<a href="#" class="badge bg-primary text-decoration-none ms-1" ' +
              "onclick=\"viewMultiRegisterBatch('" +
              b.batch_id +
              "'); return false;\">" +
              escapeHtml(b.batch_id.replace("batch_", "")) +
              " (" +
              done +
              "/" +
              b.queue.length +
              ")</a>"
            );
          })
          .join("");
      return running;
    })
    .catch(function () {
      note.style.display = "none";
      return [];
    });
}

function viewMultiRegisterBatch(batchId) {
  fetch("/api/regbot/batch/" + encodeURIComponent(batchId))
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(showMultiRegisterProgress)
    .catch(function (err) {
      showNotifyAlert("Could not load batch", err.message, "error", 6000);
    });
}

// Back to the form from a progress view, so another batch can be started
// without closing the modal. The batch being watched keeps running —
// this only stops this client polling it.
function backToMultiRegisterForm() {
  if (multiRegisterPollTimer) {
    clearInterval(multiRegisterPollTimer);
    multiRegisterPollTimer = null;
  }
  multiRegisterActiveBatchId = null;
  document.getElementById("multiRegisterFormSection").style.display = "";
  document.getElementById("multiRegisterProgressSection").style.display =
    "none";
  document.getElementById("multiRegisterStartBtn").style.display = "";
  document.getElementById("multiRegisterNewBatchBtn").style.display =
    "none";
  document.getElementById("multiRegisterCancelBtn").style.display =
    "none";
  loadMultiRegisterActiveNote();
}

function populateMultiRegisterForm(data) {
  var agentSel = document.getElementById("multiRegisterDispatchAgent");
  agentSel.innerHTML = data.dispatch_agents
    .map(function (a, i) {
      return (
        '<option value="' +
        i +
        '"' +
        (a.name === "WarriorAgent" ? " selected" : "") +
        ">" +
        escapeHtml(a.label) +
        "</option>"
      );
    })
    .join("");

  var rows = document.getElementById("multiRegisterServerRows");
  rows.innerHTML = data.servers
    .map(function (s) {
      var balText =
        s.balance_tao != null ? formatRegCostTao(s.balance_tao) : "—";
      return (
        "<tr>" +
        "<td>" +
        escapeHtml(s.name) +
        " (" +
        escapeHtml(s.wallet) +
        ")</td>" +
        "<td>" +
        balText +
        "</td>" +
        "<td>" +
        '<input type="number" ' +
        'class="form-control form-control-sm multi-register-count-input" ' +
        'min="0" step="1" value="0" data-server="' +
        escapeHtml(s.name) +
        '" oninput="updateMultiRegisterEstimate()" />' +
        "</td>" +
        "<td>" +
        '<input type="number" ' +
        'class="form-control form-control-sm multi-register-reglimit-input" ' +
        'min="0.0001" step="0.0001" placeholder="0.1051" data-server="' +
        escapeHtml(s.name) +
        '" oninput="updateMultiRegisterEstimate()" />' +
        "</td>" +
        "<td>" +
        '<input type="text" ' +
        'class="form-control form-control-sm multi-register-portprefix-input" ' +
        'placeholder="79XX" value="' +
        escapeHtml(s.port_prefix || "") +
        '" data-server="' +
        escapeHtml(s.name) +
        '" oninput="updateMultiRegisterEstimate()" />' +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
  updateMultiRegisterEstimate();
}

function multiRegisterServerConfigs() {
  var countInputs = document.querySelectorAll(
    ".multi-register-count-input",
  );
  var reglimitByServer = {};
  document
    .querySelectorAll(".multi-register-reglimit-input")
    .forEach(function (el) {
      reglimitByServer[el.dataset.server] = parseFloat(el.value);
    });
  var portPrefixByServer = {};
  document
    .querySelectorAll(".multi-register-portprefix-input")
    .forEach(function (el) {
      portPrefixByServer[el.dataset.server] = el.value.trim();
    });
  var configs = [];
  countInputs.forEach(function (el) {
    var n = parseInt(el.value, 10);
    if (!isNaN(n) && n > 0) {
      configs.push({
        server: el.dataset.server,
        count: n,
        reglimit: reglimitByServer[el.dataset.server],
        port_prefix: portPrefixByServer[el.dataset.server] || null,
      });
    }
  });
  return configs;
}

function updateMultiRegisterEstimate() {
  var configs = multiRegisterServerConfigs();
  var totalAttempts = configs.reduce(function (sum, c) {
    return sum + c.count;
  }, 0);
  var el = document.getElementById("multiRegisterEstimate");
  if (totalAttempts === 0) {
    el.textContent = "";
    return;
  }
  var maxSpend = configs.reduce(function (sum, c) {
    return sum + (isNaN(c.reglimit) ? 0 : c.reglimit * c.count);
  }, 0);
  var text =
    totalAttempts +
    " hotkey(s) total across " +
    configs.length +
    " server(s) · max spend τ " +
    maxSpend.toFixed(4);
  el.textContent = text;
}

function submitMultiRegisterBatch() {
  var errEl = document.getElementById("multiRegisterFormError");
  errEl.style.display = "none";

  var agentSel = document.getElementById("multiRegisterDispatchAgent");
  var agentIndex = agentSel.value;
  var agentEntry =
    agentIndex !== "" && regbotPresetsCache
      ? regbotPresetsCache.dispatch_agents[parseInt(agentIndex, 10)]
      : null;
  var serverConfigs = multiRegisterServerConfigs();

  if (!agentEntry) {
    errEl.style.display = "";
    errEl.textContent = "Please select a dispatch agent.";
    return;
  }
  if (serverConfigs.length === 0) {
    errEl.style.display = "";
    errEl.textContent =
      "Enter a count greater than 0 for at least one server.";
    return;
  }
  var missingReglimit = serverConfigs.filter(function (c) {
    return isNaN(c.reglimit);
  });
  if (missingReglimit.length > 0) {
    errEl.style.display = "";
    errEl.textContent =
      "Enter a reg limit for: " +
      missingReglimit
        .map(function (c) {
          return c.server;
        })
        .join(", ");
    return;
  }
  var badPortPrefix = serverConfigs.filter(function (c) {
    return c.port_prefix && !isValidPortPrefix(c.port_prefix);
  });
  if (badPortPrefix.length > 0) {
    errEl.style.display = "";
    errEl.textContent =
      "Invalid port band for " +
      badPortPrefix
        .map(function (c) {
          return c.server;
        })
        .join(", ") +
      ". " +
      PORT_PREFIX_HINT;
    return;
  }

  var totalAttempts = serverConfigs.reduce(function (sum, c) {
    return sum + c.count;
  }, 0);
  var totalMaxSpend = serverConfigs.reduce(function (sum, c) {
    return sum + c.reglimit * c.count;
  }, 0);
  multiRegisterPendingStart = {
    server_configs: serverConfigs,
    dispatch_agent: agentEntry.name,
    dispatch_agent_id: agentEntry.agent_id,
  };

  document.getElementById("multiRegisterConfirmDetails").innerHTML =
    "<div><strong>Dispatch Agent:</strong> " +
    escapeHtml(agentEntry.label) +
    "</div>" +
    "<div><strong>Servers:</strong> " +
    serverConfigs
      .map(function (c) {
        return (
          escapeHtml(c.server) +
          " ×" +
          c.count +
          " @ τ" +
          c.reglimit +
          (c.port_prefix ? " · " + escapeHtml(c.port_prefix) : "")
        );
      })
      .join(", ") +
    "</div>" +
    "<div class='mt-2'><strong>Total: " +
    totalAttempts +
    " hotkey(s) · max spend τ " +
    totalMaxSpend.toFixed(4) +
    "</strong></div>";

  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiRegisterModal"),
  ).hide();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiRegisterConfirmModal"),
  ).show();
}

document
  .getElementById("multiRegisterConfirmModal")
  .addEventListener("hidden.bs.modal", function () {
    bootstrap.Modal.getOrCreateInstance(
      document.getElementById("multiRegisterModal"),
    ).show();
  });

document
  .getElementById("multiRegisterModal")
  .addEventListener("hidden.bs.modal", function () {
    // The batch is a server-side background task independent of this
    // modal — closing it just stops polling from the client side,
    // it does NOT cancel the batch. Reopening the modal resumes
    // showing progress (see openMultiRegisterModal).
    if (multiRegisterPollTimer) {
      clearInterval(multiRegisterPollTimer);
      multiRegisterPollTimer = null;
    }
  });

function confirmMultiRegisterStart() {
  if (!multiRegisterPendingStart) return;
  var params = multiRegisterPendingStart;
  multiRegisterPendingStart = null;

  var confirmModalEl = document.getElementById(
    "multiRegisterConfirmModal",
  );
  var confirmModal = bootstrap.Modal.getInstance(confirmModalEl);
  if (confirmModal) confirmModal.hide();

  document.getElementById("multiRegisterFormSection").style.display =
    "none";
  document.getElementById("multiRegisterProgressSection").style.display =
    "";
  document.getElementById("multiRegisterStartBtn").style.display = "none";
  document.getElementById("multiRegisterStatusBadge").textContent =
    "Starting…";
  document.getElementById("multiRegisterStatusBadge").className =
    "badge bg-secondary";
  document.getElementById("multiRegisterQueueRows").innerHTML = "";

  fetch("/api/regbot/batch/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      servers: params.server_configs,
      dispatch_agent: params.dispatch_agent,
      dispatch_agent_id: params.dispatch_agent_id,
    }),
  })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (batch) {
      showMultiRegisterProgress(batch);
      loadBatchActiveRuns(); // show its pill immediately, not up to 15s later
    })
    .catch(function (err) {
      document.getElementById("multiRegisterFormSection").style.display =
        "";
      document.getElementById(
        "multiRegisterProgressSection",
      ).style.display = "none";
      document.getElementById("multiRegisterStartBtn").style.display = "";
      var errEl = document.getElementById("multiRegisterFormError");
      errEl.style.display = "";
      errEl.textContent = "Failed to start batch: " + err.message;
    });
}

function showMultiRegisterProgress(batch) {
  document.getElementById("multiRegisterFormSection").style.display =
    "none";
  document.getElementById("multiRegisterProgressSection").style.display =
    "";
  document.getElementById("multiRegisterStartBtn").style.display = "none";
  document.getElementById("multiRegisterNewBatchBtn").style.display = "";
  multiRegisterActiveBatchId = batch.batch_id;
  renderMultiRegisterProgress(batch);
  if (multiRegisterPollTimer) clearInterval(multiRegisterPollTimer);
  multiRegisterPollTimer = setInterval(function () {
    pollMultiRegisterBatch(batch.batch_id);
  }, 5000);
}

function pollMultiRegisterBatch(batchId) {
  fetch("/api/regbot/batch/" + encodeURIComponent(batchId))
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (batch) {
      renderMultiRegisterProgress(batch);
      if (
        batch.status === "DONE" ||
        batch.status === "CANCELLED" ||
        batch.status === "FAILED"
      ) {
        clearInterval(multiRegisterPollTimer);
        multiRegisterPollTimer = null;
        loadRegBotActiveRuns(true); // the batch's last step may have just finished — refresh the active bar
        loadBatchActiveRuns(); // and drop the batch's own pill
      }
    })
    .catch(function (err) {
      console.warn("Batch poll failed:", err);
    });
}

function renderMultiRegisterProgress(batch) {
  var badge = document.getElementById("multiRegisterStatusBadge");
  badge.textContent = batch.status;
  badge.className =
    "badge " +
    (batch.status === "RUNNING"
      ? "bg-primary"
      : batch.status === "DONE"
        ? "bg-success"
        : batch.status === "FAILED"
          ? "bg-danger"
          : batch.status === "CANCELLING"
            ? "bg-warning"
            : "bg-secondary");

  var doneCount = batch.queue.filter(function (i) {
    return i.status === "DONE";
  }).length;
  var failedCount = batch.queue.filter(function (i) {
    return i.status === "LAUNCH_FAILED";
  }).length;
  // Surface failures in the summary too — a batch that launched nothing
  // otherwise reads as "0 / N complete", which looks like "not started
  // yet" rather than "every one of these errored".
  document.getElementById("multiRegisterProgressSummary").textContent =
    doneCount +
    " / " +
    batch.queue.length +
    " complete" +
    (failedCount ? " · " + failedCount + " failed to launch" : "");

  document.getElementById("multiRegisterCancelBtn").style.display =
    batch.status === "RUNNING" ? "" : "none";

  var rows = document.getElementById("multiRegisterQueueRows");
  rows.innerHTML = batch.queue
    .map(function (item, idx) {
      var detail = "";
      if (item.status === "DONE") {
        detail =
          item.synced_uids && item.synced_uids.length
            ? "UID " + item.synced_uids.join(", ")
            : "no new UID";
      } else if (item.status === "RUNNING") {
        detail = escapeHtml(stripAnsi(item.last_line) || "starting…");
      } else if (item.status === "LAUNCH_FAILED") {
        detail = escapeHtml(item.error || "launch failed");
      } else if (item.status === "CANCELLED") {
        detail = "cancelled";
      }
      return (
        "<tr>" +
        "<td>" +
        (idx + 1) +
        "</td>" +
        "<td>" +
        escapeHtml(item.server) +
        " #" +
        item.seq +
        "</td>" +
        "<td>τ" +
        item.reglimit +
        "</td>" +
        "<td>" +
        (item.port_prefix ? escapeHtml(item.port_prefix) : "—") +
        "</td>" +
        "<td>" +
        (MULTI_REGISTER_STATUS_ICON[item.status] || "") +
        " " +
        item.status +
        "</td>" +
        "<td style='font-size: 0.78rem'>" +
        detail +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
}

function cancelMultiRegisterBatch() {
  if (!multiRegisterActiveBatchId) return;
  // Same single-modal-at-a-time discipline as the RegBot/Unstake
  // confirm flows — hide this modal first, the cancel-confirm modal's
  // "hidden" handler reopens it either way (Keep Running or Confirm).
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiRegisterModal"),
  ).hide();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiRegisterCancelConfirmModal"),
  ).show();
}

document
  .getElementById("multiRegisterCancelConfirmModal")
  .addEventListener("hidden.bs.modal", function () {
    bootstrap.Modal.getOrCreateInstance(
      document.getElementById("multiRegisterModal"),
    ).show();
  });

function confirmMultiRegisterCancel() {
  if (!multiRegisterActiveBatchId) return;
  var batchId = multiRegisterActiveBatchId;

  var confirmModalEl = document.getElementById(
    "multiRegisterCancelConfirmModal",
  );
  var confirmModal = bootstrap.Modal.getInstance(confirmModalEl);
  if (confirmModal) confirmModal.hide();

  var btn = document.getElementById("multiRegisterCancelBtn");
  btn.disabled = true;
  fetch("/api/regbot/batch/" + encodeURIComponent(batchId) + "/cancel", {
    method: "POST",
  })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (batch) {
      renderMultiRegisterProgress(batch);
    })
    .catch(function (err) {
      showNotifyAlert(
        "Failed to cancel batch",
        err.message,
        "error",
        8000,
      );
    })
    .finally(function () {
      btn.disabled = false;
    });
}

// ---- Multi-Server Unstake (parallel — see start_unstake_batch() for why) ----
var multiUnstakePollTimer = null;
var multiUnstakeActiveBatchId = null;
var multiUnstakePendingStart = null; // { items }

var MULTI_UNSTAKE_STATUS_ICON = {
  PENDING: '<i class="bi bi-circle text-muted"></i>',
  RUNNING: '<i class="bi bi-arrow-repeat text-primary"></i>',
  DONE: '<i class="bi bi-check-circle-fill text-success"></i>',
  LAUNCH_FAILED:
    '<i class="bi bi-exclamation-triangle-fill text-danger"></i>',
};

function resetMultiUnstakeModal() {
  document.getElementById("multiUnstakeFormSection").style.display = "";
  document.getElementById("multiUnstakeProgressSection").style.display =
    "none";
  document.getElementById("multiUnstakeFormError").style.display = "none";
  document.getElementById("multiUnstakeEstimate").textContent = "";
  document.getElementById("multiUnstakeStartBtn").style.display = "";
  document.getElementById("multiUnstakeMode").value = "unstake";
  multiUnstakePendingStart = null;
  multiUnstakeActiveBatchId = null;
  if (multiUnstakePollTimer) {
    clearInterval(multiUnstakePollTimer);
    multiUnstakePollTimer = null;
  }
  onMultiUnstakeModeChanged();
}

function openMultiUnstakeModal() {
  resetMultiUnstakeModal();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiUnstakeModal"),
  ).show();
  loadUnstakePresets()
    .then(populateMultiUnstakeForm)
    .catch(function (err) {
      var errEl = document.getElementById("multiUnstakeFormError");
      errEl.style.display = "";
      errEl.textContent = "Failed to load servers: " + err.message;
    });
  // Resume showing progress if a batch was already running — same
  // reasoning as openMultiRegisterModal().
  fetch("/api/unstake/batch/active")
    .then(function (r) {
      return r.json();
    })
    .then(function (data) {
      if (data.batch && data.batch.status !== "DONE") {
        showMultiUnstakeProgress(data.batch);
      }
    })
    .catch(function () {});
}

function populateMultiUnstakeForm(data) {
  var rows = document.getElementById("multiUnstakeServerRows");
  rows.innerHTML = data.servers
    .map(function (s) {
      var balText =
        s.balance_tao != null ? formatRegCostTao(s.balance_tao) : "—";
      var stakedText =
        s.staked_tao != null ? formatRegCostTao(s.staked_tao) : "—";
      return (
        '<tr data-server="' +
        escapeHtml(s.name) +
        '" onclick="toggleMultiUnstakeRow(this)">' +
        "<td>" +
        escapeHtml(s.name) +
        " (" +
        escapeHtml(s.wallet) +
        ")</td>" +
        "<td>" +
        balText +
        "</td>" +
        "<td>" +
        stakedText +
        "</td>" +
        "<td>" +
        '<input type="text" ' +
        'class="form-control form-control-sm multi-unstake-destination-input" ' +
        'placeholder="SS58 address" value="' +
        escapeHtml(s.default_destination || "") +
        '" data-server="' +
        escapeHtml(s.name) +
        '" onclick="event.stopPropagation()" ' +
        'oninput="updateMultiUnstakeEstimate()" />' +
        "</td>" +
        "<td>" +
        '<input type="number" min="0" step="0.0001" ' +
        'class="form-control form-control-sm multi-unstake-amount-input" ' +
        'placeholder="all" data-server="' +
        escapeHtml(s.name) +
        '" onclick="event.stopPropagation()" ' +
        'oninput="updateMultiUnstakeEstimate()" />' +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
  onMultiUnstakeModeChanged();
}

function toggleMultiUnstakeRow(row) {
  row.classList.toggle("selected");
  updateMultiUnstakeEstimate();
}

function onMultiUnstakeModeChanged() {
  var isTransfer = modeNeedsDestination(
    document.getElementById("multiUnstakeMode").value,
  );
  document
    .querySelectorAll(".multi-unstake-destination-input")
    .forEach(function (el) {
      el.disabled = !isTransfer;
    });
  document.getElementById("multiUnstakeDestinationHeader").textContent =
    isTransfer ? "Destination" : "Destination (n/a — unstake only)";
  // Amount is transfer-only, same reasoning as the single-server modal:
  // with unstake_transfer the sweep size isn't known until the unstake
  // has actually freed the stake.
  var isTransferOnly =
    document.getElementById("multiUnstakeMode").value === "transfer";
  document
    .querySelectorAll(".multi-unstake-amount-input")
    .forEach(function (el) {
      el.disabled = !isTransferOnly;
      if (!isTransferOnly) el.value = "";
    });
  document.getElementById("multiUnstakeAmountHeader").textContent =
    isTransferOnly ? "Amount (τ)" : "Amount (n/a)";
  updateMultiUnstakeEstimate();
}

function multiUnstakeSelectedItems() {
  var destByServer = {};
  document
    .querySelectorAll(".multi-unstake-destination-input")
    .forEach(function (el) {
      destByServer[el.dataset.server] = el.value.trim();
    });
  var amountByServer = {};
  document
    .querySelectorAll(".multi-unstake-amount-input")
    .forEach(function (el) {
      var raw = (el.value || "").trim();
      // Blank stays null: that means "send everything", which is a
      // different instruction from a number, not a missing value.
      amountByServer[el.dataset.server] = raw ? parseFloat(raw) : null;
    });
  return Array.prototype.slice
    .call(
      document.querySelectorAll("#multiUnstakeServerRows tr.selected"),
    )
    .map(function (row) {
      var server = row.dataset.server;
      return {
        server: server,
        destination: destByServer[server] || "",
        amount: amountByServer[server],
      };
    });
}

function updateMultiUnstakeEstimate() {
  var items = multiUnstakeSelectedItems();
  var el = document.getElementById("multiUnstakeEstimate");
  el.textContent =
    items.length === 0
      ? ""
      : items.length +
        " server(s)/coldkey(s) selected — will run at the same time.";
}

function submitMultiUnstakeBatch() {
  var errEl = document.getElementById("multiUnstakeFormError");
  errEl.style.display = "none";

  var mode = document.getElementById("multiUnstakeMode").value;
  var items = multiUnstakeSelectedItems();

  if (items.length === 0) {
    errEl.style.display = "";
    errEl.textContent = "Select at least one server.";
    return;
  }
  if (modeNeedsDestination(mode)) {
    var missingDest = items.filter(function (it) {
      return !it.destination;
    });
    if (missingDest.length > 0) {
      errEl.style.display = "";
      errEl.textContent =
        "Enter a destination address for: " +
        missingDest
          .map(function (it) {
            return it.server;
          })
          .join(", ");
      return;
    }
  }
  if (mode === "transfer") {
    var badAmount = items.filter(function (it) {
      return it.amount !== null && (isNaN(it.amount) || it.amount <= 0);
    });
    if (badAmount.length > 0) {
      errEl.style.display = "";
      errEl.textContent =
        "Amount must be a positive number of TAO (or blank to send " +
        "everything) for: " +
        badAmount
          .map(function (it) {
            return it.server;
          })
          .join(", ");
      return;
    }
  }

  var batchItems = items.map(function (it) {
    return {
      server: it.server,
      mode: mode,
      destination: modeNeedsDestination(mode) ? it.destination : null,
      amount: mode === "transfer" ? it.amount : null,
    };
  });
  multiUnstakePendingStart = { items: batchItems };

  document.getElementById("multiUnstakeConfirmDetails").innerHTML =
    "<div><strong>Action:</strong> " +
    escapeHtml(modeLabel(mode)) +
    "</div>" +
    "<div><strong>Servers:</strong> " +
    items
      .map(function (it) {
        return (
          escapeHtml(it.server) +
          (modeNeedsDestination(mode)
            ? " → " + escapeHtml(it.destination)
            : "") +
          (mode === "transfer"
            ? " (" +
              (it.amount === null
                ? "all"
                : "τ" + escapeHtml(String(it.amount))) +
              ")"
            : "")
        );
      })
      .join(", ") +
    "</div>" +
    "<div class='mt-2'><strong>" +
    items.length +
    " coldkey(s) run at the same time</strong></div>";

  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiUnstakeModal"),
  ).hide();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiUnstakeConfirmModal"),
  ).show();
}

document
  .getElementById("multiUnstakeConfirmModal")
  .addEventListener("hidden.bs.modal", function () {
    bootstrap.Modal.getOrCreateInstance(
      document.getElementById("multiUnstakeModal"),
    ).show();
  });

document
  .getElementById("multiUnstakeModal")
  .addEventListener("hidden.bs.modal", function () {
    // The batch is a server-side background task independent of this
    // modal — closing it just stops polling from the client side, it
    // does NOT stop the batch. Reopening the modal resumes showing
    // progress (see openMultiUnstakeModal).
    if (multiUnstakePollTimer) {
      clearInterval(multiUnstakePollTimer);
      multiUnstakePollTimer = null;
    }
  });

function confirmMultiUnstakeStart() {
  if (!multiUnstakePendingStart) return;
  var params = multiUnstakePendingStart;
  multiUnstakePendingStart = null;

  var confirmModalEl = document.getElementById(
    "multiUnstakeConfirmModal",
  );
  var confirmModal = bootstrap.Modal.getInstance(confirmModalEl);
  if (confirmModal) confirmModal.hide();

  document.getElementById("multiUnstakeFormSection").style.display =
    "none";
  document.getElementById("multiUnstakeProgressSection").style.display =
    "";
  document.getElementById("multiUnstakeStartBtn").style.display = "none";
  document.getElementById("multiUnstakeStatusBadge").textContent =
    "Starting…";
  document.getElementById("multiUnstakeStatusBadge").className =
    "badge bg-secondary";
  document.getElementById("multiUnstakeQueueRows").innerHTML = "";

  fetch("/api/unstake/batch/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: params.items }),
  })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (batch) {
      showMultiUnstakeProgress(batch);
    })
    .catch(function (err) {
      document.getElementById("multiUnstakeFormSection").style.display =
        "";
      document.getElementById(
        "multiUnstakeProgressSection",
      ).style.display = "none";
      document.getElementById("multiUnstakeStartBtn").style.display = "";
      var errEl = document.getElementById("multiUnstakeFormError");
      errEl.style.display = "";
      errEl.textContent = "Failed to start batch: " + err.message;
    });
}

function showMultiUnstakeProgress(batch) {
  document.getElementById("multiUnstakeFormSection").style.display =
    "none";
  document.getElementById("multiUnstakeProgressSection").style.display =
    "";
  document.getElementById("multiUnstakeStartBtn").style.display = "none";
  multiUnstakeActiveBatchId = batch.batch_id;
  renderMultiUnstakeProgress(batch);
  if (multiUnstakePollTimer) clearInterval(multiUnstakePollTimer);
  multiUnstakePollTimer = setInterval(function () {
    pollMultiUnstakeBatch(batch.batch_id);
  }, 5000);
  loadUnstakeActiveRuns(); // show its pill(s) immediately, not after up to 20s
}

function pollMultiUnstakeBatch(batchId) {
  fetch("/api/unstake/batch/" + encodeURIComponent(batchId))
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    })
    .then(function (batch) {
      renderMultiUnstakeProgress(batch);
      if (batch.status === "DONE") {
        clearInterval(multiUnstakePollTimer);
        multiUnstakePollTimer = null;
        loadRegBotBalances(); // balances may have shifted for every included server
        loadUnstakeActiveRuns(); // the batch's last item may have just finished
      }
    })
    .catch(function (err) {
      console.warn("Unstake batch poll failed:", err);
    });
}

function renderMultiUnstakeProgress(batch) {
  var badge = document.getElementById("multiUnstakeStatusBadge");
  badge.textContent = batch.status;
  badge.className =
    "badge " + (batch.status === "RUNNING" ? "bg-primary" : "bg-success");

  var doneCount = batch.queue.filter(function (i) {
    return i.status === "DONE" || i.status === "LAUNCH_FAILED";
  }).length;
  document.getElementById("multiUnstakeProgressSummary").textContent =
    doneCount + " / " + batch.queue.length + " complete";

  var rows = document.getElementById("multiUnstakeQueueRows");
  rows.innerHTML = batch.queue
    .map(function (item, idx) {
      var statusLabel = item.status;
      var icon = MULTI_UNSTAKE_STATUS_ICON[item.status] || "";
      var detail = "";
      if (item.status === "DONE") {
        statusLabel = item.timed_out
          ? "TIMEOUT"
          : item.success
            ? "SUCCESS"
            : "FAILED";
        if (!item.success) {
          icon = '<i class="bi bi-x-circle-fill text-danger"></i>';
        }
        detail = escapeHtml(stripAnsi(item.last_line) || "");
      } else if (item.status === "RUNNING") {
        detail = escapeHtml(stripAnsi(item.last_line) || "starting…");
      } else if (item.status === "LAUNCH_FAILED") {
        detail = escapeHtml(item.error || "launch failed");
      }
      return (
        "<tr>" +
        "<td>" +
        (idx + 1) +
        "</td>" +
        "<td>" +
        escapeHtml(item.server) +
        "</td>" +
        "<td>" +
        escapeHtml(modeLabel(item.mode)) +
        (item.mode === "transfer"
          ? " (" +
            (item.amount == null
              ? "all"
              : "τ" + escapeHtml(String(item.amount))) +
            ")"
          : "") +
        "</td>" +
        "<td>" +
        icon +
        " " +
        statusLabel +
        "</td>" +
        "<td style='font-size: 0.78rem'>" +
        detail +
        "</td>" +
        "</tr>"
      );
    })
    .join("");
}

// ---- RegBot active-run bar: which server(s) have a live RegBot process ----

function formatElapsedSince(iso) {
  var start = new Date(iso).getTime();
  if (isNaN(start)) return "";
  var sec = Math.max(0, Math.floor((Date.now() - start) / 1000));
  var h = Math.floor(sec / 3600);
  var m = Math.floor((sec % 3600) / 60);
  return h > 0 ? h + "h" + m + "m ago" : m + "m ago";
}

function viewRegBotRun(server, runId) {
  resetAddMinerModal();
  document.getElementById("addMinerFormSection").style.display = "none";
  document.getElementById("addMinerStatusSection").style.display = "";
  document.getElementById("addMinerRefreshLogBtn").style.display = "";
  document.getElementById("addMinerKillBtn").style.display = "";
  document.getElementById("addMinerSubmitBtn").style.display = "none";
  document.getElementById("addMinerStatusBadge").textContent =
    "Checking…";
  document.getElementById("addMinerStatusBadge").className =
    "badge bg-secondary";
  document.getElementById("addMinerStatusSummary").textContent =
    " " + server + " · run " + runId;
  addMinerCurrentRun = { server: server, run_id: runId };
  addMinerLastKnownStatus = "RUNNING";
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("addMinerModal"),
  ).show();
  refreshAddMinerLog();
  // Clear first: this is an onclick on a bar that re-renders every scan, so
  // viewing run A then run B — or double-clicking one — used to leave the
  // previous 4s poller running against /api/regbot/log forever, with only the
  // newest timer id kept and the rest unreachable.
  if (addMinerLogTimer) clearInterval(addMinerLogTimer);
  addMinerLogTimer = setInterval(refreshAddMinerLog, 4000);
}

function renderRegbotActiveBar(data) {
  var bar = document.getElementById("regbotActiveBar");
  var inner = document.getElementById("regbotActiveBarInner");
  if (
    getSubnetFromInput() !== 79 ||
    !data ||
    !data.active ||
    data.active.length === 0
  ) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "";
  var html =
    '<span class="regbot-hint" style="font-weight: 600">' +
    '<i class="bi bi-broadcast"></i> RegBot running:</span>';
  html += data.active
    .map(function (r) {
      var elapsed = r.started_at ? formatElapsedSince(r.started_at) : "";
      var lastLine = stripAnsi(r.last_line) || "";
      var titleText = escapeHtml(
        r.server +
          " — " +
          r.run_id +
          (elapsed ? "\n" + elapsed : "") +
          (lastLine ? "\n" + lastLine : ""),
      );
      var reglimitText = r.reglimit != null ? "τ" + r.reglimit : "";
      var registered =
        r.registered_count != null ? r.registered_count : "?";
      var maxCount = r.max_count != null ? r.max_count : "?";
      return (
        '<span class="regbot-run-pill" title="' +
        titleText +
        '" onclick="viewRegBotRun(\'' +
        r.server +
        "','" +
        r.run_id +
        "')\">" +
        '<span class="regbot-pulse-dot"></span>' +
        escapeHtml(r.server) +
        (reglimitText ? " · " + reglimitText : "") +
        " · " +
        registered +
        "/" +
        maxCount +
        "</span>"
      );
    })
    .join("");
  if (data.unreachable_servers && data.unreachable_servers.length) {
    html +=
      '<span class="regbot-unreachable-note">Could not check: ' +
      escapeHtml(data.unreachable_servers.join(", ")) +
      "</span>";
  }
  inner.innerHTML = html;
}

// `force` asks the server to re-run its SSH scan instead of answering from the
// scan worker's last result. Use it only after an action that just changed what
// is running (trigger, kill, a batch step finishing) — a background poll must
// never pass it, or the 690ms fan-out is right back on the request path.
function loadRegBotActiveRuns(force) {
  if (getSubnetFromInput() !== 79) {
    document.getElementById("regbotActiveBar").style.display = "none";
    return;
  }
  conditionalFetch("/api/regbot/active" + (force ? "?refresh=1" : ""))
    .then(renderRegbotActiveBar)
    .catch(function () {
      // Silent — this is a background poll, not a user-initiated action.
    });
}

// ---- Batch bar: registration batches in flight, with queue progress ----
// The RegBot bar above shows individual creg79 runs, but a run gives no
// hint that it belongs to a batch or how much of that batch is left —
// this shows the queue itself so a long batch is visible at a glance.

function openBatchFromBar(batchId) {
  resetMultiRegisterModal();
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("multiRegisterModal"),
  ).show();
  loadRegBotPresets()
    .then(populateMultiRegisterForm)
    .catch(function () {});
  viewMultiRegisterBatch(batchId);
}

function renderBatchActiveBar(data) {
  var bar = document.getElementById("batchActiveBar");
  var inner = document.getElementById("batchActiveBarInner");
  var batches = (data && data.batches) || [];
  if (getSubnetFromInput() !== 79 || !batches.length) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "";
  var html =
    '<span class="regbot-hint" style="font-weight: 600">' +
    '<i class="bi bi-list-ol"></i> Batch running:</span>';
  html += batches
    .map(function (b) {
      var total = b.queue.length;
      var done = b.queue.filter(function (i) {
        return i.status === "DONE";
      }).length;
      var failed = b.queue.filter(function (i) {
        return i.status === "LAUNCH_FAILED";
      }).length;
      var inFlight = b.queue.filter(function (i) {
        return i.status === "RUNNING";
      });
      var uids = b.queue.reduce(function (acc, i) {
        return acc.concat(i.synced_uids || []);
      }, []);
      var pct = total ? Math.round((done / total) * 100) : 0;
      var title = escapeHtml(
        b.batch_id +
          "\n" +
          done +
          "/" +
          total +
          " registered" +
          (failed ? ", " + failed + " failed to launch" : "") +
          (uids.length ? "\nUIDs: " + uids.join(", ") : "") +
          (inFlight.length
            ? "\nnow: " +
              inFlight
                .map(function (i) {
                  return (
                    i.server +
                    " — " +
                    (stripAnsi(i.last_line) || "starting…")
                  );
                })
                .join("; ")
            : ""),
      );
      return (
        '<span class="batch-run-pill" title="' +
        title +
        '" onclick="openBatchFromBar(\'' +
        b.batch_id +
        "')\">" +
        '<span class="batch-pulse-dot"></span>' +
        (b.status === "CANCELLING" ? "cancelling · " : "") +
        done +
        "/" +
        total +
        '<span class="batch-progress-track">' +
        '<span class="batch-progress-fill" style="width: ' +
        pct +
        '%"></span></span>' +
        (inFlight.length
          ? " " + escapeHtml(inFlight[0].server)
          : " queued") +
        (failed
          ? ' <span class="text-danger">' + failed + " failed</span>"
          : "") +
        "</span>"
      );
    })
    .join("");
  inner.innerHTML = html;
}

function loadBatchActiveRuns() {
  if (getSubnetFromInput() !== 79) {
    document.getElementById("batchActiveBar").style.display = "none";
    return;
  }
  conditionalFetch("/api/regbot/batch/active")
    .then(renderBatchActiveBar)
    .catch(function () {
      // Silent — background poll, not a user-initiated action.
    });
}

// ---- Unstake active-run bar: which coldkey(s) have a live unstake in
// flight right now — covers both a solo run and any in-flight batch
// item, since a batch item is just a regular run under the hood. ----

function viewUnstakeRun(server, runId, startedAt) {
  resetUnstakeModal();
  document.getElementById("unstakeFormSection").style.display = "none";
  document.getElementById("unstakeResultSection").style.display = "";
  document.getElementById("unstakeResultBadge").textContent = "Checking…";
  document.getElementById("unstakeResultBadge").className =
    "badge bg-secondary";
  document.getElementById("unstakeResultSummary").textContent =
    " " + server + " · run " + runId;
  document.getElementById("unstakeRunBtn").style.display = "none";
  unstakeCurrentRun = {
    run_id: runId,
    startedAt: startedAt ? new Date(startedAt).getTime() : Date.now(),
  };
  unstakeLastKnownStatus = "RUNNING";
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("unstakeModal"),
  ).show();
  refreshUnstakeLog();
  // Same leak as viewRegBotRun: clear before replacing, or every extra click
  // orphans a poller that nothing can stop short of a page reload.
  if (unstakeLogTimer) clearInterval(unstakeLogTimer);
  unstakeLogTimer = setInterval(refreshUnstakeLog, 4000);
}

function renderUnstakeActiveBar(data) {
  var bar = document.getElementById("unstakeActiveBar");
  var inner = document.getElementById("unstakeActiveBarInner");
  if (
    getSubnetFromInput() !== 79 ||
    !data ||
    !data.active ||
    data.active.length === 0
  ) {
    bar.style.display = "none";
    return;
  }
  bar.style.display = "";
  var html =
    '<span class="regbot-hint" style="font-weight: 600">' +
    '<i class="bi bi-arrow-down-circle"></i> Unstake running:</span>';
  html += data.active
    .map(function (r) {
      var elapsed = r.started_at ? formatElapsedSince(r.started_at) : "";
      var lastLine = stripAnsi(r.last_line) || "";
      var titleText = escapeHtml(
        r.server +
          " — " +
          r.run_id +
          (elapsed ? "\n" + elapsed : "") +
          (lastLine ? "\n" + lastLine : ""),
      );
      var modeText = modeLabel(r.mode);
      return (
        '<span class="unstake-run-pill" title="' +
        titleText +
        '" onclick="viewUnstakeRun(\'' +
        r.server +
        "','" +
        r.run_id +
        "','" +
        (r.started_at || "") +
        "')\">" +
        '<span class="unstake-pulse-dot"></span>' +
        escapeHtml(r.server) +
        " · " +
        modeText +
        "</span>"
      );
    })
    .join("");
  inner.innerHTML = html;
}

function loadUnstakeActiveRuns() {
  if (getSubnetFromInput() !== 79) {
    document.getElementById("unstakeActiveBar").style.display = "none";
    return;
  }
  conditionalFetch("/api/unstake/active")
    .then(renderUnstakeActiveBar)
    .catch(function () {
      // Silent — this is a background poll, not a user-initiated action.
    });
}

// ---- Coldkeys/IPs watch: hourly metagraph scan, flags what's new ----
function openColdkeysModal() {
  bootstrap.Modal.getOrCreateInstance(
    document.getElementById("coldkeysModal"),
  ).show();
  loadColdkeysSummary(true);
}

function renderColdkeysSummary(data) {
  var summaryEl = document.getElementById("coldkeysSummaryLine");
  var badge = document.getElementById("coldkeysBadge");
  var newCount = data.new_coldkeys ? data.new_coldkeys.length : 0;

  if (!data.run_at) {
    summaryEl.textContent = "No check has run yet.";
  } else {
    var label = document.getElementById("coldkeysNetuidLabel");
    if (label && data.netuid != null)
      label.textContent = "netuid-" + data.netuid;
    summaryEl.textContent =
      "Last checked: " +
      new Date(data.run_at).toLocaleString() +
      " · " +
      data.total_coldkeys +
      " coldkeys, " +
      data.total_ips +
      " IPs total" +
      (data.is_first_run
        ? " (baseline run — nothing flagged as new yet)"
        : "");
  }

  if (newCount > 0) {
    badge.textContent = newCount;
    badge.style.display = "";
  } else {
    badge.style.display = "none";
  }

  var ckList = document.getElementById("coldkeysNewColdkeysList");
  if (!data.new_coldkeys || data.new_coldkeys.length === 0) {
    ckList.innerHTML =
      '<div class="text-muted" style="font-size: 0.85rem">None</div>';
  } else {
    ckList.innerHTML = data.new_coldkeys
      .map(function (ck) {
        var ips =
          (data.new_coldkey_to_ips && data.new_coldkey_to_ips[ck]) || [];
        return (
          '<div class="mb-1" style="font-family: monospace; font-size: 0.82rem">' +
          escapeHtml(ck) +
          (ips.length
            ? ' <span class="text-muted">(' +
              ips.map(escapeHtml).join(", ") +
              ")</span>"
            : "") +
          "</div>"
        );
      })
      .join("");
  }

  var ipList = document.getElementById("coldkeysNewIpsList");
  if (!data.new_ips || data.new_ips.length === 0) {
    ipList.innerHTML =
      '<div class="text-muted" style="font-size: 0.85rem">None</div>';
  } else {
    ipList.innerHTML = data.new_ips
      .map(function (ip) {
        return (
          '<div class="mb-1" style="font-family: monospace; font-size: 0.82rem">' +
          escapeHtml(ip) +
          "</div>"
        );
      })
      .join("");
  }
}

function loadColdkeysSummary(showLoadingState) {
  if (showLoadingState) {
    document.getElementById("coldkeysSummaryLine").textContent =
      "Loading…";
  }
  conditionalFetch("/api/coldkeys/summary")
    .then(renderColdkeysSummary)
    .catch(function (err) {
      if (showLoadingState) {
        document.getElementById("coldkeysSummaryLine").textContent =
          "Failed to load: " + err.message;
      }
    });
}

function refreshColdkeysNow() {
  var btn = document.getElementById("coldkeysRefreshBtn");
  var originalHtml = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML =
    '<span class="spinner-border spinner-border-sm" role="status" ' +
    'aria-hidden="true"></span> Checking…';
  fetch("/api/coldkeys/refresh", { method: "POST" })
    .then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok) throw new Error(data.detail || "HTTP " + r.status);
        return data;
      });
    })
    .then(function (data) {
      renderColdkeysSummary(data);
      if (data.new_coldkeys && data.new_coldkeys.length) {
        showNotifyAlert(
          "New coldkey(s) detected",
          data.new_coldkeys.length + " new coldkey(s) on SN79",
          "success",
          10000,
        );
      }
    })
    .catch(function (err) {
      showNotifyAlert("Coldkey check failed", err.message, "error", 8000);
    })
    .finally(function () {
      btn.disabled = false;
      btn.innerHTML = originalHtml;
    });
}
