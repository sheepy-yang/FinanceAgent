# 信用卡欺诈监测 Agent（表驱动最小版）

这是一个“直接调大模型”的最小金融 Agent 对比实现。页面只要求录入少量事实字段，客户表、商户表、交易历史、资金流水和风控模型结果由后端按标识自动关联；派生特征由系统计算并只读展示。后端读取规则、上下文和提示词模板后调用大模型，并把每次判定写入日志文件。

## 文件结构

- `business_rules.md`：业务规则，可编辑。
- `prompt_template.md`：当前提示词模板，可编辑；用 `<!-- system -->` / `<!-- user -->` 两段分开。
- `prompt_template.v1.md`、`prompt_template.v2.md`：历史版本存档，供提示词 A/B 对比实验使用。
- `data/customers.csv`：客户画像表，按客户ID关联。
- `data/merchants.csv`：商户表，按商户名称关联，提供 MCC、是否高风险和商户城市。
- `data/transactions.csv`：交易表，72 笔待判定样例加上派生的历史交易。
- `data/customer_transactions.csv`：客户ID到交易ID的映射表，一个客户对应多笔交易，用 `交易角色` 区分样例与历史。
- `data/fund_flows.csv`：资金流水表，用于计算近7日入出账比。
- `data/risk_model_scores.csv`：独立风控模型输出，不允许用户填写风险分。
- `data/field_dictionary.md`：字段字典，便于后续扩字段。
- `data/sample_transaction_labels.csv`：原始72笔样例的派生统计和模拟标签备份。
- `app.py`：控制逻辑与接口，写死工作流程。
- `scripts/build_transaction_history.py`：从原始样例派生商户城市、客户-交易映射、历史交易、资金流水和模型分。
- `scripts/check_frontend_logic.js`：前端交互自检，用 DOM 桩跑 `public/app.js`，不依赖浏览器。
- `scripts/compare_rule_engine.py`：用确定性规则引擎跑72笔标签对照，得到规则本身的准确率与混淆矩阵。
- `scripts/ab_prompt_eval.py`：提示词 A/B 评测，同一批案例分别跑新旧模板，比等级准确率、JSON 可解析率、`rule_check` 覆盖率和耗时。
- `scripts/probe_llm_once.py`：单发探针，确认模型接口是否可用，原始返回写到 `/tmp/probe_raw.json`。
- `public/`：网页前端。
- `logs/`：每次判定日志，`evaluations.jsonl` 和 `evaluations.md`。

## 运行

```bash
cd credit-card-fraud-agent
python3 app.py
```

打开 http://127.0.0.1:8000 。

页面支持深链，便于固定复现某个案例：<http://127.0.0.1:8000/?客户ID=CUS01001&交易ID=TXN2026090001>。

改动前端交互后，在项目根目录跑一次自检：

```bash
osascript -l JavaScript scripts/check_frontend_logic.js
```

## 接大模型

支持 OpenAI 兼容接口，通过环境变量配置：

```bash
export OPENAI_API_KEY="sk-..."
export LLM_MODEL="Qwen/Qwen3.8-Flash-Next"
# 可选，默认 https://api-inference.modelscope.cn/v1
export LLM_ENDPOINT="https://api-inference.modelscope.cn/v1"
python3 app.py
```

不配置 `OPENAI_API_KEY` 时，后端会进入演示模式，用简单规则生成判定结果，方便无 Key 时跑通完整流程并记录日志。

可选调参：`LLM_MAX_TOKENS`（默认 8192）、`LLM_TEMPERATURE`（默认 0，判定任务要可复现；做采样对比时才调高）。

也可以把环境变量写到项目根目录的 `.env.local`，服务启动时会自动读取：

```bash
OPENAI_API_KEY="sk-..."
LLM_MODEL="Qwen/Qwen3.8-Flash-Next"
LLM_ENDPOINT="https://api-inference.modelscope.cn/v1"
```

## 这个版本的设计

- 计划：写死的整体工作逻辑（读文件 -> 拼接提示词 -> 调模型 -> 写日志）。
- 存储：固定文件和固定格式。
- 推理：调用大模型接口。
- 执行：写死的接口逻辑。
- 补全接口按输入去重：同一份输入只保留一个在途请求，输入未变直接复用上次结果，避免前端事件回环反复打接口。
- 改了 CSV 之后刷新页面即可生效，补全缓存只存在于当前页面生命周期内。

## 提示词的构成

单次请求约 5.8K 字符，分两条消息发送：

