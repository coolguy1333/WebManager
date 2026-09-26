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
      button.setAttribute("aria-busy", "true");
    });
  });
});

// Buttons disabled by data-loading stay disabled if the page is restored from
// the back/forward cache; re-enable them so the form can be used again.
window.addEventListener("pageshow", () => {
  document.querySelectorAll("button.is-loading").forEach((button) => {
    button.disabled = false;
    button.classList.remove("is-loading");
    button.removeAttribute("aria-busy");
  });
});

document.querySelectorAll(".flash-close").forEach((button) => {
  button.addEventListener("click", () => {
    button.closest(".flash")?.remove();
  });
});
document.querySelectorAll(".flash.success").forEach((flash) => {
  window.setTimeout(() => flash.remove(), 8000);
});

const connectPanel = document.querySelector("details#connect");
if (connectPanel && window.location.hash === "#connect") {
  connectPanel.open = true;
  connectPanel.querySelector("input")?.focus();
}

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

const slugifyPreview = (value) => value
  .toLowerCase()
  .replace(/[^a-z0-9]+/g, "-")
  .replace(/^-+|-+$/g, "")
  .slice(0, 48) || "site";
const HOST_LABEL = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;

// Filter rows in a folder list.
document.querySelectorAll("[data-folder-filter]").forEach((input) => {
  const scope = input.closest("section") || document;
  input.addEventListener("input", () => {
    const query = input.value.trim().toLowerCase();
    let visible = 0;
    scope.querySelectorAll("[data-folder-text]").forEach((row) => {
      row.hidden = Boolean(query) && !row.dataset.folderText.toLowerCase().includes(query);
      visible += row.hidden ? 0 : 1;
    });
    const empty = scope.querySelector("[data-folder-empty]");
    if (empty) {
      empty.hidden = visible > 0;
    }
  });
});

// Settings page: single-choice folder list.
document.querySelectorAll("[data-folder-option] input[type=radio]").forEach((radio) => {
  radio.addEventListener("change", () => {
    document.querySelectorAll("[data-folder-option]").forEach((row) => {
      row.classList.toggle("selected", row.querySelector("input").checked);
    });
  });
});

