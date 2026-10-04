// The colour theme: "auto" (follow the system), "light" or "dark", remembered in this browser. A classic script
// loaded in <head>, so the page is drawn with the right colours from the first frame.
(function () {
  var KEY = "clara.theme";
  var media = window.matchMedia("(prefers-color-scheme: dark)");

  function stored() {
    try {
      var value = localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "auto";
    } catch (e) {
      return "auto"; // storage refused (private mode): follow the system
    }
  }

  function apply(choice) {
    var root = document.documentElement;
    if (choice === "auto") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    var dark = choice === "dark" || (choice === "auto" && media.matches);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? "#1d1916" : "#f7f3ec");
  }

  apply(stored());
  media.addEventListener("change", function () { apply(stored()); });

  window.claraTheme = {
    get: stored,
    set: function (choice) {
      try { localStorage.setItem(KEY, choice); } catch (e) { /* kept for this page only */ }
      apply(choice);
    },
  };
})();
