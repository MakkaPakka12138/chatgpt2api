from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.account_service import AccountService
from services.config import config
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation
from services.reference_conversation_cleanup import ReferenceConversationCleanup
from services.reference_uploads import ReferenceUploadCache, reference_upload_cache
from services.storage.json_storage import JSONStorageBackend
from utils.helper import UpstreamHTTPError


class ReferenceCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'cleanup.json'
        self.cache = ReferenceUploadCache(ttl=10, capacity=1)
        self.delete = Mock()
        self.jobs = ReferenceConversationCleanup(self.path, self.cache, self.delete)

    def test_waits_for_cache_expiry_and_last_active_user(self):
        with patch('services.reference_uploads.time.monotonic', return_value=0):
            self.cache.get_or_upload('a', b'img', lambda: {'file_id': 'file-a'}, retain=True)
            self.cache.get_or_upload('a', b'img', Mock(), retain=True)
        self.jobs.defer('a', 'conv-a', ['file-a'])
        with patch('services.reference_uploads.time.monotonic', return_value=5):
            self.jobs.cleanup_ready()
        self.delete.assert_not_called()
        with patch('services.reference_uploads.time.monotonic', return_value=11):
            self.jobs.cleanup_ready()
            self.cache.release('a', ['file-a'])
            self.jobs.cleanup_ready()
            self.delete.assert_not_called()
            self.cache.release('a', ['file-a'])
            self.jobs.cleanup_ready()
        self.delete.assert_called_once_with('a', 'conv-a')
        self.assertEqual(json.loads(self.path.read_text()), [])

    def test_invalidation_does_not_delete_files_still_used_by_another_request(self):
        self.cache.get_or_upload('a', b'img', lambda: {'file_id': 'old'}, retain=True)
        self.jobs.defer('a', 'conv-old', ['old'])
        self.cache.invalidate('a', ['old'])
        self.cache.get_or_upload('a', b'img', lambda: {'file_id': 'new'})
        self.jobs.cleanup_ready()
        self.delete.assert_not_called()
        self.cache.release('a', ['old'])
        self.jobs.cleanup_ready()
        self.delete.assert_called_once_with('a', 'conv-old')
        self.assertTrue(self.cache.protected('a', ['new']))

    def test_eviction_waits_for_generation_to_close(self):
        self.cache.get_or_upload('a', b'one', lambda: {'file_id': 'one'}, retain=True)
        self.jobs.defer('a', 'conv-one', ['one'])
        self.cache.get_or_upload('a', b'two', lambda: {'file_id': 'two'})
        self.jobs.cleanup_ready()
        self.delete.assert_not_called()
        self.cache.release('a', ['one'])
        self.jobs.cleanup_ready()
        self.delete.assert_called_once()

    def test_restart_resumes_persisted_cleanup_without_cached_files(self):
        self.cache.get_or_upload('a', b'img', lambda: {'file_id': 'file-a'})
        self.jobs.defer('a', 'conv-a', ['file-a'])
        resumed = ReferenceConversationCleanup(self.path, ReferenceUploadCache(), self.delete)
        resumed.cleanup_ready()
        self.delete.assert_called_once_with('a', 'conv-a')

    def test_cleanup_errors_have_bounded_retries_and_remain_persisted(self):
        self.delete.side_effect = RuntimeError('connection failed')
        self.jobs.defer('a', 'conv-a', ['expired'])
        with patch('services.reference_conversation_cleanup.time.time', return_value=0):
            self.jobs.cleanup_ready()
            self.jobs.cleanup_ready()
        with patch('services.reference_conversation_cleanup.time.time', return_value=31):
            self.jobs.cleanup_ready()
        with patch('services.reference_conversation_cleanup.time.time', return_value=92):
            self.jobs.cleanup_ready()
        with patch('services.reference_conversation_cleanup.time.time', return_value=200):
            self.jobs.cleanup_ready()
        self.assertEqual(self.delete.call_count, 3)
        self.assertEqual(json.loads(self.path.read_text())[0]['attempts'], 3)

    def test_generation_defers_reference_cleanup_and_respects_disabled_option(self):
        b = Mock(access_token='a', _reference_file_ids=['file-a'])
        with patch.dict(config.data, image_remove_conversation_after_result=True,
                        image_remove_conversation_always=False), \
             patch.object(conversation, 'reference_conversation_cleanup', self.jobs):
            conversation._remove_image_conversation_later(b, 'conv-a', success=True)
        b.delete_conversation.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text())[0]['conversation_id'], 'conv-a')
        with patch.dict(config.data, image_remove_conversation_after_result=False,
                        image_remove_conversation_always=False), \
             patch.object(conversation, 'reference_conversation_cleanup') as jobs:
            conversation._remove_image_conversation_later(b, 'conv-b', success=True)
        jobs.defer.assert_not_called()

    def test_backend_close_releases_reference_leases_once(self):
        b = object.__new__(OpenAIBackendAPI)
        b.access_token = 'a'; b._reference_file_ids = ['one', 'one']; b.session = Mock()
        with patch('services.openai_backend_api.reference_upload_cache', self.cache):
            self.cache.get_or_upload('a', b'img', lambda: {'file_id': 'one'}, retain=True)
            self.cache.get_or_upload('a', b'img', Mock(), retain=True)
            self.cache.invalidate('a')
            self.assertTrue(self.cache.protected('a', ['one']))
            b.close(); b.close()
        self.assertFalse(self.cache.protected('a', ['one']))
        b.session.close.assert_called_once()


class CachedReferenceRetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = AccountService(JSONStorageBackend(Path(self.temp.name)/'accounts.json'))
        self.service.add_account_items([{'access_token': t, 'status': '正常', 'quota': 10,
                                        'upload_remaining': 80} for t in ('lifecycle-a','lifecycle-b')])
        self.service.fetch_remote_info = lambda token, event='', **kw: self.service.get_account(token)
        for token in ('lifecycle-a','lifecycle-b'):
            reference_upload_cache.invalidate(token)
            self.addCleanup(reference_upload_cache.invalidate, token)
        self.config_patch = patch.dict(config.data, account_scheduling_mode='sequential')
        self.config_patch.start(); self.addCleanup(self.config_patch.stop)
        reference_upload_cache.get_or_upload('lifecycle-a', b'img', lambda: {'file_id': 'stale'})
        self.calls = []
        self.request = conversation.ConversationRequest(model='gpt-image-2', images=[base64.b64encode(b'img').decode()])
        self.result = conversation.ImageOutput(kind='result', model='gpt-image-2', index=1, total=1,
                                               data=[{'b64_json':'out'}])

    def generate(self, stream, *, reused=True):
        def make_backend(access_token):
            self.calls.append(access_token)
            return Mock(access_token=access_token, _reference_cache_reused=reused,
                        _reference_file_ids=['stale'])
        constructor = Mock(side_effect=make_backend)
        constructor._decode_image_base64 = OpenAIBackendAPI._decode_image_base64
        with patch.object(conversation, 'account_service', self.service), \
             patch.object(conversation, 'OpenAIBackendAPI', constructor), \
             patch.object(conversation, 'stream_image_outputs', side_effect=stream), \
             patch.object(conversation.time, 'sleep'), \
             patch.object(conversation, 'reference_conversation_cleanup'):
            return conversation._generate_single_image(self.request, 1, 1)

    def test_stale_cache_empty_500_reuploads_on_same_eligible_account(self):
        uploads = Mock(return_value={'file_id':'new'})
        def stream(b, *args):
            if len(self.calls) == 1:
                raise UpstreamHTTPError('/backend-api/f/conversation', 500, '')
            reference_upload_cache.get_or_upload(b.access_token, b'img', uploads)
            yield self.result
        self.assertEqual(self.generate(stream), [self.result])
        self.assertEqual(self.calls, ['lifecycle-a','lifecycle-a'])
        uploads.assert_called_once()
        self.assertFalse(self.service._image_inflight)

    def test_second_empty_500_stops_after_one_cache_retry(self):
        stream = Mock(side_effect=UpstreamHTTPError('/backend-api/f/conversation',500,''))
        with self.assertRaises(conversation.ImageGenerationError): self.generate(stream)
        self.assertEqual(stream.call_count, 2)
        self.assertFalse(self.service._image_inflight)

    def test_retry_revalidates_upload_threshold_and_switches_when_below_twenty(self):
        self.service.update_account('lifecycle-a', {'upload_remaining':19})
        def stream(b, *args):
            if len(self.calls) == 1: raise UpstreamHTTPError('/backend-api/f/conversation',500,'')
            yield self.result
        self.generate(stream)
        self.assertEqual(self.calls, ['lifecycle-a','lifecycle-b'])
        self.assertFalse(self.service._image_inflight)

    def test_fresh_upload_and_other_http_errors_do_not_trigger_cache_retry(self):
        for reused, status, body, context in ((False,500,'','/backend-api/f/conversation'),
                (True,500,{'detail':'different failure'},'/backend-api/f/conversation'),
                (True,502,'','/backend-api/f/conversation'), (True,500,'','/backend-api/files')):
            with self.subTest(reused=reused,status=status,body=body,context=context):
                stream = Mock(side_effect=UpstreamHTTPError(context,status,body))
                with self.assertRaises(conversation.ImageGenerationError): self.generate(stream,reused=reused)
                self.assertEqual(stream.call_count,1)
                self.assertFalse(self.service._image_inflight)

    def test_no_retry_after_generation_has_emitted_progress(self):
        def stream(b, *args):
            yield conversation.ImageOutput(kind='progress', model='gpt-image-2', index=1,total=1)
            raise UpstreamHTTPError('/backend-api/f/conversation',500,'')
        with self.assertRaises(conversation.ImageGenerationError): self.generate(stream)
        self.assertEqual(len(self.calls),1)
        self.assertFalse(self.service._image_inflight)
