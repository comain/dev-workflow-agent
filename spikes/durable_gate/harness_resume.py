import json, os, sys
sys.path.insert(0, "spikes")
from durable_gate.harness_flow import build_harness_graph
from langgraph.types import Command
g = build_harness_graph("/tmp/spike_harness.db")
cfg = {"configurable": {"thread_id": "hflow-1"}}
f = g.invoke(Command(resume={"decision":"approve","comments":"must survive a worker restart"}), config=cfg)
s = g.get_state(cfg)
print(json.dumps({"pid": os.getpid(), "still_gated": bool(s.next),
  "decision": f.get("review_decision"),
  "revision_head": (f.get("revision") or "")[:130],
  "trace": f.get("trace")}, indent=2))
