import unittest
from unittest.mock import patch
from core import notify


class NotifyPrivacyTests(unittest.TestCase):
    def test_failed_response_body_is_not_logged(self):
        marker = 'private-response-value-for-test'
        with patch.object(notify, '_sdk_sc_send', object()), \
                patch.object(notify, 'sc_send', return_value={'code': 1, 'error': marker}):
            with self.assertLogs('notify', level='ERROR') as logs:
                notify._send_message('test', 'test', '', {'notify': {'enabled': True, 'sendkey': marker}})
        self.assertNotIn(marker, '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
