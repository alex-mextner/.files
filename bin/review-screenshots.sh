#!/usr/bin/env bash
# review-screenshots.sh — aggressive screenshot review via Codex
#
# Usage:
#   review-screenshots.sh <png> [png ...]                    # review specific files
#   review-screenshots.sh --dir <dir>                        # review all PNGs in dir
#   review-screenshots.sh --dir <dir> --send-tg              # also send each PNG to TG with verdict
#   review-screenshots.sh --context "what this proves" <png> # context-aware review
#
# --context: one-line description of what the screenshot is supposed to prove.
#   Without it, Codex uses generic canvas/inspector/chat criteria.
#   With it, Codex judges ONLY whether the screenshot proves that specific thing.
#   Examples:
#     --context "element selected in tree scrolls canvas viewport"
#     --context "inspector shows style values for selected element"
#     --context "AI Chat responds to user message"
#
# Exit codes:
#   0 — all screenshots passed review
#   1 — one or more screenshots rejected (empty, blank, wrong state)
#   2 — usage error
#
# Framing: ATTACK mode. Assume every screenshot is broken until proven otherwise.
# Codex finds defects — it does NOT confirm expectations.

set -euo pipefail

SEND_TG=false
DIR=""
CONTEXT=""
FILES=()
FAILED=0
PASSED=0

# tg CLI: text -> `tg "msg"`, photo+caption -> `tg --photo /abs/path.png "caption"`
TG_CLI="$(command -v tg || echo "$HOME/.files/bin/tg")"
# frames-check: deterministic selection-frame detector (color+shape isolation).
# ALWAYS run before TG — a "selection works" proof must not ship with no frame
# drawn (the 2px outline is too thin for a model to see on a full screenshot).
# Locate it whether or not review-cli/bin is on PATH.
FRAMES_CHECK="$(command -v frames-check || echo "$HOME/xp/review-cli/bin/frames-check")"
MIN_FRAMES=""   # if set (or --context mentions a selection), too few frames = hard REJECT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) DIR="$2"; shift 2 ;;
    --send-tg) SEND_TG=true; shift ;;
    --context) CONTEXT="$2"; shift 2 ;;
    --min-frames) MIN_FRAMES="$2"; shift 2 ;;
    *.png|*.PNG) FILES+=("$1"); shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -n "$DIR" ]]; then
  # Skip *.frames-only.png: frames-check artifacts (older runs wrote them beside the source).
  while IFS= read -r f; do FILES+=("$f"); done < <(find "$DIR" \( -name "*.png" -o -name "*.PNG" \) ! -name "*.frames-only.png" 2>/dev/null | sort)
fi

# frames-check writes its isolation images here, never beside the reviewed screenshots.
FC_TMP="$(mktemp -d "${TMPDIR:-/tmp}/review-screenshots.XXXXXX")"
trap 'rm -rf "$FC_TMP"' EXIT

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "Usage: review-screenshots.sh <png> [png ...] | --dir <dir> [--send-tg]" >&2
  exit 2
fi

