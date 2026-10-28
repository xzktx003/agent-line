"""Bounded delivery polling for existing pause bindings; Sim alone schedules graphs."""
import math
import threading

from .store import AdmissionClosed


def _interval(value):
    if type(value) not in (float, int) or not math.isfinite(value) or value <= 0:
        raise ValueError('Polling interval must be finite and positive')


class CompletionLoop:
    def __init__(self, store, bindings, relay, *, batch_size=50):
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError('Batch size must be 1..500')
        self.store, self.bindings, self.relay = store, bindings, relay
        self.batch_size = batch_size
        self.cursor = ''

    def tick(self):
        bindings = self.bindings.page(after=self.cursor, limit=self.batch_size)
        results = []
        for binding in bindings:
            self.cursor = binding.task_id
            try:
                task = self.store.get(binding.task_id)
                try:
                    decisions = self.store.approval_decisions(binding.task_id)
                except ValueError:
                    decisions = None
                if decisions is not None:
                    task = {**task, 'approvalDecisions': decisions}
                result = self.relay.deliver(task, binding)
                status = result.get('status')
                if status not in ('waiting', 'intent', 'accepted', 'uncertain', 'rejected'):
                    results.append({'taskId': binding.task_id, 'status': 'error', 'reason': 'invalid_delivery_result'})
                else:
                    item = {'taskId': binding.task_id, 'status': status}
                    if result.get('reason') in (
                            'declared_output_needs_human', 'verification_failure_needs_human',
                            'permission_denied_needs_human', 'execution_failure_needs_human',
                            'execution_interrupted_needs_human', 'stale_activation_not_current',
                            'memory_write_needs_retry'):
                        item['reason'] = result['reason']
                    results.append(item)
            except AdmissionClosed:
                results.append({'taskId': binding.task_id, 'status': 'guarded'})
            except KeyError:
                results.append({'taskId': binding.task_id, 'status': 'error', 'reason': 'task_unavailable'})
            except Exception:
                results.append({'taskId': binding.task_id, 'status': 'error', 'reason': 'delivery_unavailable'})
        if len(bindings) < self.batch_size:
            self.cursor = ''
        return results

    def run(self, stop_event, *, interval=1, report=None):
        _interval(interval)
        while not stop_event.is_set():
            try:
                result = self.tick()
            except AdmissionClosed:
                result = [{'status': 'guarded'}]
            except Exception:
                result = [{'status': 'error', 'reason': 'binding_scan_unavailable'}]
            if report is not None:
                report(result)
            stop_event.wait(interval)


class CompletionWorker:
    """Own one loop thread; stopping never cancels an in-flight Sim mutation."""
    def __init__(self, loop, *, interval=1, name='agent-line-completion'):
        _interval(interval)
        self.loop, self.interval = loop, interval
        self.name = name
        self.thread = None
        self.stop_event = threading.Event()
        self.last_report = []
        self._lifecycle = threading.Lock()

    def _report(self, report):
        self.last_report = report

    def start(self):
        with self._lifecycle:
            if self.thread is not None or self.stop_event.is_set():
                raise RuntimeError('Completion worker may only start once')
            self.thread = threading.Thread(
                target=self.loop.run, args=(self.stop_event,),
                kwargs={'interval': self.interval, 'report': self._report},
                name=self.name, daemon=True,
            )
            self.thread.start()

    def stop(self, timeout=35):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('Drain timeout must be finite and nonnegative')
        with self._lifecycle:
            self.stop_event.set()
            thread = self.thread
        if thread is not None:
            thread.join(timeout)
            if thread.is_alive():
                raise RuntimeError('Completion worker has not drained its current request')
