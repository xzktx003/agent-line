"""Read-only proofs of Sim published definitions and actual run snapshots."""

import re
import copy

import httpx

from .adapters.sim import segment


class PublicationUnverified(RuntimeError):
    """Bounded diagnostic: never includes upstream bodies or credentials."""

    def __init__(self, code):
        self.code = code
        super().__init__("Sim publication unverified: " + code)


class PublicationVerifier:
    def __init__(self, sim_client):
        self.sim = sim_client

    def _get(self, path, *, params=None):
        try:
            response = self.sim._client.get(
                self.sim.base_url + path, params=params,
                headers={"x-api-key": self.sim._key}, follow_redirects=False)
        except httpx.HTTPError:
            raise PublicationUnverified("unavailable") from None
        if response.status_code != 200:
            raise PublicationUnverified("unavailable")
        try:
            body = response.json()
        except ValueError:
            raise PublicationUnverified("invalid_response") from None
        if (not isinstance(body, dict) or body.get("success") is False
                or "error" in body or not isinstance(body.get("data"), dict)):
            raise PublicationUnverified("invalid_response")
        return body["data"]

    @staticmethod
    def _inputs(workflow_id, revision, deployment_version_id, human_block_ids):
        try:
            segment(workflow_id)
            segment(deployment_version_id)
            if not isinstance(revision, str) or not re.fullmatch(r"[1-9][0-9]{0,14}", revision):
                raise ValueError()
            if (not isinstance(human_block_ids, (list, tuple, frozenset))
                    or not human_block_ids):
                raise ValueError()
            for block in human_block_ids:
                segment(block)
            if len(set(human_block_ids)) != len(human_block_ids):
                raise ValueError()
        except (ValueError, TypeError):
            raise PublicationUnverified("invalid_binding") from None

    @staticmethod
    def _graph(state, human_block_ids, *, workflow_id=None, loop_policies=None, static_parallel=False,
               subworkflow_depth_limit=None):
        if not isinstance(state, dict) or not isinstance(state.get("blocks"), dict):
            raise PublicationUnverified("missing_graph")
        blocks = state["blocks"]
        for block_id in human_block_ids:
            block = blocks.get(block_id)
            if (not isinstance(block, dict) or block.get("id") != block_id
                    or block.get("type") != "human_in_the_loop_v2"
                    or block.get("enabled") is not True):
                raise PublicationUnverified("human_node_mismatch")
        if type(static_parallel) is not bool:
            raise PublicationUnverified('invalid_binding')
        if subworkflow_depth_limit is not None:
            return PublicationVerifier._subworkflow_graph(
                state, human_block_ids, workflow_id, loop_policies, static_parallel,
                subworkflow_depth_limit)
        if PublicationVerifier._has_subworkflow_marker(state):
            raise PublicationUnverified('unsupported_subworkflow_topology')
        if static_parallel:
            if loop_policies is not None:
                raise PublicationUnverified('unsupported_static_parallel_topology')
            return PublicationVerifier._static_graph(state, human_block_ids, workflow_id)
        if loop_policies is not None:
            return PublicationVerifier._loop_graph(state, human_block_ids, workflow_id, loop_policies)
        PublicationVerifier._single_activation(state, human_block_ids)

    @staticmethod
    def _has_subworkflow_marker(state):
        for block in state.get('blocks', {}).values():
            if not isinstance(block, dict) or block.get('type') != 'function':
                continue
            subblocks = block.get('subBlocks', {}) if isinstance(block, dict) else {}
            code = subblocks.get('code') if isinstance(subblocks, dict) else None
            value = code.get('value') if isinstance(code, dict) else None
            if isinstance(value, str) and value.startswith((
                    '/*AL_SUBFLOW_INPUT_V1:', '/*AL_SUBFLOW_OUTPUT_V1:')):
                return True
        return False

    @staticmethod
    def _subworkflow_graph(state, human_block_ids, workflow_id, policies, static_parallel, depth_limit):
        from .sim_authoring import compile_document, decode_document, loop_policies
        try:
            if type(depth_limit) is not int or depth_limit != 1:
                raise ValueError()
            if state.get('parallels') != {}:
                raise ValueError()
            document = decode_document(state)
            if not any(node.get('type') in ('subflow_input', 'subflow_output')
                       for node in document['nodes']):
                raise ValueError()
            has_parallel = any(node.get('type') == 'parallel' for node in document['nodes'])
            if static_parallel is not has_parallel:
                raise ValueError()
            if type(static_parallel) is not bool or (static_parallel and policies is not None):
                raise ValueError()
            compiled = compile_document(document, workflow_id)
            has_loop = any(node.get('type') == 'loop' for node in document['nodes'])
            if has_loop:
                compiled_policies = loop_policies(document, workflow_id)
                if (not isinstance(policies, dict) or compiled_policies != policies
                        or not isinstance(state.get('loops'), dict)
                        or set(state['loops']) != {item['loopId'] for item in policies.values()}):
                    raise ValueError()
            elif state.get('loops') != {} or policies is not None:
                raise ValueError()
            waits = {key for key, block in compiled['blocks'].items()
                     if block.get('type') == 'human_in_the_loop_v2'}
            if waits != set(human_block_ids):
                raise ValueError()
            return compiled
        except (ValueError, TypeError, KeyError, RecursionError):
            raise PublicationUnverified('unsupported_subworkflow_topology') from None

    @staticmethod
    def _static_graph(state, human_block_ids, workflow_id):
        from .sim_authoring import compile_document, decode_document, MAX_STATIC_PARALLEL_GROUPS
        try:
            if state.get('loops') != {} or state.get('parallels') != {}:
                raise ValueError()
            document = decode_document(state)
            if not 1 <= sum(n['type'] == 'parallel' for n in document['nodes']) <= MAX_STATIC_PARALLEL_GROUPS:
                raise ValueError()
            compiled = compile_document(document, workflow_id)
            waits = {key for key, block in compiled['blocks'].items()
                     if block['type'] == 'human_in_the_loop_v2'}
            if waits != set(human_block_ids):
                raise ValueError()
            return compiled
        except (ValueError, TypeError, KeyError, RecursionError):
            raise PublicationUnverified('unsupported_static_parallel_topology') from None

    @staticmethod
    def _loop_graph(state, human_block_ids, workflow_id, policies):
        # Only the losslessly recognized compiler subset may relax the legacy
        # single-activation fence. The decoder verifies the generated function
        # guard's fixed code, enabled state, membership and edges as part of the
        # complete graph; no user function or arbitrary container is adopted.
        from .sim_authoring import compile_document, decode_document, loop_policies
        try:
            if type(policies) is not dict or not policies or len(policies) > 200:
                raise ValueError()
            for body, policy in policies.items():
                segment(body)
                if (type(policy) is not dict or set(policy) != {'loopId', 'maxIterations'}
                        or type(policy['maxIterations']) is not int
                        or not 1 <= policy['maxIterations'] <= 3):
                    raise ValueError()
                segment(policy['loopId'])
            if not set(policies) <= set(human_block_ids):
                raise ValueError()
            if (type(state.get('loops')) is not dict
                    or set(state['loops']) != {policy['loopId'] for policy in policies.values()}
                    or state.get('parallels') != {}):
                raise ValueError()
            document = decode_document(state)
            # Exact compiler reconstruction permits ordinary DAG human pauses.
            # The compiler only permits an Agent plus its generated guard inside
            # a loop; all waits, including humans, remain explicitly declared below.
            compiled = compile_document(document, workflow_id)
            if loop_policies(document, workflow_id) != policies:
                raise ValueError()
            waits = {key for key, value in compiled['blocks'].items()
                     if value.get('type') == 'human_in_the_loop_v2'}
            if waits != set(human_block_ids):
                raise ValueError()
            return compiled
        except (ValueError, TypeError, KeyError, RecursionError):
            raise PublicationUnverified('unsupported_loop_topology') from None

    @staticmethod
    def _single_activation(state, human_block_ids):
        """Reject repeated Human contexts; leave unrelated agent cycles usable."""
        blocks = state["blocks"]
        edges, loops, parallels = (state.get(key) for key in ("edges", "loops", "parallels"))
        if (not isinstance(edges, list) or not isinstance(loops, dict)
                or not isinstance(parallels, dict)):
            raise PublicationUnverified("unsupported_topology")
        adjacency = {key: set() for key in blocks}
        parents = {key: set() for key in blocks}
        for key, block in blocks.items():
            if not isinstance(block, dict) or block.get("id") != key:
                raise PublicationUnverified("unsupported_topology")
            data = block.get("data", {})
            if not isinstance(data, dict):
                raise PublicationUnverified("unsupported_topology")
            parent = data.get("parentId")
            if parent is not None:
                if not isinstance(parent, str) or parent not in blocks:
                    raise PublicationUnverified("unsupported_topology")
                parents[key].add(parent)
        containers = set(loops) | set(parallels)
        for key, block in blocks.items():
            if block.get("type") in ("loop", "parallel") and key not in containers:
                raise PublicationUnverified("unsupported_topology")
        for section in (loops, parallels):
            for container, config in section.items():
                if (container not in blocks or not isinstance(config, dict)
                        or not isinstance(config.get("nodes"), list)):
                    raise PublicationUnverified("unsupported_topology")
                for child in config["nodes"]:
                    if not isinstance(child, str) or child not in blocks:
                        raise PublicationUnverified("unsupported_topology")
                    parents[child].add(container)
        for child, ancestors in parents.items():
            for parent in ancestors:
                if parent not in containers:
                    raise PublicationUnverified("unsupported_topology")
                # Container boundary edges can name the container rather than
                # its members. Conservatively connect both for reachability.
                adjacency[parent].add(child)
                adjacency[child].add(parent)
        for edge in edges:
            if not isinstance(edge, dict):
                raise PublicationUnverified("unsupported_topology")
            source, target = edge.get("source"), edge.get("target")
            if (not isinstance(source, str) or not isinstance(target, str)
                    or source not in blocks or target not in blocks):
                raise PublicationUnverified("unsupported_topology")
            adjacency[source].add(target)
        for human in human_block_ids:
            # Parallel containers also replicate activations; no per-activation
            # identity contract exists for them in this adapter.
            if parents[human]:
                raise PublicationUnverified("repeated_human_activation")
            pending, visited = list(adjacency[human]), set()
            while pending:
                node = pending.pop()
                if node == human:
                    raise PublicationUnverified("repeated_human_activation")
                if node not in visited:
                    visited.add(node)
                    pending.extend(adjacency[node] - visited)

    def check_definition(self, workflow_id, revision, deployment_version_id,
                         human_block_ids, *, require_active=False, loop_policies=None, static_parallel=False,
                         subworkflow_depth_limit=None):
        self._checked_definition(workflow_id, revision, deployment_version_id,
                                 human_block_ids, require_active=require_active,
                                 loop_policies=loop_policies, static_parallel=static_parallel,
                                 subworkflow_depth_limit=subworkflow_depth_limit)
        return True

    def _checked_definition(self, workflow_id, revision, deployment_version_id,
                            human_block_ids, *, require_active=False, loop_policies=None, static_parallel=False,
                            subworkflow_depth_limit=None):
        self._inputs(workflow_id, revision, deployment_version_id, human_block_ids)
        if type(require_active) is not bool:
            raise PublicationUnverified("invalid_binding")
        data = self._get(f"/api/v2/workflows/{workflow_id}/versions/{revision}")
        if (data.get("id") != deployment_version_id
                or type(data.get("version")) is not int
                or data["version"] != int(revision)):
            raise PublicationUnverified("version_mismatch")
        if require_active and data.get("isActive") is not True:
            raise PublicationUnverified("inactive_version")
        return self._graph(data.get("state"), human_block_ids,
                           workflow_id=workflow_id, loop_policies=loop_policies, static_parallel=static_parallel,
                           subworkflow_depth_limit=subworkflow_depth_limit)

    def verify_run(self, snapshot):
        if not isinstance(snapshot, dict):
            raise PublicationUnverified("invalid_binding")
        workflow_id, run_id = snapshot.get("workflowId"), snapshot.get("runId")
        revision, deployment_id = snapshot.get("revision"), snapshot.get("deploymentVersionId")
        blocks = snapshot.get("humanBlockIds")
        self._inputs(workflow_id, revision, deployment_id, blocks)
        try:
            segment(run_id)
        except ValueError:
            raise PublicationUnverified("invalid_binding") from None
        data = self._get(f"/api/v2/logs/{run_id}", params={"includeWorkflowState": "true"})
        if (data.get("runId") != run_id or data.get("workflowId") != workflow_id
                or data.get("deploymentVersionId") != deployment_id):
            raise PublicationUnverified("run_version_mismatch")
        policies = snapshot.get('loopPolicies')
        static_parallel = snapshot.get('staticParallel', False)
        depth_limit = snapshot.get('subworkflowDepthLimit')
        if depth_limit is not None:
            if type(depth_limit) is not int or depth_limit != 1:
                raise PublicationUnverified('invalid_binding')
            from .run_budget import validate_run_budget
            try:
                if validate_run_budget(snapshot.get('runBudget')) is None:
                    raise ValueError()
            except (ValueError, TypeError):
                raise PublicationUnverified('invalid_run_budget') from None
        published = self._checked_definition(workflow_id, revision, deployment_id, blocks,
                                             loop_policies=policies, static_parallel=static_parallel,
                                             subworkflow_depth_limit=depth_limit)
        state = data.get('workflowState')
        if (policies is not None or static_parallel is True or depth_limit == 1) and isinstance(state, dict):
            # Pinned public logs retain the executed graph but can omit the
            # empty variables map and persist absent edge handles as null.
            # Only fill omission after proving the immutable publication empty;
            # explicit null/nonempty variables and nonnull handles stay intact.
            state = copy.deepcopy(state)
            if 'variables' not in state and published.get('variables') == {}:
                state['variables'] = {}
            if isinstance(state.get('edges'), list):
                for edge in state['edges']:
                    if isinstance(edge, dict):
                        for field in ('sourceHandle', 'targetHandle'):
                            if field in edge and edge[field] is None:
                                del edge[field]
        actual = self._graph(state, blocks, workflow_id=workflow_id, loop_policies=policies,
                             static_parallel=static_parallel, subworkflow_depth_limit=depth_limit)
        if (policies is not None or static_parallel is True or depth_limit == 1) and actual != published:
            raise PublicationUnverified('run_graph_mismatch')
        return True
