"""``snapvisor`` command-line interface (argparse-based, no heavy dependencies)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from snapvisor import __version__
from snapvisor.errors import SnapvisorError
from snapvisor.upload import ParallelConfig, upload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snapvisor",
        description="Upload screenshots to the Snapvisor visual regression platform.",
    )
    parser.add_argument("--version", action="version", version=f"snapvisor {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    upload_parser = subparsers.add_parser(
        "upload", help="Upload a directory of screenshots as one build."
    )
    upload_parser.add_argument("directory", help="Directory of screenshots to upload.")
    upload_parser.add_argument(
        "--token",
        help="Snapvisor project token (defaults to the ARGOS_TOKEN env var).",
    )
    upload_parser.add_argument(
        "--build-name",
        help="Build name for multi-build setups (defaults to ARGOS_BUILD_NAME).",
    )
    upload_parser.add_argument(
        "--branch",
        help="Git branch (defaults to ARGOS_BRANCH, then the current git branch).",
    )
    upload_parser.add_argument(
        "--commit",
        help="Git commit SHA (defaults to ARGOS_COMMIT, then git HEAD).",
    )
    upload_parser.add_argument(
        "--reference-branch",
        help="Branch to use as the comparison baseline.",
    )
    upload_parser.add_argument(
        "--api-base-url",
        help="API base URL (defaults to ARGOS_API_BASE_URL, then the public API).",
    )
    upload_parser.add_argument(
        "--parallel-nonce",
        help="Unique nonce shared across parallel shards of the same build.",
    )
    upload_parser.add_argument(
        "--parallel-total",
        type=int,
        help="Total number of parallel shards (-1 to finalize manually).",
    )
    upload_parser.add_argument(
        "--parallel-index",
        type=int,
        help="1-based index of this parallel shard.",
    )
    upload_parser.set_defaults(func=_cmd_upload)
    return parser


def _cmd_upload(args: argparse.Namespace) -> int:
    parallel = _parallel_from_args(args)
    result = upload(
        args.directory,
        token=args.token,
        build_name=args.build_name,
        branch=args.branch,
        commit=args.commit,
        reference_branch=args.reference_branch,
        parallel=parallel,
        api_base_url=args.api_base_url,
    )
    print(f"Build created: {result.build_url}")
    return 0


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
    except SnapvisorError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
