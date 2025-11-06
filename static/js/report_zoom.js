// Shared zoom/pan initializer for report pages (practice and test)
(function(global){
  function initReportZoom(){
    const viewport = document.getElementById('report-viewport');
    const container = document.getElementById('report-img-container');
    const slider = document.getElementById('report-zoom-slider');
    const level = document.getElementById('report-zoom-level');
    const reset = document.getElementById('report-zoom-reset');
    if (!viewport || !container) return;
    global.REPORT_ZOOM = global.REPORT_ZOOM || 1;
    function apply(){
      const z = Math.max(1, Math.min(6, global.REPORT_ZOOM||1));
      global.REPORT_ZOOM = z;
      viewport.style.transform = `scale(${z})`;
      if (level) level.textContent = `${Math.round(z*100)}%`;
      try { document.dispatchEvent(new CustomEvent('report-zoom-changed', { detail: { zoom: z } })); } catch(_) {}
    }
    function styleSlider(){
      if (!slider) return;
      const min = parseInt(slider.min,10) || 100;
      const max = parseInt(slider.max,10) || 600;
      const val = parseInt(slider.value,10) || 100;
      const pct = ((val - min) * 100) / (max - min);
      slider.style.background = `linear-gradient(to right, #8B0000 0%, #8B0000 ${pct}%, #ddd ${pct}%, #ddd 100%)`;
    }
    if (slider){
      slider.addEventListener('input', ()=>{ global.REPORT_ZOOM = Math.max(1, Math.min(6, parseInt(slider.value,10)/100)); apply(); styleSlider(); });
      styleSlider();
    }
    if (reset){
      reset.addEventListener('click', ()=>{ global.REPORT_ZOOM = 1; if (slider){ slider.value = '100'; styleSlider(); } apply(); });
    }
    // Panning
    let pan=false, sx=0, sy=0, sl=0, st=0;
    function start(e){ pan=true; container.classList.add('panning'); sx=e.clientX; sy=e.clientY; sl=container.scrollLeft; st=container.scrollTop; e.preventDefault(); }
    function move(e){ if(!pan) return; container.scrollLeft = sl - (e.clientX - sx); container.scrollTop = st - (e.clientY - sy); }
    function end(){ if(!pan) return; pan=false; container.classList.remove('panning'); }
    container.classList.add('pannable');
    container.addEventListener('mousedown', (e)=>{ if(e.button===0) start(e); }, true);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', end);
    apply();
  }
  global.initReportZoom = initReportZoom;
})(window);
