// dc-mcp shim: a transparent HTTP proxy between the Cloudflare tunnel and mcp-auth-proxy. It does two jobs.
//
// A. It adds exactly two things the proxy lacks and ChatGPT's connector setup needs (RFC 9728 protected-resource metadata
//    discovery, as the MCP authorization spec asks):
//    1. every 401 that does not already carry a challenge gets
//         WWW-Authenticate: Bearer resource_metadata="<EXTERNAL_URL>/.well-known/oauth-protected-resource"
//       except under /.auth/ and /.idp/ (the proxy's own login pages answer 401 for a wrong password; a Bearer challenge
//       there would confuse the browser flow);
//    2. /.well-known/oauth-protected-resource/<anything> (the path-suffix form of RFC 9728) is rewritten to the root form the
//       proxy serves.
//    Everything else passes through untouched: method, path, query, headers (minus hop-by-hop ones, RFC 9110 7.6.1), status
//    and body, streamed in both directions (Server-Sent Events of the MCP transport must not be buffered).
//
// B. It writes a small access log, to answer "did ChatGPT's requests reach this computer, and which tool ran" later.
//    One line per request, space separated:
//      <ISO time> <status> <method> <path without query> <latency>ms client=<kind> [rpc=<method>[,..]] [tool=<name>[,..]] [suppressed=<n>]
//    What is NEVER written: query strings (OAuth codes and state live there), request or response bodies, tool arguments,
//    Authorization headers, cookies, passwords, tokens, the full User-Agent (only a short kind: the product token of an
//    openai/chatgpt agent such as openai-mcp, else browser, curl, other). For POST /mcp only the JSON-RPC `method` and, for
//    tools/call, the tool `name` are read from the body, and only when Content-Length is at most PARSE_MAX_BYTES (64 KiB);
//    a bigger or unframed body is logged as rpc=?. A body that does not parse is silently rpc=?; parsing never delays or
//    changes what is forwarded (the body is copied while it streams past and parsed once, at its end).
//    Which requests are logged: every OAuth step (/.idp/*, /.auth/*), every /mcp call that carries an Authorization header, every
//    5xx. Everything else (discovery polling, scanners, unauthenticated calls) is collapsed to one line per method+path+status
//    per minute with suppressed=<n> counting the rest; /favicon.ico is never logged.
//    The file is appended in batches (one asynchronous write per second or per 32 KiB, no fsync), rotated by this process at
//    LOG_MAX_KIB (file -> file.1 -> .. -> file.<LOG_KEEP>, the oldest dropped), mode 0600 in a 0700 directory. A log failure
//    (disk full, EACCES) never touches request handling: lines are dropped and counted, one warning goes to stderr, and a
//    recovery is reported once. Journal output of this process: start, stop, upstream errors (at most one per minute).
//
// Configuration (environment, written by the dc-mcp unit from ~/.config/dc-mcp.env, nothing hardcoded here):
//   EXTERNAL_URL   required: public https URL of the connector, e.g. https://dc-mcp.example.com
//   LISTEN_PORT    loopback port this shim listens on (the tunnel points here)   default 8767
//   UPSTREAM_PORT  loopback port of mcp-auth-proxy                               default 8766
//   UPSTREAM_HOST  host of mcp-auth-proxy                                        default 127.0.0.1
//   LOG_FILE       access log path; empty = no access log                        default empty
//   LOG_MAX_KIB    rotate when the file would pass this size                     default 2048
//   LOG_KEEP       rotated files kept                                            default 3
//   LOG_FLUSH_MS   how often buffered lines are written (the tests set it low)    default 1000
// The size cap is checked once per batch, so a file can pass it by one batch (at most about 32 KiB).
import fs from 'node:fs';
import http from 'node:http';
import path from 'node:path';

