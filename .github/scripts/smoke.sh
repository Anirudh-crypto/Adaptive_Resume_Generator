#!/usr/bin/env bash
#
# Black-box smoke test against a running instance of the app.
#
# Deliberately written to work against any base URL, because the same checks are wanted in three
# places: the container built on the CI runner, the zero-traffic Cloud Run candidate revision,
# and production immediately after the traffic cutover. One script means those three can never
# drift apart into "the check that passed" and "the check that mattered".
#
# Every assertion here is READ-ONLY. When this runs against the candidate revision it is talking
# to the real production Supabase project, and a write would pollute live data. Anything needing
# writes belongs in the integration suite, against the in-memory fakes.
#
# Usage: bash .github/scripts/smoke.sh https://host[:port]

set -euo pipefail

BASE="${1:?usage: smoke.sh BASE_URL}"
ATTEMPTS="${SMOKE_ATTEMPTS:-30}"
DELAY="${SMOKE_DELAY:-2}"

fail() {
  echo "SMOKE FAIL: $*" >&2
  exit 1
}

echo "Smoke testing ${BASE}"

# 1. Wait for the service to come up, then assert the /health *payload*. The route returns 200
#    even when the Tectonic binary is missing (it only reports the state in the body), so a bare
#    `curl -f /health` would happily pass on a deployment that cannot compile a single PDF.
health=""
for attempt in $(seq 1 "${ATTEMPTS}"); do
  if health=$(curl -fsS --max-time 10 "${BASE}/health" 2>/dev/null); then
    break
  fi
  echo "  waiting for /health (${attempt}/${ATTEMPTS})..."
  sleep "${DELAY}"
done
[ -n "${health}" ] || fail "/health never answered after $((ATTEMPTS * DELAY))s"
echo "  GET /health -> ${health}"
case "${health}" in
  *'"compiler_online":true'*) ;;
  *) fail "Tectonic is not usable in this deployment: ${health}" ;;
esac

# 2. Both server-rendered pages must actually render.
page_html="$(mktemp)"
for path in / /profile; do
  code=$(curl -fsS -o "${page_html}" -w '%{http_code}' --max-time 20 "${BASE}${path}") \
    || fail "GET ${path} failed"
  echo "  GET ${path} -> ${code}"
  [ -s "${page_html}" ] || fail "GET ${path} returned an empty body"
done

# 3. The HTML document must be uncacheable. Paired with the fingerprinting checked below: assets
#    are fingerprinted so a stale one cannot be reused, and the document is no-store so a stale
#    page cannot be reused either. Miss this half and a deploy can leave a browser rendering the
#    previous release's markup, where a newly shipped control is simply absent -- indistinguishable
#    from the feature being broken.
cache_header=$(curl -fsS -D - -o /dev/null --max-time 20 "${BASE}/" | tr -d '\r' \
  | awk 'tolower($1) == "cache-control:" { $1=""; sub(/^ /, ""); print }')
echo "  GET / Cache-Control -> ${cache_header:-<none>}"
case "${cache_header}" in
  *no-store*) ;;
  *) fail "GET / must send Cache-Control: no-store, got '${cache_header:-<none>}'" ;;
esac

# 4. Static assets must be content-fingerprinted, and the fingerprinted URL must resolve.
#    This is the check that catches a deploy serving the previous release's JS against the new
#    release's markup -- a failure that is otherwise silent in the browser.
curl -fsS -o "${page_html}" --max-time 20 "${BASE}/" || fail "GET / failed"
asset=$(grep -oE '/static/[A-Za-z0-9._/-]+\?v=[0-9a-f]{12}' "${page_html}" | head -n1 || true)
[ -n "${asset}" ] || fail "the index page references no fingerprinted /static asset"
code=$(curl -fsS -o /dev/null -w '%{http_code}' --max-time 20 "${BASE}${asset}") \
  || fail "fingerprinted asset ${asset} did not resolve"
echo "  GET ${asset} -> ${code}"

# 5. An authenticated route must reject an anonymous caller. Cheap proof that auth is wired up
#    and that we have not just deployed a service with its doors open.
#    No -f here: curl would treat the expected 401 as an error.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 "${BASE}/me/resume")
[ "${code}" = "401" ] || fail "GET /me/resume returned ${code}, expected 401"
echo "  GET /me/resume (anonymous) -> ${code}"

rm -f "${page_html}"
echo "Smoke tests passed against ${BASE}"
