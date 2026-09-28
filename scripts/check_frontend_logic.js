// 前端交互逻辑自检：用最小 DOM 桩跑 public/app.js，不依赖浏览器和网络。
// 在项目根目录执行：osascript -l JavaScript scripts/check_frontend_logic.js
ObjC.import("Foundation");
var base = $.NSFileManager.defaultManager.currentDirectoryPath.js;
var src = $.NSString.stringWithContentsOfFileEncodingError(
  base + "/public/app.js", $.NSUTF8StringEncoding, null).js;

var registry = {}, fields = {}, timers = [], calls = [];
function makeEl(id) {
  return {
    id: id, value: "", innerHTML: "", textContent: "", className: "",
    classList: { add: function () {}, remove: function () {} },
    appendChild: function () {},
    reset: function () { if (this.id === "evalForm") { Object.keys(fields).forEach(function (k) { fields[k].value = ""; }); } },
    querySelector: function (sel) {
      var m = /name="([^"]+)"/.exec(sel);
      if (!m) return null;
      if (!fields[m[1]]) fields[m[1]] = { value: "" };
      return fields[m[1]];
    }
  };
}
function getEl(id) { if (!registry[id]) registry[id] = makeEl(id); return registry[id]; }
this.document = {
  getElementById: getEl,
  addEventListener: function () {},
  createElement: function () { return { value: "", textContent: "" }; }
};
this.window = { location: { search: "" } };
this.setTimeout = function (fn) { timers.push(fn); return timers.length; };
this.clearTimeout = function () {};
this.fetch = function (url, opts) {
  calls.push({ url: url, body: JSON.parse(opts.body) });
  return { then: function () { return { catch: function () {} }; } };
};

var api = (new Function(src +
  ';return {state:state,onFormField:onFormField,applyResolvedInput:applyResolvedInput,' +
  'renderHints:renderHints,renderTxnMap:renderTxnMap,setInputValue:setInputValue,readValue:readValue,clearForm:clearForm,' +
  'isKnownTransaction:isKnownTransaction,prepare:prepare,renderResult:renderResult};'))();

api.state.options = {
  customerIds: ["CUS01001", "CUS01002"],
  transactionIds: ["TXN2026090001", "TXN2026090001-H01", "TXN2026090002"],
  sampleTransactionIds: ["TXN2026090001", "TXN2026090002"],
  transactionCounts: { "样例交易": 2, "历史交易": 1, "合计": 3 },
  customerTransactionMap: {
    CUS01001: ["TXN2026090001", "TXN2026090001-H01"],
    CUS01002: ["TXN2026090002"]
  },
  merchantNames: ["餐饮商户01"]
};

function optionCount() {
  return (registry.transactionIdList.innerHTML.match(/<option/g) || []).length;
}

var out = [];
function check(label, cond, detail) { out.push((cond ? "PASS " : "FAIL ") + label + (detail ? "  [" + detail + "]" : "")); }
function fire(name) { api.onFormField({ target: { name: name } }); }
function set(name, v) { api.setInputValue(name, v); }

// 1. 输入客户ID（小写）应识别并过滤候选、清空上一笔交易字段
set("客户ID", "cus01001");
set("交易ID", "TXN2026090002");
set("交易金额", "15560");
timers.length = 0; calls.length = 0;
fire("客户ID");
check("客户ID 命中后过滤交易候选为该客户2笔", optionCount() === 2,
      registry.transactionIdList.innerHTML.slice(0, 60));
check("切换客户后清空交易字段", api.readValue("交易ID") === "" && api.readValue("交易金额") === "");
check("客户ID 走防抖而不是立即补全", calls.length === 0 && timers.length === 1);
check("记录已应用客户ID为大写", api.state.appliedCustomerId === "CUS01001", api.state.appliedCustomerId);

// 1b. 只填交易ID（客户留空）应由交易反查客户，立即补全
set("客户ID", "");
set("交易ID", "TXN2026090002");
timers.length = 0; calls.length = 0;
fire("客户ID");
check("客户留空但交易完整时立即补全", calls.length === 1 && calls[0].body.input["交易ID"] === "TXN2026090002",
      JSON.stringify(calls.map(function (c) { return c.body.input; })));
api.applyResolvedInput({ resolved_input: { "交易ID": "TXN2026090002", "客户ID": "CUS01002", "商户名称": "加油站商户02", "交易金额": "15560", "交易时间": "2026-09-03 03:14:00", "交易渠道": "POS", "交易类型": "消费" } });
check("交易反查回填客户ID", api.readValue("客户ID") === "CUS01002", api.readValue("客户ID"));
check("候选切换为该客户1笔", optionCount() === 1);