- system：角色 + 上下文各块的来源与用法 + 业务规则第4节规则表和第5节定级逻辑。只注入这两节是因为 `business_rules.md` 其余章节是写数据和给人读的工程说明，全文塞入会挤占判定用的注意力；改文件仍然即时生效，两节标题找不到时退回全文，避免静默丢规则。
- user：待判定交易的系统上下文 + 判定纪律 + JSON 输出格式。输出约束离生成点越近，格式遵循越稳。
- 上下文包含用户输入、客户表补全、商户表补全、系统计算特征、历史行为基线（含最近 8 笔明细）、风险模型输出和数据完整性。
- 判定要求分两步：先给不看模型分的 `rule_only_level`，再给最终 `risk_level`，两者不一致必须写明分歧来源并转人工。
- `rule_check` 要求逐条写出 R2-R7 的命中或未命中以及数值比较，前端和评测脚本都能按条核对推理过程。

## 已知数据陷阱：客户分群是标签泄漏

`customers.csv` 里的 `客户分群` 与 72 笔人工标签一一对应：异常活跃 18 笔全为高、需关注 18 笔全为中、稳定客户 36 笔全为低。模型只要抄这个字段就能"判对"，准确率会失真。R1-R10 不使用该字段，因此它不进模型输入（页面仍展示）。后续扩数据时，凡是与标签强相关的画像字段都要先做这种交叉表检查。

## 数据关系

- `customers.csv` 通过 `客户ID` 连接 `customer_transactions.csv`。
- `customer_transactions.csv` 通过 `交易ID` 连接 `transactions.csv`、`fund_flows.csv` 和 `risk_model_scores.csv`。
- `transactions.csv` 通过 `商户名称` 连接 `merchants.csv`。
- 交易城市属于商户属性，`transactions.csv` 的 `交易城市` 与 `merchants.csv` 的 `商户城市` 保持一致。
- 在 `merchants.csv` 新增商户时请补上 `商户城市`，否则跨区域统计会退回用交易表里的交易城市兜底。
- 页面选择客户ID后，交易ID下拉框会过滤为该客户已有交易。
- 交易表 1716 笔 = 72 笔待判定样例（每客户 1 笔）+ 1644 笔历史交易。历史交易只服务近7日/30日特征统计，不进入判定候选，除非选定客户ID后主动挑选。
- 未选客户ID时交易ID下拉只列 72 笔样例；选定客户后切换为该客户交易，样例排在最前，历史按时间顺序跟在后面。
- 大模型每次判定只看单笔交易及其派生特征，历史交易以统计值形式进入提示词，不会整表塞入。
- 近7日/30日行为统计不包含当前待判定交易本身，只统计该客户在时间窗口内的历史交易。

## 字段边界

- 用户填写：客户ID、交易ID、商户名称、交易金额、交易时间、交易渠道、交易类型、补充说明。
- 交易ID 命中交易表时，客户ID、商户名称、交易金额、交易时间、交易渠道、交易类型以交易表为准：换交易ID这些字段一起更新，手改会被还原，页面提示「已按交易表还原：…」，日志里记下 `corrected_fields`。要做金额之类的假设场景，把交易ID 留空按新建交易填，或直接改 CSV。
- 交易ID 未命中交易表时按新建交易处理，上述字段沿用用户填写值，模型风险分与近7日入出账比会标记为不可用。
- 客户和商户字段：分别按客户ID、商户名称从连接表自动补全，商户表同时给出商户城市，因此交易城市不需要用户填写。
- 客户交易映射：按客户ID自动列出该客户的全部交易ID。
- 系统计算字段：是否跨区域（商户城市与常驻城市比较）、夜间交易、历史行为统计、近7日入出账比。城市只出现在客户表和商户表，不在特征栏重复。
- 模型输出字段：模型风险分、规则命中数、模型版本、更新时间。
- 系统计算字段和模型输出字段在页面中只读，不能人为覆盖。

## 评测与基线

评价分三层，目前前两层已经能自动跑：

1. 结果层：`python3 scripts/compare_rule_engine.py` 用确定性规则引擎对照72笔人工标签。当前 71/72 = 98.6%，唯一分歧是 TXN2026090027（休眠后激活触发 R2，人工判中）。这一步先确认"规则本身能不能复现人工判断"，然后大模型的偏差才有参照物。
2. 推理层：`python3 scripts/ab_prompt_eval.py --per-level 4` 对照新旧提示词的等级准确率、JSON 可解析率、`rule_check` 是否覆盖 R2-R7 六条、`confidence` 分布和耗时。明细追加到 `logs/ab_prompt.jsonl`。
3. 业务层：`logs/evaluations.md` 里保存每次判定的完整上下文和结论，人工按业务口径复核。

阈值校准也走这条链路：R7 的金额倍数原先是 3 倍，穷举样本分布后（低风险最高 3.2 倍、中风险最高 3.6 倍、高风险中位 6.6 倍）提到 4 倍，消掉 2 笔低风险误报且没丢高档命中，规则引擎从 95.8% 升到 98.6%。改阈值时同步改 `business_rules.md` 第4节和 `app.py` 的 `evaluate_rule_engine`。
