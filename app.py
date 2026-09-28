#!/usr/bin/env python3
import csv
import datetime
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def load_env_file(path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    except Exception:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


BASE_DIR = Path(__file__).resolve().parent
load_env_file(BASE_DIR / ".env.local")
load_env_file(BASE_DIR / ".env")

PUBLIC_DIR = BASE_DIR / "public"
RULES_FILE = BASE_DIR / "business_rules.md"
TEMPLATE_FILE = BASE_DIR / "prompt_template.md"
LOG_DIR = BASE_DIR / "logs"
JSONL_LOG = LOG_DIR / "evaluations.jsonl"
MD_LOG = LOG_DIR / "evaluations.md"

CUSTOMERS_FILE = BASE_DIR / "data" / "customers.csv"
MERCHANTS_FILE = BASE_DIR / "data" / "merchants.csv"
TRANSACTIONS_FILE = BASE_DIR / "data" / "transactions.csv"
RISK_SCORES_FILE = BASE_DIR / "data" / "risk_model_scores.csv"
FUND_FLOWS_FILE = BASE_DIR / "data" / "fund_flows.csv"
CUSTOMER_TXN_MAP_FILE = BASE_DIR / "data" / "customer_transactions.csv"
LABELS_FILE = BASE_DIR / "data" / "sample_transaction_labels.csv"

# 客户交易映射表里的交易角色取值，历史交易只为特征计算服务，样例交易才是待判定对象。
TXN_ROLE_SAMPLE = "当前样例交易"

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

USER_INPUT_FIELDS = [
    "客户ID", "交易ID", "商户名称", "交易金额", "交易时间",
    "交易渠道", "交易类型",
]
# 交易表命中时这些字段以表内事实为准，用户改的只是"要判定哪一笔"这个入口。
TXN_FACT_FIELDS = ["客户ID", "商户名称", "交易金额", "交易时间", "交易渠道", "交易类型"]
CUSTOMER_FIELDS = [
    "客户年龄", "职业", "婚姻状态", "教育程度", "客户分群", "卡片产品",
    "发卡距今天数", "卡片状态", "信用额度", "账户资产余额", "账户负债余额",
    "常驻城市", "手机号可用标识", "电话银行开通标识", "e支付开通标识",
    "ATM可用标识", "POS可用标识",
]
MERCHANT_FIELDS = ["商户MCC", "是否高风险MCC", "商户城市"]
FEATURE_FIELDS = [
    "是否跨区域", "是否夜间交易",
    "近7日夜间交易笔数", "近30日夜间交易笔数",
    "近7日线上交易笔数", "近30日线上交易笔数",
    "近7日跨区域笔数", "近30日跨区域笔数",
    "近7日高风险MCC笔数", "近7日入出账比",
]
RISK_FIELDS = ["模型风险分", "规则命中数", "模型版本", "更新时间"]

# 客户分群在这批演示数据里与人工标签一一对应（异常活跃18笔全高、需关注18笔全中、
# 稳定客户36笔全低），R1-R10 也不使用它。它是标签泄漏字段：页面照常展示，但不进模型输入。
PROMPT_HIDDEN_FIELDS = {"客户分群"}
# 业务规则真正引用的客户字段，其余画像只作背景，避免模型把无关属性当成风险依据。
CUSTOMER_RULE_FIELDS = ("发卡距今天数", "卡片状态", "常驻城市")
# 提示词模板用这两个标记把内容分成两条消息：规则与字段口径放 system，
# 待判定上下文与输出要求放 user，让格式约束离生成点更近。
SYSTEM_MARKER = "<!-- system -->"
USER_MARKER = "<!-- user -->"


def read_file(path):
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except Exception as exc:
        return f"(读取文件失败: {exc})"


def load_table(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]
    except Exception:
        return []


def index_by(rows, key):
    return {row.get(key, ""): row for row in rows if row.get(key)}


def get_customer(cid):
    if not cid:
        return None
    return index_by(load_table(CUSTOMERS_FILE), "客户ID").get(cid)


def get_merchant(name):
    if not name:
        return None
    return index_by(load_table(MERCHANTS_FILE), "商户名称").get(name)


def get_transaction(txn_id):
    if not txn_id:
        return None
    return index_by(load_table(TRANSACTIONS_FILE), "交易ID").get(txn_id)


def get_risk_score(txn_id):
    if not txn_id:
        return None
    return index_by(load_table(RISK_SCORES_FILE), "交易ID").get(txn_id)


def get_customer_history(cid):
    if not cid:
        return []
    return [row for row in load_table(TRANSACTIONS_FILE) if row.get("客户ID") == cid]


def get_customer_transaction_map(cid):
    if not cid:
        return []
    rows = [row for row in load_table(CUSTOMER_TXN_MAP_FILE) if row.get("客户ID") == cid]
    try:
        rows.sort(key=lambda row: int(row.get("交易顺序", "0") or 0))
    except ValueError:
        pass
    return rows


def get_fund_flows(cid):
    if not cid:
        return []
    return [row for row in load_table(FUND_FLOWS_FILE) if row.get("客户ID") == cid]


def parse_dt(value):
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(value).strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None


def is_night(dt):
    if not dt:
        return ""
    # 夜间时段按 23:00-06:00 定义，与样例标签口径一致。
    return "是" if dt.hour >= 23 or dt.hour < 6 else "否"


def compute_fund_flow_ratio(cid, txn_dt):
    """按当前交易时点计算近7日入账总额/出账总额，只读取资金流水表。"""
    if not cid or not txn_dt:
        return ""
    start = txn_dt - datetime.timedelta(days=7)
    inflow = 0.0
    outflow = 0.0
    for row in get_fund_flows(cid):
        flow_dt = parse_dt(row.get("流水时间"))
        if not flow_dt or not start <= flow_dt <= txn_dt:
            continue
        try:
            amount = float(row.get("金额", "0") or 0)
        except ValueError:
            continue
        if row.get("收支方向") == "入账":
            inflow += amount
        elif row.get("收支方向") == "出账":
            outflow += amount
    if outflow <= 0:
        return "不可用（无出账流水）" if inflow > 0 else ""
    return f"{inflow / outflow:.2f}"


def transaction_city(row, merchant_map):
    """交易城市属于商户：优先取商户表的商户城市，交易表城市作为兜底。"""
    merchant = merchant_map.get(row.get("商户名称", "")) or {}
    return merchant.get("商户城市") or row.get("交易城市", "")


def compute_recent_profile(cid, txn_dt, current_txn_id, base_city, merchant_map, limit=8):
    """给出本次交易之前的若干笔事实和一份历史基线，规则里的"显著高于历史"需要它才可判定。"""
    if not cid:
        return [], {}
    history = []
    for row in get_customer_history(cid):
        if row.get("交易ID") == current_txn_id:
            continue
        row_dt = parse_dt(row.get("交易时间"))
        if txn_dt and row_dt and row_dt >= txn_dt:
            continue
        history.append((row_dt or datetime.datetime.min, row))
    history.sort(key=lambda item: item[0])
    rows = []
    for _, row in history[-limit:][::-1]:
        merchant = merchant_map.get(row.get("商户名称", "")) or {}
        city = transaction_city(row, merchant_map)
        rows.append({
            "交易时间": row.get("交易时间", ""),
            "交易金额": row.get("交易金额", ""),
            "交易渠道": row.get("交易渠道", ""),
            "交易类型": row.get("交易类型", ""),
            "商户名称": row.get("商户名称", ""),
            "商户城市": city,
            "是否夜间": is_night(parse_dt(row.get("交易时间"))),
            "是否高风险MCC": merchant.get("是否高风险MCC", ""),
            "是否跨区域": "" if not (city and base_city) else ("否" if city == base_city else "是"),
        })
    if not history:
        return rows, {}
    amounts = sorted(float(r.get("交易金额") or 0) for _, r in history)
    mid = amounts[len(amounts) // 2] if len(amounts) % 2 else (
        amounts[len(amounts) // 2 - 1] + amounts[len(amounts) // 2]) / 2
    night = sum(1 for _, r in history if is_night(parse_dt(r.get("交易时间"))) == "是")
    online = sum(1 for _, r in history if r.get("交易渠道") == "线上无卡")
    region = sum(1 for _, r in history if base_city and transaction_city(r, merchant_map)
                 and transaction_city(r, merchant_map) != base_city)
    high = sum(1 for _, r in history
               if (merchant_map.get(r.get("商户名称", "")) or {}).get("是否高风险MCC") == "是")
    total = len(history)
    baseline = {
        "历史交易笔数": str(total),
        "历史金额中位数": f"{mid:.0f}",
        "历史金额均值": f"{sum(amounts) / total:.0f}",
        "历史夜间占比": f"{night / total:.0%}",
        "历史线上无卡占比": f"{online / total:.0%}",
        "历史跨区域占比": f"{region / total:.0%}",
        "历史高风险MCC占比": f"{high / total:.0%}",
    }
    return rows, baseline


def compute_history_features(cid, txn_dt, current_txn_id, base_city, merchant_map):
    """系统从交易历史表中计算近7日/30日特征，不包含当前待判定交易本身。"""
    history = get_customer_history(cid)
    result = {}
    if not history or not txn_dt:
        return result
    windows = {"7": txn_dt - datetime.timedelta(days=7), "30": txn_dt - datetime.timedelta(days=30)}
    for label, start in windows.items():
        window_rows = []
        for row in history:
            if row.get("交易ID") == current_txn_id:
                continue
            row_dt = parse_dt(row.get("交易时间"))
            if row_dt and start <= row_dt <= txn_dt:
                window_rows.append(row)
        result[f"近{label}日夜间交易笔数"] = str(sum(1 for r in window_rows if is_night(parse_dt(r.get("交易时间"))) == "是"))
        result[f"近{label}日线上交易笔数"] = str(sum(1 for r in window_rows if r.get("交易渠道") == "线上无卡"))
        if base_city:
            cities = [transaction_city(r, merchant_map) for r in window_rows]
            result[f"近{label}日跨区域笔数"] = str(sum(1 for c in cities if c and c != base_city))
        if label == "7":
            result["近7日高风险MCC笔数"] = str(sum(
                1 for r in window_rows
                if (merchant_map.get(r.get("商户名称", "")) or {}).get("是否高风险MCC") == "是"
            ))
    ratio = compute_fund_flow_ratio(cid, txn_dt)
    if ratio:
        result["近7日入出账比"] = ratio
    return result


def prepare_context(user_input):
    user = {k: str(user_input.get(k, "")).strip() for k in USER_INPUT_FIELDS}
    txn_id = user["交易ID"]
    cid = user["客户ID"]
    merchant_name = user["商户名称"]

    txn_row = get_transaction(txn_id) or {}
    # 命中交易表就整行覆盖，否则换了交易ID其他字段还停在上一笔。
    raw_input = dict(user)
    corrected = []
    for key in TXN_FACT_FIELDS:
        table_value = txn_row.get(key, "")
        if not table_value:
            continue
        if user.get(key) != table_value:
            if user.get(key):
                corrected.append(key)
            user[key] = table_value
    cid = user.get("客户ID", "")
    merchant_name = user.get("商户名称", "")

    customer = get_customer(cid) or {}
    merchant = get_merchant(merchant_name) or {}
    risk_row = get_risk_score(txn_id) or {}
    transaction_map = get_customer_transaction_map(cid)
    merchant_map = {row.get("商户名称"): row for row in load_table(MERCHANTS_FILE)}

    txn_dt = parse_dt(user.get("交易时间") or txn_row.get("交易时间"))
    txn_city = merchant.get("商户城市") or transaction_city(txn_row, merchant_map)
    base_city = customer.get("常驻城市") or txn_row.get("常驻城市", "")

    features = {}
    if txn_city and base_city:
        features["是否跨区域"] = "否" if txn_city == base_city else "是"
    if txn_dt:
        features["是否夜间交易"] = is_night(txn_dt)

    computed = compute_history_features(cid, txn_dt, txn_id, base_city, merchant_map)
    for key, value in computed.items():
        features[key] = value
    if txn_dt and not features.get("近7日入出账比"):
        features["近7日入出账比"] = "不可用（缺少资金流水）"

    recent_rows, recent_baseline = compute_recent_profile(cid, txn_dt, txn_id, base_city, merchant_map)
    try:
        amount = float(user.get("交易金额") or 0)
        median = float(recent_baseline.get("历史金额中位数") or 0)
    except ValueError:
        amount = median = 0.0
    if amount and median:
        recent_baseline["本次金额/历史中位数"] = f"{amount / median:.1f}倍"

    risk = {k: risk_row.get(k, "") for k in RISK_FIELDS}
    if not risk.get("模型风险分"):
        risk["模型风险分"] = "不可用（需风控模型）"

    missing = []
    if not cid:
        missing.append("客户ID缺失")
    elif not customer:
        missing.append(f"客户表未匹配到客户ID {cid}")
    if merchant_name and not merchant:
        missing.append(f"商户表未匹配到商户 {merchant_name}")
    if not txn_id:
        missing.append("交易ID缺失")
    if cid and not transaction_map:
        missing.append(f"客户交易映射表未匹配到客户ID {cid}")
    if risk.get("模型风险分", "").startswith("不可用"):
        missing.append("模型风险分不可用")
    if str(features.get("近7日入出账比", "")).startswith("不可用"):
        missing.append("近7日入出账比不可用")

    return {
        "raw_input": {k: v for k, v in raw_input.items() if v},
        "user_input": {k: v for k, v in user.items() if v},
        "resolved_input": user,
        "resolved_from_transaction": bool(txn_row),
        "corrected_fields": corrected,
        "customer": {k: customer.get(k, "") for k in CUSTOMER_FIELDS} if customer else {},
        "merchant": {k: merchant.get(k, "") for k in MERCHANT_FIELDS} if merchant else {},
        "features": {k: features.get(k, "") for k in FEATURE_FIELDS},
        "risk_model": risk,
        "recent_transactions": recent_rows,
        "recent_baseline": recent_baseline,
        "customer_transactions": [
            {
                "交易ID": row.get("交易ID", ""),
                "交易角色": row.get("交易角色", ""),
                "交易顺序": row.get("交易顺序", ""),
                "交易时间": row.get("交易时间", ""),
            }
            for row in transaction_map
        ],
        "missing": missing,
        "table_sources": {
            "customer": str(CUSTOMERS_FILE),
            "merchant": str(MERCHANTS_FILE),
            "transactions": str(TRANSACTIONS_FILE),
            "risk_model": str(RISK_SCORES_FILE),
            "fund_flows": str(FUND_FLOWS_FILE),
            "customer_transactions": str(CUSTOMER_TXN_MAP_FILE),
        },
    }


def format_block(data):
    if not data:
        return "(无)"
    return "\n".join(f"- {k}: {v}" for k, v in data.items() if v not in (None, ""))


def format_customer_for_prompt(customer):
    """规则引用的客户字段和纯背景画像分行列出，并剔除标签泄漏字段。"""
    visible = {k: v for k, v in (customer or {}).items()
               if v not in (None, "") and k not in PROMPT_HIDDEN_FIELDS}
    if not visible:
        return "(无)"
    referenced = {k: v for k, v in visible.items() if k in CUSTOMER_RULE_FIELDS}
    background = {k: v for k, v in visible.items() if k not in CUSTOMER_RULE_FIELDS}
    lines = []
    if referenced:
        lines.append("- 规则引用字段：" + "；".join(f"{k}={v}" for k, v in referenced.items()))
    if background:
        lines.append("- 画像背景（R1-R10 未使用，不构成风险依据）："
                     + "；".join(f"{k}={v}" for k, v in background.items()))
    return "\n".join(lines)


def format_risk_for_prompt(risk):
    """只把模型风险分当判定输入，其余外部字段明确标成不参与判定。"""
    risk = risk or {}
    lines = [f"- 模型风险分: {risk.get('模型风险分') or '不可用'}"]
    extra = "；".join(f"{k}={v}" for k, v in risk.items() if k != "模型风险分" and v)
    if extra:
        lines.append(f"- 外部模型附带说明（不参与判定）: {extra}")
    return "\n".join(lines)


RECENT_COLUMNS = ["交易时间", "交易金额", "交易渠道", "交易类型", "商户名称", "商户城市",
                  "是否夜间", "是否高风险MCC", "是否跨区域"]


def format_recent(rows):
    if not rows:
        return "无历史交易（可能是新客或该客户首笔）"
    lines = ["| " + " | ".join(RECENT_COLUMNS) + " |",
             "|" + "---|" * len(RECENT_COLUMNS)]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "") or "—") for col in RECENT_COLUMNS) + " |")
    return "\n".join(lines)


