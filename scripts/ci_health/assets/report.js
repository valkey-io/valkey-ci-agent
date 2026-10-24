(function () {
  // Staleness banner. The page is static, so whether its data is current
  // depends on when it is read, not when it was rendered: compare the newest
  // run's date with the viewer's clock and warn past the page's threshold.
  var banners = document.querySelectorAll(".stale-banner[data-latest]");
  Array.prototype.forEach.call(banners, function (el) {
    var latest = el.getAttribute("data-latest");
    var maxAge = parseInt(el.getAttribute("data-max-age-days"), 10);
    var what = el.getAttribute("data-what") || "run";
    var text;
    if (!latest) {
      text = "<b>No " + what + " on record.</b> The sync has not stored any run yet, or stopped before it could.";
    } else {
      var then = Date.parse(latest.slice(0, 10) + "T00:00:00Z");
      if (isNaN(then)) return;
      var ageDays = Math.floor((Date.now() - then) / 86400000);
      if (!(ageDays > maxAge)) return;
      text = "<b>This report is " + ageDays + " days old.</b> The newest " + what +
        " it knows is from " + latest.slice(0, 10) + "; one is expected within " + maxAge +
        " days. The job that updates this page has probably stopped.";
    }
    el.innerHTML = text;
    el.hidden = false;
  });
})();

(function () {
  var body = document.getElementById("scorecard-body");
  if (!body) return;
  var rows = Array.prototype.slice.call(body.querySelectorAll("tr"));
  var classSel = document.getElementById("sc-class");
  var catSel = document.getElementById("sc-cat");

  // Populate category filter from the rows present.
  var cats = {};
  rows.forEach(function (r) {
    cats[r.getAttribute("data-cat")] = true;
  });
  Object.keys(cats)
    .sort()
    .forEach(function (c) {
      var o = document.createElement("option");
      o.value = c;
      o.textContent = c;
      catSel.appendChild(o);
    });

  function applyFilter() {
    var cv = classSel.value,
      catv = catSel.value;
    rows.forEach(function (r) {
      var ok =
        (!cv || r.getAttribute("data-class") === cv) &&
        (!catv || r.getAttribute("data-cat") === catv);
      r.style.display = ok ? "" : "none";
    });
  }
  classSel.addEventListener("change", applyFilter);
  catSel.addEventListener("change", applyFilter);

  var sortState = {};
  function sortBy(key) {
    var desc = (sortState[key] = !sortState[key]); // first click = descending
    rows
      .slice()
      .sort(function (a, b) {
        var av = parseFloat(a.getAttribute("data-" + key)) || 0;
        var bv = parseFloat(b.getAttribute("data-" + key)) || 0;
        return desc ? bv - av : av - bv;
      })
      .forEach(function (r) {
        body.appendChild(r);
      });
  }
  document
    .querySelectorAll("#scorecard-controls button[data-sort]")
    .forEach(function (btn) {
      btn.addEventListener("click", function () {
        sortBy(btn.getAttribute("data-sort"));
      });
    });
})();

// Tab switching (Heatmap / Scorecard / Run Details / Regressions).
// The active tab is encoded in the URL hash (e.g. #regressions) so it is
// deep-linkable and survives reload / back-forward navigation.
(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  if (!tabs.length) return;
  var panels = Array.prototype.slice.call(document.querySelectorAll(".tab-panel"));

  function activate(name) {
    var target = "tab-" + name;
    if (!panels.some(function (p) { return p.id === target; })) return false;
    tabs.forEach(function (t) {
      t.classList.toggle("active", t.getAttribute("data-tab") === name);
    });
    panels.forEach(function (p) {
      p.classList.toggle("active", p.id === target);
    });
    // When the Heatmap tab becomes visible, (re)apply the scroll-right +
    // fade indicators — a hidden panel has no measurable width at load.
    if (name === "heatmap" && typeof initHeatmapScroll === "function") {
      requestAnimationFrame(initHeatmapScroll);
    }
    return true;
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      var name = tab.getAttribute("data-tab");
      activate(name);
      // replaceState avoids spamming browser history on every tab click.
      if (window.history && history.replaceState) {
        history.replaceState(null, "", "#" + name);
      } else {
        location.hash = name;
      }
    });
  });

  function fromHash() {
    return (location.hash || "").replace(/^#/, "");
  }
  // Honor a deep link on load; fall back to the default (heatmap).
  if (!activate(fromHash())) activate("heatmap");
  // React to back/forward or manual hash edits.
  window.addEventListener("hashchange", function () {
    activate(fromHash());
  });
})();

