# Web Assurance Agent Roadmap

This roadmap extends the current HTTP incident-confirmation service into deterministic application assurance for WordPress and other browser-rendered websites.

The target is not merely “the server returned HTTP 200.” The target is evidence that the approved page rendered correctly, displayed no application or plugin failures, loaded its critical resources, and completed its approved non-destructive functions.

## Delivery principles

- Use strict test-driven development: RED → GREEN → REFACTOR.
- Keep Uptime Kuma as the fast, deterministic outage detector.
- Add assurance as an independent notification path; never replace working providers.
- Prefer deterministic DOM, browser, network, and functional evidence over AI interpretation.
- Keep all production checks read-only unless a synthetic action is separately approved.
- Require human approval for every visual baseline and baseline replacement.
- Treat security controls as product behavior with adversarial tests.
- Run one complete verification suite after every vertical slice.

## Current foundation — completed

The `0.1.0` foundation provides:

- [x] Authenticated Uptime Kuma webhook intake
- [x] Authentication before body consumption
- [x] Bounded 64 KiB JSON body streaming
- [x] Strict event scalar, length, and timestamp validation
- [x] Independent asynchronous HTTPS confirmation
- [x] Exact hostname allowlisting
- [x] HTTPS/default-port/userinfo enforcement
- [x] Public IPv4 and IPv6 validation
- [x] DNS-result pinning to the actual outbound connection
- [x] Original Host header and TLS SNI preservation
- [x] Manual redirect validation and redirect-limit failure
- [x] Controlled resolver and network-failure evidence
- [x] Outage, monitor-blocking, transient, and recovery classification
- [x] Per-monitor event serialization
- [x] Stale-event rejection
- [x] Publication-aware in-memory deduplication
- [x] Retry-safe Discord publication state
- [x] Canonical Discord webhook validation
- [x] Credential-safe provider errors and report URLs
- [x] Dry-run structured report logging
- [x] Non-root, read-only, capability-dropped container

## Operating boundaries

These constraints apply to every milestone:

- Public, non-destructive browser checks only by default.
- No plugin activation, updates, cache purge, content changes, DNS changes, WAF changes, container actions, or automatic remediation.
- No form submission, purchase, email, login mutation, account change, checkout completion, or externally visible action without an explicitly approved workflow.
- No normal WordPress administrator passwords.
- All initial URLs, redirects, frames, subresources, WebSockets, service workers, and browser navigations must remain inside the destination policy.
- Page scripts must not be able to pivot the browser toward private or unauthorized networks.
- Screenshots must avoid or mask personal and customer-specific content.
- Screenshots and incident artifacts require a documented storage and retention policy before production use.
- Visual baselines are immutable until a human explicitly approves replacement.

## Milestone 1 — Declarative rendered-page checks

### 1.1 Browser evidence domain model

**Status:** complete in `0.2.0`

Create browser-check types that are independent from Playwright so evaluation and reporting remain fast and deterministic in unit tests.

Planned files:

- `src/triage_agent/browser_checks.py`
- `tests/test_browser_checks.py`

Evidence model:

- page identity and requested/final URL;
- viewport and device profile;
- document status and title;
- required text results;
- required selector visibility and geometry;
- forbidden text matches;
- console errors;
- uncaught page exceptions;
- failed requests and significant resource HTTP errors;
- duration and timeout state;
- screenshot artifact metadata, without comparison yet.

TDD order:

1. Healthy evidence evaluates healthy.
2. Missing required selector fails.
3. Zero-sized required geometry fails.
4. Forbidden WordPress/PHP text fails.
5. JavaScript exception fails.
6. Critical resource failure fails.
7. Non-critical ignored resource remains informational.

Additional completed evaluator coverage includes required text, selector visibility,
console errors, unsuccessful document responses, and browser timeouts. The model remains
independent from Playwright; browser execution begins in milestone 1.3.

### 1.2 Declarative site manifests

**Status:** complete in `0.2.0`

Create:

- `src/triage_agent/manifests.py`
- `tests/test_manifests.py`
- `config/sites.example.yaml`

Manifest fields:

- stable site and page identifiers;
- exact HTTPS URL;
- desktop/mobile viewports;
- required text and selectors;
- forbidden error signatures;
- critical and ignored resource patterns;
- dynamic screenshot masks;
- optional safe interactions, disabled by default;
- absent visual baseline reference until approved.

Validation rules:

- exact host must be allowlisted;
- duplicate IDs fail closed;
- unknown fields fail closed;
- arbitrary URLs are never accepted at execution time;
- destructive interaction verbs do not exist in the schema.

### 1.3 Playwright browser runner

**Status:** complete in `0.2.0`

Add Playwright/Chromium through an isolated adapter.

Required behavior:

