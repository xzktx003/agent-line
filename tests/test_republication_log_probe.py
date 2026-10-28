import copy
import unittest

from agent_line.published_workflows import graph_hash
from agent_line.sim_authoring import compile_document
from probes.full_workflow.republication import document, verify_execution_log


class RepublicationLogProofTests(unittest.TestCase):
    def setUp(self):
        config=dict(roleBindingId='role',instruction='Write the declared verification artifact only.',
                    inputFrom=dict(nodeId='start',field='input'),memoryPolicyId='native-role',
                    outputSchemaId='task-result',requireMemoryCommit=False,allowFailureBranch=False)
        self.doc=document(config,'original_template',False)
        state=compile_document(self.doc,'workflow')
        state.update(loops={},parallels={})
        self.publication=dict(revision=1,deploymentVersionId='old-deployment',graphSha256=graph_hash(state,'workflow'))
        self.definition=dict(id='old-deployment',version=1,isActive=False,state=state)
        self.output=dict(templateMarker='original_template',allowFailureBranch=False)
        projection={key:copy.deepcopy(state[key]) for key in ('blocks','edges','loops','parallels')}
        for edge in projection['edges']:
            edge.setdefault('sourceHandle',None)
            edge.setdefault('targetHandle',None)
        self.log=dict(runId='old-root',workflowId='workflow',deploymentVersionId='old-deployment',
                      status='completed',workflowState=projection,finalOutput=dict(data=copy.deepcopy(self.output)))

    def verify(self):
        return verify_execution_log(self.log,'workflow','old-root',self.publication,self.output,self.definition)

    def test_actual_root_projection_allows_null_handles_and_inactive_definition(self):
        self.assertIs(self.verify(),True)

    def test_root_identity_must_be_exact(self):
        for key in ('runId','workflowId','deploymentVersionId'):
            original=self.log[key]
            self.log[key]='different'
            with self.subTest(key=key),self.assertRaises(RuntimeError):self.verify()
            self.log[key]=original

    def test_completed_child_cannot_replace_unfinished_root_proof(self):
        self.log['status']='paused'
        with self.assertRaisesRegex(RuntimeError,'native_root_not_completed'):self.verify()

    def test_new_instruction_in_old_deployment_graph_rejected(self):
        changed=copy.deepcopy(self.doc)
        agent=next(node for node in changed['nodes'] if node['type']=='agent')
        agent['config']['instruction']='A different instruction from the new template.'
        self.log['workflowState']['blocks']=compile_document(changed,'workflow')['blocks']
        with self.assertRaisesRegex(RuntimeError,'actual_native_graph_changed'):self.verify()

    def test_changed_branch_handle_is_not_normalized_away(self):
        edge=next(edge for edge in self.log['workflowState']['edges'] if edge.get('sourceHandle')=='condition-passed')
        edge['sourceHandle']='condition-else'
        with self.assertRaisesRegex(RuntimeError,'actual_native_graph_changed'):self.verify()

    def test_unknown_edge_metadata_is_not_discarded(self):
        self.log['workflowState']['edges'][0]['unknownPolicy']=True
        with self.assertRaisesRegex(RuntimeError,'actual_native_graph_changed'):self.verify()

    def test_latest_definition_cannot_be_substituted_for_original(self):
        for key,value in (('id','new-deployment'),('version',2),('version',True)):
            original=self.definition[key]
            self.definition[key]=value
            with self.subTest(key=key,value=value),self.assertRaises(RuntimeError):self.verify()
            self.definition[key]=original

    def test_mismatched_final_output_does_not_count_as_old_snapshot_success(self):
        self.log['finalOutput']['data']['templateMarker']='new_template'
        with self.assertRaisesRegex(RuntimeError,'native_root_final_output_mismatch'):self.verify()

    def test_missing_graph_fields_are_not_inferred(self):
        self.log['workflowState'].pop('edges')
        with self.assertRaisesRegex(RuntimeError,'native_log_graph_incomplete'):self.verify()

    def test_unsupported_variables_cannot_be_assumed_empty(self):
        self.definition['state']['variables']={'secret':'not_empty'}
        with self.assertRaisesRegex(RuntimeError,'unsupported_probe_variables'):self.verify()


if __name__=='__main__':
    unittest.main()
