"""Strict condition projection for trusted human approval resume results."""
import copy
import json
import subprocess
import unittest

from agent_line.sim_authoring import AuthoringError, UnsupportedDocument, compile_document, decode_document, native_id


def document():
    def node(i,t,c):return {'id':i,'type':t,'name':i,'position':{'x':0,'y':0},'config':c}
    return {'schemaVersion':1,'nodes':[node('start','start',{}),node('human','human',{'requiredBoolean':'approved'}),node('gate','condition',{'rules':[{'id':'yes','source':'human','field':'result.approval.approved','operator':'eq','value':True}]}),node('yes','end',{'output':{'approved':True}}),node('no','end',{'output':{'approved':False}})],'edges':[{'id':'a','source':'start','target':'human'},{'id':'b','source':'human','target':'gate'},{'id':'c','source':'gate','target':'yes','branch':'yes'},{'id':'d','source':'gate','target':'no','branch':'else'}],'variables':{}}


class HumanConditionTests(unittest.TestCase):
    def expression(self,doc):
        state=compile_document(doc,'workflow')
        return json.loads(state['blocks'][native_id('workflow','gate')]['subBlocks']['conditions']['value'])[0]['value']

    def test_boolean_eq_ne_roundtrip_and_actual_node_execution(self):
        for operator in ('eq','ne'):
            for literal in (True,False):
                doc=document();doc['nodes'][2]['config']['rules'][0].update(operator=operator,value=literal)
                state=compile_document(doc,'workflow');self.assertEqual(decode_document(state),doc)
                expr=self.expression(doc)
                for approved in (True,False):
                    source=expr.replace('<human.resumeInput>',json.dumps({'approval':{'approved':approved}}))
                    actual=subprocess.run(['node','-e',f'console.log({source})'],text=True,capture_output=True,check=True).stdout.strip()
                    self.assertEqual(actual,str((approved==literal) if operator=='eq' else (approved!=literal)).lower())

    def test_missing_or_non_boolean_approval_throws(self):
        expr=self.expression(document())
        for result in ({},{'approval':{}},{'approval':None},{'approval':{'approved':'true'}},{'approval':{'approved':1}},{'approval':{'approved':None}},None):
            source=expr.replace('<human.resumeInput>',json.dumps(result))
            actual=subprocess.run(['node','-e',f'try {{ console.log({source}); }} catch(e) {{ console.log("ERROR"); }}'],text=True,capture_output=True,check=True).stdout.strip()
            self.assertEqual(actual,'ERROR')

    def test_human_field_operator_and_literal_allowlist(self):
        for update in ({'operator':'gt'},{'operator':'gte'},{'value':1},{'value':'true'},{'value':None},{'field':'result.output.approved'},{'field':'result.approval.other'},{'field':'result.approval.__proto__'},{'field':'result.verificationStatus'},{'source':'start'}):
            doc=document();doc['nodes'][2]['config']['rules'][0].update(update)
            with self.subTest(update=update),self.assertRaises(AuthoringError):compile_document(doc,'workflow')

    def test_future_and_bypass_human_are_not_dominators(self):
        doc=document();doc['edges']=[{'id':'a','source':'start','target':'gate'},{'id':'b','source':'gate','target':'human','branch':'yes'},{'id':'c','source':'human','target':'yes'},{'id':'d','source':'gate','target':'no','branch':'else'}]
        with self.assertRaises(AuthoringError) as caught:compile_document(doc,'workflow')
        self.assertEqual(caught.exception.code,'result_source_not_dominator')
        doc=document();doc['nodes'].append({'id':'entry','type':'condition','name':'entry','position':{'x':0,'y':0},'config':{'rules':[{'id':'go','field':'input.go','operator':'eq','value':True}]}})
        doc['edges'][0]['target']='entry';doc['edges'] += [{'id':'e','source':'entry','target':'human','branch':'go'},{'id':'f','source':'entry','target':'gate','branch':'else'}]
        with self.assertRaises(AuthoringError) as caught:compile_document(doc,'workflow')
        self.assertEqual(caught.exception.code,'result_source_not_dominator')

    def test_end_agent_sources_and_agent_condition_fields_not_widened(self):
        doc=document();doc['nodes'][3]['config']={'resultFrom':'human'}
        with self.assertRaises(AuthoringError):compile_document(doc,'workflow')
        agent={'roleBindingId':'role','instruction':'work','inputFrom':{'nodeId':'start','field':'input'},'memoryPolicyId':'native','outputSchemaId':'result','requireMemoryCommit':False,'allowFailureBranch':False}
        doc=document();doc['nodes'][1].update(type='agent',config=copy.deepcopy(agent))
        with self.assertRaises(AuthoringError):compile_document(doc,'workflow')
        doc=document();agent['inputFrom']={'nodeId':'human','field':'output'}
        doc['nodes'][2].update(type='agent',config=agent);doc['nodes'].pop();doc['edges'].pop();doc['edges'][-1].pop('branch')
        with self.assertRaises(AuthoringError):compile_document(doc,'workflow')

    def test_tampered_native_expression_is_not_editable(self):
        state=compile_document(document(),'workflow')
        field=state['blocks'][native_id('workflow','gate')]['subBlocks']['conditions']
        field['value']=field['value'].replace('=== true','!== true')
        with self.assertRaises(UnsupportedDocument):decode_document(state)