- fresh browser context per run;
- fixed user agent, viewport, locale, timezone, color scheme, and reduced motion;
- deterministic animation and transition disabling;
- navigation and total-run deadlines;
- deterministic evidence capture;
- no authenticated admin workflows;
- no public website as the automated test oracle.

Browser SSRF controls must cover:

- initial main-frame navigation;
- every redirect;
- iframes;
- scripts, styles, fonts, images, and API calls;
- WebSockets;
- service workers;
- DNS rebinding and destination changes.

A deterministic local fixture server will exercise success, console failure, resource failure, redirect, and layout-collapse cases.

The completed isolated adapter uses deterministic intercepted fixtures rather than public
websites. It disables browser DNS and service workers, blocks WebRTC, WebSockets, and non-GET requests,
pins every HTTP connection to a validated public address while preserving Host and TLS SNI,
uses a single-use transport per pinned request to isolate host/SNI identities, revalidates bounded
redirects, captures typed DOM/runtime/resource evidence without raw runtime error text, and
supports opt-in masked viewport screenshot artifacts. Scheduler integration remains in later
sections.

### 1.4 Safe manual check endpoint

Implemented API:

`POST /checks/pages/{page_id}`

Rules:

- same authenticated boundary as Kuma intake;
- `page_id` resolves only through the validated manifest registry;
- no arbitrary request-controlled URL;
- unknown page returns `404` without browser startup;
- response contains a check ID, classification, concise evidence, and artifact reference.

## Milestone 2 — WordPress and plugin failure detection

### 2.1 Deterministic error signatures

Implemented narrow signatures over rendered visible body text:

- WordPress critical-error page;
- PHP `Fatal error` and `Parse error` output;
- unhandled exception output;
- database connection failure;
- maintenance-mode leakage;
- visible unrendered shortcode patterns.

Broad words such as `error` alone are prohibited because ordinary content would create false positives.
Findings expose stable typed codes rather than raw page text. PHP signatures require both a
specific failure prefix and a PHP file/line location; shortcode detection is restricted to a small
allowlist of known WordPress/plugin shortcode names.

### 2.2 Plugin-specific public assertions

**Status:** complete in `0.2.0`

`PluginAssertionManifest` declares an assertion ID, a bounded supported `kind` (Elementor, Contact
Form 7, WooCommerce, gallery/slider, search, or multilingual components), and one or more required
CSS selectors. The Playwright runner evaluates each declared selector for existence, visibility,
and non-zero rendered geometry; browser evidence records only the assertion ID, kind, and a boolean
result. Failed assertions produce the stable `plugin_assertion_failed` finding, and the manual check
endpoint surfaces the failing assertion IDs directly, without exposing selectors or page content.
Plugin assertion IDs are validated unique per page, supported kinds are schema- and evidence-level
validated, and malformed evidence fails closed. Runner-level tests exercise satisfied and failed
assertions — including missing, hidden, and zero-geometry selectors — against deterministic local
fixtures for every supported kind.

Acceptance criteria:

- Elementor: declared public sections exist, are visible, and have non-zero geometry.
- Contact Form 7: the declared form, fields, and controls render; no submission occurs.
- WooCommerce: declared product, cart, or checkout components render; no purchase or state mutation.
- Gallery/slider: declared image and initialized-state selectors render.
- Search: a known non-mutating result component renders; the agent does not issue arbitrary queries.
- Multilingual plugins: declared language-selector and approved alternate-route components render.
- The manual endpoint returns only concise stable failure codes and assertion IDs.
- Unknown page IDs still return `404` without browser startup, and concurrent manual checks remain
  bounded with HTTP `429` when capacity is exhausted.

“Plugin active” is evidence, not proof of functionality. Behavioral assertions are required.

### 2.3 Safe interactions

**Status:** complete in `0.2.0`

Manifest-declared `click` and `fill` interactions execute through the Playwright runner only when
explicitly enabled per interaction. Each attempt records a stable `InteractionResult` (action,
selector, success); a failed attempt produces the `interaction_failed` finding without exposing the
selector. Open/close navigation, accordion expansion, slider advance, and tab selection are all
expressed as `click`; filling a field and validating client-side required-field behavior with
deliberately incomplete input are both expressed as `fill` (including with an empty value) — no
additional interaction primitives were needed. Manifest validation rejects any action outside the
closed `click`/`fill` set, and rejects a `click` carrying a stray value or a `fill` missing one.

Initial non-destructive interactions:

- open and close navigation;
- expand an accordion;
- advance a slider;
- select tabs;
- fill fields without submitting;
- validate client-side required-field behavior with deliberately incomplete input.

Deferred pending separate approval:

- form submission;
- email delivery;
- login;
- cart mutation;
- checkout progression;
- file upload;
- account or content changes.

## Milestone 3 — Visual regression