// ---------- Deploy page ----------
const deployForm = document.querySelector("[data-deploy-form]");
if (deployForm) {
  const rows = [...deployForm.querySelectorAll("[data-deploy-option]")];
  const domainSelect = deployForm.querySelector("[data-hosting-domain]");
  const submit = deployForm.querySelector("[data-deploy-submit]");
  const label = deployForm.querySelector("[data-deploy-label]");
  const summary = deployForm.querySelector("[data-deploy-summary]");
  const detail = deployForm.querySelector("[data-deploy-detail]");
  const help = deployForm.querySelector("[data-hosting-help]");
  const scheme = deployForm.dataset.publicScheme || "https";

  rows.forEach((row) => {
    const name = row.querySelector("[data-site-name]");
    const slug = row.querySelector("[data-slug-input]");
    // The subdomain follows the site name until the user edits it directly.
    if (slug) {
      slug.dataset.auto = String(slug.value === slugifyPreview(name.value));
      slug.addEventListener("input", () => {
        slug.dataset.auto = String(slug.value === "");
        update();
      });
      slug.addEventListener("blur", () => {
        if (!slug.value.trim()) {
          slug.dataset.auto = "true";
        }
        update();
      });
    }
    name.addEventListener("input", update);
    row.querySelector('input[name="selected"]').addEventListener("change", (event) => {
      if (rootMode() && event.target.checked) {
        rows.forEach((other) => {
          if (other !== row) {
            other.querySelector('input[name="selected"]').checked = false;
          }
        });
      }
      update();
      if (event.target.checked) {
        name.focus();
        name.select();
      }
    });
  });

  function rootMode() {
    return deployForm.querySelector('input[name="hosting_mode"]:checked')?.value === "root";
  }

  function update() {
    const option = domainSelect?.selectedOptions[0];
    const domain = option?.dataset.domain || "";
    const root = rootMode();
    const rootAvailable = option?.dataset.rootAvailable !== "false";
    const selected = rows.filter((row) => row.querySelector('input[name="selected"]').checked);
    const seen = new Map();
    let invalid = false;

    if (root && selected.length > 1) {
      selected.slice(1).forEach((row) => { row.querySelector('input[name="selected"]').checked = false; });
      selected.length = 1;
    }

    rows.forEach((row) => {
      const isSelected = selected.includes(row);
      const name = row.querySelector("[data-site-name]");
      const slug = row.querySelector("[data-slug-input]");
      const error = row.querySelector("[data-slug-error]");
      const suffix = row.querySelector("[data-domain-suffix]");
      const host = row.querySelector("[data-row-host]");
      const url = row.querySelector("[data-row-url]");
      row.classList.toggle("selected", isSelected);
      row.classList.toggle("root-mode", root);
      if (suffix) {
        suffix.textContent = `.${domain}`;
      }
      if (!domain) {
        return;
      }
      if (slug && slug.dataset.auto === "true") {
        slug.value = slugifyPreview(name.value || "site");
      }
      const label = slug ? slug.value.trim().toLowerCase() : "";
      let message = "";
      if (isSelected && !root) {
        if (!HOST_LABEL.test(label)) {
          message = "Use lowercase letters, numbers, and dashes.";
        } else if (seen.has(label)) {
          message = "Another selected folder uses this subdomain.";
        }
        seen.set(label, row);
      }
      if (slug) {
        slug.classList.toggle("invalid", Boolean(message));
        slug.setCustomValidity(message);
        slug.disabled = !isSelected || root;
      }
      if (error) {
        error.textContent = message;
      }
      invalid = invalid || Boolean(message);
      const hostname = root ? domain : `${label || "…"}.${domain}`;
      if (host) {
        host.textContent = hostname;
      }
      if (url) {
        url.textContent = `${scheme}://${hostname}`;
      }
    });

    deployForm.querySelectorAll("[data-domain-alias]").forEach((input) => {
      const isPrimary = input.value === domainSelect?.value;
      input.disabled = isPrimary;
      if (isPrimary) {
        input.checked = false;
      }
      input.closest("label").hidden = isPrimary;
      const note = input.closest("label").querySelector("[data-alias-note]");
      if (note) {
        note.textContent = root ? `Served at ${input.dataset.domainName}` : "Same subdomain";
      }
    });

    if (help) {
      help.textContent = root
        ? rootAvailable
          ? `Publishes exactly one folder at ${domain}.`
          : `${domain} is already used by ${option.dataset.rootSite || "another site"}. Pick another domain or use subdomains.`
        : "Each site gets its own subdomain, which you can edit below.";
      help.classList.toggle("error-text", root && !rootAvailable);
    }
    deployForm.querySelectorAll("[data-select-all]").forEach((button) => { button.disabled = root; });

    const count = selected.length;
    if (label) {
      label.textContent = count === 1 ? "1 site" : `${count} sites`;
    }
    if (summary) {
      summary.textContent = count ? `${count} folder${count === 1 ? "" : "s"} selected` : "No folders selected";
    }
    if (detail) {
      const hosts = selected.map((row) => row.querySelector("[data-row-host]")?.textContent).filter(Boolean);
      detail.textContent = hosts.length ? hosts.join(" · ") : count ? "Internal ports assigned automatically." : "Tick at least one folder above.";
    }
    const maxSites = deployForm.dataset.maxSites === undefined ? Infinity : Number(deployForm.dataset.maxSites);
    const overLimit = count > maxSites;
    if (overLimit && detail) {
      detail.textContent = `Your limit allows ${maxSites} more site${maxSites === 1 ? "" : "s"}. Untick ${count - maxSites}.`;
      detail.classList.add("error-text");
    } else {
      detail?.classList.remove("error-text");
    }
    submit.disabled = count === 0 || invalid || overLimit || (root && !rootAvailable);
  }

  deployForm.querySelector("[data-select-all]")?.addEventListener("click", () => {
    rows.filter((row) => !row.hidden).forEach((row) => { row.querySelector('input[name="selected"]').checked = true; });
    update();
  });
  deployForm.querySelector("[data-select-none]")?.addEventListener("click", () => {
    rows.forEach((row) => { row.querySelector('input[name="selected"]').checked = false; });
    update();
  });
  domainSelect?.addEventListener("change", update);
  deployForm.querySelectorAll('input[name="hosting_mode"]').forEach((input) => input.addEventListener("change", update));
  update();
}

