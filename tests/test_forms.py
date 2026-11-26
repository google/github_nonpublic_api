# Copyright 2026 The Authors (see AUTHORS file)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for forms.py."""

import base64
import hashlib
import hmac
import os
import socket
import ssl
import threading
import time
import unittest
from unittest import mock

import html5lib
import requests
from requests.exceptions import ChunkedEncodingError, InvalidURL, SSLError
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import MaxRetryError, NewConnectionError

from github_nonpublic_api import forms

BROWSER_FORM_EDGE_CASES_HTML = os.path.join(
    os.path.dirname(__file__), 'browser_form_edge_cases.html')


def _resp(text='', url='https://h/p', status=200, history=None, headers=None):
    r = mock.Mock(spec=requests.Response)
    r.text = text
    r.url = url
    r.status_code = status
    r.history = history or []
    r.headers = headers or {}
    return r


class BrowserFormDataTest(unittest.TestCase):
    """Rules of browser_form_data (the HTML "constructing the entry list")."""

    def _data(self, html, **kw):
        doc = html5lib.parse(html, namespaceHTMLElements=False)
        return forms.browser_form_data(doc, doc.find('.//form'), **kw)

    def test_controls_and_defaults(self):
        html = """<form id="f">
          <input name="t" value="v"><input name="empty"><input type="hidden" name="h" value="1">
          <input type="checkbox" name="c1" checked><input type="checkbox" name="c2" value="x">
          <input type="radio" name="r" value="a"><input type="radio" name="r" value="b" checked>
          <select name="s"><option value="1">one</option><option selected>two</option></select>
          <select name="s2"><option disabled>x</option><option>y</option></select>
          <textarea name="ta">hi</textarea>
          <input type="file" name="file"><input type="reset" name="reset"><button type="button" name="b">b</button>
          <input name="dis" value="d" disabled>
          <fieldset disabled><input name="fs" value="1"><legend><input name="lg" value="1"></legend></fieldset>
          <template><input name="tpl" value="1"></template>
          <input name="other" form="g" value="1">
          <button name="go" value="1">Go</button><button name="go2" value="2">Go2</button>
        </form><input name="outside" form="f" value="o">"""
        self.assertEqual(self._data(html), [
            ('t', 'v'), ('empty', ''), ('h', '1'), ('c1', 'on'), ('r', 'b'), ('s', 'two'),
            ('s2', 'y'), ('ta', 'hi'), ('file', ''),
            ('lg', '1'),  # inside a disabled fieldset's first <legend>: not disabled
            ('go', '1'), ('outside', 'o')])

    def test_edits(self):
        html = """<form><input name="t"><input type="hidden" name="p" value="none">
          <input type="hidden" name="a" value="0"><input type="checkbox" name="a" value="1" checked>
          <input type="radio" name="r" value="x" checked><input type="radio" name="r" value="y">
          <select name="s"><option>1</option><option>2</option></select></form>"""
        self.assertEqual(
            self._data(html, values={'t': 'T', 'p': 'read', 's': '2'},
                       checked={'a': False, 'r': 'y'}),
            [('t', 'T'), ('p', 'read'), ('a', '0'), ('r', 'y'), ('s', '2')])

    def test_explicit_submitter(self):
        html = """<form><input name="t" value="v">
          <button name="go" value="1">Go</button><button name="go2" value="2">Go2</button></form>"""
        doc = html5lib.parse(html, namespaceHTMLElements=False)
        form = doc.find('.//form')
        second = [b for b in form.iter() if b.tag == 'button'][1]
        self.assertEqual(forms.browser_form_data(doc, form, submitter=second),
                         [('t', 'v'), ('go2', '2')])

    def test_unknown_edit_raises_page_shape_error(self):
        with self.assertRaisesRegex(forms.PageShapeError, 'no controls named'):
            self._data("<form><input name='t'></form>", values={'nope': '1'})
        with self.assertRaisesRegex(ValueError, r"no controls named \['nope'\]"):
            self._data("<form><input name='t'></form>", checked={'nope': True})

    def test_form_inside_template_does_not_end_the_outer_form(self):
        # html5lib 1.1 closes the outer form at a templated </form>; browsers
        # don't. GitHub's New GitHub App page lost its submit button this way.
        html = """<form action="/x"><input name="a" value="1">
          <template><form><input name="x" value="2"></form></template>
          <input name="b" value="3"><input type="submit" name="commit" value="Go"></form>"""
        doc = forms.parse_page(html)
        data = forms.browser_form_data(doc, doc.find('.//form'))
        self.assertEqual(data, [('a', '1'), ('b', '3'), ('commit', 'Go')])

    def test_plain_html5lib_shows_the_template_bug(self):
        # Documents why parse_page exists; if html5lib fixes this, the
        # template stripping can be removed.
        html = """<form action="/x"><input name="a" value="1">
          <template><form><input name="x" value="2"></form></template>
          <input name="b" value="3"></form>"""
        doc = html5lib.parse(html, namespaceHTMLElements=False)
        self.assertNotIn(('b', '3'), forms.browser_form_data(doc, doc.find('.//form')))

    def test_edge_case_fixture(self):
        with open(BROWSER_FORM_EDGE_CASES_HTML) as fp:
            doc = forms.parse_page(fp.read())
        all_forms = doc.findall('.//form')
        self.assertEqual(len(all_forms), 3)  # templated forms are not in the document
        self.assertEqual(forms.browser_form_data(doc, all_forms[0])[-2:],
                         [('go', '1'), ('outside', 'o')])
        self.assertEqual(forms.browser_form_data(doc, all_forms[1]), [
            ('t', ''), ('p', 'none'), ('a', '0'), ('a', '1'), ('r', 'x'), ('s', '1'),
            ('sub', 'Save')])  # the image input is neither the submitter nor data
        self.assertEqual(forms.browser_form_data(doc, all_forms[2]),
                         [('a', '1'), ('b', '3'), ('commit', 'Go')])