def rule_sections_for_prompt():
    """business_rules.md 是给人完整编辑的业务文档，模型只需要第4节规则表和第5节定级逻辑。
    按章节标题裁剪，改文件仍然即时生效；两节都找不到时退回全文，避免静默丢规则。"""
    text = read_file(RULES_FILE)
    kept, capturing = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            capturing = line[3:].strip().startswith(("4 ", "5 "))
        if capturing:
            kept.append(line)
    return "\n".join(kept).strip() or text


def split_system_user(full_prompt):
    if USER_MARKER not in full_prompt:
        return "", full_prompt.strip()
    head, tail = full_prompt.split(USER_MARKER, 1)
    return head.replace(SYSTEM_MARKER, "").strip(), tail.strip()


def build_prompt(context, template_file=TEMPLATE_FILE):
    template = read_file(template_file)
    if not template:
        template = "{{business_rules}}\n\n{{context}}"
    user_block = "### 用户输入\n" + format_block(context.get("user_input"))
    corrected = context.get("corrected_fields") or []
    if corrected:
        user_block += "\n- 说明：以下字段以交易表为准，已按交易ID还原：" + "、".join(corrected)
    context_text = "\n\n".join([
        user_block,
        "### 客户表补全\n" + format_customer_for_prompt(context.get("customer")),
        "### 商户表补全\n" + format_block(context.get("merchant")),
        "### 系统计算特征\n" + format_block(context.get("features")),
        "### 历史行为基线\n" + format_block(context.get("recent_baseline"))
        + "\n\n本次交易之前该客户的近期交易（最近 8 笔，按时间倒序，事实表原样列出）：\n\n"
        + format_recent(context.get("recent_transactions")),
        "### 风险模型输出\n" + format_risk_for_prompt(context.get("risk_model")),
        "### 数据完整性\n" + ("无缺失" if not context.get("missing")
                          else "\n".join(f"- {m}" for m in context["missing"])),
    ])
    full = (template
            .replace("{{business_rules}}", rule_sections_for_prompt())
            .replace("{{context}}", context_text)
            .replace("{{recent_transactions}}", format_recent(context.get("recent_transactions")))
            .replace("{{reference_data}}", ""))
    system_text, user_text = split_system_user(full)
    return {"system": system_text, "user": user_text, "text": full.strip()}


