(function () {
  "use strict";

  function param(name) {
    return new URLSearchParams(window.location.search).get(name) || "";
  }

  function selectedTypes(root) {
    return Array.from(root.querySelectorAll(".ctx-catalog-filters input:checked")).map(
      function (input) {
        return input.value;
      }
    );
  }

  function searchableText(card) {
    return (
      card.getAttribute("data-type") +
      " " +
      card.getAttribute("data-search") +
      " " +
      card.textContent
    ).toLowerCase();
  }

  function render(root) {
    var queryInput = root.querySelector("#ctx-catalog-query");
    var count = root.querySelector("#ctx-catalog-count");
    var cards = Array.from(root.querySelectorAll(".ctx-catalog-card"));
    var selected = new Set(selectedTypes(root));
    var query = (queryInput ? queryInput.value : "").trim().toLowerCase();
    var visible = 0;

    cards.forEach(function (card) {
      var matchesType = selected.has(card.getAttribute("data-type"));
      var matchesQuery = !query || searchableText(card).indexOf(query) !== -1;
      var show = matchesType && matchesQuery;
      card.hidden = !show;
      if (show) visible += 1;
    });

    if (count) {
      count.textContent = visible + " tile" + (visible === 1 ? "" : "s") + " shown";
    }
  }

  function initCatalog() {
    var root = document.querySelector(".ctx-catalog-app");
    if (!root || root.getAttribute("data-catalog-ready") === "true") {
      return;
    }
    root.setAttribute("data-catalog-ready", "true");

    var queryInput = root.querySelector("#ctx-catalog-query");
    var initialType = param("type");
    var initialQuery = param("q");

    if (queryInput && initialQuery) {
      queryInput.value = initialQuery;
    }

    if (initialType) {
      root.querySelectorAll(".ctx-catalog-filters input").forEach(function (input) {
        input.checked = input.value === initialType;
      });
    }

    if (queryInput) {
      queryInput.addEventListener("input", function () {
        render(root);
      });
    }

    root.querySelectorAll(".ctx-catalog-filters input").forEach(function (input) {
      input.addEventListener("change", function () {
        render(root);
      });
    });

    render(root);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initCatalog, { once: true });
  } else {
    initCatalog();
  }

  if (window.document$ && typeof window.document$.subscribe === "function") {
    window.document$.subscribe(initCatalog);
  }
})();