class ParsePageTest(unittest.TestCase):

    def test_nested_templates_are_emptied(self):
        html = '<div><template><template><input name="y"></template><p>x</p></template></div>'
        self.assertEqual(forms._strip_template_contents(html),
                         '<div><template></template></div>')

    def test_page_without_templates_is_unchanged(self):
        html = '<form action="/a"><input name="x" value="1"></form>'
        self.assertEqual(forms._strip_template_contents(html), html)


class FindFormByActionTest(unittest.TestCase):

    PAGE = """
      <form action="/search"><input name="q"></form>
      <form action="/orgs/o/apps/a?x=1"><input name="_method" value="put"></form>
      <form action="/orgs/o/apps/a/"><input name="_method" value="DELETE"></form>"""

    def setUp(self):
        self.doc = forms.parse_page(self.PAGE)

    def test_matches_path_ignoring_query_and_trailing_slash(self):
        form = forms.find_form_by_action(self.doc, '/orgs/o/apps/a')
        self.assertEqual(form.attrib['action'], '/orgs/o/apps/a?x=1')

    def test_matches_rails_method_case_insensitively(self):
        form = forms.find_form_by_action(self.doc, '/orgs/o/apps/a', method='delete')
        self.assertEqual(form.attrib['action'], '/orgs/o/apps/a/')

    def test_no_match(self):
        self.assertIsNone(forms.find_form_by_action(self.doc, '/nope'))
        self.assertIsNone(forms.find_form_by_action(self.doc, '/search', method='put'))


class BrowserControlsTest(unittest.TestCase):

    def test_submit_buttons(self):
        doc = forms.parse_page(
            '<form><button>a</button><button type="reset">b</button>'
            '<input type="image"><input type="submit"><input type="button"></form>')
        kinds = [forms.is_submit_button(c) for c in forms.browser_controls(doc, doc.find('.//form'))[0]]
        self.assertEqual(kinds, [True, False, True, True, False])


class PageErrorsTest(unittest.TestCase):

    def test_ignores_hidden_page_furniture(self):
        # Hidden error markup modeled on GitHub's New GitHub App page.
        page = """
          <template class="js-flash-template"><div class="flash flash-error">tpl</div></template>
          <div id="ajax-error-message" class="ajax-error-message flash flash-error" hidden>
            You can't perform that action at this time.</div>
          <dd class="sub-permissions-error error position-static" hidden="">One or more paths were too long</dd>
          <div class="gutter js-send-events d-none"><span class="flash flash-error">Change privileges to be able to select events</span></div>
          <dialog><div class="flash flash-error">closed dialog</div></dialog>
          <div aria-hidden="true"><p class="error">aria hidden</p></div>
          <div class="flash flash-error">Name has already been taken</div>"""
        self.assertEqual(forms.page_errors(page), ['Name has already been taken'])
        self.assertEqual(forms.page_errors(page.rsplit('<div', 1)[0]), [])

    def test_falls_back_to_inline_without_flash(self):
        self.assertEqual(
            forms.page_errors('<p class="note error">Homepage URL is invalid</p>'),
            ['Homepage URL is invalid'])
        self.assertEqual(forms.page_errors('<p>ok</p>'), [])

    def test_flash_wins_over_inline(self):
        page = ('<p class="error">inline</p>'
                '<div class="flash flash-error">  Flash\n  text </div>')
        self.assertEqual(forms.page_errors(page), ['Flash text'])

    def test_limit_dedup_and_truncation(self):
        page = ''.join(f'<p class="error">e{i}</p>' for i in range(10))
        page += '<p class="error">e0</p>'
        self.assertEqual(forms.page_errors(page, limit=3), ['e0', 'e1', 'e2'])
        long_text = 'x' * 400
        self.assertEqual(forms.page_errors(f'<p class="error">{long_text}</p>'), ['x' * 300])


