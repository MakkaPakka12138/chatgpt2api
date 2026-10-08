import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from services.log_service import LogService
from services import image_service
from services.image_task_service import _timestamp
from utils.time_range import TimeRange


class BrowserTimeRangeTests(unittest.TestCase):
    start = '2026-09-30T15:00:00Z'
    end = '2026-10-01T15:00:00Z'

    def test_local_day_filter_includes_boundaries_before_limiting_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'logs.jsonl'
            times = ['2026-09-30 14:59:59', '2026-09-30 15:00:00',
                     '2026-10-01T14:10:58+09:00', '2026-10-01T14:59:59Z', '2026-10-01 15:00:00']
            p.write_text('\n'.join(json.dumps({'id':str(i),'time':t,'type':'call'}) for i,t in enumerate(times)))
            logs = LogService(p)
            self.assertEqual([x['id'] for x in logs.list(start_at=self.start, end_before=self.end)], ['3','2','1'])
            self.assertEqual([x['id'] for x in logs.list(start_at=self.start, end_before=self.end,limit=1)], ['3'])
            self.assertEqual([x['id'] for x in logs.list(start_date='2026-10-01',end_date='2026-10-01')], ['4','3','2'])
            self.assertEqual(p.read_text().count('\n'),4)

    def test_new_logs_have_explicit_utc_and_old_records_remain_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs=LogService(Path(tmp)/'logs.jsonl')
            logs.add('call','test')
            self.assertTrue(logs.list()[0]['time'].endswith('+00:00'))

    def test_invalid_or_naive_bounds_are_rejected(self):
        for start,end in [('bad',''),('2026-10-01 00:00:00',''),(self.end,self.start)]:
            with self.assertRaises(ValueError):TimeRange(start,end)

    def test_task_timestamps_keep_explicit_offsets(self):
        self.assertEqual(_timestamp('2026-10-01T14:10:58+09:00'),_timestamp('2026-10-01 05:10:58'))
        self.assertEqual(_timestamp('2026-10-01T05:10:58.123456Z'),_timestamp('2026-10-01T14:10:58.123456+09:00'))

    def test_image_listing_and_delete_selection_use_same_local_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=Mock(images_dir=Path(tmp),image_thumbnails_dir=Path(tmp)/'thumbnails')
            items=[{'path':f'{i}.png','date':'2026-10-01','created_at':t} for i,t in enumerate(
                ['2026-09-30 14:59:59','2026-09-30 15:00:00','2026-10-01 14:59:59','2026-10-01 15:00:00'])]
            with patch.object(image_service,'config',cfg),patch.object(image_service,'cleanup_image_thumbnails'), \
                 patch.object(image_service,'load_tags',return_value={}),patch.object(image_service,'remove_tags'), \
                 patch.object(image_service.image_storage_service,'list_items',return_value=items), \
                 patch.object(image_service.image_storage_service,'delete',return_value=True) as delete:
                result=image_service.list_images('http://example.test',start_at=self.start,end_before=self.end)
                self.assertEqual([x['path'] for x in result['items']],['1.png','2.png'])
                result=image_service.delete_images(all_matching=True,start_at=self.start,end_before=self.end)
                self.assertEqual(result['removed'],2)
                self.assertEqual([c.args[0] for c in delete.call_args_list],['1.png','2.png'])
                with self.assertRaises(ValueError):image_service.delete_images(all_matching=True,start_at='bad')
                self.assertEqual(delete.call_count,2)

    def test_management_api_accepts_bounds_and_rejects_bad_delete_ranges(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.system import create_router
        app=FastAPI()
        app.include_router(create_router('test'))
        client=TestClient(app)
        with tempfile.TemporaryDirectory() as tmp,patch('api.system.require_admin'):
            p=Path(tmp)/'logs.jsonl'
            p.write_text(json.dumps({'id':'legacy','time':'2026-10-01 05:10:58','type':'call'}))
            with patch('api.system.log_service',LogService(p)):
                result=client.get('/api/logs',params={'start_at':self.start,'end_before':self.end})
                self.assertEqual(result.status_code,200)
                self.assertEqual(result.json()['items'][0]['id'],'legacy')
                self.assertEqual(client.get('/api/logs',params={'start_at':'invalid'}).status_code,400)
            with patch.object(image_service.image_storage_service,'delete') as delete:
                response=client.post('/api/images/delete',json={'all_matching':True,'start_at':'invalid'})
                self.assertEqual(response.status_code,400)
                delete.assert_not_called()
