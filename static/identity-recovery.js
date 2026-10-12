(() => {
  const form = document.getElementById('identity-recovery-form');
  if (!form) return;
  const target = form.elements.target_invoice;
  const update = () => {
    const reset = form.elements.action.value === 'reset';
    const option = target.selectedOptions[0];
    if (reset) target.value = '';
    target.disabled = reset;
    document.getElementById('identity-target-field').hidden = reset;
    // Names and labels are server-escaped option data. Never insert HTML.
    for (const field of ['party', 'product']) {
      const result = form.dataset[field + 'Eligible'] === 'yes'
        ? (reset ? 'Belum diketahui' : option?.dataset[field] || 'Pilih invoice yang benar.')
        : form.dataset[field];
      document.getElementById('identity-after-' + field).textContent = result;
    }
    document.getElementById('identity-after-invoice').textContent = reset
      ? 'Belum ada invoice terhubung' : option?.dataset.number || 'Pilih invoice yang benar.';
  };
  form.addEventListener('change', update);
  update();
})();
