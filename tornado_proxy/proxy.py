#!/usr/bin/env python
#
# Simple asynchronous HTTP proxy with tunnelling (CONNECT).
#
# GET/POST proxying based on
# http://groups.google.com/group/python-tornado/msg/7bea08e7a049cf26
#
# Copyright (C) 2012 Senko Rasic <senko.rasic@dobarkod.hr>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import asyncio
import logging
import os
import sys
import json
from urllib.parse import urlparse

import tornado.httpserver
import tornado.ioloop
import tornado.iostream
import tornado.web
import tornado.httpclient
import tornado.httputil
import tornado.tcpclient
import tornado.netutil

logger = logging.getLogger('tornado_proxy')
hd = logging.StreamHandler()
fmt = logging.Formatter("||%(asctime)s|%(name)s|%(levelname)s||%(message)s")
hd.setFormatter(fmt)
logger.addHandler(hd)
logger.setLevel('DEBUG')

hostname_mapping = {}
if os.path.exists("hosts.json"):
    with open("hosts.json") as f:
        hostname_mapping = json.load(f)
print(hostname_mapping)

__all__ = ['ProxyHandler', 'run_proxy']


def get_proxy(url):
    url_parsed = urlparse(url, scheme='http')
    proxy_key = '%s_proxy' % url_parsed.scheme
    return os.environ.get(proxy_key)


def parse_proxy(proxy):
    proxy_parsed = urlparse(proxy, scheme='http')
    return proxy_parsed.hostname, proxy_parsed.port


async def fetch_request(url, **kwargs):
    proxy = get_proxy(url)
    if proxy:
        logger.debug('Forward request via upstream proxy %s', proxy)
        tornado.httpclient.AsyncHTTPClient.configure(
            'tornado.curl_httpclient.CurlAsyncHTTPClient')
        host, port = parse_proxy(proxy)
        kwargs['proxy_host'] = host
        kwargs['proxy_port'] = port

    client = tornado.httpclient.AsyncHTTPClient(force_instance=True, hostname_mapping=hostname_mapping)
    return await client.fetch(url, raise_error=False, **kwargs)


async def relay_stream(reader, writer):
    try:
        while True:
            data = await reader.read_bytes(1024 * 64, partial=True)
            if writer.closed():
                return
            if data:
                writer.write(data)
            else:
                break
    except tornado.iostream.StreamClosedError:
        pass


class ProxyHandler(tornado.web.RequestHandler):
    SUPPORTED_METHODS = ("GET", "HEAD", "POST", "DELETE", "PATCH", "PUT", "OPTIONS", "CONNECT")
    
    def compute_etag(self):
        return None # disable tornado Etag

    async def prepare(self):
        logger.debug("Prepare to %s %s" % (self.request.method, self.request.uri))
        return super(ProxyHandler, self).prepare()

    async def options(self):
        cors = self.get_argument('cors', None)
        if not cors:
            return self.get()

        self.set_header('Access-Control-Allow-Credentials', 'true')
        self.set_header('Access-Control-Max-Age', 86400)
        if 'Access-Control-Request-Headers' in self.request.headers:
            self.set_header('Access-Control-Allow-Headers',
                            self.request.headers.get('Access-Control-Request-Headers'))
        if 'Access-Control-Request-Method' in self.request.headers:
            self.set_header('Access-Control-Allow-Methods',
                            self.request.headers.get('Access-Control-Request-Method'))
        self.set_status(204)
        await self.finish()

    async def get(self):
        logger.debug('Handle %s request to %s', self.request.method,
                     self.request.uri)

        def handle_response(response):
            if (response.error and not
                    isinstance(response.error, tornado.httpclient.HTTPError)):
                self.set_status(500)
                self.write('Internal server error:\n' + str(response.error))
            else:
                self.set_status(response.code, response.reason)
                self._headers = tornado.httputil.HTTPHeaders() # clear tornado default header
                
                for header, v in response.headers.get_all():
                    if header not in ('Content-Length', 'Transfer-Encoding', 'Content-Encoding', 'Connection'):
                        self.add_header(header, v) # some header appear multiple times, eg 'Set-Cookie'
                
                if response.body:                   
                    self.set_header('Content-Length', len(response.body))
                    self.write(response.body)

        body = self.request.body
        if not body:
            body = None
        try:
            if 'Proxy-Connection' in self.request.headers:
                del self.request.headers['Proxy-Connection'] 
            resp = await fetch_request(
                self.request.uri,
                method=self.request.method, body=body,
                headers=self.request.headers, follow_redirects=False,
                allow_nonstandard_methods=True)
            handle_response(resp)
        except tornado.httpclient.HTTPError as e:
            if hasattr(e, 'response') and e.response:
                handle_response(e.response)
            else:
                self.set_status(500)
                self.write('Internal server error:\n' + str(e))
        finally:
            await self.finish()

    put = get
    post = get
    head = get
    patch = get
    delete = get

    async def connect(self):
        logger.debug('Start CONNECT to %s', self.request.uri)
        host, port = self.request.uri.split(':')
        client = self.request.connection.stream

        async def start_tunnel():
            logger.debug('CONNECT tunnel established to %s', self.request.uri)
            client.write(b'HTTP/1.0 200 Connection established\r\n\r\n')
            await asyncio.gather(
                    relay_stream(client, upstream),
                    relay_stream(upstream, client)
            )
            client.close()
            upstream.close()

        async def on_proxy_response(data=None):
            if data:
                first_line = data.splitlines()[0]
                http_v, status, text = first_line.split(None, 2)
                if int(status) == 200:
                    logger.debug('Connected to upstream proxy %s', proxy)
                    await start_tunnel()
                    return

            self.set_status(500)
            await self.finish()

        async def start_proxy_tunnel():
            await upstream.write(b'CONNECT %s HTTP/1.1\r\n' % self.request.uri)
            await upstream.write(b'Host: %s\r\n' % self.request.uri)
            await upstream.write(b'Proxy-Connection: Keep-Alive\r\n\r\n')
            data = await upstream.read_until(b'\r\n\r\n')
            await on_proxy_response(data)

        resolver = tornado.netutil.Resolver()
        tcpclient = tornado.tcpclient.TCPClient(
            resolver=tornado.netutil.OverrideResolver(resolver, mapping=hostname_mapping))

        proxy = get_proxy(self.request.uri)
        if proxy:
            proxy_host, proxy_port = parse_proxy(proxy)
            upstream = await tcpclient.connect(proxy_host, proxy_port)
            await start_proxy_tunnel()
        else:
            upstream = await tcpclient.connect(host, int(port))
            await start_tunnel()


def run_proxy(address, port, start_ioloop=True):
    """
    Run proxy on the specified port. If start_ioloop is True (default),
    the tornado IOLoop will be started immediately.
    """
    app = tornado.web.Application([
        (r'.*', ProxyHandler),
    ])
    app.listen(port=port, address=address)
    ioloop = tornado.ioloop.IOLoop.current()

    if start_ioloop:
        ioloop.start()


if __name__ == '__main__':
    address = "0.0.0.0"
    port = 8888
    if len(sys.argv) > 1:
        address = sys.argv[1]
        port = int(sys.argv[2])

    print("Starting HTTP proxy on %s:%s" % (address, port))
    run_proxy(address, port)
