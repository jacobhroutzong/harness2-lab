import { DurableObject } from 'cloudflare:workers';
import { createPreviewStartup, forward, isHarnessRequest, unavailable } from './preview-proxy.mjs';

export class HarnessPreview extends DurableObject {
  startup;

  constructor(ctx, env) {
    super(ctx, env);
    this.startup = createPreviewStartup(ctx.container);
  }

  fetch(request) {
    return forward(request, this.startup);
  }
}

export default {
  async fetch(request, env) {
    if (!isHarnessRequest(request)) return new Response(null, { status: 404 });
    try {
      return await env.HARNESS_PREVIEW.getByName('preview').fetch(request);
    } catch {
      return unavailable();
    }
  },
};
