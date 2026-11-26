# Copyright 2024 The Authors (see AUTHORS file)
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

"""Unit tests for api.py."""

import html
import os
from unittest import TestCase, mock
from urllib.parse import parse_qsl, urljoin

import requests
import responses
from responses import matchers

from github_nonpublic_api import api
from github_nonpublic_api import forms

GITHUB_FORM_HTML = os.path.join(os.path.dirname(__file__), 'github_form.html')
NEW_ORG_FORM_HTML = os.path.join(
    os.path.dirname(__file__), 'new_org_form.html')
ADD_APP_FORM_HTML = os.path.join(
    os.path.dirname(__file__), 'add_app_form.html')
SUSPEND_APP_FORM = os.path.join(
    os.path.dirname(__file__), 'suspend_app_form.html')
REQUEST_REPORT_FORM_HTML = os.path.join(
    os.path.dirname(__file__), 'request_report_form.html')
DOWNLOAD_USAGE_REPORT_CSV = os.path.join(
    os.path.dirname(__file__), 'usage_report.csv')
UPDATE_APP_PERMISSIONS_FORM_HTML = os.path.join(
    os.path.dirname(__file__), 'update_app_permissions.html')
SECURITY_ANALYSIS_FORM_HTML = os.path.join(
    os.path.dirname(__file__), 'security_analysis_form.html')
APP_MANIFEST_CONFIRMATION_HTML = os.path.join(
    os.path.dirname(__file__), 'app_manifest_confirmation.html')
APP_MANIFEST_CONTINUE_SETUP_HTML = os.path.join(
    os.path.dirname(__file__), 'app_manifest_continue_setup.html')


