const cancelModal = document.getElementById('cancel-modal');
if (cancelModal) {
  cancelModal.addEventListener('show.bs.modal', (event) => {
    const button = event.relatedTarget;
    document.getElementById('cancel-tid').value = button.dataset.tid;
    document.getElementById('cancel-total').textContent = button.dataset.total;
  });
}

// Annulation multiple (U8) : compteur de sélection et « tout sélectionner ».
const checks = Array.from(document.querySelectorAll('.row-check'));
const bulkButton = document.getElementById('bulk-cancel-button');
const bulkCount = document.getElementById('bulk-count');
const bulkModalCount = document.getElementById('bulk-modal-count');
const checkAll = document.getElementById('check-all');

function selectedCount() {
  return checks.filter((c) => c.checked).length;
}

function refreshSelection() {
  const n = selectedCount();
  if (bulkCount) bulkCount.textContent = n;
  if (bulkModalCount) bulkModalCount.textContent = n;
  if (bulkButton) bulkButton.disabled = n === 0;
  if (checkAll) {
    checkAll.checked = n > 0 && n === checks.length;
    checkAll.indeterminate = n > 0 && n < checks.length;
  }
}

checks.forEach((c) => c.addEventListener('change', refreshSelection));
if (checkAll) {
  checkAll.addEventListener('change', () => {
    checks.forEach((c) => { c.checked = checkAll.checked; });
    refreshSelection();
  });
}
refreshSelection();
