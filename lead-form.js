/**
 * SittamTech Lead Form Widget
 * Embed: <script src="https://klaiai.dk/lead-form.js" data-client="KLIENT_ID" data-color="#0a2463"></script>
 */
(function () {
  const script = document.currentScript || document.querySelector('script[data-client]');
  const CLIENT_ID = script?.getAttribute('data-client') || 'demo';
  const COLOR = script?.getAttribute('data-color') || '#0a2463';
  const TITLE = script?.getAttribute('data-title') || 'Kontakt os';
  const SUBTITLE = script?.getAttribute('data-subtitle') || 'Udfyld formularen og vi vender tilbage hurtigst muligt.';
  const BTN_TEXT = script?.getAttribute('data-btn') || 'Send besked';
  const API_URL = script?.getAttribute('data-api') || 'https://klaiai.onrender.com';

  // ── STYLES ──────────────────────────────────────────
  const style = document.createElement('style');
  style.textContent = `
    .klaiai-form-wrap * { box-sizing: border-box; font-family: -apple-system, 'Inter', sans-serif; }
    .klaiai-form-wrap {
      background: #fff;
      border-radius: 16px;
      padding: 2rem;
      max-width: 480px;
      width: 100%;
      box-shadow: 0 4px 24px rgba(0,0,0,0.08);
      border: 1px solid rgba(0,0,0,0.06);
    }
    .klaiai-form-title {
      font-size: 1.3rem;
      font-weight: 700;
      color: #0a1628;
      margin-bottom: .3rem;
    }
    .klaiai-form-subtitle {
      font-size: .875rem;
      color: #6b7a99;
      margin-bottom: 1.5rem;
      line-height: 1.5;
    }
    .klaiai-form-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: .75rem;
      margin-bottom: .75rem;
    }
    .klaiai-form-group {
      display: flex;
      flex-direction: column;
      margin-bottom: .75rem;
    }
    .klaiai-form-group.full { grid-column: 1 / -1; }
    .klaiai-label {
      font-size: .78rem;
      font-weight: 600;
      color: #374151;
      margin-bottom: .3rem;
    }
    .klaiai-input, .klaiai-textarea {
      border: 1.5px solid #e5e7eb;
      border-radius: 10px;
      padding: .65rem .9rem;
      font-size: .875rem;
      color: #0a1628;
      outline: none;
      width: 100%;
      transition: border-color .2s;
      background: #fafafa;
    }
    .klaiai-input:focus, .klaiai-textarea:focus {
      border-color: ${COLOR};
      background: #fff;
    }
    .klaiai-textarea { resize: vertical; min-height: 100px; }
    .klaiai-submit {
      width: 100%;
      padding: .85rem;
      background: ${COLOR};
      color: #fff;
      border: none;
      border-radius: 10px;
      font-size: .95rem;
      font-weight: 600;
      cursor: pointer;
      margin-top: .5rem;
      transition: opacity .2s;
    }
    .klaiai-submit:hover { opacity: .88; }
    .klaiai-submit:disabled { opacity: .6; cursor: default; }
    .klaiai-success {
      display: none;
      background: #dcfce7;
      border: 1px solid #86efac;
      border-radius: 10px;
      padding: 1rem 1.25rem;
      color: #166534;
      font-size: .875rem;
      font-weight: 500;
      margin-top: .75rem;
      text-align: center;
    }
    .klaiai-error {
      display: none;
      background: #fee2e2;
      border: 1px solid #fca5a5;
      border-radius: 10px;
      padding: .75rem 1rem;
      color: #991b1b;
      font-size: .82rem;
      margin-top: .75rem;
    }
    .klaiai-powered {
      text-align: center;
      font-size: .7rem;
      color: #ccc;
      margin-top: 1rem;
    }
    @media (max-width: 480px) {
      .klaiai-form-row { grid-template-columns: 1fr; }
      .klaiai-form-wrap { padding: 1.25rem; }
    }
  `;
  document.head.appendChild(style);

  // ── HTML ─────────────────────────────────────────────
  const wrap = document.createElement('div');
  wrap.className = 'klaiai-form-wrap';
  wrap.innerHTML = `
    <div class="klaiai-form-title">${TITLE}</div>
    <div class="klaiai-form-subtitle">${SUBTITLE}</div>
    <div class="klaiai-form-row">
      <div class="klaiai-form-group">
        <label class="klaiai-label">Navn *</label>
        <input class="klaiai-input" id="klai_navn" placeholder="Jens Hansen" autocomplete="name"/>
      </div>
      <div class="klaiai-form-group">
        <label class="klaiai-label">Telefon</label>
        <input class="klaiai-input" id="klai_telefon" placeholder="+45 12 34 56 78" autocomplete="tel"/>
      </div>
    </div>
    <div class="klaiai-form-row">
      <div class="klaiai-form-group">
        <label class="klaiai-label">Email *</label>
        <input class="klaiai-input" id="klai_email" type="email" placeholder="jens@firma.dk" autocomplete="email"/>
      </div>
      <div class="klaiai-form-group">
        <label class="klaiai-label">Virksomhed</label>
        <input class="klaiai-input" id="klai_virksomhed" placeholder="Firma A/S"/>
      </div>
    </div>
    <div class="klaiai-form-group">
      <label class="klaiai-label">Besked</label>
      <textarea class="klaiai-textarea" id="klai_besked" placeholder="Hvad kan vi hjælpe med?"></textarea>
    </div>
    <button class="klaiai-submit" id="klai_submit">${BTN_TEXT}</button>
    <div class="klaiai-success" id="klai_success">Tak for din henvendelse! Vi vender tilbage hurtigst muligt.</div>
    <div class="klaiai-error" id="klai_error">Noget gik galt. Prøv igen eller kontakt os direkte.</div>
    <div class="klaiai-powered">Drevet af <strong>SittamTech</strong></div>
  `;

  // Indsæt formularen hvor scriptet er
  script.parentNode.insertBefore(wrap, script.nextSibling);

  // ── SUBMIT ───────────────────────────────────────────
  document.getElementById('klai_submit').addEventListener('click', async () => {
    const navn = document.getElementById('klai_navn').value.trim();
    const email = document.getElementById('klai_email').value.trim();
    const telefon = document.getElementById('klai_telefon').value.trim();
    const virksomhed = document.getElementById('klai_virksomhed').value.trim();
    const besked = document.getElementById('klai_besked').value.trim();

    if (!navn || !email) {
      document.getElementById('klai_error').style.display = 'block';
      document.getElementById('klai_error').textContent = 'Udfyld venligst navn og email.';
      return;
    }

    const btn = document.getElementById('klai_submit');
    btn.disabled = true;
    btn.textContent = 'Sender...';
    document.getElementById('klai_error').style.display = 'none';

    try {
      const res = await fetch(`${API_URL}/lead`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          client: CLIENT_ID,
          send: true,
          lead: { navn, email, telefon, virksomhed, besked }
        })
      });

      if (res.ok) {
        wrap.querySelector('.klaiai-form-row') && (wrap.innerHTML = `
          <div class="klaiai-form-title">${TITLE}</div>
          <div class="klaiai-success" style="display:block; margin-top:0">
            Tak, ${navn}! Vi har modtaget din henvendelse og vender tilbage hurtigst muligt.
          </div>
          <div class="klaiai-powered">Drevet af <strong>SittamTech</strong></div>
        `);
      } else {
        throw new Error('Server fejl');
      }
    } catch (e) {
      btn.disabled = false;
      btn.textContent = BTN_TEXT;
      document.getElementById('klai_error').style.display = 'block';
    }
  });
})();
