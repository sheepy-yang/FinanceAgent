const state = {
  config: null,
  options: null,
  context: null,
  autoTimer: null,
  prepareSeq: 0,
  appliedTxnId: "",
  appliedCustomerId: "",
  lastEventKey: "",
  pendingPrepare: null,
  preparedContext: null,
};

const INPUT_FIELDS = [
  "客户ID", "交易ID", "商户名称", "交易金额", "交易时间",
  "交易渠道", "交易类型",
];

// 命中已有交易时由系统自动带出的事实字段，客户ID 单独处理。
const TXN_RESOLVED_FIELDS = ["商户名称", "交易金额", "交易时间", "交易渠道", "交易类型"];

// 规则引擎 R 代号 → 业务优先级名称，预判块只显示名称，不暴露内部编号。
const RULE_LABELS = {
  R1: "P0黑名单",
  R2: "P1高风险组合",
  R3: "P1高风险资金流",
  R4: "P2夜间异常",
  R5: "P2线上和地域异常",
  R6: "P2高风险商户",
  R7: "P2金额偏离历史",
  R8: "P3中风险复核",
  R9: "P3低风险恢复",
  R10: "P0数据质量",
};

// 把文本里出现的 R 代号统一替换成业务名称，结果区不再暴露内部编号。
function relabelRules(text) {
  return String(text).replace(/\bR(\d+)\b/g, (_, n) => RULE_LABELS["R" + n] || _);
}

function formEl(name) {
  return document.getElementById("evalForm").querySelector(`[name="${name}"]`);
}

function readValue(name) {
  const el = formEl(name);
  return el ? el.value.trim() : "";
}

function readId(name) {
  return readValue(name).toUpperCase();
}

function inList(list, value) {
  return !!value && (list || []).some((item) => String(item).toUpperCase() === value);
}

function isKnownCustomer(value) {
  return inList(state.options?.customerIds, value);
}

function isKnownTransaction(value) {
  return inList(state.options?.transactionIds, value);
}

function setStatus() {
  // 判定改为纯代码规则引擎，不再依赖大模型，状态徽标固定为规则引擎。
  const statusEl = document.getElementById("llmStatus");
  const modelEl = document.getElementById("modelBadge");
  statusEl.textContent = "规则引擎";
  statusEl.className = "badge ok";
  modelEl.textContent = "代码判定";
}

function fillDatalist(id, values) {
  const list = document.getElementById(id);
  if (!list) return;
  list.innerHTML = values.map((v) => `<option value="${escapeHtml(v)}"></option>`).join("");
}

function transactionIdsFor(customerId) {
  const map = state.options?.customerTransactionMap || {};
  const key = Object.keys(map).find((k) => k.toUpperCase() === (customerId || "").toUpperCase());
  if (key) return map[key];
  // 未选客户时只列待判定样例，否则历史交易会把这个下拉撑到上千项。
  return state.options?.sampleTransactionIds || state.options?.transactionIds || [];
}

function transactionScopeText(scope, customerChosen) {
  if (!customerChosen) {
    return `留空表示新建交易；候选为 ${scope.length} 笔待判定样例，选定客户ID后可改选该客户的历史交易`;
  }
  const samples = new Set(sampleTransactionIds().map((v) => v.toUpperCase()));
  const sampleCount = scope.filter((id) => samples.has(String(id).toUpperCase())).length;
  return `留空表示新建交易；该客户 ${scope.length} 笔（样例 ${sampleCount} / 历史 ${scope.length - sampleCount}）`;
}

function sampleTransactionIds() {
  return state.options?.sampleTransactionIds || [];
}

function syncCustomerScope(customerId) {
  state.appliedCustomerId = customerId;
  fillDatalist("transactionIdList", transactionIdsFor(customerId));
}

function prefillFromLocation() {
  const params = new URLSearchParams(window.location.search);
  let touched = false;
  INPUT_FIELDS.forEach((name) => {
    const raw = params.get(name);
    if (raw) {
      setInputValue(name, raw);
      touched = true;
    }
  });
  if (!touched) return;
  const cid = readId("客户ID");
  if (isKnownCustomer(cid)) syncCustomerScope(cid);
  schedulePrepare(isKnownTransaction(readId("交易ID")));
}