const rawExternal = process.env.EXTERNAL_URL || '';
if (!/^https?:\/\/[^/]+/.test(rawExternal)) {
  console.error('dc-mcp shim: EXTERNAL_URL must be the public URL of the connector (https://host)');
  process.exit(2);
}
const EXTERNAL_URL = rawExternal.replace(/\/+$/, '');
const LISTEN_PORT = Number(process.env.LISTEN_PORT || 8767);
const UPSTREAM_HOST = process.env.UPSTREAM_HOST || '127.0.0.1';
const UPSTREAM_PORT = Number(process.env.UPSTREAM_PORT || 8766);
const CHALLENGE = `Bearer resource_metadata="${EXTERNAL_URL}/.well-known/oauth-protected-resource"`;
const PRM = '/.well-known/oauth-protected-resource';
const HOP_BY_HOP = ['connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade'];
const PARSE_MAX_BYTES = 64 * 1024;
const COLLAPSE_MS = 60 * 1000;
const COLLAPSE_KEYS_MAX = 256;
const ERROR_REPORT_MS = 60 * 1000;
const SAFE_TOKEN = /^[A-Za-z0-9_./:-]{1,64}$/;

// A copy of the headers without the hop-by-hop ones and without the headers the Connection header names: they describe one
// TCP hop, and Node adds its own framing (chunked / content-length) for the next one.
function endToEnd(headers) {
  const named = String(headers.connection || '').split(',').map((token) => token.trim().toLowerCase()).filter(Boolean);
  const copy = {};
  for (const [name, value] of Object.entries(headers)) {
    if (!HOP_BY_HOP.includes(name) && !named.includes(name)) copy[name] = value;
  }
  return copy;
}

// ---- access log -------------------------------------------------------------------------------------------------------

class AccessLog {
  constructor(file, maxBytes, keep, flushMs) {
    this.file = file;
    this.maxBytes = maxBytes;
    this.keep = keep;
    this.lines = [];
    this.bytes = 0;
    this.fd = null;
    this.size = 0;
    this.busy = false;
    this.dropped = 0;
    this.warned = false;
    this.current = Promise.resolve(); // the write in progress; stopping waits for it instead of rewriting its batch
    if (file) {
      this.timer = setInterval(() => this.flush(), flushMs);
      this.timer.unref();
    }
  }

  write(line) {
    if (!this.file) return;
    if (this.bytes > 256 * 1024) { this.dropped += 1; return; } // the disk is not keeping up: drop, never grow without bound
    this.lines.push(line);
    this.bytes += line.length;
    if (this.bytes > 32 * 1024) this.flush();
  }

  flush() {
    if (this.busy || this.lines.length === 0) return;
    this.busy = true;
    const batch = this.lines;
    this.lines = [];
    this.bytes = 0;
    this.current = this.append(batch.join(''))
      .then(() => {
        if (this.warned) {
          this.warned = false;
          console.error(`dc-mcp shim: access log is writable again (${this.dropped} lines were dropped meanwhile)`);
          this.dropped = 0;
        }
      })
      .catch((error) => {
        this.dropped += batch.length;
        this.close();
        if (!this.warned) {
          this.warned = true;
          console.error(`dc-mcp shim: cannot write the access log ${this.file} (${error.code || error.message}); lines are dropped until it works again`);
        }
      })
      .finally(() => { this.busy = false; });
  }

  async append(text) {
    const length = Buffer.byteLength(text);
    if (this.fd === null) {
      await fs.promises.mkdir(path.dirname(this.file), { recursive: true, mode: 0o700 });
      this.fd = await fs.promises.open(this.file, 'a', 0o600);
      this.size = (await this.fd.stat()).size;
    }
    if (this.size > 0 && this.size + length > this.maxBytes) await this.rotate();
    await this.fd.write(text);
    this.size += length;
  }

  async rotate() {
    this.close();
    const ignoreMissing = (error) => { if (error.code !== 'ENOENT') throw error; };
    if (this.keep === 0) {
      await fs.promises.unlink(this.file).catch(ignoreMissing);
    } else {
      await fs.promises.unlink(`${this.file}.${this.keep}`).catch(ignoreMissing);
      for (let n = this.keep - 1; n >= 1; n -= 1) {
        await fs.promises.rename(`${this.file}.${n}`, `${this.file}.${n + 1}`).catch(ignoreMissing);
      }
      await fs.promises.rename(this.file, `${this.file}.1`).catch(ignoreMissing);
    }
    this.fd = await fs.promises.open(this.file, 'a', 0o600);
    this.size = 0;
  }

  close() {
    if (this.fd !== null) this.fd.close().catch(() => {});
    this.fd = null;
  }

