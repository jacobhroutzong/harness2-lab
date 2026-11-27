const PATHS = {
  GET: /^\/api\/(meta|budget-lab|runs\/[a-f0-9]{10}(\/events)?)$/,
  POST: /^\/api\/(runs|budget-lab|runs\/[a-f0-9]{10}\/cancel)$/,
};

export function isHarnessRequest(request) {
  const url = new URL(request.url);
  return url.origin === 'https://harness2-runtime.internal'
    && !url.search
    && Object.hasOwn(PATHS, request.method)
    && PATHS[request.method].test(url.pathname);
}

export function unavailable() {
  return Response.json({ detail: 'Harness preview temporarily unavailable' }, {
    status: 503,
    headers: { 'Cache-Control': 'no-store', 'Retry-After': '10' },
  });
}

export async function forward(request, port) {
  if (!isHarnessRequest(request)) return new Response(null, { status: 404 });
  const headers = new Headers();
  for (const name of ['accept', 'content-type', 'last-event-id']) {
    if (request.headers.has(name)) headers.set(name, request.headers.get(name));
  }
  try {
    const fetcher = await port();
    return await fetcher.fetch('http://container' + new URL(request.url).pathname, {
      method: request.method, headers, body: request.body, redirect: 'manual',
    });
  } catch {
    return unavailable();
  }
}

export function createPreviewStartup(container) {
  const idleMilliseconds = 15 * 60_000;
  let pending;
  let ready = false;

  return async function port() {
    if (!container) throw new Error('Container unavailable');
    if (ready && container.running) return container.getTcpPort(7860);
    if (pending) return pending;
    ready = false;
    pending = (async () => {
      if (!container.running) {
        container.start({
          image: container.images.base, instance: 'basic', enableInternet: false,
          env: { H2_ALLOW_SERVER_KEYS: '0' },
        });
      }
      await container.setInactivityTimeout(idleMilliseconds);
      const fetcher = container.getTcpPort(7860);
      const deadline = Date.now() + 15_000;
      while (Date.now() < deadline) {
        try {
          const response = await fetcher.fetch('http://container/healthz', { signal: AbortSignal.timeout(1000) });
          await response.body?.cancel();
          if (response.ok) {
            ready = true;
            return fetcher;
          }
        } catch {
          // The TCP listener may take a moment to accept connections after startup.
        }
        await new Promise(resolve => setTimeout(resolve, 250));
      }
      throw new Error('Container is starting');
    })();
    try {
      return await pending;
    } finally {
      pending = undefined;
    }
  };
}
