# Web Assurance Agent

Web Assurance Agent is a read-only service that turns Uptime Kuma website events into independently verified, evidence-rich incident reports.

Uptime Kuma remains the fast deterministic outage detector. This service receives an authenticated event, confirms failures through tightly controlled HTTPS probes, classifies the result, suppresses duplicate reports, and either publishes a structured Discord embed or logs it in dry-run mode.

> [!IMPORTANT]
> The `0.4.0` development branch provides HTTP incident confirmation and reporting,
> strict declarative page manifests, and an isolated Playwright/Chromium runner with
> deterministic browser evidence. An authenticated manual endpoint can invoke checks for
> manifest-defined pages, including narrow detection of visible WordPress and PHP failure
> signatures, opt-in plugin-specific rendered-state assertions, safe interactions, and
> human-approved visual regression. Authorized, site-specific read-only WordPress administrative
> health collection is now being added; scheduling remains planned in [ROADMAP.md](ROADMAP.md).

## Why this exists

An HTTP monitor can report a failure even when a public site is healthy—for example, when a WAF blocks the monitor. The inverse is also possible: a server may return `200 OK` while the application displays a WordPress critical error or renders incorrectly.

This first release solves the first problem safely:

- independently confirms Kuma failures;
- distinguishes confirmed outages from probable monitor/WAF blocking;
- records useful response evidence;
- classifies Kuma UP events as recoveries and suppresses repeated classifications;
- leaves existing Kuma notification providers untouched.

The roadmap extends this foundation toward proving that a site rendered and behaved as expected.

Built-in application failure detection currently recognizes the canonical WordPress critical-error,
database-connection, and maintenance pages; PHP fatal, parse, and uncaught exception output with
file/line evidence; and a narrow allowlist of visibly unrendered plugin shortcodes. It does not match
the word `error` by itself, and reports stable codes rather than copying raw page text into results.
Shortcode detection is opt-in per page through `application_shortcodes` in the site manifest; list
only shortcode names that are expected to render on that specific page.

Authorized WordPress administrative health checks may also publish a credential-safe Discord alert
through the existing publisher. Alerts contain stable failure codes and aggregate health evidence;
they never include endpoint URLs, secret references, credentials, raw responses, or raw fatal codes.

Authorized WordPress sites may declare a purpose-built read-only health endpoint through
`wordpress_health`. The manifest stores only a `token_secret_ref`, never the credential itself.
Health responses are limited to 64 KiB, validated against a closed typed schema, and reduced to
non-sensitive evidence rather than retained as raw response text.
Secrets are site-scoped: manifest site `example` with reference `site-token` resolves only from
`TRIAGE_SECRET_EXAMPLE_SITE_TOKEN`. Missing or short secrets fail closed before network access.

## Current capabilities

1. Accept authenticated Uptime Kuma webhook events.
2. Validate required Kuma event fields, scalar bounds, and the observation timestamp.
3. Confirm down events with one or more independent HTTPS requests.
4. Restrict all requested hostnames to an exact allowlist.
5. Resolve and inspect every IPv4 and IPv6 destination.
6. Reject private, loopback, local, link-local, reserved, or otherwise non-global addresses.
7. Pin the outbound connection to a validated address while preserving the original HTTP Host header and TLS SNI.
8. Validate each redirect and fail closed on unsafe targets or redirect exhaustion.
9. Capture status, latency, final URL, server header, and Cloudflare Ray ID.
10. Classify incidents as:
    - `confirmed_outage`
    - `monitor_blocked`
    - `transient_failure`
    - `recovered`
11. Serialize events per monitor, reject stale transitions, and suppress repeated classifications.
12. Publish a structured Discord embed or log the payload in dry-run mode.
13. Evaluate typed browser evidence deterministically without requiring a browser runtime.
14. Load strict declarative site/page manifests with exact HTTPS allowlists, stable IDs,
    viewport profiles, assertions, resource policies, masks, and disabled-by-default safe
    interactions.
15. Run isolated Playwright/Chromium checks through validated, address-pinned HTTPS fetching
    with browser DNS disabled, single-use host/SNI-isolated transports, bounded redirects and
    resources, read-only routing, deterministic context settings, blocked WebRTC/WebSocket escape
    paths, fixed typed runtime-error markers, and optional masked viewport screenshot artifacts.
    Cross-origin routed redirects fail closed rather than being fulfilled against the requesting
    origin.
