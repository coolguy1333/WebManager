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
const hostingPicker = document.querySelector("[data-hosting-picker]");
const hostingDomain = document.querySelector("[data-hosting-domain]");
const hostingModes = document.querySelectorAll('input[name="hosting_mode"]');
const deploySubmit = document.querySelector("[data-deploy-submit]");
const hostingUrl = document.querySelector("[data-hosting-url]");
const hostingHelp = document.querySelector("[data-hosting-help]");
const subdomainPreview = document.querySelector("[data-subdomain-preview]");
const rootPreview = document.querySelector("[data-root-preview]");
const slugifyPreview = (value) => value
  .toLowerCase()
  .replace(/[^a-z0-9]+/g, "-")
  .replace(/^-+|-+$/g, "")
  .slice(0, 48) || "site";

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

const updateHostingChoice = () => {
  if (!hostingPicker || !hostingDomain) {
    return;
  }
  const selectedMode = document.querySelector('input[name="hosting_mode"]:checked')?.value || "subdomain";
  const rootMode = selectedMode === "root";
  const option = hostingDomain.selectedOptions[0];
  const domain = option?.dataset.domain || option?.textContent.trim() || "";
  const rootAvailable = option?.dataset.rootAvailable !== "false";
  const rootSite = option?.dataset.rootSite || "another site";
  const scheme = hostingPicker.dataset.publicScheme || "https";
  let selectedCards = [...deployOptions].filter(
    (card) => card.querySelector('input[name="selected"]')?.checked,
  );

  if (rootMode && selectedCards.length === 0 && deployOptions.length) {
    const firstCheckbox = deployOptions[0].querySelector('input[name="selected"]');
    if (firstCheckbox) {
      firstCheckbox.checked = true;
      selectedCards = [deployOptions[0]];
    }
  }
  if (rootMode && selectedCards.length > 1) {
    selectedCards.slice(1).forEach((card) => {
      const checkbox = card.querySelector('input[name="selected"]');
      if (checkbox) {
        checkbox.checked = false;
      }
    });
  }
  deployOptions.forEach((card) => {
    const checkbox = card.querySelector('input[name="selected"]');
    if (checkbox) {
      checkbox.disabled = rootMode && !checkbox.checked;
    }
  });
  updateDeploySelection();

  const activeCard = [...deployOptions].find(
    (card) => card.querySelector('input[name="selected"]')?.checked,
  );
  const siteName = activeCard?.querySelector('input[name^="site_name_"]')?.value || "site";
  const slug = slugifyPreview(siteName);
  const previewHost = rootMode ? domain : `${slug}.${domain}`;
  if (subdomainPreview) {
    subdomainPreview.textContent = `${slug}.${domain}`;
  }
  if (rootPreview) {
    rootPreview.textContent = domain;
  }
  if (hostingUrl) {
    hostingUrl.textContent = domain ? `${scheme}://${previewHost}` : "Choose a domain";
  }
  if (hostingHelp) {
    hostingHelp.textContent = rootMode
      ? rootAvailable
        ? "Root hosting is available. Only the selected folder will be deployed."
        : `Root hosting is already used by ${rootSite}. Choose another domain or use a subdomain.`
      : "Each selected folder receives its own subdomain.";
    hostingHelp.classList.toggle("error-text", rootMode && !rootAvailable);
  }
  if (deploySubmit) {
    deploySubmit.disabled = rootMode && !rootAvailable;
  }
};

deployOptions.forEach((option) => {
  option.querySelector('input[name="selected"]')?.addEventListener("change", () => {
    updateDeploySelection();
    updateHostingChoice();
  });
  option.querySelector('input[name^="site_name_"]')?.addEventListener("input", updateHostingChoice);
});
hostingDomain?.addEventListener("change", updateHostingChoice);
hostingModes.forEach((mode) => mode.addEventListener("change", updateHostingChoice));
updateDeploySelection();
updateHostingChoice();

const settingsHostingPreview = document.querySelector("[data-settings-hosting-preview]");
const settingsHostingUrl = document.querySelector("[data-settings-hosting-url]");
const settingsHostingHelp = document.querySelector("[data-settings-hosting-help]");
const settingsDomain = document.querySelector(".settings-form [data-hosting-domain]");
const settingsSlug = document.querySelector('.settings-form input[name="slug"]');
const settingsSubmit = document.querySelector('.settings-form button[type="submit"]');
const updateSettingsHostingChoice = () => {
  if (!settingsHostingPreview || !settingsDomain) {
    return;
  }
  const mode = document.querySelector('.settings-form input[name="hosting_mode"]:checked')?.value || "subdomain";
  const option = settingsDomain.selectedOptions[0];
  const domain = option?.dataset.domain || "";
  const rootMode = mode === "root";
  const rootAvailable = option?.dataset.rootAvailable !== "false";
  const scheme = settingsHostingPreview.dataset.publicScheme || "https";
  const slug = slugifyPreview(settingsSlug?.value || "site");
  if (!domain) {
    settingsHostingUrl.textContent = "Direct port only";
    settingsHostingHelp.textContent = "Choose a public domain to use root or subdomain hosting.";
    settingsSubmit.disabled = rootMode;
    return;
  }
  settingsHostingUrl.textContent = `${scheme}://${rootMode ? domain : `${slug}.${domain}`}`;
  settingsHostingHelp.textContent = rootMode
    ? rootAvailable
      ? "This site will own the domain root."
      : `The domain root is already used by ${option.dataset.rootSite || "another site"}.`
    : "The URL slug is used as the subdomain.";
  settingsHostingHelp.classList.toggle("error-text", rootMode && !rootAvailable);
  settingsSubmit.disabled = rootMode && !rootAvailable;
};
settingsDomain?.addEventListener("change", updateSettingsHostingChoice);
settingsSlug?.addEventListener("input", updateSettingsHostingChoice);
document.querySelectorAll('.settings-form input[name="hosting_mode"]').forEach(
  (mode) => mode.addEventListener("change", updateSettingsHostingChoice),
);
updateSettingsHostingChoice();

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