// 1c. 换客户时清掉上一笔交易，改走防抖而不是按旧交易补全
set("客户ID", "cus01001");
set("交易ID", "TXN2026090002");
timers.length = 0; calls.length = 0;
fire("客户ID");
check("换客户清空交易字段", api.readValue("交易ID") === "");
check("换客户不立即按旧交易补全", calls.length === 0 && timers.length === 1, JSON.stringify(calls.map(function (c) { return c.body.input; })));

// 2. 输入完整交易ID（小写）应立即触发补全，并统一大写
set("交易ID", "txn2026090001");
timers.length = 0; calls.length = 0;
fire("交易ID");
check("交易ID 命中立即请求", calls.length === 1 && timers.length === 0);
check("请求体 ID 已大写", calls.length === 1 && calls[0].body.input["交易ID"] === "TXN2026090001" && calls[0].body.input["客户ID"] === "CUS01001",
      JSON.stringify(calls[0] && calls[0].body.input));

// 3. 返回后自动带出事实字段
api.applyResolvedInput({ resolved_input: {
  "交易ID": "TXN2026090001", "客户ID": "CUS01001", "商户名称": "餐饮商户01",
  "交易金额": "11780", "交易时间": "2026-09-02 02:07:00", "交易渠道": "线上无卡", "交易类型": "消费" } });
check("自动带出商户与金额", api.readValue("商户名称") === "餐饮商户01" && api.readValue("交易金额") === "11780");
check("交易ID 归一为大写", api.readValue("交易ID") === "TXN2026090001");

// 4a. 交易表里没有这笔交易（新建交易）时，回填一次后尊重用户编辑
set("交易金额", "999");
api.applyResolvedInput({ resolved_from_transaction: false, resolved_input: {
  "交易ID": "TXN2026090001", "客户ID": "CUS01001", "商户名称": "餐饮商户01",
  "交易金额": "11780", "交易时间": "2026-09-02 02:07:00", "交易渠道": "线上无卡", "交易类型": "消费" } });
check("新建交易不覆盖用户编辑", api.readValue("交易金额") === "999", api.readValue("交易金额"));

// 4b. 交易表命中时事实字段以表为准，手改要还原，并放开事件去重键
api.state.lastEventKey = "交易金额=999";
api.applyResolvedInput({ resolved_from_transaction: true, resolved_input: {
  "交易ID": "TXN2026090001", "客户ID": "CUS01001", "商户名称": "餐饮商户01",
  "交易金额": "11780", "交易时间": "2026-09-02 02:07:00", "交易渠道": "线上无卡", "交易类型": "消费" } });
check("交易表命中还原被手改的金额", api.readValue("交易金额") === "11780", api.readValue("交易金额"));
check("还原后放开事件去重键", api.state.lastEventKey === "", api.state.lastEventKey);

// 4c. 换交易ID要带着其他字段一起更新
api.state.lastEventKey = "";
api.applyResolvedInput({ resolved_from_transaction: true, resolved_input: {
  "交易ID": "TXN2026090002", "客户ID": "CUS01002", "商户名称": "加油站商户02",
  "交易金额": "15560", "交易时间": "2026-09-03 03:14:00", "交易渠道": "POS", "交易类型": "转账/支付" } });
check("换交易ID同步更新其他字段",
      api.readValue("交易ID") === "TXN2026090002" && api.readValue("客户ID") === "CUS01002" &&
      api.readValue("商户名称") === "加油站商户02" && api.readValue("交易金额") === "15560" &&
      api.readValue("交易时间") === "2026-09-03 03:14:00" && api.readValue("交易渠道") === "POS" &&
      api.readValue("交易类型") === "转账/支付",
      JSON.stringify(["客户ID", "商户名称", "交易金额", "交易时间", "交易渠道", "交易类型"].map(api.readValue)));
check("换交易ID同步切换候选范围", api.state.appliedCustomerId === "CUS01002" && optionCount() === 1,
      api.state.appliedCustomerId + "/" + optionCount());
api.applyResolvedInput({ resolved_from_transaction: true, resolved_input: {
  "交易ID": "TXN2026090001", "客户ID": "CUS01001", "商户名称": "餐饮商户01",
  "交易金额": "11780", "交易时间": "2026-09-02 02:07:00", "交易渠道": "线上无卡", "交易类型": "消费" } });
check("换回原交易ID字段再跟着换回", api.readValue("商户名称") === "餐饮商户01" && api.readValue("交易金额") === "11780");

// 5. 未完成的ID只防抖，且允许下次重新带出
set("交易ID", "TXN2026");
timers.length = 0; calls.length = 0;
fire("交易ID");
check("半截ID不立即请求", calls.length === 0 && timers.length === 1);
check("半截ID重置已应用标记", api.state.appliedTxnId === "");

// 6. 提示渲染不崩
var ctx = { customer: { 客户年龄: "23" }, merchant: { 商户城市: "武汉" }, customer_transactions: [
  { "交易ID": "TXN2026090001", "交易角色": "当前样例交易", "交易顺序": "2" } ] };
