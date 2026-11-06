const bridge = window.AstrBotPluginView;
const $ = (selector) => document.querySelector(selector);

let currentDate = "";
let configSchema = {};

function showMessage(element, message, isError = false) {
  element.textContent = message;
  element.classList.toggle("error", isError);
}

async function apiGet(endpoint, params) {
  const result = await bridge.apiGet(endpoint, params);
  if (result?.status === "error") throw new Error(result.message || "请求失败");
  return result;
}

async function apiPost(endpoint, body) {
  const result = await bridge.apiPost(endpoint, body);
  if (result?.status === "error") throw new Error(result.message || "请求失败");
  return result;
}

function makeStatusCard(title, value, detail = "") {
  const card = document.createElement("article");
  card.className = "card status-card";
  const heading = document.createElement("span");
  heading.className = "status-title";
  heading.textContent = title;
  const metric = document.createElement("strong");
  metric.className = "status-value";
  metric.textContent = value;
  card.append(heading, metric);
  if (detail) {
    const note = document.createElement("span");
    note.className = "status-detail";
    note.textContent = detail;
    card.append(note);
  }
  return card;
}

async function loadOverview() {
  const data = await apiGet("ui/overview");
  const status = $("#status");
  status.replaceChildren(
    makeStatusCard("日记篇数", String(data.diary_count ?? 0), `最近：${data.latest_diary || "暂无"}`),
    makeStatusCard("今日记录", String(data.record_count_today ?? 0), `昨天：${data.record_count_yesterday ?? 0} 条`),
    makeStatusCard(
      "ManicTime",
      data.manictime?.generated_at ? `${Math.max(0, Math.round(data.manictime.age_minutes ?? 0))} 分钟前` : "暂无数据",
      data.manictime?.generated_at || "尚未检测到活动摘要",
    ),
    makeStatusCard(
      "定时写日记",
      data.last_result || "等待首次运行",
      data.last_attempt ? `上次尝试：${data.last_attempt}` : "状态会在日记任务执行后更新",
    ),
  );

  const list = $("#diary-list");
  list.replaceChildren();
  for (const day of data.diary_dates || []) {
    const button = document.createElement("button");
    button.className = "diary-item";
    button.type = "button";
    button.textContent = day;
    button.classList.toggle("selected", day === currentDate);
    button.addEventListener("click", () => selectDate(day));
    list.append(button);
  }
  return data;
}

async function selectDate(day) {
  currentDate = day;
  $("#date-picker").value = day;
  $("#diary-title").textContent = `${day} 的日记`;
  $("#diary-meta").textContent = "保存后会直接写入日记文件";
  $("#diary-message").textContent = "";
  $("#save-diary").disabled = false;
  $("#generate-diary").disabled = false;
  $("#diary-content").disabled = false;
  $("#diary-content").value = "";
  for (const item of document.querySelectorAll(".diary-item")) {
    item.classList.toggle("selected", item.textContent === day);
  }
  try {
    const diary = await apiGet(`ui/diary/${encodeURIComponent(day)}`);
    $("#diary-content").value = diary.content || "";
    $("#diary-meta").textContent = diary.exists ? "编辑正文后点击保存" : "这一天还没有日记，可手动撰写或生成";
  } catch (error) {
    showMessage($("#diary-message"), error.message, true);
  }
}

function createConfigField(section, key, definition, value) {
  const field = document.createElement("div");
  field.className = "config-field";
  const id = `config-${section}-${key}`;
  const label = document.createElement("label");
  label.htmlFor = id;
  label.textContent = definition.description || key;
  const hint = document.createElement("small");
  hint.textContent = definition.hint || "";

  let input;
  if (definition.type === "bool") {
    const toggle = document.createElement("label");
    toggle.className = "toggle-field";
    input = document.createElement("input");
    input.type = "checkbox";
    input.checked = value === true;
    input.dataset.section = section;
    input.dataset.key = key;
    input.id = id;
    const text = document.createElement("span");
    text.textContent = definition.description || key;
    toggle.append(input, text);
    field.append(toggle);
  } else {
    if (definition.type === "text") {
      input = document.createElement("textarea");
      input.rows = 5;
    } else {
      input = document.createElement("input");
      input.type = definition.type === "int" || definition.type === "float" ? "number" : "text";
      if (definition.type === "int") input.step = "1";
      if (definition.type === "float") input.step = "any";
    }
    input.id = id;
    input.dataset.section = section;
    input.dataset.key = key;
    input.value = value ?? definition.default ?? "";
    field.append(label, input);
  }
  if (hint.textContent) field.append(hint);
  return field;
}

