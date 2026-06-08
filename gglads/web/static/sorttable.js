// Lightweight client-side sort + filter for `.data-table.sortable`.
// Click a header (unless it has data-nosort) to sort; numeric-aware. An
// optional `<input class="tbl-filter" data-table="ID">` filters its rows.
(function () {
  function cellValue(td) {
    if (!td) return '';
    const raw = td.textContent.trim();
    const num = parseFloat(raw.replace(/[^0-9.\-]/g, ''));
    return (raw !== '' && !isNaN(num) && /[0-9]/.test(raw)) ? num : raw.toLowerCase();
  }
  function wireTable(table) {
    const tbody = table.querySelector('tbody');
    if (!tbody) return;
    const headers = Array.from(table.querySelectorAll('thead th'));
    headers.forEach((th, idx) => {
      if (th.hasAttribute('data-nosort')) return;
      th.classList.add('th-sortable');
      let asc = false;
      th.addEventListener('click', () => {
        asc = !asc;
        const rows = Array.from(tbody.querySelectorAll('tr'));
        rows.sort((a, b) => {
          const av = cellValue(a.cells[idx]), bv = cellValue(b.cells[idx]);
          if (av < bv) return asc ? -1 : 1;
          if (av > bv) return asc ? 1 : -1;
          return 0;
        });
        rows.forEach(r => tbody.appendChild(r));
        headers.forEach(h => h.classList.remove('sorted-asc', 'sorted-desc'));
        th.classList.add(asc ? 'sorted-asc' : 'sorted-desc');
      });
    });
  }
  function wireFilter(input) {
    const table = document.getElementById(input.dataset.table);
    if (!table) return;
    const tbody = table.querySelector('tbody');
    input.addEventListener('input', () => {
      const q = input.value.toLowerCase();
      tbody.querySelectorAll('tr').forEach(r => {
        r.style.display = r.textContent.toLowerCase().includes(q) ? '' : 'none';
      });
    });
  }
  document.querySelectorAll('table.data-table.sortable').forEach(wireTable);
  document.querySelectorAll('input.tbl-filter').forEach(wireFilter);
})();
