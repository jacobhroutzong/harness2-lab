import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createPreviewStartup, forward } from './preview-proxy.mjs';

const origin = 'https://harness2-runtime.internal';

test('concurrent requests share startup and wait for the health listener', async () => {
  let boots = 0;
  let checks = 0;
  const timeouts = [];
  const fetcher = {
    async fetch(url) {
      assert.equal(url, 'http://container/healthz');
      return new Response(null, { status: ++checks === 1 ? 503 : 200 });
    },
  };
  const container = {
    running: false,
    images: { base: 'pinned-image' },
    start(options) {
      boots++;
      assert.deepEqual(options, {
        image: 'pinned-image', instance: 'basic', enableInternet: false,
        env: { H2_ALLOW_SERVER_KEYS: '0' },
      });
      this.running = true;
    },
    async setInactivityTimeout(value) { timeouts.push(value); },
    getTcpPort(port) { assert.equal(port, 7860); return fetcher; },
  };
  const start = createPreviewStartup(container);
  assert.deepEqual(await Promise.all([start(), start()]), [fetcher, fetcher]);
  assert.equal(boots, 1);
  assert.equal(checks, 2);
  assert.equal(await start(), fetcher);
  assert.equal(checks, 2);
  container.running = false;
  assert.equal(await start(), fetcher);
  assert.equal(boots, 2);
  assert.deepEqual(timeouts, [900_000, 900_000]);
});

test('forwards POST input and the reconnect cursor without buffering SSE', async () => {
  const stream = new Response('data: event\n\n').body;
  const response = await forward(new Request(origin + '/api/runs/00000000ab/events', {
    headers: { accept: 'text/event-stream', 'last-event-id': '9', authorization: 'private', cookie: 'private' },
  }), async () => ({
    async fetch(url, options) {
      assert.equal(url, 'http://container/api/runs/00000000ab/events');
      assert.equal(options.headers.get('last-event-id'), '9');
      assert.equal(options.headers.has('authorization'), false);
      assert.equal(options.headers.has('cookie'), false);
      assert.equal(options.redirect, 'manual');
      return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } });
    },
  }));
  assert.equal(response.body, stream);
  assert.equal(await response.text(), 'data: event\n\n');

  const request = new Request(origin + '/api/runs', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"backend":"sim"}',
  });
  const posted = await forward(request, async () => ({
    async fetch(url, options) {
      assert.equal(url, 'http://container/api/runs');
      assert.equal(options.method, 'POST');
      assert.equal(options.body, request.body);
      assert.equal(await new Response(options.body).text(), '{"backend":"sim"}');
      return Response.json({ id: '00000000ab' });
    },
  }));
  assert.equal(posted.status, 200);
});

test('ineligible requests cannot start a container and failures return 503', async () => {
  let unexpectedStarts = 0;
  const forbidden = async () => { unexpectedStarts++; };
  for (const request of [
    new Request('https://public.example/api/meta'),
    new Request(origin + '/healthz'),
    new Request(origin + '/api/meta?probe=1'),
    new Request(origin + '/api/runs'),
  ]) assert.equal((await forward(request, forbidden)).status, 404);
  assert.equal(unexpectedStarts, 0);
  const failure = await forward(new Request(origin + '/api/meta'), async () => { throw new Error('internal'); });
  assert.equal(failure.status, 503);
  assert.equal(failure.headers.get('retry-after'), '10');
});