class ResponseSummaryTest(unittest.TestCase):

    def test_with_redirects_and_title(self):
        redirect = _resp(status=302, headers={'Location': 'https://h/login'})
        response = _resp('<title> Sign in </title><p>secret body</p>', 'https://h/login',
                         history=[redirect])
        summary = forms.response_summary(response)
        self.assertEqual(
            summary,
            "HTTP 200 at https://h/login after redirects [302 https://h/login]; "
            "page title 'Sign in'")
        self.assertNotIn('secret', summary)

    def test_without_redirects_or_title(self):
        self.assertEqual(forms.response_summary(_resp('', 'https://h/x', status=500)),
                         'HTTP 500 at https://h/x (no redirect)')


class DescribeFormsTest(unittest.TestCase):

    def test_never_returns_token_values(self):
        page = """<form action="/a"><input name="authenticity_token" value="secret"/>
                  <input name="x" value="v"/><select name="s"></select></form>"""
        session = mock.Mock(spec=requests.Session)
        session.request.side_effect = [_resp(page, 'https://h/p')]
        described = forms.describe_forms(session, 'https://h/p')
        self.assertEqual([c['name'] for c in described[0]['controls']],
                         ['authenticity_token', 'x', 's'])
        self.assertEqual(described[0]['controls'][1]['value'], 'v')
        self.assertEqual(described[0]['browser_submits'], ['authenticity_token', 'x'])
        self.assertNotIn('secret', str(described))
        session.request.assert_called_once_with(method='GET', url='https://h/p', timeout=30)

    def test_redacts_token_like_values(self):
        page = """<form action="/a"><input type="hidden" value="FAKEtoken0123456789abcdefFAKE000"/>
                  <input type="hidden" name="integration_fingerprint" value="abc123XYZ987def"/>
                  <input type="hidden" name="p" value="none"/></form>"""
        text = str(forms.describe_document_forms(forms.parse_page(page)))
        self.assertNotIn('FAKEtoken', text)
        self.assertNotIn('abc123', text)
        self.assertIn("'none'", text)

    def test_flags_and_attributes(self):
        page = """<form action="/a" method="post" id="f"><div hidden class="x y z">
                  <input name="h" value="1" disabled required></div>
                  <button type="button">menu</button><button type="button">menu</button>
                  <select name="s"><option value="a" selected>A</option></select></form>"""
        described = forms.describe_document_forms(forms.parse_page(page))[0]
        self.assertEqual(described['method'], 'post')
        self.assertEqual(described['unnamed_type_button'], 2)
        hidden = described['controls'][0]
        self.assertEqual(hidden['flags'], ['hidden-ancestor:div.x y'])
        self.assertTrue(hidden['disabled'])
        self.assertTrue(hidden['required'])
        self.assertEqual(described['controls'][1]['options'], [('a', True)])
        self.assertEqual(described['browser_submits'], ['s'])

    def test_flags_hidden_ancestor_without_class_and_template(self):
        doc = html5lib.parse('<form><div hidden><input name="n"></div>'
                             '<template><input name="t"></template></form>',
                             namespaceHTMLElements=False)
        controls = forms.describe_document_forms(doc)[0]['controls']
        self.assertEqual([c['flags'] for c in controls],
                         [['hidden-ancestor:div'], ['in-template']])


class RetryDelayParsingTest(unittest.TestCase):

    def test_numeric_retry_after(self):
        delay = forms.parse_retry_delay(_resp(headers={'Retry-After': '45'}), attempt=0)
        self.assertGreaterEqual(delay, 45.0 + forms.JITTER_MIN_SECONDS)
        self.assertLessEqual(delay, 45.0 + forms.JITTER_MAX_SECONDS)

    def test_retry_after_is_capped(self):
        delay = forms.parse_retry_delay(_resp(headers={'Retry-After': '9999'}), attempt=0)
        self.assertLessEqual(delay, forms.MAX_DELAY_SECONDS + forms.JITTER_MAX_SECONDS)

    def test_invalid_retry_after_falls_back_to_backoff(self):
        delay = forms.parse_retry_delay(_resp(headers={'Retry-After': 'soon'}), attempt=2)
        self.assertGreaterEqual(delay, 8.0 + forms.JITTER_MIN_SECONDS)
        self.assertLessEqual(delay, 8.0 + forms.JITTER_MAX_SECONDS)

    def test_rate_limit_reset_epoch(self):
        reset = str(time.time() + 20.0)
        delay = forms.parse_retry_delay(_resp(headers={'X-RateLimit-Reset': reset}), attempt=0)
        self.assertGreaterEqual(delay, 19.5 + forms.JITTER_MIN_SECONDS)
        self.assertLessEqual(delay, 20.5 + forms.JITTER_MAX_SECONDS)

    def test_exponential_backoff_without_headers(self):
        delay = forms.parse_retry_delay(_resp(), attempt=3)
        self.assertGreaterEqual(delay, 16.0 + forms.JITTER_MIN_SECONDS)
        self.assertLessEqual(delay, 16.0 + forms.JITTER_MAX_SECONDS)


