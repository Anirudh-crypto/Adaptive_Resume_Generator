const jdInput = document.getElementById("jd");
const generateBtn = document.getElementById("generate-btn");
const progressEl = document.getElementById("progress");
const progressFill = document.getElementById("progress-fill");
const progressStage = document.getElementById("progress-stage");
const progressPercent = document.getElementById("progress-percent");
const errorBox = document.getElementById("error-box");
const resultEl = document.getElementById("result");
const downloadLink = document.getElementById("download-link");
const coverLetterLink = document.getElementById("cover-letter-link");
const coverLetterWarning = document.getElementById("cover-letter-warning");
const pdfPreview = document.getElementById("pdf-preview");
const signedOutNotice = document.getElementById("signed-out-notice");
const regionGroup = document.getElementById("region-group");

let pollTimer = null;
let isSignedIn = false;

initAuth((session) => {
  isSignedIn = !!session;
  generateBtn.disabled = !isSignedIn;
  signedOutNotice.classList.toggle("hidden", isSignedIn);
});

generateBtn.addEventListener("click", async () => {
  if (!isSignedIn) {
    showError("Please sign in first.");
    return;
  }

  const jobDescription = jdInput.value.trim();
  if (!jobDescription) {
    showError("Please paste a job description first.");
    return;
  }

  const region = regionGroup.querySelector('input[name="region"]:checked').value;

  startGenerating();

  try {
    const resp = await fetchWithAuth("/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_description: jobDescription, region }),
    });
    const data = await resp.json();

    if (!resp.ok) {
      stopGenerating();
      showError(data.error || "Something went wrong");
      // Most likely a 429 — resync the badge so it agrees with what the server just enforced.
      refreshUsageBadge();
      return;
    }

    // The slot was claimed by that request; reflect it immediately rather than at job end.
    refreshUsageBadge();

    pollJob(data.job_id);
  } catch (err) {
    stopGenerating();
    showError(`Request failed: ${err}`);
  }
});

// The job runs server-side regardless of whether a poll succeeds, so a blip on one poll
// shouldn't tear down the UI — only give up after several consecutive failures.
const MAX_CONSECUTIVE_POLL_FAILURES = 8;

function pollJob(jobId) {
  let consecutiveFailures = 0;

  const giveUp = (message) => {
    clearInterval(pollTimer);
    stopGenerating();
    showError(message);
  };

  const tolerate = (message) => {
    consecutiveFailures += 1;
    if (consecutiveFailures >= MAX_CONSECUTIVE_POLL_FAILURES) {
      giveUp(`${message} Your documents may still have been generated — reload to check.`);
    }
  };

  pollTimer = setInterval(async () => {
    try {
      const resp = await fetchWithAuth(`/jobs/${jobId}`);

      // A 5xx is usually a transient hiccup talking to the database; a 4xx (unknown job,
      // expired session) will not fix itself on the next tick.
      if (resp.status >= 500) {
        tolerate("Lost connection while checking progress.");
        return;
      }

      const job = await resp.json();

      if (!resp.ok) {
        giveUp(job.detail || "Lost track of the generation job.");
        return;
      }

      consecutiveFailures = 0;
      setProgress(job.percent ?? 0, job.stage ?? "");

      if (job.status === "done") {
        clearInterval(pollTimer);
        setProgress(100, "Done");
        downloadLink.href = job.pdf_url;
        pdfPreview.src = job.pdf_url;

        const hasCoverLetter = !!job.cover_letter_url;
        coverLetterLink.classList.toggle("hidden", !hasCoverLetter);
        if (hasCoverLetter) {
          coverLetterLink.href = job.cover_letter_url;
        }
        coverLetterWarning.classList.toggle("hidden", !job.cover_letter_error);
        coverLetterWarning.textContent = job.cover_letter_error || "";

        resultEl.classList.remove("hidden");
        stopGenerating();
      } else if (job.status === "error") {
        clearInterval(pollTimer);
        stopGenerating();
        const detail = job.error_detail ? `\n\n${job.error_detail}` : "";
        showError(`${job.error || "Generation failed"}${detail}`);
        // A failed job refunds its slot server-side, so the count goes back UP here. This has to
        // re-read rather than assume — that refund is the whole point of the change.
        refreshUsageBadge();
      }
    } catch (err) {
      tolerate(`Lost connection while checking progress: ${err}.`);
    }
  }, 600);
}

function setRegionDisabled(disabled) {
  regionGroup
    .querySelectorAll('input[name="region"]')
    .forEach((radio) => (radio.disabled = disabled));
}

function startGenerating() {
  generateBtn.disabled = true;
  jdInput.disabled = true;
  setRegionDisabled(true);
  errorBox.classList.add("hidden");
  resultEl.classList.add("hidden");
  progressEl.classList.remove("hidden");
  setProgress(0, "Starting...");
}

function stopGenerating() {
  generateBtn.disabled = !isSignedIn ? true : false;
  jdInput.disabled = false;
  setRegionDisabled(false);
}

function setProgress(percent, stage) {
  progressFill.style.width = `${percent}%`;
  progressPercent.textContent = `${percent}%`;
  progressStage.textContent = stage;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.classList.remove("hidden");
  progressEl.classList.add("hidden");
}
