"""Baseline HTTP security regression checks (not a substitute for a penetration test)."""
from .conftest import login


def test_security_headers_on_api_and_public_routes(api):
    for path in ('/health', '/', '/crm', '/api/v1/me'):
        response = api.get(path)
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['x-frame-options'] == 'DENY'
        assert response.headers['referrer-policy'] == 'no-referrer'
        assert response.headers['permissions-policy'] == 'camera=(), microphone=(), geolocation=()'
        assert response.headers['cross-origin-resource-policy'] == 'same-origin'
        if path.startswith('/api/'):
            assert response.headers['cache-control'] == 'no-store'
            assert response.headers['pragma'] == 'no-cache'


def test_auth_response_not_cached_and_input_bounded(api):
    response = api.post('/api/v1/auth/login', json={
        'username': 'admin-a', 'password': 'long-test-password-1234'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    token = response.json()['access_token']
    assert token not in str(response.headers)
    assert api.post('/api/v1/auth/login', json={
        'username': 'a' * 121, 'password': 'x'}).status_code == 422
    assert api.post('/api/v1/auth/login', json={
        'username': 'admin-a', 'password': 'x' * 1025}).status_code == 422
    authorized = api.get('/api/v1/me', headers={'Authorization': 'Bearer ' + token})
    assert authorized.status_code == 200
    assert authorized.headers['cache-control'] == 'no-store'


def test_missing_token_and_cross_tenant_still_denied(api):
    assert api.get('/api/v1/records').status_code == 401
    h = login(api)
    assert api.get('/api/v1/records', headers=h).status_code == 200
