// Validate the ws + https-proxy-agent transport used by Titan. No credentials in output.
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const WebSocket = require('ws');
const { HttpsProxyAgent } = require('https-proxy-agent');
const lab = path.join(__dirname, 'capacity_results', 'architecture');
const fixture = JSON.parse(fs.readFileSync(path.join(lab, 'fixture.json')));
const pool = JSON.parse(fs.readFileSync(process.argv[2]));
const ca = fs.readFileSync(path.join(lab, 'fixture.crt'));
// URL normalizes equivalent IPv6 spellings without adding another dependency.
const normalize = ip => net.isIP(ip) === 6 ? new URL(`http://[${ip}]`).hostname : '';
function check(row) {
  return new Promise(resolve => {
    const proxy = `http://${encodeURIComponent(row.username)}:${encodeURIComponent(row.password)}@${row.host}:${row.port}`;
    const socket = new WebSocket(`wss://[${fixture.ipv6}]:18443/ws`, {
      agent: new HttpsProxyAgent(proxy), ca, perMessageDeflate: true,
      headers: {'X-Lab-Token': fixture.token}, handshakeTimeout: 15000,
    });
    let count = 0, finished = false;
    const timer = setTimeout(() => finish(false), 20000);
    function finish(ok) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      socket.terminate();
      resolve(ok);
    }
    socket.on('message', raw => {
      try {
        const data = JSON.parse(raw);
        if (normalize(data.exit) !== normalize(row.ipv6)) return finish(false);
        if (++count >= 4) finish(true);
      } catch { finish(false); }
    });
    socket.on('error', () => finish(false));
    socket.on('close', () => { if (!finished) finish(false); });
  });
}
(async () => {
  const selected = Array.from({length: 20}, (_, i) => pool[Math.floor(i * (pool.length - 1) / 19)]);
  const results = await Promise.all(selected.map(check));
  const passed = results.filter(Boolean).length;
  console.log(JSON.stringify({node_ws: {passed, failed: results.length - passed}, logical_proxies: pool.length}));
  process.exitCode = passed === results.length ? 0 : 1;
})().catch(() => { console.log(JSON.stringify({node_ws: {fatal: true}})); process.exitCode = 1; });
