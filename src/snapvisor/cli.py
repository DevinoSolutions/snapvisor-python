"""``snapvisor`` command-line interface (argparse-based, no heavy dependencies).

0.1.0 shipped one command. 0.2.0 covers the platform: the upload protocol plus
read and write access to builds, projects, comments, reviews, changes,
deployments, and analytics — the same surface the TypeScript CLI exposes.

Exit codes are differentiated so CI can branch on the failure kind:

===== =========================================================
Code  Meaning
===== =========================================================
``0`` Success.
``1`` API error — the request reached Snapvisor and was refused.
``2`` Configuration error — missing token, branch, directory, …
``3`` Upload error — a screenshot or trace could not be stored.
``4`` Rate limited — the wait exceeded the SDK's ceiling.
===== =========================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from snapvisor import __version__, credentials
from snapvisor.api import Snapvisor
from snapvisor.config import env
from snapvisor.errors import (
    SnapvisorAPIError,
    SnapvisorConfigError,
    SnapvisorError,
    SnapvisorRateLimitError,
    SnapvisorUploadError,
)
from snapvisor.operations import body_model
from snapvisor.upload import (
    DEFAULT_CONCURRENCY,
    BuildOptions,
    ParallelConfig,
    finalize_builds,
    skip_build,
    upload,
)

EXIT_OK = 0
EXIT_API_ERROR = 1
EXIT_CONFIG_ERROR = 2
EXIT_UPLOAD_ERROR = 3
EXIT_RATE_LIMITED = 4


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snapvisor",
        description="Upload screenshots to, and query, the Snapvisor visual regression platform.",
    )
    parser.add_argument("--version", action="version", version=f"snapvisor {__version__}")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON instead of a human-readable summary.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    _add_upload(subparsers)
    _add_skip(subparsers)
    _add_finalize(subparsers)
    _add_whoami(subparsers)
    _add_build(subparsers)
    _add_project(subparsers)
    _add_comment(subparsers)
    _add_review(subparsers)
    _add_change(subparsers)
    _add_deployment(subparsers)
    _add_analytics(subparsers)
    _add_auth(subparsers)
    return parser


def _add_token_flags(parser: argparse.ArgumentParser, *, pat: bool = True) -> None:
    kind = "personal access token" if pat else "project token"
    env_name = "SNAPVISOR_PAT" if pat else "SNAPVISOR_TOKEN"
    parser.add_argument("--token", help=f"Snapvisor {kind} (defaults to {env_name}).")
    parser.add_argument(
        "--api-base-url",
        help="API base URL (defaults to SNAPVISOR_API_BASE_URL, then the public API).",
    )


def _add_project_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--owner", required=True, help="Account slug that owns the project.")
    parser.add_argument("--project", required=True, help="Project name.")


def _add_page_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--page", type=int, default=1, help="1-based page number.")
    parser.add_argument("--per-page", type=int, default=30, help="Items per page (1-100).")
    parser.add_argument(
        "--all",
        action="store_true",
        dest="all_pages",
        help="Walk every page instead of returning one.",
    )


def _add_upload(subparsers: Any) -> None:
    parser = subparsers.add_parser("upload", help="Upload a directory of screenshots as one build.")
    parser.add_argument("directory", help="Directory of screenshots to upload.")
    _add_token_flags(parser, pat=False)
    parser.add_argument("--build-name", help="Build name for multi-build setups.")
    parser.add_argument("--branch", help="Git branch (defaults to CI, then the current branch).")
    parser.add_argument("--commit", help="Git commit SHA (defaults to CI, then git HEAD).")
    parser.add_argument("--reference-branch", help="Branch to use as the comparison baseline.")
    parser.add_argument("--reference-commit", help="Commit to use as the comparison baseline.")
    parser.add_argument("--parallel-nonce", help="Nonce shared across parallel shards.")
    parser.add_argument(
        "--parallel-total", type=int, help="Total shards (-1 to finalize manually)."
    )
    parser.add_argument("--parallel-index", type=int, help="1-based index of this shard.")
    parser.add_argument("--pr-number", type=int, help="Pull request number this build belongs to.")
    parser.add_argument("--pr-head-commit", help="Head commit of that pull request.")
    parser.add_argument(
        "--parent-commit",
        action="append",
        dest="parent_commits",
        default=None,
        help="Candidate parent commit; repeatable.",
    )
    parser.add_argument("--mode", choices=["ci", "monitoring"], help="Build mode.")
    parser.add_argument("--ci-provider", help="Override the detected CI provider.")
    parser.add_argument("--run-id", help="Override the detected CI run id.")
    parser.add_argument("--run-attempt", type=int, help="Override the detected CI run attempt.")
    parser.add_argument("--merge-queue", action="store_true", help="Mark as a merge-queue build.")
    parser.add_argument(
        "--subset",
        action="store_true",
        help="Mark the build as carrying only a subset of screenshots.",
    )
    parser.add_argument(
        "--threshold", type=float, help="Default per-screenshot diff threshold (0-1)."
    )
    parser.add_argument("--metadata", help="Build-level metadata as a JSON object or @file.json.")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Concurrent file uploads (default {DEFAULT_CONCURRENCY}; 1 = serial).",
    )
    parser.add_argument(
        "--no-ci-detect", action="store_true", help="Do not auto-detect the CI environment."
    )
    parser.set_defaults(func=_cmd_upload)


def _add_skip(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "skip", help="Create a skipped build so required checks pass without a comparison."
    )
    _add_token_flags(parser, pat=False)
    parser.add_argument("--build-name", help="Build name for multi-build setups.")
    parser.add_argument("--branch", help="Git branch.")
    parser.add_argument("--commit", help="Git commit SHA.")
    parser.set_defaults(func=_cmd_skip)


def _add_finalize(subparsers: Any) -> None:
    parser = subparsers.add_parser("finalize", help="Finalize every shard of a parallel build.")
    _add_token_flags(parser, pat=False)
    parser.add_argument("--parallel-nonce", required=True, help="The shared parallel nonce.")
    parser.set_defaults(func=_cmd_finalize)


def _add_whoami(subparsers: Any) -> None:
    parser = subparsers.add_parser("whoami", help="Show the authenticated user and their accounts.")
    _add_token_flags(parser)
    parser.set_defaults(func=_cmd_whoami)


def _add_build(subparsers: Any) -> None:
    parser = subparsers.add_parser("build", help="Inspect builds.")
    actions = parser.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="List a project's builds.")
    _add_token_flags(listing)
    _add_project_flags(listing)
    _add_page_flags(listing)
    listing.add_argument("--head", help="Filter by head branch.")
    listing.set_defaults(func=_cmd_build_list)

    get = actions.add_parser("get", help="Show one build.")
    _add_token_flags(get)
    _add_project_flags(get)
    get.add_argument("--number", required=True, help="Build number.")
    get.set_defaults(func=_cmd_build_get)

    diffs = actions.add_parser("diffs", help="List a build's screenshot diffs.")
    _add_token_flags(diffs)
    _add_project_flags(diffs)
    diffs.add_argument("--number", required=True, help="Build number.")
    _add_page_flags(diffs)
    diffs.set_defaults(func=_cmd_build_diffs)


def _add_project(subparsers: Any) -> None:
    parser = subparsers.add_parser("project", help="Inspect and create projects.")
    actions = parser.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="List an account's projects.")
    _add_token_flags(listing)
    listing.add_argument("--account", required=True, help="Account slug.")
    _add_page_flags(listing)
    listing.set_defaults(func=_cmd_project_list)

    get = actions.add_parser("get", help="Show one project.")
    _add_token_flags(get)
    _add_project_flags(get)
    get.set_defaults(func=_cmd_project_get)

    create = actions.add_parser("create", help="Create a project.")
    _add_token_flags(create)
    create.add_argument("--account", required=True, help="Account slug that will own it.")
    create.add_argument("--name", required=True, help="Project name.")
    create.set_defaults(func=_cmd_project_create)


def _add_comment(subparsers: Any) -> None:
    parser = subparsers.add_parser("comment", help="Read and write build comments.")
    actions = parser.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="List a build's comments.")
    _add_token_flags(listing)
    _add_project_flags(listing)
    listing.add_argument("--number", required=True, help="Build number.")
    _add_page_flags(listing)
    listing.set_defaults(func=_cmd_comment_list)

    create = actions.add_parser("create", help="Post a comment on a build.")
    _add_token_flags(create)
    _add_project_flags(create)
    create.add_argument("--number", required=True, help="Build number.")
    create.add_argument("--body", required=True, help="Comment text.")
    create.set_defaults(func=_cmd_comment_create)

    delete = actions.add_parser("delete", help="Delete a comment.")
    _add_token_flags(delete)
    _add_project_flags(delete)
    delete.add_argument("--number", required=True, help="Build number.")
    delete.add_argument("--comment-id", required=True, help="Comment id.")
    delete.set_defaults(func=_cmd_comment_delete)

    for name, operation in (
        ("resolve", "resolveCommentThread"),
        ("unresolve", "unresolveCommentThread"),
    ):
        action = actions.add_parser(name, help=f"{name.capitalize()} a comment thread.")
        _add_token_flags(action)
        _add_project_flags(action)
        action.add_argument("--number", required=True, help="Build number.")
        action.add_argument("--comment-id", required=True, help="Comment id.")
        action.set_defaults(func=_cmd_comment_thread, operation=operation)


def _add_review(subparsers: Any) -> None:
    parser = subparsers.add_parser("review", help="Read and write build reviews.")
    actions = parser.add_subparsers(dest="action", required=True)

    listing = actions.add_parser("list", help="List a build's reviews.")
    _add_token_flags(listing)
    _add_project_flags(listing)
    listing.add_argument("--number", required=True, help="Build number.")
    _add_page_flags(listing)
    listing.set_defaults(func=_cmd_review_list)

    create = actions.add_parser("create", help="Approve or reject a build.")
    _add_token_flags(create)
    _add_project_flags(create)
    create.add_argument("--number", required=True, help="Build number.")
    create.add_argument(
        "--state", required=True, choices=["approved", "rejected"], help="Review state."
    )
    create.set_defaults(func=_cmd_review_create)

    dismiss = actions.add_parser("dismiss", help="Dismiss a review.")
    _add_token_flags(dismiss)
    _add_project_flags(dismiss)
    dismiss.add_argument("--number", required=True, help="Build number.")
    dismiss.add_argument("--review-id", required=True, help="Review id.")
    dismiss.set_defaults(func=_cmd_review_dismiss)


def _add_change(subparsers: Any) -> None:
    parser = subparsers.add_parser("change", help="Ignore or unignore a detected change.")
    actions = parser.add_subparsers(dest="action", required=True)
    for name, operation in (("ignore", "ignoreChange"), ("unignore", "unignoreChange")):
        action = actions.add_parser(name, help=f"{name.capitalize()} a change.")
        _add_token_flags(action)
        _add_project_flags(action)
        action.add_argument("--change-id", required=True, help="Change id.")
        action.set_defaults(func=_cmd_change, operation=operation)


def _add_deployment(subparsers: Any) -> None:
    parser = subparsers.add_parser("deployment", help="Inspect deployments.")
    actions = parser.add_subparsers(dest="action", required=True)

    get = actions.add_parser("get", help="Show one deployment.")
    _add_token_flags(get, pat=False)
    get.add_argument("--deployment-id", required=True, help="Deployment id.")
    get.set_defaults(func=_cmd_deployment_get)

    resolve = actions.add_parser("resolve", help="Resolve a deployment domain (no auth required).")
    _add_token_flags(resolve, pat=False)
    resolve.add_argument("--domain", required=True, help="Deployment domain.")
    resolve.set_defaults(func=_cmd_deployment_resolve)


def _add_analytics(subparsers: Any) -> None:
    parser = subparsers.add_parser("analytics", help="Fetch account analytics.")
    _add_token_flags(parser)
    parser.add_argument("--account", required=True, help="Account slug.")
    parser.add_argument("--from", dest="from_date", required=True, help="Start date (ISO 8601).")
    parser.add_argument("--to", dest="to_date", help="End date (ISO 8601).")
    parser.add_argument("--group-by", required=True, help="Grouping, e.g. day or week.")
    parser.set_defaults(func=_cmd_analytics)


def _add_auth(subparsers: Any) -> None:
    login = subparsers.add_parser("login", help="Store a personal access token for later commands.")
    login.add_argument(
        "--token",
        help="The token to store. Omit to read it from stdin (safer in shell history).",
    )
    login.add_argument("--api-base-url", help="API base URL this token belongs to.")
    login.set_defaults(func=_cmd_login)

    logout = subparsers.add_parser("logout", help="Delete the stored token.")
    logout.set_defaults(func=_cmd_logout)


def _client(args: argparse.Namespace) -> Snapvisor:
    token = getattr(args, "token", None)
    api_base_url = getattr(args, "api_base_url", None)
    if not token:
        stored = credentials.load()
        if stored is not None and not (env("PAT") or env("TOKEN")):
            token = stored.token
            api_base_url = api_base_url or stored.api_base_url
    return Snapvisor(token=token, api_base_url=api_base_url)


def _emit(args: argparse.Namespace, payload: Any, summary: str | None = None) -> int:
    if getattr(args, "json", False) or summary is None:
        print(json.dumps(_jsonable(payload), indent=2, sort_keys=True, default=str))
    else:
        print(summary)
    return EXIT_OK


def _jsonable(value: Any) -> Any:
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _paged(args: argparse.Namespace, resource: Any, operation: str, **kwargs: Any) -> Any:
    if getattr(args, "all_pages", False):
        return list(resource.auto_paginate(operation, per_page=args.per_page, **kwargs))
    return resource.page(operation, per_page=args.per_page, start_page=args.page, **kwargs).results


def _require_body_model(operation: str) -> Any:
    """The generated request-body class for ``operation``.

    Raises:
        SnapvisorError: If the vendored spec declares no body for it, which would
            mean the CLI and the spec have diverged.
    """
    model = body_model(operation)
    if model is None:
        raise SnapvisorError(f"Operation {operation!r} declares no request body in the spec.")
    return model


def _load_json_arg(value: str | None) -> dict[str, Any] | None:
    """Parse a ``--metadata``-style argument: inline JSON or ``@path/to/file.json``."""
    if not value:
        return None
    raw = value
    if value.startswith("@"):
        try:
            with open(value[1:], encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as error:
            raise SnapvisorConfigError(f"Could not read {value[1:]}: {error}") from error
    try:
        parsed = json.loads(raw)
    except ValueError as error:
        raise SnapvisorConfigError(f"Invalid JSON in {value!r}: {error}") from error
    if not isinstance(parsed, dict):
        raise SnapvisorConfigError("Expected a JSON object.")
    return parsed


def _cmd_upload(args: argparse.Namespace) -> int:
    result = upload(
        args.directory,
        token=args.token,
        build_name=args.build_name,
        branch=args.branch,
        commit=args.commit,
        reference_branch=args.reference_branch,
        parallel=_parallel_from_args(args),
        api_base_url=args.api_base_url,
        threshold=args.threshold,
        concurrency=args.concurrency,
        detect_ci_environment=not args.no_ci_detect,
        build=BuildOptions(
            pr_number=args.pr_number,
            pr_head_commit=args.pr_head_commit,
            reference_commit=args.reference_commit,
            parent_commits=args.parent_commits,
            mode=args.mode,
            ci_provider=args.ci_provider,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            merge_queue=args.merge_queue or None,
            subset=args.subset or None,
            metadata=_load_json_arg(args.metadata),
        ),
    )
    return _emit(
        args,
        {
            "buildUrl": result.build_url,
            "buildId": result.build_id,
            "buildNumber": result.build_number,
        },
        f"Build created: {result.build_url}",
    )


def _cmd_skip(args: argparse.Namespace) -> int:
    build = skip_build(
        token=args.token,
        branch=args.branch,
        commit=args.commit,
        build_name=args.build_name,
        api_base_url=args.api_base_url,
    )
    return _emit(args, build, f"Skipped build created: {build.get('url')}")


def _cmd_finalize(args: argparse.Namespace) -> int:
    payload = finalize_builds(
        parallel_nonce=args.parallel_nonce,
        token=args.token,
        api_base_url=args.api_base_url,
    )
    return _emit(args, payload, "Parallel builds finalized.")


def _cmd_whoami(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        me = sv.users.get_me()
    user = getattr(me, "user", None)
    slug = getattr(user, "slug", None) or getattr(user, "login", None)
    return _emit(args, me, f"Signed in as {slug}" if slug else None)


def _cmd_build_list(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        kwargs: dict[str, Any] = {"owner": args.owner, "project": args.project}
        if args.head:
            kwargs["head"] = args.head
        builds = _paged(args, sv.builds, "listBuilds", **kwargs)
    return _emit(args, builds, "\n".join(f"#{b.number} {b.status}" for b in builds) or "No builds.")


def _cmd_build_get(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        build = sv.builds.get_build(
            owner=args.owner, project=args.project, build_number=str(args.number)
        )
    return _emit(args, build, f"#{build.number} {build.status} — {build.url}")


def _cmd_build_diffs(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        diffs = _paged(
            args,
            sv.builds,
            "listBuildDiffs",
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
        )
    return _emit(args, diffs, f"{len(diffs)} diff(s).")


def _cmd_project_list(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        projects = _paged(args, sv.projects, "listProjects", account_slug=args.account)
    return _emit(args, projects, "\n".join(p.name for p in projects) or "No projects.")


def _cmd_project_get(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        project = sv.projects.get_project(owner=args.owner, project=args.project)
    return _emit(args, project, f"{project.name} — {project.id}")


def _cmd_project_create(args: argparse.Namespace) -> int:
    model = _require_body_model("createProject")
    with _client(args) as sv:
        project = sv.projects.create_project(
            body=model.from_dict({"name": args.name, "accountSlug": args.account})
        )
    return _emit(args, project, f"Created project {args.account}/{args.name}")


def _cmd_comment_list(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        comments = _paged(
            args,
            sv.comments,
            "listComments",
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
        )
    return _emit(args, comments, f"{len(comments)} comment(s).")


def _cmd_comment_create(args: argparse.Namespace) -> int:
    model = _require_body_model("createComment")
    with _client(args) as sv:
        comment = sv.comments.create_comment(
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
            body=model.from_dict({"body": args.body}),
        )
    return _emit(args, comment, "Comment posted.")


def _cmd_comment_delete(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        payload = sv.comments.delete_comment(
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
            comment_id=args.comment_id,
        )
    return _emit(args, payload, "Comment deleted.")


def _cmd_comment_thread(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        payload = getattr(sv.comments, args.operation)(
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
            comment_id=args.comment_id,
        )
    return _emit(args, payload, f"{args.operation} done.")


def _cmd_review_list(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        reviews = _paged(
            args,
            sv.reviews,
            "listReviews",
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
        )
    return _emit(args, reviews, f"{len(reviews)} review(s).")


def _cmd_review_create(args: argparse.Namespace) -> int:
    model = _require_body_model("createReview")
    with _client(args) as sv:
        review = sv.reviews.create_review(
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
            body=model.from_dict({"state": args.state}),
        )
    return _emit(args, review, f"Review {args.state}.")


def _cmd_review_dismiss(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        payload = sv.reviews.dismiss_review(
            owner=args.owner,
            project=args.project,
            build_number=str(args.number),
            review_id=args.review_id,
        )
    return _emit(args, payload, "Review dismissed.")


def _cmd_change(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        payload = getattr(sv.changes, args.operation)(
            owner=args.owner, project=args.project, change_id=args.change_id
        )
    return _emit(args, payload, f"{args.operation} done.")


def _cmd_deployment_get(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        payload = sv.deployments.get_deployment(deployment_id=args.deployment_id)
    return _emit(args, payload)


def _cmd_deployment_resolve(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        payload = sv.deployments.resolve_deployment_domain(domain=args.domain)
    return _emit(args, payload)


def _cmd_analytics(args: argparse.Namespace) -> int:
    with _client(args) as sv:
        kwargs: dict[str, Any] = {
            "account_slug": args.account,
            "from_": args.from_date,
            "group_by": args.group_by,
        }
        if args.to_date:
            kwargs["to"] = args.to_date
        payload = sv.analytics.get_account_analytics(**kwargs)
    return _emit(args, payload)


def _cmd_login(args: argparse.Namespace) -> int:
    token = args.token
    if not token:
        if sys.stdin.isatty():
            print("Paste your Snapvisor personal access token, then press Enter:", file=sys.stderr)
        token = sys.stdin.readline().strip()
    if not token:
        raise SnapvisorConfigError("No token provided.")
    path = credentials.save(credentials.Credentials(token=token, api_base_url=args.api_base_url))
    print(f"Token stored in {path}")
    return EXIT_OK


def _cmd_logout(args: argparse.Namespace) -> int:
    removed = credentials.clear()
    print("Token removed." if removed else "No stored token.")
    return EXIT_OK


def _parallel_from_args(args: argparse.Namespace) -> ParallelConfig | None:
    if args.parallel_nonce is None and args.parallel_total is None:
        return None
    if args.parallel_nonce is None or args.parallel_total is None:
        raise SnapvisorError(
            "Both --parallel-nonce and --parallel-total are required for parallel builds."
        )
    return ParallelConfig(
        nonce=args.parallel_nonce,
        total=args.parallel_total,
        index=args.parallel_index,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result: int = args.func(args)
        return result
    except SnapvisorRateLimitError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_RATE_LIMITED
    except SnapvisorConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except SnapvisorUploadError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_UPLOAD_ERROR
    except SnapvisorAPIError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_API_ERROR
    except SnapvisorError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_API_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
