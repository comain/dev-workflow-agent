"""Process A: start the workflow, hit the gate, then EXIT.

The exit is the point. If this process can die while the workflow waits, the
worker is genuinely released and a gate costs nothing while a human thinks.
"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from durable_gate.graph import build_graph

db, thread = sys.argv[1], sys.argv[2]
graph = build_graph(db)
config = {"configurable": {"thread_id": thread}}

result = graph.invoke({"task": "add SSE progress endpoint"}, config=config)

snap = graph.get_state(config)
print(json.dumps({
    "process": os.getpid(),
    "interrupted": bool(snap.next),
    "next_nodes": list(snap.next),
    "interrupt_payload": result.get("__interrupt__")[0].value if result.get("__interrupt__") else None,
    "state_keys": sorted(snap.values.keys()),
    "worker_trace": snap.values.get("worker_trace"),
}, indent=2, default=str))
