// One live-update stream for every planning tab, run as a SharedWorker.
// A browser opens at most six connections to one host; with a stream per tab,
// the sixth tab left none for anything else, and a click on Approve waited in
// the browser for good. Here every tab of this origin and version shares one
// /api/events?scope=all and hears which repositories changed.
'use strict';

const ports = new Set();
let stream = null;
let last = null;  // the latest hello or open/error state, for a tab that joins late

function send(msg) {
  for (const p of ports) {
    try { p.postMessage(msg); } catch (e) { ports.delete(p); }
  }
}

function open() {
  stream = new EventSource('/api/events?scope=all');
  stream.addEventListener('hello', e => {
    let version = '';
    try { version = JSON.parse(e.data).version; } catch (err) { version = ''; }
    last = { type: 'hello', version };
    send(last);
  });
  stream.addEventListener('change', e => {
    let data = {};
    try { data = JSON.parse(e.data) || {}; } catch (err) { data = {}; }
    send({ type: 'change', index: !!data.index, slugs: data.slugs || [] });
  });
  stream.onopen = () => send({ type: 'open' });
  stream.onerror = () => send({ type: 'error' });
}

self.onconnect = (e) => {
  const port = e.ports[0];
  ports.add(port);
  port.onmessage = (m) => {
    if (m.data === 'bye') {
      ports.delete(port);
      // The last tab went: let the stream go too, rather than hold the
      // connection until the browser gets round to ending this worker.
      if (!ports.size && stream) { stream.close(); stream = null; last = null; }
    }
  };
  port.start();
  if (!stream) open();
  else if (last) port.postMessage(last);
};
