#!/usr/bin/env python3
"""提示词 A/B 对比：同一份上下文，不同模板，跑真实大模型看结果和推理质量。

这是"先比结果、再比推理"的最小评测脚本：只比风险等级是否等于人工标签，
同时统计 JSON 可解析率、rule_check 覆盖率和耗时，确定性规则引擎作为参照列。

用法：
  python3 scripts/ab_prompt_eval.py                      # v2 与 v3 各跑每档 4 笔
  python3 scripts/ab_prompt_eval.py --per-level 6        # 每档 6 笔
  python3 scripts/ab_prompt_eval.py --templates v3       # 只跑新模板
  python3 scripts/ab_prompt_eval.py --workers 2
"""
import argparse
import concurrent.futures
import csv
import datetime
import importlib.util
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]


def load_app():
    spec = importlib.util.spec_from_file_location("agent_app", BASE_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["agent_app"] = module
    spec.loader.exec_module(module)
    return module


def pick_cases(rows, per_level):
    """每个标签档等距取样，避免只测开头几笔，结果可复现。"""
    by_level = defaultdict(list)
    for row in rows:
        level = row.get("模拟风险等级", "")
        if level:
            by_level[level].append(row)
    picked = []
    for level in sorted(by_level):
        group = by_level[level]
        if per_level <= 0:
            picked.extend(group)
            continue
        step = max(1, len(group) // per_level)
        chosen = [group[i * step] for i in range(min(per_level, len(group)))]
        picked.extend({row["交易ID"]: row for row in chosen}.values())
    return picked


def run_case(app, row, name, template_path, retries=2):
    context = app.prepare_context({"客户ID": row["客户ID"], "交易ID": row["交易ID"]})
    prompt = app.build_prompt(context, template_path)
    engine = app.evaluate_rule_engine(context)
    # ModelScope 端点在并发下会瞬时限流，失败后退避重试，避免整轮样本被误判成"解析失败"。
    started = time.time()
    mode, content, error = "error", "", None
    for attempt in range(retries + 1):
        mode, content, error = app.call_llm(prompt["user"], prompt["system"])
        if mode == "llm":
            break
        if attempt < retries:
            time.sleep(5 * (attempt + 1))
    elapsed = time.time() - started
    decision = app.parse_decision(content) if mode == "llm" else {}
    return {
        "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
        "template": name,
        "txn_id": row["交易ID"],
        "label": row.get("模拟风险等级", ""),
        "mode": mode,
        "error": error,
        "elapsed": round(elapsed, 1),
        "prompt_chars": len(prompt["text"]),
        "risk_level": decision.get("risk_level", ""),
        "rule_only_level": decision.get("rule_only_level", ""),
        "confidence": decision.get("confidence"),
        "rules_hit": decision.get("rules_hit", []),
        "rule_check": decision.get("rule_check", []),
        "reasons": decision.get("reasons", []),
        "engine_level": engine.get("risk_level", ""),
        "engine_rules_hit": engine.get("rules_hit", []),
    }


def summarize(name, records):
    ok = [r for r in records if r["mode"] == "llm" and r["risk_level"] not in ("", "无法判定")]
    ok_ids = {id(r) for r in ok}
    correct = [r for r in ok if r["risk_level"] == r["label"]]
    recall = Counter()
    labelled = Counter()
    for r in records:
        labelled[r["label"]] += 1
        if id(r) in ok_ids and r["risk_level"] == r["label"]:
            recall[r["label"]] += 1
    checks = [len(r["rule_check"]) for r in ok]
    conf = [r["confidence"] for r in ok if isinstance(r["confidence"], (int, float))]
    print(f"\n=== {name} ===")
    print(f"  有效返回 {len(ok)}/{len(records)}  JSON不可解析 {len(records) - len(ok)}")
    print(f"  等级准确 {len(correct)}/{len(ok)} = "
          f"{(len(correct) / len(ok) * 100 if ok else 0):.1f}%")
    print("  分档召回: " + ", ".join(
        f"{lv} {recall[lv]}/{labelled[lv]}" for lv in sorted(labelled)))
    if checks:
        print(f"  rule_check 条数: 中位 {statistics.median(checks):.0f}，"
              f"覆盖 R2-R7 全六条的 {sum(1 for c in checks if c >= 6)}/{len(checks)}")
    if conf:
        print(f"  confidence: 中位 {statistics.median(conf):.2f}，最低 {min(conf):.2f}")
    print(f"  耗时: 中位 {statistics.median([r['elapsed'] for r in records]):.1f}s，"
          f"最长 {max(r['elapsed'] for r in records):.1f}s")
    print("  与标签不一致:")
    for r in records:
        if r["mode"] != "llm":
            print(f"    {r['txn_id']} 接口失败 {r['error']}")
        elif r["risk_level"] != r["label"]:
            print(f"    {r['txn_id']} 标签={r['label']} 模型={r['risk_level'] or '解析失败'}"
                  f" 规则={','.join(r['rules_hit']) or '无'}"
                  f" 规则独立={r['rule_only_level'] or '-'} 引擎={r['engine_level']}")
    return len(correct), len(ok)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-level", type=int, default=4, help="每个标签档取样数，0=全部")
    parser.add_argument("--templates", default="v2,v3", help="逗号分隔：v1,v2,v3")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=2, help="接口报错后的退避重试次数")
    parser.add_argument("--labels", default=str(BASE_DIR / "data" / "sample_transaction_labels.csv"))
    parser.add_argument("--out", default=str(BASE_DIR / "logs" / "ab_prompt.jsonl"))
    args = parser.parse_args()

    app = load_app()
    cfg = app.get_llm_config()
    if not cfg["configured"]:
        print("未配置 LLM_API_KEY，无法跑真实对比；先看规则引擎：scripts/compare_rule_engine.py")
        return 1
    with open(args.labels, newline="", encoding="utf-8-sig") as f:
        rows = [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(f)]
    cases = pick_cases(rows, args.per_level)
    variants = []
    for name in [n.strip() for n in args.templates.split(",") if n.strip()]:
        path = app.BASE_DIR / (f"prompt_template.md" if name == "v3" else f"prompt_template.{name}.md")
        if not path.exists():
            print(f"模板不存在: {path}")
            return 1
        variants.append((name, path))
    print(f"模型 {cfg['model']} 温度 {cfg['temperature']}｜样本 {len(cases)} 笔 × 模板 {len(variants)} 个"
          f"｜并发 {args.workers}")
    for name, path in variants:
        print(f"  - {name}: {path.name} ({path.stat().st_size} 字节)")

    jobs = [(row, name, path) for name, path in variants for row in cases]
    results = defaultdict(list)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_case, app, row, name, path, args.retries): (name, row["交易ID"])
                   for row, name, path in jobs}
        for future in concurrent.futures.as_completed(futures):
            name, txn_id = futures[future]
            record = future.result()
            results[name].append(record)
            print(f"  [{name}] {txn_id} 标签={record['label']} -> {record['risk_level'] or '失败'}"
                  f" ({record['elapsed']}s)")
    for name, path in variants:
        results[name].sort(key=lambda r: r["txn_id"])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        for name, _ in variants:
            for record in results[name]:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    scores = {}
    for name, _ in variants:
        scores[name] = summarize(name, results[name])
    names = [n for n, _ in variants]
    if len(names) == 2:
        (a_ok, a_total), (b_ok, b_total) = scores[names[0]], scores[names[1]]
        if a_total and b_total:
            print(f"\n准确率差值: {names[1]} - {names[0]} = "
                  f"{(b_ok / b_total - a_ok / a_total) * 100:+.1f} 个百分点")
    print(f"\n明细已追加到 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
