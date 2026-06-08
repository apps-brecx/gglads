// Global Escape-to-go-back.
// First Escape closes any open overlay (image lightbox, modal, @-mention menu).
// If you're typing in a field with content, Escape blurs it (so you don't lose
// text or navigate away by accident). Otherwise Escape navigates back — every
// page is a real URL, so this walks browser history.
(function () {
  function closeTopOverlay() {
    var sels = ['#img-lightbox.open', '.modal-backdrop.open', '.modal.open', '.img-lightbox.open'];
    for (var i = 0; i < sels.length; i++) {
      var el = document.querySelector(sels[i]);
      if (el) { el.classList.remove('open'); return true; }
    }
    var mention = document.getElementById('mention-menu');
    if (mention && !mention.hidden) { mention.hidden = true; return true; }
    return false;
  }

  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' && e.keyCode !== 27) return;
    if (closeTopOverlay()) { e.preventDefault(); return; }
    var ae = document.activeElement;
    var typing = ae && (
      ae.tagName === 'TEXTAREA' ||
      (ae.tagName === 'INPUT' && !/^(checkbox|radio|button|submit|file|range|color)$/i.test(ae.type || 'text')) ||
      ae.isContentEditable
    );
    if (typing && (ae.value || ae.textContent || '').length) { ae.blur(); return; }
    if (window.history.length > 1) { e.preventDefault(); window.history.back(); }
  });
})();
