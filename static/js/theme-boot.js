/* Runs synchronously in <head> (before first paint) so the saved theme/book never flash.
   Classic script on purpose: CSP forbids inline scripts. The persisted preferences live in
   localStorage 'pd.theme' = 'light' | 'dark' | 'system' and 'pd.book' = 'real' | 'practice'
   (js/main.js and js/book.js own them afterwards; same storage keys, read the same way). */
(function () {
  try {
    var pref = localStorage.getItem('pd.theme');
    if (pref === 'light' || pref === 'dark') document.documentElement.setAttribute('data-theme', pref);
  } catch (e) { /* storage blocked: follow the OS scheme */ }

  try {
    var book = 'real';
    var m = /(?:^|[&#])book=([a-z]+)/.exec(location.hash);
    if (m && (m[1] === 'practice' || m[1] === 'real')) {
      book = m[1]; // #book= deep link overrides storage for this load only (never written back)
    } else {
      var stored = localStorage.getItem('pd.book');
      if (stored === 'practice') book = stored;
    }
    if (book !== 'real') document.documentElement.setAttribute('data-book', book);
  } catch (e) { /* storage blocked: default to Real */ }
})();
