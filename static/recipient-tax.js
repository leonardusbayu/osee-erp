(() => {
  'use strict';
  function toggle(root, names, visible) {
    names.forEach(name => {
      root.querySelectorAll(`[name="${name}"]`).forEach(control => {
        const row = control.closest('.field-row');
        if (!row) return;
        if (control.dataset.taxOriginalDisabled === undefined) control.dataset.taxOriginalDisabled = String(control.disabled);
        row.hidden = !visible;
        control.disabled = !visible || control.dataset.taxOriginalDisabled === 'true';
      });
    });
  }
  document.querySelectorAll('[data-recipient-preparation]').forEach(form => {
    const target = form.querySelector('[name="target"]');
    const kind = form.querySelector('[name="recipient_type"]');
    const type = form.querySelector('[name="identity_type"]');
    const refresh = () => {
      toggle(form, ['recipient_party', 'name_variation_reason'], target.value === 'supplier');
      toggle(form, ['nik_validated', 'nik_integrated', 'nik_activated'], kind.value === 'person' && type.value === 'nik');
    };
    [target, kind, type].forEach(control => control.addEventListener('change', refresh));
    refresh();
  });
  document.querySelectorAll('[data-recipient-tax]').forEach(form => {
    const mode = form.dataset.recipientTax;
    const rule = form.querySelector('[name="rule_code"]');
    const group = form.querySelector('[data-pph23-group]');
    const certificate = form.querySelector('[data-pph23-credit-group]');
    const direction = form.querySelector('[name="direction"]');
    const kind = form.querySelector('[name="kind"]');
    const refresh = () => {
      if (group) {
        const active = !!rule && rule.value.startsWith('pph23_');
        group.hidden = !active;
        toggle(group, Array.from(group.querySelectorAll('[name]')).map(control => control.name), active);
        if (active && mode === 'obligation' && direction) {
          toggle(group, ['recipient_party'], direction.value === 'payable');
          toggle(group, ['withholder_party'], direction.value === 'receivable');
        }
        if (mode === 'obligation' && direction) toggle(form, ['source_invoice'], active && direction.value === 'receivable');
      }
      if (certificate) {
        const active = !!kind && kind.value === 'credit';
        certificate.hidden = !active;
        toggle(certificate, Array.from(certificate.querySelectorAll('[name]')).map(control => control.name), active);
      }
    };
    [rule, direction, kind].filter(Boolean).forEach(control => control.addEventListener('change', refresh));
    refresh();
  });
})();
