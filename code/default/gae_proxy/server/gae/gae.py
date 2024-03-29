#!/usr/bin/env python
# coding:utf-8


# GAE limit:
# only support http/https request, don't support tcp/udp connect for unpaid user.
# max timeout for every request is 60 seconds
# max upload data size is 30M
# max download data size is 10M

# How to Download file large then 10M?
# HTTP protocol support range fetch.
# If server return header include "accept-ranges", then client can request special range
# by put Content-Range in request header.
#
# GAE server will return 206 status code if file is too large and server support range fetch.
# Then GAE_proxy local client will switch to range fetch mode.


__version__ = '3.4.0'
__password__ = ''
__hostsdeny__ = ()

import os
import re
import time
from datetime import timedelta, datetime, tzinfo
import struct
import zlib
import base64
import logging
import urlparse
import httplib
import io
import string
import traceback
from mimetypes import guess_type

from google.appengine.api import urlfetch
from google.appengine.api.taskqueue.taskqueue import MAX_URL_LENGTH
from google.appengine.runtime import apiproxy_errors
from google.appengine.api import memcache

URLFETCH_MAX = 2
URLFETCH_MAXSIZE = 4 * 1024 * 1024
URLFETCH_DEFLATE_MAXSIZE = 4 * 1024 * 1024
URLFETCH_TIMEOUT = 30
allowed_traffic = 1024 * 1024 * 1024 * 0.9


def message_html(title, banner, detail=''):
    MESSAGE_TEMPLATE = '''
    <html><head>
    <meta http-equiv="content-type" content="text/html;charset=utf-8">
    <title>$title</title>
    <style><!--
    body {font-family: arial,sans-serif}
    div.nav {margin-top: 1ex}
    div.nav A {font-size: 10pt; font-family: arial,sans-serif}
    span.nav {font-size: 10pt; font-family: arial,sans-serif; font-weight: bold}
    div.nav A,span.big {font-size: 12pt; color: #0000cc}
    div.nav A {font-size: 10pt; color: black}
    A.l:link {color: #6f6f6f}
    A.u:link {color: green}
    //--></style>
    </head>
    <body text=#000000 bgcolor=#ffffff>
    <table border=0 cellpadding=2 cellspacing=0 width=100%>
    <tr><td bgcolor=#3366cc><font face=arial,sans-serif color=#ffffff><b>Message From FetchServer</b></td></tr>
    <tr><td> </td></tr></table>
    <blockquote>
    <H1>$banner</H1>
    $detail
    <p>
    </blockquote>
    <table width=100% cellpadding=0 cellspacing=0><tr><td bgcolor=#3366cc><img alt="" width=1 height=4></td></tr></table>
    </body></html>
    '''
    return string.Template(MESSAGE_TEMPLATE).substitute(title=title, banner=banner, detail=detail)


try:
    from Crypto.Cipher.ARC4 import new as RC4Cipher
except ImportError:
    logging.warn('Load Crypto.Cipher.ARC4 Failed, Use Pure Python Instead.')


    class RC4Cipher(object):
        def __init__(self, key):
            x = 0
            box = range(256)
            for i, y in enumerate(box):
                x = (x + y + ord(key[i % len(key)])) & 0xff
                box[i], box[x] = box[x], y
            self.__box = box
            self.__x = 0
            self.__y = 0

        def encrypt(self, data):
            out = []
            out_append = out.append
            x = self.__x
            y = self.__y
            box = self.__box
            for char in data:
                x = (x + 1) & 0xff
                y = (y + box[x]) & 0xff
                box[x], box[y] = box[y], box[x]
                out_append(chr(ord(char) ^ box[(box[x] + box[y]) & 0xff]))
            self.__x = x
            self.__y = y
            return ''.join(out)


def inflate(data):
    return zlib.decompress(data, -zlib.MAX_WBITS)


def deflate(data):
    return zlib.compress(data)[2:-4]


