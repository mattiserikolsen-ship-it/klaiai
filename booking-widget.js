/**
 * SittamTech Booking Widget
 * Embed: <script src="https://klaiai.dk/booking-widget.js" data-client="KLIENT_ID"></script>
 */
(function () {
  const script = document.currentScript || document.querySelector('script[data-client-booking]');
  const CLIENT_ID = script?.getAttribute('data-client') || 'demo';
  const API_URL = script?.getAttribute('data-api') || 'https://klaiai.onrender.com';

  let cfg = {
    titel: 'Book en tid',
    farve: '#0a2463',
    ydelser: ['Konsultation'],
    dage: [1,2,3,4,5],
    start_tid: '09:00',
    slut_tid: '17:00',
    varighed: 60,
    buffer: 0
  };

  let valgtDato = null;
  let valgtTid = null;
  let valgtYdelse = null;
  let step = 1; // 1=dato, 2=tid+ydelse, 3=info, 4=bekræftet
  let optaget = []; // optagne tider for valgt dato

  // ── STYLES ──────────────────────────────────────────
  const style = document.createElement('style');
  style.textContent = `
    .klai-bw * { box-sizing: border-box; font-family: -apple-system,'Inter',sans-serif; }
    .klai-bw {
      background: #fff; border-radius: 16px; padding: 1.75rem;
      max-width: 520px; width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,0.08);
      border: 1px solid rgba(0,0,0,0.06);
    }
    .klai-bw-title { font-size: 1.2rem; font-weight: 700; color: #0a1628; margin-bottom: 1.25rem; }
    .klai-steps { display: flex; gap: .5rem; margin-bottom: 1.5rem; }
    .klai-step {
      flex: 1; height: 4px; border-radius: 2px; background: #e5e7eb;
      transition: background .3s;
    }
    .klai-step.active { background: var(--klai-color, #0a2463); }
    .klai-cal-header {
      display: flex; align-items: center; justify-content: space-between;
      margin-bottom: .75rem;
    }
    .klai-cal-header button {
      background: none; border: 1.5px solid #e5e7eb; border-radius: 8px;
      width: 32px; height: 32px; cursor: pointer; font-size: 14px;
      display: flex; align-items: center; justify-content: center;
      transition: border-color .2s;
    }
    .klai-cal-header button:hover { border-color: var(--klai-color, #0a2463); }
    .klai-cal-month { font-weight: 600; font-size: .9rem; }
    .klai-cal-grid {
      display: grid; grid-template-columns: repeat(7, 1fr); gap: 4px;
    }
    .klai-cal-day-label {
      text-align: center; font-size: .7rem; font-weight: 600;
      color: #9ca3af; padding: .3rem 0;
    }
    .klai-cal-day {
      aspect-ratio: 1; display: flex; align-items: center; justify-content: center;
      border-radius: 8px; font-size: .82rem; cursor: pointer;
      transition: background .15s, color .15s;
      border: 1.5px solid transparent;
    }
    .klai-cal-day.empty { cursor: default; }
    .klai-cal-day.disabled { color: #d1d5db; cursor: default; }
    .klai-cal-day.available:hover { background: #eff6ff; border-color: var(--klai-color,#0a2463); color: var(--klai-color,#0a2463); }
    .klai-cal-day.selected { background: var(--klai-color,#0a2463) !important; color: #fff !important; }
    .klai-cal-day.today { font-weight: 700; }
    .klai-slots { display: grid; grid-template-columns: repeat(3,1fr); gap: .5rem; margin-top: .75rem; }
    .klai-slot {
      padding: .5rem; border: 1.5px solid #e5e7eb; border-radius: 8px;
      text-align: center; font-size: .82rem; cursor: pointer; font-weight: 500;
      transition: all .15s;
    }
    .klai-slot:hover { border-color: var(--klai-color,#0a2463); color: var(--klai-color,#0a2463); }
    .klai-slot.selected { background: var(--klai-color,#0a2463); color: #fff; border-color: var(--klai-color,#0a2463); }
    .klai-ydelse-list { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .75rem; }
    .klai-ydelse {
      padding: .45rem .9rem; border: 1.5px solid #e5e7eb; border-radius: 20px;
      font-size: .82rem; cursor: pointer; transition: all .15s;
    }
    .klai-ydelse:hover { border-color: var(--klai-color,#0a2463); color: var(--klai-color,#0a2463); }
    .klai-ydelse.selected { background: var(--klai-color,#0a2463); color: #fff; border-color: var(--klai-color,#0a2463); }
    .klai-label { font-size: .78rem; font-weight: 600; color: #374151; margin-bottom: .3rem; display: block; }
    .klai-input {
      width: 100%; border: 1.5px solid #e5e7eb; border-radius: 10px;
      padding: .65rem .9rem; font-size: .875rem; outline: none;
      transition: border-color .2s; background: #fafafa; margin-bottom: .75rem;
    }
    .klai-input:focus { border-color: var(--klai-color,#0a2463); background: #fff; }
    .klai-row { display: grid; grid-template-columns: 1fr 1fr; gap: .75rem; }
    .klai-btn {
      width: 100%; padding: .8rem; border-radius: 10px; border: none;
      font-size: .9rem; font-weight: 600; cursor: pointer; margin-top: .5rem;
      transition: opacity .2s;
    }
    .klai-btn-primary { background: var(--klai-color,#0a2463); color: #fff; }
    .klai-btn-primary:hover { opacity: .88; }
    .klai-btn-primary:disabled { opacity: .5; cursor: default; }
    .klai-btn-secondary {
      background: none; border: 1.5px solid #e5e7eb; color: #374151;
      margin-top: .5rem;
    }
    .klai-summary {
      background: #f8faff; border: 1px solid #dbeafe; border-radius: 10px;
      padding: 1rem; margin-bottom: 1rem; font-size: .85rem; line-height: 1.8;
    }
    .klai-success-icon { font-size: 2.5rem; text-align: center; margin-bottom: .75rem; }
    .klai-success-title { font-size: 1.1rem; font-weight: 700; text-align: center; margin-bottom: .5rem; }
    .klai-success-sub { font-size: .85rem; color: #6b7a99; text-align: center; line-height: 1.6; }
    .klai-section-title { font-size: .78rem; font-weight: 700; text-transform: uppercase; letter-spacing: .5px; color: #9ca3af; margin-bottom: .6rem; }
    .klai-powered { text-align: center; font-size: .7rem; color: #ccc; margin-top: 1rem; }
    .klai-error { background:#fee2e2; border:1px solid #fca5a5; border-radius:8px; padding:.6rem .9rem; color:#991b1b; font-size:.82rem; margin-top:.5rem; }
  `;
  document.head.appendChild(style);

  // ── MOUNT ─────────────────────────────────────────────
  const wrap = document.createElement('div');
  wrap.className = 'klai-bw';
  script.parentNode.insertBefore(wrap, script.nextSibling);

  // ── INIT ─────────────────────────────────────────────
  async function init() {
    try {
      const res = await fetch(`${API_URL}/booking-config/${CLIENT_ID}`);
      if (res.ok) {
        const data = await res.json();
        Object.assign(cfg, data);
      }
    } catch(e) {}
    wrap.style.setProperty('--klai-color', cfg.farve);
    render();
  }

  // ── RENDER ────────────────────────────────────────────
  function render() {
    if (step === 1) renderDato();
    else if (step === 2) renderTidYdelse();
    else if (step === 3) renderInfo();
    else if (step === 4) renderBekraeftet();
  }

  function stepsHtml() {
    return `<div class="klai-steps">
      ${[1,2,3].map(i => `<div class="klai-step ${step >= i ? 'active' : ''}"></div>`).join('')}
    </div>`;
  }

  // ── STEP 1: DATO ──────────────────────────────────────
  let calYear, calMonth;

  function renderDato() {
    const now = new Date();
    if (!calYear) { calYear = now.getFullYear(); calMonth = now.getMonth(); }

    const monthNames = ['Januar','Februar','Marts','April','Maj','Juni','Juli','August','September','Oktober','November','December'];
    const dayLabels = ['Man','Tir','Ons','Tor','Fre','Lør','Søn'];

    // Første dag i måneden (0=søndag, justér til man=0)
    const firstDay = new Date(calYear, calMonth, 1);
    let startDow = firstDay.getDay() - 1; if (startDow < 0) startDow = 6;
    const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();

    let cells = '';
    for (let i = 0; i < startDow; i++) cells += `<div class="klai-cal-day empty"></div>`;
    for (let d = 1; d <= daysInMonth; d++) {
      const date = new Date(calYear, calMonth, d);
      const dow = date.getDay() === 0 ? 6 : date.getDay() - 1; // mon=0
      const isPast = date < new Date(now.getFullYear(), now.getMonth(), now.getDate());
      const isAvail = cfg.dage.includes(dow + 1) && !isPast; // cfg.dage: 1=man,...5=fre
      const isToday = date.toDateString() === now.toDateString();
      const dateStr = date.toISOString().split('T')[0];
      const isSelected = valgtDato === dateStr;
      const cls = ['klai-cal-day', isToday?'today':'', isSelected?'selected':isAvail?'available':'disabled'].join(' ');
      cells += `<div class="${cls}" ${isAvail?`onclick="klaiPickDate('${dateStr}')"`:''}>${d}</div>`;
    }

    wrap.innerHTML = `
      ${stepsHtml()}
      <div class="klai-bw-title">${cfg.titel}</div>
      <div class="klai-section-title">Vælg dato</div>
      <div class="klai-cal-header">
        <button onclick="klaiCalNav(-1)">‹</button>
        <span class="klai-cal-month">${monthNames[calMonth]} ${calYear}</span>
        <button onclick="klaiCalNav(1)">›</button>
      </div>
      <div class="klai-cal-grid">
        ${dayLabels.map(l => `<div class="klai-cal-day-label">${l}</div>`).join('')}
        ${cells}
      </div>
      <button class="klai-btn klai-btn-primary" style="margin-top:1rem" onclick="klaiNextStep()" ${!valgtDato?'disabled':''}>Vælg tidspunkt →</button>
      <div class="klai-powered">Drevet af <strong>SittamTech</strong></div>
    `;
  }

  // ── STEP 2: TID + YDELSE ─────────────────────────────
  function renderTidYdelse() {
    const slots = genererSlots();
    const datoFormateret = new Date(valgtDato).toLocaleDateString('da-DK', { weekday:'long', day:'numeric', month:'long' });

    wrap.innerHTML = `
      ${stepsHtml()}
      <div class="klai-bw-title">${cfg.titel}</div>
      <div class="klai-section-title">Tidspunkt — ${datoFormateret}</div>
      <div class="klai-slots">
        ${slots.map(t => {
          const erOptaget = optaget.includes(t);
          const erValgt = valgtTid === t;
          return `<div class="klai-slot ${erValgt?'selected':''} ${erOptaget?'booked':''}"
            ${erOptaget ? 'title="Optaget"' : `onclick="klaiPickTid('${t}')"`}
            style="${erOptaget ? 'opacity:.35;cursor:not-allowed;text-decoration:line-through;' : ''}">
            ${t}${erOptaget ? '' : ''}
          </div>`;
        }).join('')}
      </div>
      ${cfg.ydelser.length > 1 ? `
        <div class="klai-section-title" style="margin-top:1.25rem">Ydelse</div>
        <div class="klai-ydelse-list">
          ${cfg.ydelser.map(y => `<div class="klai-ydelse ${valgtYdelse===y?'selected':''}" onclick="klaiPickYdelse('${y}')">${y}</div>`).join('')}
        </div>
      ` : ''}
      <button class="klai-btn klai-btn-primary" onclick="klaiNextStep()" ${!valgtTid||(cfg.ydelser.length>1&&!valgtYdelse)?'disabled':''}>Udfyld dine oplysninger →</button>
      <button class="klai-btn klai-btn-secondary" onclick="klaiBack()">← Tilbage</button>
      <div class="klai-powered">Drevet af <strong>SittamTech</strong></div>
    `;
    if (cfg.ydelser.length === 1) valgtYdelse = cfg.ydelser[0];
  }

  function genererSlots() {
    const [sh, sm] = cfg.start_tid.split(':').map(Number);
    const [eh, em] = cfg.slut_tid.split(':').map(Number);
    const start = sh * 60 + sm;
    const slut = eh * 60 + em;
    const interval = cfg.varighed + (cfg.buffer || 0);
    const slots = [];
    for (let t = start; t + cfg.varighed <= slut; t += interval) {
      const h = Math.floor(t / 60).toString().padStart(2,'0');
      const m = (t % 60).toString().padStart(2,'0');
      slots.push(`${h}:${m}`);
    }
    return slots;
  }

  // ── STEP 3: INFO ──────────────────────────────────────
  function renderInfo() {
    const datoFormateret = new Date(valgtDato).toLocaleDateString('da-DK', { weekday:'long', day:'numeric', month:'long' });
    wrap.innerHTML = `
      ${stepsHtml()}
      <div class="klai-bw-title">${cfg.titel}</div>
      <div class="klai-summary">
        📅 <strong>${datoFormateret}</strong> kl. <strong>${valgtTid}</strong><br>
        ${valgtYdelse ? `🔧 <strong>${valgtYdelse}</strong>` : ''}
      </div>
      <div class="klai-section-title">Dine oplysninger</div>
      <div class="klai-row">
        <div>
          <label class="klai-label">Navn *</label>
          <input class="klai-input" id="klai_b_navn" placeholder="Jens Hansen" autocomplete="name"/>
        </div>
        <div>
          <label class="klai-label">Telefon</label>
          <input class="klai-input" id="klai_b_tlf" placeholder="+45 12 34 56 78" autocomplete="tel"/>
        </div>
      </div>
      <label class="klai-label">Email *</label>
      <input class="klai-input" id="klai_b_email" type="email" placeholder="jens@firma.dk" autocomplete="email"/>
      <label class="klai-label">Besked (valgfri)</label>
      <input class="klai-input" id="klai_b_besked" placeholder="Evt. særlige ønsker eller spørgsmål"/>
      <div id="klai_b_error"></div>
      <button class="klai-btn klai-btn-primary" id="klai_b_submit" onclick="klaiSendBooking()">Bekræft booking</button>
      <button class="klai-btn klai-btn-secondary" onclick="klaiBack()">← Tilbage</button>
      <div class="klai-powered">Drevet af <strong>SittamTech</strong></div>
    `;
  }

  // ── STEP 4: BEKRÆFTET ────────────────────────────────
  function renderBekraeftet() {
    const datoFormateret = new Date(valgtDato).toLocaleDateString('da-DK', { weekday:'long', day:'numeric', month:'long' });
    wrap.innerHTML = `
      <div class="klai-success-icon">✅</div>
      <div class="klai-success-title">Booking bekræftet!</div>
      <div class="klai-summary" style="margin:1rem 0">
        📅 ${datoFormateret} kl. ${valgtTid}<br>
        ${valgtYdelse ? `🔧 ${valgtYdelse}` : ''}
      </div>
      <div class="klai-success-sub">En bekræftelse er sendt til din email.<br>Vi glæder os til at se dig!</div>
      <div class="klai-powered" style="margin-top:1.25rem">Drevet af <strong>SittamTech</strong></div>
    `;
  }

  // ── ACTIONS ───────────────────────────────────────────
  window.klaiPickDate = async function(d) {
    valgtDato = d;
    valgtTid = null;
    optaget = [];
    render();
    // Hent optagne tider i baggrunden
    try {
      const res = await fetch(`${API_URL}/booking-optaget/${CLIENT_ID}/${d}`);
      if (res.ok) {
        const data = await res.json();
        optaget = data.optaget || [];
        render(); // genrender med opdaterede optagne tider
      }
    } catch(e) {}
  };
  window.klaiPickTid = function(t) { valgtTid = t; render(); };
  window.klaiPickYdelse = function(y) { valgtYdelse = y; render(); };
  window.klaiCalNav = function(dir) {
    calMonth += dir;
    if (calMonth < 0) { calMonth = 11; calYear--; }
    if (calMonth > 11) { calMonth = 0; calYear++; }
    render();
  };
  window.klaiNextStep = function() { step++; render(); };
  window.klaiBack = function() { step--; render(); };

  window.klaiSendBooking = async function() {
    const navn = document.getElementById('klai_b_navn').value.trim();
    const email = document.getElementById('klai_b_email').value.trim();
    const tlf = document.getElementById('klai_b_tlf').value.trim();
    const besked = document.getElementById('klai_b_besked').value.trim();

    if (!navn || !email) {
      document.getElementById('klai_b_error').innerHTML = '<div class="klai-error">Udfyld venligst navn og email.</div>';
      return;
    }

    const btn = document.getElementById('klai_b_submit');
    btn.disabled = true; btn.textContent = 'Sender...';

    try {
      const res = await fetch(`${API_URL}/booking`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          client: CLIENT_ID,
          booking: { navn, email, telefon: tlf, ydelse: valgtYdelse, dato: valgtDato, tid: valgtTid, besked }
        })
      });
      if (res.ok) {
        step = 4; render();
      } else if (res.status === 409) {
        // Dobbeltbooking — opdater optaget-liste og gå tilbage til tidsvalg
        const errData = await res.json();
        optaget.push(valgtTid);
        valgtTid = null;
        step = 2;
        render();
        setTimeout(() => {
          const err = document.createElement('div');
          err.className = 'klai-error';
          err.style.marginBottom = '.75rem';
          err.textContent = errData.error || 'Dette tidspunkt er allerede booket. Vælg et andet.';
          wrap.querySelector('.klai-slots')?.before(err);
        }, 50);
      } else {
        throw new Error();
      }
    } catch(e) {
      btn.disabled = false; btn.textContent = 'Bekræft booking';
      document.getElementById('klai_b_error').innerHTML = '<div class="klai-error">Noget gik galt. Prøv igen.</div>';
    }
  };

  init();
})();
