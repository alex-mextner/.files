#!/usr/bin/env bash
# .claude/scripts/gh-attach-image.sh — Upload an image via GitHub's user-attachments
# API and return the embeddable markdown URL.
#
# Usage:
#   gh-attach-image.sh [--url-only] [--repo OWNER/REPO] <image-path> [PR#]
#
# How it works:
#   1. Uploads <image-path> to `https://uploads.github.com/user-attachments/assets`
#      (`gh api`, full-URL form) — the SAME endpoint GitHub's own web UI calls when a
#      user drags an image into a PR/issue comment box. This is the "attach" flow, not
#      the "commit a file to the repo" flow.
#   2. The response is `{"url": "https://github.com/user-attachments/assets/<uuid>"}`.
#      That URL is a github.com page route, not a raw asset URL — GitHub resolves it at
#      RENDER time (whenever the markdown is displayed) into a short-lived, JWT-signed
#      `private-user-images.githubusercontent.com` URL and rewrites the `<img src>` to
#      point straight at it. The JWT is bound to the repo/asset, not to a specific
#      viewer's cookies, so it loads for ANY viewer with access to the rendering
#      surface — no auth header, no session cookie required to fetch the pixels.
#   3. When PR# is provided, posts a PR comment embedding the image as
#      `![<basename>](<url>)` — ALWAYS the markdown form, regardless of `--url-only`.
#      `--url-only` controls stdout only (see step 4); it never degrades the PR-comment
#      side effect to a bare link. (Keeping these two independent is deliberate — an
#      earlier version let `--url-only` also flatten the PR comment to a bare link, which
#      defeated the comment's entire purpose for no caller-visible reason.)
#   4. Prints to stdout: the complete markdown image string — or, with `--url-only`, just
#      the bare url with no markdown wrapper (HYP-1252: for a caller that needs a plain
#      URL, e.g. `ci/ship/ship.sh`'s SHIP_IMAGE_UPLOAD_CMD contract — "print a bare URL",
#      not markdown — parsing this script's markdown back out in a sibling wrapper is more
#      failure-prone than not producing markdown in the first place).
#
# Why NOT the old mechanism (repo contents API → raw.githubusercontent.com):
#   raw.githubusercontent.com is a separate domain from github.com and does not accept
#   github.com session cookies — only an explicit `Authorization` header. A private
#   repo's raw.githubusercontent.com URL therefore 404s for every browser viewer,
#   including the repo owner looking at the PR in their own logged-in browser (HYP-1248).
#   It also committed a screenshot file into the repo's git history on every call, which
#   this mechanism no longer does — nothing is committed, no branch is touched.
#
# Return codes:
#   0  success — markdown URL printed to stdout
#   1  argument error or upload failure
#
# Invariants:
#   - gh CLI on PATH, authenticated with repo write access (same requirement as
#     pr-ship.sh — no extra credentials needed).
#   - `file` on PATH for MIME-type detection (standard on macOS and Linux).
#   - jq is NOT required; gh's built-in -q (embedded jq) handles the response.
#
# Past bugs:
#   - HYP-1248: the previous version used the repo Contents API and returned a
#     raw.githubusercontent.com URL, which is structurally incapable of rendering for a
#     private repo (see "Why NOT the old mechanism" above). Do not revert to it.
#
# On static review of the upload call below (no `Content-Type` header; `gh api` given a
# full URL on a non-api.github.com host): both were flagged as theoretical breakage by
# static review and both are contradicted by live testing against this repo (real 201
# Created, a resolvable asset URL, and a confirmed `200 image/png` render through
# GitHub's own rendering pipeline — see the HYP-1248 PR description for the exact
# commands and responses). `gh api` resolves credentials by the request's actual host,
# not by assuming api.github.com, and this endpoint reads `content_type` from the query
# string, not an HTTP header. If a future `gh` release changes either behavior, this will
# start failing loudly (`ASSET_URL` empty or the literal string "null" -> exit 1, stderr
# captured and printed) rather than silently.
set -euo pipefail

URL_ONLY=0
REPO_OVERRIDE=""
while :; do
  case "${1:-}" in
    --url-only)
      URL_ONLY=1
      shift
      ;;
    --repo)
      # An explicit --repo flag, not an ambient $GH_REPO read: intercepting the ambient env
      # var would change behavior for EVERY caller of this script, not just the `gh ship
      # --repo` path that needs an override (review finding — an unrelated ambient GH_REPO a
      # developer has exported for their own gh usage silently retargeted uploads, and a
      # host-qualified value hard-failed a caller that never wanted the override at all). A
      # caller that wants a different repo says so explicitly; every other caller keeps the
      # pre-HYP-1252 behavior (bare `gh repo view`) byte-for-byte.
      if [ "$#" -lt 2 ]; then
        echo "Error: --repo requires a value (OWNER/REPO)" >&2
        exit 1
      fi
      REPO_OVERRIDE="$2"
      shift 2
      ;;
    *) break ;;
  esac
done
IMAGE_PATH="${1:-}"
PR="${2:-}"