build_prompt() {
  local context="$1"
  local context_block=""
  if [[ -n "$context" ]]; then
    context_block="
WHAT THIS SCREENSHOT IS SUPPOSED TO PROVE:
\"${context}\"

Judge ONLY whether the screenshot proves that specific thing.
Ignore panels/areas unrelated to what is being proved.
If the screenshot shows the claimed state — PASS. If it doesn't — REJECT.
"
  fi

  cat <<PROMPT
ATTACK MODE: You are a hostile QA engineer trying to reject this screenshot.
Your job is to FIND DEFECTS, not confirm that things look OK.
${context_block}
Always check these regardless of context:

1. BLANK/EMPTY STATE — Is the main content area empty? Specifically:
   - "No active project" / "No project" text visible?
   - Canvas showing only gray background with no content?
   - Preview iframe blank or showing 404/loading?
   - VS Code window loaded but no extension content visible?

2. WRONG STATE — Is the UI showing a state that does NOT prove the feature works?
   - Loading spinner still visible?
   - Error toast/banner present?
   - Stale/previous state shown (e.g., teardown state after test cleanup)?

3. UNREADABLE — Is the screenshot technically valid but useless as proof?
   - Completely cropped (no context)?
   - Too small to read text?
   - Screenshot of a terminal/log instead of the actual UI?

4. FEATURE ABSENCE — Does the screenshot actually show what it claims to prove?$(if [[ -z "$context" ]]; then echo "
   Generic checks (no --context given):
   - Inspector panel: are there actual style values, not 'No component open'?
   - Chat: is there a message exchange visible, or just an empty input?
   - Canvas: are elements rendered, or is it gray?
   - Element selection: is an element highlighted with bounding box?"; fi)

5. BROKEN THEME — Does the UI respect the active color theme? REJECT if any of:
   - Light/white inputs, cards, fields, or popovers sitting on a DARK panel (or dark
     elements on a LIGHT panel) — a hard-coded color that ignores the theme.
   - Text with poor contrast against its own background (label/value barely readable).
   - Mixed theming within one panel (some controls themed, some not).
   You see ONE screenshot of a series; the other theme may be in a separate file, so do
   NOT reject this one merely for showing a single theme — judge that the theme it shows
   is rendered correctly. (When the caller's --context names a specific theme, judge that
   the screenshot actually matches it.)

VERDICT FORMAT (mandatory, exact):
VERDICT: PASS — [one-line reason: what specifically proves the feature works]
or
VERDICT: REJECT — [list every defect found, be specific, no mercy]

Be hostile. One visible defect = REJECT. Do not give benefit of the doubt.
PROMPT
}

ATTACK_PROMPT="$(build_prompt "$CONTEXT")"

echo "=== Screenshot Review (ATTACK MODE) ==="
echo "Files: ${#FILES[@]}"
echo ""

RESULTS=()

for f in "${FILES[@]}"; do
  if [[ ! -f "$f" ]]; then
    echo "SKIP (not found): $f"
    continue
  fi

  fname=$(basename "$f")
  echo "--- Reviewing: $fname"

  # Fast heuristic: check file size (very small = probably blank)
  fsize=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f" 2>/dev/null || echo 0)
  if [[ "$fsize" -lt 5000 ]]; then
    echo "VERDICT: REJECT — file too small (${fsize} bytes), likely blank or corrupt"
    FAILED=$((FAILED + 1))
    RESULTS+=("REJECT|$fname|too small (${fsize} bytes)")
    if $SEND_TG; then
      "$TG_CLI" "❌ REJECT: $fname — too small (${fsize}B), likely blank" 2>/dev/null || true
    fi
    continue
  fi

  # --- frames-check: isolate + count selection frames (color+shape) BEFORE codex ---
  frames_only=""; frames_n=0
  if [[ -x "$FRAMES_CHECK" ]]; then
    fc_out="$FC_TMP/$((FAILED + PASSED)).${fname%.*}.frames-only.png"
    fj="$("$FRAMES_CHECK" "$f" --out "$fc_out" --json 2>/dev/null || true)"
    frames_n="$(printf '%s' "$fj" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("detected") or 0)
except Exception: print(0)' 2>/dev/null || echo 0)"
    [[ -f "$fc_out" ]] && frames_only="$fc_out"
  fi
  # Is a selection/frame claimed here? (explicit --min-frames, or selection words in context)
  selection_claim=false
  if [[ -n "$MIN_FRAMES" ]] || printf '%s' "$CONTEXT" | grep -qiE 'select|multi-?select|frame|выдел|рамк'; then
    selection_claim=true
  fi
  min_required="${MIN_FRAMES:-1}"
  # Deterministic gate: selection claimed but CV found too few frames → REJECT before codex.
  if $selection_claim && [[ "$frames_n" -lt "$min_required" ]]; then
    echo "VERDICT: REJECT — selection claimed but frames-check found $frames_n frame(s) (need >= $min_required)"
    FAILED=$((FAILED + 1))
    RESULTS+=("REJECT|$fname|frames-check: $frames_n < $min_required selection frames")
    if $SEND_TG; then
      "$TG_CLI" --photo "${frames_only:-$f}" "❌ REJECT: $fname — frames-check: $frames_n selection frame(s), need >= $min_required" 2>/dev/null || true
    fi
    continue
  fi
  frames_note=""
  [[ -n "$frames_only" ]] && frames_note="A SECOND image is attached showing ONLY those frames (lit on black) — use it to confirm presence and positions."
  frames_block="

CV FRAME-ISOLATION (deterministic): a detector isolated selection-frame pixels by color (rgb(59,130,246)) and shape (hollow rectangle) and found ${frames_n} frame(s). ${frames_note} If this screenshot claims a selection/multi-select, there MUST be at least 1 frame and the count should match the claim; 0 frames while a selection is claimed = REJECT."
  fc_img_args=(); [[ -n "$frames_only" ]] && fc_img_args=(-i "$frames_only")

  # Codex analysis with image(s): original + the frames-only isolation
  verdict=$(timeout 120 codex exec -i "$f" ${fc_img_args[@]+"${fc_img_args[@]}"} \
    --config "model=gpt-5.4" \
    --config 'sandbox_permissions=["disk-full-read-access"]' \
    "${ATTACK_PROMPT}${frames_block}" 2>/dev/null | grep "^VERDICT:" | tail -1 || echo "VERDICT: REJECT — codex analysis failed or timed out")

  echo "$verdict"
  echo ""

  if echo "$verdict" | grep -q "^VERDICT: PASS"; then
    PASSED=$((PASSED + 1))
    RESULTS+=("PASS|$fname|${verdict#VERDICT: PASS — }")
    if $SEND_TG; then
      "$TG_CLI" --photo "$f" "✅ PASS: $fname — ${verdict#VERDICT: PASS — }" 2>/dev/null || true
    fi
  else
    FAILED=$((FAILED + 1))
    RESULTS+=("REJECT|$fname|${verdict#VERDICT: REJECT — }")
    if $SEND_TG; then
      "$TG_CLI" --photo "$f" "❌ REJECT: $fname — ${verdict#VERDICT: REJECT — }" 2>/dev/null || true
    fi
  fi
done

echo "=== Summary ==="
echo "PASSED: $PASSED"
echo "REJECTED: $FAILED"
echo ""

for r in "${RESULTS[@]}"; do
  verdict="${r%%|*}"
  rest="${r#*|}"
  name="${rest%%|*}"
  reason="${rest#*|}"
  if [[ "$verdict" == "PASS" ]]; then
    echo "  ✅ $name — $reason"
  else
    echo "  ❌ $name — $reason"
  fi
done

if [[ $FAILED -gt 0 ]]; then
  echo ""
  echo "RESULT: REJECTED ($FAILED of $((PASSED + FAILED)) screenshots failed review)"
  echo "Do NOT send to TG. Fix the screenshots first."
  exit 1
fi

echo ""
echo "RESULT: ALL PASSED — safe to send to TG"
exit 0
