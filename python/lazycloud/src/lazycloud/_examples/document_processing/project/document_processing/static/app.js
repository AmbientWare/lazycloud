const form = document.querySelector("#upload-form");
const fileInput = document.querySelector("#document");
const statusLine = document.querySelector("#status");
const result = document.querySelector("#result");
const deleteButton = document.querySelector("#delete");

let jobToken = null;

function jobHeaders() {
  return { "X-Job-Token": jobToken };
}

async function poll() {
  const response = await fetch("/api/jobs/status", { headers: jobHeaders() });
  if (!response.ok) throw new Error("Could not read job status");
  const state = await response.json();
  statusLine.textContent = `Status: ${state.status}`;
  if (state.ready) {
    const resultResponse = await fetch("/api/jobs/result", { headers: jobHeaders() });
    if (!resultResponse.ok) throw new Error("Could not read OCR result");
    const value = await resultResponse.json();
    result.textContent = value.text || "No text was detected.";
    result.hidden = false;
    deleteButton.hidden = false;
    return;
  }
  if (state.error) throw new Error(state.error);
  window.setTimeout(() => poll().catch(showError), 1000);
}

function showError(error) {
  statusLine.textContent = error instanceof Error ? error.message : "Request failed";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) return;
  statusLine.textContent = "Uploading…";
  result.hidden = true;
  deleteButton.hidden = true;
  try {
    const response = await fetch(`/api/documents/${encodeURIComponent(file.name)}`, {
      method: "PUT",
      headers: { "Content-Type": file.type },
      body: file,
    });
    if (!response.ok) throw new Error(await response.text());
    const accepted = await response.json();
    jobToken = accepted.job_token;
    await poll();
  } catch (error) {
    showError(error);
  }
});

deleteButton.addEventListener("click", async () => {
  try {
    const response = await fetch("/api/jobs", { method: "DELETE", headers: jobHeaders() });
    if (!response.ok) throw new Error("Could not delete result");
    jobToken = null;
    result.textContent = "";
    result.hidden = true;
    deleteButton.hidden = true;
    statusLine.textContent = "Result deleted.";
  } catch (error) {
    showError(error);
  }
});
