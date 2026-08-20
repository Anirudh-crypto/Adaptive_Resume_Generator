const signedOutNotice = document.getElementById("signed-out-notice");
const profileContent = document.getElementById("profile-content");

const photoPreview = document.getElementById("photo-preview");
const noPhotoNotice = document.getElementById("no-photo-notice");
const photoFileInput = document.getElementById("photo-file");
const uploadPhotoBtn = document.getElementById("upload-photo-btn");
const deletePhotoBtn = document.getElementById("delete-photo-btn");
const photoStatus = document.getElementById("photo-status");

const pdfFileInput = document.getElementById("pdf-file");
const importStatus = document.getElementById("import-status");
const resumeMdInput = document.getElementById("resume-md");
const saveResumeBtn = document.getElementById("save-resume-btn");
const resetVariantBtn = document.getElementById("reset-variant-btn");
const resumeStatus = document.getElementById("resume-status");
const resumeError = document.getElementById("resume-error");
const resumeRegionRadios = document.querySelectorAll('input[name="resume-region"]');
const fallbackNotice = document.getElementById("fallback-notice");

const DEFAULT_REGION = "germany";

// Which document the textarea currently holds, and whether it has been edited since it was
// loaded. Once there is more than one document, switching tabs with unsaved edits would silently
// throw them away, so the dirty flag guards that.
let currentRegion = DEFAULT_REGION;
let dirty = false;

initAuth((session) => {
  const signedIn = !!session;
  signedOutNotice.classList.toggle("hidden", signedIn);
  profileContent.classList.toggle("hidden", !signedIn);
  if (signedIn) {
    loadResume(currentRegion);
    loadPhoto();
  }
});

async function loadResume(region) {
  const resp = await fetchWithAuth(`/me/resume?region=${encodeURIComponent(region)}`);
  const data = await resp.json();
  resumeMdInput.value = data.markdown_text || "";
  currentRegion = region;
  dirty = false;

  // A region showing the canonical resume because it has none of its own gets the explanation and
  // no Reset button -- there is nothing to reset to yet.
  const isFallback = region !== DEFAULT_REGION && !data.is_variant;
  fallbackNotice.classList.toggle("hidden", !isFallback);
  resetVariantBtn.classList.toggle("hidden", region === DEFAULT_REGION || isFallback);
  resumeStatus.textContent = "";
  resumeError.classList.add("hidden");
}

resumeMdInput.addEventListener("input", () => {
  dirty = true;
});

resumeRegionRadios.forEach((radio) => {
  radio.addEventListener("change", () => {
    if (radio.value === currentRegion) return;
    if (dirty && !confirm("You have unsaved changes. Switch versions and discard them?")) {
      // Put the selection back where it was; the change event has already moved it.
      resumeRegionRadios.forEach((other) => {
        other.checked = other.value === currentRegion;
      });
      return;
    }
    loadResume(radio.value);
  });
});

async function loadPhoto() {
  const resp = await fetchWithAuth("/me/photo");
  if (resp.status === 404) {
    photoPreview.classList.add("hidden");
    noPhotoNotice.classList.remove("hidden");
    return;
  }
  const blob = await resp.blob();
  photoPreview.src = URL.createObjectURL(blob);
  photoPreview.classList.remove("hidden");
  noPhotoNotice.classList.add("hidden");
}

uploadPhotoBtn.addEventListener("click", async () => {
  const file = photoFileInput.files[0];
  if (!file) {
    photoStatus.textContent = "Choose an image file first.";
    return;
  }
  photoStatus.textContent = "Uploading...";
  const formData = new FormData();
  formData.append("file", file);
  const resp = await fetchWithAuth("/me/photo", { method: "POST", body: formData });
  const data = await resp.json();
  if (!resp.ok) {
    photoStatus.textContent = data.error || "Upload failed.";
    return;
  }
  photoStatus.textContent = "Photo saved.";
  loadPhoto();
});

deletePhotoBtn.addEventListener("click", async () => {
  photoStatus.textContent = "Removing...";
  await fetchWithAuth("/me/photo", { method: "DELETE" });
  photoStatus.textContent = "Photo removed.";
  loadPhoto();
});

pdfFileInput.addEventListener("change", async () => {
  const file = pdfFileInput.files[0];
  if (!file) return;

  importStatus.textContent = "Extracting... this can take 10-20 seconds.";
  pdfFileInput.disabled = true;

  try {
    const formData = new FormData();
    formData.append("file", file);
    const resp = await fetchWithAuth("/me/resume/import", { method: "POST", body: formData });
    const data = await resp.json();
    if (!resp.ok) {
      importStatus.textContent = data.error || "Import failed.";
      return;
    }
    resumeMdInput.value = data.markdown_text;
    // Extracted text is unsaved text: it lands in whichever version is open, and switching away
    // before saving must warn rather than silently drop it.
    dirty = true;
    if (data.photo_extracted) {
      await loadPhoto();
      importStatus.textContent = "Extracted below, including your photo — review, then click Save.";
    } else {
      importStatus.textContent = "Extracted below (no photo found in the PDF) — review, then click Save.";
    }
  } catch (err) {
    importStatus.textContent = `Import failed: ${err}`;
  } finally {
    pdfFileInput.disabled = false;
    pdfFileInput.value = "";
    // Importing draws on the same quota as generating. A successful import spent a slot; a failed
    // one refunded it server-side. Either way the badge needs the server's number, not a guess.
    refreshUsageBadge();
  }
});

saveResumeBtn.addEventListener("click", async () => {
  const region = currentRegion;
  resumeStatus.textContent = "Saving...";
  resumeError.classList.add("hidden");

  const resp = await fetchWithAuth(`/me/resume?region=${encodeURIComponent(region)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ markdown_text: resumeMdInput.value }),
  });
  const data = await resp.json();

  if (!resp.ok) {
    resumeStatus.textContent = "";
    resumeError.textContent = data.error || "Save failed.";
    resumeError.classList.remove("hidden");
    return;
  }
  dirty = false;
  // The first save of a non-default region turns a fallback view into a real document, so the
  // notice and the Reset button both have to change over.
  if (region !== DEFAULT_REGION) {
    fallbackNotice.classList.add("hidden");
    resetVariantBtn.classList.remove("hidden");
  }
  resumeStatus.textContent = "Saved.";
});

resetVariantBtn.addEventListener("click", async () => {
  const region = currentRegion;
  if (!confirm(`Delete your separate ${region} version and go back to your main resume?`)) return;

  resumeStatus.textContent = "Resetting...";
  resumeError.classList.add("hidden");

  const resp = await fetchWithAuth(`/me/resume?region=${encodeURIComponent(region)}`, {
    method: "DELETE",
  });
  if (!resp.ok) {
    const data = await resp.json();
    resumeStatus.textContent = "";
    resumeError.textContent = data.error || "Reset failed.";
    resumeError.classList.remove("hidden");
    return;
  }
  await loadResume(region);
  resumeStatus.textContent = "Reset to your main resume.";
});
