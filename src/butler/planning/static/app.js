// The planning pages. One shell, three views, no inline script (the CSP forbids it).
// Everything the user says goes to the service one entry at a time; the item's
// file is never written from here.
(function () {
  'use strict';

  const HEADER = { 'X-Butler-Planning': '1' };
  const main = document.getElementById('main');
  const crumbs = document.getElementById('crumbs');
  const live = document.getElementById('live');
  const bar = document.getElementById('bar');
  const toastEl = document.getElementById('toast');
  let timer = null;
  let stream = null;
  // The version this page's script was served as: /static/<version>/app.js.
  const VERSION = ((document.currentScript && document.currentScript.src) || '').split('/static/')[1]?.split('/')[0] || '';

  // ---- small helpers ------------------------------------------------------- //

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
    for (const kid of kids.flat()) {
      if (kid === null || kid === undefined || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  let markedReady = false;
  function md(text, cls) {
    const div = h('div', { class: 'prose' + (cls ? ' ' + cls : '') });
    if (window.marked && !markedReady) {
      // Raw HTML in a plan is shown as text, never rendered: this page records approvals.
      window.marked.use({ gfm: true, breaks: false,
        renderer: { html(t) { return esc(typeof t === 'string' ? t : (t.raw || t.text || '')); } } });
      markedReady = true;
    }
    if (!text) return div;
    div.innerHTML = window.marked ? window.marked.parse(String(text)) : esc(text);
    div.querySelectorAll('a[href]').forEach(a => {
      const href = a.getAttribute('href');
      if (/^\s*(javascript|data|vbscript):/i.test(href)) a.removeAttribute('href');
      else if (/^https?:/i.test(href)) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
    });
    return div;
  }

  // One line of Markdown (a title, a label): code and emphasis, no blocks.
  function mdi(text) {
    const span = h('span', { class: 'prose' });
    if (!text) return span;
    md('', null);  // configures marked once
    span.innerHTML = window.marked ? window.marked.parseInline(String(text)) : esc(text);
    span.querySelectorAll('a[href]').forEach(a => { if (/^\s*(javascript|data|vbscript):/i.test(a.getAttribute('href'))) a.removeAttribute('href'); });
    return span;
  }

  function toast(msg) {
    toastEl.textContent = msg; toastEl.classList.add('on');
    clearTimeout(toast.t); toast.t = setTimeout(() => toastEl.classList.remove('on'), 2500);
  }

  function say(msg, err) { live.textContent = msg || ''; live.className = 'live' + (err ? ' err' : ''); }

  async function api(method, path, body) {
    const opt = { method, cache: 'no-store', headers: Object.assign({}, HEADER) };
    if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    const r = await fetch(path, opt);
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
    return data;
  }

  function badge(text, cls) { return h('span', { class: 'badge ' + (cls || '') }, text); }

  function setCrumbs(parts) {
    crumbs.replaceChildren(...parts.map(p => p.href ? h('a', { href: p.href }, p.text) : h('span', {}, p.text)));
  }

  function poll(fn, ms) {
    clearInterval(timer);
    timer = setInterval(() => { if (document.visibilityState === 'visible') fn(); }, ms);
  }

  // Reload the moment a file behind this page changes: the service watches them
  // and says so over Server-Sent Events. The timer in poll() stays as a slow
  // fallback, for a stream that is down or a proxy that buffers it.
  function watch(fn, scope, slug) {
    if (stream) stream.close();
    if (!window.EventSource) return;
    const q = new URLSearchParams({ scope });
    if (slug) q.set('slug', slug);
    stream = new EventSource('/api/events?' + q);
    stream.addEventListener('hello', e => {
      let v = '';
      try { v = JSON.parse(e.data).version; } catch (err) { v = ''; }
      // The service was upgraded under this page: load the new script too.
      if (v && VERSION && v !== VERSION) location.reload();
    });
    stream.addEventListener('change', () => fn());
    stream.onerror = () => say('live updates paused; retrying', true);
    stream.onopen = () => say('');
  }

  function typing() {
    const a = document.activeElement;
    return a && (a.tagName === 'TEXTAREA' || (a.tagName === 'INPUT' && a.type === 'text'));
  }

  // ---- index --------------------------------------------------------------- //

  async function viewIndex() {
    setCrumbs([]);
    document.title = 'Planning';
    bar.hidden = true;
    async function load() {
      let data;
      try { data = await api('GET', '/api/repos'); say(''); } catch (e) { say('the service does not answer: ' + e.message, true); return; }
      if (typing()) return;
      const repos = data.repos || [];
      const out = [h('h1', {}, 'Planning')];
      const waiting = repos.filter(r => (r.waiting || []).length);
      if (waiting.length) {
        const box = h('div', { class: 'waiting' }, h('strong', {}, 'Waiting on you'));
        for (const r of waiting) {
          const ul = h('ul');
          for (const w of r.waiting) {
            ul.append(h('li', {}, h('a', { href: `/r/${r.slug}/i/${w.id}` }, w.title), ' ',
              h('span', { class: 'muted small' }, w.for_user.map(x => x.what + (x.local ? ' ' + x.local : '')).join(' · '))));
          }
          box.append(h('div', {}, h('a', { href: `/r/${r.slug}/` }, r.name), ul));
        }
        out.push(box);
      } else if (repos.length) {
        out.push(h('p', { class: 'lead' }, 'Nothing waits on you.'));
      }
      if (!repos.length) {
        out.push(h('p', { class: 'lead' }, 'No repository is registered. Run ',
          h('code', {}, 'python butler.py planning serve'), ' in one.'));
      }
      // Worktrees under their main checkout: the one whose .git is the common dir.
      const groups = new Map();
      for (const r of repos) {
        const key = r.common || r.path;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
      }
      out.push(h('h2', {}, 'Repositories'));
      const grid = h('div', { class: 'grid' });
      for (const [common, list] of groups) {
        list.sort((a, b) => (common === a.path + '/.git' ? -1 : 0) - (common === b.path + '/.git' ? -1 : 0) || a.slug.localeCompare(b.slug));
        const [first, ...rest] = list;
        const card = h('div', { class: 'card item-card' }, repoLine(first));
        if (rest.length) {
          const d = h('details', {}, h('summary', { class: 'small muted' }, `${rest.length} worktree${rest.length > 1 ? 's' : ''}`));
          for (const r of rest) d.append(h('div', { class: 'small' }, repoLine(r)));
          card.append(d);
        }
        grid.append(card);
      }
      out.push(grid);
      main.replaceChildren(...out);
    }
    function repoLine(r) {
      const counts = {};
      for (const it of r.items || []) counts[it.folder] = (counts[it.folder] || 0) + 1;
      const line = h('div', {}, h('a', { class: 'title', href: `/r/${r.slug}/` }, r.name), ' ',
        r.branch ? h('span', { class: 'muted small' }, r.branch) : null);
      const meta = h('div', { class: 'meta' }, r.path);
      if (r.missing) {
        meta.append(' — ', badge('missing', 'warn'), ' ',
          h('button', { class: 'link', onclick: async () => {
            try { await api('DELETE', `/api/repos/${r.slug}`); viewIndex(); } catch (e) { toast(e.message); }
          } }, 'Forget'));
      } else {
        const parts = ['draft', 'todo', 'in-progress', 'done', 'notes'].filter(k => counts[k]).map(k => `${k} ${counts[k]}`);
        if (!r.typed) parts.push('not typed' + ((r.legacy || []).length ? ` · ${r.legacy.length} Markdown` : ''));
        meta.append(h('div', {}, parts.join(' · ')));
      }
      return h('div', {}, line, meta);
    }
    await load();
    watch(load, 'index');
    poll(load, 30000);
  }

  // ---- one repository ------------------------------------------------------ //

  async function viewRepo(slug) {
    bar.hidden = true;
    async function load() {
      let r;
      try { r = await api('GET', `/api/r/${slug}`); say(''); } catch (e) { say(e.message, true); return; }
      if (typing()) return;
      document.title = r.name + ' · planning';
      setCrumbs([{ text: r.name }]);
      const out = [h('h1', {}, r.name), h('p', { class: 'lead mono small' }, r.path + (r.branch ? '  (' + r.branch + ')' : ''))];
      if (r.missing) { out.push(h('p', {}, 'This repository is no longer on disk.')); main.replaceChildren(...out); return; }
      if (!r.typed) {
        out.push(h('div', { class: 'problems warn' }, 'Not a typed planning folder: ',
          h('code', {}, 'python butler.py planning init'), ' makes one. Markdown plans are shown read-only.'));
      } else if (r.errors || r.warnings) {
        out.push(h('div', { class: 'problems' + (r.errors ? '' : ' warn') },
          `planning check: ${r.errors} error${r.errors === 1 ? '' : 's'}, ${r.warnings} warning${r.warnings === 1 ? '' : 's'}. `,
          h('code', {}, 'python butler.py planning check'), ' says which.'));
      }
      if ((r.waiting || []).length) {
        const box = h('div', { class: 'waiting' }, h('strong', {}, 'Waiting on you'));
        const ul = h('ul');
        for (const w of r.waiting) ul.append(h('li', {}, h('a', { href: `/r/${slug}/i/${w.id}` }, w.title), ' ',
          h('span', { class: 'muted small' }, w.for_user.map(x => x.what + (x.local ? ' ' + x.local : '')).join(' · '))));
        box.append(ul); out.push(box);
      }
      for (const folder of ['draft', 'todo', 'in-progress', 'done', 'notes']) {
        const items = (r.items || []).filter(i => i.folder === folder);
        if (!items.length) continue;
        out.push(h('h2', {}, folder + '/'));
        const grid = h('div', { class: 'grid' });
        for (const it of items) grid.append(itemCard(slug, it));
        out.push(grid);
      }
      const legacy = (r.legacy || []).concat(r.forms || []);
      if (legacy.length) {
        const d = h('details', {}, h('summary', {}, `Older files (${legacy.length}), read-only`));
        const ul = h('ul');
        for (const p of r.legacy || []) ul.append(h('li', {}, h('a', { href: `/r/${slug}/m/${p}` }, p)));
        for (const p of r.forms || []) ul.append(h('li', {}, h('a', { href: `/r/${slug}/f/${p}` }, p)));
        d.append(ul);
        out.push(h('h2', {}, 'Other files'), d);
      }
      main.replaceChildren(...out);
    }
    function itemCard(slug, it) {
      if (it.broken) {
        return h('div', { class: 'card item-card' }, h('div', {}, it.file), h('div', { class: 'meta' }, badge('cannot be read', 'warn'), ' see planning check'));
      }
      const pct = it.steps.total ? Math.round(100 * it.steps.done / it.steps.total) : 0;
      const card = h('div', { class: 'card item-card' },
        h('div', {}, badge(it.kind, 'muted'), ' ', h('a', { class: 'title', href: `/r/${slug}/i/${it.id}` }, mdi(it.title))),
        h('div', { class: 'meta mono' }, it.id));
      if (it.steps.total) {
        card.append(h('div', { class: 'progress' }, h('span', { style: null })),
          h('div', { class: 'meta' }, `${it.steps.done}/${it.steps.total} steps`));
        card.querySelector('.progress > span').style.width = pct + '%';
      }
      const b = h('div', { class: 'badges' });
      if (it.for_user.length) b.append(badge(`${it.for_user.length} for you`, 'open'));
      if (it.for_agent.length) b.append(badge(`${it.for_agent.length} for the agent`, 'agent'));
      if (b.childNodes.length) card.append(b);
      return card;
    }
    await load();
    watch(load, 'repo', slug);
    poll(load, 30000);
  }

  // ---- a Markdown file, read-only ------------------------------------------ //

  async function viewMarkdown(slug, path) {
    bar.hidden = true;
    clearInterval(timer);
    setCrumbs([{ text: slug, href: `/r/${slug}/` }, { text: path }]);
    async function load() {
      try {
        const f = await api('GET', `/api/r/${slug}/file/${path}`);
        main.replaceChildren(h('p', { class: 'lead' }, badge('read-only', 'muted'), ' a Markdown plan from before format 1'), md(f.text));
      } catch (e) { main.replaceChildren(h('p', {}, e.message)); }
    }
    await load();
    watch(load, 'repo', slug);
  }

  // ---- one item ------------------------------------------------------------ //

  async function viewItem(slug, id) {
    let etag = null, view = null, pendingRender = false;
    // What the user has typed and not yet saved, by box. A re-render (after a
    // save, or when the agent changes the file) rebuilds every box from the
    // server; without this, anything unsent in them would be wiped.
    const drafts = new Map();
    function draftBox(el, key, saved) {
      el.value = drafts.has(key) ? drafts.get(key) : (saved || '');
      el.addEventListener('input', () => drafts.set(key, el.value));
      return el;
    }
    const base = `/api/r/${slug}`;

    async function load(force) {
      let r;
      try {
        r = await fetch(`${base}/item/${id}`, { cache: 'no-store', headers: etag && !force ? { 'If-None-Match': etag } : {} });
      } catch (e) { say('the service does not answer', true); return; }
      if (r.status === 304) { say(''); return; }
      if (!r.ok) { say('HTTP ' + r.status, true); if (r.status === 404) main.replaceChildren(h('p', {}, 'No such item. It may have been deleted.')); return; }
      etag = r.headers.get('ETag');
      view = await r.json();
      say('');
      if (typing()) { pendingRender = true; return; }
      render();
    }
    document.addEventListener('focusout', (e) => {
      // Leaving a text box for a button: let the click land first, or the
      // re-render would replace the button under the pointer.
      const wait = e.relatedTarget && e.relatedTarget.tagName === 'BUTTON' ? 600 : 50;
      setTimeout(() => { if (pendingRender && !typing()) { pendingRender = false; render(); } }, wait);
    });

    async function save(path, body, what, draftKey) {
      say('saving…');
      try {
        const { __draft, ...payload } = body;  // __draft: what the box held when this was sent
        await api('PUT', `${base}/${path}`, payload);
        if (draftKey && drafts.get(draftKey) === __draft) drafts.delete(draftKey);
        say('saved ' + new Date().toLocaleTimeString()); etag = null; if (what !== 'quiet') load(true);
      }
      catch (e) { say('not saved: ' + e.message, true); toast('Not saved: ' + e.message); }
    }

    function render() {
      const v = view, d = v.data, ans = v.answers || {};
      document.title = d.title + ' · planning';
      setCrumbs([{ text: v.repo.name, href: `/r/${slug}/` }, { text: v.folder + '/' }, { text: v.id }]);
      const out = [];
      out.push(h('div', { class: 'badges' }, badge(v.kind, 'muted'), badge(v.folder, 'muted'), h('span', { class: 'mono small muted' }, v.id + ' · ' + v.file)));
      out.push(h('h1', {}, d.title));
      if (d.summary) out.push(h('p', { class: 'lead' }, mdi(d.summary)));

      const probs = (v.problems || []);
      if (probs.length) {
        const errs = probs.filter(p => p.level === 'error');
        out.push(h('div', { class: 'problems' + (errs.length ? '' : ' warn') }, h('strong', {}, 'planning check'),
          h('ul', {}, probs.map(p => h('li', {}, (p.line ? `line ${p.line}: ` : '') + p.message)))));
      }
      out.push(todoPanel(v));

      const kv = [];
      for (const key of ['needs', 'context', 'related']) {
        if ((d[key] || []).length) kv.push(h('div', { class: 'kv' }, key + ': ', ...d[key].map((x, i) => [i ? ', ' : '', h('code', {}, x)])));
      }
      if ((d.touches || []).length) kv.push(h('div', { class: 'kv' }, 'touches: ', ...d.touches.map((x, i) => [i ? ', ' : '', h('code', {}, x)])));
      if (d.created) kv.push(h('div', { class: 'kv' }, `created ${d.created} · updated ${d.updated}`));
      out.push(...kv);

      if (v.kind === 'plan' && v.plan_approval_required) out.push(planApproval(v));

      if (d.why) out.push(h('h2', { class: 'reading' }, 'Why'), md(d.why, 'reading'));
      if (d.subject) out.push(h('h2', { class: 'reading' }, 'Subject'), md(d.subject, 'reading'));
      if (d.method) out.push(h('h2', { class: 'reading' }, 'Method'), md(d.method, 'reading'));
      if (d.scope && ((d.scope.in || []).length || (d.scope.out || []).length)) {
        out.push(h('h2', { class: 'reading' }, 'Scope'));
        if ((d.scope.in || []).length) out.push(h('h3', { class: 'reading' }, 'In'), h('ul', { class: 'reading' }, d.scope.in.map(x => h('li', {}, md(x)))));
        if ((d.scope.out || []).length) out.push(h('h3', { class: 'reading' }, 'Out'), h('ul', { class: 'reading' }, d.scope.out.map(x => h('li', {}, md(x)))));
      }
      if ((d.acceptance || []).length) out.push(h('h2', { class: 'reading' }, 'Acceptance'), h('ul', { class: 'reading' }, d.acceptance.map(x => h('li', {}, md(x)))));

      if ((d.section || []).length) {
        out.push(h('h2', { class: v.kind === 'note' ? '' : 'reading' }, v.kind === 'note' ? 'Sections' : 'Design'));
        d.section.forEach((s, i) => {
          const det = h('details', { class: 'section card' + (v.kind === 'note' ? '' : ' reading'), id: s.id || null, open: v.kind === 'note' || null },
            h('summary', {}, s.title), md(s.body), s.id ? comments(s.id) : null);
          out.push(det);
        });
      }

      const decisions = v.kind === 'decision' ? [Object.assign({}, d, { id: v.id })] : (d.decision || []);
      if (decisions.length) {
        const open = decisions.filter(x => !(x.resolved || []).length);
        out.push(h('h2', {}, v.kind === 'decision' ? 'The decision' : `Decisions (${open.length} open of ${decisions.length})`));
        for (const dec of decisions) out.push(decisionCard(dec, ans));
      }

      for (const ph of d.phase || []) {
        const steps = ph.step || [];
        const done = steps.filter(s => s.state === 'done' || s.state === 'dropped').length;
        const fin = steps.length && done === steps.length ? ' phase-done' : '';
        out.push(h('h2', { class: 'phase' + fin, id: ph.id }, ph.title, ' ', h('span', { class: 'muted small' }, `${ph.id} · ${done}/${steps.length}`)));
        if (ph.goal) out.push(h('p', { class: 'muted' + fin }, ph.goal));
        const pc = comments(ph.id, true); if (fin) pc.classList.add('phase-done');
        out.push(pc);
        out.push(h('ul', { class: 'steps' + fin }, steps.map(s => stepCard(s, ans))));
      }

      if ((d.finding || []).length) {
        out.push(h('h2', {}, `Findings (${d.finding.filter(f => (f.state || 'open') === 'open').length} open)`));
        for (const f of d.finding) {
          out.push(h('div', { class: 'card', id: f.id },
            h('h3', {}, badge(f.severity, f.severity === 'critical' || f.severity === 'high' ? 'gate' : 'muted'), badge(f.state || 'open', (f.state || 'open') === 'open' ? 'open' : 'ok'), mdi(f.title)),
            h('div', { class: 'kv' }, f.id + (f.area ? ' · ' + f.area : '') + (f.to ? ' · to ' + f.to : '')),
            f.details ? md(f.details) : null,
            f.evidence ? h('details', {}, h('summary', { class: 'small' }, 'Evidence'), md(f.evidence)) : null,
            f.reason ? h('p', { class: 'small muted' }, 'Reason: ' + f.reason) : null,
            comments(f.id)));
        }
      }

      if ((d.risk || []).length) {
        out.push(h('h2', { class: 'reading' }, 'Risks'), h('ul', { class: 'reading' }, d.risk.map(r => h('li', {}, md(r.what), r.mitigation ? h('div', { class: 'muted small' }, md(r.mitigation)) : null))));
      }
      if ((d.log || []).length) {
        out.push(h('h2', { class: 'reading' }, 'Log'), h('ul', { class: 'reading' }, d.log.map(l => h('li', {}, h('span', { class: 'mono small' }, l.on), ' ',
          l.step ? h('code', {}, l.step) : null, ' ', mdi(l.text), l.commit ? h('code', {}, ' ' + l.commit) : null))));
      }

      out.push(h('h2', {}, 'Comments on the whole item'), comments('', true, true));
      main.replaceChildren(...out);
      document.body.classList.toggle('focus', focusOn());
      renderBar(v, decisions, ans);
      if (location.hash && !render.scrolled) { const t = document.getElementById(location.hash.slice(1)); if (t) t.scrollIntoView(); render.scrolled = true; }
    }

    // What the user has to do, and what is not done yet, in one place at the
    // top: nobody should have to read the whole plan to find their part.
    const WHAT = { decision: 'Answer', approval: 'Approve or disapprove', 'plan-approval': 'Approve the plan',
                   step: 'Do', disapproved: 'Disapproved' };
    function jump(local) {
      return (e) => {
        e.preventDefault();
        const t = document.getElementById(local || 'plan-approval');
        if (!t) return;
        if (t.tagName === 'DETAILS') t.open = true;
        t.scrollIntoView({ behavior: 'smooth', block: 'center' });
        t.classList.remove('flash'); void t.offsetWidth; t.classList.add('flash');
      };
    }
    function todoPanel(v) {
      const s = v.summary, user = v.user || 'you';
      const now = s.for_user || [];
      const open = s.open || [];
      const later = open.filter(x => x.by === 'user' && !x.ready && !x.ticked);
      const panel = h('section', { class: 'todo card', id: 'todo' });
      panel.append(h('h2', {}, now.length ? `For you now (${now.length})` : 'Nothing waits on you right now'));
      if (now.length) {
        panel.append(h('ul', { class: 'todo-list' }, now.map(x => h('li', {},
          h('a', { href: '#' + (x.local || 'plan-approval'), onclick: jump(x.local) }, badge(WHAT[x.what] || x.what, x.what === 'step' ? 'user' : x.what === 'decision' ? 'open' : 'gate')),
          ' ', x.local ? h('span', { class: 'mono small muted' }, x.local + ' ') : null, mdi(x.text)))));
      }
      if (later.length) {
        panel.append(h('h3', {}, `Coming up for you (${later.length})`),
          h('ul', { class: 'todo-list' }, later.map(x => h('li', {},
            h('a', { href: '#' + x.id, onclick: jump(x.id) }, h('span', { class: 'mono small' }, x.id)), ' ', mdi(x.title),
            h('span', { class: 'muted small' }, ' — waits on ' + x.waiting_on.join(', '))))));
      }
      if (open.length) {
        const agent = open.filter(x => x.by === 'agent').length, mine = open.length - agent;
        const det = h('details', { class: 'todo-open' }, h('summary', {}, `Not done yet: ${open.length} step${open.length === 1 ? '' : 's'} (${mine} yours, ${agent} the agent's)`));
        det.append(h('ul', { class: 'todo-list' }, open.map(x => h('li', {},
          h('a', { href: '#' + x.id, onclick: jump(x.id) }, h('span', { class: 'mono small' }, x.id)), ' ',
          badge(x.by, x.by), ' ',
          x.state !== 'open' ? badge(x.state, x.state === 'blocked' ? 'warn' : 'muted') : null, ' ',
          x.ticked ? badge('you ticked it', 'ok') : null, ' ',
          x.gate ? badge(x.approval === 'approved' ? 'approved' : 'gate', x.approval === 'approved' ? 'ok' : 'gate') : null, ' ',
          mdi(x.title),
          x.waiting_on.length ? h('span', { class: 'muted small' }, ' — waits on ' + x.waiting_on.join(', ')) : (x.by === 'agent' ? h('span', { class: 'muted small' }, ' — ready') : null)))));
        panel.append(det);
      } else if (s.steps.total) {
        panel.append(h('p', { class: 'muted' }, 'Every step is done or dropped.'));
      }
      return panel;
    }

    function planApproval(v) {
      const pa = v.plan_approval || { status: 'none' };
      const box = h('div', { class: 'gatebox card ' + (pa.status === 'approved' ? 'approved' : pa.status === 'disapproved' ? 'disapproved' : ''), id: 'plan-approval' });
      box.append(h('h3', {}, 'The plan as a whole'));
      box.append(h('p', { class: 'small' }, statusText(pa, 'these phases and steps')));
      if (pa.status === 'stale') box.append(diff(pa.snapshot, pa.now));
      box.append(approveRow(pa, 'approval/' + v.id, 'Approve plan'));
      return box;
    }

    function statusText(a, what) {
      if (a.status === 'approved') return `You approved ${what} at ${a.at}.` + (a.note ? ` Note: ${a.note}` : '');
      if (a.status === 'disapproved') return `You disapproved ${what} at ${a.at}.` + (a.note ? ` Note: ${a.note}` : '');
      if (a.status === 'stale') return `You ${a.verdict} ${what} at ${a.at}, but they have changed since. Look at the difference and answer again.`;
      return `Not approved yet.`;
    }

    function approveRow(a, path, label) {
      const key = 'note:' + path;
      const note = draftBox(h('input', { type: 'text', placeholder: 'A note for the agent (say why when you disapprove)' }), key, a.note);
      const verdict = (a.status === 'approved' || a.status === 'disapproved') ? a.status : null;
      const hint = h('div', { class: 'kv' }, verdict ? 'The note saves as you type.' : 'The note is sent with Approve or Disapprove.');
      const send = (v) => { const body = { verdict: v, note: note.value, __draft: note.value }; save(path, body, undefined, key); };
      if (verdict) {
        let t = null;
        note.addEventListener('input', () => { clearTimeout(t); say('typing…'); t = setTimeout(() => {
          save(path, { verdict, note: note.value, __draft: note.value }, 'quiet', key); }, 600); });
      }
      const row = h('div', { class: 'row' },
        h('button', { class: 'approve' + (a.status === 'approved' ? ' on' : ''), onclick: () => send('approved') }, a.status === 'approved' ? 'Approved' : (label || 'Approve')),
        h('button', { class: 'disapprove' + (a.status === 'disapproved' ? ' on' : ''), onclick: () => send('disapproved') }, a.status === 'disapproved' ? 'Disapproved' : 'Disapprove'),
        (a.status !== 'none') ? h('button', { class: 'link', onclick: () => save(path, { verdict: null }, undefined, key) }, 'withdraw') : null);
      return h('div', {}, note, hint, row);
    }

    function decisionCard(dec, ans) {
      const local = dec.id;
      const a = (ans.decisions || {})[local] || { selected: [], text: '' };
      const resolved = (dec.resolved || []).length > 0;
      const recs = Array.isArray(dec.recommend) ? dec.recommend : (dec.recommend ? [dec.recommend] : []);
      const answered = (a.selected || []).length || (a.text || '').trim();
      const card = h('section', { class: 'card' + (resolved ? ' resolved' : answered ? ' is-answered' : ''), id: local });
      card.append(h('h3', {}, resolved ? badge('resolved', 'ok') : answered ? badge('answered', 'ok') : badge('open', 'open'), mdi(dec.question)));
      if (dec.context) card.append(md(dec.context));
      if (dec.as_of) card.append(h('div', { class: 'kv' }, 'as checked on ' + dec.as_of));
      const fs = h('fieldset', { disabled: resolved || null });
      const type = dec.multiple ? 'checkbox' : 'radio';
      const inputs = [];
      for (const o of dec.option || []) {
        const inp = h('input', { type, name: 'd-' + local, value: o.id });
        inp.checked = (resolved ? dec.resolved : (a.selected || [])).includes(o.id);
        inputs.push(inp);
        fs.append(h('label', { class: 'opt' }, inp, h('span', {}, mdi(o.label),
          recs.includes(o.id) ? h('span', { class: 'rec' }, '★ recommended') : null,
          o.hint ? h('span', { class: 'hint' }, mdi(o.hint)) : null)));
      }
      let ta = null;
      if (dec.own_answer !== false) {
        ta = draftBox(h('textarea', { 'aria-label': 'Your own answer or a note', placeholder: 'Or write your own answer, or add a note' }), 'decision:' + local, a.text);
        fs.append(ta);
      }
      const push = (quiet) => save(`decision/${v_id()}/${local}`, { selected: inputs.filter(i => i.checked).map(i => i.value), text: ta ? ta.value : '', __draft: ta ? ta.value : '' }, quiet, 'decision:' + local);
      inputs.forEach(i => i.addEventListener('change', () => push()));
      if (ta) { let t = null; ta.addEventListener('input', () => { clearTimeout(t); say('typing…'); t = setTimeout(() => push('quiet'), 500); }); }
      card.append(fs);
      if (dec.because) card.append(h('p', { class: 'small muted' }, 'Recommended because: ', mdi(dec.because)));
      if (resolved) card.append(h('div', { class: 'gatebox approved' }, h('strong', {}, 'Outcome'), ' ', dec.resolved_on ? h('span', { class: 'muted small' }, dec.resolved_on) : null, md(dec.outcome || '')));
      if (a.at && !resolved) card.append(h('div', { class: 'kv' }, 'your answer, saved ' + a.at + (a.source ? ' (carried over from ' + a.source + ')' : '')));
      card.append(comments(local));
      return card;
    }

    function v_id() { return view.id; }

    // "Only what is open": done and dropped steps, resolved decisions and the
    // long reading (sections, log) fold away. Remembered per browser.
    function focusOn() { try { return localStorage.getItem('planning-focus') === '1'; } catch (e) { return false; } }
    function setFocus(on) {
      try { localStorage.setItem('planning-focus', on ? '1' : '0'); } catch (e) { /* private window */ }
      document.body.classList.toggle('focus', on);
    }

    function stepCard(s, ans) {
      const state = s.state || 'open';
      const tick = (ans.steps || {})[s.id];
      const li = h('li', { class: 'step ' + state, id: s.id });
      const head = h('div', { class: 'head' });
      // Every step gets a real checkbox. The user ticks their own; the agent's
      // are set in the file, so theirs only show the state (active: half-filled).
      const mine = s.by === 'user' && state !== 'dropped';
      const cb = h('input', { type: 'checkbox', class: 'stepbox', 'aria-label': mine ? 'done' : `${state} (set by the agent in the file)`,
                              title: mine ? 'Tick when you have done it' : `${state}: the agent sets this in the file` });
      cb.checked = state === 'done' || (mine && tick && tick.state === 'done');
      cb.indeterminate = state === 'active';
      if (mine) cb.addEventListener('change', () => save(`step/${view.id}/${s.id}`, { state: cb.checked ? 'done' : 'open' }));
      else {
        // Not `disabled`: that greys a done step out. A click just does nothing.
        cb.setAttribute('aria-readonly', 'true');
        cb.addEventListener('click', e => e.preventDefault());
        cb.classList.add('readonly');
      }
      head.append(cb);
      head.append(h('span', { class: 't' }, mdi(s.title)),
        h('span', { class: 'badges' }, badge(s.by, s.by), s.gate ? badge('gate', 'gate') : null,
          state !== 'open' ? badge(state, state === 'done' ? 'ok' : state === 'blocked' ? 'warn' : 'muted') : null,
          h('span', { class: 'mono small muted' }, s.id)));
      li.append(head);
      if (tick && tick.state === 'done' && state !== 'done') li.append(h('div', { class: 'kv' }, 'done in the page; not yet in the file'));
      const meta = [];
      if ((s.needs || []).length) meta.push('needs ' + s.needs.join(', '));
      if (s.commit) meta.push('commit ' + s.commit);
      if (meta.length) li.append(h('div', { class: 'kv' }, meta.join(' · ')));
      if (s.reason) li.append(h('div', { class: 'small' }, h('strong', {}, state + ': '), s.reason));
      if (s.details) li.append(md(s.details, 'small'));
      if (s.done_when) li.append(h('div', { class: 'small muted' }, 'Done when: ', mdi(s.done_when)));
      if (s.gate) {
        const a = (view.approvals || {})[s.id] || { status: 'none' };
        const box = h('div', { class: 'gatebox ' + (a.status === 'approved' ? 'approved' : a.status === 'disapproved' ? 'disapproved' : '') });
        box.append(h('div', { class: 'small' }, h('strong', {}, 'Needs your approval before it starts. '), statusText(a, 'this step')));
        if (a.status === 'stale') box.append(diff(a.snapshot, a.now));
        if (state !== 'done' && state !== 'dropped') box.append(approveRow(a, `approval/${view.id}/${s.id}`));
        li.append(box);
      }
      li.append(comments(s.id));
      return li;
    }

    function diff(before, after) {
      const a = JSON.stringify(before, null, 2).split('\n'), b = JSON.stringify(after, null, 2).split('\n');
      const n = a.length, m = b.length, L = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
      for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
      const box = h('div', { class: 'diff' });
      let i = 0, j = 0;
      while (i < n || j < m) {
        if (i < n && j < m && a[i] === b[j]) { box.append(h('div', {}, '  ' + a[i])); i++; j++; }
        else if (j < m && (i >= n || L[i][j + 1] >= L[i + 1][j])) { box.append(h('div', { class: 'add' }, '+ ' + b[j])); j++; }
        else { box.append(h('div', { class: 'del' }, '- ' + a[i])); i++; }
      }
      return h('details', { open: true }, h('summary', { class: 'small' }, 'What changed since your answer'), box);
    }

    function comments(on, openBox, alwaysOpen) {
      const list = ((view.answers || {}).comments || []).filter(c => (c.on || '') === on);
      const wrap = h('div', { class: 'comments' });
      for (const c of list) {
        wrap.append(h('div', { class: 'comment' + (c.resolved ? ' resolved' : '') },
          h('span', { class: 'mono small muted' }, `${c.id} · ${c.at}`), ' ', c.text,
          c.resolved ? h('div', { class: 'small' }, '✓ handled: ' + (c.resolved.note || '')) : null));
      }
      const ckey = 'comment:' + on;
      const ta = draftBox(h('textarea', { placeholder: on ? 'Comment on ' + on + ': split it, add a step, an option is missing…' : 'A comment on the whole item' }), ckey, '');
      const send = h('button', { onclick: async () => {
        if (!ta.value.trim()) return;
        try { await api('POST', `${base}/comment/${view.id}`, { on, text: ta.value }); ta.value = ''; drafts.delete(ckey); toast('Comment saved'); etag = null; load(true); }
        catch (e) { toast('Not saved: ' + e.message); }
      } }, 'Comment');
      const box = h('div', { hidden: alwaysOpen || drafts.get(ckey) ? null : true }, ta, h('div', { class: 'row' }, send));
      if (!alwaysOpen) wrap.append(h('button', { class: 'link', onclick: () => { box.hidden = !box.hidden; if (!box.hidden) ta.focus(); } }, list.length ? 'reply / comment' : 'comment'));
      wrap.append(box);
      return wrap;
    }

    function renderBar(v, decisions, ans) {
      const s = v.summary;
      const openDec = decisions.filter(x => !(x.resolved || []).length);
      const answeredOpen = openDec.filter(x => { const a = (ans.decisions || {})[x.id]; return a && ((a.selected || []).length || (a.text || '').trim()); });
      const parts = [];
      if (openDec.length) parts.push(`${answeredOpen.length} of ${openDec.length} open decisions answered`);
      if (s.steps.total) parts.push(`${s.steps.done}/${s.steps.total} steps done`);
      if (s.for_user.length) parts.push(`${s.for_user.length} waiting on you`);
      const focusBtn = h('button', { class: 'focus-toggle', 'aria-pressed': String(focusOn()), onclick: () => { setFocus(!focusOn()); focusBtn.setAttribute('aria-pressed', String(focusOn())); focusBtn.textContent = focusOn() ? 'Show everything' : 'Only what is open'; } },
        focusOn() ? 'Show everything' : 'Only what is open');
      const todoBtn = h('button', { onclick: () => document.getElementById('todo').scrollIntoView({ behavior: 'smooth' }) }, 'To-do');
      bar.replaceChildren(h('div', { class: 'in' }, h('span', { class: 'progress-text' }, parts.join(' · ') || 'Nothing open'),
        todoBtn, focusBtn, h('button', { class: 'primary', onclick: copyAnswers }, 'Copy answers')));
      bar.hidden = false;
    }

    async function copyAnswers() {
      const v = view, d = v.data, ans = v.answers || {};
      const decisions = v.kind === 'decision' ? [Object.assign({}, d, { id: v.id })] : (d.decision || []);
      let out = `### Answers to ${d.title} (${v.id}, ${new Date().toISOString().slice(0, 10)})\n\n`;
      for (const dec of decisions) {
        if ((dec.resolved || []).length) continue;
        const a = (ans.decisions || {})[dec.id] || {};
        const labels = (dec.option || []).filter(o => (a.selected || []).includes(o.id)).map(o => o.label);
        const parts = [];
        if (labels.length) parts.push(labels.join('; '));
        if ((a.text || '').trim()) parts.push('own answer: ' + a.text.trim().replace(/\n+/g, ' '));
        out += `- ${dec.id}: ${dec.question} — ${parts.length ? parts.join(' | ') : '(no answer)'}\n`;
      }
      for (const [local, a] of Object.entries(v.approvals || {})) {
        if (a.status !== 'none') out += `- step ${local}: ${a.status}${a.note ? ' — ' + a.note : ''}\n`;
      }
      try { await navigator.clipboard.writeText(out); toast('Copied: paste it to the agent'); }
      catch (e) { const w = window.open('', '_blank'); if (w) w.document.body.innerText = out; else alert(out); }
    }

    await load(true);
    watch(() => load(false), 'repo', slug);
    poll(() => load(false), 15000);
  }

  // ---- routing ------------------------------------------------------------- //

  function route() {
    const p = location.pathname.split('/').filter(Boolean).map(decodeURIComponent);
    if (!p.length) return viewIndex();
    if (p[0] === 'r' && p.length === 2) return viewRepo(p[1]);
    if (p[0] === 'r' && p[2] === 'i' && p[3]) return viewItem(p[1], p[3]);
    if (p[0] === 'r' && p[2] === 'm' && p.length > 3) return viewMarkdown(p[1], p.slice(3).join('/'));
    main.replaceChildren(h('p', {}, 'Nothing here. ', h('a', { href: '/' }, 'All repositories')));
  }
  route();
})();
