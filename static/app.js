document.addEventListener('DOMContentLoaded', () => {
  const sidebar = document.querySelector('#sidebar');
  const toggle = document.querySelector('[data-menu-toggle]');
  const backdrop = document.querySelector('[data-menu-close]');
  const setMenu = (open) => {
    if (!sidebar || !toggle || !backdrop) return;
    sidebar.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    backdrop.hidden = !open;
  };
  toggle?.addEventListener('click', () => setMenu(toggle.getAttribute('aria-expanded') !== 'true'));
  backdrop?.addEventListener('click', () => setMenu(false));
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') setMenu(false); });
  document.querySelectorAll('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => { if (!window.confirm(form.dataset.confirm)) event.preventDefault(); });
  });

  const chat = document.querySelector('[data-tax-chat]');
  if (!chat) return;
  const form = chat.querySelector('[data-chat-form]');
  const input = form.querySelector('textarea');
  const send = form.querySelector('button[type="submit"]');
  const history = chat.querySelector('[data-chat-history]');
  const status = chat.querySelector('[data-chat-status]');
  const counter = chat.querySelector('[data-chat-counter]');
  const welcome = chat.querySelector('[data-chat-welcome]');
  const conversationList = chat.querySelector('[data-conversation-list]');
  let conversationId = null;
  let busy = false;
  const canSend = !input.disabled;
  const apiUrl = new URL(chat.dataset.apiUrl, window.location.origin);
  const conversationUrl = (id) => new URL(`../conversations/${encodeURIComponent(id)}/`, apiUrl).href;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const setBusy = (value) => {
    busy = value;
    send.disabled = value || !canSend;
    input.readOnly = value;
    chat.querySelectorAll('[data-starter], [data-conversation-url], [data-new-chat]').forEach((button) => { button.disabled = value; });
  };
  const showCounter = () => { counter.textContent = `${input.value.length.toLocaleString('id-ID')}/2.000`; };
  input.addEventListener('input', showCounter);
  const chooseQuestion = (question) => {
    if (busy || !canSend) return;
    input.value = question.slice(0, 2000);
    showCounter();
    input.focus();
  };
  chat.querySelectorAll('[data-starter]').forEach((button) => {
    button.addEventListener('click', () => chooseQuestion(button.dataset.starter));
  });
  const renderMessage = (message) => {
    const isUser = message.role === 'user';
    const row = element('article', `chat-message ${isUser ? 'user' : 'assistant'}`);
    row.append(element('span', 'chat-message-avatar', isUser ? 'A' : 'o.'));
    const body = element('div', 'chat-message-content');
    body.append(element('div', 'chat-message-role', isUser ? 'Anda' : 'Asisten pajak OSEE'));
    body.append(element('p', '', message.content || message.answer || ''));
    if (!isUser) {
      if (message.mode) body.append(element('span', 'chat-mode', message.mode));
      if (message.review_required) body.append(element('span', 'pill pill-amber', 'Memerlukan pemeriksaan kondisi perusahaan'));
      if (Array.isArray(message.sources) && message.sources.length) {
        const sources = element('div', 'chat-sources');
        for (const source of message.sources) {
          try {
            const url = new URL(source.url);
            if (url.protocol !== 'https:') continue;
            const link = element('a', 'chat-source', `${source.title || 'Sumber resmi'} ↗`);
            link.href = url.href;
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.title = [source.article, source.reviewed_on ? `Ditinjau ${source.reviewed_on}` : ''].filter(Boolean).join(' · ');
            sources.append(link);
          } catch (_) { /* Invalid source URLs are not rendered as links. */ }
        }
        body.append(sources);
      }
      if (Array.isArray(message.followups) && message.followups.length) {
        const followups = element('div', 'chat-followups');
        for (const question of message.followups.slice(0, 4)) {
          if (typeof question !== 'string') continue;
          const button = element('button', '', question);
          button.type = 'button';
          button.addEventListener('click', () => chooseQuestion(question));
          followups.append(button);
        }
        body.append(followups);
      }
      if (message.notice) body.append(element('span', 'chat-mode', message.notice));
    }
    row.append(body);
    history.append(row);
    history.scrollTop = history.scrollHeight;
    return row;
  };
  const readResponse = async (response) => {
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('Sesi atau layanan tidak tersedia. Muat ulang halaman dan coba kembali.'); }
    if (!data || typeof data !== 'object') throw new Error('Respons layanan belum lengkap. Coba kembali nanti.');
    if (!response.ok || data.error) throw new Error(data.error || 'Permintaan belum dapat diproses. Coba kembali nanti.');
    return data;
  };
  const loadConversation = async (url) => {
    if (busy) return;
    const target = new URL(url, window.location.origin);
    if (target.origin !== window.location.origin) return;
    setBusy(true);
    status.textContent = 'Membuka percakapan…';
    try {
      const data = await readResponse(await fetch(target, { credentials: 'same-origin', headers: { Accept: 'application/json' } }));
      if (!Array.isArray(data.messages)) throw new Error('Percakapan belum dapat ditampilkan.');
      history.replaceChildren();
      conversationId = data.conversation_id;
      data.messages.forEach(renderMessage);
      status.textContent = '';
    } catch (error) { status.textContent = error.message; }
    finally { setBusy(false); }
  };
  conversationList.addEventListener('click', (event) => {
    const button = event.target.closest('[data-conversation-url]');
    if (button) loadConversation(button.dataset.conversationUrl);
  });
  chat.querySelector('[data-new-chat]').addEventListener('click', () => {
    if (busy) return;
    conversationId = null;
    history.replaceChildren();
    if (welcome) history.append(welcome);
    status.textContent = '';
    input.value = '';
    showCounter();
    if (canSend) input.focus();
  });
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy || !canSend) return;
    const question = input.value.trim();
    if (!question) return;
    if (question.length > 2000) { status.textContent = 'Pertanyaan maksimal 2.000 karakter.'; return; }
    if (apiUrl.origin !== window.location.origin) { status.textContent = 'Alamat layanan chat tidak valid.'; return; }
    setBusy(true);
    welcome?.remove();
    const userRow = renderMessage({ role: 'user', content: question });
    status.textContent = 'Mencari panduan dan sumber yang sesuai…';
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 45000);
    try {
      const payload = { message: question };
      if (conversationId) payload.conversation_id = conversationId;
      const response = await fetch(apiUrl, {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-CSRFToken': form.querySelector('[name="csrfmiddlewaretoken"]').value },
        body: JSON.stringify(payload),
      });
      const data = await readResponse(response);
      if (typeof data.answer !== 'string' || !data.answer.trim() || !data.conversation_id) throw new Error('Jawaban belum lengkap. Muat ulang riwayat sebelum mencoba kembali.');
      const isNew = !conversationId;
      conversationId = data.conversation_id;
      renderMessage({ ...data, role: 'assistant' });
      if (isNew) {
        conversationList.querySelector('[data-conversation-empty]')?.remove();
        const item = element('li');
        const button = element('button', '', question.slice(0, 100));
        button.type = 'button';
        button.dataset.conversationUrl = conversationUrl(conversationId);
        item.append(button);
        conversationList.prepend(item);
      }
      input.value = '';
      showCounter();
      status.textContent = '';
    } catch (error) {
      userRow.remove();
      status.textContent = error.name === 'AbortError' ? 'Layanan memerlukan waktu lebih lama. Periksa riwayat sebelum mengirim ulang pertanyaan.' : (error.message || 'Jawaban belum tersedia. Periksa riwayat sebelum mencoba kembali.');
    } finally { window.clearTimeout(timeout); setBusy(false); input.focus(); }
  });
});
