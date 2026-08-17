# CI/CD setup

Everything here runs on free tiers. Follow it in order — several steps fail if done early.

## How the pipeline works

**Stage 1 — feature branch (`ci.yml`).** On every push to a non-`main` branch and every pull
request: ruff (advisory), unit tests, integration tests, LaTeX compile tests, then a Docker build
and a smoke test against the running container. Emails you the result. This is the check branch
protection gates on, so a red branch cannot merge.

**Stage 2 — merge to `main` (`deploy.yml`).** Re-runs the whole suite against the *merge commit*
— a commit nothing has tested before, and the only place a semantic conflict between two
independently-green PRs will show up. Then it builds the image once, smoke-tests it locally,
pushes it to Artifact Registry, and deploys it to Cloud Run as a **candidate revision serving
zero traffic**. It smoke-tests that candidate on real infrastructure, emails you the results, and
opens a GitHub issue. Commenting `approve` shifts production traffic to it.

The image is built once and deployed **by digest**, so what goes live is byte-identical to what
was tested. Because the candidate is already running when you approve, rollback needs no rebuild:

```bash
gcloud run services update-traffic resume-builder --region us-central1 \
  --project resume-builder-503517 --to-revisions <PREVIOUS_REVISION>=100
```

The approval issue prints that command with the right revision already filled in.

## Prerequisites

- A GCP project with **billing enabled**. Cloud Run's free tier still requires a billing account
  attached, even if you stay inside the allowance.
- 2-Step Verification enabled on the Google account you'll send mail from (app passwords require
  it).
- `gcloud` and `gh` installed and authenticated.

## Step 1 — Before the first push (do not skip)

This repository is **public**. Verify nothing private is staged:

```bash
git init -b main
git add -A
git status --porcelain | sort          # read every line
git check-ignore -v .env data/resume.md data/photo.jpg
```

All three of those paths must print a matching ignore rule. If any doesn't, stop.

Already handled: `data/` (your real resume and photo) is gitignored and the test suite generates
its own neutral fixtures with Pillow instead. The stray `ERROR` file is gone, and
`client_secret_*.json` has been moved out of the tree to `%USERPROFILE%\secrets\` — nothing in
`app/` ever read it (Google sign-in is handled entirely by Supabase).

**On rotating that OAuth secret:** it was never committed (there was no git repo), so rotation
isn't strictly required. Rotate only if that file could have left the machine — a cloud-synced
folder, a backup, a shared screen. If you do: Cloud Console → APIs & Services → Credentials → the
OAuth Web client → rotate, then *immediately* paste the new value into Supabase → Authentication →
Providers → Google. Sign-in is broken between those two steps.

## Step 2 — Verify locally

```powershell
./.venv/Scripts/python -m pip install -r requirements-dev.txt
./.venv/Scripts/python -m pytest                    # 91 tests
./.venv/Scripts/python -m ruff check .              # clean
docker build -t rb:local .
docker run --rm -d --name rb -p 8080:8080 -e PORT=8080 `
  -e GEMINI_API_KEY=x -e SUPABASE_URL=https://ci-dummy.supabase.co `
  -e SUPABASE_ANON_KEY=x -e SUPABASE_SERVICE_ROLE_KEY=x rb:local