// ---------- Settings page: main address ----------
const settingsHosting = document.querySelector("[data-settings-hosting-preview]");
if (settingsHosting) {
  const domainSelect = settingsHosting.querySelector("[data-hosting-domain]");
  const slug = settingsHosting.querySelector("[data-settings-slug]");
  const rootToggle = settingsHosting.querySelector("[data-root-toggle]");
  const url = settingsHosting.querySelector("[data-settings-hosting-url]");
  const help = settingsHosting.querySelector("[data-settings-hosting-help]");
  const row = settingsHosting.querySelector(".hostname-row");
  const submit = document.querySelector('.settings-form button[type="submit"]');
  const scheme = settingsHosting.dataset.publicScheme || "https";
  const updateSettingsHosting = () => {
    const option = domainSelect.selectedOptions[0];
    const domain = option?.dataset.domain || "";
    const root = rootToggle.checked;
    const rootAvailable = option?.dataset.rootAvailable !== "false";
    const label = slugifyPreview(slug.value || "site");
    row.classList.toggle("root-mode", root && Boolean(domain));
    rootToggle.disabled = !domain;
    let blocked = false;
    if (!domain) {
      url.textContent = "Internal port only";
      help.textContent = "Choose a domain to give this site a public address.";
    } else if (root) {
      url.textContent = `${scheme}://${domain}`;
      blocked = !rootAvailable;
      help.textContent = blocked
        ? `${domain} is already used by ${option.dataset.rootSite || "another site"}.`
        : "This site will own the bare domain. The subdomain is still used for alternate addresses.";
    } else {
      url.textContent = `${scheme}://${label}.${domain}`;
      help.textContent = slug.value && slug.value.toLowerCase() !== label ? `Saved as “${label}”.` : "";
    }
    help.classList.toggle("error-text", blocked);
    if (submit) {
      submit.disabled = blocked;
    }
  };
  [domainSelect, rootToggle].forEach((el) => el.addEventListener("change", updateSettingsHosting));
  slug.addEventListener("input", updateSettingsHosting);
  updateSettingsHosting();
}

// Generic list filtering: a search box plus optional tag radios per group.
const runFilter = (group) => {
  const input = document.querySelector(`[data-filter-input="${group}"]`);
  const query = (input?.value || "").trim().toLowerCase();
  const tag = document.querySelector(`input[data-filter-kind="${group}"]:checked`)?.value || "";
  let visible = 0;
  document.querySelectorAll(`[data-filter-item="${group}"]`).forEach((item) => {
    const text = (item.dataset.filterText || item.textContent).toLowerCase();
    const tags = (item.dataset.filterTags || "").split(" ");
    item.hidden = (Boolean(query) && !text.includes(query)) || (Boolean(tag) && !tags.includes(tag));
    visible += item.hidden ? 0 : 1;
  });
  const empty = document.querySelector(`[data-filter-empty="${group}"]`);
  if (empty) {
    empty.hidden = visible > 0;
  }
};
document.querySelectorAll("[data-filter-input]").forEach((input) => {
  input.addEventListener("input", () => runFilter(input.dataset.filterInput));
});
document.querySelectorAll("input[data-filter-kind]").forEach((input) => {
  input.addEventListener("change", () => runFilter(input.dataset.filterKind));
});

document.querySelectorAll("[data-permission-profile]").forEach((select) => {
  const form = select.closest("form");
  const customPermissions = form?.querySelector("[data-custom-permissions]");
  if (!customPermissions) {
    return;
  }
  const updateCustomPermissions = () => {
    customPermissions.hidden = select.value !== "custom";
  };
  select.addEventListener("change", updateCustomPermissions);
  updateCustomPermissions();
});

const sidebar = document.querySelector("[data-sidebar]");
const sidebarToggle = document.querySelector("[data-sidebar-toggle]");
const setSidebar = (open) => {
  sidebar?.classList.toggle("open", open);
  sidebarToggle?.setAttribute("aria-expanded", String(open));
  if (open) {
    sidebar?.querySelector("a, button")?.focus();
  }
};
sidebarToggle?.addEventListener("click", () => setSidebar(!sidebar?.classList.contains("open")));
document.querySelector("[data-sidebar-close]")?.addEventListener("click", () => setSidebar(false));
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && sidebar?.classList.contains("open")) {
    setSidebar(false);
    sidebarToggle?.focus();
  }
});

