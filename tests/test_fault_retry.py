"""Exercise the exact-page frontend-fault retry expression."""
import json
import subprocess
import unittest

from chat_watchdog.relay_page import _build_retry_fault_expression


NODE_FIXTURE = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const {expression, config} = JSON.parse(fs.readFileSync(0, 'utf8'));
let genericClicks = 0;
let faultClicks = 0;
class Element {
  getBoundingClientRect() { return {width: 10, height: 10}; }
  getAttribute(name) { return null; }
}
const genericParent = {innerText: 'ordinary card', textContent: 'ordinary card', parentElement: null};
const faultParent = {innerText: config.faultText || '消息发送超时', textContent: config.faultText || '消息发送超时', parentElement: null};
const generic = new Element();
generic.innerText = 'Retry'; generic.textContent = 'Retry'; generic.parentElement = genericParent;
generic.click = () => { genericClicks += 1; };
const fault = new Element();
fault.innerText = 'Retry'; fault.textContent = 'Retry'; fault.parentElement = faultParent;
fault.click = () => { faultClicks += 1; };
const buttons = config.includeFault === false ? [generic] : [generic, fault];
const context = {
  document: { querySelectorAll: selector => selector === 'button' ? buttons : [] },
  window: {getComputedStyle: () => ({display: 'block', visibility: 'visible'})}
};
const result = vm.runInNewContext(expression, context, {timeout: 1000});
console.log(JSON.stringify({result, genericClicks, faultClicks}));
"""


class FaultRetryExpressionTests(unittest.TestCase):
    def run_expression(self, **config):
        run = subprocess.run(
            ['node', '-e', NODE_FIXTURE],
            input=json.dumps({'expression': _build_retry_fault_expression(), 'config': config}),
            text=True,
            encoding='utf-8',
            capture_output=True,
            timeout=10,
            check=True,
        )
        return json.loads(run.stdout)

    def test_clicks_only_retry_button_inside_frontend_fault_context(self):
        actual = self.run_expression()
        self.assertTrue(actual['result']['retried'])
        self.assertEqual(actual['faultClicks'], 1)
        self.assertEqual(actual['genericClicks'], 0)

    def test_refuses_unrelated_retry_button(self):
        actual = self.run_expression(includeFault=False)
        self.assertFalse(actual['result']['retried'])
        self.assertEqual(actual['genericClicks'], 0)
        self.assertEqual(actual['faultClicks'], 0)


if __name__ == '__main__':
    unittest.main()
