// Deployed entry point. Serves the dashboard page from the Worker bundle (an imported
// Text module) instead of Workers Assets, then hands everything else to worker.js.
// worker.js stays importable by Node tests, which cannot import .html files.
import page from './public/index.html';
import worker from './worker.js';

export function servePage(request) {
  const { pathname } = new URL(request.url);
  if (!['GET', 'HEAD'].includes(request.method) || !['/', '/index.html'].includes(pathname)) return null;
  return new Response(request.method === 'HEAD' ? null : page, {
    headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' },
  });
}

export default {
  scheduled: (event, env, ctx) => worker.scheduled(event, env, ctx),
  fetch: (request, env, ctx) => servePage(request) || worker.fetch(request, env, ctx),
};
