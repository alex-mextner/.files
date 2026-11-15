// Preview-only entry of the HSW editor preview on this PC (kept in ~/.files, not in the hsw-editor product code):
// hsw-editor-preview-refresh copies it into apps/server/src/ of each build of origin/main. It starts the daemon on
// loopback (index.ts registers its own modules: bank, vision, photo-clean) and fronts it with a reverse proxy on the
// tailnet address, a stand-in for `tailscale serve`, which needs operator rights.
//
// The daemon's guard (apps/server/src/guard.ts) admits only its loopback origin, so the proxy rewrites Host and Origin
// to it. That rewrite would also launder a foreign page's request (a cross-site WebSocket, a DNS-rebound Host), so the
// proxy first accepts only this machine's own names on the public port: the tailnet address, the bare host name and
// its MagicDNS name; anything else gets 403.
import { hostname } from 'node:os';
import { loadConfig } from './config.ts';
// The daemon module awaits its own startServer, so the proxy below only starts once loopback serves.
import './index.ts';

// The daemon's own resolution of its port (HSW_EDITOR_PORT, else its default), so the proxy targets what it listens on.
const upstreamPort = loadConfig().port;
const upstreamOrigin = `http://127.0.0.1:${upstreamPort}`;
const publicHost = process.env['HSW_PREVIEW_PUBLIC_HOST'];
const publicPort = Number(process.env['HSW_PREVIEW_PUBLIC_PORT']);

// The node's own MagicDNS name and its short form; the tailnet name can differ from the OS host name (renamed in the
// admin console, a -1 suffix after a collision). The timeout keeps a hung tailscaled from blocking the proxy start,
// and any failure only drops these names: the daemon runs in this process too.
function tailnetNames(): string[] {
  const status = Bun.spawnSync(['tailscale', 'status', '--json'], { stderr: 'ignore', timeout: 5000 });
  if (!status.success) return [];
  let dnsName: unknown;
  try {
    dnsName = JSON.parse(status.stdout.toString())?.Self?.DNSName;
  } catch {
    return [];
  }
  if (typeof dnsName !== 'string' || dnsName === '') return [];
  const full = dnsName.replace(/\.$/, '').toLowerCase();
  return [full, full.split('.')[0] ?? full];
}

// Close codes 1005 and 1006 are reported locally and may not be sent, so they become a normal or an error close.
function sendableCode(code: number): number {
  if (code === 1005) return 1000;
  if (code === 1006) return 1011;
  return code;
}
// A WebSocket client may only send 1000 or an application code (3000-4999) when it closes.
function clientCode(code: number): number {
  return code === 1000 || (code >= 3000 && code <= 4999) ? code : 1000;
}

if (publicHost !== undefined) {
  if (!Number.isInteger(publicPort) || publicPort <= 0) {
    throw new Error(`HSW_PREVIEW_PUBLIC_PORT must be a port number with HSW_PREVIEW_PUBLIC_HOST set, got ${publicPort}`);
  }
  const names = new Set([publicHost.toLowerCase(), hostname().toLowerCase(), ...tailnetNames()]);
  const allowed = (host: string | null): boolean => {
    if (host === null || host === '') return false;
    const url = URL.parse(`http://${host}`);
    return url !== null && url.port === String(publicPort) && names.has(url.hostname.toLowerCase());
  };

  interface Relay {
    upstream?: WebSocket;
    queue: (string | Uint8Array)[];
    url: string;
    protocols: string[];
    headers: Record<string, string>;
  }
  // Handshake headers the upstream WebSocket client generates itself; every other request header (cookies,
  // authorization) is forwarded like a plain HTTP request's.
  const HANDSHAKE: Record<string, true> = {
    host: true,
    origin: true,
    upgrade: true,
    connection: true,
    'content-length': true,
    'transfer-encoding': true,
  };
  // A request from anywhere but this machine's own names (Host, and Origin when the browser sends one) is refused.
  const foreign = (request: Request): boolean => {
    const origin = request.headers.get('origin');
    return !allowed(request.headers.get('host')) || (origin !== null && !allowed(URL.parse(origin)?.host ?? null));
  };
  const upgrade = (request: Request, server: Bun.Server<Relay>, target: string): Response | undefined => {
    const protocols = (request.headers.get('sec-websocket-protocol') ?? '')
      .split(',')
      .map((p) => p.trim())
      .filter(Boolean);
    const forwarded: Record<string, string> = { origin: upstreamOrigin };
    for (const [name, value] of request.headers) {
      if (HANDSHAKE[name] === undefined && !name.startsWith('sec-websocket-')) forwarded[name] = value;
    }
    const data: Relay = { queue: [], url: target.replace('http:', 'ws:'), protocols, headers: forwarded };
    const ok = server.upgrade(
      request,
      protocols[0] ? { data, headers: { 'sec-websocket-protocol': protocols[0] } } : { data },
    );
    return ok ? undefined : new Response('upgrade failed', { status: 400 });
  };
  const forward = async (request: Request, target: string): Promise<Response> => {
    const headers = new Headers(request.headers);
    headers.set('host', `127.0.0.1:${upstreamPort}`);
    if (request.headers.has('origin')) headers.set('origin', upstreamOrigin);
    try {
      return await fetch(target, { method: request.method, headers, body: request.body, redirect: 'manual', signal: request.signal });
    } catch (error) {
      // A client that went away aborts its upstream request; anything else means the daemon is unreachable.
      if (request.signal.aborted) return new Response(null, { status: 499 });
      return new Response(`preview proxy: daemon unreachable (${String(error)})`, { status: 502 });
    }
  };
  Bun.serve<Relay>({
    hostname: publicHost,
    port: publicPort,
    idleTimeout: 120,
    maxRequestBodySize: 128 * 1024 * 1024,
    fetch(request, server) {
      if (foreign(request)) return new Response('forbidden: not this preview host', { status: 403 });
      const url = new URL(request.url);
      const target = `${upstreamOrigin}${url.pathname}${url.search}`;
      if (request.headers.get('upgrade')?.toLowerCase() === 'websocket') return upgrade(request, server, target);
      return forward(request, target);
    },
    websocket: {
      open(ws) {
        const relay = ws.data;
        // Bun's WebSocket client takes headers next to the standard protocols option.
        const upstream = new WebSocket(relay.url, { headers: relay.headers, protocols: relay.protocols });
        relay.upstream = upstream;
        upstream.binaryType = 'arraybuffer';
        upstream.onopen = () => {
          for (const m of relay.queue) upstream.send(m);
          relay.queue.length = 0;
        };
        upstream.onmessage = (event: MessageEvent<string | ArrayBuffer>) => ws.send(event.data);
        upstream.onclose = (event) => ws.close(sendableCode(event.code), event.reason);
      },
      message(ws, message) {
        const relay = ws.data;
        if (relay.upstream?.readyState === WebSocket.OPEN) relay.upstream.send(message);
        else relay.queue.push(message);
      },
      close(ws, code, reason) {
        ws.data.upstream?.close(clientCode(code), reason);
      },
    },
  });
  console.info(`preview proxy listening on http://${publicHost}:${publicPort} for ${[...names].join(', ')}`);
}