16. Invoke a manifest-defined page manually through `POST /checks/pages/{page_id}` using the
    existing `X-Triage-Token` authentication boundary. Arbitrary request URLs are not accepted.
17. Assert, opt-in per page, that declared plugin components actually rendered — Elementor,
    Contact Form 7, WooCommerce, gallery/slider, search, and multilingual components each
    declare required CSS selectors that must exist, be visible, and have non-zero geometry.
    A failed assertion reports the stable `plugin_assertion_failed` code and the assertion ID,
    never the selector or page content. This is read-only rendered-state inspection; no form
    submission, cart mutation, checkout, or content change occurs.

## Safety model

The service is deliberately read-only. It cannot restart containers, change DNS, modify Cloudflare, purge caches, update WordPress, alter plugins, or remediate a site.

### Webhook boundary

- Requests require `X-Triage-Token`.
- Authentication is checked before the request body is consumed.
- Request bodies are streamed with a 64 KiB limit.
- Malformed JSON and non-object JSON fail closed.
- Kuma-controlled fields use explicit type, length, and timestamp validation.

### Outbound request boundary

- HTTPS only.
- Default HTTPS port only.
- No URL userinfo or embedded credentials.
- Exact hostname allowlist; no wildcard or suffix matching.
- URL policy is checked before DNS lookup.
- Every returned IPv4 and IPv6 address must be globally routable.
- The validated address is the address used for the connection, preventing DNS validation/connection time-of-check-to-time-of-use gaps.
- Redirects are manually followed and revalidated.

### Reporting boundary

- Discord endpoints must be canonical HTTPS Discord webhook URLs.
- Discord delivery errors never include the credential-bearing webhook URL.
- Target URL credentials, query strings, and fragments are removed from report links.
- Failed publication remains retryable; deduplication state is committed only after successful delivery.

### Container boundary

The supplied Compose service:

- runs as UID/GID `10001:10001`;
- uses a read-only root filesystem;
- drops every Linux capability;
- sets `no-new-privileges`;
- provides only a small `/tmp` tmpfs;
- binds to `127.0.0.1:8080` by default;
- runs one worker because incident state is currently process-local.

## Architecture

```text
Uptime Kuma
    |
    | authenticated webhook
    v
FastAPI intake
    |
    +-- strict event parsing
    +-- per-monitor ordering and deduplication
    |
    v
SSRF-safe HTTPS confirmation
    |
    +-- exact allowlist
    +-- public IPv4/IPv6 validation
    +-- DNS-to-connection pinning
    +-- manual redirect validation
    |
    v
Incident classification
    |
    +-- Discord webhook, when configured
    `-- structured dry-run log
```

## Requirements

- Python 3.11 or later
- [uv](https://docs.astral.sh/uv/)
- Docker and Docker Compose for the container workflow

## Local development

```bash
uv sync --python 3.11
uv run playwright install --with-deps chromium
uv run pytest -q
uv run ruff check .
uv run mypy src tests
```

The Playwright browser install step is required once per environment for the isolated
Chromium runner (Milestone 3) and manual browser checks; it is skipped only inside the
Docker image, which installs it during the build instead.

Start the API in dry-run mode:

```bash
export TRIAGE_WEBHOOK_TOKEN='replace-with-a-long-random-token'
export TRIAGE_ALLOWED_HOSTS='example.com,www.example.com'
export TRIAGE_CONFIRMATION_ATTEMPTS=2
export TRIAGE_CONFIRMATION_DELAY_SECONDS=0

uv run uvicorn triage_agent.main:create_runtime_app \
  --factory \
  --host 127.0.0.1 \
  --port 8080
```

Verify the service:

```bash
curl --fail http://127.0.0.1:8080/
curl --fail http://127.0.0.1:8080/healthz
```

## Send a test event

```bash
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  -H 'X-Triage-Token: replace-with-a-long-random-token' \
  -d '{
    "heartbeat": {
      "status": 0,
      "time": "2026-08-04 13:14:37",
      "msg": "Request failed with status code 403"
    },
    "monitor": {
      "id": 10,
      "name": "Example website",
      "type": "http",
      "url": "https://example.com/"
    }
  }' \
  http://127.0.0.1:8080/webhooks/uptime-kuma
```

A valid request returns HTTP `202` with:

```json
{"status":"accepted"}
```

Without `TRIAGE_DISCORD_WEBHOOK_URL`, the rendered Discord payload appears only in service logs.

## Docker Compose

```bash
cp .env.example .env
cp config/sites.example.yaml config/sites.yaml
# Replace every placeholder before continuing.