// Wide CI (per-commit) heatmaps: start scrolled fully right (newest visible)
// and show left/right fade indicators so it's obvious there's off-screen
// content. Defined at top level so tab activation can re-run it.
function updateHeatmapFade(el) {
  var wrap = el.parentNode;
  if (!wrap || !wrap.classList.contains("heatmap-scroll-wrap")) return;
  var maxScroll = el.scrollWidth - el.clientWidth;
  wrap.classList.toggle("show-left", el.scrollLeft > 2);
  wrap.classList.toggle("show-right", el.scrollLeft < maxScroll - 2);
}

function initHeatmapScroll() {
  document.querySelectorAll(".heatmap-scroll.scroll-right").forEach(function (el) {
    el.scrollLeft = el.scrollWidth; // newest runs on the right
    updateHeatmapFade(el);
    if (!el.__fadeWired) {
      el.addEventListener("scroll", function () {
        updateHeatmapFade(el);
      });
      el.__fadeWired = true;
    }
  });
}

initHeatmapScroll();
// Re-apply once layout/fonts settle (rotated SHA headers affect width).
window.addEventListener("load", initHeatmapScroll);

// Adaptive heatmap columns: show as many date columns as fit the viewport,
// with a minimum of 14. Oldest (leftmost) columns are hidden first.
(function () {
  var MIN_COLS = 14;
  // Fixed-width budget per date column (matches .date-col min-width: 32px)
  var COL_WIDTH = 32;
  // Non-date columns: test-name (~340px) + tracked (~70px) + trend
  // sparkline (~100px) + padding
  var FIXED_WIDTH = 530;
  var styleEl = null;

  function adaptColumns() {
    var tables = document.querySelectorAll("table[data-total-cols]");
    if (!tables.length) return;

    // Remove previous adaptive style
    if (styleEl) styleEl.remove();
    styleEl = document.createElement("style");
    document.head.appendChild(styleEl);

    var rules = [];
    tables.forEach(function (table, idx) {
      var totalCols = parseInt(table.getAttribute("data-total-cols"), 10);
      if (!totalCols || totalCols <= MIN_COLS) return;

      // Measure available width from the table's scroll container
      var container = table.closest(".heatmap-scroll") || table.parentElement;
      var available = container ? container.clientWidth : window.innerWidth;
      var colBudget = Math.floor((available - FIXED_WIDTH) / COL_WIDTH);
      var show = Math.max(MIN_COLS, Math.min(totalCols, colBudget));
      var hide = totalCols - show;

      if (hide <= 0) return;

      // Add a unique class for CSS targeting
      var cls = "hm-adaptive-" + idx;
      table.classList.add(cls);

      // Date columns start at child position 3 (after test-name and the
      // trend sparkline). Hide the first `hide` date columns (oldest).
      var start = 3; // 1-indexed nth-child
      var end = start + hide - 1;
      rules.push(
        "." + cls + " tr > :nth-child(n+" + start + "):nth-child(-n+" + end + ")" +
        "{display:none}"
      );
    });

    styleEl.textContent = rules.join("\n");
  }

  // Run on load and resize (debounced)
  var resizeTimer;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(adaptColumns, 150);
  });

  // Initial run after layout settles
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", adaptColumns);
  } else {
    requestAnimationFrame(adaptColumns);
  }
  window.addEventListener("load", adaptColumns);
})();

// Collapsible failure clusters: rows with identical failure-day signatures
// are grouped under a single summary row (rendered by _render_heatmap_table).
// Clicking the summary row toggles its member rows. Scoped to the containing
// table so group ids only need to be unique per table.
(function () {
  document.querySelectorAll("tr.hm-cluster-toggle").forEach(function (row) {
    row.addEventListener("click", function (e) {
      if (e.target.closest("a")) return; // let the ⚠️ regression link navigate
      var g = row.getAttribute("data-group");
      var table = row.closest("table");
      var expanded = row.classList.toggle("hm-expanded");
      var caret = row.querySelector(".hm-caret");
      if (caret) caret.textContent = expanded ? "▾" : "▸";
      table
        .querySelectorAll('tr.hm-cluster-member[data-group="' + g + '"]')
        .forEach(function (r) {
          r.style.display = expanded ? "" : "none";
        });
    });
  });
})();
