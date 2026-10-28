#!/usr/bin/env python3
"""Explicit initialization/configuration for the isolated G0 execution bridge."""
import importlib.util
import os
from pathlib import Path
import secrets

ROOT=Path(__file__).resolve().parents[2]
os.environ['QWENPAW_WORKING_DIR']=str(ROOT/'var/qwenpaw')
os.environ['QWENPAW_SECRET_DIR']=str(ROOT/'var/qwenpaw-secrets')
from qwenpaw.config.utils import load_config, save_config
spec=importlib.util.spec_from_file_location('agent_line_bridge_init',ROOT/'plugins/agent-line/execution_bridge.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
db=ROOT/'var/qwenpaw/agent-line-execution-receipts.sqlite3'
if not db.exists(): module.ReceiptLedger.initialize(db)
else:
    module.ReceiptLedger.migrate(db)
    module.ReceiptLedger(db)
token=ROOT/'var/qwenpaw-secrets/execution-service-token'
if not token.exists():
    fd=os.open(token,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream: stream.write(secrets.token_urlsafe(48))
config=load_config()
config.plugins['agent-line']={**config.plugins.get('agent-line',{}),'managed_role_ids':['agent-line-g0-parent'],'codex_target_agent_id':'agent-line-g0-codex','execution_bridge':{
    'receipt_db':str(db),'service_token_file':str(token),
    'target_agent_id':'agent-line-g0-codex',
    'workspace':str(ROOT/'var/qwenpaw/workspaces/agent-line-g0-codex'),
    'codex_home':str(ROOT/'var/codex-g0'),'timeout_seconds':3600,
}}
save_config(config)
print('Configured isolated bridge; receipt ledger preserved; secret not printed.')