docker compose config
docker compose build
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8080/healthz
```

A production deployment should place the service behind an approved TLS reverse proxy and restrict ingress to the Uptime Kuma host where practical. Do not use a temporary tunnel as a permanent endpoint.

## Configuration

| Variable | Required | Default | Purpose |
|---|---:|---:|---|
| `TRIAGE_WEBHOOK_TOKEN` | Yes | — | Shared secret sent as `X-Triage-Token`; at least 16 characters |
| `TRIAGE_ALLOWED_HOSTS` | Yes | — | Comma-separated exact public hostnames the service may request |
| `TRIAGE_DISCORD_WEBHOOK_URL` | No | empty | Discord destination; empty enables dry-run logging |
| `TRIAGE_CONFIRMATION_ATTEMPTS` | No | `2` | Confirmation attempts, from 1 to 5 |
| `TRIAGE_CONFIRMATION_DELAY_SECONDS` | No | `5` | Delay between attempts, from 0 to 60 seconds |
| `TRIAGE_REQUEST_TIMEOUT_SECONDS` | No | `15` | Per-request timeout, from 1 to 60 seconds |
| `TRIAGE_SITE_MANIFEST_PATH` | No | empty | Enables manifest-backed manual browser checks when set |
| `TRIAGE_BROWSER_ARTIFACT_DIRECTORY` | No | empty | Directory for optional browser screenshot artifacts |
| `TRIAGE_VISUAL_BASELINE_DIRECTORY` | No | empty | Directory containing separately stored human-approved visual baselines |
| `TRIAGE_MANUAL_CHECK_CONCURRENCY` | No | `1` | Maximum simultaneous manual browser checks per service process, from 1 to 4; excess requests receive HTTP 429 |
| `TRIAGE_STATE_DATABASE_PATH` | No | empty | SQLite file for durable incident/publication state; unset keeps state in memory only (lost on restart, unsafe for multiple workers) |
| `TRIAGE_SCHEDULER_GLOBAL_CONCURRENCY` | No | `2` | Maximum simultaneous scheduled (fast/deep) checks across all sites, from 1 to 8 |
| `TRIAGE_SCHEDULER_SITE_CONCURRENCY` | No | `1` | Maximum simultaneous scheduled checks per site, from 1 to 4 |
| `LOG_LEVEL` | No | `INFO` | Runtime log level |

For Compose deployments, copy `config/sites.example.yaml` to `config/sites.yaml` and replace the
example host, page IDs, assertions, and viewports before starting the service. Compose mounts that
file read-only and stores transient screenshot artifacts under the container's bounded `/tmp`
filesystem.

### Check scheduling

Set `fast_check_interval_seconds` (60–300) and/or `deep_check_interval_seconds` (900–3600) on a
page in the site manifest to put it on a background schedule, in addition to the on-demand
`/checks/pages/{page_id}` endpoint:

- **Fast checks** re-probe the page's origin on the given interval and feed the result through the
  same triage/dedup/Discord pipeline as a real Uptime Kuma webhook. This requires `kuma_monitor_id`
  (the Kuma monitor ID for this page) so the resulting incident dedupes correctly against Kuma's own
  webhook traffic for the same monitor.
- **Deep checks** re-run the full Playwright + WordPress health check on the given interval and
  publish a Discord alert if it fails; they do not require `kuma_monitor_id`.
- Whenever a fast check (or a real Kuma webhook) confirms an outage for a page that also has
  `kuma_monitor_id` set, a deep check for that page runs immediately, outside its normal interval.

Scheduled checks share the same global/per-site concurrency budgets as manual checks
(`TRIAGE_SCHEDULER_GLOBAL_CONCURRENCY` / `TRIAGE_SCHEDULER_SITE_CONCURRENCY`), and each run is
jittered to avoid every page waking up in lockstep.

A page can also declare `maintenance_windows`: recurring weekly UTC windows (day of week, start
time, end time) during which scheduled fast/deep checks still run and record evidence as normal,
but any resulting incident is suppressed rather than published to Discord. This is meant for known
deploy/backup windows so they stop generating alert noise. Manual `/checks/pages/{page_id}` calls
and real Uptime Kuma webhooks are never suppressed, only scheduled checks are — an outage that
persists past the window's end will alert on the next check that runs outside it, and deep checks
have no dedup registry, so it keeps alerting on every deep-check interval for as long as it stays
unhealthy.

### Browser check classification

Both `/checks/pages/{page_id}` and scheduled deep checks return a single `classification` field
that reduces every collected failure code to one overall verdict, most severe first:

| Classification | Meaning |
| --- | --- |
| `render_failure` | The page timed out, returned no document, or the evidence itself was invalid |
| `wordpress_error_page` | A PHP/WordPress fatal error, forbidden error text, or WordPress health check failed |
| `critical_resource_failure` | A resource marked critical in the manifest failed to load |
| `javascript_failure` | A browser console error or uncaught page exception was recorded |
| `functional_regression` | Required text/selectors were missing, or a plugin assertion/interaction failed |
| `visual_regression` | The screenshot exceeded the approved visual diff threshold, or the comparison failed |
| `baseline_pending` | No approved visual baseline exists yet; not treated as a failure |
| `healthy` | No failures and no pending baseline |

When several failure kinds occur in the same check, the table order above is the priority: e.g. a
render failure is reported over a simultaneous JavaScript error. `baseline_pending` never overrides
an actual failure and never triggers a scheduled deep-check Discord alert.

Each classification also carries a static `confidence` (`high`/`medium`/`low`) and `next_action`
(the cheapest next diagnostic step), returned alongside `classification` in the check response.

### Extended check reports

`/checks/pages/{page_id}` and scheduled deep checks also return:

- `site_id` and `kuma_monitor_id` — so a report can be correlated back to the Kuma monitor that
  triggered it;
- `viewports` — one entry per checked viewport with its own `device_profile`, `classification`,
  `failure_codes`, `console_error_count`, `resource_failure_count`, `screenshot` artifact path, and
  `visual_status`. Raw console text, resource URLs, and selector strings are never included — only
  stable codes and counts, consistent with the existing redaction policy;
- `wordpress_health` already reports approved core/plugin/site-health evidence per check.

No diff-image artifact is written to disk today, so no diff path is reported; the current
screenshot path plus `visual_status` (`matched`/`changed`/`unavailable`/`baseline_pending`) is the
available visual evidence.

Kuma incident reports (`render_discord_payload`) include a `Recovery duration` field whenever a
`RECOVERED` event follows a tracked outage; the duration is computed by
`DurableIncidentRegistry.reserve()` from the recorded outage start.

Real `.env` files, pilot configuration, webhook tokens, credentials, and tunnel details must never be committed. The repository includes only `.env.example` placeholders.

## Additive Uptime Kuma integration

The service is intended to be added alongside existing notification providers:

1. Deploy Web Assurance Agent at an approved stable HTTPS endpoint.
2. Generate a unique webhook token.
3. Configure the exact production hostname allowlist.
4. Create a new Kuma Webhook notification targeting:
   `https://<agent-host>/webhooks/uptime-kuma`
