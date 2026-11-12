import argparse
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import quote

from .domain import collect
from .storage import Store
from .tracker import Tracker
from .transport import Client, replay_server


def main():
    parser = argparse.ArgumentParser(description='本地采集、持续追踪与凭据查看')
    parser.add_argument('command', choices=['demo', 'import', 'show', 'serve', 'track'])
    parser.add_argument('--input', default='fixtures/sample.json')
    parser.add_argument('--db', default='var/records.sqlite3')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--url', action='append', default=[])
    parser.add_argument('--allow', action='append', default=[])
    arguments = parser.parse_args()
    if arguments.command == 'serve':
        from .api import serve
        serve(arguments.db, arguments.port)
        return
    if arguments.command == 'demo':
        run_demo()
        return
    if arguments.command == 'track':
        run_track(arguments)
        return
    Path(arguments.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(arguments.db)
    try:
        if arguments.command == 'import':
            raw = Path(arguments.input).read_bytes()
            store.ingest(str(Path(arguments.input)), raw, collect(json.loads(raw)))
        print(json.dumps(store.records(), ensure_ascii=False, indent=2))
    finally:
        store.close()


def run_track(arguments):
    Path(arguments.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(arguments.db)
    try:
        hook = None
        if os.environ.get('COLLECTOR_TEST_CRASH_AFTER_RAW'):
            hook = lambda: os._exit(1)  # 测试钩子：正文保存后立即退出进程
        tracker = Tracker(store, allow_origins=arguments.allow, after_raw_saved=hook)
        results = [tracker.poll(url) for url in arguments.url]
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        store.close()


class SimulatedCrash(Exception):
    pass


def run_demo():
    fixture = json.loads(Path('fixtures/bulletins.json').read_text(encoding='utf-8'))
    allow = fixture['allow_origins']
    dates = [f'Wed, 11 Nov 2026 08:{index:02d}:00 GMT' for index in range(30)]

    def reply(entry, date):
        result = {'status': entry['status'], 'headers': {'Date': date}}
        if entry.get('etag'):
            result['headers']['ETag'] = entry['etag']
        html = entry.get('html')
        if entry.get('canonical'):
            html = (f'<html><head><link rel="canonical" href="{entry["canonical"]}">'
                    f'</head><body>{html}</body></html>')
        if html is not None:
            result['body'] = html
        if entry['status'] == 410:
            result['body'] = entry.get('reason', '已撤回')
        return result

    scripts = {}
    for entry in fixture['versions']:
        scripts.setdefault(entry['url'], []).append(entry)

    lab_routes = {
        '/item/1': [reply(entry, dates[index])
                    for index, entry in enumerate(scripts['https://lab.example/item/1'])],
        # 崩溃恢复场景（合成补充）：同一更正响应出现两次，供崩溃后重采。
        '/item/2': [
            reply(dict(status=200, etag='"s1"',
                       html='<h1>成果乙</h1><article>初步结论：样本有效</article>'), dates[8]),
            reply(dict(status=200, etag='"s2"',
                       html='<h1>成果乙</h1><article>更正：样本编号登记有误，结论暂缓</article>'), dates[9]),
            reply(dict(status=200, etag='"s2"',
                       html='<h1>成果乙</h1><article>更正：样本编号登记有误，结论暂缓</article>'), dates[10]),
        ],
    }
    mirror_replies = [reply(entry, dates[6]) for entry in scripts['https://mirror.example/a']]
    # 转载滞后（合成补充）：镜像仍是旧正文，对本链接自己的 ETag 返回 304。
    mirror_replies.append(dict(status=304, headers={'Date': dates[7], 'ETag': '"m1"'}))

    times = iter(f'2026-11-11T09:{index:02d}:00+00:00' for index in range(40))
    clock = lambda: next(times)
    steps = []

    with tempfile.TemporaryDirectory() as directory, \
            replay_server(lab_routes) as (lab_base, lab_calls), \
            replay_server({'/a': mirror_replies}) as (mirror_base, mirror_calls):

        def resolver(url):
            return url.replace('https://lab.example', lab_base).replace(
                'https://mirror.example', mirror_base)

        db_path = Path(directory) / 'tracking.sqlite3'
        store = Store(db_path)
        tracker = Tracker(store, Client(), allow, resolver, clock)

        def snapshot():
            view = []
            for item in store.items_view():
                head = item['head']
                view.append({
                    '条目': item['main_url'],
                    '状态': item['status'],
                    '当前版本': None if head is None else
                        f"v{head['seq']}《{head['title']}》{head['content']}",
                    '版本数': item['versions'],
                    '滞后转载': [link['url'] for link in item['links'] if link['lagging']],
                })
            return view

        def step(action, result):
            steps.append({'步骤': len(steps) + 1, '动作': action,
                          '结果': result, '编辑所见': snapshot()})

        step('首采原文', tracker.poll('https://lab.example/item/1'))
        step('原文未变（304，沿用本链接正文）', tracker.poll('https://lab.example/item/1'))
        step('发现转载页（canonical 指向允许来源）', tracker.poll('https://mirror.example/a'))
        step('原文更正', tracker.poll('https://lab.example/item/1'))
        step('转载滞后（镜像 304 沿用其自身旧文）', tracker.poll('https://mirror.example/a'))
        step('原文暂时故障（503）', tracker.poll('https://lab.example/item/1'))
        step('原文撤稿（410）', tracker.poll('https://lab.example/item/1'))

        step('另一成果首采', tracker.poll('https://lab.example/item/2'))
        crashing = Tracker(store, Client(), allow, resolver, clock,
                           after_raw_saved=lambda: (_ for _ in ()).throw(SimulatedCrash()))
        try:
            crashing.poll('https://lab.example/item/2')
        except SimulatedCrash:
            pass
        steps.append({'步骤': len(steps) + 1,
                      '动作': '更正正文已保存、索引更新前进程退出',
                      '结果': {'outcome': 'crashed'}, '编辑所见': snapshot()})
        store.close()  # 模拟进程退出后重启：状态全部来自数据库
        store = Store(db_path)
        tracker = Tracker(store, Client(), allow, resolver, clock)
        step('重启后再次采集同一更正（仅一次有效切换）',
             tracker.poll('https://lab.example/item/2'))

        from .api import create_server
        server = create_server(str(db_path), 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            api_base = f'http://127.0.0.1:{server.server_port}'
            client = Client()
            api_view = {
                'GET /items': json.loads(client.request(api_base + '/items')[2]),
                'GET /versions?url=lab/item/1': json.loads(client.request(
                    api_base + '/versions?url=' + quote('https://lab.example/item/1'))[2]),
                'GET /status': json.loads(client.request(api_base + '/status')[2]),
            }
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        store.close()

    report = {'演示': steps, '查询接口实测': api_view}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