# 简化版 LLM 终判 prompt：字段说明 + 已判定规则 + 判定逻辑 + 字段值 → 结果。
# 规则引擎已经把每条规则的命中情况算好，LLM 只需要在此基础上结合判定逻辑给最终结论，
# 不再塞历史交易明细、完整业务规则文档和上下文大表，避免 prompt 冗长。
SIMPLIFIED_FIELD_DICT = {
    "客户ID": "客户唯一标识",
    "交易ID": "交易唯一标识",
    "商户名称": "交易商户",
    "交易金额": "本次交易金额",
    "交易时间": "交易发生时间",
    "交易渠道": "线上无卡 / POS / ATM / 其他",
    "交易类型": "消费 / 转账支付 / 取现 / 其他",
    "发卡距今天数": "卡片发行距今天数，<30 视为新开卡",
    "卡片状态": "正常 / 休眠后激活",
    "常驻城市": "客户常驻城市",
    "商户MCC": "商户类别码",
    "是否高风险MCC": "是否属于高风险商户类别",
    "商户城市": "商户所在城市，交易城市取自此字段",
    "是否跨区域": "商户城市是否与客户常驻城市不同",
    "是否夜间交易": "交易时间是否落在 23:00-06:00",
    "近7日夜间交易笔数": "近 7 日夜间交易频率",
    "近30日夜间交易笔数": "近 30 日夜间交易频率",
    "近7日线上交易笔数": "近 7 日线上交易频率",
    "近30日线上交易笔数": "近 30 日线上交易频率",
    "近7日跨区域笔数": "近 7 日跨区域交易频率",
    "近30日跨区域笔数": "近 30 日跨区域交易频率",
    "近7日高风险MCC笔数": "近 7 日高风险商户交易频率",
    "近7日入出账比": "近 7 日入账总额 / 出账总额",
    "模型风险分": "独立风控模型输出分数，0-1",
    "本次金额/历史中位数": "本次金额相对历史中位数的倍数",
    "历史交易笔数": "客户历史交易总数",
}

