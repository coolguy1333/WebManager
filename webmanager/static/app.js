document.querySelectorAll("form[data-confirm]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (!window.confirm(form.dataset.confirm)) {
      event.preventDefault();
    }
  });
});

document.querySelectorAll("[data-confirm-button]").forEach((button) => {
  button.addEventListener("click", (event) => {
    if (!window.confirm(button.dataset.confirmButton)) {
      event.preventDefault();
    }
  });
});

document.querySelectorAll("form[data-loading]").forEach((form) => {
  form.addEventListener("submit", (event) => {
    if (event.defaultPrevented) {
      return;
    }

    const buttons = form.querySelectorAll('button[type="submit"]');
    buttons.forEach((button) => {
      button.disabled = true;
      button.classList.add("is-loading");
      if (button.dataset.loadingText) {
        button.dataset.originalText = button.textContent.trim();
        button.textContent = button.dataset.loadingText;
      }
    });
  });
});

document.querySelectorAll(".flash-close").forEach((button) => {
  button.addEventListener("click", () => {
    button.closest(".flash")?.remove();
  });
});

document.querySelector("[data-history-back]")?.addEventListener("click", () => {
  window.history.back();
});

document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const original = button.textContent;
    try {
      await navigator.clipboard.writeText(button.dataset.copy);
      button.textContent = "Copied";
    } catch {
      button.textContent = "Copy failed";
    }
    window.setTimeout(() => {
      button.textContent = original;
    }, 1600);
  });
});

const selectedFolder = document.querySelector("[data-selected-folder]");
document.querySelectorAll('input[name="folder"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    if (selectedFolder && radio.checked) {
      selectedFolder.textContent = radio.value;
    }
  });
});

const deployOptions = document.querySelectorAll("[data-deploy-option]");
const selectedCount = document.querySelector("[data-selected-count]");
const updateDeploySelection = () => {
  let count = 0;
  deployOptions.forEach((option) => {
    const checkbox = option.querySelector('input[name="selected"]');
    const selected = Boolean(checkbox?.checked);
    option.classList.toggle("selected", selected);
    if (selected) {
      count += 1;
    }
  });
  if (selectedCount) {
    selectedCount.textContent = count;
  }
};
deployOptions.forEach((option) => {
  option.querySelector('input[name="selected"]')?.addEventListener("change", updateDeploySelection);
});
updateDeploySelection();

document.querySelectorAll("[data-filter-input]").forEach((input) => {
  input.addEventListener("input", () => {
    const group = input.dataset.filterInput;
    const query = input.value.trim().toLowerCase();
    document.querySelectorAll(`[data-filter-item="${group}"]`).forEach((item) => {
      const text = (item.dataset.filterText || item.textContent).toLowerCase();
      item.hidden = Boolean(query) && !text.includes(query);
    });
  });
});

const editor = document.querySelector(".code-editor");
if (editor) {
  const form = editor.closest("[data-editor-form]");
  const counter = document.querySelector("[data-character-count]");
  const initialValue = editor.value;
  let submitted = false;

  const updateCounter = () => {
    if (counter) {
      counter.textContent = editor.value.length.toLocaleString();
    }
  };

  editor.addEventListener("input", updateCounter);
  form?.addEventListener("submit", () => {
    submitted = true;
  });
  window.addEventListener("beforeunload", (event) => {
    if (!submitted && editor.value !== initialValue) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  editor.addEventListener("keydown", (event) => {
    if (event.key === "Tab") {
      event.preventDefault();
      const start = editor.selectionStart;
      editor.value = `${editor.value.slice(0, start)}    ${editor.value.slice(editor.selectionEnd)}`;
      editor.selectionStart = editor.selectionEnd = start + 4;
      updateCounter();
    }
  });
}
