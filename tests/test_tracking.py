import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from collector.api import create_server
from collector.domain import normalize_url, resolve_identity
from collector.storage import Store
from collector.tracker import Tracker
from collector.transport import Client, replay_server

ALLOW = ['https://lab.example']


def page(title, body, canonical=None):
    link = f'<link rel="canonical" href="{canonical}">' if canonical else ''
    return (f'<html><head>{link}</head><body>'
            f'<h1>{title}</h1><article>{body}</article></body></html>')


def ok(body, etag=None, last_modified=None, date='Wed, 11 Nov 2026 08:00:00 GMT'):
    headers = {'Date': date}
    if etag:
        headers['ETag'] = etag
    if last_modified:
        headers['Last-Modified'] = last_modified
    return dict(status=200, headers=headers, body=body)


class TrackingTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temporary.name) / 'track.sqlite3')
        self.store = Store(self.db)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def tracker(self, resolver=None, **kwargs):
        return Tracker(self.store, Client(), ALLOW, resolver, **kwargs)


class UrlIdentityTests(unittest.TestCase):
    def test_tracking_params_stripped_version_params_kept(self):
        self.assertEqual(
            normalize_url('https://lab.example/item/1?utm_source=rss&utm_medium=mail&v=2#top'),
            'https://lab.example/item/1?v=2')
        self.assertEqual(
            normalize_url('https://lab.example/item/1?fbclid=abc&version=3&id=42'),
            'https://lab.example/item/1?id=42&version=3')
        self.assertEqual(
            normalize_url('HTTPS://LAB.Example:443/item/1'),
            'https://lab.example/item/1')

    def test_canonical_trusted_only_within_allowed_origins(self):
        main, relation, note = resolve_identity(
            'https://mirror.example/a', 'https://lab.example/item/1', ALLOW)
        self.assertEqual((main, relation, note), ('https://lab.example/item/1', 'reprint', None))
        main, relation, note = resolve_identity(
            'https://mirror.example/a', 'https://elsewhere.example/x', ALLOW)
        self.assertEqual((main, relation), ('https://mirror.example/a', 'original'))
        self.assertIsNotNone(note)
        main, relation, note = resolve_identity(
            'https://lab.example/item/1', 'https://lab.example/item/1?utm_source=x', ALLOW)
        self.assertEqual((main, relation), ('https://lab.example/item/1', 'original'))