async function loadConfig() {
  const res = await fetch("/api/config");
  if (!res.ok) throw new Error("config failed");
  state.config = await res.json();
  state.options = state.config.options || {};
  setStatus();
  document.getElementById("rulesText").value = state.config.businessRules || "";
  document.getElementById("templateText").value = state.config.promptTemplate || "";
  document.getElementById("rulesPath").textContent = state.config.rulesPath;
  document.getElementById("templatePath").textContent = state.config.templatePath;
  const src = state.config.tableSources || {};
  document.getElementById("tableSources").textContent =
    `客户表: ${src.customers || ""}\n商户表: ${src.merchants || ""}\n交易表: ${src.transactions || ""}\n客户交易映射: ${src.customer_transactions || ""}\n资金流水: ${src.fund_flows || ""}\n风控模型: ${src.risk_model || ""}`;
  const tc = state.config.options?.transactionCounts || {};
  const countsEl = document.getElementById("tableCounts");
  if (tc["合计"]) {
    countsEl.textContent = `交易表 ${tc["合计"]} 笔 = 待判定样例 ${tc["样例交易"]} 笔（每客户 1 笔）+ 历史交易 ${tc["历史交易"]} 笔（只用于特征计算，不参与判定）`;
    countsEl.classList.remove("hidden");
  }
  fillDatalist("customerIdList", state.options.customerIds || []);
  fillDatalist("merchantNameList", state.options.merchantNames || []);
  syncCustomerScope("");
  prefillFromLocation();
  renderHints(state.context || {});
}

function collectInput() {
  const input = {};
  INPUT_FIELDS.forEach((name) => {
    input[name] = readValue(name);
  });
  // ID 允许小写输入，查表前统一大写。
  input["客户ID"] = input["客户ID"].toUpperCase();
  input["交易ID"] = input["交易ID"].toUpperCase();
  return input;
}

function setInputValue(name, value) {
  const el = formEl(name);
  if (el) el.value = value ?? "";
}

function clearTransactionFields() {
  ["交易ID", ...TXN_RESOLVED_FIELDS].forEach((name) => setInputValue(name, ""));
  state.appliedTxnId = "";
}

function renderKV(containerId, data, options = {}) {
  const el = document.getElementById(containerId);
  if (!data || !Object.keys(data).length) {
    el.innerHTML = '<div class="kv empty"><span class="k">未匹配</span><span class="v">—</span></div>';
    return;
  }
  el.innerHTML = Object.entries(data).map(([k, v]) => {
    const value = v === "" || v === null || v === undefined ? "—" : v;
    const warnKeys = options.warnKeys || [];
    const cls = warnKeys.includes(k) && String(value).includes("不可用") ? "kv warn" : "kv";
    return `<div class="${cls}"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(value)}</span></div>`;
  }).join("");
}

function renderContext(context) {
  state.context = context;
  renderKV("customerFields", context.customer);
  renderKV("merchantFields", context.merchant);
  renderTxnMap(context.customer_transactions || []);
  renderKV("featureFields", context.features, { warnKeys: ["近7日入出账比"] });
  renderKV("riskFields", context.risk_model, { warnKeys: ["模型风险分"] });
  const missing = context.missing || [];
  const box = document.getElementById("missingBox");
  const status = document.getElementById("prepStatus");
  if (missing.length) {
    box.classList.remove("hidden");
    box.innerHTML = `<strong>数据不完整</strong><ul>${missing.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul>`;
    status.textContent = `补全完成，${missing.length} 项待处理`;
    status.className = "prep-status warn";
  } else {
    box.classList.add("hidden");
    status.textContent = "补全完成，字段齐全";
    status.className = "prep-status ok";
  }
  renderHints(context);
}

function setHint(id, text, tone) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = text;
  el.className = tone ? `field-hint ${tone}` : "field-hint";
}

function renderHints(context) {
  const cid = readId("客户ID");
  const tid = readId("交易ID");
  const customerCount = Object.keys(context?.customer || {}).length;
  if (!cid) setHint("customerIdHint", "留空则无法补全客户画像", "");
  else if (customerCount) setHint("customerIdHint", `客户表已匹配，自动补全 ${customerCount} 个字段`, "ok");
  else setHint("customerIdHint", "客户表未匹配该客户ID", "warn");

  const rows = context?.customer_transactions || [];
  const hit = rows.find((r) => (r["交易ID"] || "").toUpperCase() === tid);
  const scope = transactionIdsFor(state.appliedCustomerId);
  if (!tid) {
    setHint("transactionIdHint", transactionScopeText(scope, !!state.appliedCustomerId), "");
  } else if (hit) {
    const corrected = (context?.corrected_fields || []).join("、");
    setHint("transactionIdHint",
      `${hit["交易角色"] || "交易"} · 该客户第 ${hit["交易顺序"] || "?"}/${rows.length} 笔` +
      (corrected ? ` · 已按交易表还原：${corrected}` : ""),
      corrected ? "warn" : "ok");
  } else if (isKnownTransaction(tid)) {
    setHint("transactionIdHint", "交易表已匹配，客户ID以交易表为准", "warn");
  } else {
    setHint("transactionIdHint", "交易表未匹配，按新建交易处理", "");
  }

  const merchant = context?.merchant || {};
  const merchantCount = Object.keys(merchant).length;
  if (!readValue("商户名称")) setHint("merchantNameHint", "商户MCC、是否高风险、商户城市由商户表带出", "");
  else if (merchantCount) setHint("merchantNameHint", `商户表已匹配 · 商户城市 ${merchant["商户城市"] || "—"}`, "ok");
  else setHint("merchantNameHint", "商户表未匹配该商户", "warn");
}

