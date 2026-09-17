const cancelModal = document.getElementById('cancel-modal');
if (cancelModal) {
  cancelModal.addEventListener('show.bs.modal', (event) => {
    const button = event.relatedTarget;
    document.getElementById('cancel-tid').value = button.dataset.tid;
    document.getElementById('cancel-total').textContent = button.dataset.total;
  });
}
