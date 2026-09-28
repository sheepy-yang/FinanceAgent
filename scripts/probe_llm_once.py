#!/usr/bin/env python3
"""单发探针：确认模型接口在低并发下是否可用，并把原始返回写到 /tmp。"""
import importlib.util
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent_app", BASE_DIR / "app.py")
app = importlib.util.module_from_spec(spec)
sys.modules["agent_app"] = app
spec.loader.exec_module(app)

txn = sys.argv[1] if len(sys.argv) > 1 else "TXN2026090037"
label = {r["交易ID"]: r["模拟风险等级"] for r in
         __import__("csv").DictReader(open(BASE_DIR / "data" / "sample_transaction_labels.csv", encoding="utf-8-sig"))}
ctx = app.prepare_context({"交易ID": txn})
prompt = app.build_prompt(ctx)
started = time.time()
mode, content, error = app.call_llm(prompt["user"], prompt["system"])
elapsed = time.time() - started
decision = app.parse_decision(content)
print(f"{txn} 标签={label.get(txn)} mode={mode} 耗时={elapsed:.1f}s error={error}")
print("等级:", decision.get("risk_level"), "| 规则独立:", decision.get("rule_only_level"),
      "| 置信:", decision.get("confidence"), "| rules_hit:", decision.get("rules_hit"))
print("rule_check 条数:", len(decision.get("rule_check") or []))
for line in (decision.get("rule_check") or [])[:6]:
    print("  ", line)
Path("/tmp/probe_raw.json").write_text(json.dumps(
    {"mode": mode, "error": error, "elapsed": elapsed, "raw": content[:6000],
     "decision": decision}, ensure_ascii=False, indent=2), encoding="utf-8")