def format_response(status, headers, content):
    if content:
        headers.pop('content-length', None)
        headers['Content-Length'] = str(len(content))
    data = 'HTTP/1.1 %d %s\r\n%s\r\n\r\n%s' % \
           (status,
            httplib.responses.get(status, 'Unknown'),
            '\r\n'.join('%s: %s' % (k.title(), v) for k, v in headers.items()),
            content)
    data = deflate(data)
    return struct.pack('!h', len(data)) + data


def is_text_content_type(content_type):
    mct, _, sct = content_type.partition('/')
    if mct == 'text':
        return True
    if mct == 'application':
        sct = sct.split(';', 1)[0]
        if (sct in ('json', 'javascript', 'x-www-form-urlencoded') or
                sct.endswith(('xml', 'script')) or
                sct.startswith(('xml', 'rss', 'atom'))):
            return True
    return False


def is_deflate(data):
    if len(data) > 1:
        CMF, FLG = bytearray(data[:2])
        if CMF & 0x0F == 8 and CMF & 0x80 == 0 and ((CMF << 8) + FLG) % 31 == 0:
            return True
    if len(data) > 0:
        try:
            decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
            decompressor.decompress(data[:1024])
            return decompressor.unused_data == ''
        except:
            return False
    return False


class Pacific(tzinfo):
    def utcoffset(self, dt):
        return timedelta(hours=-8) + self.dst(dt)

    def dst(self, dt):
        # DST starts last Sunday in March
        d = datetime(dt.year, 3, 12)   # ends last Sunday in October
        self.dston = d - timedelta(days=d.weekday() + 1)
        d = datetime(dt.year, 11, 6)
        self.dstoff = d - timedelta(days=d.weekday() + 1)
        if self.dston <=  dt.replace(tzinfo=None) < self.dstoff:
            return timedelta(hours=1)
        else:
            return timedelta(0)

    def tzname(self,dt):
         return "Pacific"


def get_pacific_date():
    tz = Pacific()
    sa_time = datetime.now(tz)
    return sa_time.strftime('%Y-%m-%d')


def traffic(environ, start_response):
    try:
        reset_date = memcache.get(key="reset_date")
    except:
        reset_date = None

    try:
        traffic_sum = memcache.get(key="traffic")
        if not traffic_sum:
            traffic_sum = "0"
    except Exception as e:
        traffic_sum = "0"

    start_response('200 OK', [('Content-Type', 'text/plain')])
    yield 'traffic:%s\r\n' % traffic_sum
    yield 'Reset date:%s\r\n' % reset_date
    yield 'Usage: %f %%\r\n' % int(int(traffic_sum) * 100 / allowed_traffic)

    tz = Pacific()
    sa_time = datetime.now(tz)
    pacific_time = sa_time.strftime('%Y-%m-%d %H:%M:%S')
    yield "American Pacific time:%s" % pacific_time

    raise StopIteration


def reset(environ, start_response):
    try:
        memcache.set(key="traffic", value="0")
    except:
        pass

    start_response('200 OK', [('Content-Type', 'text/plain')])
    yield 'traffic reset finished.'
    raise StopIteration


def is_traffic_exceed():
    try:
        reset_date = memcache.get(key="reset_date")
    except:
        reset_date = None

    pacific_date = get_pacific_date()
    if reset_date != pacific_date:
        memcache.set(key="reset_date", value=pacific_date)
        memcache.set(key="traffic", value="0")
        return False

    try:
        traffic_sum = int(memcache.get(key="traffic"))
    except:
        traffic_sum = 0

    if traffic_sum > allowed_traffic:
        return True
    else:
        return False


def count_traffic(add_traffic):
    try:
        traffic_sum = int(memcache.get(key="traffic"))
    except:
        traffic_sum = 0

    try:
        v = str(traffic_sum + add_traffic)
        memcache.set(key="traffic", value=v)
    except Exception as e:
        logging.exception('memcache.set fail:%r', e)


