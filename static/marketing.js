/* Native forms remain usable without JavaScript. Avoid duplicate clicks while saving. */
document.addEventListener("submit", function (event) {
  const form = event.target;
  if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== "post" || !form.closest(".marketing-workspace")) return;
  const button = event.submitter;
  if (!(button instanceof HTMLButtonElement)) return;
  window.setTimeout(function () { button.disabled = true; button.setAttribute("aria-busy", "true"); }, 0);
});
window.addEventListener("pageshow", function () {
  document.querySelectorAll(".marketing-workspace button[aria-busy='true']").forEach(function (button) {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  });
});
