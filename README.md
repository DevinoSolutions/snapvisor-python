# Snapvisor Python SDK

The official **Python SDK and CLI** for [Snapvisor](https://snapvisor.io), the visual
regression testing platform. Upload a directory of screenshots as a build from any
Python test suite or CI pipeline — and reach every other operation the platform
publishes — with no Node.js required.

Snapvisor is a fork of [Argos](https://argos-ci.com). This SDK speaks the same upload
protocol as `@snapvisor/core` / `@argos-ci/core`, so it is a drop-in way to send builds
from Python, and it still reads the `ARGOS_*` environment variables the JavaScript SDKs
use so it works unchanged in pipelines already wired for Argos/Snapvisor.

## Install

```bash
pip install snapvisor
```

Requires Python 3.10+. Runtime dependencies: `httpx` and `attrs`.

## Quick start

```python
import snapvisor

result = snapvisor.upload("screenshots/", token="<project-token>")
print(result.build_url)
```

```bash
export SNAPVISOR_TOKEN="<project-token>"   # ARGOS_TOKEN also works
snapvisor upload ./screenshots
# Build created: https://app.snapvisor.io/<account>/<project>/builds/<n>
```

## Two entry points

| You want to…                                        | Use                                    |
| --------------------------------------------------- | -------------------------------------- |
| Upload screenshots as a build                        | `snapvisor.upload()` / `snapvisor upload` |
| Read or write anything else (builds, projects, comments, reviews, changes, deployments, analytics) | `snapvisor.Snapvisor` |

Both share one transport policy: retries with exponential backoff, IETF draft-8
rate-limit handling on 429, request-id correlation, and a typed error hierarchy.

## Uploading builds

```python
from snapvisor import upload, BuildOptions

result = upload(
    "screenshots",
    token="...",  # or SNAPVISOR_TOKEN / ARGOS_TOKEN
    build_name="my-suite",  # optional, for multi-build setups
    branch="main",  # optional; resolved from env, CI, then git
    commit="abc...def",  # optional; resolved from env, CI, then git
    reference_branch="main",  # optional baseline
    threshold=0.1,  # optional default diff threshold
    concurrency=8,  # files uploaded in parallel
    build=BuildOptions(
        pr_number=77,
        mode="ci",
        subset=True,
        metadata={"testReport": {"status": "passed"}},
    ),
)

print(result.build_url, result.build_id, result.build_number)
```

`upload()` returns an `UploadResult`. `aupload()` is the identical async version.

### Screenshot metadata and Playwright traces

Put sidecars next to a screenshot and the SDK picks them up — the same convention the
JavaScript test-runner integrations write:

```
screenshots/
  home.png
  home.png.argos.json      # viewport, url, colorScheme, test info, transient.threshold
  home.png.pw-trace.zip    # uploaded as the screenshot's Playwright trace
```

```json
{
  "url": "https://example.com/",
  "viewport": { "width": 1280, "height": 720 },
  "colorScheme": "dark",
  "test": { "title": "home page", "titlePath": ["suite", "home page"] },
  "transient": { "threshold": 0.25, "baseName": "base.png", "parentName": "parent.png" }
}
```

### CI detection

Running inside GitHub Actions, GitLab CI, CircleCI, Buildkite, Travis, Azure Pipelines,
or Jenkins, the SDK fills in `ciProvider`, `runId`, `runAttempt`, `prNumber`,
`prHeadCommit`, `branch`, and `commit` automatically. Explicit arguments always win; pass
`detect_ci_environment=False` to turn it off.

### Parallel builds

```python
from snapvisor import upload, ParallelConfig, finalize_builds

upload("screenshots", parallel=ParallelConfig(nonce="ci-run-42", total=4, index=2))

# When the shard count is not known up front (total=-1), finalize explicitly:
finalize_builds(parallel_nonce="ci-run-42")
```

`skip_build()` creates a skipped build so required status checks pass without a
comparison, and `find_baseline(commits=[...])` asks the API which commit can serve as a
baseline.

## The full API

```python
from snapvisor import Snapvisor

sv = Snapvisor(token="<personal-access-token>")

me = sv.users.get_me()
project = sv.projects.get_project(owner="acme", project="web")
build = sv.builds.get_build(owner="acme", project="web", build_number="42")

for build in sv.builds.auto_paginate("listBuilds", owner="acme", project="web"):
    print(build.number, build.status)
```

All 37 operations the API publishes are reachable, grouped into namespaces:
`builds`, `comments`, `deployments`, `projects`, `reviews`, `changes`, `analytics`,
`users`, `auth`, `repos`. Both naming conventions resolve — `sv.builds.list_builds(...)`
and `sv.builds.listBuilds(...)` are the same call.

Responses are typed models, not `dict[str, Any]`:

```python
from snapvisor.models import Build, Project
```

### Async

```python
from snapvisor import AsyncSnapvisor

async with AsyncSnapvisor(token="...") as sv:
    me = await sv.users.get_me()
    async for build in sv.builds.auto_paginate("listBuilds", owner="acme", project="web"):
        print(build.number)
```

### Which token do I need?

Only 8 of the 37 operations accept a **project token** (`createBuild`, `updateBuild`,
`finalizeBuilds`, `findBaseline`, `getAuthProject`, the deployment operations, and the
read-only build/project lookups). The other 29 require a **personal access token** (or an
OAuth 2.1 access token). `Snapvisor()` prefers `SNAPVISOR_PAT`, then `SNAPVISOR_TOKEN` /
`ARGOS_TOKEN`; using a project token where a PAT is required raises
`SnapvisorAuthError` rather than a generic failure.

### Static typing per operation

The facade dispatches dynamically, which is what keeps coverage in step with the API.
When you want a type checker to see an individual operation's signature, import its module:

```python
from snapvisor import Snapvisor
from snapvisor.operations import list_builds

sv = Snapvisor()
response = list_builds.sync_detailed(owner="acme", project="web", client=sv.raw)
```

## Pagination

```python
from snapvisor import Snapvisor

sv = Snapvisor()

# Everything, page by page, transparently:
for comment in sv.comments.auto_paginate(
    "listComments", owner="acme", project="web", build_number="42"
):
    print(comment.body)

# Or one page with its metadata:
page = sv.builds.page("listBuilds", owner="acme", project="web", per_page=50)
print(page.total, page.page, page.per_page, page.has_more)
```

`per_page` is clamped to the documented 1–100 range.

## Retries and rate limits

```python
from snapvisor import Snapvisor, RetryConfig, upload

retry = RetryConfig(
    max_retries=3,  # retries after the first attempt
    backoff_factor=0.5,  # exponential, with full jitter
    max_backoff=8.0,
    max_rate_limit_wait=60.0,  # longest sleep honoured for one 429
)

sv = Snapvisor(token="...", retry=retry)
upload("screenshots", retry=retry)
```

- 5xx responses, connect errors, and timeouts are retried; `POST`/`PATCH` are replayed
  only when the connection never got established, so a completed write is never duplicated.
- 4xx responses are never retried.
- On 429 the SDK reads the draft-8 `RateLimit` / `RateLimit-Policy` headers (falling back
  to `Retry-After`), sleeps until the window resets, and resumes. If that wait exceeds
  `max_rate_limit_wait`, it raises `SnapvisorRateLimitError` carrying `limit`,
  `remaining`, and `reset`.
- Every request carries a stable `x-argos-request-id` across retry attempts and an
  incrementing `x-argos-retry-attempt`; the request id is on every exception.

## Errors

| Exception                  | When                                      |
| -------------------------- | ----------------------------------------- |
| `SnapvisorConfigError`     | Missing/invalid token, branch, commit, directory |
| `SnapvisorAuthError`       | 401 — token missing, malformed, or the wrong kind |
| `SnapvisorForbiddenError`  | 403 — missing scope or permission          |
| `SnapvisorNotFoundError`   | 404                                        |
| `SnapvisorConflictError`   | 409                                        |
| `SnapvisorRateLimitError`  | 429 beyond the wait ceiling                |
| `SnapvisorServerError`     | 5xx after retries were exhausted           |
| `SnapvisorUploadError`     | A screenshot or trace was rejected by storage |

All API errors subclass `SnapvisorAPIError` (so 0.1.0-era `except SnapvisorAPIError`
still catches everything) and carry `status_code`, `message`, `details` (the server's
per-field validation messages), and `request_id`.

## CLI

```
snapvisor upload <directory>       Upload a directory of screenshots as one build
snapvisor skip                     Create a skipped build so required checks pass
snapvisor finalize                 Finalize every shard of a parallel build
snapvisor whoami                   Show the authenticated user
snapvisor build   list|get|diffs
snapvisor project list|get|create
snapvisor comment list|create|delete|resolve|unresolve
snapvisor review  list|create|dismiss
snapvisor change  ignore|unignore
snapvisor deployment get|resolve
snapvisor analytics
snapvisor login|logout             Store/remove a personal access token
```

Add `--json` for machine-readable output. Exit codes are differentiated so CI can branch
on the failure kind: `0` success, `1` API error, `2` configuration error, `3` upload
error, `4` rate limited.

Run `snapvisor <command> --help` for the flags of any command.

## Environment variables

Every variable is read as `SNAPVISOR_<NAME>` first and `ARGOS_<NAME>` second, so existing
Argos pipelines keep working unchanged. Explicit function/CLI arguments always take
precedence over the environment.

| Variable (either prefix) | Purpose                                                   |
| ------------------------ | --------------------------------------------------------- |
| `…_TOKEN`                | Project token used to authenticate (`Authorization: Bearer …`). |
| `SNAPVISOR_PAT`          | Personal access token, preferred by `Snapvisor()`.        |
| `…_API_BASE_URL`         | API base URL. Default `https://api.snapvisor.io/v2/`.     |
| `…_BRANCH`               | Git branch. Falls back to CI detection, then git.         |
| `…_COMMIT`               | Git commit SHA. Falls back to CI detection, then git.     |
| `…_BUILD_NAME`           | Build name for multi-build setups.                        |
| `…_PARALLEL`             | `true` to enable parallel mode (with the vars below).     |
| `…_PARALLEL_NONCE`       | Shared nonce across shards. Defaults to the CI run id.    |
| `…_PARALLEL_TOTAL`       | Total number of shards.                                   |
| `…_PARALLEL_INDEX`       | 1-based index of the current shard.                       |
| `SNAPVISOR_CONFIG_DIR`   | Where `snapvisor login` stores its token.                 |

## How it works

`upload()` implements the Snapvisor upload protocol:

1. `POST /v2/builds` with the SHA-256 key and content type of every unique screenshot
   (and of every Playwright trace). The server replies with the build and signed upload
   targets for the files it does not already have, deduplicated by content hash.
2. Each missing file is uploaded to its target — a proxied/presigned `POST` (with policy
   `fields`) or a presigned `PUT` — with a bounded pool of concurrent workers.
3. `PUT /v2/builds/{id}` finalizes the build with the full screenshot list and returns the
   build, including its `url`.

Everything else is generated: `snapvisor._generated` is produced by
[`openapi-python-client`](https://github.com/openapi-generators/openapi-python-client)
from `https://api.snapvisor.io/v2/openapi.yaml`, and a `spec-drift` CI job regenerates
from the live spec on every run and fails on any diff. To regenerate locally:

```bash
pip install -e ".[dev,codegen]"
python scripts/regen.py
```

Never hand-edit `src/snapvisor/_generated/` or `src/snapvisor/_operations.py`.

## Relationship to Argos

Snapvisor is an independent visual-testing platform, forked from the open-source Argos
project, which is MIT licensed. This SDK is original work, wire-compatible with the Argos
upload protocol and deliberately compatible with its `ARGOS_*` environment variables and
`x-argos-*` headers; it is built and maintained for Snapvisor, and is not affiliated with
or endorsed by Argos.

## License

MIT © 2026 Devino Solutions Inc.