  async drain() { // on exit: let the write in progress finish (never repeat its batch), then write what is still buffered
    await this.current.catch(() => {});
    this.flushSync();
  }

  flushSync() { // whatever is buffered, without rotation
    if (!this.file || this.lines.length === 0) return;
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true, mode: 0o700 });
      fs.appendFileSync(this.file, this.lines.join(''), { mode: 0o600 });
    } catch { /* nothing left to do */ }
    this.lines = [];
  }
}

const accessLog = new AccessLog(
  process.env.LOG_FILE || '',
  Math.max(1, Number(process.env.LOG_MAX_KIB || 2048)) * 1024,
  Math.max(0, Number.isInteger(Number(process.env.LOG_KEEP)) && process.env.LOG_KEEP !== '' ? Number(process.env.LOG_KEEP) : 3),
  Math.max(10, Number(process.env.LOG_FLUSH_MS || 1000)),
);

// A short, non-identifying kind of client: the product token of an OpenAI agent as it is (openai-mcp), else a family.
function clientKind(userAgent) {
  const ua = String(userAgent || '').trim();
  const product = (ua.split(/[\s/]/)[0] || '').toLowerCase();
  if (/^(openai|chatgpt)[a-z0-9._-]*$/.test(product)) return product.slice(0, 32);
  if (/^curl$/.test(product)) return 'curl';
  if (/mozilla|chrome|safari|firefox|edg/i.test(ua)) return 'browser';
  return 'other';
}

// Only the JSON-RPC method and, for tools/call, the tool name: nothing else of the body is read into the log.
const safeToken = (value) => (typeof value === 'string' && SAFE_TOKEN.test(value) ? value : '?');

function describeItem(item) {
  const method = safeToken(item && typeof item === 'object' ? item.method : undefined);
  const tool = method === 'tools/call' ? safeToken(item.params && item.params.name) : null;
  return { method, tool };
}

function describeRpc(buffer) {
  try {
    const parsed = JSON.parse(buffer.toString('utf8'));
    const items = (Array.isArray(parsed) ? parsed : [parsed]).slice(0, 4).map(describeItem);
    return { rpc: items.map((item) => item.method).join(','), tool: items.filter((item) => item.tool !== null).map((item) => item.tool).join(',') };
  } catch {
    return { rpc: '?', tool: '' };
  }
}

// Paths the connector really uses keep their own collapse key; any other path shares one, so a scanner that invents a new path
// per request cannot push the real records out of the log.
const KNOWN_PATHS = new Set(['/mcp', PRM, '/.well-known/oauth-authorization-server', '/.well-known/openid-configuration']);
const collapseName = (pathname) => {
  if (pathname.startsWith(PRM + '/')) return PRM;
  return KNOWN_PATHS.has(pathname) ? pathname : '(other)';
};
const collapsed = new Map(); // "METHOD path status" -> { last, suppressed }
// Returns the suppressed count to print (0 = none), or -1 when this line is collapsed away.
function collapse(key, now) {
  if (collapsed.size >= COLLAPSE_KEYS_MAX) collapsed.delete(collapsed.keys().next().value); // drop the oldest key only
  const entry = collapsed.get(key);
  if (!entry) { collapsed.set(key, { last: now, suppressed: 0 }); return 0; }
  if (now - entry.last < COLLAPSE_MS) { entry.suppressed += 1; return -1; }
  const suppressed = entry.suppressed;
  entry.last = now;
  entry.suppressed = 0;
  return suppressed;
}

function logRequest({ req, pathname, status, startedAt, rpc }) {
  if (!accessLog.file || pathname === '/favicon.ico') return;
  const oauthStep = pathname.startsWith('/.idp/') || pathname.startsWith('/.auth/');
  const authenticatedCall = pathname === '/mcp' && Boolean(req.headers.authorization);
  let suppressed = 0;
  if (!oauthStep && !authenticatedCall && status < 500) {
    suppressed = collapse(`${req.method} ${collapseName(pathname)} ${status}`, Date.now());
    if (suppressed < 0) return;
  }
  const ms = Math.round(Number(process.hrtime.bigint() - startedAt) / 1e6);
  const safePath = pathname.slice(0, 120).replace(/[^\x21-\x7e]/g, '?');
  let line = `${new Date().toISOString()} ${status} ${req.method} ${safePath} ${ms}ms client=${clientKind(req.headers['user-agent'])}`;
  if (rpc && rpc.rpc) line += ` rpc=${rpc.rpc}`;
  if (rpc && rpc.tool) line += ` tool=${rpc.tool}`;
  if (suppressed > 0) line += ` suppressed=${suppressed}`;
  accessLog.write(`${line}\n`);
}

