# Snapvisor Python SDK

The official **Python SDK and CLI** for [Snapvisor](https://snapvisor.io), the visual
regression testing platform. Upload a directory of screenshots as a build from any
Python test suite or CI pipeline — no Node.js required.

Snapvisor is a fork of [Argos](https://argos-ci.com). This SDK speaks the same upload
protocol as `@snapvisor/core` / `@argos-ci/core`, so it is a drop-in way to send builds
from Python. It deliberately reads the same `ARGOS_*` environment variables the
JavaScript SDKs use, so it works unchanged in pipelines already wired for Argos/Snapvisor.

## Install

Coming to PyPI as `snapvisor`. Until then, install straight from GitHub:

```bash
pip install git+https://github.com/DevinoSolutions/snapvisor-python
# once published:
# pip install snapvisor
```

Requires Python 3.10+.

## CLI usage

```bash
export ARGOS_TOKEN="<your-project-token>"
snapvisor upload ./screenshots
# Build created: https://app.snapvisor.io/<account>/<project>/builds/<n>
```

Options:

```
snapvisor upload <directory>
  --token TOKEN              Project token (default: $ARGOS_TOKEN)
  --build-name NAME          Build name for multi-build setups (default: $ARGOS_BUILD_NAME)
  --branch BRANCH            Git branch (default: $ARGOS_BRANCH, then `git branch --show-current`)
  --commit SHA               Git commit (default: $ARGOS_COMMIT, then `git rev-parse HEAD`)
  --reference-branch BRANCH  Baseline branch for comparison
  --api-base-url URL         API base URL (default: $ARGOS_API_BASE_URL, then the public API)
  --parallel-nonce NONCE     Shared nonce for parallel shards
  --parallel-total N         Total number of parallel shards (-1 = finalize manually)
  --parallel-index I         1-based index of this shard
```

## Python API

```python
from snapvisor import upload

result = upload(
    "screenshots",
    token="...",  # or set ARGOS_TOKEN
    build_name="my-suite",  # optional
    branch="main",  # optional; resolved from env/git otherwise
    commit="abc...def",  # optional; resolved from env/git otherwise
    reference_branch="main",  # optional baseline
)

print(result.build_url)  # https://app.snapvisor.io/.../builds/123
print(result.build_id)  # "123"
print(result.build_number)  # 123
```

`upload()` returns an `UploadResult` with `build_url`, `build_id`, and `build_number`.
Every failure raises loudly: `SnapvisorConfigError` (bad/missing token, branch, commit, or
directory), `SnapvisorAPIError` (non-2xx API response, carrying the status and server
message), or `SnapvisorUploadError` (a screenshot upload was rejected).

### Parallel builds

```python
from snapvisor import upload, ParallelConfig

upload(
    "screenshots",
    parallel=ParallelConfig(nonce="ci-run-42", total=4, index=2),
)
```

## Environment variables

The SDK reads the same variable names as the Argos/Snapvisor JavaScript SDKs, so it drops
into existing pipelines. Explicit function/CLI arguments always take precedence over the
environment.

| Variable               | Purpose                                                         |
| ---------------------- | --------------------------------------------------------------- |
| `ARGOS_TOKEN`          | Project token used to authenticate (`Authorization: Bearer …`). |
| `ARGOS_API_BASE_URL`   | API base URL. Default `https://api.snapvisor.io/v2/`.           |
| `ARGOS_BRANCH`         | Git branch. Falls back to `git branch --show-current`.          |
| `ARGOS_COMMIT`         | Git commit SHA. Falls back to `git rev-parse HEAD`.             |
| `ARGOS_BUILD_NAME`     | Build name for multi-build setups.                              |
| `ARGOS_PARALLEL`       | `true` to enable parallel mode (with the vars below).           |
| `ARGOS_PARALLEL_NONCE` | Shared nonce across parallel shards.                            |
| `ARGOS_PARALLEL_TOTAL` | Total number of shards.                                         |
| `ARGOS_PARALLEL_INDEX` | 1-based index of the current shard.                             |

> The `ARGOS_*` names are intentional for drop-in compatibility with the existing Argos
> ecosystem. There are no `SNAPVISOR_*` aliases yet.

## How it works

`upload()` implements the Snapvisor upload protocol:

1. `POST /v2/builds` with the SHA-256 key and content type of every unique screenshot.
   The server replies with the build and signed upload targets for the screenshots it does
   not already have (deduplicated by content hash).
2. Each missing screenshot is uploaded to its target — a proxied/presigned `POST` (with
   policy `fields`) or a presigned `PUT`.
3. `PUT /v2/builds/{id}` finalizes the build with the full screenshot list and returns the
   build, including its `url`.

## Relationship to Argos

Snapvisor is an independent visual-testing platform, forked from the open-source Argos
project. This SDK is wire-compatible with the Argos upload protocol but is built and
maintained for Snapvisor. It is not affiliated with or endorsed by Argos.

## License

MIT © 2026 Devino Solutions Inc.