### 3.1 Deterministic screenshot normalization

**Status:** complete in `0.3.0`

- wait for declared ready selectors, fonts, and images;
- disable animations, transitions, caret, and smooth scrolling;
- hide or mask configured dynamic regions;
- capture fixed desktop/mobile viewports;
- record page dimensions and browser version.

An optional per-page `ready_selector` gates evidence capture until that element is visible; page
fonts and images are always awaited before capture. Animation/transition/caret suppression, dynamic
region masking, and fixed-viewport capture were already in place from the browser runner. Evidence
now also records the full document's scroll dimensions (`page_width`/`page_height`, distinct from
the fixed viewport) and the Chromium `browser_version` actually used.

### 3.2 Perceptual comparison

**Status:** complete in `0.3.0`

`compare_screenshots` decodes baseline/current PNG bytes with Pillow and computes a normalized
perceptual difference score (mean per-pixel luma delta, 0.0-1.0) and a changed-pixel percentage
(delta above a per-pixel tolerance, ignoring anti-aliasing noise) purely from the diff image's
histogram — no per-pixel Python loop. Changed regions are found by flagging a coarse tile grid and
merging adjacent flagged tiles into bounding boxes; a dimension mismatch between baseline and
current is treated as a maximal, whole-page difference rather than raised as an error. The function
is a pure, standalone comparison: it takes screenshot bytes and path references in, and returns a
`VisualDiffResult` (score, percentage, regions, a highlighted diff PNG, and `exceeds_threshold`
against a caller-supplied per-page/per-viewport threshold) — it does not read or write baselines,
and is not yet wired into `BrowserEvidence` or the runner, since baseline storage and approval
belong to 3.3. `exceeds_threshold` is evidence for the caller to classify, not an automatic outage.

Planned files:

- `src/triage_agent/visual_diff.py`
- `tests/test_visual_diff.py`

Evidence:

- normalized perceptual difference score;
- changed-pixel percentage;
- changed bounding boxes or regions;
- baseline, current, and highlighted diff artifact references.

Policy:

- never rely on exact full-page pixel equality;
- thresholds are versioned per page and viewport;
- visual differences are evidence, not automatically outages;
- deterministic browser failures outrank visual differences;
- AI vision may summarize a detected difference but cannot be the primary pass/fail mechanism.

### 3.3 Human-approved baselines

**Status:** complete in `0.3.0`

`BaselineStore` (`src/triage_agent/baselines.py`) is a filesystem-backed store, independent of
`visual_diff.py` and not yet wired into it or into `BrowserEvidence`. `capture()` writes a screenshot
to `pending/` and records its hash and capture time but never makes it a usable baseline. Only
`approve()` — given an operator label — copies that capture into `approved/` and appends an
immutable record (page ID, viewport, hash, capture time, operator label, approval time) to an
append-only `audit.jsonl`; `current()` reads the latest such record per page/viewport, and
`history()` returns the full trail. Replacing an approved baseline is just another capture+approve
cycle, so every replacement is itself an audit record — nothing is ever overwritten or deleted.
`approved/` and `pending/` are separate subdirectories, distinct from wherever a caller stores
incident/current-run screenshots. Page and viewport identifiers are validated against the same
`[a-z0-9]+(-[a-z0-9]+)*` shape as manifest IDs, closing off path traversal through crafted IDs.

- first capture is `baseline_pending`;
- no automatic baseline acceptance;
- approval records page ID, viewport, hash, capture time, and operator label;
- replacement requires an explicit action and audit record;
- approved baselines and incident artifacts are stored separately.

### 3.4 End-to-end visual evaluation

**Status:** complete in `0.3.0`

Manifest viewports may opt into visual comparison with a bounded
`visual_threshold_percentage`. The runtime decorates the existing read-only Playwright checker with
`VisualPageChecker`, loads only human-approved baselines from the separately mounted baseline store,
and evaluates the current screenshot with the perceptual comparison from 3.2. A missing approved
baseline produces informational `baseline_pending` evidence and never auto-accepts the current
capture. A comparison above threshold produces the stable `visual_regression` failure; matching
captures remain healthy. Browser evidence exposes only status, changed-pixel percentage, and a
bounded changed-region count — no baseline bytes or raw image content.

## Milestone 4 — Read-only WordPress administrative health

**Status:** in progress in `0.4.0`

**Prerequisite:** explicit authorization for each site and access method.

Preferred access order:

1. purpose-built read-only health endpoint with a site-specific credential;
2. least-privilege WordPress Application Password;
3. host-side WP-CLI collector where authorized.

Current implementation (4.1):

- `WordPressHealthManifest` declares an endpoint and a `token_secret_ref` per site;
- the caller must provide the actual token through the runtime's secret loader
  (`WordPressHealthManifest` itself stores no secret);