5. Add `X-Triage-Token: <generated-token>`.
6. Preserve every existing Discord, Telegram, email, and other notification provider.
7. Attach the new provider to a small pilot monitor set.
8. Trigger a controlled test and review the classification and evidence.
9. Expand gradually after measuring noise and accuracy.

The repository contains no automation that changes Kuma configuration.

## Pilot rollout

`docs/pilot-rollout.md` turns the steps above (plus dry-run review and
manual baseline approval) into a concrete, repeatable runbook against this
repository's actual settings and `scripts/approve_baseline.py`.
`docs/pilot-inventory.md` is the per-site inventory template (critical
pages, plugins, workflows, owner, severity, maintenance window) the runbook
expects to exist before a page goes on a check schedule.

## Verification gates

Before publishing a change:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src tests
TRIAGE_WEBHOOK_TOKEN='configured-test-value' \
TRIAGE_ALLOWED_HOSTS='example.com' \
docker compose config --quiet
docker build -t web-assurance-agent:local .
```

## Current limitations

- Incident state is in memory and resets when the process restarts.
- One worker is required until state becomes durable.
- Confirmation originates from one deployment location.
- The isolated browser runner is not yet wired to an API endpoint or scheduler.
- Screenshot capture requires an explicit artifact directory; production retention policy,
  screenshot comparison, and human-approved baseline management are not yet implemented.
- Plugin-specific rendered-state assertions are implemented; safe synthetic interactions
  (opening menus, expanding accordions, advancing sliders) are not yet implemented.
- No automatic remediation exists or is planned for the initial milestones.

See [ROADMAP.md](ROADMAP.md) for the controlled path to application-level assurance.
