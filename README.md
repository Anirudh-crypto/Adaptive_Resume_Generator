# Resume Builder

Sign in, upload your resume as a PDF (or write it in markdown), paste a job description, and get a
version of your resume tailored to it — rendered from a LaTeX template and compiled to PDF — plus a
matching cover letter as a `.docx`. Multi-user: each person who signs in gets their own resume,
photo, and generated documents.

## How it works

1. Sign in with Google.
2. On the **My Resume & Photo** page, upload your existing resume as a PDF (auto-extracted into an
   editable markdown convention via Gemini) or write it directly, plus an optional headshot photo.
3. On the home page, paste a job description and click Generate. The app sends your resume content
   + the job description to Gemini, asking it to reorder, select, and rephrase your existing
   summary/bullets/skills to match the JD — it is not allowed to invent employers, titles, dates,
   or skills that aren't already in your resume (see "Anti-hallucination design" below).
4. The same run then writes a **cover letter** from the *tailored* resume plus the job description,
   so the letter argues from the same evidence the resume leads with. It's rendered to a `.docx`
   (`app/docx_render.py`, via `python-docx`) so you can edit it before sending.
5. The tailored resume content is rendered into a LaTeX document and compiled to a PDF in-process by
   [Tectonic](https://tectonic-typesetting.github.io/) (a self-contained LaTeX engine — no
   TeX Live install needed). Both documents are stored in Supabase Storage and served back to you
   as signed download links.

One click, one rate-limit unit: there's no separate "generate cover letter" button. If cover letter
generation fails, the resume PDF is still delivered and the failure is surfaced as a warning on the
result card rather than failing the whole job.

## Architecture

- **App**: FastAPI + vanilla HTML/JS (no frontend build step), deployable as a single container.
- **Auth**: Google sign-in via Supabase Auth. The backend verifies session JWTs against Supabase's
  public JWKS endpoint (`app/auth.py`) — no shared secret to manage.
- **Data**: Supabase Postgres (`resumes`, `generation_jobs`, `usage_counters` tables) + Supabase
  Storage (`photo`, `resume-pdf` buckets — the latter holds every generated artifact, resume PDFs
  and cover letter `.docx` files alike), accessed server-side via the service_role key
  (`app/db.py`). Job/progress state lives in the database, not in-process memory, so it's correct
  under Cloud Run's multi-instance autoscaling.
- **Tailoring + cover letter**: Google Gemini, one shared API key, with a per-user daily generation
  limit (`DAILY_GENERATION_LIMIT`) to protect the shared free-tier quota. One generation = one
  tailoring call (`app/tailor.py`) + one cover letter call (`app/cover_letter.py`).
- **PDF compiling**: Tectonic, invoked in-process (`app/compile_service.py`) — no separate service.
- **DOCX writing**: `python-docx`, in-process (`app/docx_render.py`) — no Word/LibreOffice needed.
- **Hosting**: designed for Google Cloud Run's always-free tier (scales to zero, no time-boxed
  trial) + Supabase's free tier.

## Local development setup

### 1. Supabase project