const aliasRows = document.querySelectorAll("[data-alias-row]");
const aliasPrimaryDomain = document.querySelector(".settings-form [data-hosting-domain]");
const aliasSiteSlug = document.querySelector('.settings-form input[name="slug"]');
const updateAliasRows = () => {
  const primaryId = aliasPrimaryDomain?.value || "";
  const slug = slugifyPreview(aliasSiteSlug?.value || "site");
  aliasRows.forEach((row) => {
    const enabled = row.querySelector("[data-alias-enabled]");
    const mode = row.querySelector("[data-alias-mode]");
    const prefix = row.querySelector("[data-alias-prefix]");
    const prefixWrap = row.querySelector("[data-alias-prefix-wrap]");
    const result = row.querySelector("[data-alias-result]");
    const summary = row.querySelector("[data-alias-summary]");
    const domain = row.querySelector(".alias-enable strong")?.textContent.trim() || "";
    const isPrimary = row.dataset.domainId === primaryId;
    row.classList.toggle("is-primary", isPrimary);

    if (isPrimary) {
      enabled.checked = false;
      enabled.disabled = true;
      mode.disabled = true;
      prefix.disabled = true;
      prefixWrap.hidden = true;
      result.textContent = "Main domain";
      summary.textContent = "Selected as the main domain";
      return;
    }

    enabled.disabled = false;
    const active = enabled.checked;
    mode.disabled = !active;
    const customSubdomain = mode.value === "subdomain";
    prefixWrap.hidden = !customSubdomain;
    prefix.disabled = !active || !customSubdomain;
    prefix.required = active && customSubdomain;
    row.classList.toggle("enabled", active);

    if (!active) {
      result.textContent = "Not connected";
      summary.textContent = "Not connected";
    } else if (mode.value === "root") {
      result.textContent = domain;
      summary.textContent = "Hosted at domain root";
    } else if (customSubdomain) {
      result.textContent = `${slugifyPreview(prefix.value || "subdomain")}.${domain}`;
      summary.textContent = "Custom subdomain";
    } else {
      result.textContent = `${slug}.${domain}`;
      summary.textContent = "Alternate address";
    }
  });
};
aliasRows.forEach((row) => {
  row.querySelector("[data-alias-enabled]")?.addEventListener("change", updateAliasRows);
  row.querySelector("[data-alias-mode]")?.addEventListener("change", updateAliasRows);
  row.querySelector("[data-alias-prefix]")?.addEventListener("input", updateAliasRows);
});
aliasPrimaryDomain?.addEventListener("change", updateAliasRows);
aliasSiteSlug?.addEventListener("input", updateAliasRows);
updateAliasRows();

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

  // Tab indents; pressing Escape first lets Tab move focus out (keyboard users
  // are never trapped). execCommand keeps the browser's undo history intact.
  let releaseTab = false;
  editor.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      releaseTab = true;
      return;
    }
    if (event.key === "Tab" && !event.shiftKey && !releaseTab) {
      event.preventDefault();
      if (!document.execCommand("insertText", false, "    ")) {
        const start = editor.selectionStart;
        editor.value = `${editor.value.slice(0, start)}    ${editor.value.slice(editor.selectionEnd)}`;
        editor.selectionStart = editor.selectionEnd = start + 4;
      }
      updateCounter();
      return;
    }
    releaseTab = false;
  });
}

// Sources: only show the auto-update interval when "Install automatically" is chosen.
document.querySelectorAll("[data-update-form]").forEach((form) => {
  const interval = form.querySelector("[data-auto-interval]");
  const sync = () => {
    const auto = form.querySelector('input[name="update_mode"]:checked')?.value === "auto";
    interval?.classList.toggle("is-hidden", !auto);
    interval?.querySelectorAll("input, select").forEach((input) => { input.disabled = !auto; });
  };
  form.querySelectorAll('input[name="update_mode"]').forEach((input) => input.addEventListener("change", sync));
  sync();
});