function renderTxnMap(rows) {
  const el = document.getElementById("txnMapFields");
  if (!rows.length) {
    el.innerHTML = '<div class="kv empty"><span class="k">未匹配</span><span class="v">—</span></div>';
    return;
  }
  const current = rows.find((r) => r["交易角色"] === "当前样例交易")?.["交易ID"] || "—";
  const historyCount = rows.filter((r) => r["交易角色"] === "历史交易").length;
  const head = rows.slice(0, 7);
  let preview = head.map((r) => `${r["交易顺序"] || "?"}.${r["交易ID"]}`).join("，");
  // 样例交易常排在时间线末尾，预览里补一条，避免只看到一串历史ID。
  if (current && !head.some((r) => r["交易ID"] === current)) {
    preview += `，样例 ${current}（第 ${rows.find((r) => r["交易ID"] === current)?.["交易顺序"] || "?"} 笔）`;
  }
  el.innerHTML = [
    ["客户交易数", `${rows.length} 笔`],
    ["当前样例交易", current],
    ["历史交易数", `${historyCount} 笔`],
    ["映射预览", preview],
  ].map(([k, v]) => `<div class="kv"><span class="k">${escapeHtml(k)}</span><span class="v">${escapeHtml(v)}</span></div>`).join("");
}

async function prepare(applyResolved = false) {
  const input = collectInput();
  const key = JSON.stringify(input);
  if (!applyResolved) {
    // 同一份输入只允许一个在途请求，命中上次结果直接重绘，避免事件回环打爆接口。
    if (state.pendingPrepare?.key === key) return state.pendingPrepare.promise;
    if (state.preparedContext?.key === key) {
      renderContext(state.preparedContext.context);
      return state.preparedContext.context;
    }
  }
  const status = document.getElementById("prepStatus");
  status.textContent = "正在补全...";
  status.className = "prep-status";
  const seq = ++state.prepareSeq;
  const promise = requestPrepare(input, key, applyResolved, seq);
  if (!applyResolved) {
    state.pendingPrepare = { key, promise };
    promise.finally(() => {
      if (state.pendingPrepare?.key === key) state.pendingPrepare = null;
    });
  }
  return promise;
}

