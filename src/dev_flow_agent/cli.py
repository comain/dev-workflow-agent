"""serve / worker / dev."""

from __future__ import annotations

import argparse
import threading

from agent_core.identity import assert_trusted_deployment
from agent_core.runtime import DaemonConfig, DaemonPorts, TaskDaemon

from dev_flow_agent.app import create_app
from dev_flow_agent.config import Settings
from dev_flow_agent.tasks import TaskQueue
from dev_flow_agent.worker import Worker


def _settings() -> Settings:
    return Settings()


def serve(settings: Settings, host: str, port: int) -> None:
    import uvicorn

    assert_trusted_deployment(
        bind_host=host, trust_proxy_headers=False, proxy_is_fronting=False
    )
    app = create_app(settings)
    uvicorn.run(app, host=host, port=port)


def worker_loop(settings: Settings) -> None:
    from dev_flow_agent.app import create_app

    app = create_app(settings)
    queue = TaskQueue(app.state.tasks, app.state.runtime)
    worker = Worker(settings, app.state.tasks, app.state.runtime, app.state.artifacts)
    TaskDaemon(
        app.state.runtime,
        DaemonPorts(
            claim=queue.claim,
            execute=worker.execute,
            renew_lease=queue.renew_lease,
            release=queue.release,
        ),
        config=DaemonConfig(),
    ).run_forever()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="dev-flow-agent")
    parser.add_argument("command", choices=["serve", "worker", "dev"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--allow-anonymous-gates", action="store_true")
    args = parser.parse_args(argv)
    settings = _settings()
    if args.allow_anonymous_gates:
        settings = settings.model_copy(update={"allow_anonymous_gates": True})
    if args.command == "worker":
        worker_loop(settings)
        return
    if args.command == "dev":
        thread = threading.Thread(target=worker_loop, args=(settings,), daemon=True)
        thread.start()
    serve(settings, args.host, args.port)


if __name__ == "__main__":
    main()
