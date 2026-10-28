"""Explicit local initialization and authenticated adapter service entry point."""

import argparse
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import os
from pathlib import Path

from .service import ServiceConfig, create_app
from .store import TaskStore
from .execution_runtime import _json


def load_app(config_path):
    path = Path(config_path).resolve(strict=True)
    mode = path.stat()
    if mode.st_uid != os.getuid() or mode.st_mode & 0o077:
        raise ValueError("Service configuration must be owner-only (0600)")
    data = _json(path)
    expected = {"schemaVersion", "database", "tokens", "projects", "bindings"}
    if (
        not isinstance(data, dict)
        or not expected <= set(data) <= expected | {"sim", "executionFile", "approvalFile", "memoryFile", "authoringFile", "taskFlowFile", "verification", "triggerFile", "readinessFile"}
        or type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1
    ):
        raise ValueError("Unsupported service configuration")
    projects, bindings, tokens = data["projects"], data["bindings"], data["tokens"]
    if not all(isinstance(item, dict) for item in (projects, bindings, tokens)):
        raise ValueError("Invalid service registries")
    if 'readinessFile' in data and (
        not isinstance(data['readinessFile'], str) or not data['readinessFile'].strip()
        or data['readinessFile'] != data['readinessFile'].strip()
        or 'executionFile' not in data or 'verification' not in data
    ):
        raise ValueError('Execution readiness requires a proof path, execution binding and verifier')
    for token, scopes in tokens.items():
        if (
            not isinstance(scopes, list)
            or not scopes
            or any(scope not in projects for scope in scopes)
        ):
            raise ValueError("Token scopes must reference configured projects")
    for binding in bindings.values():
        if not isinstance(binding, dict) or binding.get("projectId") not in projects:
            raise ValueError("Binding must reference a configured project")
    database = Path(data["database"])
    if not database.is_absolute():
        database = path.parent / database
    store = TaskStore(database)
    # No config switch may assert a capability whose live proof is absent.
    workflows = None
    if "sim" in data:
        from .adapters.sim import SimClient
        from .sim_journal import SimJournal
        from .workflows import WorkflowGateway

        settings = data["sim"]
        auth_path = (path.parent / settings["authFile"]).resolve(strict=True)
        auth_mode = auth_path.stat()
        if auth_mode.st_uid != os.getuid() or auth_mode.st_mode & 0o077:
            raise ValueError("Sim credentials must be owner-only")
        auth = _json(auth_path)
        workflow_bindings = settings["bindings"]
        if any(b.get("projectId") not in projects for b in workflow_bindings.values()):
            raise ValueError("Workflow binding must reference a configured project")
        journal = SimJournal(path.parent / settings["journal"])
        client = SimClient(auth["baseUrl"], auth["apiKey"])
        try:
            workflows = WorkflowGateway(
                client,
                journal,
                workflow_bindings,
                installation_id=settings["installationId"],
            )
        except BaseException:
            client.close()
            raise
    gateways = {}
    memories = None
    task_flow = None
    verifier = None
    execution_error = None
    readiness = None
    try:
        if 'verification' in data:
            from .verification_runtime import load_verifier
            verifier = load_verifier(data['verification'], tasks=store, projects=projects)
        if "executionFile" in data:
            from .execution_runtime import ExecutionCompatibilityError, load_execution_gateways

            try:
                gateways = load_execution_gateways(
                    path.parent / data["executionFile"],
                    projects=projects,
                    bindings=bindings,
                )
            except ExecutionCompatibilityError as error:
                execution_error = error
        approvals = None
        if "approvalFile" in data:
            from .approval_runtime import load_approval_api
            if workflows is None:
                raise ValueError("Approval configuration requires Sim")
            approvals = load_approval_api(
                path.parent / data["approvalFile"], projects=projects,
                service_tokens=tokens, client=workflows.client, journal=workflows.journal,
            )
        if approvals is not None:
            workflows.approval_discovery = approvals.discovery
            workflows.publication_verifier = approvals.publication_verifier
        if 'taskFlowFile' in data:
            from .task_flow_runtime import load_task_flow
            if workflows is None:
                raise ValueError('Machine tasks require Sim')
            task_flow = load_task_flow(path.parent / data['taskFlowFile'], tasks=store, projects=projects,
                                       role_bindings=bindings, client=workflows.client, journal=workflows.journal)
            workflows.task_flow = task_flow
            if workflows.publication_verifier is None:
                workflows.publication_verifier = task_flow.discovery.publication_verifier
        if "memoryFile" in data:
            from .memory_runtime import load_memory_api
            memories = load_memory_api(
                path.parent / data["memoryFile"], projects=projects, bindings=bindings,
                service_tokens=tokens, tasks=store,
            )
        authoring = None
        if "authoringFile" in data:
            from .authoring_runtime import load_authoring_api
            if workflows is None:
                raise ValueError("Authoring configuration requires Sim")
            authoring = load_authoring_api(
                path.parent / data["authoringFile"], projects=projects,
                service_tokens=tokens, client=workflows.client, journal=workflows.journal,
                role_bindings=bindings,
            )
        triggers = None
        if 'triggerFile' in data:
            from .trigger_runtime import load_trigger_api
            if workflows is None:
                raise ValueError('Trigger configuration requires Sim')
            if not isinstance(data['triggerFile'], str) or not data['triggerFile']:
                raise ValueError('Trigger configuration path required')
            triggers = load_trigger_api(path.parent / data['triggerFile'], projects=projects, workflows=workflows)
        from .dispatcher import Dispatcher

        if 'readinessFile' in data and execution_error is None:
            from .execution_readiness import evaluate_readiness
            if (not isinstance(data['readinessFile'], str) or not data['readinessFile']
                    or not gateways or verifier is None):
                raise ValueError('Execution readiness requires gateways and an automatic verifier')
            readiness = evaluate_readiness(path.parent / data['readinessFile'],
                root=Path(__file__).resolve().parents[1], projects=projects, bindings=bindings,
                gateways=gateways, tasks=store, verification_directory=verifier.directory)
            gateways = {key: replace(gateway, readiness_check=readiness.assert_current)
                        for key, gateway in gateways.items()}

        # Validate every configured local registry/store above even when the remote
        # descriptor is incompatible. Never mask a local configuration/data error.
        if execution_error is not None:
            task_flow = None
            if workflows is not None:
                workflows.task_flow = None
        app = create_app(
            store,
            ServiceConfig(
                tokens={key: set(scopes) for key, scopes in tokens.items()},
                projects=projects,
                bindings=bindings,
                controlled_execution=('unsupported' if execution_error.code == 'binding_incompatible'
                    else 'unknown') if execution_error is not None else
                    'confirmed' if readiness is not None and readiness.confirmed else 'unknown',
                execution_block_reason=str(execution_error) if execution_error is not None else None,
            ),
            workflows=workflows,
            approvals=approvals,
            memories=memories,
            authoring=authoring,
            task_flow=task_flow,
            verifier=verifier,
            triggers=triggers,
            execution_readiness=readiness,
            dispatcher=Dispatcher(store, gateways) if gateways and execution_error is None else None,
        )
    except BaseException:
        if memories is not None:
            memories.close()
        for gateway in gateways.values():
            gateway.close()
        if workflows is not None:
            workflows.client.close()
        raise
    from .approval_runtime import ApprovalDiscoveryWorker
    discovery_worker = (ApprovalDiscoveryWorker(approvals.discovery)
                        if execution_error is None and approvals is not None
                        and approvals.discovery is not None else None)
    app.state.approval_discovery_worker = discovery_worker
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application) as state:
                try:
                    if discovery_worker is not None:
                        discovery_worker.start()
                    yield state
                finally:
                    if discovery_worker is not None:
                        await asyncio.to_thread(discovery_worker.stop)
        finally:
            if (discovery_worker is not None and discovery_worker.thread is not None
                    and discovery_worker.thread.is_alive()):
                raise RuntimeError("Approval discovery transport has not drained")
            worker = app.state.execution_worker
            if task_flow is not None and any(owned.thread is not None and owned.thread.is_alive() for owned in task_flow.workers):
                raise RuntimeError('Machine task workers still own their transport')
            if (
                worker is not None
                and worker.thread is not None
                and worker.thread.is_alive()
            ):
                raise RuntimeError(
                    "Execution worker still owns its transport; shutdown did not drain"
                )
            if memories is not None:
                memories.close()
            for gateway in gateways.values():
                gateway.close()
            if workflows is not None:
                workflows.client.close()

    app.router.lifespan_context = lifespan
    from .workbench import mount_workbench

    mount_workbench(app, Path(__file__).resolve().parent.parent / "frontend")
    return app


def main():
    parser = argparse.ArgumentParser(description="Agent Line 适配服务")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init", help="显式创建任务数据库；不会覆盖已有文件")
    init.add_argument("--database", type=Path, required=True)
    serve = sub.add_parser("serve", help="启动认证适配服务")
    serve.add_argument("--config", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=3193)
    args = parser.parse_args()
    if args.command == "init":
        TaskStore.initialize(args.database)
        print("任务数据库已初始化")
    else:
        import uvicorn

        uvicorn.run(
            load_app(args.config), host=args.host, port=args.port, access_log=False
        )


if __name__ == "__main__":
    main()