set("交易ID", "TXN2026090001");
try { api.renderHints(ctx); check("renderHints 正常", /当前样例交易/.test(registry.transactionIdHint.textContent), registry.transactionIdHint.textContent); }
catch (e) { check("renderHints 正常", false, String(e)); }
check("商户提示显示商户城市", /武汉/.test(registry.merchantNameHint.textContent), registry.merchantNameHint.textContent);

// 7. 清空表单后只恢复待判定样例，历史交易不进未选客户时的下拉
api.clearForm();
check("清空后重置状态", api.state.appliedTxnId === "" && api.state.appliedCustomerId === "");
check("清空后候选缩到样例2笔", optionCount() === 2, "实际 " + optionCount());
check("清空后提示说明样例口径", /候选为 2 笔待判定样例/.test(registry.transactionIdHint.textContent),
      registry.transactionIdHint.textContent);
check("历史交易ID仍在交易表中可识别", api.isKnownTransaction("TXN2026090001-H01") === true);
check("手输未知交易ID按新建处理", api.isKnownTransaction("TXN9999999999") === false);
set("客户ID", "cus01001");
timers.length = 0;
fire("客户ID");
api.renderHints({ customer: { 客户年龄: "23" }, customer_transactions: [] });
check("选定客户后提示拆分样例与历史", /该客户 2 笔（样例 1 \/ 历史 1）/.test(registry.transactionIdHint.textContent),
      registry.transactionIdHint.textContent);

// 8. 样例交易排在时间线末尾时，映射预览仍要点名它
var many = [];
for (var i = 1; i <= 12; i++) {
  many.push({ "交易ID": "TXN2026090001-H" + i, "交易角色": "历史交易", "交易顺序": String(i) });
}
many.push({ "交易ID": "TXN2026090001", "交易角色": "当前样例交易", "交易顺序": "13" });
api.renderTxnMap(many);
check("映射预览补出样例交易", /样例 TXN2026090001/.test(registry.txnMapFields.innerHTML),
      registry.txnMapFields.innerHTML.replace(/<[^>]+>/g, "|").slice(0, 160));

// 9. 同一份输入不重复打接口，输入变化后才再请求
api.clearForm();
set("客户ID", "CUS01001");
calls.length = 0;
api.prepare(false);
api.prepare(false);
api.prepare(false);
check("同一输入只有一次补全请求", calls.length === 1, "实际 " + calls.length);
set("交易金额", "888");
api.prepare(false);
check("输入变化后重新请求", calls.length === 2, "实际 " + calls.length);

// 10. 判定结果渲染：两步定级、置信度、逐条规则核对、反向证据
api.renderResult({ mode: "llm", decision: {
  risk_level: "高", rule_only_level: "中", confidence: 0.55, verdict: "结论", action: "复核",
  rules_hit: ["R2", "R4"], rule_check: ["R2 命中：异常项=2", "R4 未命中：近7日夜间=4"],
  reasons: ["R2 夜间=是"], disconfirming_evidence: ["近7日入出账比=1.2 未达 2.0"],
  evidence_fields: ["是否夜间交易=是"], requires_human_review: true, customer_facing_note: ""
}});
var html = registry.resultArea.innerHTML;
check("两步判定分歧高亮", /badge split/.test(html) && /规则独立判定 中/.test(html), html.slice(0, 200));
check("渲染置信度", /置信度 0\.55/.test(html));
check("渲染规则逐条核对", /规则逐条核对/.test(html) && /R4 未命中/.test(html));
check("渲染反向证据", /反向证据/.test(html) && /近7日入出账比/.test(html));
api.renderResult({ mode: "llm", decision: {
  risk_level: "低", rule_only_level: "低", confidence: 0.9, verdict: "结论", action: "放行",
  rules_hit: [], rule_check: ["R2 未命中：发卡天数=1200"], reasons: ["R9"], disconfirming_evidence: [],
  evidence_fields: [], requires_human_review: false, customer_facing_note: ""
}});
var html2 = registry.resultArea.innerHTML;
check("两步一致时不分歧高亮", !/badge split/.test(html2) && /规则独立判定 低/.test(html2), html2.slice(0, 200));
api.renderResult({ mode: "demo", decision: { risk_level: "低", verdict: "v", action: "a", reasons: ["R9"] } });
check("结果缺新字段时照常渲染不报错",
      /risk-badge/.test(registry.resultArea.innerHTML) && !/置信度/.test(registry.resultArea.innerHTML),
      registry.resultArea.innerHTML.slice(0, 200));

var failed = out.filter(function (line) { return line.indexOf("FAIL") === 0; });
out.join("\n") + "\n\n" + (failed.length ? failed.length + " 项失败" : out.length + " 项全部通过");
