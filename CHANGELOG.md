# Changelog

All notable changes to `snapvisor` are documented here. This project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — unreleased

Full coverage of the Snapvisor v2 API. **No breaking changes**: every 0.1.0 symbol
(`upload`, `UploadResult`, `ParallelConfig`, `SnapvisorError`, `SnapvisorAPIError`) keeps
its name, signature, and behaviour, and a 0.1.0-era script runs against 0.2.0 untouched.

### Added

- **All 37 API operations**, generated from the live OpenAPI document into
  `snapvisor._generated` and exposed through a `Snapvisor` facade with resource
  namespaces: `builds`, `comments`, `deployments`, `projects`, `reviews`, `changes`,
  `analytics`, `users`, `auth`, `repos`. 0.1.0 reached 2 operations, both partially.
- **Typed response models** for every API object (`snapvisor.models`), replacing
  `dict[str, Any]`.
- **Async support**: `AsyncSnapvisor` for the API surface and `aupload()` for the upload
  protocol, sharing the synchronous transport policy.
- **Retries** with exponential backoff and full jitter on 5xx, connect errors, and
  timeouts, configurable via `RetryConfig`. `POST`/`PATCH` are replayed only when the
  connection never got established, so a completed write is never duplicated.
- **Rate-limit handling**: the IETF draft-8 `RateLimit` / `RateLimit-Policy` headers the
  API emits (and `Retry-After`) are honoured — the SDK sleeps until the window resets and
  resumes instead of failing the build. `SnapvisorRateLimitError` carries `limit`,
  `remaining`, and `reset`.
- **Request correlation**: a stable `x-argos-request-id` per logical request plus an
  incrementing `x-argos-retry-attempt`, exposed as `request_id` on every exception.
- **Error taxonomy**: `SnapvisorAuthError` (401), `SnapvisorForbiddenError` (403),
  `SnapvisorNotFoundError` (404), `SnapvisorConflictError` (409),
  `SnapvisorRateLimitError` (429), `SnapvisorServerError` (5xx) — all subclassing
  `SnapvisorAPIError`. The server's `details[].message` validation messages are parsed
  and surfaced instead of dropped.
- **Pagination**: `Page`, `auto_paginate()`, `iter_pages()`, and per-namespace
  `auto_paginate`/`iter_pages`/`page` helpers, with `per_page` clamped to 1–100.
- **Concurrent uploads** (default 8 in flight, `concurrency=1` restores the 0.1.0 serial
  path). A 5000-screenshot build is no longer 5000 sequential round trips.
- **Complete `createBuild` body**: `pwTraceKeys`, `prNumber`, `prHeadCommit`,
  `referenceCommit`, `parentCommits`, `mode`, `ciProvider`, `runId`, `runAttempt`,
  `skipped`, `mergeQueue`, `mergeQueuePrNumbers`, and `subset` via `BuildOptions`.
- **Complete `ScreenshotInput`**: `metadata`, `baseName`, `parentName`, `threshold`, and
  `pwTraceKey` — 0.1.0 hardcoded all five to `None`. Build-level `metadata` is sent too.
- Screenshot metadata is completed with the `sdk` and `automationLibrary` provenance
  blocks the API requires, so a hand-written sidecar is accepted rather than 400ing.
- **Sidecar discovery**: `<screenshot>.argos.json` metadata and
  `<screenshot>.pw-trace.zip` Playwright traces, matching the JavaScript SDK's convention.
  Trace targets returned by `createBuild` are now uploaded; 0.1.0 discarded them.
- **CI environment detection** for GitHub Actions, GitLab CI, CircleCI, Buildkite,
  Travis, Azure Pipelines, and Jenkins.
- **Helpers**: `skip_build()`, `finalize_builds()`, `find_baseline()`.
- **CLI command groups** matching the TypeScript CLI: `build`, `project`, `comment`,
  `review`, `change`, `deployment`, `analytics`, `whoami`, `skip`, `finalize`, `login`,
  `logout`, plus `--json` output and differentiated exit codes (0 ok, 1 API error,
  2 config error, 3 upload error, 4 rate limited).
- **`SNAPVISOR_*` environment variables**, preferred over the `ARGOS_*` names, which keep
  working.
- **`spec-drift` CI job**: regenerates from `https://api.snapvisor.io/v2/openapi.yaml` on
  every run and fails on any diff, so the SDK cannot silently lag the platform.

### Changed

- `snapvisor.upload()` now sends the full documented build body and auto-detects the CI
  environment. The Python signature is unchanged and every new parameter is optional.
- Runtime dependencies are now `httpx` **and** `attrs`.

### Fixed

- The published README no longer says the package is "coming to PyPI" — it has been on
  PyPI since 0.1.0.
- `publish.yml`: the stale "do NOT dispatch this workflow" header is gone; releases are
  now triggered by a `v*` tag, assert that the tag matches `__version__` and that the
  version is not already on PyPI, and require the full CI suite to be green on the exact
  SHA being published.

## [0.1.0] — 2026-07-25

Initial release: `snapvisor.upload()` and `snapvisor upload <dir>`, implementing the
three-step create → upload → finalize protocol, prod-verified against
`devino/snapvisor-js-e2e`.
