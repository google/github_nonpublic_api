"""An API to do things that aren't in the official public GitHub REST API."""

from urllib.parse import urljoin
from enum import Enum

import os.path
import html5lib
import pyotp
import requests

from absl import logging
from configobj import ConfigObj


def _get_and_submit_form(session, url: str, data_callback=None, form_id: str = None):
    logging.info('Fetching URL %s', url)
    response = session.get(url)
    response.raise_for_status()

    doc = html5lib.parse(response.text, namespaceHTMLElements=False)
    forms = doc.findall('.//form')

    # If no form_id is specified, just use the first (and probably only)
    # form.  Otherwise find the named form to submit to.
    submit_form = forms[0]
    if form_id:
        for form in forms:
            if form.attrib.get('id') == form_id:
                submit_form = form
                break
        else:
            raise ValueError('%s form not found' % form_id)

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


class OrganizationUsage(Enum):
    """Organization Usage for Organization Creation."""

    PERSONAL = 'standard'
    BUSINESS = 'corporate'


class Api(object):
    """API Endpoing for doing non-public things to GitHub.

    Ideally these would all exist as REST API endpoints, but instead we get
    to pretend to be a real user.
    """

    def __init__(self, username: str = None, password: str = None, tfa_callback = None,
                    session: requests.Session = None):
        self._session = session or create_login_session(
            username=username, password=password, tfa_callback=tfa_callback, session=session)

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
                                form_id='org-new-form')


if __name__ == "__main__":
    config = ConfigObj(os.path.expanduser('~/github.ini'), _inspec=True)

    api = Api(config['username'], config['password'],
              tfa_callback=lambda: pyotp.TOTP(config['otp_seed']).now())
    api.create_organization(org_name='blah',
                            contact_email='example@example.com',
                            org_usage=OrganizationUsage.PERSONAL)