# ── Argument validation ──────────────────────────────────────────────────────
if [ -z "$IMAGE_PATH" ]; then
  echo "Usage: gh-attach-image.sh [--url-only] [--repo OWNER/REPO] <image-path> [PR#]" >&2
  exit 1
fi
# Any argument past PR# (slot $3+) is silently DROPPED by the bare `IMAGE_PATH=$1; PR=$2`
# above unless checked explicitly — the same "flag typed one position too late" mistake the
# numeric-PR guard catches at $2, one slot further out (e.g. `img.png 42 --repo
# other-org/other-repo` would silently discard the --repo override and upload to the cwd
# repo instead — review finding). Fail loud instead of a slot-position footgun.
if [ "$#" -gt 2 ]; then
  echo "Error: unexpected extra argument(s) after PR#: ${*:3} (flags must come BEFORE the image path)" >&2
  exit 1
fi
if [ ! -f "$IMAGE_PATH" ]; then
  echo "Error: file not found: $IMAGE_PATH" >&2
  exit 1
fi
# A non-numeric PR# is almost always a caller mistake (e.g. `gh-attach-image.sh img.png
# --url-only` — a flag typed AFTER the path lands here as $2, not recognized as the
# --url-only flag it looks like, silently posts nothing and exits 0). Fail loud instead.
if [ -n "$PR" ]; then
  case "$PR" in
    *[!0-9]*)
      echo "Error: PR# must be numeric, got '$PR' (did you mean to pass --url-only BEFORE the image path?)" >&2
      exit 1
      ;;
  esac
fi
command -v gh >/dev/null 2>&1 || { echo "Error: gh CLI not found on PATH" >&2; exit 1; }

