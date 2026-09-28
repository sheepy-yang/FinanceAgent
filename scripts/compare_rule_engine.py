#!/usr/bin/env python3
"""用确定性规则引擎跑一遍带标签样例，和模拟风险等级对照。

这是"先看结果准不准"的最小评价脚本：不调大模型，只验证 business_rules.md
第4节的 R2-R10 阈值能否复现人工标签。改完规则或阈值后重跑即可。

用法：python3 scripts/compare_rule_engine.py [标签文件]
"""
import collections
import csv
import importlib.util
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
LABELS_FILE = BASE_DIR / "data" / "sample_transaction_labels.csv"


def load_app():
    spec = importlib.util.spec_from_file_location("agent_app", BASE_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_app"] = module
    spec.loader.exec_module(module)
    return module


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LABELS_FILE
    app = load_app()
    rows = read_rows(path)
    confusion = collections.Counter()
    misses = []
    hit_counter = collections.Counter()
    correct = 0
    for row in rows:
        expected = row.get("模拟风险等级", "")
        if not expected:
            continue
        context = app.prepare_context({"客户ID": row.get("客户ID", ""),
                                       "交易ID": row.get("交易ID", "")})
        decision = app.evaluate_rule_engine(context)
        actual = decision.get("risk_level", "")
        confusion[(expected, actual)] += 1
        for rule in decision.get("rules_hit", []):
            hit_counter[rule] += 1
        if actual == expected:
            correct += 1
        else:
            misses.append((row.get("交易ID", ""), expected, actual,
                           decision.get("rules_hit", []),
                           decision.get("reasons", [])[:1]))
    total = sum(confusion.values())
    if not total:
        print("标签文件里没有模拟风险等级")
        return
    levels = sorted({expected for expected, _ in confusion})
    print(f"标签文件: {path.name}")
    print(f"样本数: {total}  规则引擎与标签一致: {correct}  准确率: {correct / total:.1%}\n")
    print("混淆矩阵（行=人工标签，列=规则引擎）")
    print("        " + "".join(f"{lv:>6}" for lv in levels))
    for expected in levels:
        print(f"{expected:>6}  " + "".join(f"{confusion.get((expected, actual), 0):>6}" for actual in levels))
    print("\n规则命中分布: " + ", ".join(f"{k}={v}" for k, v in sorted(hit_counter.items())))
    if misses:
        print(f"\n不一致 {len(misses)} 笔（最多列 15 笔）:")
        for txn_id, expected, actual, rules, reason in misses[:15]:
            print(f"  {txn_id} 标签={expected} 引擎={actual} 命中={','.join(rules) or '无'} "
                  f"{reason[0] if reason else ''}")


if __name__ == "__main__":
    main()
