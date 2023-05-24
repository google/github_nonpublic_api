"""Unit tests for api.py."""

import os
from unittest import TestCase, mock

from truth.truth import AssertThat

from github_nonpublic_api import api

GITHUB_FORM_HTML = os.path.join(os.path.dirname(__file__), 'github_form.html')
NEW_ORG_FORM_HTML = os.path.join(
    os.path.dirname(__file__), 'new_org_form.html')


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

        AssertThat(self.session.post).WasCalled().Once().With(
            'http://github.com/foo', data=dict(add='yes', key='value'))

    def test_get_and_submit_form_by_id(self):
        self._seed_session_with_file(GITHUB_FORM_HTML)

        api._get_and_submit_form(
            session=self.session, url='http://github.com', form_id='form2')

        AssertThat(self.session.post).WasCalled().Once().With(
            'http://github.com/form2', data=dict(key='value2'))

    def test_get_and_submit_form_by_id_error(self):
        self._seed_session_with_file(GITHUB_FORM_HTML)

        with AssertThat(ValueError).IsRaised():
            api._get_and_submit_form(
                session=self.session, url='http://github.com', form_id='no_form')

    def test_create_business_org(self):
        self._seed_session_with_file(NEW_ORG_FORM_HTML)
        gh = api.Api(session=self.session)
        gh.create_organization(org_name='test', contact_email='nobody@google.com',
                                org_usage=api.OrganizationUsage.BUSINESS, 
                                business_name='A Fake Business')
        AssertThat(self.session.post).WasCalled().Once().With(
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
        AssertThat(self.session.post).WasCalled().Once().With(
            'https://github.com/account/organizations/new_org', data={
                'authenticity_token': 'value',
                'agreed_to_terms': 'yes',
                'terms_of_service_type': 'standard',
                'organization[billing_email]': 'nobody@google.com',
                'organization[profile_name]': 'test',
                'organization[login]': 'test',
            })
