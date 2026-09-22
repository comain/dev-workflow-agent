"""Process B: a DIFFERENT process resumes the same workflow from the DB alone."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from durable_gate.graph import build_graph
from langgraph.types import Command

db, thread, decision, comments = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
graph = build_graph(db)
config = {"configurable": {"thread_id": thread}}

final = graph.invoke(Command(resume={"decision": decision, "comments": comments}), config=config)
snap = graph.get_state(config)
print(json.dumps({
    "process": os.getpid(),
    "still_interrupted": bool(snap.next),
    "review_decision": final.get("review_decision"),
    "review_comments": final.get("review_comments"),
    "build_log": final.get("build_log"),
    "worker_trace": final.get("worker_trace"),
}, indent=2, default=str))
