/* Runs synchronously in <head> (before first paint) so the saved theme never flashes.
   Classic script on purpose: CSP forbids inline scripts. The persisted preference lives in
   localStorage 'pd.theme' = 'light' | 'dark' | 'system' (js/main.js owns it afterwards). */
(function () {
  try {
    var pref = localStorage.getItem('pd.theme');
    if (pref === 'light' || pref === 'dark') document.documentElement.setAttribute('data-theme', pref);
  } catch (e) { /* storage blocked: follow the OS scheme */ }
})();
