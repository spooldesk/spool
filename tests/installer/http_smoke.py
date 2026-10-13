"""CI-only login check against the disposable installation started by CI."""
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import os
from urllib.parse import urlencode, urlparse
from urllib.request import build_opener, HTTPCookieProcessor


class LoginForm(HTMLParser):
    csrf = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'input' and attrs.get('name') == 'csrf_token':
            self.csrf = attrs.get('value')


def main():
    password = os.environ['INSTALLER_SMOKE_PASSWORD']
    origin = 'http://localhost:8000'
    client = build_opener(HTTPCookieProcessor(CookieJar()))
    with client.open(origin + '/login', timeout=15) as response:
        form = LoginForm()
        form.feed(response.read().decode('utf-8'))
    assert form.csrf, 'Login form has no CSRF token'
    body = urlencode({'email': 'admin@local', 'password': password,
                      'csrf_token': form.csrf, 'next': '/tickets'}).encode()
    with client.open(origin + '/login', body, timeout=15) as response:
        assert response.status == 200 and urlparse(response.url).path == '/tickets', 'Owner login failed'
    with client.open(origin + '/tickets', timeout=15) as response:
        assert response.status == 200 and urlparse(response.url).path == '/tickets', 'Session did not persist'
    print('Owner login and authenticated ticket queue passed')


if __name__ == '__main__':
    main()