class PollFlowTests(TrackingTestBase):
    def test_same_title_different_research_not_merged(self):
        routes = {
            '/item/1': [ok(page('成果甲', '正文一'))],
            '/item/2': [ok(page('成果甲', '正文二'))],
        }
        with replay_server(routes) as (base, calls):
            tracker = self.tracker(lambda url: url.replace('https://lab.example', base))
            tracker.poll('https://lab.example/item/1')
            tracker.poll('https://lab.example/item/2')
        items = self.store.items_view()
        self.assertEqual(len(items), 2)
        self.assertEqual({item['head']['title'] for item in items}, {'成果甲'})

    def test_repeated_corrections_form_one_item_with_versions(self):
        routes = {'/item/1': [
            ok(page('成果甲', '测深100米'), etag='"r1"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
            ok(page('成果甲', '再次更正为120米'), etag='"r3"'),
        ]}
        with replay_server(routes) as (base, calls):
            tracker = self.tracker(lambda url: url.replace('https://lab.example', base))
            first = tracker.poll('https://lab.example/item/1')
            second = tracker.poll('https://lab.example/item/1')
            third = tracker.poll('https://lab.example/item/1')
        self.assertEqual(first['outcome'], 'initial')
        self.assertEqual(second['outcome'], 'correction')
        self.assertEqual(third['outcome'], 'correction')
        view = self.store.versions_view('https://lab.example/item/1')
        self.assertEqual([v['seq'] for v in view['versions']], [1, 2, 3])
        self.assertEqual([v['current'] for v in view['versions']], [False, False, True])
        self.assertEqual(view['versions'][0]['content'], '测深100米')  # 变更前证据保留
        self.assertEqual(self.store.items_view()[0]['head']['content'], '再次更正为120米')

    def test_conditional_request_uses_links_own_validators(self):
        routes = {
            '/item/1': [ok(page('成果甲', '一'), etag='"r1"', last_modified='Tue, 10 Nov 2026 00:00:00 GMT'),
                        dict(status=304, headers={'ETag': '"r1"'})],
        }
        with replay_server(routes) as (base, calls):
            tracker = self.tracker(lambda url: url.replace('https://lab.example', base))
            tracker.poll('https://lab.example/item/1')
            result = tracker.poll('https://lab.example/item/1')
        sent = {name.lower(): value for name, value in calls[1]['headers'].items()}
        self.assertEqual(sent.get('if-none-match'), '"r1"')
        self.assertEqual(sent.get('if-modified-since'), 'Tue, 10 Nov 2026 00:00:00 GMT')
        self.assertEqual(result['outcome'], 'unchanged')
        outcomes = [event['outcome'] for event in self.store.events_view()]
        self.assertIn('not_modified', outcomes)

    def test_304_reuses_own_link_body_not_other_pages_cache(self):
        routes = {
            '/item/1': [ok(page('成果甲', '测深100米'), etag='"r1"'),
                        ok(page('成果甲', '更正为110米'), etag='"r2"'),
                        dict(status=304, headers={'ETag': '"r2"'})],
            '/a': [ok(page('成果甲', '测深100米', canonical='https://lab.example/item/1'), etag='"m1"'),
                   dict(status=304, headers={'ETag': '"m1"'})],
        }

        def resolver(url):
            # 两个来源分别起服务，保证镜像有独立缓存
            return url.replace('https://lab.example', lab).replace('https://mirror.example', mirror)

        with replay_server({'/item/1': routes['/item/1']}) as (lab, lab_calls), \
                replay_server({'/a': routes['/a']}) as (mirror, mirror_calls):
            tracker = self.tracker(resolver)
            tracker.poll('https://lab.example/item/1')      # v1
            tracker.poll('https://mirror.example/a')        # 转载 v1
            tracker.poll('https://lab.example/item/1')      # 更正 v2
            tracker.poll('https://mirror.example/a')        # 镜像 304：沿用其自身旧文
            tracker.poll('https://lab.example/item/1')      # 原文 304：沿用其自身新文
        links = {link['url']: link for link in self.store.status_view()['links']}
        self.assertTrue(links['https://mirror.example/a']['lagging'])   # 仍是旧文 → 滞后
        self.assertFalse(links['https://lab.example/item/1']['lagging'])
        item = self.store.items_view()[0]
        self.assertEqual(item['versions'], 2)                            # 304 不产生新版本
        self.assertEqual(item['head']['content'], '更正为110米')          # 未被镜像旧文覆盖

    def test_reprint_does_not_create_versions_or_move_head(self):
        routes = {
            '/item/1': [ok(page('成果甲', '测深100米'), etag='"r1"'),
                        ok(page('成果甲', '更正为110米'), etag='"r2"')],
            '/a': [ok(page('成果甲', '测深100米', canonical='https://lab.example/item/1'), etag='"m1"')],
        }
        with replay_server({'/item/1': routes['/item/1']}) as (lab, calls1), \
                replay_server({'/a': routes['/a']}) as (mirror, calls2):
            tracker = self.tracker(
                lambda url: url.replace('https://lab.example', lab).replace('https://mirror.example', mirror))
            tracker.poll('https://lab.example/item/1')
            tracker.poll('https://lab.example/item/1')  # 更正 v2
            result = tracker.poll('https://mirror.example/a')  # 转载仍送旧文
        self.assertEqual(result['relation'], 'reprint')
        item = self.store.items_view()[0]
        self.assertEqual(item['versions'], 2)
        self.assertEqual(item['head']['content'], '更正为110米')
        self.assertEqual(item['links'][0]['relation'], 'original')

    def test_temporary_failure_keeps_head_and_records_evidence(self):
        routes = {'/item/1': [
            ok(page('成果甲', '测深100米'), etag='"r1"'),
            dict(status=503, headers={'Retry-After': '2'}),
            ok(page('成果甲', '测深100米'), etag='"r1"'),
        ]}
        with replay_server(routes) as (base, calls):
            tracker = self.tracker(lambda url: url.replace('https://lab.example', base))
            tracker.poll('https://lab.example/item/1')
            failed = tracker.poll('https://lab.example/item/1')
            recovered = tracker.poll('https://lab.example/item/1')
        self.assertEqual(failed['outcome'], 'failed')
        self.assertEqual(failed['status'], 503)
        self.assertEqual(recovered['outcome'], 'unchanged')
        item = self.store.items_view()[0]
        self.assertEqual(item['versions'], 1)  # 失败不产生版本
        status = self.store.status_view()
        self.assertEqual(status['recent_failures'][0]['status'], 503)
        self.assertIsNone(status['links'][0]['last_failure'])  # 恢复后清除

    def test_retraction_distinguished_and_idempotent(self):
        routes = {'/item/1': [
            ok(page('成果甲', '测深100米'), etag='"r1"'),
            dict(status=410, headers={'Date': 'Wed, 11 Nov 2026 09:00:00 GMT'}, body='发布方明确撤回'),
            dict(status=410, headers={'Date': 'Wed, 11 Nov 2026 09:05:00 GMT'}, body='发布方明确撤回'),
        ]}
        with replay_server(routes) as (base, calls):
            tracker = self.tracker(lambda url: url.replace('https://lab.example', base))
            tracker.poll('https://lab.example/item/1')
            first = tracker.poll('https://lab.example/item/1')
            second = tracker.poll('https://lab.example/item/1')
        self.assertEqual(first['outcome'], 'retracted')
        self.assertTrue(first['changed'])
        self.assertFalse(second['changed'])  # 重复撤稿只生效一次
        view = self.store.versions_view('https://lab.example/item/1')
        self.assertEqual(view['status'], 'retracted')
        self.assertEqual(view['retraction']['reason'], '发布方明确撤回')
        self.assertEqual(view['retraction']['source_date'], 'Wed, 11 Nov 2026 09:00:00 GMT')
        self.assertEqual(len(view['versions']), 1)  # 历史版本保留可查
        events = [e for e in self.store.events_view() if e['outcome'] == 'retracted']
        self.assertEqual(len(events), 1)

    def test_reprint_first_then_origin_dedupes_by_body(self):
        routes = {
            '/item/1': [ok(page('成果甲', '测深100米'), etag='"r1"')],
            '/a': [ok(page('成果甲', '测深100米', canonical='https://lab.example/item/1'), etag='"m1"')],
        }
        with replay_server({'/item/1': routes['/item/1']}) as (lab, c1), \
                replay_server({'/a': routes['/a']}) as (mirror, c2):
            tracker = self.tracker(
                lambda url: url.replace('https://lab.example', lab).replace('https://mirror.example', mirror))
            first = tracker.poll('https://mirror.example/a')   # 先看到转载
            second = tracker.poll('https://lab.example/item/1')  # 原文同文
        self.assertEqual(first['outcome'], 'initial')
        self.assertEqual(first['main_url'], 'https://lab.example/item/1')
        self.assertEqual(second['outcome'], 'unchanged')
        self.assertEqual(self.store.items_view()[0]['versions'], 1)


class ConcurrencyAndCrashTests(TrackingTestBase):
    def test_concurrent_workers_single_version_switch(self):
        routes = {'/item/1': [
            ok(page('成果甲', '测深100米'), etag='"r1"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
        ]}
        with replay_server(routes) as (base, calls):
            resolver = lambda url: url.replace('https://lab.example', base)
            self.tracker(resolver).poll('https://lab.example/item/1')  # v1
            barrier = threading.Barrier(3)
            results = []

            def worker():
                store = Store(self.db)  # 每个工作进程/线程使用自己的连接
                try:
                    tracker = Tracker(store, Client(), ALLOW, resolver)
                    barrier.wait(timeout=5)
                    results.append(tracker.poll('https://lab.example/item/1'))
                finally:
                    store.close()

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            barrier.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(1 for r in results if r['switched']), 1)  # 仅一次有效切换
        view = self.store.versions_view('https://lab.example/item/1')
        self.assertEqual(len(view['versions']), 2)
        self.assertEqual(view['versions'][1]['content'], '更正为110米')

    def test_crash_between_raw_save_and_index_update_recovers_once(self):
        routes = {'/item/1': [
            ok(page('成果甲', '测深100米'), etag='"r1"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
        ]}
        with replay_server(routes) as (base, calls):
            resolver = lambda url: url.replace('https://lab.example', base)
            self.tracker(resolver).poll('https://lab.example/item/1')  # v1

            class Crash(Exception):
                pass

            def boom():
                raise Crash()

            crashing = Tracker(self.store, Client(), ALLOW, resolver, after_raw_saved=boom)
            with self.assertRaises(Crash):
                crashing.poll('https://lab.example/item/1')  # 原文已保存、索引未更新
            during = self.store.items_view()[0]
            self.assertEqual(during['head']['content'], '测深100米')  # 编辑仍见旧版
            self.assertEqual(during['versions'], 1)                    # 无半份新版本
            self.assertEqual(
                self.store.connection.execute('SELECT COUNT(*) FROM raw_objects').fetchone()[0], 2)

            self.store.close()  # 模拟进程退出后重启
            self.store = Store(self.db)
            recovered = self.tracker(resolver).poll('https://lab.example/item/1')
        self.assertEqual(recovered['outcome'], 'correction')
        self.assertTrue(recovered['switched'])
        view = self.store.versions_view('https://lab.example/item/1')
        self.assertEqual(len(view['versions']), 2)  # 重启后只产生一次有效切换
        self.assertEqual(view['versions'][1]['content'], '更正为110米')

    def test_real_process_crash_and_restart(self):
        routes = {'/item/1': [
            ok(page('成果甲', '测深100米'), etag='"r1"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
        ]}
        with replay_server(routes) as (base, calls):
            url = base + '/item/1'
            command = [sys.executable, '-m', 'collector', 'track',
                       '--db', self.db, '--url', url, '--allow', base]
            first = subprocess.run(command, capture_output=True, text=True, cwd=os.getcwd())
            self.assertEqual(first.returncode, 0, first.stderr)
            env = dict(os.environ, COLLECTOR_TEST_CRASH_AFTER_RAW='1')
            crashed = subprocess.run(command, capture_output=True, text=True,
                                     cwd=os.getcwd(), env=env)
            self.assertEqual(crashed.returncode, 1)  # 正文保存后进程退出
            reopened = Store(self.db)
            try:
                self.assertEqual(reopened.items_view()[0]['head']['content'], '测深100米')
            finally:
                reopened.close()
            third = subprocess.run(command, capture_output=True, text=True, cwd=os.getcwd())
            self.assertEqual(third.returncode, 0, third.stderr)
        reopened = Store(self.db)
        try:
            view = reopened.versions_view(url)
            self.assertEqual(len(view['versions']), 2)
            self.assertEqual(view['versions'][1]['content'], '更正为110米')
        finally:
            reopened.close()


class ApiTests(TrackingTestBase):
    def test_query_endpoints(self):
        routes = {'/item/1': [
            ok(page('成果甲', '测深100米'), etag='"r1"'),
            ok(page('成果甲', '更正为110米'), etag='"r2"'),
        ]}
        with replay_server(routes) as (base, calls):
            tracker = self.tracker(lambda url: url.replace('https://lab.example', base))
            tracker.poll('https://lab.example/item/1')
            tracker.poll('https://lab.example/item/1')
        self.store.close()
        server = create_server(self.db, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            root = f'http://127.0.0.1:{server.server_port}'
            client = Client()
            status, _, body = client.request(root + '/items')
            self.assertEqual(status, 200)
            items = json.loads(body)
            self.assertEqual(items[0]['head']['seq'], 2)
            status, _, body = client.request(root + '/versions?url=https%3A%2F%2Flab.example%2Fitem%2F1')
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)['versions']), 2)
            status, _, body = client.request(root + '/versions?url=https%3A%2F%2Flab.example%2Fnone')
            self.assertEqual(status, 404)
            status, _, body = client.request(root + '/status')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['links'][0]['etag'], '"r2"')
            status, _, body = client.request(root + '/events?url=https%3A%2F%2Flab.example%2Fitem%2F1')
            self.assertEqual(status, 200)
            self.assertEqual(len(json.loads(body)), 2)
            status, _, _ = client.request(root + '/unknown')
            self.assertEqual(status, 404)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.store = Store(self.db)  # tearDown 需要


if __name__ == '__main__':
    unittest.main()
