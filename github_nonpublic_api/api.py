"""An API to do things that aren't in the official public GitHub REST API."""

from urllib.parse import urljoin
from enum import Enum

import os.path
import html5lib
import pyotp
import requests

from absl import logging
from configobj import ConfigObj


def _get_and_submit_form(session, url: str, data_callback=None, form_matcher=lambda form: True):
    logging.info('Fetching URL %s', url)
    response = session.get(url)
    response.raise_for_status()

    doc = html5lib.parse(response.text, namespaceHTMLElements=False)
    forms = doc.findall('.//form')

    submit_form = None
    for form in forms:
        if form_matcher(form):
            submit_form = form
            break
    if submit_form is None:
        raise ValueError('Unable to find form')

    action_url = submit_form.attrib['action']
    # Look at all the inputs under the given form.
    inputs = submit_form.findall('.//input')

    data = dict()
    for form_input in inputs:
        value = form_input.attrib.get('value')
        if value and 'name' in form_input.attrib:
            data[form_input.attrib['name']] = value

    # Have the caller provide additional data
    if data_callback:
        data_callback(data)

    logging.debug('Form data: %s', str(data))

    logging.info('Posting from to  URL %s', url)

    response = session.post(urljoin(url, action_url), data=data)
    response.raise_for_status()
    return response


def create_login_session(username: str, password: str,
                         tfa_callback, session: requests.Session = None) -> requests.Session:
    session = session or requests.Session()

    def _login_callback(data):
        data.update(dict(login=username, password=password))
    _get_and_submit_form(
        session=session, url='https://github.com/login', data_callback=_login_callback)

    def _tfa_callback(data):
        data.update(dict(otp=tfa_callback()))
    _get_and_submit_form(
        session=session, url='https://github.com/sessions/two-factor', data_callback=_tfa_callback)

    return session


_CREATE_ORG_URL = 'https://github.com/account/organizations/new?plan=free'
_INSTALL_APP_URL = 'https://github.com/apps/{app_name}/installations/new/permissions?target_id={org_id}'
_APP_SUSPEND_URL = 'https://github.com/organizations/{org_name}/settings/installations/{app_install_id}'
_REQUEST_USAGE_URL= 'https://github.com/enterprises/alphabet/settings/billing'


class OrganizationUsage(Enum):
    """Organization Usage for Organization Creation."""

    PERSONAL = 'standard'
    BUSINESS = 'corporate'


class Api(object):
    """API Endpoing for doing non-public things to GitHub.

    Ideally these would all exist as REST API endpoints, but instead we get
    to pretend to be a real user.
    """

    def __init__(self, username: str = None, password: str = None, tfa_callback=None,
                 session: requests.Session = None):
        self._session = session or create_login_session(
            username=username, password=password, tfa_callback=tfa_callback, session=session)
        
        
    def request_usage(self, days: int = 30):
        """Requests a GitHub usage report.

        Github will send an email link when the report is available.
        """

        def _request_usage_callback(data):
            data['days'] = days
        
        _get_and_submit_form(session=self._session,
                             url=_REQUEST_USAGE_URL, 
                             data_callback=_request_usage_callback,
                             form_matcher=lambda form: form.attrib.get('action') == 
                                '/enterprises/alphabet/settings/metered_exports')

    def create_organization(self, org_name: str, contact_email: str,
                            org_usage: OrganizationUsage, business_name: str = None):
        """Create the specified GitHub organization.

        Right now, only creates free tier organizations.
        """

        def _create_org_callback(data):
            data['organization[profile_name]'] = org_name
            data['organization[login]'] = org_name
            data['organization[billing_email]'] = contact_email
            data['terms_of_service_type'] = org_usage.value
            data['agreed_to_terms'] = 'yes'
            if org_usage == OrganizationUsage.BUSINESS:
                data['organization[company_name]'] = business_name

        _get_and_submit_form(session=self._session,
                             url=_CREATE_ORG_URL, data_callback=_create_org_callback,
                             form_matcher=lambda form: form.attrib.get('id') == 'org-new-form')

    def install_application_in_organization(self, app_name: str, org_id: int):
        """Installs the specified app on the given organization."""
        url = _INSTALL_APP_URL.format(app_name=app_name, org_id=org_id)

        _get_and_submit_form(session=self._session,
                             url=url,
                             form_matcher=lambda form: app_name in form.attrib.get('action'))

    def toggle_app_suspended(self, org_name: str, app_install_id: int):
        """Set this applicaiton install to be suspended or not."""
        url = _APP_SUSPEND_URL.format(org_name=org_name, app_install_id=app_install_id)

        _get_and_submit_form(session=self._session,
                             url=url,
                             form_matcher=lambda form: 'suspended' in form.attrib.get('action'))


if __name__ == "__main__":
    config = ConfigObj(os.path.expanduser('~/github.ini'), _inspec=True)

    api = Api(config['username'], config['password'],
              tfa_callback=lambda: pyotp.TOTP(config['otp_seed']).now())
