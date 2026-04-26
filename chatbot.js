/**
 * KlarAI Chatbot Widget
 * Embed: <script src="https://klaiai.dk/chatbot.js" data-client="KLIENT_ID" data-color="#0a2463"></script>
 */
(function () {
  const script = document.currentScript || document.querySelector('script[data-client]');
  const CLIENT_ID = script?.getAttribute('data-client') || 'demo';
  const COLOR = script?.getAttribute('data-color') || '#0a2463';
  const API_URL = script?.getAttribute('data-api') || 'https://klaiai.onrender.com';

  let history = [];
  let isOpen = false;
  let config = { navn: 'Alma', velkomst: 'Hej! Hvordan kan jeg hjælpe dig?', farve: COLOR };

  // ── STYLES ──────────────────────────────────────────
  const style = document.createElement('style');
  style.textContent = `
    #klaiai-widget * { box-sizing: border-box; font-family: -apple-system, 'Inter', sans-serif; }
    #klaiai-btn {
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      width: 56px; height: 56px; border-radius: 50%;
      background: ${COLOR}; color: #fff; border: none; cursor: pointer;
      box-shadow: 0 4px 20px rgba(0,0,0,0.2);
      display: flex; align-items: center; justify-content: center;
      font-size: 24px; transition: transform .2s, box-shadow .2s;
    }
    #klaiai-btn:hover { transform: scale(1.08); box-shadow: 0 6px 28px rgba(0,0,0,0.25); }
    #klaiai-box {
      position: fixed; bottom: 92px; right: 24px; z-index: 9999;
      width: 360px; max-height: 520px;
      background: #fff; border-radius: 20px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.15);
      display: flex; flex-direction: column;
      overflow: hidden; transition: opacity .2s, transform .2s;
      opacity: 0; transform: translateY(12px) scale(.97); pointer-events: none;
    }
    #klaiai-box.open { opacity: 1; transform: translateY(0) scale(1); pointer-events: all; }
    #klaiai-header {
      background: ${COLOR}; color: #fff;
      padding: 16px 18px; display: flex; align-items: center; gap: 10px;
    }
    #klaiai-avatar {
      width: 36px; height: 36px; border-radius: 50%;
      background: rgba(255,255,255,0.2);
      display: flex; align-items: center; justify-content: center;
      font-size: 18px; flex-shrink: 0;
    }
    #klaiai-header-name { font-weight: 700; font-size: 15px; }
    #klaiai-header-status { font-size: 12px; opacity: .75; margin-top: 1px; }
    #klaiai-close {
      margin-left: auto; background: none; border: none; color: #fff;
      cursor: pointer; font-size: 20px; opacity: .7; padding: 0;
    }
    #klaiai-close:hover { opacity: 1; }
    #klaiai-messages {
      flex: 1; overflow-y: auto; padding: 16px;
      display: flex; flex-direction: column; gap: 10px;
      min-height: 280px; max-height: 340px;
    }
    .klaiai-msg { display: flex; flex-direction: column; max-width: 82%; }
    .klaiai-msg.bot { align-self: flex-start; }
    .klaiai-msg.user { align-self: flex-end; }
    .klaiai-bubble {
      padding: 10px 14px; border-radius: 16px; font-size: 14px; line-height: 1.5;
    }
    .klaiai-msg.bot .klaiai-bubble {
      background: #f3f4f6; color: #111; border-bottom-left-radius: 4px;
    }
    .klaiai-msg.user .klaiai-bubble {
      background: ${COLOR}; color: #fff; border-bottom-right-radius: 4px;
    }
    .klaiai-time { font-size: 11px; color: #aaa; margin-top: 3px; }
    .klaiai-msg.user .klaiai-time { text-align: right; }
    .klaiai-typing { display: flex; gap: 4px; align-items: center; padding: 10px 14px; }
    .klaiai-dot {
      width: 7px; height: 7px; background: #bbb; border-radius: 50%;
      animation: klaiai-bounce 1.2s infinite;
    }
    .klaiai-dot:nth-child(2) { animation-delay: .2s; }
    .klaiai-dot:nth-child(3) { animation-delay: .4s; }
    @keyframes klaiai-bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-6px)} }
    #klaiai-input-row {
      padding: 12px 16px; border-top: 1px solid #f0f0f0;
      display: flex; gap: 8px; align-items: center;
    }
    #klaiai-input {
      flex: 1; border: 1px solid #e5e7eb; border-radius: 12px;
      padding: 9px 14px; font-size: 14px; outline: none;
      transition: border-color .2s; resize: none;
    }
    #klaiai-input:focus { border-color: ${COLOR}; }
    #klaiai-send {
      width: 38px; height: 38px; border-radius: 10px;
      background: ${COLOR}; color: #fff; border: none;
      cursor: pointer; display: flex; align-items: center; justify-content: center;
      font-size: 16px; flex-shrink: 0; transition: opacity .2s;
    }
    #klaiai-send:hover { opacity: .85; }
    #klaiai-powered {
      text-align: center; font-size: 11px; color: #ccc;
      padding: 6px; border-top: 1px solid #f5f5f5;
    }
  `;
  document.head.appendChild(style);

  // ── HTML ─────────────────────────────────────────────
  const widget = document.createElement('div');
  widget.id = 'klaiai-widget';
  widget.innerHTML = `
    <button id="klaiai-btn" title="Chat med os">💬</button>
    <div id="klaiai-box">
      <div id="klaiai-header">
        <div id="klaiai-avatar">🤖</div>
        <div>
          <div id="klaiai-header-name">Alma</div>
          <div id="klaiai-header-status">● Online — svarer straks</div>
        </div>
        <button id="klaiai-close">✕</button>
      </div>
      <div id="klaiai-messages"></div>
      <div id="klaiai-input-row">
        <input id="klaiai-input" placeholder="Skriv en besked..." autocomplete="off"/>
        <button id="klaiai-send">➤</button>
      </div>
      <div id="klaiai-powered">Drevet af <strong>KlarAI</strong></div>
    </div>
  `;
  document.body.appendChild(widget);

  // ── REFS ─────────────────────────────────────────────
  const btn = document.getElementById('klaiai-btn');
  const box = document.getElementById('klaiai-box');
  const msgs = document.getElementById('klaiai-messages');
  const input = document.getElementById('klaiai-input');
  const send = document.getElementById('klaiai-send');

  // ── INIT ─────────────────────────────────────────────
  async function init() {
    try {
      const res = await fetch(`${API_URL}/widget/${CLIENT_ID}`);
      if (res.ok) {
        config = await res.json();
        document.getElementById('klaiai-header-name').textContent = config.navn;
      }
    } catch (e) { /* bruger default config */ }
    addMsg('bot', config.velkomst);
  }

  // ── TOGGLE ───────────────────────────────────────────
  btn.addEventListener('click', () => {
    isOpen = !isOpen;
    box.classList.toggle('open', isOpen);
    btn.textContent = isOpen ? '✕' : '💬';
    if (isOpen) input.focus();
  });

  document.getElementById('klaiai-close').addEventListener('click', () => {
    isOpen = false;
    box.classList.remove('open');
    btn.textContent = '💬';
  });

  // ── SEND ─────────────────────────────────────────────
  async function sendMessage() {
    const text = input.value.trim();
    if (!text) return;
    input.value = '';

    addMsg('user', text);
    history.push({ role: 'user', content: text });

    const typing = showTyping();

    try {
      const res = await fetch(`${API_URL}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client: CLIENT_ID, message: text, history: history.slice(-8) })
      });
      const data = await res.json();
      removeTyping(typing);

      if (data.reply) {
        addMsg('bot', data.reply);
        history.push({ role: 'assistant', content: data.reply });
        if (data.lead_gemt) {
          setTimeout(() => {
            const note = document.createElement('div');
            note.style.cssText = 'text-align:center;font-size:.7rem;color:#6b7a99;padding:.4rem .75rem;margin:.25rem 0;';
            note.textContent = '✓ Dine oplysninger er gemt — vi kontakter dig snarest';
            msgs.appendChild(note);
            msgs.scrollTop = msgs.scrollHeight;
          }, 400);
        }
      }
    } catch (e) {
      removeTyping(typing);
      addMsg('bot', 'Beklager, jeg kunne ikke forbinde lige nu. Prøv igen om lidt.');
    }
  }

  send.addEventListener('click', sendMessage);
  input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); } });

  // ── HELPERS ──────────────────────────────────────────
  function parseMarkdown(text) {
    return text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, '<a href="$2" target="_blank" style="color:inherit;text-decoration:underline">$1</a>')
      .replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" style="color:inherit;text-decoration:underline">$1</a>')
      .replace(/\n/g, '<br>');
  }

  function addMsg(type, text) {
    const time = new Date().toLocaleTimeString('da-DK', { hour: '2-digit', minute: '2-digit' });
    const el = document.createElement('div');
    el.className = `klaiai-msg ${type}`;
    const html = type === 'bot' ? parseMarkdown(text) : text.replace(/</g, '&lt;');
    el.innerHTML = `<div class="klaiai-bubble">${html}</div><div class="klaiai-time">${time}</div>`;
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function showTyping() {
    const el = document.createElement('div');
    el.className = 'klaiai-msg bot';
    el.innerHTML = `<div class="klaiai-bubble klaiai-typing"><div class="klaiai-dot"></div><div class="klaiai-dot"></div><div class="klaiai-dot"></div></div>`;
    msgs.appendChild(el);
    msgs.scrollTop = msgs.scrollHeight;
    return el;
  }

  function removeTyping(el) { el?.remove(); }

  init();
})();
