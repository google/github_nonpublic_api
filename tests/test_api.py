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

import os
from unittest import TestCase, mock

from github_nonpublic_api import api

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

    def test_create_business_org(self):
        self._seed_session_with_file(NEW_ORG_FORM_HTML)
        gh = api.Api(session=self.session)
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
        gh = api.Api(session=self.session)
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
        gh = api.Api(session=self.session)
        gh.request_usage(enterprise_name='test-enterprise', days=7)
        self.session.post.assert_called_once_with(
            'https://github.com/enterprises/test-enterprise/settings/metered_exports', data={
                'authenticity_token': 'value',
                'days': 7,
            })

    def test_install_app_on_org(self):
        self._seed_session_with_file(ADD_APP_FORM_HTML)
        gh = api.Api(session=self.session)
        gh.install_application_in_organization(app_name='test-app', org_id=42)
        self.session.post.assert_called_once_with(
            'https://github.com/apps/test-app/installations', data={
                'authenticity_token': 'value',
                'install_target': 'all',
            })

    def test_suspend_app_toggle(self):
        self._seed_session_with_file(SUSPEND_APP_FORM)
        gh = api.Api(session=self.session)
        gh.toggle_app_suspended(org_name='test-org', app_install_id=42)
        self.session.post.assert_called_once_with(
            'https://github.com/long/url/suspended', data={
                'authenticity_token': 'value',
            })

    def test_download_usage_report(self):
        self._seed_session_with_file(DOWNLOAD_USAGE_REPORT_CSV)
        gh = api.Api(session=self.session)
        gh.download_usage_report(enterprise_name='test-enterprise', report_id=1)
        self.session.get.assert_called_once_with('https://github.com/enterprises/test-enterprise/settings/metered_exports/1')

    def test_update_app_permissions(self):
        self._seed_session_with_file(UPDATE_APP_PERMISSIONS_FORM_HTML)
        gh = api.Api(session=self.session)
        gh.approve_updated_app_permissions(org_name='test-org', app_install_id=42)
        self.session.post.assert_called_once_with(
            'https://github.com/organizations/test-org/settings/installations/42/permissions/update',
            data={
                '_method': 'put',
                'authenticity_token': 'value',
                'version_id': '112233',
                'integration_fingerprint': 'value',
            },
        )