bash .github/scripts/smoke.sh http://localhost:8080
docker rm -f rb
```

Test lanes, if you want them individually:

| Lane | Command | Count |
| --- | --- | --- |
| Unit | `pytest -m "not integration and not tectonic"` | 36 |
| Integration | `pytest -m "integration and not tectonic"` | 47 |
| LaTeX compile | `pytest -m tectonic` | 8 |

The three lanes partition the suite exactly — 36 + 47 + 8 = 91, nothing run twice, nothing
skipped. The compile lane needs the Tectonic binary and, on a cold cache, network access; it
holds the 3 `compile_service` tests plus the 5 pipeline tests that render and compile a real PDF.

## Step 3 — Create the repo

```bash
gh repo create <owner>/resume_builder --public --source=. --remote=origin
```

Do **not** push yet.

## Step 4 — GCP: keyless auth for GitHub Actions

> **This one is PowerShell, not bash.** Most commands in this document are shell-agnostic, but
> `setup-ci.ps1` must run under PowerShell. Running it from Git Bash fails with
> `syntax error near unexpected token '('` at the `param(` block — that's bash trying to parse
> PowerShell, not a problem with the script.

From a PowerShell prompt:

```powershell
./setup-ci.ps1 -GitHubRepo "<owner>/resume_builder"
```

Or, without leaving Git Bash:

```bash
powershell -ExecutionPolicy Bypass -File ./setup-ci.ps1 -GitHubRepo "<owner>/resume_builder"
```

Use the exact capitalisation GitHub created — a casing mismatch makes the Workload Identity
attribute condition silently never match, which surfaces later as an opaque *Unable to acquire
impersonated credentials*.

The script creates two service accounts (`github-deployer`, which cannot read your API keys, and
`resume-builder-runtime`, which can read exactly two secrets), a Workload Identity pool and OIDC
provider restricted to your repository, and the minimum IAM bindings. No JSON key is created, so
there is no long-lived GCP credential to leak.

If it reports that `gemini-api-key` / `supabase-service-role-key` don't exist yet, run
`./deploy.ps1` once (it writes them to Secret Manager from your `.env`) and re-run the script.

It finishes by printing the exact `gh variable set` / `gh secret set` commands for step 5.

**This step must happen before anything reaches `main`**, or `deploy.yml` dies at the auth step.
No harm done if it does — nothing gets pushed to Artifact Registry.

## Step 5 — GitHub secrets and variables

Run the commands `setup-ci.ps1` printed. For reference:

**Variables** (plaintext, visible in public logs): `WIF_PROVIDER`, `APPROVERS`, `GEMINI_MODEL`,
`GENERATION_LIMIT`, `GENERATION_WINDOW_HOURS`, `PHOTO_BUCKET`, `PDF_BUCKET`.

**Secrets** (masked): `SMTP_USERNAME`, `SMTP_PASSWORD`, `NOTIFY_EMAIL`, `SUPABASE_URL`,
`SUPABASE_ANON_KEY`.

The last two aren't confidential — the anon key ships to browsers and is gated by RLS — they're
secrets only so they're masked in public build logs.

**Never in GitHub:** `GEMINI_API_KEY` and `SUPABASE_SERVICE_ROLE_KEY`. They live only in Secret
Manager and are injected by Cloud Run at instance start.

Get the Gmail app password at Google Account → Security → 2-Step Verification → App passwords.
Strip the spaces. Test it locally before trusting a workflow to it:

```python
import smtplib, ssl
from email.message import EmailMessage
m = EmailMessage(); m["Subject"] = "smtp probe"; m["From"] = USER; m["To"] = USER
m.set_content("hello")
with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
    s.login(USER, APP_PASSWORD); s.send_message(m)
```

## Step 6 — Repository settings

Settings → Actions → General:
- Workflow permissions: **Read repository contents and packages permissions**.
- Fork pull request workflows: **Require approval for all outside collaborators**.

Settings → General → Pull Requests: enable **Allow auto-merge**.

## Step 7 — First push (bootstrap)

**Only for the very first push, into an empty repo.** GitHub makes the first branch it receives
the default branch, so `main` has to go first — pushing a feature branch into an empty repo leaves
you with that branch as your default:

```bash
git checkout main
git push -u origin main
```

There's a chicken-and-egg problem here worth naming: this push puts the workflows on `main`
without them ever having passed Stage 1, because until they exist there is nothing to run. That's
unavoidable and fine.

It does mean **`deploy.yml` fires immediately**. If steps 4–6 are done, that's a real Stage 2 run
and it will stop at the approval issue without touching production. If they aren't, the run fails
at the `google-github-actions/auth` step — harmless, nothing reaches Artifact Registry, and no
traffic moves. Doing steps 4–6 first is simply less noisy.

## Step 7b — Prove Stage 1 works, on a branch

From here on, every change goes through a branch. This is the normal loop:

```bash
git checkout -b feat/some-change
# ... edit ...
git commit -am "..."
git push -u origin feat/some-change
gh pr create --fill
```

`deploy.yml` cannot fire (the branch isn't `main`), so this is a zero-risk dry run of lint, all
three test lanes, the dependency audit, the Docker build, the smoke test, and the report email.
Iterate here until green.

While you're at it, verify the Workload Identity setup in isolation — add a throwaway workflow
with `on: workflow_dispatch`, `permissions: {contents: read, id-token: write}` that only runs
`google-github-actions/auth`, `setup-gcloud`, and `gcloud artifacts repositories list
--location=us-central1`. A green run proves the pool, provider, attribute condition, and AR
access all work. Delete it afterwards.

## Step 8 — Branch protection

Only now, once you know the real check names. Settings → Branches → add a rule for `main`
requiring status checks to pass. Pick the checks from the list the PR run produced — a reusable
workflow's check appears as `<caller job> / <called job>` (e.g. `test / test`). **Typing a guessed
name matches nothing and silently enforces nothing.**

Then `gh pr merge --auto --squash` merges the moment checks go green.

## Step 9 — Stage 2, dry run first

```bash
gh workflow run deploy.yml -f dry_run=true
```

This runs everything — tests, build, push to Artifact Registry, candidate deploy, real-infra
checks, the report email, the approval issue — but **skips the traffic cutover**. Production keeps
serving the old revision the whole time. Comment `approve` to watch the rest of the flow; the
`promote` job will be skipped.

Then do it for real: merge to `main` (or `gh workflow run deploy.yml`), read the email, comment
`approve`.

Practise the rollback command once, deliberately, so you've used it before you need it.

## Step 10 — Finish up

- **Set a $1 budget alert** on the billing account. Console only, two minutes, and it's the real
  safety net. Do not skip this.
- Add the Cloud Run URL to Supabase → Authentication → Providers → Google as an allowed redirect
  URL.
- Arm the Artifact Registry cleanup policy once you've read its dry-run output:
  ```bash
  gcloud artifacts repositories set-cleanup-policies resume-builder \
    --location=us-central1 --project=resume-builder-503517 \
    --policy=ar-cleanup-policy.json --no-dry-run
  ```
- Create the Supabase keepalive (below).

## Keeping Supabase awake

Free Supabase projects pause after 7 days with no database traffic. **Cloud Scheduler is the
primary mechanism** (3 jobs free), because GitHub silently disables `schedule:` workflows after 60
days of repository inactivity — exactly the situation a keepalive exists to survive.

```bash
gcloud scheduler jobs create http supabase-keepalive \
  --location=us-central1 --project=resume-builder-503517 \
  --schedule="17 6 */2 * *" --time-zone=UTC \
  --uri="https://<PROJECT_REF>.supabase.co/rest/v1/resumes?select=user_id&limit=1" \
  --http-method=GET \
  --headers="apikey=<ANON_KEY>,Authorization=Bearer <ANON_KEY>"
```

That URL, not `/health`: **`GET /health` never touches Postgres** — it only checks whether the
Tectonic binary is on `PATH` — so pinging it would let the project pause anyway. A PostgREST
`select` is a real query, and under RLS an anonymous caller gets `200 []`.

`keepalive.yml` does the same thing as a committed fallback. Running both is fine.

**Do not schedule anything against Cloud Run.** It never auto-pauses, and because the service runs
with `--no-cpu-throttling` every wake-up bills roughly 15 minutes of instance time.

## Cost

| Service | Free allowance | Notes |
| --- | --- | --- |
| Actions minutes | unlimited (public repo) | ~6 min Stage 1, ~12 min Stage 2 |
| Actions cache | 10 GB/repo | buildx `mode=max` uses ~1.5–2 GB |
| Artifact Registry | 0.5 GB | fits *because* of the Dockerfile layer order and the cleanup policy — see below |
| AR egress | billed | $0 — the pipeline never pulls from AR |
| Cloud Build | ~2,500 min/mo | $0 — images are built on the runner |
| Secret Manager | 6 active versions | destroy old versions when you rotate |
| Cloud Run | 180k vCPU-s/mo | see below |
| Cloud Scheduler | 3 jobs | 1 used |
| Gmail SMTP | ~500/day | 1–2 per run |

**The one real caveat.** `--no-cpu-throttling` puts the service on instance-based billing: you pay
for the whole instance lifecycle including the ~15 minute idle window, not just request handling.
One cold interaction therefore costs roughly 900 vCPU-s, so the 180,000 free vCPU-s works out to
**about 200 cold interactions per month**. Bursts are cheap (they reuse a warm instance); sparse
traffic is what costs. Keep `--min-instances=0`, don't ping Cloud Run, and set that budget alert.

Each deploy adds one candidate wake-up, about 0.5% of the monthly allowance.

**Artifact Registry, measured.** The built image is ~687 MB uncompressed; Artifact Registry bills
the *compressed* size, which is roughly half that. The shared base layers are stored once, and
thanks to the Dockerfile ordering each new commit adds only the few MB of the `COPY app/` layer —
so three retained tags land in the low hundreds of MB rather than three times the whole image.
That's inside the 0.5 GB allowance but not by a wide margin, so check occasionally:

```bash
gcloud artifacts repositories describe resume-builder \
  --location=us-central1 --project=resume-builder-503517 --format='value(sizeBytes)'
```

If it creeps up, drop `keepCount` in `ar-cleanup-policy.json` from 3 to 2. Keep at least 2, so an
instant rollback always has an image to roll back to.

## Things that fail on the first run if done out of order

- Pushing to `main` before step 4 → `deploy.yml` dies at `google-github-actions/auth`.
- `WIF_PROVIDER` variable not set → `invalid target audience`.
- Repo owner/name casing mismatch in the attribute condition → silent *Unable to acquire
  impersonated credentials*.
- Artifact Registry repo missing → `docker push` returns 403, not a helpful 404.
- Runtime SA without `secretmanager.secretAccessor` → *Permission denied on secret*.
- Deployer without `iam.serviceAccountUser` on the runtime SA → *does not have permission to act
  as*.
- Branch protection configured with guessed check names → matches nothing, enforces nothing.
- SMTP secrets missing when `report` first runs → red report job on an otherwise green build.
- `--no-cpu-throttling` dropped from `deploy.yml` → `POST /generate` returns 202 and the
  background thread freezes. Jobs hang mid-pipeline, silently.
- The "Warm the Tectonic cache" step removed from `_test.yml`, or `scripts/warm_tectonic.sh`
  changed → see below. It looks redundant. It isn't.

## Tectonic's cache, and why warming it is not optional

Both the Docker build and CI run `scripts/warm_tectonic.sh` before anything compiles for real. Three
behaviours make that necessary, and all three cost real debugging time to find:

1. **Tectonic fetches font metrics lazily, separately from `.sty` files.** Loading a package does not
   pull the fonts that package's output will need. `prewarm.tex` originally exercised the *packages*
   but not the *typography*, so a container with no network could not compile a resume at all —
   `Font T1/lmr/bx/n/24.88=ec-lmbx12 not loadable`, from the `\Huge \textbf` in the header. This was
   invisible in production only because Cloud Run has egress and silently downloaded them, which is
   precisely what the prewarm exists to avoid. Both prewarm documents now sweep every size, series
   and shape their template can reach.
2. **On a cold cache Tectonic halts at the first missing file** rather than fetching it and
   continuing (`halted on potentially-recoverable error as specified`), so one run resolves roughly
   one missing font. A plain retry loop cannot converge on a document with dozens of font
   combinations. The script's first pass therefore uses `-Z continue-on-errors`, which walks the
   whole document and pulls everything down in one go; the second, strict pass is the real
   assertion. In CI this bug showed up as exactly one failing test with the ten after it passing —
   because that first failure was itself what warmed the cache.
3. **The first fetch is flaky**: `error: could not open format file latex`, roughly one run in three,
   within a second of starting, fixed by retrying. Hence the outer retry loop.

Also note the cache lives at `~/.cache/tectonic`, **lowercase**. Pointing `actions/cache` at
`~/.cache/Tectonic` fails soft: the save step warns `Path(s) specified in the action for caching
do(es) not exist` and caches nothing, so every run silently re-downloads the whole bundle.

## Notes on the test suite

`tests/conftest.py` hard-sets the eight config environment variables. pydantic-settings ranks
`os.environ` above the dotenv file, so this *overwrites* anything in your `.env` or exported in
your shell — a test run can never reach the real Supabase project or spend real Gemini quota. The
`pytest_configure` guard re-reads `Settings()` and aborts if the URL isn't the dummy; it exists to
catch the shadowing itself breaking, not to catch stray exports (those are already neutralised).

The integration tests patch `app.db` functions on the *module*, which works because `app/main.py`
calls them as `db.get_resume_markdown(...)`. The Gemini helpers are patched on **`app.main`**, not
on `app.tailor` / `app.cover_letter` / `app.pdf_import`, because `main.py` imports them by name —
patching the defining module would leave `main`'s reference untouched and the tests would silently
call the real API.