async function requestPrepare(input, key, applyResolved, seq) {
  const status = document.getElementById("prepStatus");
  try {
    const res = await fetch("/api/prepare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "prepare failed");
    if (seq !== state.prepareSeq) return data.context;
    // 交易表里有这笔交易时，事实字段一律按表回填，用户手改也会被还原。
    if (applyResolved || data.context.resolved_from_transaction) applyResolvedInput(data.context);
    renderContext(data.context);
    state.preparedContext = { key, context: data.context };
    return data.context;
  } catch (err) {
    if (seq !== state.prepareSeq) return null;
    status.textContent = `补全失败: ${err}`;
    status.className = "prep-status warn";
    return null;
  }
}

// 交易表命中时事实字段以表内数据为准：换交易ID要跟着更新，被手改也要还原。
// 交易表里没有这笔交易（按新建处理）时只回填一次，之后尊重用户编辑。
function applyResolvedInput(context) {
  const resolved = context.resolved_input || {};
  const tid = (resolved["交易ID"] || readId("交易ID")).toUpperCase();
  if (!tid) return;
  const fromTable = !!context.resolved_from_transaction;
  if (!fromTable && tid === state.appliedTxnId) return;
  let touched = false;
  const write = (name, value) => {
    if (readValue(name) !== value) {
      setInputValue(name, value);
      touched = true;
    }
  };
  write("交易ID", tid);
  TXN_RESOLVED_FIELDS.forEach((name) => write(name, resolved[name] || ""));
  const cid = (resolved["客户ID"] || "").toUpperCase();
  if (cid) {
    write("客户ID", cid);
    if (cid !== state.appliedCustomerId) syncCustomerScope(cid);
  }
  state.appliedTxnId = tid;
  // 程序回填不触发 input 事件，放开去重键，保证用户下一次改同样的值仍会被处理。
  if (touched) state.lastEventKey = "";
}

function schedulePrepare(applyResolved = false) {
  clearTimeout(state.autoTimer);
  if (applyResolved) {
    prepare(true);
    return;
  }
  state.autoTimer = setTimeout(() => prepare(false), 350);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function listItems(items) {
  // 列表项里出现的 R 代号统一替换成业务名称，跟命中规则 chips 保持一致。
  // LLM 的逐条核对可能返回对象（如 {rule, result, detail}），按对象渲染成「规则 + 结果标签 + 说明」。
  const arr = (Array.isArray(items) ? items : []).map((item) => {
    if (item && typeof item === "object") {
      // LLM 返回的核对项键名不稳定（result/status、detail/check/reason、hit 布尔…），逐一兜底。
      const rule = relabelRules(String(item.rule ?? item.name ?? ""));
      let result = String(item.result ?? item.status ?? "");
      if (!result && typeof item.hit === "boolean") result = item.hit ? "命中" : "未命中";
      let detail = String(item.detail ?? item.check ?? item.reason ?? item.note ?? item.description ?? "");
      if (!detail) {
        const known = new Set(["rule", "name", "result", "status", "hit", "detail", "check", "reason", "note", "description"]);
        detail = Object.entries(item)
          .filter(([k, v]) => !known.has(k) && v !== null && v !== undefined && v !== "")
          .map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`)
          .join("；");
      }
      if (!rule && !result && !detail) {
        return `<code>${escapeHtml(JSON.stringify(item))}</code>`;
      }
      return [
        rule ? `<strong>${escapeHtml(rule)}</strong>` : "",
        result ? `<span class="check-status${result.includes("未命中") ? "" : " hit"}">${escapeHtml(result)}</span>` : "",
        detail ? escapeHtml(detail) : "",
      ].filter(Boolean).join(" ");
    }
    return escapeHtml(relabelRules(item));
  });
  if (!arr.length) return '<p class="block-label">无</p>';
  return `<ul class="reason-list">${arr.map((item) => `<li>${item}</li>`).join("")}</ul>`;
}

function renderResult(data, areaId = "resultArea") {
  const area = document.getElementById(areaId);
  if (data.error) {
    area.innerHTML = `<div class="result"><div class="risk-row"><span class="risk-badge">接口错误</span></div><div class="verdict">${escapeHtml(data.error)}</div></div>`;
    return;
  }
  const d = data.decision || {};
  const level = d.risk_level || "无法判定";
  const levelKey = String(level).includes("高") ? "high" : String(level).includes("中") ? "medium" : String(level).includes("完整") ? "incomplete" : "low";
  const mode = data.mode === "rules" ? "规则预判"
    : data.mode === "llm" ? "LLM 终判"
    : data.mode === "demo" ? "演示规则"
    : "错误";
  const rules = (d.rules_hit || []).map((r) => `<span class="chip">${escapeHtml(RULE_LABELS[r] || r)}</span>`).join("");
  // 规则预判单独一个块，只保留风险等级（第一行）和命中规则，不铺完整结论。
  if (data.mode === "rules") {
    area.innerHTML = `<div class="result"><div class="risk-row risk-${levelKey}"><span class="risk-badge">${escapeHtml(level)}</span><span class="badge muted">${escapeHtml(mode)}</span></div><div><div class="block-label">命中规则</div><div class="chips">${rules || '<span class="chip">未命中</span>'}</div></div></div>`;
    return;
  }
  // 证据字段一般是不符点字段名；LLM 偶尔返回对象（如 {field, value}），兜底解析。
  const evidenceItems = (d.evidence_fields || []).map((e) => {
    if (e && typeof e === "object") {
      const k = String(e.field ?? e.name ?? e.key ?? "");
      const v = String(e.value ?? e.detail ?? "");
      if (k && v) return `${escapeHtml(k)}：${escapeHtml(v)}`;
      if (k) return escapeHtml(k);
      return `<code>${escapeHtml(JSON.stringify(e))}</code>`;
    }
    return escapeHtml(e);
  });
  const evidence = evidenceItems.length
    ? `<ul class="evidence-list">${evidenceItems.map((e) => `<li>${e}</li>`).join("")}</ul>`
    : '<p class="block-label">无</p>';
  const ruleCheck = (d.rule_check || []).length
    ? `<div><div class="block-label">规则逐条核对</div>${listItems(d.rule_check)}</div>`
    : "";
  const counter = (d.disconfirming_evidence || []).length
    ? `<div><div class="block-label">反向证据</div>${listItems(d.disconfirming_evidence)}</div>`
    : "";
  const note = d.customer_facing_note ? `<div class="action-box"><strong>沟通口径</strong>${escapeHtml(d.customer_facing_note)}</div>` : "";
  // 判定结果只展示大模型自身输出的字段：徽标行与规则引擎预判块不进结果区（规则情况看上方「规则预判」面板）。
  area.innerHTML = `
    <div class="result">
      <div class="verdict">${escapeHtml(d.verdict || "")}</div>
      <div class="action-box"><strong>建议动作</strong>${escapeHtml(d.action || "")}</div>
      ${ruleCheck}
      <div><div class="block-label">命中规则</div><div class="chips">${rules || '<span class="chip">未命中增强规则</span>'}</div></div>
      <div><div class="block-label">判定逻辑</div>${listItems(d.reasons)}</div>
      ${counter}
      <div><div class="block-label">关键证据字段</div>${evidence}</div>
      ${note}
    </div>`;
}

async function submitForm() {
  const button = document.getElementById("submitBtn");
  const area = document.getElementById("resultArea");
  button.disabled = true;
  area.innerHTML = '<div class="result-empty loading"><span class="spinner"></span>LLM 最终判定中，可能需要几十秒...</div>';
  try {
    const res = await fetch("/api/llm_judge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ input: collectInput() }),
    });
    const data = await res.json();
    if (data.context) renderContext(data.context);
    renderResult(res.ok ? data : { error: data.error || "请求失败" });
  } catch (err) {
    renderResult({ error: String(err) });
  } finally {
    button.disabled = false;
  }
}

function clearForm() {
  document.getElementById("evalForm").reset();
  state.context = null;
  state.appliedTxnId = "";
  state.appliedCustomerId = "";
  state.lastEventKey = "";
  state.preparedContext = null;
  clearTimeout(state.autoTimer);
  syncCustomerScope("");
  document.getElementById("customerFields").innerHTML = "";
  document.getElementById("merchantFields").innerHTML = "";
  document.getElementById("txnMapFields").innerHTML = "";
  document.getElementById("featureFields").innerHTML = "";
  document.getElementById("riskFields").innerHTML = "";
  document.getElementById("missingBox").classList.add("hidden");
  document.getElementById("prepStatus").textContent = "等待输入客户ID或交易ID";
  document.getElementById("prepStatus").className = "prep-status";
  document.getElementById("resultArea").innerHTML = "等待输入";
  renderHints(null);
}

// ID 既是下拉候选也可手输：命中已有交易就立刻补全并写回事实字段。
function onFormField(e) {
  const name = e.target?.name;
  if (!name) return;
  const key = `${name}=${readValue(name)}`;
  if (key === state.lastEventKey) return;
  state.lastEventKey = key;

  if (name === "客户ID") {
    const cid = readId("客户ID");
    if (!cid) syncCustomerScope("");
    else if (isKnownCustomer(cid) && cid !== state.appliedCustomerId) {
      clearTransactionFields();
      syncCustomerScope(cid);
    }
  }

  // 交易ID 要在客户切换清理之后再判断，否则会用上一个客户的交易去补全。
  const txnId = readId("交易ID");
  if (!isKnownTransaction(txnId)) state.appliedTxnId = "";
  schedulePrepare(isKnownTransaction(txnId));
}

function showInputView() {
  document.getElementById("inputView").classList.remove("hidden");
  document.getElementById("assetsView").classList.add("hidden");
  document.getElementById("tabInput").classList.add("active");
  document.getElementById("tabAssets").classList.remove("active");
}

function showAssetsView() {
  document.getElementById("inputView").classList.add("hidden");
  document.getElementById("assetsView").classList.remove("hidden");
  document.getElementById("tabInput").classList.remove("active");
  document.getElementById("tabAssets").classList.add("active");
}

document.addEventListener("DOMContentLoaded", () => {
  loadConfig().catch(() => setStatus());
  const form = document.getElementById("evalForm");
  form.addEventListener("submit", (e) => { e.preventDefault(); submitForm(); });
  form.addEventListener("input", onFormField);
  form.addEventListener("change", onFormField);
  document.getElementById("clearBtn").addEventListener("click", clearForm);
  document.getElementById("tabInput").addEventListener("click", showInputView);
  document.getElementById("tabAssets").addEventListener("click", showAssetsView);
});