# ── Repo resolution ───────────────────────────────────────────────────────────
# --repo (if given) overrides `gh repo view`'s bare (no-argument) behavior, which is
# unconditionally "the repository for the current directory" per `gh repo view --help` — it
# does NOT fall back to $GH_REPO the way most other `gh` subcommands do (verified: with
# GH_REPO set to a different, real, accessible repo, bare `gh repo view` still returned the
# cwd's own repo). ci/ship/ship.sh passes `--repo "${GH_REPO:-}"` as LITERAL text into the
# command it exports as SHIP_IMAGE_UPLOAD_CMD — that `$GH_REPO` reference is expanded by the
# canonical ship.sh's own `eval` at upload time, by which point ship.sh's `--repo`/GH_REPO
# handling (which runs before upload_png()) has already set it, if `gh ship --repo
# owner/other` was used. Without --repo, ONLY `gh ship`'s own `--repo`-set GH_REPO would
# have retargeted the upload; without this flag at all (every other, non-ship caller of this
# script) the pre-HYP-1252 behavior is unchanged.
if [ -n "$REPO_OVERRIDE" ]; then
  # The documented GH_REPO format is `[HOST/]OWNER/REPO` (gh's own `gh help environment`) — a
  # host-qualified value has THREE slash-separated segments. The upload/repo-id calls below
  # (`gh api "repos/$REPO"`, the asset endpoint's `repository_id` lookup) assume plain
  # `OWNER/REPO` and would silently build a garbage API path (`repos/host/owner/repo`) for a
  # host-qualified value — this deployment targets github.com only (no GH Enterprise host in
  # use anywhere in this repo's tooling), so fail loud rather than send a request that would
  # 404 confusingly.
  case "$REPO_OVERRIDE" in
    */*/*)
      echo "Error: --repo '$REPO_OVERRIDE' looks host-qualified ([HOST/]OWNER/REPO) — this script only supports plain OWNER/REPO (github.com)." >&2
      exit 1
      ;;
  esac
  REPO="$REPO_OVERRIDE"
else
  # `||` is load-bearing here, not decorative: under `set -e`, `REPO=$(cmd)` propagates a
  # failing `cmd`'s exit status to the assignment itself and aborts the script AT THIS LINE
  # — before the intended "could not resolve repo" diagnostic below ever runs. The `||`
  # shields the substitution so the diagnostic branch is actually reachable (regression
  # caught in review: an earlier `REPO="${GH_REPO:-$(...)}"` form lost this guard).
  REPO=$(gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null) \
    || { echo "Error: could not resolve repo — is gh authenticated?" >&2; exit 1; }
fi
REPO_ID=$(gh api "repos/$REPO" --jq '.id' 2>/dev/null) \
  || { echo "Error: could not resolve repository id for $REPO" >&2; exit 1; }

# ── MIME type detection ───────────────────────────────────────────────────────
detect_mime_type() {
  local path="$1" mime=""
  if command -v file >/dev/null 2>&1; then
    mime=$(file --mime-type -b "$path" 2>/dev/null || true)
  fi
  if [ -z "$mime" ] || [ "$mime" = "application/octet-stream" ]; then
    case "${path##*.}" in
      png) mime="image/png" ;;
      jpg | jpeg) mime="image/jpeg" ;;
      gif) mime="image/gif" ;;
      webp) mime="image/webp" ;;
      svg) mime="image/svg+xml" ;;
      *) mime="application/octet-stream" ;;
    esac
  fi
  printf '%s' "$mime"
}
MIME_TYPE=$(detect_mime_type "$IMAGE_PATH")
BASENAME=$(basename "$IMAGE_PATH")

# ── URL-encoding ───────────────────────────────────────────────────────────────
# BASENAME and MIME_TYPE are interpolated straight into a query string below. A
# filename with `&`/`#`/spaces (or `image/svg+xml`'s literal `+`, which a naive
# query string would decode back as a space) corrupts the request or sends the
# wrong content_type — percent-encode both before building the URL.
# `local LC_ALL=C` is load-bearing, not decorative: under a UTF-8 locale (the
# default on this machine, `en_US.UTF-8`), bash's `[a-zA-Z0-9.~_-]` case-pattern
# range matches by COLLATION order, not raw ASCII — accented letters (e.g. the
# 'é' in café.png) can silently pass the "safe" branch unescaped. Forcing the C
# locale for this function makes both `${#s}` / `${s:i:1}` iterate BYTES (not
# multibyte characters) and the range match plain ASCII only, so every non-ASCII
# byte of a UTF-8 filename is percent-encoded correctly, one byte at a time.
urlencode() {
  local LC_ALL=C
  local s="$1" out="" c i n
  for (( i = 0; i < ${#s}; i++ )); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      # `printf '%d' "'$c"` sign-extends any byte with the high bit set (>127,
      # i.e. every non-ASCII UTF-8 continuation byte) to a 64-bit negative
      # number — `& 0xFF` masks it back down to the original byte value before
      # hex-formatting. Skipping the mask produces garbage like `%FFFFFFFFFFFFFFC3`
      # instead of `%C3` for accented/emoji filenames.
      *)
        printf -v n '%d' "'$c"
        out+=$(printf '%%%02X' "$(( n & 0xFF ))")
        ;;
    esac
  done
  printf '%s' "$out"
}
BASENAME_ENC=$(urlencode "$BASENAME")
MIME_TYPE_ENC=$(urlencode "$MIME_TYPE")

# ── Upload via the user-attachments asset endpoint ────────────────────────────
# This is the same endpoint GitHub's own web UI calls for drag-and-drop image
# uploads into a PR/issue comment box (`uploads.github.com/user-attachments/assets`).
# `gh api` accepts a full URL (not just an api.github.com-relative path) and still
# reuses the caller's `gh auth` credentials — no separate token handling needed.
UPLOAD_STDERR_FILE=$(mktemp)
trap 'rm -f "$UPLOAD_STDERR_FILE"' EXIT
ASSET_URL=$(gh api \
  --method POST \
  -q '.url' \
  "https://uploads.github.com/user-attachments/assets?name=${BASENAME_ENC}&content_type=${MIME_TYPE_ENC}&repository_id=${REPO_ID}" \
  --input "$IMAGE_PATH" \
  2>"$UPLOAD_STDERR_FILE") || true

# `-q '.url'` prints the literal string "null" (not empty) when the response JSON
# has no `.url` field — a 200-with-error-body, a login/proxy page, or an endpoint
# shape change would otherwise pass the empty-string check below and ship a
# broken `![name](null)` embed. Treat both as failure.
if [ -z "$ASSET_URL" ] || [ "$ASSET_URL" = "null" ]; then
  echo "Error: upload to uploads.github.com/user-attachments/assets failed — no URL returned." >&2
  if [ -s "$UPLOAD_STDERR_FILE" ]; then
    echo "gh api stderr:" >&2
    cat "$UPLOAD_STDERR_FILE" >&2
  fi
  exit 1
fi

# MARKDOWN is what gets posted to the PR (always the embed form, see step 3 above);
# STDOUT_TEXT is what gets printed (controlled by --url-only, see step 4 above). Keeping
# these as two separate variables is deliberate — collapsing them into one would let
# --url-only silently flatten the PR-comment embed to a bare link (HYP-1252 review).
MARKDOWN="![${BASENAME}](${ASSET_URL})"
if [ "$URL_ONLY" = "1" ]; then
  STDOUT_TEXT="$ASSET_URL"
else
  STDOUT_TEXT="$MARKDOWN"
fi

# ── Optional PR comment ───────────────────────────────────────────────────────
# --repo "$REPO" is passed explicitly (not left to gh's own cwd-based resolution) — $REPO is
# always resolved by this point, either from --repo or from the cwd's own repo, so this is a
# no-op restating the same value in the common case. Without it, a --repo override would
# retarget the upload but comment on the WRONG repo's PR (the cwd's, not the override's) —
# review finding: gh-attach-image.sh --repo other-org/other-repo img.png 42 would silently
# post to the cwd repo's PR #42 while the asset itself belongs to other-org/other-repo.
if [ -n "$PR" ]; then
  if ! gh pr comment "$PR" --repo "$REPO" --body "$MARKDOWN" 2>/dev/null; then
    echo "Warning: image uploaded but PR comment failed — markdown URL:" >&2
  fi
fi

printf '%s\n' "$STDOUT_TEXT"