class IsRetryableTest(unittest.TestCase):

    def test_retryable_statuses(self):
        self.assertTrue(forms.is_retryable(_resp(status=429)))
        self.assertTrue(forms.is_retryable(_resp(status=403, headers={'Retry-After': '10'})))
        self.assertTrue(forms.is_retryable(
            _resp(status=403, headers={'X-RateLimit-Remaining': '0'})))
        self.assertTrue(forms.is_retryable(
            _resp('You have exceeded a secondary rate limit.', status=403)))
        for status in (500, 502, 503, 504):
            self.assertTrue(forms.is_retryable(_resp(status=status)))

    def test_not_retryable_statuses(self):
        self.assertFalse(forms.is_retryable(_resp('Must have admin rights', status=403)))
        for status in (400, 401, 404, 422, 501):
            self.assertFalse(forms.is_retryable(_resp(status=status)))


def _http_error_resp(status, headers=None, text=''):
    r = _resp(text, status=status, headers=headers or {})
    r.raise_for_status.side_effect = requests.HTTPError(f'{status} error', response=r)
    return r


@mock.patch('time.sleep', return_value=None)
class RequestWithRetryTest(unittest.TestCase):

    def setUp(self):
        self.session = mock.create_autospec(requests.Session, instance=True)

    def test_get_retried_on_429(self, mock_sleep):
        ok = _resp('OK')
        self.session.request.side_effect = [
            _http_error_resp(429, headers={'Retry-After': '2'}), ok]
        self.assertIs(forms.request_with_retry(self.session, 'GET', 'https://h/x'), ok)
        self.assertEqual(self.session.request.call_count, 2)
        mock_sleep.assert_called_once()
        self.assertGreaterEqual(mock_sleep.call_args[0][0], 2.0)

    def test_kwargs_are_passed_through(self, mock_sleep):
        self.session.request.return_value = _resp()
        forms.request_with_retry(self.session, 'GET', 'https://h/x', timeout=5, params={'a': 1})
        self.session.request.assert_called_once_with(
            method='GET', url='https://h/x', timeout=5, params={'a': 1})

    def test_unfollowed_redirect_is_returned(self, mock_sleep):
        redirect = _resp(status=302, headers={'Location': 'https://h/y'})
        self.session.request.return_value = redirect
        self.assertIs(
            forms.request_with_retry(self.session, 'POST', 'https://h/x', allow_redirects=False),
            redirect)

    def test_fatal_403_not_retried(self, mock_sleep):
        self.session.request.return_value = _http_error_resp(403, text='Forbidden')
        with self.assertRaises(requests.HTTPError):
            forms.request_with_retry(self.session, 'GET', 'https://h/x')
        self.assertEqual(self.session.request.call_count, 1)
        mock_sleep.assert_not_called()

    def test_gives_up_after_max_retries(self, mock_sleep):
        self.session.request.return_value = _http_error_resp(503)
        with self.assertRaises(requests.HTTPError):
            forms.request_with_retry(self.session, 'GET', 'https://h/x', max_retries=5)
        self.assertEqual(self.session.request.call_count, 6)
        self.assertEqual(mock_sleep.call_count, 5)

    def test_zero_and_negative_max_retries_send_once(self, mock_sleep):
        self.session.request.return_value = _http_error_resp(503)
        for max_retries in (0, -1):
            self.session.request.reset_mock()
            with self.assertRaises(requests.HTTPError):
                forms.request_with_retry(self.session, 'GET', 'https://h/x',
                                         max_retries=max_retries)
            self.assertEqual(self.session.request.call_count, 1)

    def test_post_not_retried_by_default(self, mock_sleep):
        for status in (500, 502, 503, 504, 429):
            self.session.request.reset_mock()
            self.session.request.return_value = _http_error_resp(status)
            with self.assertRaises(requests.HTTPError):
                forms.request_with_retry(self.session, 'POST', 'https://h/x', data={'a': '1'})
            self.assertEqual(self.session.request.call_count, 1, status)
        mock_sleep.assert_not_called()

    def test_lowercase_post_not_retried(self, mock_sleep):
        self.session.request.return_value = _http_error_resp(502)
        with self.assertRaises(requests.HTTPError):
            forms.request_with_retry(self.session, 'post', 'https://h/x')
        self.assertEqual(self.session.request.call_count, 1)

    def test_post_retried_when_enabled_and_payload_preserved(self, mock_sleep):
        ok = _resp('Submitted')
        self.session.request.side_effect = [_http_error_resp(502), ok]
        data = [('authenticity_token', 'csrf-123')]
        result = forms.request_with_retry(
            self.session, 'POST', 'https://h/x', retry_unsafe_methods=True, data=data)
        self.assertIs(result, ok)
        self.assertEqual(self.session.request.call_args_list, [
            mock.call(method='POST', url='https://h/x', data=data)] * 2)


