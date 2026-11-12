import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .storage import Store


def create_server(database, port):
    Path(database).parent.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format_string, *arguments):
            return None

        def do_GET(self):
            parsed = urlsplit(self.path)
            params = parse_qs(parsed.query)
            store = Store(database)
            try:
                if parsed.path == '/records':
                    status, data = 200, store.records()
                elif parsed.path == '/items':
                    status, data = 200, store.items_view()
                elif parsed.path == '/versions':
                    url = params.get('url', [None])[0]
                    view = store.versions_view(url) if url else None
                    if view is None:
                        status, data = 404, {'error': '条目不存在或缺少 url 参数'}
                    else:
                        status, data = 200, view
                elif parsed.path == '/status':
                    status, data = 200, store.status_view()
                elif parsed.path == '/events':
                    status, data = 200, store.events_view(params.get('url', [None])[0])
                else:
                    status, data = 404, {'error': '资源不存在'}
            finally:
                store.close()
            payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def serve(database, port):
    server = create_server(database, port)
    print(f'采集记录服务启动于 127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
