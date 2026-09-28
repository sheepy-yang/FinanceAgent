#!/usr/bin/env python3
"""从原始 72 笔样例交易生成商户城市、客户-交易映射、历史交易、资金流水和模型分。

交易城市属于商户属性：历史交易按“是否跨区域、是否高风险MCC”倒推挑选商户，
交易的交易城市始终等于所选商户在商户表中的商户城市。
"""
import csv
import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
TRANSACTIONS_FILE = DATA_DIR / "transactions.csv"
LABELS_FILE = DATA_DIR / "sample_transaction_labels.csv"
MAPPING_FILE = DATA_DIR / "customer_transactions.csv"
FUND_FLOWS_FILE = DATA_DIR / "fund_flows.csv"
RISK_SCORES_FILE = DATA_DIR / "risk_model_scores.csv"
CUSTOMERS_FILE = DATA_DIR / "customers.csv"
MERCHANTS_FILE = DATA_DIR / "merchants.csv"

MERCHANT_FIELDS = ["商户名称", "商户MCC", "是否高风险MCC", "商户城市"]


def read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_dt(value):
    return datetime.datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M:%S")


def fmt_dt(value):
    return value.strftime("%Y-%m-%d %H:%M:%S")


def adjust_time(dt, night, idx):
    if night:
        return dt.replace(hour=2, minute=idx % 60, second=0)
    return dt.replace(hour=10, minute=(idx * 7) % 60, second=0)


def sync_merchant_cities(merchants, source_rows):
    """商户表可人工编辑：只在缺少商户城市时用原始样例补齐，已有值保持不变。"""
    city_by_name = {}
    for row in source_rows:
        name, city = row.get("商户名称", ""), row.get("交易城市", "")
        if name and city:
            city_by_name.setdefault(name, city)
    for row in merchants:
        if not row.get("商户城市"):
            row["商户城市"] = city_by_name.get(row.get("商户名称", ""), "")
    write_csv(MERCHANTS_FILE, MERCHANT_FIELDS,
              [{k: row.get(k, "") for k in MERCHANT_FIELDS} for row in merchants])
    return merchants


def pick_merchant(merchants, base_city, need_region, need_high, idx):
    """按区域与风险维度挑选商户，保证商户城市就是交易的交易城市。"""
    high_flag = "是" if need_high else "否"
    usable = [r for r in merchants if r.get("是否高风险MCC") == high_flag and r.get("商户城市")]
    pool = [r for r in usable if (r["商户城市"] != base_city) == need_region] or usable or merchants
    return pool[idx % len(pool)]


