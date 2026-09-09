/* Local UI only. Financial calculations and authorization remain on the server. */
document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('[data-director-prompt]').forEach(button => {
    button.addEventListener('click', () => {
      const question = document.querySelector('#id_question');
      if (question) { question.value = button.dataset.directorPrompt; question.focus(); }
    });
  });
  document.querySelectorAll('[data-director-print]').forEach(button => {
    button.addEventListener('click', () => window.print());
  });
  document.querySelectorAll('.director-workspace [aria-current]').forEach(item => item.removeAttribute('aria-current'));
  document.querySelectorAll('.main-nav a.active').forEach(item => item.setAttribute('aria-current', 'page'));
});