- `fetch_wordpress_health` uses the existing `validate_probe_url`/`validate_resolved_addresses`
  boundaries, connects to a resolved IP while preserving Host and TLS SNI, rejects redirects,
  and bounds response size to 64 KiB;
- response is parsed into a strict pydantic model with `extra="forbid"` and typed fields;
- the result exposes only typed evidence: core/theme version, pending plugin updates,
  site health status, overdue/failing cron, REST API state, and a small tuple of fatal-error
  codes. No raw response body is retained.

Current implementation (4.2):

- environment secrets are scoped by manifest site and reference as
  `TRIAGE_SECRET_<SITE_ID>_<REFERENCE>`; missing or short values fail closed before network access;
- the authenticated manual page check runs WordPress health collection under the same bounded
  concurrency permit as browser collection;
- WordPress health failures contribute to the top-level `failed` classification and stable failure
  codes, including typed collection errors, core/theme updates, critical Site Health, cron failures,
  REST API failure, and fatal-error evidence;
- API output excludes endpoint URLs, secret references, credentials, and raw response bodies.

Current implementation (4.3):

- typed WordPress health results are normalized through one stable failure-code policy shared by
  the manual API response and alert formatter;
- any failed administrative collection or detected health condition is routed through the existing
  Discord/dry-run publisher without introducing a second webhook credential;
- critical Site Health or fatal-error evidence receives critical alert severity; update, cron,
  REST API, and collection conditions use warning severity;
- alert payloads contain only page/check IDs, bounded aggregate counts, versions, health state, and
  stable failure codes. Endpoint URLs, secret references, credentials, raw bodies, raw fatal codes,
  and plugin inventories are excluded.

Deferred for later sub-milestones:

- external secret-manager adapters and rotation;
- host-side WP-CLI collector and Application Password strategy;

Credentials remain site-specific. Compromise of one monitoring identity must not expose the fleet.

## Milestone 5 — Durable state, scheduling, and reporting

### 5.1 Durable incident state

Replace process-local memory with a transactional store before enabling multiple workers or instances.

Requirements:

- atomic publication reservations;
- delivery retry state;
- stale-event watermarks;
- recovery correlation and duration;
- restart-safe deduplication;
- bounded retention and migration tests.

### 5.2 Check scheduling

- fast checks every 1–5 minutes for critical public pages;
- deep checks every 15–60 minutes;
- immediate incident-triggered checks after Kuma down events;
- jitter, concurrency limits, and per-site budgets.

### 5.3 Expanded classifications

- `render_failure`
- `javascript_failure`
- `critical_resource_failure`
- `wordpress_error_page`
- `functional_regression`
- `visual_regression`
- `baseline_pending`
- `healthy`

Each classification reports deterministic evidence, confidence, and the cheapest next diagnostic action.

### 5.4 Extended reports

Include, where policy permits:

- site/page/viewport;
- console and resource failures;
- missing selectors and error signatures;
- baseline/current/diff artifact references;
- approved WordPress/plugin evidence;
- correlated external monitoring evidence;
- recovery duration.

Existing monitoring notifications remain independent fallback paths.

## Milestone 6 — Controlled pilot and gradual rollout

1. Select a small approved set of public pages.
2. Define desktop and mobile manifests.
3. Run in dry-run mode.
4. Review false positives for several days.
5. Approve visual baselines manually.
6. Configure a dedicated reporting destination.
7. Add the assurance webhook alongside existing providers.
8. Expand in small batches only after noise and accuracy are acceptable.

Fleet rollout requires a per-site inventory of critical pages, plugins, workflows, owner, severity, and maintenance windows.

## Global verification gates

Every vertical slice must pass:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy src tests
TRIAGE_WEBHOOK_TOKEN='configured-test-value' \
TRIAGE_ALLOWED_HOSTS='example.com' \
docker compose config --quiet
docker build -t web-assurance-agent:local .
```

Browser milestones must additionally prove in the built container that:

- Chromium launches;
- a deterministic local fixture passes;
- a console exception fails with captured evidence;
- a missing critical resource fails;
- collapsed required geometry fails;
- desktop and mobile screenshots have stable dimensions;
- scripts cannot request unauthorized or non-public destinations;
- automated tests modify no external system.

## Immediate implementation order

1. Browser evidence model and deterministic evaluator.
2. Validated declarative manifests.
3. Browser network-policy adapter and adversarial SSRF tests.
4. Playwright runner against deterministic local fixtures.
5. Manifest-backed manual check endpoint.
6. WordPress/PHP error signatures.
7. Plugin-specific public assertions and safe interactions.
8. Screenshot normalization and perceptual comparison.
9. Human-approved baseline workflow.
10. Durable state and scheduling.
11. Separately authorized read-only WordPress health collection.

Automatic remediation is outside this roadmap.
