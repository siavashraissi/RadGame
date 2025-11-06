(function(){
  // Namespace keys by access code to keep per-user separation
  const code = (window.ACCESS_CODE || 'anon').toString();
  const k = (suffix) => `rg_${code}_${suffix}`;
  // Declare early so functions above its original location can reference safely
  let lastCaseSavePromise = null; // moved up to avoid TDZ ReferenceError in logoutSession

  // If not logged in, do nothing (prevents heartbeat/snapshot calls on login page)
  if (!window.ACCESS_CODE) { return; }

  // Mark that the shared annotation script is active for this page
  try { window.RG_ANNOTATION_LOADED = true; } catch(e) {}

  // When user changes or server run changes, initialize storage for this code
  if (localStorage.getItem(k('run_id')) !== window.RUN_ID) {
    localStorage.setItem(k('run_id'), window.RUN_ID);
    localStorage.removeItem(k('correct'));
    localStorage.removeItem(k('incorrect'));
    localStorage.removeItem(k('cases'));
    localStorage.removeItem(k('session_start'));
    localStorage.removeItem(k('paused'));
    localStorage.removeItem(k('pause_start'));
    localStorage.removeItem(k('accumulated_pause_time'));
    localStorage.removeItem(k('images'));
    localStorage.removeItem(k('total_time')); // cumulative across sessions (we still fetch from server)
  }

  // Initialize session timer for this user
  if (!localStorage.getItem(k('session_start'))) {
    localStorage.setItem(k('session_start'), Date.now().toString());
  }
  if (!localStorage.getItem(k('paused'))) localStorage.setItem(k('paused'), 'false');
  if (!localStorage.getItem(k('pause_start'))) localStorage.setItem(k('pause_start'), '0');
  if (!localStorage.getItem(k('accumulated_pause_time'))) localStorage.setItem(k('accumulated_pause_time'), '0');
  if (!localStorage.getItem(k('images'))) localStorage.setItem(k('images'), '0');
  // Ensure a base checkpoint of 0 for first-time users so initial pause shows 00:00
  if (!localStorage.getItem(k('timer_base_ms'))) {
    localStorage.setItem(k('timer_base_ms'), '0');
  }

  // Preload server-side summary for this access code so progress persists across logins
  let serverSummary = null;
  async function loadServerSummary(){
    try {
      const resp = await fetch('/api/progress/summary');
      if (resp.ok) {
        serverSummary = await resp.json();
        // Initialize local timer to resume from last checkpoint if provided
        const lastCp = parseInt(serverSummary.last_timer_checkpoint_ms || '0') || 0;
        if (lastCp > 0) {
          // Set session_start so that elapsedNow() + lastCp == total time
          const now = Date.now();
          localStorage.setItem(k('session_start'), now.toString());
          localStorage.setItem(k('accumulated_pause_time'), '0');
          localStorage.setItem(k('paused'), 'false');
          localStorage.setItem(k('pause_start'), '0');
          // Stash a base offset so we can add to elapsed
          localStorage.setItem(k('timer_base_ms'), String(lastCp));
        } else {
          localStorage.setItem(k('timer_base_ms'), '0');
          // Explicitly reset session start so first pause reflects 00:00 rather than elapsed since page load
          localStorage.setItem(k('session_start'), Date.now().toString());
          localStorage.setItem(k('accumulated_pause_time'), '0');
          localStorage.setItem(k('paused'), 'false');
          localStorage.setItem(k('pause_start'), '0');
        }
        // Sync local image counter upwards to avoid regressions after reloads/app restarts
        const serverImgs = parseInt(serverSummary.images_total) || 0;
        const localImgs = parseInt(localStorage.getItem(k('images')) || '1');
        if (serverImgs > localImgs) {
          localStorage.setItem(k('images'), String(serverImgs));
        }
        // Push a snapshot immediately so server reflects current client counters
        try { saveProgressSnapshot(); } catch(e) {}
        // Ensure banner reflects server values immediately
        try { updateCounter(); } catch(e) {}
      }
    } catch(e) { console.warn('Failed to load summary', e); }
  }

  // Fetch-only refresher to keep banner synced with DB (no snapshot side-effects)
  const IS_REPORT_PAGE = window.location.pathname.startsWith('/report');
  // Report timer key helper & initialization (independent from localization timer)
  const rk = (s) => `rg_${code}_report_${s}`;
  if (IS_REPORT_PAGE) {
    if (!localStorage.getItem(rk('session_start'))) localStorage.setItem(rk('session_start'), Date.now().toString());
    if (!localStorage.getItem(rk('paused'))) localStorage.setItem(rk('paused'), 'false');
    if (!localStorage.getItem(rk('pause_start'))) localStorage.setItem(rk('pause_start'), '0');
    if (!localStorage.getItem(rk('accumulated_pause_time'))) localStorage.setItem(rk('accumulated_pause_time'), '0');
    if (!localStorage.getItem(rk('timer_base_ms'))) localStorage.setItem(rk('timer_base_ms'), '0');
    // REPORT TIMER AUTO-RESUME (after leaving to main menu)
    try {
        // AUTO-RESUME after leaving to main menu (auto pause nav flag)
        const autoNav = localStorage.getItem(rk('auto_nav_pause'));
        if (autoNav === '1' && localStorage.getItem(rk('paused')) === 'true') {
          const rpstart2 = parseInt(localStorage.getItem(rk('pause_start'))) || 0;
          if (rpstart2 > 0) {
            const racc2 = parseInt(localStorage.getItem(rk('accumulated_pause_time'))) || 0;
            const delta2 = Date.now() - rpstart2;
            if (isFinite(delta2) && delta2 > 0) {
              localStorage.setItem(rk('accumulated_pause_time'), String(racc2 + delta2));
            }
          }
          localStorage.setItem(rk('paused'), 'false');
          localStorage.setItem(rk('pause_start'), '0');
          localStorage.removeItem(rk('auto_nav_pause'));
        }
        // Fallback: if still paused but not marked manual, auto-resume
        if (localStorage.getItem(rk('paused')) === 'true' && localStorage.getItem(rk('manual_pause')) !== '1') {
          const rpstart3 = parseInt(localStorage.getItem(rk('pause_start'))) || 0;
          if (rpstart3 > 0) {
            const racc3 = parseInt(localStorage.getItem(rk('accumulated_pause_time'))) || 0;
            const delta3 = Date.now() - rpstart3;
            if (isFinite(delta3) && delta3 > 0) {
              localStorage.setItem(rk('accumulated_pause_time'), String(racc3 + delta3));
            }
          }
          localStorage.setItem(rk('paused'), 'false');
          localStorage.setItem(rk('pause_start'), '0');
        }
    } catch(e) { /* ignore */ }
  }
  async function refreshServerSummary(){
    try {
      const endpoint = IS_REPORT_PAGE ? '/api/report/summary' : '/api/progress/summary';
      const resp = await fetch(endpoint);
      if (resp.ok) {
        const data = await resp.json();
        // Normalize fields so updateCounter can branch cleanly
        if (IS_REPORT_PAGE) {
          serverSummary = {
            report_cases_completed: data.report_cases_completed || 0,
            avg_green_score: data.avg_green_score
          };
        } else {
            serverSummary = data;
        }
        try { updateCounter(); } catch(e) {}
      }
    } catch(e) { /* silent */ }
  }
  // Expose refresh for external pages (e.g., passive report) to force immediate header update
  try { window.RG_refreshReportSummary = refreshServerSummary; } catch(e) {}

  // Fire and forget, UI reads when ready
  // Patch loadServerSummary to pick correct endpoint if on report page
  const _origLoad = loadServerSummary;
  loadServerSummary = async function(){
    if (IS_REPORT_PAGE) {
      try {
        const resp = await fetch('/api/report/summary');
        if (resp.ok) {
          const data = await resp.json();
          serverSummary = {
            report_cases_completed: data.report_cases_completed || 0,
            avg_green_score: data.avg_green_score,
            last_timer_checkpoint_ms: data.last_timer_checkpoint_ms || 0
          };
          // Initialize report timer base if checkpoint exists
          try {
            const rcp = parseInt(data.last_timer_checkpoint_ms || 0) || 0;
            if (rcp > 0) {
              localStorage.setItem(rk('timer_base_ms'), String(rcp));
              localStorage.setItem(rk('session_start'), Date.now().toString());
              localStorage.setItem(rk('accumulated_pause_time'), '0');
              localStorage.setItem(rk('paused'), 'false');
              localStorage.setItem(rk('pause_start'), '0');
            } else {
              // First-time report user: ensure explicit zero baseline so first pause is 00:00
              if (!localStorage.getItem(rk('timer_base_ms'))) {
                localStorage.setItem(rk('timer_base_ms'), '0');
              } else {
                localStorage.setItem(rk('timer_base_ms'), '0');
              }
              localStorage.setItem(rk('session_start'), Date.now().toString());
              localStorage.setItem(rk('accumulated_pause_time'), '0');
              localStorage.setItem(rk('paused'), 'false');
              localStorage.setItem(rk('pause_start'), '0');
            }
          } catch(e) { /* ignore */ }
          updateCounter();
        }
      } catch(e) { /* ignore */ }
    } else {
      try { await _origLoad(); } catch(e) {}
    }
  };
  loadServerSummary();
  // For report pages, immediately fetch report summary too
  if (IS_REPORT_PAGE) { refreshServerSummary(); }

  // Optionally, in test mode, set session start 30 minutes ago
  if (window.IS_TEST_MODE) {
    localStorage.setItem(k('session_start'), (Date.now() - 30 * 60 * 1000).toString());
  }

  // Build banner unless explicitly suppressed (test pages set window.RG_SUPPRESS_BANNER=true)
  let counterDiv = null;
  const BANNER_SUPPRESSED = !!window.RG_SUPPRESS_BANNER;
  if (!BANNER_SUPPRESSED) {
    counterDiv = document.createElement('div');
    counterDiv.id = 'rg-counter';
    Object.assign(counterDiv.style, {
      position: 'sticky', top: '0', left: '0', width: '100%', background: '#ffffff', padding: '1rem', zIndex: 1000,
      boxShadow: '0 2px 8px rgba(0,0,0,0.1)', borderBottom: '1px solid #e9ecef', display: 'flex',
      alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap'
    });
    document.body.insertBefore(counterDiv, document.body.firstChild);
  }

  function elapsedNow(){
    const start = parseInt(localStorage.getItem(k('session_start'))) || Date.now();
    const paused = localStorage.getItem(k('paused')) === 'true';
    const pstart = parseInt(localStorage.getItem(k('pause_start'))) || 0;
    const acc = parseInt(localStorage.getItem(k('accumulated_pause_time'))) || 0;
  const localElapsed = paused ? (pstart - start - acc) : (Date.now() - start - acc);
  const base = parseInt(localStorage.getItem(k('timer_base_ms')) || '0') || 0;
  return base + localElapsed;
  }
  function elapsedReport(){
    const start = parseInt(localStorage.getItem(rk('session_start'))) || Date.now();
    const paused = localStorage.getItem(rk('paused')) === 'true';
    const pstart = parseInt(localStorage.getItem(rk('pause_start'))) || 0;
    const acc = parseInt(localStorage.getItem(rk('accumulated_pause_time'))) || 0;
    const localElapsed = paused ? (pstart - start - acc) : (Date.now() - start - acc);
    const base = parseInt(localStorage.getItem(rk('timer_base_ms')) || '0') || 0;
    return base + localElapsed;
  }
  function fmt(ms){
    if (!ms || ms < 0) ms = 0;
    const h = Math.floor(ms/3600000), m = Math.floor((ms%3600000)/60000), s = Math.floor((ms%60000)/1000);
    return `${h.toString().padStart(2,'0')}:${m.toString().padStart(2,'0')}:${s.toString().padStart(2,'0')}`;
  }

  // Expose elapsed + standardized after-case advance checkpointing so passive guided mode can reuse logic
  try { window.RG_elapsedNow = elapsedNow; } catch(e) {}
  try {
    window.RG_afterCaseAdvance = function(serverCheckpoint){
      // Use provided server checkpoint if valid, else current elapsed
      let baseVal = (typeof serverCheckpoint === 'number' && serverCheckpoint >= 0) ? serverCheckpoint : elapsedNow();
      if (!isFinite(baseVal) || baseVal < 0) baseVal = 0;
      localStorage.setItem(k('timer_base_ms'), String(baseVal));
      localStorage.setItem(k('session_start'), Date.now().toString());
      localStorage.setItem(k('accumulated_pause_time'), '0');
      localStorage.setItem(k('paused'), 'false');
      localStorage.setItem(k('pause_start'), '0');
    };
  } catch(e) {}

  // Helper: send progress snapshot to server (best-effort)
  async function saveProgressSnapshot(imagesOverride, endpoint) {
    try {
      const totalC = parseInt(localStorage.getItem(k('correct'))) || 0;
      const totalI = parseInt(localStorage.getItem(k('incorrect'))) || 0;
      const totalCases = parseInt(localStorage.getItem(k('cases'))) || 0;
      const localImgs = imagesOverride || (parseInt(localStorage.getItem(k('images'))) || 1);
      const serverImgs = (serverSummary && parseInt(serverSummary.images_total)) || 0;
      const imgs = Math.max(localImgs, serverImgs);
      const ms = elapsedNow();
      const metadata = {
        total_correct: totalC,
        total_incorrect: totalI,
        total_cases: totalCases,
        images_processed: imgs,
        session_time_ms: ms,
        session_time_formatted: fmt(ms)
      };
      await fetch(endpoint || '/api/progress/snapshot', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ metadata }),
        keepalive: true
      });
    } catch (e) {
      console.warn('Failed to save snapshot', e);
    }
  }

  // Helper: send snapshot using Beacon API (non-blocking, reliable on unload)
  function saveProgressBeacon(imagesOverride, endpoint){
    try {
      const totalC = parseInt(localStorage.getItem(k('correct'))) || 0;
      const totalI = parseInt(localStorage.getItem(k('incorrect'))) || 0;
      const totalCases = parseInt(localStorage.getItem(k('cases'))) || 0;
      const localImgs = imagesOverride || (parseInt(localStorage.getItem(k('images'))) || 1);
      const serverImgs = (serverSummary && parseInt(serverSummary.images_total)) || 0;
      const imgs = Math.max(localImgs, serverImgs);
      const ms = elapsedNow();
      const metadata = {
        total_correct: totalC,
        total_incorrect: totalI,
        total_cases: totalCases,
        images_processed: imgs,
        session_time_ms: ms,
        session_time_formatted: fmt(ms)
      };
      const blob = new Blob([JSON.stringify({ metadata })], { type: 'application/json' });
      navigator.sendBeacon((endpoint || '/api/progress/snapshot'), blob);
    } catch(e) {
      // swallow
    }
  }

  // Periodic heartbeat to persist progress while working
  let heartbeatTimer = null;
  function startHeartbeat(){
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    heartbeatTimer = setInterval(()=>{
      if (localStorage.getItem(k('paused')) !== 'true' && document.visibilityState === 'visible') {
        saveProgressSnapshot(undefined, '/api/progress/heartbeat');
        // Refresh DB-backed banner numbers periodically
        refreshServerSummary();
      }
    }, 15000);
  }
  document.addEventListener('visibilitychange', ()=>{
    if (document.visibilityState === 'visible') startHeartbeat();
  });

  function togglePause(){
    const paused = localStorage.getItem(k('paused')) === 'true';
    if (paused) {
      const pstart = parseInt(localStorage.getItem(k('pause_start'))) || 0;
      const acc = parseInt(localStorage.getItem(k('accumulated_pause_time'))) || 0;
      localStorage.setItem(k('accumulated_pause_time'), (acc + (Date.now()-pstart)).toString());
      localStorage.setItem(k('paused'), 'false');
      localStorage.setItem(k('pause_start'), '0');
      removePauseOverlay();
    } else {
      localStorage.setItem(k('paused'), 'true');
      localStorage.setItem(k('pause_start'), Date.now().toString());
      // Manual pause
      addPauseOverlay();
    }
    updateCounter();
  }
  function toggleReportPause(){
    const paused = localStorage.getItem(rk('paused')) === 'true';
    if (paused) {
      const pstart = parseInt(localStorage.getItem(rk('pause_start'))) || 0;
      const acc = parseInt(localStorage.getItem(rk('accumulated_pause_time'))) || 0;
      localStorage.setItem(rk('accumulated_pause_time'), (acc + (Date.now()-pstart)).toString());
      localStorage.setItem(rk('paused'), 'false');
      localStorage.setItem(rk('pause_start'), '0');
  localStorage.removeItem(rk('manual_pause'));
  removePauseOverlay();
    } else {
      localStorage.setItem(rk('paused'), 'true');
      localStorage.setItem(rk('pause_start'), Date.now().toString());
  localStorage.setItem(rk('manual_pause'), '1');
  addPauseOverlay();
    }
    updateCounter();
    if (typeof syncPauseOverlay === 'function') syncPauseOverlay();
  }

  // Exposed for next-case button on report pages
  window.checkpointReportTimer = function(){
    // Add current elapsed to base and reset session window
    const start = parseInt(localStorage.getItem(rk('session_start'))) || Date.now();
    const paused = localStorage.getItem(rk('paused')) === 'true';
    const pstart = parseInt(localStorage.getItem(rk('pause_start'))) || 0;
    const acc = parseInt(localStorage.getItem(rk('accumulated_pause_time'))) || 0;
    const localElapsed = paused ? (pstart - start - acc) : (Date.now() - start - acc);
    const base = parseInt(localStorage.getItem(rk('timer_base_ms')) || '0') || 0;
    const total = Math.max(0, base + localElapsed);
    localStorage.setItem(rk('timer_base_ms'), String(total));
    localStorage.setItem(rk('session_start'), Date.now().toString());
    localStorage.setItem(rk('accumulated_pause_time'), '0');
    if (paused) { // resume automatically after checkpoint
      localStorage.setItem(rk('paused'), 'false');
      localStorage.setItem(rk('pause_start'), '0');
      removePauseOverlay();
    }
  };

  function addPauseOverlay(){
    removePauseOverlay();
    const bar = document.getElementById('rg-counter');
    const h = bar ? bar.offsetHeight : 80;
    const overlay = document.createElement('div');
    overlay.id = 'pause-overlay';
    Object.assign(overlay.style, { position:'fixed', top: h+'px', left:0, width:'100%', height:`calc(100% - ${h}px)`,
      backgroundColor:'rgba(0,0,0,0.3)', zIndex:2147483647, display:'flex', alignItems:'center', justifyContent:'center' });
    const msg = document.createElement('div');
    Object.assign(msg.style, { background:'linear-gradient(135deg,#8B0000,#800020)', color:'#fff', padding:'2rem 3rem', borderRadius:'12px', fontSize:'1.5rem', fontWeight:'bold' });
    msg.textContent = 'Session Paused';
    overlay.appendChild(msg);
    document.body.appendChild(overlay);
    disableInteractiveElements();
  }
  function removePauseOverlay(){ const o = document.getElementById('pause-overlay'); if (o) o.remove(); enableInteractiveElements(); }
  function syncPauseOverlay(){
    try {
      if (IS_REPORT_PAGE) {
        const pr = localStorage.getItem(rk('paused')) === 'true';
        if (pr) { if (!document.getElementById('pause-overlay')) addPauseOverlay(); }
        else { if (document.getElementById('pause-overlay')) removePauseOverlay(); }
      } else {
        const pl = localStorage.getItem(k('paused')) === 'true';
        if (pl) { if (!document.getElementById('pause-overlay')) addPauseOverlay(); }
        else { if (document.getElementById('pause-overlay')) removePauseOverlay(); }
      }
    } catch(e) { /* noop */ }
  }

  function disableInteractiveElements(){
    if (IS_REPORT_PAGE) {
      // Report page has different interactive elements
      document.querySelectorAll('.report-pane button, .report-pane textarea').forEach(el => {
        if(el){ el.style.pointerEvents='none'; el.style.opacity='0.6'; el.disabled=true; }
      });
      return;
    }
    const canvas = document.getElementById('canvas'); if (canvas){ canvas.style.pointerEvents='none'; canvas.style.opacity='0.6'; }
    document.querySelectorAll('.label-btn,.nonlocal-btn,#submit-btn,#next-btn,#report-container button').forEach(btn=>{ if(btn){ btn.style.pointerEvents='none'; btn.style.opacity='0.6'; btn.disabled=true; }});
    if (typeof deleteBtn !== 'undefined' && deleteBtn){ deleteBtn.style.pointerEvents='none'; deleteBtn.style.opacity='0.6'; }
  }
  function enableInteractiveElements(){
    if (IS_REPORT_PAGE) {
      document.querySelectorAll('.report-pane button, .report-pane textarea').forEach(el => {
        if(el){ el.style.pointerEvents='auto'; el.style.opacity='1'; el.disabled=false; }
      });
      return;
    }
    const canvas = document.getElementById('canvas'); if (canvas){ canvas.style.pointerEvents='auto'; canvas.style.opacity='1'; }
    document.querySelectorAll('.label-btn,.nonlocal-btn,#submit-btn,#next-btn,#report-container button').forEach(btn=>{ if(btn){ btn.style.pointerEvents='auto'; btn.style.opacity='1'; btn.disabled=false; }});
    if (typeof deleteBtn !== 'undefined' && deleteBtn){ deleteBtn.style.pointerEvents='auto'; deleteBtn.style.opacity='1'; }
  }

  // Logout functionality removed

  function updateCounter(){
    if (BANNER_SUPPRESSED) return; // skip all banner rendering logic on suppressed pages
    // Use DB-backed numbers only to avoid client/server mismatch
    if (IS_REPORT_PAGE) {
      const reportCount = (serverSummary && serverSummary.report_cases_completed) || 0;
      const avgGreen = serverSummary ? serverSummary.avg_green_score : null;
      // Build last 5 GREEN stats
      let recentReportHTML = '';
      try {
        const rs = JSON.parse(localStorage.getItem(rk('recent_scores'))||'[]');
        if (Array.isArray(rs) && rs.length) {
          const scores = rs.map(o=>parseFloat(o.g)||0);
          const min = Math.min(...scores), max = Math.max(...scores);
          const avg = scores.reduce((a,b)=>a+b,0)/scores.length;
          recentReportHTML = `<div style=\"display:flex; gap:0.5rem; flex-wrap:wrap; font-size:0.7rem; margin-top:0.35rem; letter-spacing:.3px;\">`
            + `<span style=\"font-weight:600; color:#555;\">Last ${scores.length}</span>`
            + `<span style=\"color:#8B0000; font-weight:600;\">High ${max.toFixed(0)}%</span>`
            + `<span style=\"color:#dc3545; font-weight:600;\">Low ${min.toFixed(0)}%</span>`
            + `<span style=\"color:#28a745; font-weight:600;\">Average ${avg.toFixed(0)}%</span>`
            + `</div>`;
        }
      } catch(_) {}
      const IS_GUIDED_REPORT = !!window.RG_REPORT_GUIDED;
  const totalTime = elapsedReport();
  const isPaused = localStorage.getItem(rk('paused')) === 'true';
  const pauseButtonText = isPaused ? 'Resume' : 'Pause';
  const pauseBtnClass = 'rg-btn rg-btn--pause' + (isPaused ? ' is-paused' : '');
  counterDiv.innerHTML = `
        <div style="display:flex; flex-direction:column; gap:0.5rem; min-width:200px;">
          <div style="display:flex; align-items:flex-start; gap:0.75rem; flex-direction:column;">
            <span style="font-weight:600; color:#333; font-size:1.1rem;">Report Images: ${reportCount}</span>
            ${IS_GUIDED_REPORT ? '' : `
            <div style=\"display:flex; align-items:center; gap:0.5rem;\">
              <span style=\"font-weight:500; color:#6c757d; font-size:0.9rem;\">Avg GREEN:</span>
              <span style=\"font-weight:600; color:#8B0000;\">${avgGreen != null ? `${(avgGreen * 100).toFixed(0)}%` : 'N/A'}</span>
            </div>`}
            ${IS_GUIDED_REPORT ? '' : recentReportHTML}
          </div>
        </div>
        <div style="display:flex; align-items:center; gap:1rem; flex-wrap:wrap; justify-content:flex-end;">
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <span style="font-weight:500; color:#6c757d; font-size:0.9rem;">Time total:</span>
            <span style="font-weight:600; color:#333; font-family:monospace; font-size:1.05rem;">${fmt(totalTime)}</span>
            ${isPaused ? '<span style=\"color:#dc3545; font-weight:700; font-size:0.9rem;\">(PAUSED)</span>' : ''}
          </div>
          <button id="pause-btn" class="${pauseBtnClass}">${pauseButtonText}</button>
        </div>`;
  const pauseBtn = document.getElementById('pause-btn'); if (pauseBtn) pauseBtn.addEventListener('click', toggleReportPause);
  if (typeof syncPauseOverlay === 'function') syncPauseOverlay();
      return;
    }

  const serverCorrect = (serverSummary && serverSummary.correct_cases) || 0;
  const serverIncorrect = (serverSummary && serverSummary.incorrect_cases) || 0;
  const serverImages = (serverSummary && serverSummary.images_total) || 0;
  const localImages = parseInt(localStorage.getItem(k('images')) || '0');
  const c = serverCorrect;
  const i = serverIncorrect;
  const total = c + i;
  // Use max to reflect immediate client increment before server snapshot catches up
  const images = Math.max(serverImages, localImages);
  const totalTime = elapsedNow();
  const correctPercent = total > 0 ? (c/total)*100 : 0;
  const incorrectPercent = total > 0 ? (i/total)*100 : 0;
  const IS_GUIDED_LOCALIZE = !!window.RG_GUIDED_MODE; // passive localize page (non-report)
  // Build recent last 5 metrics (only for active localization)
  let recentHTML = '';
  if (!IS_GUIDED_LOCALIZE) {
    try {
      const recent = JSON.parse(localStorage.getItem(k('recent_cases'))||'[]');
      if (Array.isArray(recent) && recent.length) {
        const rC = recent.reduce((a,b)=>a + (parseInt(b.c)||0),0);
        const rI = recent.reduce((a,b)=>a + (parseInt(b.i)||0),0);
        const rT = rC + rI;
        const rAcc = rT>0 ? ((rC/rT)*100).toFixed(0) : '0';
        recentHTML = `<div style=\"display:flex; gap:0.4rem; flex-wrap:wrap; font-size:0.7rem; letter-spacing:.3px; margin-top:0.35rem;\">`
          + `<span style=\"font-weight:600; color:#555;\">Last ${recent.length}</span>`
          + `<span style=\"color:#28a745; font-weight:600;\">Correct ${rC}</span>`
          + `<span style=\"color:#dc3545; font-weight:600;\">Incorrect ${rI}</span>`
          + `<span style=\"color:#8B0000; font-weight:600;\">Accuracy ${rAcc}%</span>`
          + `</div>`;
      }
    } catch(_) {}
  }

  const isPaused = localStorage.getItem(k('paused')) === 'true';
  const pauseButtonText = isPaused ? 'Resume' : 'Pause';
  const pauseBtnClass = 'rg-btn rg-btn--pause' + (isPaused ? ' is-paused' : '');

    if (IS_GUIDED_LOCALIZE) {
      // Passive guided: suppress correct/incorrect finding counts & accuracy bar
  counterDiv.innerHTML = `
        <div style="display:flex; flex-direction:column; gap:0.5rem; min-width:160px;">
          <span style="font-weight:600; color:#333; font-size:1.05rem;">Images: ${images}</span>
        </div>
        <div style="display:flex; align-items:center; gap:1rem; flex-wrap:wrap; justify-content:flex-end;">
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <span style="font-weight:500; color:#6c757d; font-size:0.9rem;">Time total:</span>
            <span style="font-weight:600; color:#333; font-family:monospace; font-size:1.05rem;">${fmt(totalTime)}</span>
            ${isPaused ? '<span style=\"color:#dc3545; font-weight:700; font-size:0.9rem;\">(PAUSED)</span>' : ''}
          </div>
          <button id="pause-btn" class="${pauseBtnClass}">${pauseButtonText}</button>
        </div>`;
    } else {
  const includeLogoutActive = !window.TEST_MODE;
  counterDiv.innerHTML = `
        <div style="display:flex; flex-direction:column; gap:0.5rem; min-width:200px;">
          <div style="display:flex; align-items:center; gap:1rem; flex-wrap:wrap;">
            <span style="font-weight:600; color:#333; font-size:1.05rem;">Images: ${images}</span>
            <span style="display:inline-flex; align-items:center; gap:0.5rem; padding:0.18rem 0.6rem; border-radius:9999px; background:linear-gradient(135deg,#28a745,#20c997); color:#fff; font-weight:700; font-size:0.9rem;">
              <span>${c}</span>
              <span style="font-weight:600; opacity:0.95;">Correct findings</span>
            </span>
            <span style="display:inline-flex; align-items:center; gap:0.5rem; padding:0.18rem 0.6rem; border-radius:9999px; background:linear-gradient(135deg,#dc3545,#c82333); color:#fff; font-weight:700; font-size:0.9rem;">
              <span>${i}</span>
              <span style="font-weight:600; opacity:0.95;">Incorrect findings</span>
            </span>
          </div>
          ${total>0 ? `
            <div style=\"display:flex; align-items:center; gap:0.5rem;\">
              <span style=\"font-weight:500; color:#6c757d; font-size:0.9rem;\">Accuracy:</span>
              <span style=\"font-weight:700; color:#8B0000;\">${(correctPercent).toFixed(1)}%</span>
            </div>
            <div style=\"width:100%; height:8px; background:#f8f9fa; border-radius:999px; overflow:hidden; border:1px solid #e9ecef; position:relative;\">
              <div style=\"width:${correctPercent}%; height:100%; background:linear-gradient(90deg,#28a745,#20c997);\"></div>
              <div style=\"width:${incorrectPercent}%; height:100%; background:linear-gradient(90deg,#dc3545,#c82333); position:absolute; left:${correctPercent}%; top:0;\"></div>
            </div>` : ''}
              ${recentHTML}
        </div>
        <div style="display:flex; align-items:center; gap:1rem; flex-wrap:wrap; justify-content:flex-end;">
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <span style="font-weight:500; color:#6c757d; font-size:0.9rem;">Time total:</span>
            <span style="font-weight:600; color:#333; font-family:monospace; font-size:1.05rem;">${fmt(totalTime)}</span>
            ${isPaused ? '<span style="color:#dc3545; font-weight:700; font-size:0.9rem;">(PAUSED)</span>' : ''}
          </div>
          <button id="pause-btn" class="${pauseBtnClass}">${pauseButtonText}</button>
        </div>`;
    }

    const pauseBtn = document.getElementById('pause-btn'); if (pauseBtn) pauseBtn.addEventListener('click', togglePause);
    if (typeof syncPauseOverlay === 'function') syncPauseOverlay();
  }

  if (!BANNER_SUPPRESSED) {
    updateCounter();
    setInterval(updateCounter, 1000);
  }
  // Ensure overlay restores correctly if page loads already paused
  if (typeof syncPauseOverlay === 'function') syncPauseOverlay();
  startHeartbeat();

  // The rest of the original file logic follows, with all localStorage keys replaced by namespaced versions
  // colors & helpers based on localizable labels
  const colorMap = {};
  window.localLabels.forEach((_, i) => {
    const hue = Math.round(360 * i / window.localLabels.length);
    colorMap[i] = `hsla(${hue}, 70%, 65%, 0.8)`;
  });

  const deleteBtn = document.createElement('button');
  deleteBtn.textContent = '×';
  Object.assign(deleteBtn.style, {
    position:'absolute', display:'none', background:'#e74c3c', color:'#fff', border:'none', borderRadius:'50%',
    width:'20px', height:'20px', textAlign:'center', lineHeight:'20px', cursor:'pointer', zIndex:1001,
    transformOrigin: 'top left', fontSize: '14px', boxSizing: 'border-box'
  });
  let deleteBtnHover = false;

  function interArea(a,b){ const xA = Math.max(a.x1,b.x1), yA = Math.max(a.y1,b.y1), xB = Math.min(a.x2,b.x2), yB = Math.min(a.y2,b.y2); return Math.max(0, xB-xA) * Math.max(0, yB-yA); }
  function boxArea(b){ return (b.x2 - b.x1) * (b.y2 - b.y1); }

  const labelButtons = document.querySelectorAll('.label-btn');
  const IS_GUIDED = !!window.RG_GUIDED_MODE; // passive localization page
  if (IS_GUIDED) {
    // In guided mode we show only GT boxes, no user interaction besides timers/banner.
    try {
      const imgEl = document.getElementById('cxr-img');
      const canvasEl = document.getElementById('canvas');
      if (imgEl && canvasEl) {
        const ctxG = canvasEl.getContext('2d');
        function resizeGuided(){
          canvasEl.width = imgEl.clientWidth;
          canvasEl.height = imgEl.clientHeight;
          drawGuided();
        }
        function drawGuided(){
          ctxG.clearRect(0,0,canvasEl.width,canvasEl.height);
          // Generate stable colors per label (same logic as templates)
          const colors = {};
          const ll = Array.isArray(window.localLabels)? window.localLabels : [];
          ll.forEach((_, i)=>{ const hue=Math.round(360*i/Math.max(ll.length,1)); colors[i]=`hsla(${hue},70%,55%,0.9)`; });
          Object.entries(window.actualBoxes || {}).forEach(([lbl, arr])=>{
            const idx = ll.indexOf(lbl);
            const color = idx>=0 ? colors[idx] : '#8B0000';
            (arr||[]).forEach(b=>{
              if(!Array.isArray(b) || b.length!==4) return;
              const x1=b[0]*canvasEl.width, y1=b[1]*canvasEl.height, x2=b[2]*canvasEl.width, y2=b[3]*canvasEl.height;
              ctxG.strokeStyle = color; ctxG.lineWidth = 3; ctxG.setLineDash([6,3]);
              ctxG.strokeRect(x1,y1,x2-x1,y2-y1);
            });
          });
        }
        imgEl.addEventListener('load', resizeGuided, { once:false });
        window.addEventListener('resize', resizeGuided);
        // If image already loaded (cached), draw immediately
        if (imgEl.complete && imgEl.naturalWidth>0) { resizeGuided(); }
      }
    } catch(e) { /* silent for guided */ }
  }
  // Skip interactive drawing setup entirely for guided mode
  if (IS_GUIDED) return;
  let currentIdx = null;
  // Avoid attaching a second handler when the page (index.html) manages draw label selection itself
  const isInline = !!window.RG_INLINE_DRAWING;
  if (!isInline) {
    labelButtons.forEach(btn => {
      const idx = +btn.dataset.index;
      const bgColor = colorMap[idx];
      btn.style.background = bgColor;
      btn.style.color = '#000000';
      btn.addEventListener('click', () => {
        labelButtons.forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        currentIdx = idx;
      });
    });
  }

  const nonlocalSelections = {};
  const nonlocalButtons = document.querySelectorAll('.nonlocal-btn');
  nonlocalButtons.forEach(btn => {
    const lbl = btn.dataset.label;
    nonlocalSelections[lbl] = !!nonlocalSelections[lbl];
    btn.addEventListener('click', () => {
      const next = !nonlocalSelections[lbl];
      nonlocalSelections[lbl] = next;
      if (next) { btn.classList.add('active','selected'); }
      else { btn.classList.remove('active','selected'); }
    });
  });

  const img = document.getElementById('cxr-img'), canvas = document.getElementById('canvas'), ctx = canvas.getContext('2d');
  const viewportEl = document.getElementById('viewport');
  const INLINE = !!window.RG_INLINE_DRAWING;
  let boxes = (INLINE && Array.isArray(window.boxes)) ? window.boxes : [];
  let drawing = false, sx=0, sy=0, gtBoxes = [], submitted=false, hoveredBoxInfo=null; // lastCaseSavePromise declared earlier

  // Anchor delete button inside the scaled viewport so it moves with zoom and scroll
  if (viewportEl) { viewportEl.appendChild(deleteBtn); } else { document.getElementById('img-container').appendChild(deleteBtn); }
  deleteBtn.addEventListener('click', () => {
    if (hoveredBoxInfo) {
      boxes.splice(hoveredBoxInfo.idx,1);
      hoveredBoxInfo = null;
      deleteBtn.style.display='none';
      if (INLINE && typeof window.redraw === 'function') { window.redraw(); } else { redraw(); }
    }
  });
  deleteBtn.addEventListener('mouseenter', ()=>{ deleteBtnHover = true; });
  deleteBtn.addEventListener('mouseleave', ()=>{ deleteBtnHover = false; if (!hoveredBoxInfo) deleteBtn.style.display='none'; });

  function resizeCanvas(){
    canvas.width = img.clientWidth; canvas.height = img.clientHeight;
    canvas.style.position='absolute'; canvas.style.top = '0px'; canvas.style.left = '0px'; canvas.style.width = img.clientWidth + 'px'; canvas.style.height = img.clientHeight + 'px';
    redraw();
  }
  function getZoom(){ return (window.RG_ZOOM && Number(window.RG_ZOOM)) ? Number(window.RG_ZOOM) : 1; }
  function evtToCanvas(e){ const r = canvas.getBoundingClientRect(); const z = getZoom(); return { x:(e.clientX - r.left)/z, y:(e.clientY - r.top)/z }; }
  function redraw(){
    ctx.clearRect(0,0,canvas.width,canvas.height);
    boxes.forEach(b=>{ ctx.strokeStyle=b.color; ctx.lineWidth=3; ctx.setLineDash([]); ctx.strokeRect(b.x,b.y,b.w,b.h); });
    gtBoxes.forEach(g=>{ const baseIdx = parseInt(g.id.split('-')[0],10); ctx.strokeStyle = g.reported ? '#888' : colorMap[baseIdx]; ctx.lineWidth=4; ctx.setLineDash([5,5]); ctx.strokeRect(g.x1,g.y1,g.x2-g.x1,g.y2-g.y1); });
  }

  if (!INLINE) {
    canvas.addEventListener('mousedown', e=>{ if (currentIdx===null) return; drawing=true; deleteBtn.style.display='none'; const p=evtToCanvas(e); sx = p.x; sy = p.y; });
  }
  function updateDeleteBtnScale(){
    const z = getZoom();
  // Counter-scale so the button stays same visual size while its position rides the scaled layer
  // Align the button's top-right corner to the box corner using translate(-100%, 0)
  // Translate first then scale; translation gets scaled to exactly match the scaled width
  deleteBtn.style.transform = `translate(-100%, 0) scale(${(z>0? (1/z):1)})`;
  }
  function positionDeleteBtn(){
    if (!hoveredBoxInfo) return;
    const found = hoveredBoxInfo;
    // Position in unscaled coordinates within the viewport; scaling is handled by the parent and counter-scale
  const z = getZoom();
  deleteBtn.style.left = (found.box.x + found.box.w) + 'px';
  deleteBtn.style.top = (found.box.y) + 'px';
    updateDeleteBtnScale();
    if (found.justCreated) { try { delete found.justCreated; } catch(_) {} }
  }
  // When a box is created in inline mode, position the delete button for that new box
  try {
    document.addEventListener('rg-box-created', ()=>{
      if (!INLINE) return;
      if (!boxes || boxes.length===0) return;
      const last = boxes[boxes.length-1];
  hoveredBoxInfo = { box:last, idx: boxes.length-1, justCreated: true };
      positionDeleteBtn();
      deleteBtn.style.display='block';
    });
  } catch(e) {}
  canvas.addEventListener('mousemove', e=>{
    const p = evtToCanvas(e), mx = p.x, my = p.y;
  if (drawing && !INLINE){ redraw(); ctx.strokeStyle=colorMap[currentIdx]; ctx.lineWidth=3; ctx.setLineDash([]); ctx.strokeRect(sx,sy,mx-sx,my-sy); return; }
    if (!submitted){ let found=null; for (let i=boxes.length-1; i>=0; i--){ const b=boxes[i]; if (mx>=b.x && mx<=b.x+b.w && my>=b.y && my<=b.y+b.h){ found={box:b, idx:i}; break; } }
      if (found){
        hoveredBoxInfo=found;
  positionDeleteBtn();
        deleteBtn.style.display='block';
        canvas.style.cursor='pointer';
      }
      else {
        hoveredBoxInfo=null;
        if (!deleteBtnHover) deleteBtn.style.display='none';
        canvas.style.cursor='default';
      }
    }
  });
  // Reposition delete button on zoom or resize
  try { document.addEventListener('rg-zoom-changed', ()=>{ updateDeleteBtnScale(); positionDeleteBtn(); }); } catch(e) {}
  window.addEventListener('resize', positionDeleteBtn);
  try { const ctn = document.getElementById('img-container'); if (ctn) ctn.addEventListener('scroll', positionDeleteBtn, { passive: true }); } catch(e) {}
  if (!INLINE) {
    canvas.addEventListener('mouseup', e=>{ if (!drawing) return; drawing=false; const p=evtToCanvas(e), ex=p.x, ey=p.y; boxes.push({ idx:currentIdx, x:Math.min(sx,ex), y:Math.min(sy,ey), w:Math.abs(ex-sx), h:Math.abs(ey-sy), color:colorMap[currentIdx] }); redraw(); });
  }
  window.addEventListener('resize', resizeCanvas);
  img.addEventListener('load', ()=> setTimeout(resizeCanvas, 100)); if (img.complete) setTimeout(resizeCanvas, 100);

  async function sendCaseCompletionData(caseId, metadata, selections, timeSpentMs, counts){
    try {
      const res = await fetch('/api/complete_case', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          case_id: caseId,
          metadata,
          selections,
          time_spent_ms: timeSpentMs,
          correct_count: (counts && counts.correct) || undefined,
          incorrect_count: (counts && counts.incorrect) || undefined
        }),
        keepalive: true
      });
      if (!res.ok) console.error('Failed to send case completion data');
    } catch(e){
      console.error('Error sending case completion data:', e);
    }
  }

  const __submitEl = document.getElementById('submit-btn');
  if (__submitEl) __submitEl.addEventListener('click', ()=>{
    submitted = true; // mark state so internal hover/delete logic ceases
    // Immediately lock further interaction (drawing / label toggling) until page reload (Next Image)
    try {
      const canvasLock = document.getElementById('canvas');
      if (canvasLock) { canvasLock.style.pointerEvents='none'; }
      document.querySelectorAll('.label-btn, .nonlocal-btn').forEach(b=>{
        b.disabled = true;
        b.classList.add('disabled');
        b.classList.remove('selected','active');
      });
    } catch(_) {}
    // Defensive capture handlers (once) to suppress any late events reaching original listeners
    if (!window.__RG_PRACTICE_LOCK_GUARD) {
      window.__RG_PRACTICE_LOCK_GUARD = true;
      ['mousedown','mousemove','mouseup','click','touchstart','touchmove','touchend'].forEach(ev=>{
        document.addEventListener(ev, function(e){
          if (!submitted) return; // allow normal pre-submit behavior
          const t = e.target;
          if (t && (t.id === 'canvas' || (t.classList && (t.classList.contains('label-btn') || t.classList.contains('nonlocal-btn'))))) {
            e.stopImmediatePropagation();
            e.preventDefault();
          }
        }, true);
      });
    }
    deleteBtn.style.display='none';
    const submitBtn=document.getElementById('submit-btn'); if (submitBtn) submitBtn.remove();
    resizeCanvas();

    gtBoxes = []; Object.entries(window.actualBoxes).forEach(([lbl, arr])=>{ let idx = window.localLabels.indexOf(lbl); if (idx<0) idx = window.nonLocalLabels.indexOf(lbl); arr.forEach((b,bi)=>{ const x1=b[0]*canvas.width, y1=b[1]*canvas.height, x2=b[2]*canvas.width, y2=b[3]*canvas.height; gtBoxes.push({ id:`${idx}-${bi}`, x1,y1,x2,y2, nx1:b[0], ny1:b[1], nx2:b[2], ny2:b[3], reported:false }); }); }); redraw();

    const userByIdx = {}; const userBoxesArray = window.boxes || boxes; userBoxesArray.forEach(u=>{ userByIdx[u.idx] = userByIdx[u.idx] || []; userByIdx[u.idx].push({ x1:u.x, y1:u.y, x2:u.x+u.w, y2:u.y+u.h }); });

    const resultsDiv = document.getElementById('results');
    resultsDiv.innerHTML = `
      <div style="display:flex; gap:2rem; margin:2rem 0;">
        <div style="flex:1;">
          <h3 style="color:#8B0000; margin-bottom:1rem; border-bottom:2px solid #8B0000; padding-bottom:0.5rem;">Similarity Scores</h3>
          <div id="similarity-scores"></div>
        </div>
        <div style="flex:1;">
          <h3 style="color:#8B0000; margin-bottom:1rem; border-bottom:2px solid #8B0000; padding-bottom:0.5rem;">Current Case Results</h3>
          <div id="case-results"></div>
        </div>
      </div>`;

  let currentCorrect=0, currentIncorrect=0; const similarityScoresDiv=document.getElementById('similarity-scores'); const caseResultsDiv=document.getElementById('case-results');

    // Simple HTML escaper for safe insertion of model text
    const escapeHTML = (s)=> (s==null?'' : s
      .replace(/&/g,'&amp;')
      .replace(/</g,'&lt;')
      .replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;')
      .replace(/'/g,'&#39;'));

    window.localLabels.forEach((lbl,i)=>{
      const trueArr = (window.actualBoxes[lbl]||[]).map(b=>({ x1:b[0]*canvas.width, y1:b[1]*canvas.height, x2:b[2]*canvas.width, y2:b[3]*canvas.height }));
      const userArr = userByIdx[i]||[]; if (trueArr.length===0 && userArr.length===0) return;
      let I=0; trueArr.forEach(t=> userArr.forEach(u=> I += interArea(t,u))); const A = trueArr.reduce((s,t)=>s+boxArea(t),0), B = userArr.reduce((s,u)=>s+boxArea(u),0), U=Math.max(A+B-I,1e-6), iou=I/U;
  const isCorrect = iou>=0.3; if (isCorrect) currentCorrect++; else currentIncorrect++;
  const pct=(iou*100).toFixed(1)+'%';
  // Provide MedGemma explanation only when the finding exists (GT boxes) but user missed (false negative)
  let mgExpl = null;
  if (!isCorrect && trueArr.length>0) {
    try {
      const arr = window.medgemmaExplanations && window.medgemmaExplanations[lbl];
      if (Array.isArray(arr) && arr.length) mgExpl = arr[0];
    } catch(_) {}
  }
    similarityScoresDiv.innerHTML += `
      <div style="margin-bottom:0.8rem; padding:0.8rem; background:${isCorrect?'rgba(40,167,69,0.08)':'rgba(220,53,69,0.08)'}; border:1px solid ${isCorrect?'rgba(40,167,69,0.35)':'rgba(220,53,69,0.35)'}; border-left:4px solid ${isCorrect?'#28a745':'#dc3545'}; border-radius:8px; position:relative;">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:0.5rem; margin-bottom:0.35rem;">
          <div style="position:relative;">
            <span style="background:#8B0000; color:#fff; padding:0.18rem 0.5rem; border-radius:3px; font-size:0.7rem; font-weight:700;">DRAW</span>
          </div>
          <span class="pill ${isCorrect?'success':'danger'}" style="display:inline-flex; align-items:center; gap:0.35rem; padding:0.18rem 0.55rem; border-radius:9999px; font-weight:700; font-size:0.8rem; ${isCorrect?'background:linear-gradient(135deg,#28a745,#20c997); color:#fff;':'background:linear-gradient(135deg,#dc3545,#c82333); color:#fff;'}">
            ${isCorrect?'Correct':'Incorrect'}
          </span>
        </div>
        <div style="text-align:center; padding-top:0.1rem;">
          <span style="color:${colorMap[i]}; font-weight:700;">${lbl}</span>
        </div>
        <div style="margin-top:0.4rem; font-size:0.9rem; color:#666;">IoU: ${pct}</div>
        ${mgExpl ? `<div style="margin-top:0.55rem; font-size:0.75rem; line-height:1.25; background:#fff; border:1px solid rgba(139,0,0,0.25); padding:0.55rem 0.6rem; border-radius:6px;">
            <strong style=\"display:block; color:#8B0000; font-size:0.65rem; letter-spacing:0.5px; text-transform:uppercase; margin-bottom:0.3rem;\">Explanation</strong>
            <div style=\"white-space:pre-wrap; color:#333;\">${escapeHTML(mgExpl)}</div>
          </div>` : ''}
      </div>`;
    });

    window.nonLocalLabels.forEach(lbl=>{
      const present = !!(window.nonLocalPresence && window.nonLocalPresence[lbl]);
      const selected = !!nonlocalSelections[lbl];
      if (!selected && !present) return;
      const ok = selected && present;
      if (ok) currentCorrect++; else currentIncorrect++;
      let mgExpl = null;
      if (!ok && present) {
        try { const arr = window.medgemmaExplanations && window.medgemmaExplanations[lbl]; if (Array.isArray(arr) && arr.length) mgExpl = arr[0]; } catch(_) {}
      }
      similarityScoresDiv.innerHTML += `
      <div style="margin-bottom:0.8rem; padding:0.8rem; background:${ok?'rgba(40,167,69,0.08)':'rgba(220,53,69,0.08)'}; border:1px solid ${ok?'rgba(40,167,69,0.35)':'rgba(220,53,69,0.35)'}; border-left:4px solid ${ok?'#28a745':'#dc3545'}; border-radius:8px; position:relative;">
        <div style="display:flex; align-items:center; justify-content:space-between; gap:0.5rem; margin-bottom:0.35rem;">
          <div><span style=\"background:#fff; color:#8B0000; padding:0.18rem 0.5rem; border-radius:3px; font-size:0.7rem; font-weight:700; border:1px solid #8B0000;\">SELECT</span></div>
          <span class="pill ${ok?'success':'danger'}" style="display:inline-flex; align-items:center; gap:0.35rem; padding:0.18rem 0.55rem; border-radius:9999px; font-weight:700; font-size:0.8rem; ${ok?'background:linear-gradient(135deg,#28a745,#20c997); color:#fff;':'background:linear-gradient(135deg,#dc3545,#c82333); color:#fff;'}">
            ${ok?'Correct':'Incorrect'}
          </span>
        </div>
        <div style="text-align:center; padding-top:0.1rem;"><span style="font-weight:700;">${lbl}</span></div>
        <div style="margin-top:0.4rem; font-size:0.9rem; color:#666;">${present?'Present':'Not Present'}</div>
        ${mgExpl ? `<div style=\"margin-top:0.55rem; font-size:0.75rem; line-height:1.25; background:#fff; border:1px solid rgba(139,0,0,0.25); padding:0.55rem 0.6rem; border-radius:6px;\">
            <strong style=\"display:block; color:#8B0000; font-size:0.65rem; letter-spacing:0.5px; text-transform:uppercase; margin-bottom:0.3rem;\">Explanation</strong>
            <div style=\"white-space:pre-wrap; color:#333;\">${escapeHTML(mgExpl)}</div>
          </div>` : ''}
      </div>`;
    });

    caseResultsDiv.innerHTML = `
      <div style="text-align:center; padding:1.5rem; background:linear-gradient(135deg, rgba(139,0,0,0.05) 0%, rgba(128,0,32,0.05) 100%); border-radius:12px; border:2px solid rgba(139,0,0,0.2);">
        <div style="display:flex; justify-content:center; gap:2rem; margin-bottom:1.25rem; flex-wrap:wrap;">
          <div style="min-width:180px; background:#fff; border:1px solid rgba(40,167,69,0.25); border-left:4px solid #28a745; border-radius:10px; padding:0.9rem 1rem;">
            <div style="display:flex; align-items:center; justify-content:center; gap:0.5rem; color:#28a745; font-weight:800; font-size:1.8rem;"><span>${currentCorrect}</span></div>
            <div style="font-size:0.95rem; color:#2f8f57; font-weight:600;">Correct findings</div>
          </div>
          <div style="min-width:180px; background:#fff; border:1px solid rgba(220,53,69,0.25); border-left:4px solid #dc3545; border-radius:10px; padding:0.9rem 1rem;">
            <div style="display:flex; align-items:center; justify-content:center; gap:0.5rem; color:#dc3545; font-weight:800; font-size:1.8rem;"><span>${currentIncorrect}</span></div>
            <div style="font-size:0.95rem; color:#b63a46; font-weight:600;">Incorrect findings</div>
          </div>
        </div>
        <div style="font-size:1.05rem; font-weight:700; color:#8B0000; margin-bottom:0.6rem;">Total findings: ${currentCorrect+currentIncorrect}</div>
        <div style="width:100%; height:8px; background:#f8f9fa; border-radius:999px; overflow:hidden; margin-bottom:0.6rem; border:1px solid #e9ecef;">
          <div style="width:${(currentCorrect+currentIncorrect)>0 ? (currentCorrect/(currentCorrect+currentIncorrect)*100) : 0}%; height:100%; background:linear-gradient(90deg,#28a745,#20c997);"></div>
        </div>
        <div style="font-size:0.9rem; color:#666;">Accuracy: ${(currentCorrect+currentIncorrect)>0 ? ((currentCorrect/(currentCorrect+currentIncorrect)*100).toFixed(1)) : 0}%</div>
      </div>`;

    const prevC = parseInt(localStorage.getItem(k('correct')))||0;
    const prevI = parseInt(localStorage.getItem(k('incorrect')))||0;
    const prevCases = parseInt(localStorage.getItem(k('cases')))||0;
    const totalC = prevC + currentCorrect;
    const totalI = prevI + currentIncorrect;
    const totalCases = prevCases + 1;
    localStorage.setItem(k('correct'), totalC);
    localStorage.setItem(k('incorrect'), totalI);
    localStorage.setItem(k('cases'), totalCases);
    // Track recent 5 case performance (active localization only)
    try {
      let recent = JSON.parse(localStorage.getItem(k('recent_cases'))||'[]');
      if(!Array.isArray(recent)) recent = [];
      recent.push({ c: currentCorrect, i: currentIncorrect, t: Date.now() });
      if (recent.length > 5) recent = recent.slice(-5);
      localStorage.setItem(k('recent_cases'), JSON.stringify(recent));
    } catch(e) {}
    updateCounter();

    // Immediately persist a snapshot so server is in-sync even before Next/Logout
    try { saveProgressSnapshot(); } catch(e) {}
    // Refresh server-backed banner numbers to match DB after case completion
    try { refreshServerSummary(); } catch(e) {}

    const currentImages = parseInt(localStorage.getItem(k('images'))) || 1;
    const start = parseInt(localStorage.getItem(k('session_start'))) || Date.now();
    const paused = localStorage.getItem(k('paused')) === 'true';
    const pstart = parseInt(localStorage.getItem(k('pause_start'))) || 0;
    const acc = parseInt(localStorage.getItem(k('accumulated_pause_time'))) || 0;
    const elapsed = paused ? (pstart - start - acc) : (Date.now() - start - acc);

    const metadata = {
      is_correct: currentCorrect > currentIncorrect,
      current_correct: currentCorrect,
      current_incorrect: currentIncorrect,
      total_correct: totalC,
      total_incorrect: totalI,
      total_cases: totalCases,
      images_processed: currentImages,
      session_time_ms: elapsed,
      session_time_formatted: `${Math.floor(elapsed / 3600000)}:${Math.floor((elapsed % 3600000)/60000)}:${Math.floor((elapsed % 60000)/1000)}`,
      bounding_boxes: {
        ground_truth: gtBoxes.map(box=>({
          label: window.localLabels[parseInt(box.id.split('-')[0])] || 'Unknown',
          coordinates: [box.nx1, box.ny1, box.nx2, box.ny2],
          // confidence_score removed
        })),
        user_submission: boxes.map(box=>({
          label: window.localLabels[box.idx] || 'Unknown',
          coordinates: [box.x/canvas.width, box.y/canvas.height, (box.x+box.w)/canvas.width, (box.y+box.h)/canvas.height]
        }))
      },
      nonlocalizable_selections: nonlocalSelections,
      image_id: (function(){
        const src = (document.getElementById('cxr-img')||{}).getAttribute ? document.getElementById('cxr-img').getAttribute('src') : '';
        return src ? src.split('/').pop() : (window.location.pathname.split('/').pop() || 'current_image');
      })()
    };

    // Build selections payload for DB: include toggles, normalized user boxes, and set of labels drawn
    const selections = {
      nonlocalizable: { ...nonlocalSelections },
      user_boxes: (boxes||[]).map(b=>({ label: window.localLabels[b.idx] || 'Unknown', coordinates:[b.x/canvas.width, b.y/canvas.height, (b.x+b.w)/canvas.width, (b.y+b.h)/canvas.height] })),
      localize_selected_labels: Array.from(new Set((boxes||[]).map(b=> window.localLabels[b.idx] || 'Unknown')))
    };

    // Use image filename as the case_id so server can resolve ground truth
    const imgEl = document.getElementById('cxr-img');
    const caseId = imgEl && imgEl.getAttribute('src') ? imgEl.getAttribute('src').split('/').pop() : `case_${Date.now()}`;

    // Persist case completion and hold the promise for navigation safety
    lastCaseSavePromise = sendCaseCompletionData(caseId, metadata, selections, elapsed, { correct: currentCorrect, incorrect: currentIncorrect });

    const resultsDiv2 = document.getElementById('results');
    resultsDiv2.innerHTML += `
      <div style="text-align:center; margin:3rem 0;">
        <button id="next-btn" style="background:linear-gradient(135deg, #8B0000 0%, #800020 100%); color:white; border:none; padding:1rem 2rem; font-size:1.1rem; font-weight:bold; border-radius:12px; cursor:pointer;">Next Image</button>
      </div>`;
    const nextBtn = document.getElementById('next-btn');
    nextBtn.addEventListener('click', async ()=>{
  const cur = parseInt(localStorage.getItem(k('images'))) || 0;
      // Ensure the case completion has been saved before moving on
      if (lastCaseSavePromise) {
        try { await lastCaseSavePromise; } catch(e) { /* already logged */ }
      }
      // Persist snapshot with incremented image count before moving on using keepalive fetch
      try { await saveProgressSnapshot(cur + 1, '/api/progress/snapshot'); } catch(e) {}
  // Update banner with latest DB numbers before reload (best effort)
      try { await refreshServerSummary(); } catch(e) {}
      // Persist timer checkpoint to the server for continuous timer across cases
      try {
        const totalNow = elapsedNow();
        await fetch('/api/user_timer_checkpoint', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ timer_checkpoint_ms: totalNow }), keepalive: true
        });
      } catch(e) { /* ignore */ }
      // Reset client timer start and carry forward base via shared helper (mirrors active mode behavior)
      if (typeof window.RG_afterCaseAdvance === 'function') {
        window.RG_afterCaseAdvance();
      } else {
        const currentTotal = elapsedNow();
        localStorage.setItem(k('timer_base_ms'), String(currentTotal));
        localStorage.setItem(k('session_start'), Date.now().toString());
        localStorage.setItem(k('accumulated_pause_time'), '0');
        localStorage.setItem(k('paused'), 'false');
        localStorage.setItem(k('pause_start'), '0');
      }
  localStorage.setItem(k('images'), (cur+1).toString());
      // slight delay to ensure network stack queues the request
      setTimeout(()=> window.location.reload(), 50);
    });
  });

  // The following code is for the legacy case submission flow and should be removed in the future
  /*
  document.getElementById('submit-btn').addEventListener('click', ()=>{
    const userBoxesArray = window.boxes || boxes;
    const userBoxes = userBoxesArray.map(b=>({ x1:b.x, y1:b.y, x2:b.x+b.w, y2:b.y+b.h }));
    const gtBoxes = []; Object.entries(window.actualBoxes).forEach(([lbl, arr])=>{ let idx = window.localLabels.indexOf(lbl); if (idx<0) idx = window.nonLocalLabels.indexOf(lbl); arr.forEach((b,bi)=>{ const x1=b[0]*canvas.width, y1=b[1]*canvas.height, x2=b[2]*canvas.width, y2=b[3]*canvas.height; gtBoxes.push({ id:`${idx}-${bi}`, x1,y1,x2,y2, nx1:b[0], ny1:b[1], nx2:b[2], ny2:b[3], reported:false }); }); }); // redraw to ensure gt boxes are visible
    redraw();
    setTimeout(redraw, 50);

    const metadata = {
      is_correct: false, current_correct: 0, current_incorrect: 0, total_correct: 0, total_incorrect: 0, total_cases: 0,
      images_processed: (parseInt(localStorage.getItem(k('images'))) || 1),
      session_time_ms: elapsedNow(),
      session_time_formatted: fmt(elapsedNow()),
      bounding_boxes: {
        ground_truth: gtBoxes.map(box=>({
          label: window.localLabels[parseInt(box.id.split('-')[0])] || 'Unknown',
          coordinates: [box.nx1, box.ny1, box.nx2, box.ny2],
          // confidence_score removed
        })),
        user_submission: userBoxes.map(box=>({
          label: window.localLabels[box.idx] || 'Unknown',
          coordinates: [box.x/canvas.width, box.y/canvas.height, (box.x+box.w)/canvas.width, (box.y+box.h)/canvas.height]
        }))
      },
      nonlocalizable_selections: Object.fromEntries(Object.keys(nonlocalSelections).map(k=>[k, nonlocalSelections[k]])),
      image_id: window.location.pathname === '/' ? 'current_image' : window.location.pathname.split('/').pop()
    };

    // Legacy submission endpoint, to be removed in the future
    fetch('/api/legacy_complete_case', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({ metadata }),
      keepalive: true
    }).then(res=>{
      if (res.ok) {
        alert('Case submitted successfully (legacy endpoint).');
        window.location.reload();
      } else {
        alert('Failed to submit case. Please try again.');
      }
    }).catch(err=>{
      console.error('Error submitting case:', err);
      alert('Error submitting case. Please try again.');
    });
  });
  */

  window.addEventListener('beforeunload', function(){
    // Send a final snapshot best-effort via beacon
    try { saveProgressBeacon(); } catch(e) {}
    const start = parseInt(localStorage.getItem(k('session_start'))) || Date.now();
    const paused = localStorage.getItem(k('paused')) === 'true';
    const pstart = parseInt(localStorage.getItem(k('pause_start'))) || 0;
    const acc = parseInt(localStorage.getItem(k('accumulated_pause_time'))) || 0;
    const total = paused ? (pstart - start - acc) : (Date.now() - start - acc);
    localStorage.setItem(k('total_time'), ((parseInt(localStorage.getItem(k('total_time'))||'0') + total)).toString());
  });
})();