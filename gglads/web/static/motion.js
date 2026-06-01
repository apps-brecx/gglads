// motion.js — drives the ambient mouse-follow glow on .app-body and
// fades panels in as they scroll into view.
//
// Honors prefers-reduced-motion: bails out and does nothing.

(function () {
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    return;
  }

  // --- mouse-follow accent glow ---------------------------------------------
  // Throttled to one update per animation frame so it stays cheap.
  var pending = false;
  var lx = 0, ly = 0;
  var body = document.body;
  function applyGlow() {
    pending = false;
    body.style.setProperty('--mx', lx + 'px');
    body.style.setProperty('--my', ly + 'px');
  }
  window.addEventListener('mousemove', function (e) {
    lx = e.clientX; ly = e.clientY;
    if (!pending) {
      pending = true;
      requestAnimationFrame(applyGlow);
    }
  }, { passive: true });

  // --- panel tilt on hover (subtle, ≤3deg) ----------------------------------
  // Only applied to .panel / .card / .collection-card. Skips on touch-only
  // devices since hover is non-existent there.
  if (!window.matchMedia('(hover: hover)').matches) return;

  function bindTilt(el) {
    el.addEventListener('mousemove', function (e) {
      var rect = el.getBoundingClientRect();
      var rx = ((e.clientY - rect.top) / rect.height - 0.5) * -2.2;
      var ry = ((e.clientX - rect.left) / rect.width - 0.5) * 2.2;
      el.style.transform =
        'translateY(-2px) perspective(1200px) rotateX(' + rx.toFixed(2) +
        'deg) rotateY(' + ry.toFixed(2) + 'deg)';
    });
    el.addEventListener('mouseleave', function () {
      el.style.transform = '';
    });
  }
  // Tilt is too busy on dense pages (tables, grids of inputs). Apply only to
  // collection cards and dashboard cards — places where the user is scanning.
  document.querySelectorAll('.collection-card, .card').forEach(bindTilt);
})();
