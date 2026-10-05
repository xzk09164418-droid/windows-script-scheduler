// Independent front-end server; only Node built-ins, no browser runtime bundle.
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.CONFIG_EDITOR_FRONTEND_PORT || 5173);
const backend = new URL(process.env.CONFIG_EDITOR_BACKEND || 'http://127.0.0.1:8765');
const token = process.env.CONFIG_EDITOR_TOKEN;
if (!token || backend.hostname !== '127.0.0.1') {
  console.error('请先启动 Python 后端并设置 CONFIG_EDITOR_TOKEN（或使用一键启动器）。');
  process.exit(1);
}
const files = new Map([['/', ['index.html', 'text/html']], ['/app.js', ['app.js', 'text/javascript']], ['/style.css', ['style.css', 'text/css']]]);
const server = http.createServer(async (req, res) => {
  const localHosts = [`127.0.0.1:${port}`, `localhost:${port}`];
  if (!localHosts.includes(req.headers.host) || req.headers.origin && !localHosts.some(host => req.headers.origin === `http://${host}`)) {
    res.writeHead(403); res.end('仅允许本机访问'); return;
  }
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Content-Type-Options', 'nosniff');
  res.setHeader('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'");
  const pathname = new URL(req.url, `http://${req.headers.host}`).pathname;
  if (pathname.startsWith('/api/')) {
    const headers = {'Authorization': `Bearer ${token}`};
    for (const name of ['content-type', 'content-length']) if (req.headers[name]) headers[name] = req.headers[name];
    const proxy = http.request({hostname: backend.hostname, port: backend.port, path: pathname, method: req.method, headers}, upstream => {
      res.writeHead(upstream.statusCode, {'Content-Type': 'application/json; charset=utf-8'});
      upstream.pipe(res);
    });
    proxy.setTimeout(15000, () => proxy.destroy(new Error('timeout')));
    proxy.on('error', () => {
      if (!res.headersSent) res.writeHead(502, {'Content-Type': 'application/json; charset=utf-8'});
      res.end(JSON.stringify({error: 'Python 配置服务未连接，请重新启动编辑器。'}));
    });
    req.pipe(proxy);
    return;
  }
  if (req.method !== 'GET' || !files.has(pathname)) {res.writeHead(404);res.end('Not found');return;}
  try {
    const [file, type] = files.get(pathname);
    const content = await readFile(path.join(root, file));
    res.writeHead(200, {'Content-Type': `${type}; charset=utf-8`});
    res.end(content);
  } catch {res.writeHead(500);res.end('Unable to read frontend files');}
});
server.on('error', error => {console.error(`前端启动失败：${error.message}`);process.exitCode=1;});
server.listen(port, '127.0.0.1', () => console.log(`READY http://127.0.0.1:${port}`));
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => server.close(() => process.exit(0)));