class SubmitOnceTest(unittest.TestCase):

    def setUp(self):
        self.session = mock.create_autospec(requests.Session, instance=True)

    def test_returns_responses_below_500_without_resending(self):
        for status in (200, 302, 404, 422, 429):
            self.session.request.reset_mock()
            response = _resp(status=status)
            self.session.request.return_value = response
            self.assertIs(forms.submit_once(self.session, 'https://h/x', data=[('a', '1')],
                                            allow_redirects=False), response)
            self.session.request.assert_called_once_with(
                method='POST', url='https://h/x', data=[('a', '1')], allow_redirects=False)

    def test_server_error_is_unknown_outcome(self):
        self.session.request.return_value = _resp('<p>body</p>', 'https://h/x', status=503)
        with self.assertRaisesRegex(forms.OutcomeUnknownError, 'HTTP 503') as ctx:
            forms.submit_once(self.session, 'https://h/x', data=[])
        self.assertEqual(ctx.exception.summary, 'HTTP 503 at https://h/x (no redirect)')
        self.assertNotIn('body', str(ctx.exception))
        self.assertEqual(self.session.request.call_count, 1)

    def test_errors_after_sending_are_unknown_outcome(self):
        for error in (requests.ReadTimeout('read'), requests.ConnectionError('reset'),
                      ChunkedEncodingError('chunk')):
            self.session.request.side_effect = error
            with self.assertRaises(forms.OutcomeUnknownError) as ctx:
                forms.submit_once(self.session, 'https://h/x', data=[])
            self.assertIs(ctx.exception.__cause__, error)

    def test_errors_before_sending_are_reraised(self):
        refused = requests.ConnectionError(MaxRetryError(
            None, '/x', reason=NewConnectionError(None, 'refused')))
        for error in (requests.ConnectTimeout('connect'), refused, InvalidURL('bad')):
            self.session.request.side_effect = error
            with self.assertRaises(type(error)) as ctx:
                forms.submit_once(self.session, 'https://h/x', data=[])
            self.assertNotIsInstance(ctx.exception, forms.OutcomeUnknownError)

    def test_real_refused_connection_is_reraised(self):
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]  # closed again before the request
        session = requests.Session()
        session.trust_env = False  # connect to the loopback server, not an env proxy
        forms.configure_session_adapters(session)
        session.adapters['http://'].max_retries.backoff_factor = 0
        with self.assertRaises(requests.ConnectionError) as ctx:
            forms.submit_once(session, f'http://127.0.0.1:{port}/x', data=[('a', '1')], timeout=5)
        self.assertNotIsInstance(ctx.exception, forms.OutcomeUnknownError)

    def test_real_dropped_connection_is_unknown_outcome_and_not_resent(self):
        server = socket.socket()
        server.bind(('127.0.0.1', 0))
        server.listen()
        server.settimeout(5)
        accepted = []

        def serve_one():
            conn, _ = server.accept()
            accepted.append(conn.recv(65536))
            conn.close()  # read the request, then hang up without answering

        thread = threading.Thread(target=serve_one, daemon=True)
        thread.start()
        session = requests.Session()
        session.trust_env = False  # connect to the loopback server, not an env proxy
        forms.configure_session_adapters(session)
        try:
            with self.assertRaises(forms.OutcomeUnknownError):
                forms.submit_once(session, f'http://127.0.0.1:{server.getsockname()[1]}/x',
                                  data=[('a', '1')], timeout=5)
            thread.join(5)
            self.assertEqual(len(accepted), 1)
            self.assertTrue(accepted[0].startswith(b'POST /x'))
            # A resent request would be waiting in the backlog.
            server.settimeout(0.2)
            with self.assertRaises(socket.timeout):
                server.accept()
        finally:
            server.close()


class SessionAdaptersTest(unittest.TestCase):

    def test_mounts_transport_retries(self):
        session = requests.Session()
        forms.configure_session_adapters(session)
        for prefix in ('https://', 'http://'):
            retries = session.adapters[prefix].max_retries
            self.assertEqual(retries.total, 3)
            self.assertEqual(retries.connect, 3)
            self.assertEqual(retries.read, 3)
            self.assertEqual(retries.other, 0)
            self.assertEqual(retries.status, 0)

    def test_read_errors_are_not_retried_for_post(self):
        session = requests.Session()
        forms.configure_session_adapters(session)
        retries = session.adapters['https://'].max_retries
        self.assertFalse(retries._is_method_retryable('POST'))
        self.assertTrue(retries._is_method_retryable('GET'))

    def test_post_failing_with_ssl_error_is_sent_once(self):
        # urllib3 counts an SSL error as neither a connect nor a read error, so
        # only other=0 stops it from resending the POST.
        attempts = []

        def fail_with_ssl_error(pool, conn, method, url, **kwargs):
            attempts.append(method)
            raise ssl.SSLError('TLS failure after the request was written')

        session = requests.Session()
        session.trust_env = False
        forms.configure_session_adapters(session)
        with mock.patch.object(HTTPConnectionPool, '_make_request', fail_with_ssl_error), \
                mock.patch.object(time, 'sleep'):
            with self.assertRaises(forms.OutcomeUnknownError) as ctx:
                forms.submit_once(session, 'https://github.example.com/settings/apps',
                                  data=[('a', '1')], timeout=5)
        self.assertIsInstance(ctx.exception.__cause__, SSLError)
        self.assertEqual(attempts, ['POST'])


