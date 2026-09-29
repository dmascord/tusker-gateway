#!/usr/bin/env python3
"""Pin the built image digest and build identity into a tracked manifest.

deploy.sh runs on the build host, whose .git may be stale or absent, so it can
never commit on the operator's behalf. This helper keeps the manifest edit
mechanical: it prints a unified diff by default and rewrites the file with
--write, leaving the commit itself to the workstation.

Pinned fields:
  * the container image as repo@sha256:<digest> - an immutable reference, because
    a tag lets a re-push silently change what a restarted pod runs
  * Deployment annotations tusker.net.au/{commit,image-tag,image-digest}, which
    record the source revision next to the artifact it produced
"""

import argparse
import difflib
import re
import sys

IMAGE_RE = re.compile(
    r"^(\s*image: registry\.tusker\.net\.au:5000/tusker-gateway)@sha256:[0-9a-f]{64}$",
    re.MULTILINE,
)

LABELS_BLOCK = "  labels:\n    app: tusker-gateway\nspec:\n"

ANNOTATIONS_RE = re.compile(
    r"^  annotations:\n(?:    tusker\.net\.au/[^\n]*\n)+", re.MULTILINE
)


def pin(text, digest, commit, tag):
    """Return `text` with the image digest and build annotations pinned."""
    text, replacements = IMAGE_RE.subn(r"\g<1>@" + digest, text, count=1)
    if replacements != 1:
        raise SystemExit(
            "ERROR: expected exactly one "
            "'image: registry.tusker.net.au:5000/tusker-gateway@sha256:<64 hex>' line"
        )

    annotations = (
        "  annotations:\n"
        "    tusker.net.au/commit: {commit}\n"
        "    tusker.net.au/image-tag: {tag}\n"
        "    tusker.net.au/image-digest: {digest}\n"
    ).format(commit=commit, tag=tag, digest=digest)

    if ANNOTATIONS_RE.search(text):
        return ANNOTATIONS_RE.sub(annotations, text, count=1)
    if LABELS_BLOCK not in text:
        raise SystemExit("ERROR: cannot locate the Deployment metadata labels block")
    return text.replace(
        LABELS_BLOCK, "  labels:\n    app: tusker-gateway\n" + annotations + "spec:\n", 1
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("--digest", required=True, help="manifest digest (sha256:...)")
    parser.add_argument("--commit", required=True, help="full 40-character source SHA")
    parser.add_argument("--tag", required=True, help="image tag published for that SHA")
    parser.add_argument(
        "--write", action="store_true", help="rewrite the file instead of printing a diff"
    )
    args = parser.parse_args()

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.digest):
        raise SystemExit("ERROR: not a manifest digest: {}".format(args.digest))
    if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
        raise SystemExit("ERROR: not a full 40-character commit SHA: {}".format(args.commit))

    with open(args.manifest) as handle:
        original = handle.read()

    pinned = pin(original, args.digest, args.commit, args.tag)

    if pinned == original:
        print("{} already pinned to {}; nothing to commit".format(args.manifest, args.digest))
        return 0

    if args.write:
        with open(args.manifest, "w") as handle:
            handle.write(pinned)
        print("pinned {}: {} (commit {})".format(args.manifest, args.digest, args.commit))
        return 0

    sys.stdout.writelines(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            pinned.splitlines(keepends=True),
            fromfile="a/" + args.manifest,
            tofile="b/" + args.manifest,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())