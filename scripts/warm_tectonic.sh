#!/usr/bin/env sh
#
# Warm Tectonic's cache by compiling every prewarm document.
#
# Shared by the Docker build and by CI, because both need the same thing for the same reason and a
# drifting second copy would be worse than the indirection.
#
# Tectonic fetches packages, the LaTeX format file AND font metrics lazily, each on first use, and
# on a cold cache it *halts* at the first missing file rather than fetching it and carrying on
# ("halted on potentially-recoverable error as specified"). One run therefore resolves roughly one
# missing font, so a document touching a few dozen size/shape combinations would need a few dozen
# runs to converge. That is why this happens in two passes:
#
#   Pass 1 uses -Z continue-on-errors so a single run walks the whole document and pulls down
#   everything it is missing, instead of dying on the first gap.
#   Pass 2 compiles normally. It is the actual assertion: if a prewarm document cannot compile
#   cleanly now, the cache is not warm and we want to fail loudly here rather than at runtime.
#
# The outer retry covers a separate, genuinely transient failure: "error: could not open format
# file latex", which appears perhaps one run in three within about a second of starting and goes
# away on a retry. Without it a network blip breaks the whole image build or CI run.
#
# Usage: sh scripts/warm_tectonic.sh [OUTPUT_DIR]
# Honours TECTONIC_BIN, matching the app's own setting.

set -eu

OUT="${1:-/tmp}"
TECTONIC="${TECTONIC_BIN:-tectonic}"
ATTEMPTS="${WARM_ATTEMPTS:-3}"
DOCS="latex_templates/prewarm.tex latex_templates/prewarm_in.tex"

cleanup() {
  rm -f "$OUT/prewarm.pdf" "$OUT/prewarm_in.pdf"
}

attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
  ok=yes

  # Pass 1: tolerant. Errors are expected and ignored -- the point is the downloads it triggers.
  for doc in $DOCS; do
    "$TECTONIC" --untrusted -Z continue-on-errors -o "$OUT" "$doc" >/dev/null 2>&1 || true
  done

  # Pass 2: strict. This is the check that matters.
  for doc in $DOCS; do
    if ! "$TECTONIC" --untrusted -o "$OUT" "$doc"; then
      ok=no
      break
    fi
  done

  if [ "$ok" = yes ]; then
    cleanup
    echo "Tectonic cache warmed for both layouts"
    exit 0
  fi

  echo "warm attempt ${attempt}/${ATTEMPTS} failed; retrying" >&2
  attempt=$((attempt + 1))
  [ "$attempt" -le "$ATTEMPTS" ] && sleep 5
done

cleanup
echo "could not warm the Tectonic cache after ${ATTEMPTS} attempts" >&2
exit 1