// Filter forms that apply as soon as a choice changes (analytics site/period).
document.querySelectorAll("form[data-autosubmit]").forEach((form) => {
  form.addEventListener("change", () => form.requestSubmit());
});

// ---------- Sites: search, status filter, and grouping ----------
const siteToolbar = document.querySelector("[data-site-toolbar]");
if (siteToolbar) {
  const list = document.querySelector(".sites-list");
  const rows = [...list.querySelectorAll("[data-site-row]")];
  const search = siteToolbar.querySelector("[data-site-search]");
  const groupSelect = siteToolbar.querySelector("[data-site-group]");
  const empty = list.querySelector("[data-site-empty]");
  const storageKey = "webmanager.sites.groupBy";
  try {
    const saved = window.localStorage.getItem(storageKey);
    if (saved !== null && groupSelect.querySelector(`option[value="${CSS.escape(saved)}"]`)) {
      groupSelect.value = saved;
    }
  } catch { /* storage unavailable */ }

  const apply = () => {
    const query = search.value.trim().toLowerCase();
    const terms = query.split(/\s+/).filter(Boolean);
    const status = siteToolbar.querySelector('input[name="site_status"]:checked')?.value || "all";
    const group = groupSelect.value;
    list.querySelectorAll(".list-group-head").forEach((head) => head.remove());

    const visible = rows.filter((row) => {
      const matches = terms.every((term) => row.dataset.search.includes(term))
        && (status === "all" || row.dataset.status === status);
      row.hidden = !matches;
      return matches;
    });

    let ordered = rows;
    if (group) {
      const key = `group${group[0].toUpperCase()}${group.slice(1)}`;
      ordered = [...rows].sort((a, b) => a.dataset[key].localeCompare(b.dataset[key], undefined, { sensitivity: "base" }));
      const counts = new Map();
      visible.forEach((row) => counts.set(row.dataset[key], (counts.get(row.dataset[key]) || 0) + 1));
      let current = null;
      ordered.forEach((row) => {
        list.insertBefore(row, empty);
        if (!row.hidden && row.dataset[key] !== current) {
          current = row.dataset[key];
          const head = document.createElement("div");
          head.className = "list-group-head";
          head.setAttribute("role", "row");
          const name = document.createElement("span");
          name.textContent = current;
          const badge = document.createElement("span");
          badge.className = "badge";
          badge.textContent = counts.get(current);
          head.append(name, badge);
          list.insertBefore(head, row);
        }
      });
    } else {
      ordered.forEach((row) => list.insertBefore(row, empty));
    }
    empty.hidden = visible.length > 0;
    try { window.localStorage.setItem(storageKey, group); } catch { /* ignore */ }
  };

  search.addEventListener("input", apply);
  groupSelect.addEventListener("change", apply);
  siteToolbar.querySelectorAll('input[name="site_status"]').forEach((input) => input.addEventListener("change", apply));
  list.querySelector("[data-site-reset]")?.addEventListener("click", () => {
    search.value = "";
    siteToolbar.querySelector('input[name="site_status"][value="all"]').checked = true;
    apply();
  });
  // The "Needs attention" stat jumps straight to the filtered list.
  document.querySelector(".stat.danger")?.addEventListener("click", () => {
    siteToolbar.querySelector('input[name="site_status"][value="attention"]').checked = true;
    apply();
    list.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  apply();
}

// ---------- System: live resource usage ----------
const metricsPanel = document.querySelector("[data-metrics]");
if (metricsPanel) {
  const formatBytes = (value) => {
    if (value === null || value === undefined) return "n/a";
    const units = [["TB", 1099511627776], ["GB", 1073741824], ["MB", 1048576]];
    for (const [unit, size] of units) {
      if (value >= size) return `${(value / size).toFixed(1)} ${unit}`;
    }
    return `${Math.round(value / 1024)} KB`;
  };
  const set = (name, value) => {
    const el = metricsPanel.querySelector(`[data-metric="${name}"]`);
    if (el && value !== null && value !== undefined) el.textContent = value;
  };
  const setMeter = (resource, percent) => {
    const fill = metricsPanel.querySelector(`[data-resource="${resource}"] [data-meter-fill]`);
    if (!fill || percent === null || percent === undefined) return;
    fill.setAttribute("width", String(Math.min(100, percent)));
    fill.classList.toggle("warm", percent >= 75 && percent < 90);
    fill.classList.toggle("hot", percent >= 90);
  };
  const live = metricsPanel.querySelector("[data-metrics-live]");
  const refresh = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(metricsPanel.dataset.metricsUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      const m = await response.json();
      set("cpu_percent", m.cpu_percent);
      setMeter("cpu", m.cpu_percent);
      if (m.load) set("load", m.load.join(" / "));
      if (m.memory) {
        set("memory_percent", m.memory.percent);
        set("memory_used", formatBytes(m.memory.used));
        setMeter("memory", m.memory.percent);
      }
      if (m.disk) {
        set("disk_percent", m.disk.percent);
        set("disk_free", formatBytes(m.disk.free));
        setMeter("disk", m.disk.percent);
      }
      set("process_memory", formatBytes(m.process_memory));
      if (m.network) {
        set("net_rx", formatBytes(m.network.received));
        set("net_tx", formatBytes(m.network.sent));
      }
      if (m.uptime) {
        set("uptime", m.uptime >= 86400
          ? `${Math.floor(m.uptime / 86400)}d ${Math.floor((m.uptime % 86400) / 3600)}h`
          : `${Math.floor(m.uptime / 3600)}h ${Math.floor((m.uptime % 3600) / 60)}m`);
      }
      live?.classList.remove("stale");
    } catch {
      live?.classList.add("stale");
    }
  };
  refresh();
  window.setInterval(refresh, 2000);
}

// ---------- Apps: live container usage (list views) ----------
const appsLive = document.querySelector("[data-apps-live]");
if (appsLive) {
  const refreshAppsList = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(appsLive.dataset.appsStatsUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      const stats = await response.json();
      appsLive.querySelectorAll("[data-app-row]").forEach((row) => {
        const stat = stats[row.dataset.siteId];
        const cpu = row.querySelector('[data-stat="cpu"]');
        const memory = row.querySelector('[data-stat="memory"]');
        const net = row.querySelector('[data-stat="net"]');
        if (cpu) cpu.textContent = stat && stat.cpu_percent !== null && stat.cpu_percent !== undefined ? `${stat.cpu_percent}%` : "—";
        if (memory) memory.textContent = stat ? `${stat.memory_used} / ${stat.memory_limit}` : "—";
        if (net) net.textContent = stat ? `↓ ${stat.net_rx} ↑ ${stat.net_tx}` : "—";
      });
    } catch {
      // Leave the last known values on screen; try again next tick.
    }
  };
  window.setInterval(refreshAppsList, 2000);
}

