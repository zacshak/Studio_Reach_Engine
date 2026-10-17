import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import draft_runtime as runtime
import draft_cloud


class DraftRuntimeTest(unittest.TestCase):
    def client(self, side_effect):
        create = Mock(side_effect=side_effect)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), create

    def test_incompatible_vision_route_stops_immediately(self):
        error = RuntimeError('endpoint does not accept image/non-text content parts')
        client, create = self.client(error)
        with self.assertRaises(runtime.ProviderUnavailable) as result:
            runtime.infer(client, 'test', [], json.loads)
        self.assertTrue(result.exception.fatal)
        self.assertEqual(create.call_count, 1)

    def test_transient_errors_and_bad_json_have_bounded_retries(self):
        for failure in (TimeoutError('timeout'), ValueError('bad JSON')):
            client, create = self.client(failure)
            with patch.object(runtime.time, 'sleep'), self.assertRaises(runtime.ProviderUnavailable):
                runtime.infer(client, 'test', [], json.loads)
            self.assertEqual(create.call_count, 3)
            self.assertEqual(create.call_args.kwargs['timeout'], 60)

    def test_real_image_preflight_requires_correct_json_color(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"color":"red"}'))])
        client, create = self.client([response])
        with patch.object(runtime.secrets, 'choice', return_value=('red',(255,0,0))):
            runtime.preflight(client, 'test')
        content = create.call_args.kwargs['messages'][0]['content']
        self.assertEqual(content[1]['type'], 'image_url')
        self.assertTrue(content[1]['image_url']['url'].startswith('data:image/png;base64,'))
        client, create = self.client([response] * 3)
        with patch.object(runtime.secrets, 'choice', return_value=('blue',(0,0,255))), patch.object(runtime.time,'sleep'), self.assertRaises(runtime.ModelOutputError):
            runtime.preflight(client, 'test')

    def test_truncated_json_increases_budget_and_never_returns_partial_output(self):
        partial = SimpleNamespace(choices=[SimpleNamespace(finish_reason='length', message=SimpleNamespace(content='{"observation_1": "cut'))])
        complete = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='{"observation_1": "complete"}'))])
        client, create = self.client([partial, complete])
        with patch.object(runtime.time, 'sleep'):
            result = runtime.infer(client, 'test', [], json.loads)
        self.assertEqual(result, {'observation_1': 'complete'})
        self.assertEqual([call.kwargs['max_tokens'] for call in create.call_args_list], [4096,8192])

    def test_output_rejection_is_not_a_provider_failure(self):
        response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content='not JSON'))])
        client, create = self.client([response]*3)
        with patch.object(runtime.time,'sleep'), self.assertRaises(runtime.ModelOutputError):
            runtime.infer(client,'test',[],json.loads)
        self.assertEqual(create.call_count,3)

    def test_failed_preflight_never_reads_queue_or_changes_records(self):
        with patch.object(draft_cloud.sys, 'argv', ['draft_cloud.py','--check-only']), patch.object(draft_cloud, 'configured_client', return_value=(None,'test')), patch.object(draft_cloud, 'preflight', side_effect=runtime.ProviderUnavailable('down')), patch.object(draft_cloud, 'run_queue') as queue:
            self.assertEqual(draft_cloud.main(), 1)
            queue.assert_not_called()

    def test_empty_queue_never_calls_provider(self):
        with patch.object(draft_cloud.sys, 'argv', ['draft_cloud.py']), patch.object(draft_cloud.pipeline,'mail_status_appids',return_value=[]), patch.object(draft_cloud,'configured_client') as client:
            self.assertEqual(draft_cloud.main(),0)
            client.assert_not_called()

    def test_three_provider_failures_stop_large_queue_without_status_changes(self):
        with patch.object(draft_cloud, '_load_templates', return_value=[(1,'<observation>')]), patch.object(draft_cloud.media_store, 'fetch_index', return_value={str(i):str(i) for i in range(100)}), patch.object(draft_cloud.pipeline,'mail_status_appids',return_value=list(range(100))), patch.object(draft_cloud.media_store,'fetch_manifest',return_value={'name':'Synthetic'}), patch.object(draft_cloud,'_draft',side_effect=runtime.ProviderUnavailable('unavailable')) as draft, patch.object(draft_cloud.pipeline,'set_mail_status') as status, patch.object(draft_cloud.media_store,'write_manifest') as write:
            self.assertEqual(draft_cloud.run_queue(None,'test'),1)
            self.assertEqual(draft.call_count,3)
            status.assert_not_called()
            write.assert_not_called()

    def test_one_persistence_failure_does_not_abort_remaining_drafts(self):
        with patch.object(draft_cloud, '_load_templates', return_value=[(1,'<observation>')]), \
             patch.object(draft_cloud.media_store, 'fetch_index', return_value={'1':'One','2':'Two'}), \
             patch.object(draft_cloud.pipeline, 'mail_status_appids', return_value=[1,2]), \
             patch.object(draft_cloud.media_store, 'fetch_manifest', side_effect=[{'name':'One'},{'name':'Two'}]), \
             patch.object(draft_cloud, '_draft', return_value='Subject: hello\n\nBody'), \
             patch.object(draft_cloud.pipeline, 'set_mail_template'), \
             patch.object(draft_cloud.pipeline, 'complete_saved_draft') as status, \
             patch.object(draft_cloud.media_store, 'write_manifest', side_effect=[RuntimeError('storage down'),None]):
            self.assertEqual(draft_cloud.run_queue(None,'test'),1)
            status.assert_called_once_with(2,1,None)


if __name__ == '__main__':
    unittest.main()