class TestApi(TestCase):
    def _seed_session_with_file(self, filename):
        self.session = mock.MagicMock()
        with open(filename) as fp:
            self.session.get.return_value.text = fp.read()

    """Unit tests for api.py."""

    def test_get_and_submit_form(self):
        self._seed_session_with_file(GITHUB_FORM_HTML)

        def _data_callback(data):
            data['add'] = 'yes'

        api._get_and_submit_form(
            session=self.session, url='http://github.com', data_callback=_data_callback)

        self.session.post.assert_called_once_with(
            'http://github.com/foo', data=dict(add='yes', key='value'))

    def test_get_and_submit_form_by_id(self):
        self._seed_session_with_file(GITHUB_FORM_HTML)

        api._get_and_submit_form(
            session=self.session, url='http://github.com',
            form_matcher=lambda form: form.attrib.get('id') == 'form2')

        self.session.post.assert_called_once_with(
            'http://github.com/form2', data=dict(key='value2'))

    def test_get_and_submit_form_by_id_error(self):
        self._seed_session_with_file(GITHUB_FORM_HTML)

        with self.assertRaises(ValueError):
            api._get_and_submit_form(
                session=self.session, url='http://github.com',
                form_matcher=lambda form: False)

    def test_create_login_session_captcha_challenge(self):
        with mock.patch.object(api, '_get_and_submit_form') as mock_submit:
            mock_res = mock.MagicMock()
            mock_res.url = 'https://github.com/sessions/verified-device'
            mock_submit.return_value = mock_res

            with self.assertRaisesRegex(RuntimeError, 'Login challenged by GitHub'):
                api.create_login_session(
                    username='user', password='password', tfa_callback=lambda: '123'
                )

    def test_create_login_session_success(self):
        with mock.patch.object(api, '_get_and_submit_form') as mock_submit:
            # The password lands on the two-factor page; the code on the dashboard.
            mock_submit.side_effect = [
                mock.MagicMock(url='https://github.com/sessions/two-factor'),
                mock.MagicMock(url='https://github.com/'),
            ]
            
            session = api.create_login_session(
                username='user', password='password', tfa_callback=lambda: '123'
            )
            self.assertIsNotNone(session)
            self.assertEqual(mock_submit.call_count, 2)

    def test_create_business_org(self):
        self._seed_session_with_file(NEW_ORG_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.create_organization(org_name='test', contact_email='nobody@google.com',
                               org_usage=api.OrganizationUsage.BUSINESS,
                               business_name='A Fake Business')
        self.session.post.assert_called_once_with(
            'https://github.com/account/organizations/new_org', data={
                'authenticity_token': 'value',
                'agreed_to_terms': 'yes',
                'terms_of_service_type': 'corporate',
                'organization[billing_email]': 'nobody@google.com',
                'organization[profile_name]': 'test',
                'organization[login]': 'test',
                'organization[company_name]': 'A Fake Business',
            })

    def test_create_personal_org(self):
        self._seed_session_with_file(NEW_ORG_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.create_organization(org_name='test', contact_email='nobody@google.com',
                               org_usage=api.OrganizationUsage.PERSONAL)
        self.session.post.assert_called_once_with(
            'https://github.com/account/organizations/new_org', data={
                'authenticity_token': 'value',
                'agreed_to_terms': 'yes',
                'terms_of_service_type': 'standard',
                'organization[billing_email]': 'nobody@google.com',
                'organization[profile_name]': 'test',
                'organization[login]': 'test',
            })

    def test_request_usage_report(self):
        self._seed_session_with_file(REQUEST_REPORT_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.request_usage(enterprise_name='test-enterprise', days=7)
        self.session.post.assert_called_once_with(
            'https://github.com/enterprises/test-enterprise/settings/metered_exports', data={
                'authenticity_token': 'value',
                'days': 7,
            })

    def test_install_app_on_org(self):
        self._seed_session_with_file(ADD_APP_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.install_application_in_organization(app_name='test-app', org_id=42)
        self.session.post.assert_called_once_with(
            'https://github.com/apps/test-app/installations', data={
                'authenticity_token': 'value',
                'install_target': 'all',
            })

    def test_suspend_app_toggle(self):
        self._seed_session_with_file(SUSPEND_APP_FORM)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.toggle_app_suspended(org_name='test-org', app_install_id=42)
        self.session.post.assert_called_once_with(
            'https://github.com/long/url/suspended', data={
                'authenticity_token': 'value',
            })

    def test_download_usage_report(self):
        self._seed_session_with_file(DOWNLOAD_USAGE_REPORT_CSV)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.download_usage_report(enterprise_name='test-enterprise', report_id=1)
        self.session.get.assert_called_once_with('https://github.com/enterprises/test-enterprise/settings/metered_exports/1')

    def test_update_app_permissions(self):
        self._seed_session_with_file(UPDATE_APP_PERMISSIONS_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.approve_updated_app_permissions(org_name='test-org', app_install_id='42')
        self.session.post.assert_called_once_with(
            'https://github.com/organizations/test-org/settings/installations/42/permissions/update',
            data={
                '_method': 'put',
                'authenticity_token': 'value',
                'version_id': '112233',
                'integration_fingerprint': 'value',
            },
        )
    
    def test_update_security_analysis_settings_code_scanning(self):
        self._seed_session_with_file(SECURITY_ANALYSIS_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.update_security_analysis_settings(org_name='test-org', code_scanning_autofix=False)
        self.session.post.assert_called_once_with(
            'https://github.com/organizations/test-org/settings/security_analysis/update?owner=test-org',
            data={
                '_method': 'put',
                'authenticity_token': 'sekr3t_2',
                'code_scanning_autofix': 'disabled',
            },
        )

    def test_update_security_analysis_settings_code_scanning_3p(self):
        self._seed_session_with_file(SECURITY_ANALYSIS_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session)
        gh.update_security_analysis_settings(org_name='test-org', code_scanning_autofix_third_party_tools=True)
        self.session.post.assert_called_once_with(
            'https://github.com/organizations/test-org/settings/security_analysis/update?owner=test-org',
            data={
                '_method': 'put',
                'authenticity_token': 'sekr3t_3',
                'code_scanning_autofix_third_party_tools': 'enabled',
            },
        )

    def test_base_url_applies_to_existing_methods(self):
        self._seed_session_with_file(NEW_ORG_FORM_HTML)
        gh = api.Api(username='user', password='pass', session=self.session,
                     base_url='https://github.example.com/')
        gh.create_organization(org_name='test', contact_email='nobody@google.com',
                               org_usage=api.OrganizationUsage.PERSONAL)
        self.session.get.assert_called_once_with(
            'https://github.example.com/account/organizations/new?plan=free')
        self.assertEqual(self.session.post.call_args.args[0],
                         'https://github.example.com/account/organizations/new_org')

    def test_download_dormant_users_report_follows_the_link_for_base_url_and_enterprise(self):
        export_path = '/settings/dormant-users/exports/0123abcd-0123-4567-89ab-0123456789ab'
        export = 'https://github.example.com/enterprises/test-enterprise' + export_path
        self.session = mock.MagicMock()
        self.session.get.return_value.content = (
            f'<a href="https://github.com/enterprises/test-enterprise{export_path}">github.com</a>'
            f'<a href="https://github.example.com/enterprises/other{export_path}">other</a>'
            f'<a href="{export}">report</a>').encode()
        gh = api.Api(username='user', password='pass', session=self.session,
                     base_url='https://github.example.com/')
        gh.download_dormant_users_report(enterprise_name='test-enterprise')
        self.assertEqual(self.session.get.call_args_list, [
            mock.call('https://github.example.com/enterprises/test-enterprise/settings/compliance'),
            mock.call(export),
        ])


class TestCreateLoginSession(TestCase):
    """Login with an optional TOTP callback on github.com or GHES."""

    BASE = 'https://github.example.com'

    def _login(self, landing_url, tfa_callback=None, after_code_url=None, **kwargs):
        """Signs in with `_get_and_submit_form` mocked: the password form
        lands on `landing_url`, and the two-factor form, if submitted, on
        `after_code_url` (the dashboard by default)."""
        after_code = mock.Mock(url=after_code_url or self.BASE + '/')
        with mock.patch.object(api, '_get_and_submit_form') as mock_submit:
            mock_submit.side_effect = [mock.Mock(url=landing_url), after_code]
            session = api.create_login_session(
                username='user', password='password', tfa_callback=tfa_callback,
                base_url=self.BASE + '/', **kwargs)
            return session, mock_submit

    def test_without_tfa_callback_signs_in_with_one_form(self):
        session, mock_submit = self._login(self.BASE + '/')
        self.assertIsNotNone(session)
        mock_submit.assert_called_once()
        self.assertEqual(mock_submit.call_args.kwargs['url'], self.BASE + '/login')

    def test_without_tfa_callback_challenge_pages_raise(self):
        for path in ('/sessions/verified-device', '/session', '/login?return_to=%2F',
                     '/login/device'):
            with self.assertRaisesRegex(
                    api.LoginChallengeError,
                    r'Sign-in did not complete \(wrong credentials, captcha or device '
                    r'verification\)'):
                self._login(self.BASE + path)

    def test_without_tfa_callback_two_factor_prompt_raises(self):
        with self.assertRaisesRegex(api.LoginChallengeError, 'no tfa_callback'):
            self._login(self.BASE + '/sessions/two-factor/app')

    def test_challenge_is_still_a_runtime_error(self):
        with self.assertRaises(RuntimeError):
            self._login(self.BASE + '/sessions/verified-device')

    def test_with_tfa_callback_submits_code_to_base_url(self):
        _, mock_submit = self._login(self.BASE + '/sessions/two-factor', tfa_callback=lambda: '123')
        self.assertEqual(mock_submit.call_count, 2)
        self.assertEqual(mock_submit.call_args.kwargs['url'], self.BASE + '/sessions/two-factor')
        data = {}
        mock_submit.call_args.kwargs['data_callback'](data)
        self.assertEqual(data, {'otp': '123'})

    def test_with_tfa_callback_rejected_code_raises(self):
        for after_code_path in ('/sessions/two-factor', '/sessions/two-factor/app', '/login'):
            with self.subTest(after_code_path=after_code_path):
                with self.assertRaises(api.LoginChallengeError) as ctx:
                    self._login(self.BASE + '/sessions/two-factor', tfa_callback=lambda: '123',
                                after_code_url=self.BASE + after_code_path)
                self.assertEqual(str(ctx.exception), 'Two-factor code was not accepted. Stuck at: '
                                 + self.BASE + after_code_path)

    def test_with_tfa_callback_off_host_page_after_code_is_signed_in(self):
        session, _ = self._login(self.BASE + '/sessions/two-factor', tfa_callback=lambda: '123',
                                 after_code_url='https://idp.example.com/login')
        self.assertIsNotNone(session)

    def test_with_tfa_callback_challenge_on_base_host_raises(self):
        with self.assertRaisesRegex(api.LoginChallengeError,
                                    'expected a two-factor prompt.*verified-device'):
            self._login(self.BASE + '/sessions/verified-device', tfa_callback=lambda: '123')

    def test_mounts_transport_retries_on_given_session(self):
        session, _ = self._login(self.BASE + '/', session=requests.Session())
        self.assertEqual(session.adapters['https://'].max_retries.total, 3)

    def test_with_tfa_callback_off_host_landing_still_submits_code(self):
        # Upstream behavior: only a non-two-factor page on the GitHub host is a
        # challenge; anywhere else, the code is still submitted.
        _, mock_submit = self._login('https://idp.example.com/sso', tfa_callback=lambda: '123')
        self.assertEqual(mock_submit.call_count, 2)
        self.assertEqual(mock_submit.call_args.kwargs['url'], self.BASE + '/sessions/two-factor')

    def test_base_url_must_be_absolute(self):
        for bad in ('github.example.com', 'ftp://github.example.com', 'https://', '',
                    'https://github.example.com/prefix', 'https://github.example.com/prefix/',
                    'https://github.example.com/;x', 'https://github.example.com?x=1',
                    'https://github.example.com/?', 'https://github.example.com#top',
                    'https://github.example.com/#'):
            with self.subTest(base_url=bad), mock.patch.object(api, '_get_and_submit_form') as m:
                with self.assertRaisesRegex(ValueError, 'absolute http'):
                    api.create_login_session('u', 'p', base_url=bad)
                m.assert_not_called()

    def test_base_url_may_end_with_slashes(self):
        for base_url in ('https://github.example.com/', 'https://github.example.com//'):
            with self.subTest(base_url=base_url):
                gh = api.Api(username='u', password='p', session=requests.Session(),
                             base_url=base_url)
                self.assertEqual(gh._base_url, 'https://github.example.com')

    def test_api_validates_base_url_with_a_given_session(self):
        for bad in ('github.example.com', 'https://github.example.com/prefix'):
            with self.subTest(base_url=bad), self.assertRaisesRegex(ValueError, 'absolute http'):
                api.Api(username='u', password='p', session=requests.Session(), base_url=bad)


MANIFEST = '{"name": "Test App", "redirect_url": "http://127.0.0.1/callback"}'
CALLBACK = 'http://127.0.0.1/callback?code=c0de&state=s1'
# The callback URL in the continue-setup page fixture.
CONTINUE_SETUP_CALLBACK = 'http://127.0.0.1/callback?code=FIXTURE_CODE&state=test_state'
# Text that must never appear in an exception message: messages carry visible
# errors and a response summary, never the page body.
BODY_MARKER = 'BODY-MARKER-NOT-IN-MESSAGES'


class TestCreateAppFromManifest(TestCase):
    BASE = 'https://github.example.com'
    ORG_NEW = BASE + '/organizations/test-org/settings/apps/new'
    # GitHub answers a manifest posted to a new-app page with a redirect to
    # this page, which renders the confirmation form.
    MANIFEST_PAGE = BASE + '/settings/apps/manifest'
    # Where the confirmation form posts, for organization- and user-owned
    # apps alike.
    CONFIRM = BASE + '/settings/apps'
    # The continue-setup page's meta refresh and the href of its a#redirect
    # link, both to the callback.
    CONTINUE_SETUP_META_REFRESH = (
        '<meta http-equiv="refresh" content="0;url=http://127.0.0.1/callback?code=FIXTURE_CODE'
        '&amp;state=test_state" data-url="http://127.0.0.1/callback?code=FIXTURE_CODE'
        '&amp;state=test_state">')
    CONTINUE_SETUP_LINK_HREF = 'href="http://127.0.0.1/callback?code=FIXTURE_CODE&amp;state=test_state"'

    def setUp(self):
        self.gh = api.Api(username='u', password='p', session=requests.Session(),
                          base_url=self.BASE + '/')
        with open(APP_MANIFEST_CONFIRMATION_HTML) as fp:
            self.page = fp.read()
        # What GitHub offers when the signed-in user, test-user, can't create
        # test-org's apps, or when the manifest was posted for test-user.
        self.user_page = (
            self.page.replace('value="test-org"', 'value="test-user"')
            .replace('for test-org', 'for test-user'))
        # The continue-setup page that answers the confirmation.
        with open(APP_MANIFEST_CONTINUE_SETUP_HTML) as fp:
            self.continue_setup_page = fp.read()

    def _add_preview(self, body=None, url=None, redirect=True):
        """Answers the manifest POST to `url` (the organization's new-app
        page by default) with `body`, the fixture by default: rendered at
        the redirect target as GitHub does, or directly if not
        `redirect`."""
        url = url or self.ORG_NEW
        body = self.page if body is None else body
        match = [matchers.query_param_matcher({'state': 's1'}),
                 matchers.urlencoded_params_matcher({'manifest': MANIFEST})]
        if not redirect:
            return responses.post(url, body=body, match=match)
        manifest_page = urljoin(url, '/settings/apps/manifest')
        responses.post(url, status=302, headers={'Location': manifest_page}, match=match)
        return responses.get(manifest_page, body=body)

    def _add_confirm(self, **kwargs):
        return responses.post(self.CONFIRM, **kwargs)

    def _create(self, **kwargs):
        return self.gh.create_app_from_manifest(
            manifest_payload=MANIFEST, state='s1', org_name='test-org', **kwargs)

    def _assert_body_not_in(self, error):
        self.assertNotIn(BODY_MARKER, str(error))
        self.assertNotIn(BODY_MARKER, error.summary)

    @responses.activate
    def test_submits_manifest_then_confirmation_and_returns_callback(self):
        self._add_preview()
        self._add_confirm(body=self.continue_setup_page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)
        # The manifest POST's redirect to the confirmation page is followed;
        # the callback itself is never requested.
        self.assertEqual([(c.request.method, c.request.url.split('?')[0]) for c in responses.calls], [
            ('POST', self.ORG_NEW),
            ('GET', self.MANIFEST_PAGE),
            ('POST', self.CONFIRM),
        ])
        self.assertEqual(parse_qsl(responses.calls[2].request.body, keep_blank_values=True), [
            ('authenticity_token', 'FIXTURE_TOKEN'),
            ('integration_manifest[name]', 'test-app'),
            ('integration_manifest[data]',
             '{"name": "test-app", "url": "https://example.com", "public": false, '
             '"redirect_url": "http://127.0.0.1/callback"}'),
            ('integration_manifest[owner_login]', 'test-org'),
            ('integration_manifest[manifest_token]', 'FIXTURE_MANIFEST_TOKEN'),
            ('integration_manifest[state]', 'test_state'),
            ('commit', 'Create GitHub App for test-org'),
        ])

    @responses.activate
    def test_user_owned_app_with_default_base_url(self):
        gh = api.Api(username='u', password='p', session=requests.Session())
        self._add_preview(body=self.user_page, url='https://github.com/settings/apps/new')
        responses.post('https://github.com/settings/apps', body=self.continue_setup_page)
        self.assertEqual(gh.create_app_from_manifest(MANIFEST, 's1'), CONTINUE_SETUP_CALLBACK)
        self.assertIn(('integration_manifest[owner_login]', 'test-user'),
                      parse_qsl(responses.calls[2].request.body))

    @responses.activate
    def test_user_owned_form_does_not_confirm_an_org_owned_app(self):
        # GitHub answered a manifest posted to an organization the user
        # didn't own with a redirect to the user's confirmation page, which
        # would have made the app user-owned.
        self._add_preview(body=self.user_page)
        with self.assertRaisesRegex(
                forms.PageShapeError,
                r'^GitHub offered to create the app for \[test-user\], not \[test-org\] \(is '
                r'\[test-user\] an owner of \[test-org\]\?\): HTTP 200 at .*/settings/apps/manifest '
                r"after redirects \[302 .*\]; page title 'Create GitHub App'\.$") as ctx:
            self._create()
        excerpt = ctx.exception.body_excerpt
        self.assertIn('value="test-user" type="hidden" name="integration_manifest[owner_login]"',
                      excerpt)
        self.assertNotIn('FIXTURE_MANIFEST_TOKEN', excerpt)
        self.assertNotIn('FIXTURE_TOKEN', excerpt)
        self.assertEqual(len(responses.calls), 2)
        self.assertEqual(responses.calls[1].request.method, 'GET')

    @responses.activate
    def test_org_mismatch_without_user_login_meta_names_no_user(self):
        page = self.user_page.replace('<meta name="user-login" content="test-user">', '')
        self._add_preview(body=page)
        with self.assertRaisesRegex(forms.PageShapeError,
                                    r'not \[test-org\] \(is the signed-in user an owner of \[test-org\]\?\)'):
            self._create()
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_org_owned_form_does_not_confirm_a_user_owned_app(self):
        responses.post(self.BASE + '/settings/apps/new', body=self.page)
        with self.assertRaisesRegex(
                forms.PageShapeError,
                r'offered to create the app for \[test-org\], not for the signed-in user \[test-user\]'):
            self.gh.create_app_from_manifest(MANIFEST, 's1')
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_user_owned_app_requires_the_signed_in_login(self):
        page = self.user_page.replace('<meta name="user-login" content="test-user">', '')
        responses.post(self.BASE + '/settings/apps/new', body=page)
        with self.assertRaisesRegex(forms.PageShapeError, r"doesn't say which user is signed in"):
            self.gh.create_app_from_manifest(MANIFEST, 's1')
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_form_is_found_by_its_owner_field_wherever_it_posts(self):
        page = self.page.replace('action="/settings/apps"',
                                 'action="/organizations/test-org/settings/apps"')
        self._add_preview(body=page)
        responses.post(self.BASE + '/organizations/test-org/settings/apps',
                       body=self.continue_setup_page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)

    @responses.activate
    def test_confirmation_is_posted_to_the_form_action_with_its_query(self):
        page = self.page.replace('action="/settings/apps"',
                                 'action="/settings/apps?state=test-state"')
        self._add_preview(body=page)
        self._add_confirm(match=[matchers.query_param_matcher({'state': 'test-state'})],
                          body=self.continue_setup_page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)

    @responses.activate
    def test_owner_field_that_a_browser_would_not_submit_raises(self):
        page = self.page.replace('name="integration_manifest[owner_login]"',
                                 'name="integration_manifest[owner_login]" disabled')
        self._add_preview(body=page)
        with self.assertRaisesRegex(forms.PageShapeError, r'submits 0 values for integration_manifest'):
            self._create()
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    @mock.patch('time.sleep', return_value=None)
    def test_manifest_post_is_retried_because_it_changes_nothing(self, mock_sleep):
        responses.post(self.ORG_NEW, status=502)
        self._add_preview()
        self._add_confirm(body=self.continue_setup_page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)
        self.assertEqual(len(responses.calls), 4)
        mock_sleep.assert_called_once()

    @responses.activate
    def test_sign_in_page_raises_login_challenge(self):
        responses.post(self.ORG_NEW, status=302,
                       headers={'Location': self.BASE + '/login?return_to=x'})
        responses.get(self.BASE + '/login', body='<title>Sign in</title>')
        with self.assertRaisesRegex(api.LoginChallengeError,
                                    "sign-in or sudo confirmation page.*'Sign in'"):
            self._create()
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_rejected_manifest_raises_visible_errors(self):
        self._add_preview(body=f'<p>{BODY_MARKER}</p>'
                               '<div class="flash flash-error">Invalid manifest</div>',
                          redirect=False)
        with self.assertRaises(forms.FormRejectedError) as ctx:
            self._create()
        self.assertEqual(ctx.exception.errors, ['Invalid manifest'])
        self.assertIn('HTTP 200 at ' + self.ORG_NEW, ctx.exception.summary)
        self._assert_body_not_in(ctx.exception)
        self.assertEqual(ctx.exception.body_excerpt, '')  # Explained by the errors.
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_missing_confirmation_form_raises_page_shape(self):
        # A POST form to the confirmation's action without an owner field
        # isn't one.
        self._add_preview(body=f'<p>{BODY_MARKER}</p><form action="/search"></form>'
                               '<form method="post" action="/settings/apps">'
                               '<input type="hidden" name="integration_manifest[name]" value="x">'
                               '<button>Create</button></form>')
        with self.assertRaisesRegex(
                forms.PageShapeError,
                r"^Expected one manifest confirmation form with an "
                r"integration_manifest\[owner_login\] field, found 0\. Form actions on the page: "
                r"\['/search', '/settings/apps'\]: HTTP 200 at .*/settings/apps/manifest ") as ctx:
            self._create()
        self._assert_body_not_in(ctx.exception)
        self.assertIn(BODY_MARKER, ctx.exception.body_excerpt)
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_ambiguous_confirmation_forms_raise_page_shape(self):
        extra = ('<form method="post" action="/settings/apps/other">'
                 '<input type="hidden" name="integration_manifest[owner_login]" value="test-org">'
                 '<button>Other</button></form></main>')
        self._add_preview(body=self.page.replace('</main>', extra))
        with self.assertRaisesRegex(forms.PageShapeError, 'found 2') as ctx:
            self._create()
        self.assertIn('/settings/apps/other', ctx.exception.body_excerpt)
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_owner_field_outside_the_form_counts_through_the_form_attribute(self):
        page = self.page.replace(
            '<input autocomplete="off" value="test-org" type="hidden" '
            'name="integration_manifest[owner_login]" id="integration_manifest_owner_login" />', ''
        ).replace('</main>', '<input type="hidden" form="new_integration_manifest" '
                             'name="integration_manifest[owner_login]" value="test-org"></main>')
        self.assertNotIn('id="integration_manifest_owner_login"', page)  # Removed from the form.
        self._add_preview(body=page)
        self._add_confirm(body=self.continue_setup_page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)
        self.assertIn(('integration_manifest[owner_login]', 'test-org'),
                      parse_qsl(responses.calls[2].request.body))

    @responses.activate
    def test_confirmation_without_submit_button_raises_before_posting(self):
        page = self.page.replace('type="submit" name="commit"', 'type="button" name="commit"')
        self._add_preview(body=page)
        with self.assertRaisesRegex(forms.PageShapeError, 'no submit button') as ctx:
            self._create()
        excerpt = ctx.exception.body_excerpt
        self.assertTrue(excerpt.startswith('<main>'), excerpt[:40])
        self.assertIn('type="button" name="commit"', excerpt)
        self.assertNotIn('FIXTURE_TOKEN', excerpt)
        self.assertNotIn('FIXTURE_MANIFEST_TOKEN', excerpt)
        self.assertIn('name="authenticity_token" value="[REDACTED]"', excerpt)
        self.assertNotIn(excerpt, str(ctx.exception))
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_confirmation_answered_with_visible_errors(self):
        self._add_preview()
        self._add_confirm(body=f'<title>New GitHub App</title><p>{BODY_MARKER}</p>'
                               '<div class="flash flash-error">Name has already been taken</div>')
        with self.assertRaisesRegex(
                forms.FormRejectedError,
                r"HTTP 200 at https://github\.example\.com/settings/apps \(no redirect\); page title "
                r"'New GitHub App'\. Visible errors: Name has already been taken\.$") as ctx:
            self._create()
        self.assertEqual(ctx.exception.errors, ['Name has already been taken'])
        self._assert_body_not_in(ctx.exception)
        self.assertEqual(ctx.exception.body_excerpt, '')

    @responses.activate
    def test_confirmation_answered_with_unrecognized_page_is_unknown_outcome(self):
        # The confirmation was sent and the page neither delivers a code nor
        # shows an error, so whether GitHub accepted it is unknown.
        self._add_preview()
        self._add_confirm(body=f'<p>{BODY_MARKER}</p>')
        with self.assertRaisesRegex(
                forms.OutcomeUnknownError,
                r'^The manifest confirmation was answered without a redirect by a page that has no '
                r'a#redirect link or meta refresh to a URL with a code, so whether GitHub accepted '
                r'it is unknown: HTTP 200 at .*/settings/apps \(no redirect\)\. Visible errors: '
                r'none\.$') as ctx:
            self._create()
        self._assert_body_not_in(ctx.exception)
        self.assertEqual(ctx.exception.body_excerpt, f'<p>{BODY_MARKER}</p>')
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_unrecognized_page_excerpt_never_carries_the_manifest_code(self):
        # The code converts into the app's private key, so a page that links
        # to the callback, but not the way the continue-setup page does, must
        # not leak it through the excerpt.
        self._add_preview()
        self._add_confirm(body=f'<body><a href="{CALLBACK.replace("&", "&amp;")}">continue</a>'
                               '<script>location = "' + CALLBACK + '"</script></body>')
        with self.assertRaises(forms.OutcomeUnknownError) as ctx:
            self._create()
        excerpt = ctx.exception.body_excerpt
        self.assertNotIn('c0de', excerpt)
        self.assertNotIn('c0de', str(ctx.exception))
        self.assertIn('callback?code=[REDACTED]&amp;state=s1', excerpt)

    @responses.activate
    def test_continue_setup_page_returns_its_callback_link(self):
        # GitHub answers the confirmation with a 200 page whose meta refresh
        # and a#redirect link carry the code. They point to the same URL: one
        # callback.
        self.assertIn(self.CONTINUE_SETUP_META_REFRESH, self.continue_setup_page)
        self.assertIn(self.CONTINUE_SETUP_LINK_HREF, self.continue_setup_page)
        self._add_preview()
        self._add_confirm(body=self.continue_setup_page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)
        # The callback itself is never requested.
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_continue_setup_page_relative_link_is_resolved_against_the_page(self):
        page = self.continue_setup_page.replace('http://127.0.0.1/callback?', '/callback?')
        self._add_preview()
        self._add_confirm(body=page)
        self.assertEqual(self._create(),
                         self.BASE + '/callback?code=FIXTURE_CODE&state=test_state')

    def test_continue_setup_page_without_code_is_unknown_outcome(self):
        for link in ('http://127.0.0.1/callback?state=test_state',
                     'http://127.0.0.1/callback?code=&amp;state=test_state',
                     'http://127.0.0.1/callback?code=%20&amp;state=test_state',
                     ''):
            with self.subTest(link=link), responses.RequestsMock() as rsps:
                page = self.continue_setup_page.replace(
                    'http://127.0.0.1/callback?code=FIXTURE_CODE&amp;state=test_state', link)
                self.assertNotIn('FIXTURE_CODE', page)
                rsps.post(self.ORG_NEW, status=302, headers={'Location': self.MANIFEST_PAGE})
                rsps.get(self.MANIFEST_PAGE, body=self.page)
                rsps.post(self.CONFIRM, body=page)
                with self.assertRaisesRegex(forms.OutcomeUnknownError,
                                            r"has no a#redirect link.*whether GitHub accepted it "
                                            r"is unknown.*"
                                            r"'Continue setting up your GitHub App'") as ctx:
                    self._create()
                self.assertIn('You are being redirected to continue setup',
                              ctx.exception.body_excerpt)
                self.assertEqual(ctx.exception.errors, [])

    @responses.activate
    def test_meta_refresh_to_a_url_with_a_code_is_returned(self):
        self._add_preview()
        self._add_confirm(body='<head><meta http-equiv="Refresh" '
                               f'content="0; URL=\'{CALLBACK.replace("&", "&amp;")}\'"></head>')
        self.assertEqual(self._create(), CALLBACK)

    def test_meta_refresh_content_is_parsed_as_a_browser_does(self):
        # The "url=" prefix and the quotes are optional.
        for content in (f'0; url={CALLBACK}', f'0;URL="{CALLBACK}"', f' 0 , {CALLBACK} ',
                        f'0;{CALLBACK}', f"1.5; url = '{CALLBACK}"):
            with self.subTest(content=content), responses.RequestsMock() as rsps:
                rsps.post(self.ORG_NEW, status=302, headers={'Location': self.MANIFEST_PAGE})
                rsps.get(self.MANIFEST_PAGE, body=self.page)
                rsps.post(self.CONFIRM, body=(f'<head><meta http-equiv="refresh" '
                                              f'content="{html.escape(content)}"></head>'))
                self.assertEqual(self._create(), CALLBACK)

    @responses.activate
    def test_meta_refresh_without_a_url_is_not_a_callback(self):
        self._add_preview()
        self._add_confirm(body='<head><meta http-equiv="refresh" content="30"></head>')
        with self.assertRaisesRegex(forms.OutcomeUnknownError, 'has no a#redirect link'):
            self._create()

    @responses.activate
    def test_callback_link_on_a_client_error_page_is_returned(self):
        # Withholding a delivered code would lose the only way to register
        # the app and to reach its credentials; a code GitHub didn't issue
        # just fails to convert.
        self._add_preview()
        self._add_confirm(status=422, body=self.continue_setup_page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)

    def test_continue_setup_page_link_or_meta_refresh_alone_is_the_callback(self):
        for name, page in (
                ('link only', self.continue_setup_page.replace(self.CONTINUE_SETUP_META_REFRESH, '')),
                ('meta refresh only',
                 self.continue_setup_page.replace(self.CONTINUE_SETUP_LINK_HREF, 'href="#"'))):
            with self.subTest(name), responses.RequestsMock() as rsps:
                self.assertNotEqual(page, self.continue_setup_page)
                rsps.post(self.ORG_NEW, status=302, headers={'Location': self.MANIFEST_PAGE})
                rsps.get(self.MANIFEST_PAGE, body=self.page)
                rsps.post(self.CONFIRM, body=page)
                self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)

    @responses.activate
    def test_different_urls_with_a_code_are_unknown_outcome(self):
        page = self.continue_setup_page.replace(
            self.CONTINUE_SETUP_META_REFRESH,
            self.CONTINUE_SETUP_META_REFRESH.replace('FIXTURE_CODE', 'OTHER_CODE'))
        self.assertIn('OTHER_CODE', page)
        self._add_preview()
        self._add_confirm(body=page)
        with self.assertRaisesRegex(forms.OutcomeUnknownError,
                                    'links to 2 different URLs with a code, so whether GitHub '
                                    'accepted it is unknown') as ctx:
            self._create()
        for code in ('FIXTURE_CODE', 'OTHER_CODE'):
            self.assertNotIn(code, str(ctx.exception))
            self.assertNotIn(code, ctx.exception.body_excerpt)

    @responses.activate
    def test_redirect_link_inside_a_template_is_not_the_callback(self):
        page = self.continue_setup_page.replace(self.CONTINUE_SETUP_META_REFRESH, '')
        page = page.replace('<a id="redirect"', '<template><a id="redirect"')
        page = page.replace('this setup page</a>', 'this setup page</a></template>')
        self._add_preview()
        self._add_confirm(body=page)
        with self.assertRaises(forms.OutcomeUnknownError):
            self._create()

    @responses.activate
    def test_callback_link_is_returned_despite_visible_errors(self):
        # The code is the only way to the app's credentials.
        page = self.continue_setup_page.replace(
            '<div class="container-md',
            '<div class="flash flash-error">Something went wrong</div><div class="container-md')
        self._add_preview()
        self._add_confirm(body=page)
        self.assertEqual(self._create(), CONTINUE_SETUP_CALLBACK)

    @responses.activate
    def test_confirmation_client_error_raises_page_shape(self):
        self._add_preview()
        self._add_confirm(status=422, body=f'<p>{BODY_MARKER}</p>')
        with self.assertRaisesRegex(forms.PageShapeError, 'HTTP 422') as ctx:
            self._create()
        self._assert_body_not_in(ctx.exception)
        self.assertIn(BODY_MARKER, ctx.exception.body_excerpt)

    @responses.activate
    def test_confirmation_server_error_is_unknown_outcome_and_not_retried(self):
        self._add_preview()
        self._add_confirm(status=502, body=f'<p>{BODY_MARKER}</p>')
        with self.assertRaisesRegex(forms.OutcomeUnknownError, 'HTTP 502') as ctx:
            self._create()
        self._assert_body_not_in(ctx.exception)
        self.assertEqual(ctx.exception.body_excerpt, '')
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_confirmation_rate_limited_raises_http_error(self):
        self._add_preview()
        self._add_confirm(status=429, body=BODY_MARKER)
        with self.assertRaises(requests.HTTPError) as ctx:
            self._create()
        self.assertNotIsInstance(ctx.exception, forms.FormError)
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_confirmation_read_timeout_is_unknown_outcome(self):
        self._add_preview()
        self._add_confirm(body=requests.ReadTimeout('read timed out'))
        with self.assertRaisesRegex(forms.OutcomeUnknownError, 'may have reached GitHub') as ctx:
            self._create()
        self.assertIsInstance(ctx.exception.__cause__, requests.ReadTimeout)

    @responses.activate
    def test_confirmation_connect_timeout_is_reraised(self):
        self._add_preview()
        self._add_confirm(body=requests.ConnectTimeout('connect timed out'))
        with self.assertRaises(requests.ConnectTimeout):
            self._create()

    @responses.activate
    def test_redirect_without_location_is_unknown_outcome(self):
        self._add_preview()
        self._add_confirm(status=302)
        with self.assertRaisesRegex(
                forms.OutcomeUnknownError,
                r'^The manifest confirmation was answered with a redirect to \[\] instead of a page '
                r'with the callback URL, so whether GitHub accepted it is unknown\. HTTP 302 at '
                r'https://github\.example\.com/settings/apps \(no redirect\)$'):
            self._create()

    @responses.activate
    def test_redirect_without_code_is_unknown_outcome(self):
        # For example GitHub may have accepted the confirmation but ignored
        # redirect_url: the code would be lost, so this must not be reported
        # as success.
        self._add_preview()
        app_page = self.BASE + '/organizations/test-org/settings/apps/test-app'
        self._add_confirm(status=302, headers={'Location': app_page})
        with self.assertRaisesRegex(forms.OutcomeUnknownError,
                                    r'redirect to \[.*/apps/test-app\].*whether GitHub '
                                    r'accepted it is unknown'):
            self._create()
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_redirect_to_sign_in_raises_login_challenge(self):
        self._add_preview()
        self._add_confirm(status=302, headers={'Location': '/login?return_to=x'})
        with self.assertRaisesRegex(api.LoginChallengeError, 'sign-in or sudo confirmation page'):
            self._create()
        self.assertEqual(len(responses.calls), 3)

    @responses.activate
    def test_redirect_to_sudo_confirmation_raises_login_challenge(self):
        self._add_preview()
        self._add_confirm(status=302, headers={'Location': self.BASE + '/sessions/sudo'})
        with self.assertRaises(api.LoginChallengeError) as ctx:
            self._create()
        self.assertIn('sudo confirmation page', str(ctx.exception))
        self.assertNotIn('Not signed in', str(ctx.exception))

    def test_redirect_with_a_code_is_unknown_outcome(self):
        # GitHub delivers the code on the continue-setup page, so a redirect
        # is not a definitive answer even if it carries one. The confirmation
        # may have been accepted, and the code never appears in the message.
        for location in (CALLBACK, '/callback?code=c0de&state=s1',
                         'http://127.0.0.1/callback?state=s1&code=c0de#x'):
            with self.subTest(location=location), responses.RequestsMock() as rsps:
                rsps.post(self.ORG_NEW, status=302, headers={'Location': self.MANIFEST_PAGE})
                rsps.get(self.MANIFEST_PAGE, body=self.page)
                rsps.post(self.CONFIRM, status=302, headers={'Location': location})
                with self.assertRaisesRegex(forms.OutcomeUnknownError,
                                            r'redirect to \[.*callback\?.*code=\[REDACTED\].*\].*'
                                            r'whether GitHub accepted it is unknown') as ctx:
                    self._create()
                self.assertNotIn('c0de', str(ctx.exception))
                self.assertNotIn('c0de', ctx.exception.summary)
                # The callback itself is never requested.
                self.assertEqual(len(rsps.calls), 3)

    @responses.activate
    def test_redirect_to_sign_in_redacts_its_code(self):
        self._add_preview()
        self._add_confirm(status=302, headers={'Location': '/login?return_to=x&code=c0de'})
        with self.assertRaises(api.LoginChallengeError) as ctx:
            self._create()
        self.assertIn('code=[REDACTED]', str(ctx.exception))
        self.assertNotIn('c0de', str(ctx.exception))

    @responses.activate
    def test_redirect_to_another_hosts_login_path_is_unknown_outcome(self):
        self._add_preview()
        self._add_confirm(status=302, headers={'Location': 'https://idp.example.com/login'})
        with self.assertRaises(forms.OutcomeUnknownError):
            self._create()

    @responses.activate
    def test_manifest_client_error_is_typed_and_not_retried(self):
        responses.post(self.ORG_NEW, status=404,
                       body=f'<title>Page not found</title><p>{BODY_MARKER}</p>')
        with self.assertRaisesRegex(
                forms.PageShapeError,
                r"did not render the manifest confirmation page: HTTP 404 .*'Page not found'"
        ) as ctx:
            self._create()
        self.assertIsInstance(ctx.exception.__cause__, requests.HTTPError)
        self._assert_body_not_in(ctx.exception)
        self.assertIn(BODY_MARKER, ctx.exception.body_excerpt)
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    def test_manifest_client_error_with_visible_errors_is_rejected(self):
        responses.post(self.ORG_NEW, status=422,
                       body='<div class="flash flash-error">Invalid manifest</div>')
        with self.assertRaises(forms.FormRejectedError) as ctx:
            self._create()
        self.assertEqual(ctx.exception.errors, ['Invalid manifest'])
        self.assertEqual(len(responses.calls), 1)

    @responses.activate
    @mock.patch('time.sleep', return_value=None)
    def test_manifest_server_errors_after_retries_raise_http_error(self, mock_sleep):
        responses.post(self.ORG_NEW, status=503, body=BODY_MARKER)
        with self.assertRaises(requests.HTTPError) as ctx:
            self._create()
        self.assertNotIsInstance(ctx.exception, forms.FormError)
        self.assertEqual(len(responses.calls), forms.MAX_RETRIES + 1)

    @responses.activate
    def test_owner_login_matches_org_name_ignoring_case(self):
        self._add_preview(url=self.BASE + '/organizations/Test-Org/settings/apps/new')
        self._add_confirm(body=self.continue_setup_page)
        callback = self.gh.create_app_from_manifest(MANIFEST, 's1', org_name='Test-Org')
        self.assertEqual(callback, CONTINUE_SETUP_CALLBACK)

    @responses.activate
    def test_owner_login_matches_signed_in_user_ignoring_case(self):
        page = self.user_page.replace('content="test-user"', 'content="Test-User"')
        responses.post(self.BASE + '/settings/apps/new', body=page)
        responses.post(self.CONFIRM, body=self.continue_setup_page)
        self.assertEqual(self.gh.create_app_from_manifest(MANIFEST, 's1'), CONTINUE_SETUP_CALLBACK)

    @responses.activate
    def test_form_posting_to_another_host_is_not_the_confirmation(self):
        page = self.page.replace('action="/settings/apps"',
                                 'action="https://evil.example.com/settings/apps"')
        self._add_preview(body=page)
        with self.assertRaisesRegex(forms.PageShapeError, 'found 0'):
            self._create()
        self.assertEqual(len(responses.calls), 2)

    @responses.activate
    def test_timeout_is_passed_to_every_request(self):
        self._add_preview()
        self._add_confirm(body=self.continue_setup_page)
        self._create(timeout=7)
        # The manifest POST, the redirect to the confirmation page and the
        # confirmation.
        self.assertEqual([c.request.req_kwargs['timeout'] for c in responses.calls], [7, 7, 7])