# 规则判定真正用到的字段及其来源块，待判定值只收这些，其余画像不进 prompt。
JUDGE_VALUE_SOURCES = [
    ("user_input", ["客户ID", "交易ID", "商户名称", "交易金额", "交易时间", "交易渠道", "交易类型"]),
    ("customer", ["发卡距今天数", "卡片状态", "常驻城市"]),
    ("merchant", ["商户MCC", "是否高风险MCC", "商户城市"]),
    ("features", ["是否跨区域", "是否夜间交易", "近7日夜间交易笔数", "近30日夜间交易笔数",
                  "近7日线上交易笔数", "近30日线上交易笔数", "近7日跨区域笔数", "近30日跨区域笔数",
                  "近7日高风险MCC笔数", "近7日入出账比"]),
    ("risk_model", ["模型风险分"]),
    ("recent_baseline", ["本次金额/历史中位数", "历史交易笔数"]),
]


def collect_judge_values(context):
    """扁平化收集规则用到的字段值，给 LLM 看的待判定事实。"""
    values = {}
    for block, keys in JUDGE_VALUE_SOURCES:
        data = context.get(block, {}) or {}
        for k in keys:
            v = data.get(k, "")
            if v not in ("", None):
                values[k] = v
    return values


def build_simplified_prompt(context, rule_decision):
    """简化版 LLM 终判 prompt：字段说明 + 规则引擎预判结果 + 判定逻辑 + 字段值 + 输出要求。"""
    values = collect_judge_values(context)
    field_intro = "\n".join(f"- {k}: {SIMPLIFIED_FIELD_DICT[k]}"
                            for k in SIMPLIFIED_FIELD_DICT if k in values) or "(无)"

    rd = rule_decision or {}
    rule_block = [
        f"- 规则独立判定（不看模型分）: {rd.get('rule_only_level', '') or '—'}",
        f"- 规则引擎最终等级: {rd.get('risk_level', '') or '—'}",
        f"- 命中规则: {', '.join(rd.get('rules_hit', [])) or '无'}",
        f"- 建议动作: {rd.get('action', '') or '—'}",
        "",
        "逐条核对:",
    ]
    for line in rd.get("rule_check", []):
        rule_block.append(f"  - {line}")
    rule_text = "\n".join(rule_block)

    logic = read_file(RULES_FILE)
    value_lines = "\n".join(f"- {k}: {v}" for k, v in values.items()) or "(无)"

    user_text = (
        "## 字段说明\n" + field_intro + "\n\n"
        "## 规则引擎预判结果（已判定好的规则，仅供参考，可调整）\n" + rule_text + "\n\n"
        "## 业务规则全文\n" + logic + "\n\n"
        "## 待判定交易字段值\n" + value_lines + "\n\n"
        "## 输出要求\n"
        "基于字段值和规则引擎预判结果，按判定逻辑给出最终风险判定。"
        "如果认同规则引擎结论可直接复用；如有分歧须写明分歧来源。"
        "输出 JSON，字段：risk_level（高/中/低/数据不完整）、rule_only_level、"
        "rule_check（逐条核对列表）、rules_hit、reasons、evidence_fields、"
        "disconfirming_evidence、confidence（0-1）、verdict、action、"
        "requires_human_review、customer_facing_note。只输出 JSON，不要解释。"
    )
    system_text = (
        "你是信用卡欺诈监测风控判定助手。规则引擎已基于事实字段给出每条规则的命中情况，"
        "你需要在此基础上结合判定逻辑给出最终风险判定。只输出 JSON。"
    )
    return {"system": system_text, "user": user_text, "text": user_text}


