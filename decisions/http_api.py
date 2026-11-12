from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
import hmac
import json
from equipment_domain.values import fields, canonical
from decisions.workspace import Workspace
from decisions.query import report


def dispatch(workspace, operation, body):
    if operation == 'run':
        fields(body, ('id',))
        return workspace.result(body['id'])
    if operation == 'review':
        fields(body, ('id',))
        return workspace.review(body['id'])
    if operation == 'publication':
        fields(body, ('label',))
        return workspace.publication(body['label'])
    if operation == 'report':
        fields(body, ('id', 'kind'), ('options',))
        result = workspace.result(body['id'])['result']
        if body['kind'] in ('tradeoffs', 'budget-frontier', 'uncertainty', 'weights', 'crew-frontier'):
            raise ValueError('expensive counterfactuals are available through local CLI only')
        return report(workspace.catalog(body['id']), result, body['kind'], body.get('options'))
    if operation == 'status':
        fields(body, ())
        sources = [{'id': row['id'], 'revision': row['revision']} for row in workspace.db.execute('SELECT id,revision FROM sources ORDER BY id')]
        runs = [row[0] for row in workspace.db.execute('SELECT id FROM runs ORDER BY id')]
        return {'sources': sources, 'runs': runs}
    raise ValueError('unknown read operation')


def handler(database, token):
    if not isinstance(token, str) or len(token) < 24 or not token.isascii():
        raise ValueError('at least 24 ASCII characters required for token')
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def reply(self, code, body):
            encoded = canonical(body).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(encoded)
            self.close_connection = True

        def do_GET(self):
            self.reply(405, {'error': 'authenticated_POST_required'})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.reply(401, {'error': 'unauthorized'})
                return
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                self.reply(400, {'error': 'one_content_length_required'})
                return
            length = int(lengths[0])
            if length > 1000000:
                self.reply(413, {'error': 'request_too_large'})
                return
            if self.headers.get_content_type() != 'application/json':
                self.reply(415, {'error': 'JSON_required'})
                return
            route = urlsplit(self.path)
            if route.query or route.fragment or not route.path.startswith('/read/') or '/' in route.path[6:] or not route.path[6:]:
                self.reply(404, {'error': 'unknown_route'})
                return
            self.connection.settimeout(10)
            try:
                content = self.rfile.read(length)
                if len(content) != length:
                    raise ValueError('truncated request')
                body = json.loads(content)
                with Workspace(database) as workspace:
                    result = dispatch(workspace, route.path[6:], body)
            except KeyError:
                self.reply(404, {'error': 'not_found'})
            except (ValueError, TypeError, UnicodeError) as exc:
                self.reply(400, {'error': str(exc)[:1000]})
            except TimeoutError:
                self.reply(408, {'error': 'request_timeout'})
            else:
                self.reply(200, {'result': result})
    return Handler


def serve(database, token, port=8080):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('invalid port')
    with HTTPServer(('127.0.0.1', port), handler(database, token)) as server:
        server.serve_forever(poll_interval=.2)
