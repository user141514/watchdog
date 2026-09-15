import unittest
from chat_watchdog.model import PageSnapshot, Phase
from chat_watchdog.supervisor import Supervisor, StepResult

class Page:
    target_url = 'https://chatgpt.com/c/00000000-0000-0000-0000-000000000011'
    def __init__(self, fault=False):
        self.current = PageSnapshot(phase=Phase.BLOCKED if fault else Phase.FINISHED,
            assistant_turn_id='a1', assistant_text_signature='sig', assistant_text='partial task',
            assistant_count=1, user_count=1, user_turn_id='u1', send_timeout=fault)
        self.effects = 0
    def snapshot(self): return self.current
    def send_continue(self, *args): self.effects += 1; raise AssertionError('managed Relay write')
    def retry_fault(self, *args): self.effects += 1; raise AssertionError('managed Relay retry')
class Pool:
    calls = 0
    def try_acquire(self, *args, **kwargs): self.calls += 1; raise AssertionError('unmanaged recovery')
class Intents:
    def __init__(self, response=None, failure=False):
        self.response = response or {'accepted': True}
        self.failure = failure
        self.calls = []
    def submit(self, target, snapshot, text, *, kind='continue'):
        self.calls.append((target, snapshot.user_turn_id, snapshot.assistant_turn_id, kind))
        if self.failure: raise TimeoutError('response lost')
        return self.response
class IntentSupervisorTests(unittest.TestCase):
    def test_managed_continue_only_publishes_and_never_uses_relay(self):
        page, pool, client = Page(), Pool(), Intents()
        s = Supervisor(page, pool, intent_client=client)
        self.assertEqual(s.step(), StepResult.CONTINUED)
        self.assertEqual(client.calls, [(page.target_url, 'u1', 'a1', 'continue')])
        self.assertEqual(page.effects, 0)
        self.assertEqual(pool.calls, 0)
        self.assertEqual(s.step(), StepResult.ALREADY_HANDLED)
    def test_owner_timeout_is_unknown_not_an_alternative_send_path(self):
        page, pool, client = Page(), Pool(), Intents(failure=True)
        s = Supervisor(page, pool, intent_client=client)
        for _ in range(2): self.assertEqual(s.step(), StepResult.DELIVERY_UNCERTAIN)
        self.assertEqual(page.effects, 0)
        self.assertEqual(pool.calls, 0)
    def test_owner_denial_and_frontend_fault_never_fall_back(self):
        for fault in [False, True]:
            page, pool = Page(fault), Pool()
            client = Intents({'accepted': False, 'reason': 'recovery_requires_reconciliation' if fault else 'busy'})
            s = Supervisor(page, pool, intent_client=client)
            self.assertIn(s.step(), {StepResult.BLOCKED, StepResult.WAITING})
            self.assertEqual(page.effects, 0)
            self.assertEqual(pool.calls, 0)
            self.assertEqual(client.calls[0][-1], 'retry' if fault else 'continue')