def application(environ, start_response):
    if environ['REQUEST_METHOD'] == 'GET' and 'HTTP_X_URLFETCH_PS1' not in environ:
        # xxnet 自用
        timestamp = long(os.environ['CURRENT_VERSION_ID'].split('.')[1]) / 2 ** 28
        ctime = time.strftime(
            '%Y-%m-%d %H:%M:%S',
            time.gmtime(
                timestamp + 8 * 3600))
        start_response('200 OK', [('Content-Type', 'text/plain')])
        yield 'GoAgent Python Server %s works, deployed at %s\n' % (__version__, ctime)
        if len(__password__) > 2:
            yield 'Password: %s%s%s' % (__password__[0], '*' * (len(__password__) - 2), __password__[-1])
        raise StopIteration

    start_response('200 OK', [('Content-Type', 'image/gif'), ('X-Server', 'GPS ' + __version__)])

    if environ['REQUEST_METHOD'] == 'HEAD':
        raise StopIteration
        # 请求头则已经完成

    options = environ.get('HTTP_X_URLFETCH_OPTIONS', '')
    # 不知道怎么直接获得的
    # 但一般，此段语句无用
    if 'rc4' in options and not __password__:
        # 如果客户端需要加密，但ｇａｅ无密码

        # 但rc4 如不改源码，则恒为假
        yield format_response(400,
                              {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('400 Bad Request',
                                           'Bad Request (options) - please set __password__ in gae.py',
                                           'please set __password__ and upload gae.py again'))
        raise StopIteration

    try:
        if 'HTTP_X_URLFETCH_PS1' in environ:
            # 第一部分
            payload = inflate(base64.b64decode(environ['HTTP_X_URLFETCH_PS1']))
            body = inflate(
                base64.b64decode(
                    # 第二部分　即原始ｂｏｄｙ
                    environ['HTTP_X_URLFETCH_PS2'])) if 'HTTP_X_URLFETCH_PS2' in environ else ''
        else:
            # POST
            # POST 获取数据的方式
            wsgi_input = environ['wsgi.input']
            input_data = wsgi_input.read(int(environ.get('CONTENT_LENGTH', '0')))

            if 'rc4' in options:
                input_data = RC4Cipher(__password__).encrypt(input_data)
            payload_length, = struct.unpack('!h', input_data[:2])  # 获取长度
            payload = inflate(input_data[2:2 + payload_length])  # 获取负载
            body = input_data[2 + payload_length:]  # 获取ｂｏｄｙ

        count_traffic(len(input_data))
        raw_response_line, payload = payload.split('\r\n', 1)
        method, url = raw_response_line.split()[:2]
        # http content:
        # 此为ｂｏｄｙ
        # {
        # pack_req_head_len: 2 bytes,＃ＰＯＳＴ　时使用

        # pack_req_head : deflate{
        # 此为负载
        # original request line,
        # original request headers,
        # X-URLFETCH-kwargs HEADS, {
        # password,
        # maxsize, defined in config AUTO RANGE MAX SIZE
        # timeout, request timeout for GAE urlfetch.
        # }
        # }
        # body
        # }

        headers = {}
        # 获取　原始头
        for line in payload.splitlines():
            key, value = line.split(':', 1)
            headers[key.title()] = value.strip()
    except (zlib.error, KeyError, ValueError):
        yield format_response(500,
                              {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('500 Internal Server Error',
                                           'Bad Request (payload) - Possible Wrong Password',
                                           '<pre>%s</pre>' % traceback.format_exc()))
        raise StopIteration

    # 获取ｇａｅ用的头
    kwargs = {}
    any(kwargs.__setitem__(x[len('x-urlfetch-'):].lower(), headers.pop(x)) for x in headers.keys() if
        x.lower().startswith('x-urlfetch-'))

    if 'Content-Encoding' in headers and body:
        # fix bug for LinkedIn android client
        if headers['Content-Encoding'] == 'deflate':
            try:
                body2 = inflate(body)
                headers['Content-Length'] = str(len(body2))
                del headers['Content-Encoding']
                body = body2
            except BaseException:
                pass

    ref = headers.get('Referer', '')
    logging.info('%s "%s %s %s" - -', environ['REMOTE_ADDR'], method, url, ref)

    # 参数使用
    if __password__ and __password__ != kwargs.get('password', ''):
        yield format_response(401, {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('403 Wrong password', 'Wrong password(%r)' % kwargs.get('password', ''),
                                           'GoAgent proxy.ini password is wrong!'))
        raise StopIteration

    netloc = urlparse.urlparse(url).netloc
    if is_traffic_exceed():
        yield format_response(510, {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('510 Traffic exceed',
                                           'Traffic exceed',
                                           'Traffic exceed!'))
        raise StopIteration

    if len(url) > MAX_URL_LENGTH:
        yield format_response(400,
                              {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('400 Bad Request',
                                           'length of URL too long(greater than %r)' % MAX_URL_LENGTH,
                                           detail='url=%r' % url))
        raise StopIteration

    if netloc.startswith(('127.0.0.', '::1', 'localhost')):
        # 测试用
        yield format_response(400, {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('GoAgent %s is Running' % __version__, 'Now you can visit some websites',
                                           ''.join('<a href="https://%s/">%s</a><br/>' % (x, x) for x in
                                                   ('google.com', 'mail.google.com'))))
        raise StopIteration

    fetchmethod = getattr(urlfetch, method, None)
    if not fetchmethod:
        yield format_response(405, {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('405 Method Not Allowed', 'Method Not Allowed: %r' % method,
                                           detail='Method Not Allowed URL=%r' % url))
        raise StopIteration

    timeout = int(kwargs.get('timeout', URLFETCH_TIMEOUT))
    validate_certificate = bool(int(kwargs.get('validate', 0)))
    maxsize = int(kwargs.get('maxsize', 0))
    # https://www.freebsdchina.org/forum/viewtopic.php?t=54269
    accept_encoding = headers.get('Accept-Encoding', '') or headers.get('Bccept-Encoding', '')
    errors = []
    allow_truncated = False
    for i in xrange(int(kwargs.get('fetchmax', URLFETCH_MAX))):
        try:
            response = urlfetch.fetch(
                url,
                body,
                fetchmethod,
                headers,
                allow_truncated=allow_truncated,
                follow_redirects=False,
                deadline=timeout,
                validate_certificate=validate_certificate)
            # 获取真正ｒｅｓｐｏｎｓｅ
            break
        except apiproxy_errors.OverQuotaError as e:
            time.sleep(5)
        except urlfetch.DeadlineExceededError as e:
            errors.append('%r, timeout=%s' % (e, timeout))
            logging.error(
                'DeadlineExceededError(timeout=%s, url=%r)',
                timeout,
                url)
            time.sleep(1)

            # 必须truncaated
            allow_truncated = True
            m = re.search(r'=\s*(\d+)-', headers.get('Range')
                          or headers.get('range') or '')
            if m is None:
                headers['Range'] = 'bytes=0-%d' % (maxsize or URLFETCH_MAXSIZE)
            else:
                headers.pop('Range', '')
                headers.pop('range', '')
                start = int(m.group(1))
                headers['Range'] = 'bytes=%s-%d' % (start,
                                                    start + (maxsize or URLFETCH_MAXSIZE))

            timeout *= 2
        except urlfetch.DownloadError as e:
            errors.append('%r, timeout=%s' % (e, timeout))
            logging.error('DownloadError(timeout=%s, url=%r)', timeout, url)
            time.sleep(1)
            timeout *= 2
        except urlfetch.ResponseTooLargeError as e:
            errors.append('%r, timeout=%s' % (e, timeout))
            response = e.response
            logging.error(
                'ResponseTooLargeError(timeout=%s, url=%r) response(%r)',
                timeout,
                url,
                response)

            m = re.search(r'=\s*(\d+)-', headers.get('Range')
                          or headers.get('range') or '')
            if m is None:
                headers['Range'] = 'bytes=0-%d' % (maxsize or URLFETCH_MAXSIZE)
            else:
                headers.pop('Range', '')
                headers.pop('range', '')
                start = int(m.group(1))
                headers['Range'] = 'bytes=%s-%d' % (start,
                                                    start + (maxsize or URLFETCH_MAXSIZE))
            timeout *= 2
        except urlfetch.SSLCertificateError as e:
            errors.append('%r, should validate=0 ?' % e)
            logging.error('%r, timeout=%s', e, timeout)
        except Exception as e:
            errors.append(str(e))
            stack_str = "stack:%s" % traceback.format_exc()
            errors.append(stack_str)
            if i == 0 and method == 'GET':
                timeout *= 2
    else:
        error_string = '<br />\n'.join(errors)
        if not error_string:
            logurl = 'https://appengine.google.com/logs?&app_id=%s' % os.environ['APPLICATION_ID']
            error_string = 'Internal Server Error. <p/>try <a href="javascript:window.location.reload(true);">refresh' \
                           '</a> or goto <a href="%s" target="_blank">appengine.google.com</a> for details' % logurl
        yield format_response(502, {'Content-Type': 'text/html; charset=utf-8'},
                              message_html('502 Urlfetch Error', 'Python Urlfetch Error: %r' % method, error_string))
        raise StopIteration

    # logging.debug('url=%r response.status_code=%r response.headers=%r response.content[:1024]=%r', url,
    # response.status_code, dict(response.headers), response.content[:1024])

    # 以上实现ｆｅｔｃｈ 的细节

    status_code = int(response.status_code)
    data = response.content
    response_headers = response.headers
    response_headers['X-Head-Content-Length'] = response_headers.get(
        'Content-Length', '')
    # for k in response_headers:
    #    v = response_headers[k]
    #    logging.debug("Head:%s: %s", k, v)
    content_type = response_headers.get('content-type', '')
    content_encoding = response_headers.get('content-encoding', '')
    # 也是分片合并之类的细节
    if status_code == 200 and maxsize and len(data) > maxsize and response_headers.get(
            'accept-ranges', '').lower() == 'bytes' and int(response_headers.get('content-length', 0)):
        logging.debug("data len:%d max:%d", len(data), maxsize)
        status_code = 206
        response_headers['Content-Range'] = 'bytes 0-%d/%d' % (
            maxsize - 1, len(data))
        data = data[:maxsize]
    if 'gzip' in accept_encoding:
        if (data and status_code == 200 and
                content_encoding == '' and
                is_text_content_type(content_type) and
                is_deflate(data)):
            # ignore wrong "Content-Type"
            type = guess_type(url)[0]
            if type is None or is_text_content_type(type):
                if 'deflate' in accept_encoding:
                    response_headers['Content-Encoding'] = content_encoding = 'deflate'
                else:
                    data = inflate(data)
    else:
        if content_encoding in ('gzip', 'deflate', 'br'):
            del response_headers['Content-Encoding']
            content_encoding = ''
    if status_code == 200 and content_encoding == '' and 512 < len(
            data) < URLFETCH_DEFLATE_MAXSIZE and is_text_content_type(content_type):
        if 'gzip' in accept_encoding:
            response_headers['Content-Encoding'] = 'gzip'
            compressobj = zlib.compressobj(
                zlib.Z_DEFAULT_COMPRESSION, zlib.DEFLATED, -zlib.MAX_WBITS, zlib.DEF_MEM_LEVEL, 0)
            dataio = io.BytesIO()
            dataio.write('\x1f\x8b\x08\x00\x00\x00\x00\x00\x02\xff')
            dataio.write(compressobj.compress(data))
            dataio.write(compressobj.flush())
            dataio.write(
                struct.pack(
                    '<LL',
                    zlib.crc32(data) & 0xFFFFFFFF,
                    len(data) & 0xFFFFFFFF))
            data = dataio.getvalue()
        elif 'deflate' in accept_encoding:
            response_headers['Content-Encoding'] = 'deflate'
            data = deflate(data)
    response_headers['Content-Length'] = str(len(data))
    if 'rc4' not in options:
        yield format_response(status_code, response_headers, '')
        yield data
    else:
        cipher = RC4Cipher(__password__)
        yield cipher.encrypt(format_response(status_code, response_headers, ''))
        yield cipher.encrypt(data)

    count_traffic(len(data))
