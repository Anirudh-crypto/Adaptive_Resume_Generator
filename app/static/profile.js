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
const resumeStatus = document.getElementById("resume-status");
const resumeError = document.getElementById("resume-error");

initAuth((session) => {
  const signedIn = !!session;
  signedOutNotice.classList.toggle("hidden", signedIn);
  profileContent.classList.toggle("hidden", !signedIn);
  if (signedIn) {
    loadResume();
    loadPhoto();
  }
});

async function loadResume() {
  const resp = await fetchWithAuth("/me/resume");
  const data = await resp.json();
  resumeMdInput.value = data.markdown_text || "";
}

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
  resumeStatus.textContent = "Saving...";
  resumeError.classList.add("hidden");

  const resp = await fetchWithAuth("/me/resume", {
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
  resumeStatus.textContent = "Saved.";
});
