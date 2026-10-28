"""Thin authenticated event ingress; the existing gateway owns durable execution.

This module neither measures time nor schedules work. Schedule callers must send
an explicit occurrence slot from the sole trusted scheduler. Bindings are local,
immutable configuration tied to a published version, never event-controlled.
"""
import copy
from datetime import datetime
import hashlib
import re
import json
import math
import time

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .store import AdmissionClosed, Conflict, canonical
from .workflows import StartRequest


class WebhookEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    eventId: str = Field(min_length=1, max_length=1024)
    input: dict

    @field_validator('eventId')
    @classmethod
    def stable_event_id(cls, value):
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('Stable event identity required')
        return value


class ScheduleEvent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    slot: str = Field(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:00Z$')
    input: dict

    @field_validator('slot')
    @classmethod
    def utc_minute(cls, value):
        # Canonical UTC only: one spelling per occurrence, independent of the
        # receiver's arrival time, timezone, restart, or daylight saving time.
        datetime.strptime(value, '%Y-%m-%dT%H:%M:00Z')
        return value


class EventLookup(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    eventId: str = Field(min_length=1, max_length=1024)


def event_key(trigger_id, kind, event_id):
    """Bounded, domain-separated identity; business input remains unhashed here."""
    digest = hashlib.sha256(canonical([trigger_id, kind, event_id]).encode()).hexdigest()
    return 'trigger-v1-' + digest


class TriggerAPI:
    def __init__(self, workflows, bindings, *, projects):
        if not isinstance(bindings, dict):
            raise ValueError('Trigger bindings must be an object')
        for trigger_id, binding in bindings.items():
            if (not isinstance(trigger_id, str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{1,128}', trigger_id) is None
                    or not isinstance(binding, dict)
                    or set(binding) != {'projectId', 'bindingId', 'type'}
                    or not isinstance(binding['projectId'], str)
                    or binding['projectId'] not in projects
                    or not isinstance(binding['bindingId'], str)
                    or re.fullmatch(r'[A-Za-z0-9_-]{1,160}', binding['bindingId']) is None
                    or binding['type'] not in ('webhook', 'schedule')):
                raise ValueError('Invalid trusted trigger binding')
        self.workflows = workflows
        self.bindings = copy.deepcopy(bindings)
        self.projects = copy.deepcopy(projects)

    def _trigger(self, trigger_id, scopes):
        trigger = self.bindings.get(trigger_id)
        if (trigger is None or trigger['projectId'] not in scopes
                or self.projects[trigger['projectId']].get('enabled', True) is not True):
            raise HTTPException(404, '触发器不存在')
        return trigger

    @staticmethod
    def _verified_record(row, *, expected=None):
        try:
            if (row['status'] not in ('intent', 'accepted', 'rejected', 'uncertain')
                    or type(row['updated']) not in (int, float)
                    or not math.isfinite(row['updated']) or row['updated'] < 0
                    or canonical(json.loads(row['input'])) != row['input']
                    or (row['receipt'] is not None and canonical(json.loads(row['receipt'])) != row['receipt'])):
                raise ValueError
            if expected is not None and (row['status'] != 'accepted'
                                         or row['input'] != expected or row['receipt'] != expected):
                raise ValueError
        except (ValueError, TypeError, RecursionError):
            raise Conflict('Trigger event record cannot be verified') from None

    def lookup(self, trigger_id, event_id, scopes):
        trigger = self._trigger(trigger_id, scopes)
        if trigger['type'] == 'schedule':
            ScheduleEvent(slot=event_id, input={})
        else:
            WebhookEvent(eventId=event_id, input={})
        binding = self.workflows.binding_for(trigger['bindingId'], frozenset({trigger['projectId']}))
        if 'publication' not in binding or binding['projectId'] != trigger['projectId']:
            raise Conflict('Trigger does not reference a fixed publication')
        run = self.workflows.run_id_for(trigger['bindingId'], binding, event_key(trigger_id, trigger['type'], event_id))
        journal = self.workflows.journal
        namespace = journal.trigger_namespace(self.workflows.installation_id, trigger_id)
        result = {'source': 'local_trigger_event_lookup', 'readOnly': True, 'triggerId': trigger_id,
                  **trigger, 'lookupStatus': 'definition_missing', 'runId': None,
                  'submissionStatus': None, 'safeToResubmit': False}
        with journal.connect() as db:
            db.execute('BEGIN')
            frozen = db.execute("SELECT * FROM operations WHERE workflow=? AND run='configuration' AND context='author-trigger-binding'",
                                (namespace,)).fetchone()
            if frozen is not None:
                self._verified_record(frozen, expected=canonical(trigger))
                pin = db.execute("SELECT * FROM operations WHERE workflow=? AND run=? AND context='run-publication'",
                                 (binding['workflowId'], run)).fetchone()
                root = db.execute("SELECT * FROM operations WHERE workflow=? AND run=? AND context=''",
                                  (binding['workflowId'], run)).fetchone()
                if pin is not None:
                    self._verified_record(pin, expected=canonical(self.workflows.publication_snapshot(trigger['bindingId'], binding)))
                if root is not None:
                    if pin is None:
                        raise Conflict('Trigger root has no matching publication')
                    self._verified_record(root)
                    result.update(lookupStatus='recorded', runId=run, submissionStatus=root['status'])
                else:
                    result.update(lookupStatus='root_missing', runId=run)
        return {**result, 'observedAt': time.time()}

    def _start(self, trigger_id, kind, event_id, inputs, scopes):
        trigger = self._trigger(trigger_id, scopes)
        if trigger['type'] != kind:
            raise HTTPException(409, '事件类型与已配置触发器不一致；未启动流程')
        # Narrow the shared authorization to the fixed project even if the
        # caller has several projects. Never accept a payload-selected binding.
        authorized = frozenset({trigger['projectId']})
        binding = self.workflows.binding_for(trigger['bindingId'], authorized)
        if 'publication' not in binding:
            raise HTTPException(503, '触发器必须绑定已核实的发布版本；未启动流程')
        try:
            self.workflows.journal.reserve_trigger_binding(
                self.workflows.installation_id, trigger_id, trigger)
        except AdmissionClosed:
            raise HTTPException(503, '维护或恢复核对中，暂不登记新的触发器定义') from None
        except Conflict:
            raise HTTPException(409, '触发器定义已冻结；变更项目、发布版本或类型请使用新的触发器标识') from None
        return self.workflows.start(trigger['bindingId'], StartRequest(
            idempotencyKey=event_key(trigger_id, kind, event_id), input=inputs), authorized)

    def router(self, authorize):
        router = APIRouter(prefix='/extension/v1/triggers')

        @router.get('')
        def directory(after: str | None = Query(default=None, min_length=1, max_length=128, pattern=r'^[A-Za-z0-9_-]+$'),
                      limit: int = Query(default=25, ge=1, le=100), scopes=Depends(authorize)):
            visible = [(key, value) for key, value in sorted(self.bindings.items())
                       if value['projectId'] in scopes
                       and self.projects[value['projectId']].get('enabled', True) is True]
            if after is not None and after not in {key for key, _ in visible}:
                raise HTTPException(404, '触发入口分页记录不可用')
            remaining = [(key, value) for key, value in visible if after is None or key > after]
            page = [{'triggerId': key, **value, 'state': 'configured'} for key, value in remaining[:limit]]
            return JSONResponse({'source': 'configured_trigger_directory', 'configured': True, 'triggers': page,
                    'nextCursor': page[-1]['triggerId'] if len(remaining) > limit else None},
                    headers={'Cache-Control': 'no-store'})

        @router.post('/{trigger_id}/lookup')
        def lookup(trigger_id: str, body: EventLookup, scopes=Depends(authorize)):
            try:
                return JSONResponse(self.lookup(trigger_id, body.eventId, scopes),
                                    headers={'Cache-Control': 'no-store'})
            except Conflict:
                raise HTTPException(409, '触发器定义或事件记录无法对应，请保留原事件标识核对') from None
            except (ValueError, TypeError, RecursionError):
                raise HTTPException(422, '事件标识无效；定时事件须使用规范UTC分钟标识') from None

        @router.post('/{trigger_id}/webhook', status_code=202)
        def webhook(trigger_id: str, body: WebhookEvent, scopes=Depends(authorize)):
            return self._start(trigger_id, 'webhook', body.eventId, body.input, scopes)

        @router.post('/{trigger_id}/schedule', status_code=202)
        def schedule(trigger_id: str, body: ScheduleEvent, scopes=Depends(authorize)):
            return self._start(trigger_id, 'schedule', body.slot, body.input, scopes)

        return router
