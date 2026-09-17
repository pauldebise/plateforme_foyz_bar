const deleteModal = document.getElementById('del-modal');
if (deleteModal) {
  deleteModal.addEventListener('show.bs.modal', (event) => {
    document.getElementById('del-nid').value = event.relatedTarget.dataset.nid;
  });
}
