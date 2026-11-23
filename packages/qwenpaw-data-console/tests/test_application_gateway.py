"""Deployment-owned extensions retain generic gateway restrictions."""
import httpx
from fastapi.testclient import TestClient

from qwenpaw_data.console import create_app


def test_application_routes_remain_opt_in_and_documents_keep_csp(tmp_path):
    (tmp_path / 'index.html').write_text('<html>Console</html>')
    seen = []

    def upstream(request):
        seen.append(request.url.path)
        assert request.headers['authorization'] == 'Bearer server-only'
        return httpx.Response(200, text='<html>Document</html>', headers={
            'content-type': 'text/html', 'content-security-policy': "default-src 'none'"})

    kwargs = dict(host_url='http://127.0.0.1:9999', token='server-only',
                  frontend=tmp_path, transport=httpx.MockTransport(upstream))
    route = '/api/v1/example/documents/report.html'
    with TestClient(create_app(**kwargs), base_url='http://localhost') as client:
        assert client.get(route, headers={'X-QwenPaw-Console': '1'}).status_code == 404
    app = create_app(**kwargs, application_routes=((r'example/documents/report\.html', ('GET',)),),
                     document_routes=(r'/api/v1/example/documents/report\.html',))
    with TestClient(app, base_url='http://localhost') as client:
        assert client.get(route).status_code == 403
        response = client.get(route, headers={'sec-fetch-site': 'same-origin',
                                             'referer': 'http://localhost/console'})
        assert response.status_code == 200
        assert "default-src 'none'" in response.headers['content-security-policy']
        assert 'sandbox' in response.headers['content-security-policy']
        assert client.get(route, headers={'sec-fetch-site': 'cross-site',
                                         'referer': 'http://evil.invalid/'}).status_code == 403
        assert client.post(route, headers={'X-QwenPaw-Console': '1'}).status_code == 405
        assert client.get('/api/v1/example/admin', headers={'X-QwenPaw-Console': '1'}).status_code == 404
    assert seen == [route]
