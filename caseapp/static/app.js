/* ระบบรายงานข้อมูลคดีและ Case ID — แอนิเมชันและตัวช่วยหน้าเว็บ
   ปิดแอนิเมชันอัตโนมัติเมื่อผู้ใช้ตั้ง "ลดการเคลื่อนไหว" ในระบบปฏิบัติการ */
(function () {
  'use strict';
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var $$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  // ---------------------------------------------------------------- แถบโหลดด้านบน
  var bar = document.getElementById('topbar');
  function startBar() { if (!bar) return; bar.className = ''; void bar.offsetWidth; bar.className = 'go'; }
  function endBar() { if (!bar) return; bar.className = 'done'; }
  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[href]');
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    var href = a.getAttribute('href');
    if (!href || href.charAt(0) === '#' || a.target === '_blank' || /\/export(\?|$)/.test(href)) return;
    startBar();
  });
  document.addEventListener('submit', function (e) {
    if (!e.defaultPrevented) startBar();
  });
  window.addEventListener('pageshow', endBar);

  // ---------------------------------------------------------------- ข้อความแจ้งเตือนแบบ toast
  $$('.toasts .flash').forEach(function (el, i) {
    el.style.animationDelay = (i * 90) + 'ms';
    var x = document.createElement('button');
    x.className = 'x'; x.type = 'button'; x.setAttribute('aria-label', 'ปิด'); x.textContent = '×';
    x.onclick = function () { dismiss(el); };
    el.appendChild(x);
    if (!el.classList.contains('error')) {  // ข้อความผิดพลาดค้างไว้จนกว่าจะปิดเอง
      var life = document.createElement('span'); life.className = 'life'; el.appendChild(life);
      setTimeout(function () { dismiss(el); }, 7000);
    }
  });
  function dismiss(el) {
    if (el.classList.contains('leaving')) return;
    el.classList.add('leaving');
    setTimeout(function () { el.remove(); }, 400);
  }

  // ---------------------------------------------------------------- ระลอกคลื่นบนปุ่ม
  document.addEventListener('pointerdown', function (e) {
    var b = e.target.closest && e.target.closest('.btn');
    if (!b || reduce || b.disabled) return;
    var r = b.getBoundingClientRect(), s = Math.max(r.width, r.height);
    var dot = document.createElement('span');
    dot.className = 'ripple';
    dot.style.cssText = 'width:' + s + 'px;height:' + s + 'px;left:' + (e.clientX - r.left - s / 2) + 'px;top:' + (e.clientY - r.top - s / 2) + 'px';
    b.appendChild(dot);
    setTimeout(function () { dot.remove(); }, 700);
  });

  // ---------------------------------------------------------------- เนื้อหาค่อย ๆ ลอยขึ้นทีละชิ้น
  if (!reduce) {
    var blocks = $$('main > *:not(script):not(form[hidden])').concat($$('.stats > *, .rings > *, .grid-cards > *'));
    var n = 0;
    blocks.forEach(function (el) {
      if (el.closest('.auth-wrap')) return;
      el.classList.add('reveal'); el.style.setProperty('--i', n++ % 14);
    });
    $$('table.data tbody tr').slice(0, 40).forEach(function (tr, i) {
      tr.classList.add('reveal-row'); tr.style.setProperty('--i', i);
    });
  }

  // ---------------------------------------------------------------- ตัวเลขนับขึ้น
  function countUp(el) {
    var target = parseInt(el.textContent.replace(/[^\d]/g, ''), 10);
    if (!target || reduce) return;
    var dur = Math.min(1600, 600 + target * 2), t0 = null, fmt = el.textContent.indexOf(',') !== -1 || target >= 1000;
    function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min(1, (ts - t0) / dur), v = Math.round(target * (1 - Math.pow(1 - p, 3)));
      el.textContent = fmt ? v.toLocaleString('en-US') : String(v);
      if (p < 1) requestAnimationFrame(step);
    }
    el.textContent = '0';
    requestAnimationFrame(step);
  }
  var io = 'IntersectionObserver' in window ? new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (!en.isIntersecting) return;
      io.unobserve(en.target);
      if (en.target.classList.contains('ring')) fillRing(en.target); else countUp(en.target);
    });
  }, {threshold: .3}) : null;
  $$('.stat .v, [data-count]').forEach(function (el) { io ? io.observe(el) : countUp(el); });

  // ---------------------------------------------------------------- วงแหวนเปอร์เซ็นต์
  function fillRing(svg) {
    var pct = Math.max(0, Math.min(100, parseFloat(svg.getAttribute('data-pct')) || 0));
    var bar = svg.querySelector('.bar');
    bar.style.strokeDashoffset = String(251.33 * (1 - pct / 100));
    var label = svg.parentNode.querySelector('.pct');
    if (label && !reduce) {
      var t0 = null;
      (function step(ts) {
        if (!t0) t0 = ts;
        var p = Math.min(1, (ts - t0) / 1600);
        label.textContent = Math.round(pct * (1 - Math.pow(1 - p, 3))) + '%';
        if (p < 1) requestAnimationFrame(step);
      })(performance.now());
    }
  }
  $$('svg.ring').forEach(function (el) { io ? io.observe(el) : fillRing(el); });

  // ---------------------------------------------------------------- ค้นหาในตาราง <input data-filter="#id">
  $$('input[data-filter]').forEach(function (inp) {
    var tbl = document.querySelector(inp.dataset.filter);
    if (!tbl) return;
    inp.addEventListener('input', function () {
      var q = inp.value.trim().toLowerCase(), shown = 0;
      $$('tbody tr', tbl).forEach(function (tr) {
        var hit = !q || tr.textContent.toLowerCase().indexOf(q) !== -1;
        tr.style.display = hit ? '' : 'none';
        if (hit) shown++;
      });
      var out = document.querySelector(inp.dataset.count || '');
      if (out) out.textContent = shown;
    });
  });
  $$('form[data-confirm]').forEach(function (f) {
    f.addEventListener('submit', function (e) { if (!confirm(f.dataset.confirm)) { e.preventDefault(); endBar(); } });
  });

  // ---------------------------------------------------------------- พื้นหลังเครือข่ายเคลื่อนไหว (หน้าเข้าสู่ระบบ)
  var cv = document.getElementById('net');
  if (cv && cv.getContext) {
    var ctx = cv.getContext('2d'), W, H, dpr = Math.min(2, window.devicePixelRatio || 1), pts = [], mouse = {x: -1e4, y: -1e4};
    function size() {
      W = cv.clientWidth; H = cv.clientHeight; cv.width = W * dpr; cv.height = H * dpr;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      var want = Math.round(Math.min(110, W * H / 14000));
      while (pts.length < want) pts.push({x: Math.random() * W, y: Math.random() * H,
        vx: (Math.random() - .5) * .35, vy: (Math.random() - .5) * .35, gold: Math.random() < .18});
      pts.length = want;
    }
    function frame() {
      ctx.clearRect(0, 0, W, H);
      for (var i = 0; i < pts.length; i++) {
        var p = pts[i];
        if (!reduce) { p.x += p.vx; p.y += p.vy; }
        if (p.x < 0 || p.x > W) p.vx *= -1;
        if (p.y < 0 || p.y > H) p.vy *= -1;
        for (var j = i + 1; j < pts.length; j++) {
          var q = pts[j], dx = p.x - q.x, dy = p.y - q.y, d = dx * dx + dy * dy;
          if (d < 15000) {
            ctx.strokeStyle = 'rgba(34,211,238,' + (0.16 * (1 - d / 15000)) + ')';
            ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke();
          }
        }
        var md = (p.x - mouse.x) * (p.x - mouse.x) + (p.y - mouse.y) * (p.y - mouse.y);
        if (md < 30000) {
          ctx.strokeStyle = 'rgba(232,205,133,' + (0.35 * (1 - md / 30000)) + ')';
          ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(mouse.x, mouse.y); ctx.stroke();
        }
        ctx.fillStyle = p.gold ? 'rgba(232,205,133,.9)' : 'rgba(34,211,238,.75)';
        ctx.beginPath(); ctx.arc(p.x, p.y, p.gold ? 2.2 : 1.6, 0, 6.283); ctx.fill();
      }
      if (!reduce && !document.hidden) requestAnimationFrame(frame);
    }
    size(); frame();
    window.addEventListener('resize', size);
    window.addEventListener('pointermove', function (e) { mouse.x = e.clientX; mouse.y = e.clientY; });
    document.addEventListener('visibilitychange', function () { if (!document.hidden && !reduce) requestAnimationFrame(frame); });
  }

  // ---------------------------------------------------------------- ดาวน์โหลด: ถามชื่อผู้ดาวน์โหลดทุกครั้ง
  var dlg = document.getElementById('dl-dialog');
  if (dlg && typeof dlg.showModal === 'function') {
    var dlName = document.getElementById('dl-name'), pending = null;
    document.addEventListener('click', function (e) {
      var a = e.target.closest && e.target.closest('a[data-download]');
      if (!a || e.button !== 0) return;
      e.preventDefault();
      pending = a.getAttribute('href');
      var remembered = '';
      try { remembered = localStorage.getItem('dl-name') || ''; } catch (_) {}
      dlName.value = remembered || a.dataset.name || '';
      dlg.showModal();
      setTimeout(function () { dlName.focus(); dlName.select(); }, 30);
    });
    document.getElementById('dl-cancel').onclick = function () { dlg.close(); };
    document.getElementById('dl-form').addEventListener('submit', function (e) {
      var name = dlName.value.replace(/\s+/g, ' ').trim();
      if (name.length < 2) { e.preventDefault(); dlName.focus(); return; }
      try { localStorage.setItem('dl-name', name); } catch (_) {}
      if (pending) window.location = pending + (pending.indexOf('?') === -1 ? '?' : '&') + 'by=' + encodeURIComponent(name);
    });
  }

  // ---------------------------------------------------------------- คัดลอกข้อความ (ลิงก์ส่วนตัว)
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-copy]');
    if (!b) return;
    var src = document.querySelector(b.dataset.copy);
    if (!src) return;
    var text = src.value || src.textContent;
    var done = function () { var t = b.textContent; b.textContent = 'คัดลอกแล้ว ✓'; setTimeout(function () { b.textContent = t; }, 1600); };
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () { src.select && src.select(); });
    else { src.select && src.select(); document.execCommand('copy'); done(); }
  });

  // ให้หน้าอื่นใช้แถบโหลด/แจ้งเตือนได้
  window.UI = {startBar: startBar, endBar: endBar};
})();
