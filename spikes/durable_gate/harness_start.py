import json, os, pathlib, sys, tempfile
sys.path.insert(0, "spikes")
from durable_gate.harness_flow import build_harness_graph
key = [l.split("=",1)[1].strip() for l in open("agent-core/.env.local") if "=" in l][0]
repo = tempfile.mkdtemp(); pathlib.Path(repo, "README.md").write_text("spike\n")
g = build_harness_graph("/tmp/spike_harness.db")
cfg = {"configurable": {"thread_id": "hflow-1"}}
r = g.invoke({"repo": repo, "key": key}, config=cfg)
s = g.get_state(cfg)
print(json.dumps({"pid": os.getpid(), "gated_at": list(s.next),
  "proposal_len": len(s.values.get("proposal") or ""),
  "proposal_head": (s.values.get("proposal") or "")[:110],
  "trace": s.values.get("trace")}, indent=2))
