# Private Harness² simulator preview

This candidate uses the existing Dockerfile. The Worker has no public hostname
or preview alias. A separately approved, isolated Pages preview can reach it
through the `HARNESS2_RUNTIME` binding added in portfolio PR #48.

The configuration selects the Basic container size for qualification. The
gateway routes the existing public application contract to one named Durable
Object so create, status, cancel and reconnect requests share the same process.
It waits for the internal health endpoint, preserves streaming and reconnect
cursors, and idles after 15 minutes. The preview starts without Internet egress
and with server provider keys disabled. This configuration qualifies the
simulator and Budget Lab. It leaves online model execution on the existing
production backend until a separately approved provider preview passes.

Run the local gateway tests with `node --test cloudflare/preview-proxy.test.mjs`.
Wrangler 4.168.0 can validate the configuration and generate binding types.
Actual image creation requires an available Docker engine.

The preview requires owner approval before provisioning. Record worker and
container IDs, image digest, Pages preview commit, cold start, SSE and reconnect
results, CPU and memory, billed duration and idle timing. Establish the required
production reconnect behavior and verify the model-backed workflow before
changing the live Pages binding. A configuration rollback keeps the existing HF
origin available. Pushing to this repository's main branch may mirror to the
production HF Space through the existing workflow, so keep the qualification
branch separate until that production effect is approved.
