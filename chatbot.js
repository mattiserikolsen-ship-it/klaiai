/**
 * KlarAI Chatbot Widget
 * Embed: <script src="https://klaiai.onrender.com/chatbot.js" data-client="KLIENT_ID"></script>
 */
(function () {
  const script = document.currentScript || document.querySelector('script[data-client],script[data-demo]');
  const CLIENT_ID = script?.getAttribute('data-client') || 'demo';
  const DEMO_ID   = script?.getAttribute('data-demo') || null;
  const COLOR = script?.getAttribute('data-color') || '#0a2463';
  const API_URL = script?.getAttribute('data-api') || 'https://klaiai.onrender.com';
  const AUTO_OPEN_DELAY = DEMO_ID ? 600 : 1500;

  let history = [];
  let isOpen = false;
  let hasAutoOpened = false;
  let config = { navn: 'Alma', velkomst: 'Hej! Hvordan kan jeg hjælpe dig?', farve: COLOR };

  // ── STYLES ──────────────────────────────────────────
  const style = document.createElement('style');
  style.textContent = `
    #klaiai-widget * { box-sizing: border-box; font-family: -apple-system, 'Inter', sans-serif; }

    /* Minimeret fane */
    #klaiai-tab {
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      display: flex; align-items: center; gap: 10px;
      background: ${COLOR}; color: #fff;
      border: none; border-radius: 50px;
      padding: 12px 20px 12px 14px;
      cursor: pointer;
      box-shadow: 0 4px 20px rgba(0,0,0,0.22);
      transition: transform .2s, box-shadow .2s, opacity .3s;
      font-size: 14px; font-weight: 600;
    }
    #klaiai-tab:hover { transform: translateY(-2px); box-shadow: 0 8px 28px rgba(0,0,0,0.28); }
    #klaiai-tab-avatar {
      width: 32px; height: 32px; border-radius: 50%;
      background: rgba(255,255,255,0.2);
      display: flex; align-items: center; justify-content: center;
      font-size: 16px; flex-shrink: 0;
    }
    #klaiai-tab-label { display: flex; flex-direction: column; line-height: 1.2; }
    #klaiai-tab-name { font-size: 13px; font-weight: 700; }
    #klaiai-tab-hint { font-size: 11px; opacity: .75; font-weight: 400; }
    #klaiai-tab-dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: #4ade80; margin-left: 2px; flex-shrink: 0;
      box-shadow: 0 0 0 2px rgba(74,222,128,0.3);
      animation: klaiai-pulse 2s infinite;
    }
    @keyframes klaiai-pulse { 0%,100%{opacity:1} 50%{opacity:.5} }

    /* Chat-vindue */
    #klaiai-box {
      position: fixed; bottom: 24px; right: 24px; z-index: 9999;
      width: 360px; max-height: 540px;
      background: #fff; border-radius: 20px;
      box-shadow: 0 8px 40px rgba(0,0,0,0.18);
      display: flex; flex-direction: column;
      overflow: hidden;
      transition: opacity .25s, transform .25s;
      opacity: 0; transform: translateY(16px) scale(.96); pointer-events: none;
    }
    #klaiai-box.open {
      opacity: 1; transform: translateY(0) scale(1); pointer-events: all;
    }
    #klaiai-header {
      background: ${COLOR}; color: #fff;
      padding: 14px 16px; display: flex; align-items: center; gap: 10px;
      flex-shrink: 0;
    }
    #klaiai-avatar {
      width: 36px; height: 36px; border-radius: 50%;
      background: rgba(255,255,255,0.2);
      display: flex; align-items: center; justify-content: center;
      font-size: 18px; flex-shrink: 0;
    }
    #klaiai-header-info { flex: 1; }
    #klaiai-header-name { font-weight: 700; font-size: 15px; }
    #klaiai-header-status { font-size: 11px; opacity: .75; margin-top: 1px; }
    #klaiai-minimize {
      background: none; border: none; color: #fff;
      cursor: pointer; font-size: 18px; opacity: .7;
      padding: 4px 6px; border-radius: 6px; line-height: 1;
      transition: opacity .15s, background .15s;
    }
    #klaiai-minimize:hover { opacity: 1; background: rgba(255,255,255,0.15); }
    #klaiai-messages {
      flex: 1; overflow-y: auto; padding: 14px 14px 8px;
      display: flex; flex-direction: column; gap: 10px;
      min-height: 260px; max-height: 360px;
    }
    .klaiai-msg { display: flex; flex-direction: column; max-width: 84%; }
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
      padding: 10px 14px; border-top: 1px solid #f0f0f0;
      display: flex; gap: 8px; align-items: center; flex-shrink: 0;
    }
    #klaiai-input {
      flex: 1; border: 1px solid #e5e7eb; border-radius: 12px;
      padding: 9px 14px; font-size: 14px; outline: none;
      transition: border-color .2s; resize: none; font-family: inherit;
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
      padding: 5px; border-top: 1px solid #f5f5f5; flex-shrink: 0;
    }
    @media (max-width: 400px) {
      #klaiai-box { width: calc(100vw - 16px); right: 8px; bottom: 8px; border-radius: 16px; }
      #klaiai-tab { right: 8px; bottom: 8px; }
    }
  `;
  document.head.appendChild(style);

  // ── HTML ─────────────────────────────────────────────
  const widget = document.createElement('div');
  widget.id = 'klaiai-widget';
  widget.innerHTML = `
    <button id="klaiai-tab" aria-label="Åbn chat">
      <div id="klaiai-tab-avatar">🤖</div>
      <div id="klaiai-tab-label">
        <span id="klaiai-tab-name">Alma</span>
        <span id="klaiai-tab-hint">Skriv til os her</span>
      </div>
      <div id="klaiai-tab-dot"></div>
    </button>
    <div id="klaiai-box" role="dialog" aria-label="Chat">
      <div id="klaiai-header">
        <div id="klaiai-avatar">🤖</div>
        <div id="klaiai-header-info">
          <div id="klaiai-header-name">Alma</div>
          <div id="klaiai-header-status">● Online — svarer straks</div>
        </div>
        <button id="klaiai-minimize" title="Minimér">⌄</button>
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
  const tab  = document.getElementById('klaiai-tab');
  const box  = document.getElementById('klaiai-box');
  const msgs = document.getElementById('klaiai-messages');
  const input = document.getElementById('klaiai-input');
  const send  = document.getElementById('klaiai-send');

  // ── OPEN / CLOSE ─────────────────────────────────────
  function openChat() {
    isOpen = true;
    tab.style.display = 'none';
    box.classList.add('open');
    input.focus();
  }

  function closeChat() {
    isOpen = false;
    box.classList.remove('open');
    tab.style.display = 'flex';
  }

  tab.addEventListener('click', openChat);
  document.getElementById('klaiai-minimize').addEventListener('click', closeChat);

  // ── INIT ─────────────────────────────────────────────
  async function init() {
    try {
      const url = DEMO_ID
        ? `${API_URL}/demo/config/${DEMO_ID}`
        : `${API_URL}/widget/${CLIENT_ID}`;
      const res = await fetch(url);
      if (res.ok) {
        config = await res.json();
        const botNavn = config.navn || 'Alma';
        document.getElementById('klaiai-header-name').textContent = botNavn;
        document.getElementById('klaiai-tab-name').textContent = botNavn;
        // Anvend dynamisk farve fra demo config
        if (config.farve && config.farve !== COLOR) {
          document.querySelectorAll('[style*="background: ' + COLOR + '"], [style*="background:' + COLOR + '"]').forEach(el => {
            el.style.background = config.farve;
          });
        }
      }
    } catch (e) { /* bruger default config */ }

    addMsg('bot', config.velkomst);

    // Auto-åbn efter kort forsinkelse
    setTimeout(() => {
      if (!hasAutoOpened) {
        hasAutoOpened = true;
        openChat();
      }
    }, AUTO_OPEN_DELAY);
  }

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
        body: JSON.stringify({
          client: DEMO_ID ? 'demo' : CLIENT_ID,
          demo_id: DEMO_ID || null,
          message: text,
          history: history.slice(-10)
        })
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
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });

  // ── HELPERS ──────────────────────────────────────────
  function parseMarkdown(text) {
    return text
      .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
      .replace(/\*(.*?)\*/g, '<em>$1</em>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\)]+)\)/g, '<a href="$2" target="_blank" style="color:inherit;text-decoration:underline">$1</a>')
      .replace(/(https?:\/\/[^\s<"]+)/g, (url) => `<a href="${url}" target="_blank" style="color:inherit;text-decoration:underline">${url}</a>`)
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
