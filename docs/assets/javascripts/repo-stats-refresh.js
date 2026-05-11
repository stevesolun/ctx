(function () {
  "use strict";

  var REPO_URL = "https://github.com/stevesolun/ctx";
  var REPO_API_URL = "https://api.github.com/repos/stevesolun/ctx";
  var MATERIAL_SOURCE_CACHE_KEY = "__source";

  function isCtxSource(anchor) {
    var href = anchor.getAttribute("href") || "";
    return href.replace(/\/$/, "") === REPO_URL;
  }

  function formatCount(value) {
    return new Intl.NumberFormat("en", {
      maximumFractionDigits: 1,
      notation: value >= 10000 ? "compact" : "standard",
    })
      .format(value)
      .toLowerCase();
  }

  function createFact(kind, value) {
    var item = document.createElement("li");
    item.className = "md-source__fact md-source__fact--" + kind;
    item.textContent = formatCount(value);
    return item;
  }

  function clearRenderedFacts(source) {
    source.querySelectorAll(".md-source__facts").forEach(function (facts) {
      facts.remove();
    });

    var repository = source.querySelector(".md-source__repository");
    if (repository) {
      repository.classList.remove("md-source__repository--active");
    }
  }

  function renderStats(stats) {
    document.querySelectorAll(".md-source").forEach(function (source) {
      if (!(source instanceof HTMLAnchorElement) || !isCtxSource(source)) {
        return;
      }

      clearRenderedFacts(source);

      var repository = source.querySelector(".md-source__repository");
      if (!repository) {
        return;
      }

      var facts = document.createElement("ul");
      facts.className = "md-source__facts";
      facts.appendChild(createFact("stars", stats.stars));
      facts.appendChild(createFact("forks", stats.forks));

      repository.appendChild(facts);
      repository.classList.add("md-source__repository--active");
    });
  }

  function clearStaleStats() {
    document.querySelectorAll(".md-source").forEach(function (source) {
      if (source instanceof HTMLAnchorElement && isCtxSource(source)) {
        clearRenderedFacts(source);
      }
    });
  }

  function updateMaterialCache(stats) {
    try {
      sessionStorage.setItem(
        MATERIAL_SOURCE_CACHE_KEY,
        JSON.stringify({ stars: stats.stars, forks: stats.forks })
      );
    } catch (error) {
      /* Browser storage may be disabled. The visible header is still updated. */
    }
  }

  async function fetchStats() {
    var response = await fetch(REPO_API_URL, {
      cache: "no-store",
      headers: { Accept: "application/vnd.github+json" },
    });

    if (!response.ok) {
      throw new Error("GitHub repository stats request failed");
    }

    var data = await response.json();
    return {
      forks: Number(data.forks_count || 0),
      stars: Number(data.stargazers_count || 0),
    };
  }

  async function refreshRepoStats() {
    clearStaleStats();

    try {
      var stats = await fetchStats();
      updateMaterialCache(stats);
      renderStats(stats);
    } catch (error) {
      clearStaleStats();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", refreshRepoStats, { once: true });
  } else {
    refreshRepoStats();
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(refreshRepoStats);
  }
})();