def get_llm_config():
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    model = os.environ.get("LLM_MODEL", "Qwen/Qwen3.8-Flash-Next")
    endpoint = os.environ.get("LLM_ENDPOINT") or "https://api-inference.modelscope.cn/v1"
    if not endpoint.endswith("/chat/completions"):
        endpoint = endpoint.rstrip("/") + "/chat/completions"
    max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8192"))
    # 判定任务要可复现，默认温度 0；做采样对比时用 LLM_TEMPERATURE 覆盖。
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0"))
    return {"api_key": api_key, "configured": bool(api_key), "model": model,
            "endpoint": endpoint, "max_tokens": max_tokens, "temperature": temperature}


def call_llm(prompt, system_prompt=None):
    cfg = get_llm_config()
    if not cfg["configured"]:
        return "demo", "", None
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    payload = {"model": cfg["model"], "messages": messages,
               "max_tokens": cfg["max_tokens"], "temperature": cfg["temperature"]}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg['api_key']}"}
    request = urllib.request.Request(cfg["endpoint"], data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        msg = data["choices"][0]["message"]
        return "llm", (msg.get("content") or msg.get("reasoning_content") or ""), None
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
            parsed = json.loads(detail)
            message = parsed.get("error", {}).get("message") or detail
        except Exception:
            message = str(exc)
        return "error", "", f"HTTP {exc.code}: {message}"
    except Exception as exc:
        return "error", "", str(exc)


def to_num(value, default=None):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def evaluate_rule_engine(context):
    """按 business_rules.md 第4节的 R2-R10 做确定性判定，作为大模型的对照基线。

    阈值与规则表一一对应；R1 黑名单在本演示数据里没有字段，按未命中处理。
    """
    features = context.get("features", {})
    customer = context.get("customer", {})
    merchant = context.get("merchant", {})
    user = context.get("user_input", {})
    baseline = context.get("recent_baseline", {})
    score = to_num(context.get("risk_model", {}).get("模型风险分", ""))

    def count(key):
        return to_num(features.get(key, ""), 0.0) or 0.0

    night = features.get("是否夜间交易") == "是"
    region = features.get("是否跨区域") == "是"
    online = user.get("交易渠道") == "线上无卡"
    high_mcc = merchant.get("是否高风险MCC") == "是"
    ratio = to_num(features.get("近7日入出账比", ""))
    new_card = (to_num(customer.get("发卡距今天数", ""), None) is not None
                and to_num(customer.get("发卡距今天数", ""), 0) < 30) \
        or "休眠后激活" in str(customer.get("卡片状态", ""))
    abnormal_combo = sum([night, region, online])
    amount_multiple = to_num(str(baseline.get("本次金额/历史中位数", "")).replace("倍", ""))
    history_count = to_num(baseline.get("历史交易笔数", ""), 0) or 0

    hits, reasons, evidence, checks = [], [], [], []

    def record(rule, hit, detail):
        """逐条留下命中与未命中理由，和大模型输出的 rule_check 对齐，便于按规则核对。"""
        if hit:
            hits.append(rule)
            reasons.append(f"{rule} {detail}")
        checks.append(f"{rule} {'命中' if hit else '未命中'}：{detail}")

    record("R2", new_card and abnormal_combo >= 2,
           f"发卡距今天数={customer.get('发卡距今天数','')}、卡片状态={customer.get('卡片状态','')}，"
           f"夜间={features.get('是否夜间交易','')}、跨区域={features.get('是否跨区域','')}、渠道={user.get('交易渠道','')}，"
           f"异常项={abnormal_combo}（阈值：新卡或休眠后激活 且 异常项>=2）")
    record("R3", bool(ratio is not None and ratio >= 2.0 and (high_mcc or region)),
           f"近7日入出账比={features.get('近7日入出账比','')}（阈值>=2.0），"
           f"高风险MCC={merchant.get('是否高风险MCC','')}、跨区域={features.get('是否跨区域','')}")
    record("R4", count("近7日夜间交易笔数") >= 5 or count("近30日夜间交易笔数") >= 12,
           f"近7日夜间交易笔数={features.get('近7日夜间交易笔数','')}（阈值>=5），"
           f"近30日夜间交易笔数={features.get('近30日夜间交易笔数','')}（阈值>=12）")
    record("R5", count("近30日线上交易笔数") >= 15 or count("近30日跨区域笔数") >= 6,
           f"近30日线上交易笔数={features.get('近30日线上交易笔数','')}（阈值>=15），"
           f"近30日跨区域笔数={features.get('近30日跨区域笔数','')}（阈值>=6）")
    record("R6", count("近7日高风险MCC笔数") >= 2,
           f"近7日高风险MCC笔数={features.get('近7日高风险MCC笔数','')}（阈值>=2）")
    record("R7", bool(amount_multiple is not None and amount_multiple >= 4 and history_count >= 5),
           f"本次金额={user.get('交易金额','')} 为历史中位数 {baseline.get('历史金额中位数','')} 的 "
           f"{baseline.get('本次金额/历史中位数','')}（阈值>=4倍），历史笔数={int(history_count)}（阈值>=5）")
    signal_hits = [r for r in hits if r in ("R4", "R5", "R6", "R7")]
    rule_only = "高" if ("R2" in hits or "R3" in hits) else ("中" if signal_hits else "低")

    if score is None or (context.get("missing") and "模型风险分不可用" in context["missing"]):
        hits.append("R10")
        level, action, human = "数据不完整", "补齐风控模型结果后重新判定，期间转人工核验", True
        reasons.append("R10 模型风险分不可用，按数据质量规则不得自动放行。")
    else:
        band = "高" if score >= 0.8 else ("中" if score >= 0.4 else "低")
        if "R2" in hits or "R3" in hits:
            level, action, human = "高", "人工复核并维持管控，核验资金来源与用途", True
        elif score >= 0.8 and signal_hits:
            level, action, human = "高", "人工复核并维持管控", True
        elif signal_hits and band in ("中", "高"):
            hits.append("R8")
            level, action, human = "中", "加强认证并转人工复核", True
        elif not hits and band == "低":
            hits.append("R9")
            level, action, human = "低", "正常放行，保留审计记录", False
        else:
            level, action, human = "中", "规则与模型结论不一致，转人工复核", True
        reasons.append(f"模型风险分={score:.2f} 属于{band}风险区间；规则独立判定为{rule_only}。")
        if rule_only != level:
            reasons.append(f"规则独立判定（{rule_only}）与最终等级（{level}）不一致，按第5节转人工确认。")

    for key in ("是否夜间交易", "是否跨区域", "近7日入出账比", "近7日夜间交易笔数", "近30日夜间交易笔数",
                "近30日线上交易笔数", "近30日跨区域笔数", "近7日高风险MCC笔数"):
        if features.get(key):
            evidence.append(f"{key}={features[key]}")
    if merchant.get("是否高风险MCC"):
        evidence.append(f"是否高风险MCC={merchant['是否高风险MCC']}")
    counter = []
    if rule_only != "高":
        counter.append(f"未命中 R2/R3 高风险组合，规则独立判定仅为{rule_only}")
    if amount_multiple is not None and amount_multiple < 4:
        counter.append(f"本次金额仅为历史中位数的 {baseline.get('本次金额/历史中位数','')}，未达 R7 阈值4倍")
    if not counter:
        counter.append("无反向证据")
    return {"rule_check": checks,
            "rule_only_level": rule_only, "risk_level": level, "confidence": 0.9,
            "verdict": f"规则引擎判定为{level}风险（规则独立判定={rule_only}）。",
            "action": action, "rules_hit": hits, "reasons": reasons,
            "disconfirming_evidence": counter, "evidence_fields": evidence,
            "requires_human_review": human, "customer_facing_note": ""}


def demo_decision(context):
    return evaluate_rule_engine(context)


def parse_decision(content):
    if not content:
        return {"risk_level": "无法判定", "verdict": "未收到模型返回。", "action": "人工复核",
                "rules_hit": [], "reasons": ["模型接口未返回内容。"], "evidence_fields": [],
                "requires_human_review": True, "customer_facing_note": "请配合进一步核验。"}
    text = content.strip()
    candidates = []
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
    if m:
        candidates.append(m.group(1).strip())
    if text.startswith("{"):
        candidates.append(text)
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidates.append(m.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            get = lambda *keys: next((data[k] for k in keys if k in data), None)
            level = get("risk_level", "riskLevel", "风险等级") or "无法判定"
            rules = get("rules_hit", "rulesHit", "命中规则")
            reasons = get("reasons", "判定逻辑", "原因")
            evidence = get("evidence_fields", "evidenceFields", "关键证据")
            counter = get("disconfirming_evidence", "disconfirmingEvidence", "反向证据")
            confidence = get("confidence", "置信度")
            rule_check = get("rule_check", "ruleCheck", "规则核对")
            return {"risk_level": level,
                    "rule_only_level": get("rule_only_level", "ruleOnlyLevel", "规则独立判定") or "",
                    "rule_check": rule_check if isinstance(rule_check, list)
                    else ([str(rule_check)] if rule_check else []),
                    "confidence": confidence if isinstance(confidence, (int, float)) else None,
                    "verdict": get("verdict", "判定结论") or "模型未给出明确结论。",
                    "action": get("action", "建议动作") or "人工复核",
                    "rules_hit": rules if isinstance(rules, list) else ([rules] if rules else []),
                    "reasons": reasons if isinstance(reasons, list) else ([str(reasons)] if reasons else []),
                    "evidence_fields": evidence if isinstance(evidence, list) else ([evidence] if evidence else []),
                    "disconfirming_evidence": counter if isinstance(counter, list) else ([counter] if counter else []),
                    "requires_human_review": bool(get("requires_human_review", "requiresHumanReview", "需人工审核") or level in ("高", "数据不完整")),
                    "customer_facing_note": get("customer_facing_note", "customerFacingNote", "客户沟通口径") or ""}
    return {"risk_level": "无法判定", "verdict": "模型返回内容无法解析为 JSON。", "action": "人工复核",
            "rules_hit": [], "reasons": ["模型输出不符合约定结构，请查看原始输出。"], "evidence_fields": [],
            "requires_human_review": True, "customer_facing_note": "请配合进一步核验。", "raw": content[:4000]}


def append_log(context, prompt, mode, raw_content, decision, error):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    entry = {"timestamp": ts, "mode": mode, "raw_input": context.get("raw_input"),
             "user_input": context.get("user_input"), "corrected_fields": context.get("corrected_fields"),
             "context": context, "prompt": prompt, "llm_raw": raw_content,
             "decision": decision, "error": error}
    with open(JSONL_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    with open(MD_LOG, "a", encoding="utf-8") as f:
        f.write(f"## 判定日志 {ts}\n\n- 模式：{mode}\n- 错误：{error or '无'}\n\n")
        if context.get("corrected_fields"):
            f.write("### 输入还原\n\n" + "\n".join(f"- {k}：按交易表覆盖用户填写值" for k in context["corrected_fields"]) + "\n\n")
        f.write("### 系统上下文\n\n```json\n" + json.dumps(context, ensure_ascii=False, indent=2) + "\n```\n\n")
        f.write("### 判定结果\n\n```json\n" + json.dumps(decision, ensure_ascii=False, indent=2) + "\n```\n\n")


def get_options():
    customers = load_table(CUSTOMERS_FILE)
    merchants = load_table(MERCHANTS_FILE)
    txns = load_table(TRANSACTIONS_FILE)
    transaction_map = load_table(CUSTOMER_TXN_MAP_FILE)
    role_by_txn = {row.get("交易ID", ""): row.get("交易角色", "") for row in transaction_map}
    sample_ids = sorted(t for t, role in role_by_txn.items() if role == TXN_ROLE_SAMPLE)
    customer_txn_ids = {}
    order_by_txn = {}
    for row in transaction_map:
        cid = row.get("客户ID", "")
        txn_id = row.get("交易ID", "")
        if cid and txn_id:
            customer_txn_ids.setdefault(cid, []).append(txn_id)
            order_by_txn.setdefault(txn_id, int(row.get("交易顺序", "0") or 0))
    # 待判定样例排在前面，历史交易按时间顺序跟在后面。
    for rows in customer_txn_ids.values():
        rows.sort(key=lambda txn_id: (role_by_txn.get(txn_id) != TXN_ROLE_SAMPLE,
                                      order_by_txn.get(txn_id, 0)))
    return {
        "customerIds": [c.get("客户ID", "") for c in customers],
        "merchantNames": [m.get("商户名称", "") for m in merchants],
        "transactionIds": [t.get("交易ID", "") for t in txns],
        "sampleTransactionIds": sample_ids,
        "transactionCounts": {
            "样例交易": len(sample_ids),
            "历史交易": len(txns) - len(sample_ids),
            "合计": len(txns),
        },
        "customerTransactionMap": customer_txn_ids,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MinimalFinancialAgent/2.0"

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            body = path.read_bytes()
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        # 规则和页面都要随手可改，禁用缓存避免看到旧版前端。
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        route = urllib.parse.urlparse(self.path).path
        if route in ("/", "/index.html"):
            return self.send_file(PUBLIC_DIR / "index.html", "text/html; charset=utf-8")
        if route == "/app.css":
            return self.send_file(PUBLIC_DIR / "app.css", "text/css; charset=utf-8")
        if route == "/app.js":
            return self.send_file(PUBLIC_DIR / "app.js", "text/javascript; charset=utf-8")
        if route == "/api/health":
            return self.send_json({"ok": True})
        if route == "/api/config":
            cfg = get_llm_config()
            return self.send_json({
                "llmConfigured": cfg["configured"], "llmModel": cfg["model"], "llmEndpoint": cfg["endpoint"],
                "rulesPath": str(RULES_FILE), "templatePath": str(TEMPLATE_FILE),
                "logsPath": str(JSONL_LOG),
                "businessRules": read_file(RULES_FILE),
                "promptTemplate": read_file(TEMPLATE_FILE),
                "options": get_options(),
                "tableSources": {
                    "customers": str(CUSTOMERS_FILE), "merchants": str(MERCHANTS_FILE),
                    "transactions": str(TRANSACTIONS_FILE), "risk_model": str(RISK_SCORES_FILE),
                    "fund_flows": str(FUND_FLOWS_FILE),
                    "customer_transactions": str(CUSTOMER_TXN_MAP_FILE),
                },
            })
        return self.send_json({"error": "not found"}, status=404)

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            return self.send_json({"error": f"请求格式错误: {exc}"}, status=400)
        if route == "/api/prepare":
            return self.send_json({"context": prepare_context(payload.get("input", payload))})
        if route != "/api/evaluate" and route != "/api/llm_judge":
            return self.send_json({"error": "not found"}, status=404)
        try:
            context = prepare_context(payload.get("input", payload))
            # 规则引擎做确定性预判，/api/evaluate 就到这里，字段填完自动出结果。
            rule_decision = evaluate_rule_engine(context)
            if route == "/api/evaluate":
                append_log(context, None, "rules", None, rule_decision, None)
                return self.send_json({"mode": "rules", "decision": rule_decision,
                                       "context": context, "error": None})
            # /api/llm_judge：用简化版 prompt 把规则预判结果交给 LLM 做最终判定。
            prompt = build_simplified_prompt(context, rule_decision)
            mode, raw_content, error = call_llm(prompt["user"], prompt["system"])
            if mode == "error":
                decision = {"risk_level": "无法判定", "verdict": "调用大模型接口失败，转人工处理。",
                            "action": "人工复核", "rules_hit": rule_decision.get("rules_hit", []),
                            "reasons": [f"接口错误：{error}"], "evidence_fields": [],
                            "requires_human_review": True,
                            "customer_facing_note": "系统暂无法自动判定，请配合进一步核验。"}
            else:
                decision = parse_decision(raw_content) if raw_content else rule_decision
            append_log(context, prompt, mode, raw_content, decision, error)
            return self.send_json({"mode": mode, "decision": decision, "rule_decision": rule_decision,
                                   "context": context, "error": error})
        except Exception as exc:
            import traceback
            traceback.print_exc()
            return self.send_json({"error": str(exc)}, status=500)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Minimal Financial Agent running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