class ErrorsTest(unittest.TestCase):

    def test_attributes_and_hierarchy(self):
        err = forms.FormRejectedError('rejected', summary='HTTP 200', errors=['taken'])
        self.assertEqual((str(err), err.summary, err.errors), ('rejected', 'HTTP 200', ['taken']))
        self.assertIsInstance(err, ValueError)
        self.assertIsInstance(forms.PageShapeError('x'), ValueError)
        self.assertNotIsInstance(forms.OutcomeUnknownError('x'), ValueError)
        self.assertEqual(forms.OutcomeUnknownError('x').errors, [])

    def test_body_excerpt_is_kept_out_of_the_message(self):
        err = forms.PageShapeError('changed', summary='HTTP 200', body_excerpt='<p>page</p>')
        self.assertEqual(err.body_excerpt, '<p>page</p>')
        self.assertNotIn('page', str(err))
        self.assertNotIn('page', repr(err))
        self.assertEqual(forms.FormRejectedError('x').body_excerpt, '')


class RedactedBodyExcerptTest(unittest.TestCase):

    def test_redacts_every_occurrence_of_token_values(self):
        page = """<html><head><meta name="csrf-token" content="META_TOK"></head><body>
          <form><input value="FORM_TOK" type="hidden" name='authenticity_token'>
          <input type="hidden" name="timestamp_secret" value="TS_SECRET">
          <input type="hidden" data-csrf="true" class="js-data-url-csrf" value="DATA_CSRF">
          <input name="keep" value="visible"></form>
          <template><form><input name=authenticity_token value=TPL_TOK></form></template>
          <p>echo FORM_TOK and META_TOK</p></body></html>"""
        excerpt = forms.redacted_body_excerpt(page)
        for secret in ('META_TOK', 'FORM_TOK', 'TS_SECRET', 'DATA_CSRF', 'TPL_TOK'):
            self.assertNotIn(secret, excerpt)
        self.assertIn('value="visible"', excerpt)
        self.assertIn('echo [REDACTED] and', excerpt)

    def test_redacts_entity_escaped_values(self):
        page = '<input name="authenticity_token" value="a&amp;b&quot;c"><p>a&amp;b&quot;c</p>'
        self.assertNotIn('a&amp;b', forms.redacted_body_excerpt(page))

    def test_redacts_csrf_tokens_in_inline_json_and_urls(self):
        page = ('<body><script type="application/json">{"authenticity_token":"JSON_TOK",'
                '"csrf_token": "JSON2"}</script>'
                '<div data-props="{&quot;authenticity_token&quot;:&quot;ATTR_TOK&quot;}"></div>'
                '<a href="/x?authenticity_token=URL_TOK&amp;y=1">x</a>'
                '<script>var c = {csrfToken: \'JS_TOK\'}; var s = "{\\"authenticity_token\\":\\"ESC_TOK\\"}";</script>'
                '</body>')
        excerpt = forms.redacted_body_excerpt(page)
        for secret in ('JSON_TOK', 'JSON2', 'ATTR_TOK', 'URL_TOK', 'JS_TOK', 'ESC_TOK'):
            self.assertNotIn(secret, excerpt)
        self.assertIn('&amp;y=1', excerpt)

    def test_drops_script_contents(self):
        page = ('<body><main>m</main><script type="application/json" data-target="react-app.'
                'embeddedData">{"csrf_tokens":{"/o/r/settings":{"post":"REACT_TOK"}}}</script>'
                '<script>var unclosed = "UNCLOSED_TOK"')
        excerpt = forms.redacted_body_excerpt(page)
        self.assertNotIn('REACT_TOK', excerpt)
        self.assertNotIn('UNCLOSED_TOK', excerpt)
        self.assertIn('data-target="react-app.embeddedData">[REDACTED]</script>', excerpt)

    def test_redacts_token_named_attributes_and_parameters(self):
        page = ('<body><auto-check src="/x" csrf="ATTR_CSRF_TOK"></auto-check>'
                '<input type="hidden" class="js-confirm-csrf-token" value="CLASS_TOK">'
                '<input type="hidden" data-csrf="true" value="DATA_TOK"><p>true story</p>'
                '<a href="https://raw.example/f?token=RAW_TOK">raw</a>'
                '<a href="/callback?state=s&amp;code=CODE_TOK">cb</a>'
                '<p>Echo ATTR_CSRF_TOK</p>'
                '<form action="/organizations/MyOrg/settings/apps"></form></body>')
        excerpt = forms.redacted_body_excerpt(page)
        for secret in ('ATTR_CSRF_TOK', 'CLASS_TOK', 'DATA_TOK', 'RAW_TOK', 'CODE_TOK'):
            self.assertNotIn(secret, excerpt)
        # Short flags are not treated as secrets elsewhere on the page.
        self.assertIn('<p>true story</p>', excerpt)
        self.assertIn('action="/organizations/MyOrg/settings/apps"', excerpt)

    def test_redacts_character_reference_encodings(self):
        page = ('<input name="authenticity_token" value="ab&#43;cd&#x2F;ef&#X3d;">'
                '<p>ab+cd&#47;ef=</p><p>ab&#0043;cd/ef&#x3D;</p>')
        excerpt = forms.redacted_body_excerpt(page)
        self.assertNotIn('ab', excerpt)
        self.assertNotIn('cd', excerpt)

    def test_long_word_runs_are_scanned_in_linear_time(self):
        # Without the word-boundary anchor, key matching is quadratic here.
        page = '<body>' + 'a' * 200000 + ' ' + 'b_' * 100000 + ' token=T0K</body>'
        self.assertTrue(forms.redacted_body_excerpt(page, limit=10**6).endswith(
            'token=[REDACTED]</body>'))

    def test_redacts_rails_signed_values(self):
        # Modeled on a GitHub Enterprise Server page, with made-up IDs, hosts
        # and digests: Rails signs the live-update socket session and the
        # notification channel as "<base64 JSON>--<hex digest>", under names
        # no token rule knows.
        session = base64.b64encode(
            b'{"v":"V3","u":4242,"s":7777777,"c":3141592653,"t":1700000000}').decode()
        channel = base64.b64encode(b'{"c":"notification-changed:4242","t":1700000000}').decode()
        session_digest, channel_digest = 'ab12' * 16, 'cd34' * 16
        cookie = 'U0VDUkVUREFUQQ==--aXZpdml2aXZpdml2--' + 'QUFBQUFBQUFBQUFBQUFBQQ' + '=='
        sha = '0123456789abcdef0123456789abcdef01234567'
        page = (
            '<body><main>'
            '<link rel="shared-web-socket" href="wss://alive.example.com/_sockets/u/4242/ws?'
            f'session={session}--{session_digest}&amp;shared=true&amp;p=1234_1700000000">'
            '<link rel="shared-web-socket-src" href="/assets-cdn/worker/socket-worker-0a1b2c3d.js">'
            f'<a class="AppHeader-button js-socket-channel" data-channel="{channel}--{channel_digest}"'
            ' href="/notifications">Notifications</a>'
            f'<a href="/login?return_to=%2F_alive%3Fsession%3D{session.replace("=", "%3D")}'
            f'--{session_digest}%26shared%3Dtrue">encoded</a>'
            f'<div data-cookie="{cookie}"></div>'
            '<button class="Button--secondary Button--small">Cancel</button>'
            f'<a href="/o/r/commit/{sha}">commit</a>'
            '<link rel="stylesheet" href="/assets/light-0123456789ab.css"'
            ' integrity="sha512-AbCd/EfGh+IjKl==">'
            '</main></body>')
        excerpt = forms.redacted_body_excerpt(page, limit=10**6)
        for secret in (session, channel, session[:20], channel[:20], session_digest,
                       channel_digest, 'eyJ', 'U0VDUkVUREFUQQ', 'QUFBQUFB'):
            self.assertNotIn(secret, excerpt)
        self.assertIn('/ws?session=[REDACTED]&amp;shared=true&amp;p=1234_1700000000"', excerpt)
        self.assertIn('data-channel="[REDACTED]"', excerpt)
        self.assertIn('%3Fsession%3D[REDACTED]%26shared%3Dtrue"', excerpt)
        self.assertIn('data-cookie="[REDACTED]"', excerpt)
        # Asset hashes, commit SHAs and BEM class names are not signed values.
        for kept in ('socket-worker-0a1b2c3d.js', 'class="Button--secondary Button--small"',
                     f'/commit/{sha}"', 'light-0123456789ab.css', 'sha512-AbCd/EfGh+IjKl=='):
            self.assertIn(kept, excerpt)

    def test_redacts_token_named_rails_signed_values_whole(self):
        excerpt = forms.redacted_body_excerpt(
            '<a href="/x?csrf_token=eyJhIjoxfQ==--' + 'f' * 40 + '&amp;y=1">x</a>')
        self.assertIn('csrf_token=[REDACTED]&amp;y=1', excerpt)
        self.assertNotIn('ff', excerpt)

    def test_redacts_signed_values_on_a_github_enterprise_server_page(self):
        # The elements that carried signed values on a captured GitHub
        # Enterprise Server page, with some attributes dropped and the host,
        # user ID, timestamps, hashes and signing key made up. There is no
        # <body> or <main> here, so the excerpt is the whole page.
        def signed(data: bytes) -> str:
            encoded = base64.b64encode(data)
            return f'{encoded.decode()}--{hmac.new(b"key", encoded, "sha256").hexdigest()}'

        session = signed(b'{"v":"V3","u":4242,"s":123,"c":1234567890,"t":1700000000}')
        channel = signed(b'{"c":"notification-changed:4242","t":1700000000}')
        session_id = hashlib.sha256(b'session').hexdigest()
        pjax_version = hashlib.sha256(b'pjax').hexdigest()
        page = (
            f'<meta http-equiv="x-pjax-version" content="{pjax_version}" data-turbo-track="reload">\n'
            '<link rel="shared-web-socket" href="wss://github.example.com/_sockets/u/4242/ws?'
            f'session={session}" data-refresh-url="/_alive" data-session-id="{session_id}">\n'
            '<link rel="shared-web-socket-src" href="/assets/socket-worker-0123456789abcdef.js">\n'
            f'<notification-indicator data-channel="{channel}" data-indicator-mode="none" '
            'data-tooltip-global="You have unread notifications" '
            'data-fetch-indicator-src="/notifications/indicator" data-fetch-indicator-enabled="true" '
            'data-view-component="true" class="js-socket-channel">\n'
            '<notification-shelf-watcher data-base-url="https://github.example.com/notifications/beta'
            f'/shelf" data-channel="{channel}" data-view-component="true" class="js-socket-channel">'
            '</notification-shelf-watcher>\n'
            # The same channel in percent-encoded JSON, as in a query string.
            '<a href="/x?props=%7B%22channel%22%3A%22'
            f'{channel.replace("=", "%3D")}%22%7D">x</a>\n')
        excerpt = forms.redacted_body_excerpt(page, limit=10**6)
        for secret in ('eyJ', session[-64:], channel[-64:], session_id):
            self.assertNotIn(secret, excerpt)
        self.assertIn('/ws?session=[REDACTED]" data-refresh-url="/_alive" '
                      'data-session-id="[REDACTED]">', excerpt)
        self.assertEqual(excerpt.count('data-channel="[REDACTED]"'), 2)
        self.assertIn('%3A%22[REDACTED]%22%7D', excerpt)
        # Release and asset hashes are not secrets.
        self.assertIn(f'content="{pjax_version}"', excerpt)
        self.assertIn('socket-worker-0123456789abcdef.js', excerpt)

    def test_base64_runs_are_scanned_in_linear_time(self):
        page = ('<body>' + 'A' * 300000 + '-x ' + 'aA--' * 50000 + ' '
                + 'a%2B' * 50000 + '==--' + 'f' * 39 + ' ' + '%22aA' * 50000 + '</body>')
        start = time.monotonic()
        excerpt = forms.redacted_body_excerpt(page, limit=10**7)
        self.assertLess(time.monotonic() - start, 5)
        self.assertNotIn(forms.REDACTED, excerpt)

    def test_redacts_before_truncating(self):
        token = 'T' * 100
        page = f'<input name="authenticity_token" value="{token}">'
        for limit in range(0, len(page) + 20):
            excerpt = forms.redacted_body_excerpt(page, limit=limit)
            self.assertNotIn('TT', excerpt)
            self.assertLessEqual(len(excerpt.encode('utf-8')), limit)

    def test_starts_at_main_then_body(self):
        head = '<head>' + 'h' * 5000 + '</head>'
        self.assertTrue(forms.redacted_body_excerpt(
            f'<html>{head}<body><main-nav>n</main-nav><main class="x">m</main></body>'
        ).startswith('<main class="x">'))
        self.assertTrue(forms.redacted_body_excerpt(
            f'<html>{head}<body class="logged-in"><p>b</p></body>').startswith('<body class='))
        self.assertEqual(forms.redacted_body_excerpt('<p>fragment</p>'), '<p>fragment</p>')

    def test_truncates_to_4_kb_without_splitting_characters(self):
        excerpt = forms.redacted_body_excerpt('<body>' + '\u00e9' * 5000)
        self.assertLessEqual(len(excerpt.encode('utf-8')), forms.MAX_BODY_EXCERPT_BYTES)
        self.assertGreater(len(excerpt.encode('utf-8')), forms.MAX_BODY_EXCERPT_BYTES - 2)
        self.assertEqual(forms.redacted_body_excerpt(''), '')

    def test_fails_closed_when_the_page_cannot_be_scanned(self):
        page = '<input name="authenticity_token" value="SECRET">'
        with mock.patch.object(forms.html5lib, 'parse', side_effect=RuntimeError('boom')):
            self.assertEqual(forms.redacted_body_excerpt(page), '')


if __name__ == '__main__':
    unittest.main()