// ---- proxy ------------------------------------------------------------------------------------------------------------

let lastErrorReport = 0;
let errorsSinceReport = 0;
function reportUpstreamError(code) { // journal: at most one line a minute, however many requests fail
  errorsSinceReport += 1;
  const now = Date.now();
  if (now - lastErrorReport < ERROR_REPORT_MS) return;
  console.error(`dc-mcp shim: upstream ${UPSTREAM_HOST}:${UPSTREAM_PORT} failed (${code}); ${errorsSinceReport} request(s) since the last report`);
  lastErrorReport = now;
  errorsSinceReport = 0;
}

const server = http.createServer((req, res) => {
  const startedAt = process.hrtime.bigint();
  const origUrl = req.url;
  const qi = origUrl.indexOf('?');
  const pathname = qi < 0 ? origUrl : origUrl.slice(0, qi);
  const query = qi < 0 ? '' : origUrl.slice(qi);
  const upstreamPath = pathname.startsWith(PRM + '/') ? PRM + query : origUrl;
  const exempt = pathname.startsWith('/.auth/') || pathname.startsWith('/.idp/');

  // The JSON-RPC method of a small POST /mcp body, copied while the body streams to the upstream.
  const holder = { rpc: null };
  if (accessLog.file && req.method === 'POST' && pathname === '/mcp') {
    const declared = Number(req.headers['content-length']);
    if (req.headers['content-length'] !== undefined && Number.isFinite(declared) && declared <= PARSE_MAX_BYTES) {
      const chunks = [];
      let received = 0;
      req.on('data', (chunk) => { received += chunk.length; if (received <= PARSE_MAX_BYTES) chunks.push(chunk); });
      req.on('end', () => { holder.rpc = received <= PARSE_MAX_BYTES ? describeRpc(Buffer.concat(chunks)) : { rpc: '?', tool: '' }; });
    } else {
      holder.rpc = { rpc: '?', tool: '' };
    }
  }
  res.on('close', () => {
    logRequest({ req, pathname, status: res.headersSent ? res.statusCode : 499, startedAt, rpc: holder.rpc });
    if (!res.writableFinished) up.destroy();
  });

  const up = http.request({
    host: UPSTREAM_HOST, port: UPSTREAM_PORT, method: req.method,
    path: upstreamPath, headers: endToEnd(req.headers),
  }, (ures) => {
    const headers = endToEnd(ures.headers);
    if (ures.statusCode === 401 && !exempt && !headers['www-authenticate']) {
      headers['www-authenticate'] = CHALLENGE;
    }
    res.writeHead(ures.statusCode, ures.statusMessage, headers);
    res.flushHeaders();
    ures.pipe(res);
    ures.on('error', () => res.destroy());
  });
  up.on('error', (e) => {
    reportUpstreamError(e.code || e.message);
    if (!res.headersSent) { res.writeHead(502, { 'content-type': 'text/plain' }); res.end('bad gateway'); }
    else res.destroy();
  });
  req.on('aborted', () => up.destroy());
  req.pipe(up);
});
server.keepAliveTimeout = 65000;
server.listen(LISTEN_PORT, '127.0.0.1', () => console.log(`dc-mcp shim listening 127.0.0.1:${LISTEN_PORT} -> ${UPSTREAM_HOST}:${UPSTREAM_PORT}${accessLog.file ? `, access log ${accessLog.file}` : ''}`));

for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    console.log(`dc-mcp shim stopping (${signal})`);
    const giveUp = setTimeout(() => process.exit(0), 1500); // never hold a stop for a stuck disk
    accessLog.drain().finally(() => { clearTimeout(giveUp); process.exit(0); });
  });
}