async function loadConfig() {
  const data = await apiGet("ui/config");
  configSchema = data.schema || {};
  const form = $("#config-form");
  form.replaceChildren();
  for (const [section, definition] of Object.entries(configSchema)) {
    if (definition.type !== "object" || !definition.items) continue;
    const group = document.createElement("section");
    group.className = "config-section";
    const heading = document.createElement("h3");
    heading.textContent = definition.description || section;
    group.append(heading);
    const values = data.config?.[section] || {};
    for (const [key, fieldDefinition] of Object.entries(definition.items)) {
      group.append(createConfigField(section, key, fieldDefinition, values[key]));
    }
    form.append(group);
  }
}

function readConfigForm() {
  const result = {};
  for (const input of document.querySelectorAll("#config-form [data-section]")) {
    const { section, key } = input.dataset;
    result[section] ||= {};
    const definition = configSchema[section]?.items?.[key];
    if (!definition) continue;
    if (definition.type === "bool") {
      result[section][key] = input.checked;
    } else if (definition.type === "int") {
      result[section][key] = input.value === "" ? 0 : Number(input.value);
    } else if (definition.type === "float") {
      result[section][key] = input.value === "" ? 0 : Number.parseFloat(input.value);
    } else {
      result[section][key] = input.value;
    }
  }
  return result;
}

async function refresh() {
  $("#refresh").disabled = true;
  try {
    const overview = await loadOverview();
    if (currentDate && !overview.diary_dates?.includes(currentDate)) {
      await selectDate(currentDate);
    }
  } catch (error) {
    showMessage($("#diary-message"), error.message, true);
  } finally {
    $("#refresh").disabled = false;
  }
}

async function initialize() {
  await bridge.ready();
  $("#refresh").addEventListener("click", refresh);
  $("#date-picker").addEventListener("change", (event) => {
    if (event.target.value) selectDate(event.target.value);
  });
  $("#save-diary").addEventListener("click", async () => {
    if (!currentDate) return;
    const button = $("#save-diary");
    button.disabled = true;
    try {
      await apiPost(`ui/diary/${encodeURIComponent(currentDate)}`, { content: $("#diary-content").value });
      showMessage($("#diary-message"), "日记已保存");
      await loadOverview();
    } catch (error) {
      showMessage($("#diary-message"), error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#generate-diary").addEventListener("click", async () => {
    if (!currentDate) return;
    const button = $("#generate-diary");
    button.disabled = true;
    showMessage($("#diary-message"), "正在调用模型生成日记……");
    try {
      const result = await apiPost("ui/generate", { date: currentDate });
      if (!result.ok) throw new Error(result.message || "日记生成失败");
      await selectDate(currentDate);
      showMessage($("#diary-message"), `日记已生成（${result.message}）`);
      await loadOverview();
    } catch (error) {
      showMessage($("#diary-message"), error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  $("#save-config").addEventListener("click", async () => {
    const button = $("#save-config");
    button.disabled = true;
    showMessage($("#config-message"), "正在保存……");
    try {
      await apiPost("ui/config", { config: readConfigForm() });
      showMessage($("#config-message"), "配置已保存并立即应用");
    } catch (error) {
      showMessage($("#config-message"), error.message, true);
    } finally {
      button.disabled = false;
    }
  });
  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", async () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === tab));
      document.querySelectorAll(".panel").forEach((panel) => panel.classList.toggle("active", panel.id === tab.dataset.panel));
      if (tab.dataset.panel === "settings" && !$("#config-form").childElementCount) {
        try {
          await loadConfig();
        } catch (error) {
          showMessage($("#config-message"), error.message, true);
        }
      }
    });
  }
  await refresh();
}

initialize().catch((error) => {
  showMessage($("#diary-message"), error.message || "插件页面初始化失败", true);
});