Create a free project at [supabase.com](https://supabase.com), then in the SQL Editor run the
schema in `supabase_schema.sql`. Create two **private** Storage buckets: `photo` and `resume-pdf`
(the latter holds cover letter `.docx` files too — no extra bucket needed).
Enable the Google provider under Authentication → Providers (needs a Google OAuth Client ID/Secret
from the [Google Cloud Console](https://console.cloud.google.com/apis/credentials); use the
redirect URL Supabase shows on that page). Grab your Project URL, anon key, and service_role key
from Project Settings → API.

Already have a project from before cover letters landed? Don't re-run the whole schema — just run
the migration block at the bottom of `supabase_schema.sql` (it's `add column if not exists`, so
it's safe to re-run).

### 2. Python environment

```
python -m venv .venv
./.venv/Scripts/pip install -r requirements.txt
```

For running the tests or the linter, install the dev tooling too — it's kept separate so none of
it ships inside the production container image:

```
./.venv/Scripts/pip install -r requirements-dev.txt
```

### 3. Tectonic

Needed locally for PDF compiling. On Windows, grab a release binary from the
[Tectonic releases page](https://github.com/tectonic-typesetting/tectonic/releases) and point
`TECTONIC_BIN` at it in `.env`. On Linux/macOS, install it and leave `TECTONIC_BIN=tectonic` (must
be on `PATH`).

### 4. Configure secrets

Copy `.env.example` to `.env` and fill in `GEMINI_API_KEY` (free at
[aistudio.google.com](https://aistudio.google.com)) and the Supabase values from step 1.

### 5. Run the app

```
./.venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000, sign in, add your resume on the profile page, then generate.

## Deploying to Google Cloud Run

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com

printf '%s' "$GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=-
printf '%s' "$SUPABASE_SERVICE_ROLE_KEY" | gcloud secrets create supabase-service-role-key --data-file=-

gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT_ID/resume-builder/app:latest .

gcloud run deploy resume-builder \
  --image REGION-docker.pkg.dev/PROJECT_ID/resume-builder/app:latest \
  --region REGION --allow-unauthenticated \
  --no-cpu-throttling \
  --min-instances=0 --max-instances=2 --memory=512Mi --cpu=1 \
  --set-env-vars "SUPABASE_URL=https://xxxx.supabase.co,SUPABASE_ANON_KEY=your-anon-key,GEMINI_MODEL=gemini-3.5-flash,DAILY_GENERATION_LIMIT=5,PHOTO_BUCKET=photo,PDF_BUCKET=resume-pdf" \
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,SUPABASE_SERVICE_ROLE_KEY=supabase-service-role-key:latest"
```

Important flags/gotchas:
- **`--no-cpu-throttling` is required.** By default Cloud Run only allocates CPU while actively
  handling a request; the generation pipeline runs in a background thread after `/generate`
  returns, which silently stalls without this flag.
- **`--max-instances=2`** (or higher) is intentional, not a typo — job/progress state lives in
  Supabase specifically so it stays correct across multiple Cloud Run instances. Don't "fix" a
  perceived bug by dropping to `--max-instances=1`; that just hides the thing this design solves.
- **Google OAuth redirect URL**: once deployed, add the Cloud Run URL to Supabase's Google
  provider's allowed redirect URLs — otherwise sign-in will fail after moving off `localhost`.
- **Supabase free-tier auto-pause**: a Supabase project on the free tier pauses after 7 days with
  zero API traffic. If this app gets infrequent use, add a Cloud Scheduler job hitting `/health`
  every few days (and consider having `/health` also touch Supabase, since only real API traffic
  resets the pause timer).

## `resumes.markdown_text` convention

Whether typed by hand or produced by the PDF import/extraction step, resume content follows this
markdown convention:

```markdown
# Full Name

Email: you@example.com | Phone: +1 555 123 4567 | Location: City, Country | LinkedIn: linkedin.com/in/you | Website: yoursite.com

## Summary

A short paragraph summarizing your background.

## Skills

Programming Languages: Python, C++, C
Cloud & DevOps: AWS, Docker, CI/CD

## Experience

### Job Title | Organization | Location | Dates

- A bullet point describing what you did.
- Another bullet point.

Technologies: Python, Docker

### Another Job Title | Another Org | Location | Dates

- ...

## Projects

### Project Name | link-or-blank | Dates

- Bullet point.

Technologies: Python

## Education

### Degree | Institution | Location | Dates

## Languages

English (Native), German (B2)
```

Rules:
- Exactly one `# ` heading at the top = your name.
- The line right after it (before the first `## `) is the contact line: `Key: value` pairs
  separated by ` | `. Recognized keys: Email, Phone, Location, LinkedIn, GitHub, Website.
- `## ` section headings are matched case-insensitively (`Summary`/`Objective`,
  `Experience`/`Work Experience`, `Education`, `Skills`/`Technical Skills`, `Projects`,
  `Languages`). Unrecognized `## ` sections are skipped (logged as a warning), not silently
  merged into another section.
- Under Experience/Projects, `### ` headings are pipe-delimited entry headers:
  `Title | Organization | Location | Dates` (leave Location blank if not applicable — two `|`
  in a row). Education uses `Degree | Institution | Location | Dates`. Missing trailing fields
  default to blank rather than erroring.
- Bullets are `- ` or `* ` lines directly under an entry header.
- A plain (non-bulleted) `Technologies: X, Y, Z` line right after an entry's bullets is parsed
  as that entry's technology list and rendered as a separate italic line — it's treated as fixed
  metadata, not sent to Gemini for tailoring.
- Skills accept either categorized lines (`Category Name: item, item, ...`, one category per
  line) or a single flat comma-separated/bullet list if you don't want categories.
  Languages always uses a flat comma-separated line or bullet list.
- An optional photo (uploaded via the profile page) is included in the header if present.

## Anti-hallucination design

**Tailoring** (`app/tailor.py`): only the summary, per-entry bullet text, and per-category skill
selection are sent to Gemini as editable — and even then, skills can only be reordered/subset
-selected within their original category, never moved to a different category or invented.
Company names, job titles, dates, locations, education, and the `Technologies:` line per entry are
never sent as editable fields at all — they're merged back from your saved resume text in code
after tailoring, so the model cannot alter or invent them regardless of what it returns.

**Cover letter** (`app/cover_letter.py`): prose can't be diffed against a source the way structured
fields can, so the guardrails are narrower by construction. The model is given *only* the tailored
resume as evidence (`_resume_digest`) and told not to claim anything it doesn't show — but it is
genuinely writing new sentences, so **read the letter before you send it**. What is enforced in
code: your name and contact details are never model output at all (they're taken from your saved
resume when the `.docx` is built), and unresolved mail-merge placeholders like `[Company Name]` are
stripped rather than shipped.

**PDF import** (`app/pdf_import.py`): a different guardrail concern, since there's no "original" to
diff against — the uploaded PDF *is* the source of truth. The extraction prompt instructs the model
to organize content faithfully without inventing anything not present in the source text, and the
extracted markdown is always shown to you for review/correction before it's saved (uploading a PDF
never auto-saves).

## Tests

```
./.venv/Scripts/python -m pytest
```

91 tests in three lanes, which partition the suite exactly:

| Lane | Command | Count | Needs |
| --- | --- | --- | --- |
| Unit | `pytest -m "not integration and not tectonic"` | 36 | nothing |
| Integration | `pytest -m "integration and not tectonic"` | 47 | nothing |
| LaTeX compile | `pytest -m tectonic` | 8 | Tectonic binary, network on a cold cache |

The integration lane drives the real FastAPI app through `TestClient` — real routing, real
validation, real Jinja2 rendering, real resume parsing, real `.docx` output — with only Supabase,
Gemini, and authentication faked. The compile lane additionally renders and compiles real PDFs.

`tests/conftest.py` hard-sets the config environment variables, and pydantic-settings ranks
`os.environ` above the dotenv file, so a test run can never reach your real Supabase project or
spend real Gemini quota, whatever is in your `.env`.

## CI/CD

Push a branch → tests run and you get an emailed report → merge to `main` → the full suite re-runs
on the merge commit, the image is built once and deployed to a **zero-traffic candidate revision**
on Cloud Run, checked there, and then a GitHub issue asks you to approve. Commenting `approve`
shifts production traffic to the already-running revision, so rollback needs no rebuild.

Everything runs on free tiers. Setup, cost breakdown, and the ordering constraints that matter are
in **[docs/ci-cd-setup.md](docs/ci-cd-setup.md)**. `deploy.ps1` remains the manual escape hatch.

## Notes

- Every user's resume, photo, and generated documents are private to them — enforced in app code
  (all Supabase access is server-side via the service_role key, filtered by the authenticated
  user's id) with Postgres row-level security as defense-in-depth.
- Static asset URLs are **content-fingerprinted** (`static_url()` in `app/main.py`, used by the
  templates). Starlette's `StaticFiles` sends an etag but no `Cache-Control`, so browsers fall
  back to heuristic freshness and can serve a stale `script.js`/`style.css` for a long time
  without revalidating. Since the HTML templates are never cached, a deploy could otherwise leave
  a browser running the previous release's JS against the new release's markup — which fails
  silently. Keep new assets going through `static_url()` rather than a hardcoded `/static/...`.
- The Supabase client is deliberately pinned to **HTTP/1.1** (`app/db.py`). `postgrest`/`storage3`
  default to `http2=True`, and httpcore's *sync* HTTP/2 transport multiplexes every request over
  one socket — but this client is used from several threads at once (the generation pipeline runs
  in a background thread while the browser polls `/jobs/{id}` through FastAPI's threadpool). That
  concurrent use of a single socket fails on Windows with `WinError 10035`. Don't "modernize" this
  back to HTTP/2 without switching the whole data layer to async.
- Photo/PDF storage paths are never reused on replace/regenerate — Supabase Storage sits behind a
  CDN that can keep serving a deleted/replaced object from a cached path, so every upload gets a
  fresh path (see `app/db.py`'s `save_photo`).
- If PDF generation fails with a LaTeX compile error, the error log tail is returned in the UI —
  it usually means a resume field has characters the escaping logic doesn't handle, or a resume
  field is empty in a way the template didn't expect.
