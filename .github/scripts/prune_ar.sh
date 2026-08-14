#!/usr/bin/env bash
#
# Keep Artifact Registry inside the 0.5 GB free tier.
#
# This is belt-and-braces alongside the server-side cleanup policy (ar-cleanup-policy.json),
# which is the primary mechanism but only runs about once a day. Both are needed: the policy
# handles the steady state, this handles a burst of deploys in one afternoon.
#
# Safety: deleting the digest a Cloud Run revision references makes that revision unstartable --
# the service keeps working until it next needs to scale up, and then fails in a way that looks
# nothing like "someone deleted an image". So this script never touches the newest KEEP images,
# and never touches any digest passed in as protected.
#
# Usage: prune_ar.sh AR_IMAGE_BASE [PROTECTED_IMAGE_REF ...]
#   e.g. prune_ar.sh us-central1-docker.pkg.dev/proj/repo/app us-central1-.../app@sha256:abc

set -uo pipefail

BASE="${1:?usage: prune_ar.sh AR_IMAGE_BASE [PROTECTED_IMAGE_REF ...]}"
shift
KEEP="${AR_KEEP_COUNT:-5}"

# Protected digests, as bare sha256:... strings. KEEP=5 means the previous few revisions' images
# survive too, so an instant rollback always has an image to roll back to.
protected=""
for ref in "$@"; do
  protected="${protected} ${ref##*@}"
done
echo "Protected digests:${protected:-<none>}"

mapfile -t digests < <(
  gcloud artifacts docker images list "${BASE}" \
    --sort-by='~CREATE_TIME' --format='value(version)' 2>/dev/null
)

total="${#digests[@]}"
echo "Found ${total} image version(s) under ${BASE}; keeping the newest ${KEEP}."

if [ "${total}" -le "${KEEP}" ]; then
  echo "Nothing to prune."
  exit 0
fi

deleted=0
for index in $(seq "${KEEP}" $((total - 1))); do
  digest="${digests[${index}]}"
  [ -n "${digest}" ] || continue

  case " ${protected} " in
    *" ${digest} "*)
      echo "  SKIP (protected) ${digest}"
      continue
      ;;
  esac

  echo "  DELETE ${digest}"
  if gcloud artifacts docker images delete "${BASE}@${digest}" \
    --delete-tags --quiet 2>&1; then
    deleted=$((deleted + 1))
  else
    # Most likely still referenced by a Cloud Run revision, which Artifact Registry refuses to
    # orphan. Not a failure worth stopping for.
    echo "  could not delete ${digest} (probably still referenced); continuing"
  fi
done

echo "Pruned ${deleted} image version(s)."
