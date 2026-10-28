"""Project-owned publication and role checks before admitting a child fragment."""
from .published_workflows import PublishedWorkflowCatalog, binding_id
from .run_budget import validate_run_budget
from .sim_authoring import AuthoringError, compile_document
from .store import canonical
from .subworkflow_reference import read_reference, validate_reference


def validate_admission(authoring, project, parent_workflow, document):
    """Return a bounded depth policy, or None for an ordinary unchanged graph.

    This reads existing ownership/publication records and immutable native
    versions. It never saves, publishes, starts, or changes a task or budget.
    """
    entries = [node for node in document['nodes'] if node['type'] == 'subflow_input']
    if not entries:
        return None
    compile_document(document, parent_workflow)
    authoring._owned(project, parent_workflow)
    try:
        budget = validate_run_budget(authoring.projects[project].get('runBudget'))
    except (ValueError, TypeError, KeyError):
        raise AuthoringError('subworkflow_budget_required') from None
    if budget is None:
        raise AuthoringError('subworkflow_budget_required')
    if not any(node['type'] == 'agent' for node in document['nodes']):
        raise AuthoringError('subworkflow_agent_required')
    references = []
    for entry in entries:
        reference = validate_reference(entry['config']['reference'], project, parent_workflow)
        authoring._owned(project, reference['workflowId'])
        references.append((entry, reference))
    bindings = PublishedWorkflowCatalog(authoring).bindings(frozenset({project}))
    checked = {}
    from .workflow_archive import record as archive_record
    for entry, reference in references:
        key = binding_id(project, reference['workflowId'], reference['deploymentVersionId'])
        binding = bindings.get(key)
        expected_publication = {name: reference[name] for name in
                                ('revision', 'deploymentVersionId', 'graphSha256')}
        if (binding is None or binding['projectId'] != project
                or binding['workflowId'] != reference['workflowId']
                or canonical(binding['publication']) != canonical(expected_publication)):
            raise AuthoringError('subworkflow_source_unpublished')
        if archive_record(authoring.journal, project, reference['workflowId']) is not None:
            raise AuthoringError('subworkflow_source_archived')
        if key not in checked:
            snapshot = read_reference(authoring.clients[project], reference, project=project,
                                      parent_workflow=parent_workflow)
            current_policies = authoring._agent_policies(project, reference['workflowId'], snapshot['document'])
            original_policies = binding.get('externalTasks', {}).get('agentPolicies', {})
            if (current_policies is None
                    or canonical(current_policies) != canonical(original_policies)):
                raise AuthoringError('subworkflow_role_changed')
            checked[key] = snapshot
        if canonical(checked[key]['document']) != canonical(entry['config']['sourceDocument']):
            raise AuthoringError('subworkflow_source_changed')
    return {'depthLimit': 1, 'requiresRunBudget': True, 'referenceCount': len(references)}
