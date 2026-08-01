// Sets document.documentElement.dataset.theme to a literal "light" or "dark"
// before first paint, so the app never flashes light before flipping dark for
// a user whose OS (or stored choice) prefers dark. Loaded first in <head>,
// with no defer/async/type=module, before any stylesheet <link> — see
// docs/plans/light-dark-theme.md's "The FOUC decision".
//
// Every localStorage access is wrapped so it cannot throw: Safari private
// mode and blocked-storage configurations throw on ACCESS, not just on
// write, and a throw here (a render-blocking head script) would otherwise
// take the whole page down before it ever painted.
(function () {
  "use strict";

  var KEY = "ss-theme";
  var MODES = ["system", "light", "dark"];

  function readStored() {
    try {
      return window.localStorage.getItem(KEY);
    } catch (e) {
      return null;
    }
  }

  function writeStored(mode) {
    try {
      window.localStorage.setItem(KEY, mode);
    } catch (e) {
      // Best-effort only — an explicit choice just won't survive reload.
    }
  }

  function get() {
    var stored = readStored();
    // Absent or unrecognized is treated as "system" without rewriting
    // storage — a future version may add a value, and clobbering an
    // unrecognized-but-intentional value is not this script's job.
    if (MODES.indexOf(stored) === -1) {
      return "system";
    }
    return stored;
  }

  function prefersDark() {
    try {
      return window.matchMedia("(prefers-color-scheme: dark)").matches;
    } catch (e) {
      return false;
    }
  }

  function resolve(mode) {
    if (mode === "light" || mode === "dark") {
      return mode;
    }
    return prefersDark() ? "dark" : "light";
  }

  function apply() {
    document.documentElement.dataset.theme = resolve(get());
  }

  function set(mode) {
    writeStored(mode);
    apply();
  }

  apply();

  try {
    window
      .matchMedia("(prefers-color-scheme: dark)")
      .addEventListener("change", function () {
        // Only follow the OS while the mode is "system" — an explicit
        // light/dark choice must not be overridden by an OS flip.
        if (get() === "system") {
          apply();
        }
      });
  } catch (e) {
    // matchMedia or addEventListener unavailable — the app still works,
    // it just won't live-follow an OS theme change until next navigation.
  }

  // Cross-tab sync. `storage` fires only in OTHER tabs on the same origin,
  // never in the one that wrote, so this cannot loop back on itself. Without
  // it two simultaneously-open tabs disagree until one of them navigates,
  // since every navigation already re-reads localStorage.
  //
  // e.key is null when a page calls localStorage.clear(), which invalidates
  // our value as surely as writing to it, so that case re-applies too.
  window.addEventListener("storage", function (e) {
    if (e.key === KEY || e.key === null) {
      apply();
    }
  });

  window.ssTheme = {
    KEY: KEY,
    get: get,
    set: set,
    resolve: resolve,
    apply: apply,
  };
})();