def history_time(current_dt, idx, within_7d):
    if within_7d:
        base = current_dt - datetime.timedelta(hours=12 + idx * 2)
    else:
        base = current_dt - datetime.timedelta(days=8 + idx // 3, hours=(idx % 3) * 7)
    return base


def make_source_rows():
    if LABELS_FILE.exists():
        return read_csv(LABELS_FILE)
    rows = read_csv(TRANSACTIONS_FILE)
    if not rows:
        raise RuntimeError("未找到样例交易表")
    required = {"近7日夜间交易笔数", "近30日夜间交易笔数", "近7日入出账比", "模型风险分"}
    if not required.issubset(rows[0].keys()):
        raise RuntimeError("当前交易表已不是原始派生样例，请先恢复 sample_transaction_labels.csv")
    write_csv(LABELS_FILE, list(rows[0].keys()), rows)
    return rows


def risk_score_for_history(flags):
    score = 0.15
    score += 0.20 * int(flags["night"])
    score += 0.20 * int(flags["online"])
    score += 0.20 * int(flags["region"])
    score += 0.25 * int(flags["high"])
    return min(0.95, round(score, 2))


def emit_history(records, current_id, cid, seq, txn_dt, flags, merchants, base_city):
    """追加一笔历史交易，以及它的映射行、模型分和成对资金流水。"""
    merchant = pick_merchant(merchants, base_city, flags["region"], flags["high"], seq)
    txn_id = f"{current_id}-H{seq:02d}"
    flow_time = fmt_dt(txn_dt - datetime.timedelta(hours=2))
    records["transactions"].append({
        "交易ID": txn_id,
        "客户ID": cid,
        "交易时间": fmt_dt(txn_dt),
        "交易金额": str(1000 + seq * 100),
        "交易币种": "CNY",
        "交易渠道": "线上无卡" if flags["online"] else "POS",
        "交易类型": "消费",
        "商户名称": merchant["商户名称"],
        "交易城市": merchant.get("商户城市") or base_city,
    })
    records["mapping"].append({
        "客户ID": cid,
        "交易ID": txn_id,
        "交易角色": "历史交易",
        "交易时间": fmt_dt(txn_dt),
        "交易顺序": "",
        "模拟风险等级": "",
        "模拟标签": "",
    })
    records["risk"].append({
        "交易ID": txn_id,
        "模型风险分": f"{risk_score_for_history(flags):.2f}",
        "规则命中数": str(sum(flags.values())),
        "模型版本": "risk-model-v1-history",
        "更新时间": "2026-09-26 00:00:00",
    })
    for direction, suffix in (("出账", "OUT"), ("入账", "IN")):
        records["flows"].append({
            "流水ID": f"{txn_id}-BASE-{suffix}",
            "客户ID": cid,
            "流水时间": flow_time,
            "收支方向": direction,
            "金额": "10000.00",
            "关联交易ID": txn_id,
        })


def main():
    source_rows = make_source_rows()
    customers = {row["客户ID"]: row for row in read_csv(CUSTOMERS_FILE)}
    merchants = sync_merchant_cities(read_csv(MERCHANTS_FILE), source_rows)

    raw_transactions = []
    mapping_rows = []
    risk_rows = []
    flow_rows = []
    records = {
        "transactions": raw_transactions,
        "mapping": mapping_rows,
        "risk": risk_rows,
        "flows": flow_rows,
    }

    for source in source_rows:
        cid = source["客户ID"]
        current_id = source["交易ID"]
        current_dt = parse_dt(source["交易时间"])
        customer = customers.get(cid, {})
        base_city = customer.get("常驻城市", source.get("常驻城市", "北京"))

        raw_transactions.append({
            "交易ID": current_id,
            "客户ID": cid,
            "交易时间": source["交易时间"],
            "交易金额": source["交易金额"],
            "交易币种": source.get("交易币种", "CNY"),
            "交易渠道": source["交易渠道"],
            "交易类型": source["交易类型"],
            "商户名称": source["商户名称"],
            "交易城市": source["交易城市"],
        })
        mapping_rows.append({
            "客户ID": cid,
            "交易ID": current_id,
            "交易角色": "当前样例交易",
            "交易时间": source["交易时间"],
            "交易顺序": "",
            "模拟风险等级": source.get("模拟风险等级", ""),
            "模拟标签": source.get("模拟标签", ""),
        })
        risk_rows.append({
            "交易ID": current_id,
            "模型风险分": source["模型风险分"],
            "规则命中数": source.get("规则命中数", ""),
            "模型版本": "risk-model-v1-demo",
            "更新时间": "2026-09-26 00:00:00",
        })
        flow_rows.append({
            "流水ID": current_id + "-BASE-OUT",
            "客户ID": cid,
            "流水时间": fmt_dt(current_dt - datetime.timedelta(hours=2)),
            "收支方向": "出账",
            "金额": "10000.00",
            "关联交易ID": current_id,
        })
        flow_rows.append({
            "流水ID": current_id + "-BASE-IN",
            "客户ID": cid,
            "流水时间": fmt_dt(current_dt - datetime.timedelta(hours=2)),
            "收支方向": "入账",
            "金额": "10000.00",
            "关联交易ID": current_id,
        })

        seq = 0
        dims = [
            ("night", int(source["近7日夜间交易笔数"]), int(source["近30日夜间交易笔数"])),
            ("online", int(source["近7日线上交易笔数"]), int(source["近30日线上交易笔数"])),
            ("region", int(source["近7日跨区域笔数"]), int(source["近30日跨区域笔数"])),
            ("high", int(source["近7日高风险MCC笔数"]), int(source["近7日高风险MCC笔数"])),
        ]

        dimensions = ("night", "online", "region", "high")
        for dim, target7, target30 in dims:
            for i in range(max(0, target7)):
                seq += 1
                flags = {name: name == dim for name in dimensions}
                txn_dt = adjust_time(history_time(current_dt, i, True), dim == "night", i)
                emit_history(records, current_id, cid, seq, txn_dt, flags, merchants, base_city)

            if dim == "high":
                continue
            for i in range(max(0, target30 - target7)):
                seq += 1
                flags = {name: name == dim for name in dimensions}
                txn_dt = adjust_time(history_time(current_dt, i, False), dim == "night", i)
                emit_history(records, current_id, cid, seq, txn_dt, flags, merchants, base_city)

    # 让当前样例交易的近7日入出账比回到原样例值。
    for source in source_rows:
        cid = source["客户ID"]
        current_id = source["交易ID"]
        current_dt = parse_dt(source["交易时间"])
        target = float(source["近7日入出账比"])
        start = current_dt - datetime.timedelta(days=7)
        inflow = 0.0
        outflow = 0.0
        for row in flow_rows:
            if row["客户ID"] != cid:
                continue
            flow_dt = parse_dt(row["流水时间"])
            if start <= flow_dt <= current_dt:
                if row["收支方向"] == "入账":
                    inflow += float(row["金额"])
                else:
                    outflow += float(row["金额"])
        current_ratio = inflow / outflow if outflow else 1.0
        if abs(current_ratio - target) < 0.005:
            continue
        if target > current_ratio:
            add_out = 10000.0
            add_in = target * (outflow + add_out) - inflow
            if add_in < 0:
                add_in = 0.0
                add_out = max(0.0, inflow / target - outflow) if target else 0.0
        else:
            add_in = 0.0
            add_out = inflow / target - outflow if target else 0.0
        if add_out < 0:
            add_out = 0.0
        if add_in < 0:
            add_in = 0.0
        flow_time = fmt_dt(current_dt - datetime.timedelta(hours=1))
        if add_in > 0:
            flow_rows.append({
                "流水ID": current_id + "-ADJ-IN",
                "客户ID": cid,
                "流水时间": flow_time,
                "收支方向": "入账",
                "金额": f"{add_in:.2f}",
                "关联交易ID": current_id,
            })
        if add_out > 0:
            flow_rows.append({
                "流水ID": current_id + "-ADJ-OUT",
                "客户ID": cid,
                "流水时间": flow_time,
                "收支方向": "出账",
                "金额": f"{add_out:.2f}",
                "关联交易ID": current_id,
            })

    raw_transactions.sort(key=lambda r: (r["客户ID"], parse_dt(r["交易时间"])))
    order_by_customer = {}
    for row in raw_transactions:
        cid = row["客户ID"]
        order_by_customer.setdefault(cid, 0)
        order_by_customer[cid] += 1
        row["交易顺序"] = str(order_by_customer[cid])

    for row in mapping_rows:
        row["交易顺序"] = next(
            (t["交易顺序"] for t in raw_transactions if t["交易ID"] == row["交易ID"]),
            "",
        )
    mapping_rows.sort(key=lambda r: (r["客户ID"], int(r["交易顺序"] or 0)))

    transaction_fields = ["交易ID", "客户ID", "交易顺序", "交易时间", "交易金额", "交易币种", "交易渠道", "交易类型", "商户名称", "交易城市"]
    mapping_fields = ["客户ID", "交易ID", "交易角色", "交易顺序", "交易时间", "模拟风险等级", "模拟标签"]
    flow_fields = ["流水ID", "客户ID", "流水时间", "收支方向", "金额", "关联交易ID"]
    risk_fields = ["交易ID", "模型风险分", "规则命中数", "模型版本", "更新时间"]

    write_csv(TRANSACTIONS_FILE, transaction_fields, [{k: r.get(k, "") for k in transaction_fields} for r in raw_transactions])
    write_csv(MAPPING_FILE, mapping_fields, mapping_rows)
    write_csv(FUND_FLOWS_FILE, flow_fields, flow_rows)
    write_csv(RISK_SCORES_FILE, risk_fields, risk_rows)

    print(f"raw_transactions={len(raw_transactions)}")
    print(f"mapping_rows={len(mapping_rows)}")
    print(f"fund_flow_rows={len(flow_rows)}")
    print(f"risk_score_rows={len(risk_rows)}")

    city_by_name = {row["商户名称"]: row["商户城市"] for row in merchants if row.get("商户城市")}
    mismatched = [row["交易ID"] for row in raw_transactions
                  if row["商户名称"] in city_by_name and row["交易城市"] != city_by_name[row["商户名称"]]]
    print(f"merchants={len(merchants)}")
    print(f"merchant_city_mismatch={len(mismatched)}")


if __name__ == "__main__":
    main()
