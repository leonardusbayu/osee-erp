"use strict";

document.querySelectorAll("[data-monthly-review]").forEach((form) => {
  const rows = form.querySelector("[data-adjustment-rows]");
  const template = form.querySelector("[data-adjustment-template]");
  const count = form.querySelector('[name="adjustments-TOTAL_FORMS"]');
  const maximum = Number(form.querySelector('[name="adjustments-MAX_NUM_FORMS"]').value);
  const note = form.querySelector("[data-adjustment-note]");
  const add = form.querySelector("[data-add-adjustment]");
  const renumber = () => {
    rows.querySelectorAll("[data-adjustment]").forEach((row, index) => {
      row.querySelectorAll("[name], [id], [for]").forEach((element) => {
        for (const attribute of ["name", "id", "for"]) {
          if (element.hasAttribute(attribute)) element.setAttribute(attribute, element.getAttribute(attribute).replace(/adjustments-\d+-/g, `adjustments-${index}-`));
        }
      });
    });
    count.value = String(rows.querySelectorAll("[data-adjustment]").length);
  };
  add.addEventListener("click", () => {
    const index = Number(count.value);
    if (index >= maximum) {
      note.textContent = `Paket ini mendukung ${maximum} penyesuaian. Hubungi penanggung jawab aplikasi jika masih ada penyesuaian lain; jangan gabungkan invoice berbeda atau menyetujui sumber yang belum lengkap.`;
      return;
    }
    const fragment = template.content.cloneNode(true);
    fragment.querySelectorAll("[name], [id], [for]").forEach((element) => {
      for (const attribute of ["name", "id", "for"]) {
        if (element.hasAttribute(attribute)) element.setAttribute(attribute, element.getAttribute(attribute).replaceAll("__prefix__", String(index)));
      }
    });
    rows.appendChild(fragment);
    count.value = String(index + 1);
    rows.lastElementChild.querySelector("select").focus();
    note.textContent = "Penyesuaian ditambahkan. Isi selisih, alasan, bukti dan transaksi pendukungnya.";
  });
  rows.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-adjustment]");
    if (!button) return;
    const row = button.closest("[data-adjustment]");
    row.remove();
    renumber();
    add.focus();
    note.textContent = "Penyesuaian dihapus dari pengajuan ini.";
  });
  // These are unsaved form rows. Reclaim their slots without touching saved history.
  rows.querySelectorAll("[data-adjustment]").forEach((row) => {
    if (row.querySelector('[name$="-DELETE"]').checked) row.remove();
  });
  renumber();
  form.addEventListener("submit", () => {
    const choices = [];
    form.querySelectorAll('[data-source-choice="decision"]').forEach((element) => {
      const reason = form.querySelector(`[data-source-choice="reason"][data-source-index="${element.dataset.sourceIndex}"]`);
      choices.push({ decision: element.value, reason: reason.value });
    });
    let payload = form.querySelector('[name="row_choices"]');
    if (!payload) {
      payload = document.createElement("input");
      payload.type = "hidden";
      payload.name = "row_choices";
      form.appendChild(payload);
    }
    payload.value = JSON.stringify(choices);
    form.querySelectorAll("[data-source-choice]").forEach((element) => element.removeAttribute("name"));
    const adjustmentInputs = [];
    rows.querySelectorAll("[data-adjustment]").forEach((row) => {
      if (row.querySelector('[name$="-DELETE"]').checked) return;
      const values = {};
      for (const name of ["kind", "amount", "reason", "evidence"]) values[name] = row.querySelector(`[name$="-${name}"]`).value;
      values.source_row_ids = [...row.querySelectorAll('[name$="-source_row_ids"]:checked')].map(element => element.value);
      adjustmentInputs.push(values);
    });
    let adjustmentPayload = form.querySelector('[name="adjustment_inputs"]');
    if (!adjustmentPayload) {
      adjustmentPayload = document.createElement("input");
      adjustmentPayload.type = "hidden";
      adjustmentPayload.name = "adjustment_inputs";
      form.appendChild(adjustmentPayload);
    }
    adjustmentPayload.value = JSON.stringify(adjustmentInputs);
    form.querySelectorAll('[name^="adjustments-"]').forEach(element => element.removeAttribute("name"));
  });
});