// ---------- App page: live container status & usage ----------
const appStatus = document.querySelector("[data-app-status]");
if (appStatus) {
  const refreshAppStatus = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(appStatus.dataset.appStatusUrl, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      const set = (name, value) => {
        const el = appStatus.querySelector(`[data-app-stat="${name}"]`);
        if (el) el.textContent = value;
      };
      if (data.status) set("status", data.status.charAt(0).toUpperCase() + data.status.slice(1));
      set("restarts", data.restarts !== null && data.restarts !== undefined ? data.restarts : "—");
      const stat = data.stats;
      set("cpu", stat && stat.cpu_percent !== null && stat.cpu_percent !== undefined ? `${stat.cpu_percent}%` : "—");
      set("memory", stat ? `${stat.memory_used} / ${stat.memory_limit}` : "—");
      set("net", stat ? `↓ ${stat.net_rx} ↑ ${stat.net_tx}` : "—");
      const hint = appStatus.querySelector('[data-app-stat="hint"]');
      if (hint) hint.textContent = stat ? "CPU, memory and network refresh every 2 seconds while this page is open." : "";
    } catch {
      // Leave the last known values on screen; try again next tick.
    }
  };
  window.setInterval(refreshAppStatus, 2000);
}

// Reload while an app is building/starting so the page reflects the result.
const autoRefresh = document.querySelector("[data-auto-refresh]");
if (autoRefresh) {
  window.setTimeout(() => window.location.reload(), Number(autoRefresh.dataset.autoRefresh || 5) * 1000);
}
