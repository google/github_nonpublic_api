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

"""An API to do things that aren't in the official public GitHub REST API."""

import os.path
from enum import Enum
from urllib.parse import urljoin

import html5lib
import pyotp
import requests
from absl import logging
from configobj import ConfigObj
from typing import Optional


def _get_and_submit_form(
    session, url: str, data_callback=None, form_matcher=lambda form: True
):
    logging.info("Fetching URL %s", url)
    response = session.get(url)
    response.raise_for_status()

    logging.info("Fetching URL %s", response.url)
    for redirect_response in response.history:
        logging.info("Redirected from: %s", redirect_response.url)

    doc = html5lib.parse(response.text, namespaceHTMLElements=False)
    forms = doc.findall(".//form")

    submit_form = None
    for form in forms:
        if form_matcher(form):
            submit_form = form
            break
    if submit_form is None:
        raise ValueError("Unable to find form")

    action_url = submit_form.attrib["action"]
    # Look at all the inputs under the given form.
    inputs = submit_form.findall(".//input")

    data = dict()
    for form_input in inputs:
        value = form_input.attrib.get("value")
        if value and "name" in form_input.attrib:
            data[form_input.attrib["name"]] = value

    # Have the caller provide additional data
    if data_callback:
        data_callback(data)

    logging.debug("Form data: %s", str(data))

    submit_url = urljoin(url, action_url)
    logging.info("Posting form to URL %s", submit_url)

    response = session.post(submit_url, data=data)
    response.raise_for_status()
    return response


def _get_url_with_session(session, url: str):
    logging.info("Fetching URL %s", url)
    response = session.get(url)
    response.raise_for_status()
    return response


def create_login_session(
    username: str, password: str, tfa_callback, session: Optional[requests.Session] = None
) -> requests.Session:
    """Create a requests.Session object with logged in GitHub cookies for the user."""
    session = session or requests.Session()

    # Clear cookies before re-authentication
    session.cookies.clear()

    def _login_callback(data):
        data.update(dict(login=username, password=password))

    _get_and_submit_form(
        session=session, url="https://github.com/login", data_callback=_login_callback
    )

    def _tfa_callback(data):
        data.update(dict(otp=tfa_callback()))

    _get_and_submit_form(
        session=session,
        url="https://github.com/sessions/two-factor",
        data_callback=_tfa_callback,
    )

    return session


_CREATE_ORG_URL = "https://github.com/account/organizations/new?plan=free"
_INSTALL_APP_URL = "https://github.com/apps/{app_name}/installations/new/permissions?target_id={org_id}"
_APP_SUSPEND_URL = "https://github.com/organizations/{org_name}/settings/installations/{app_install_id}"
_REQUEST_USAGE_URL = "https://github.com/enterprises/{enterprise_name}/settings/billing"
_USAGE_REPORT_URL = "https://github.com/enterprises/{enterprise_name}/settings/metered_exports/{report_id}"
_UPDATE_APP_INSTALL_URL = "https://github.com/organizations/{org_name}/settings/installations/{app_install_id}/permissions/update"
_UPDATE_SECURITY_ANALYSIS_URL = "https://github.com/organizations/{org_name}/settings/security_analysis"


class OrganizationUsage(Enum):
    """Organization Usage for Organization Creation."""

    PERSONAL = "standard"
    BUSINESS = "corporate"


class Api(object):
    """API Endpoing for doing non-public things to GitHub.

    Ideally these would all exist as REST API endpoints, but instead we get
    to pretend to be a real user.
    """

    def __init__(
        self,
        username: str,
        password: str,
        tfa_callback=None,
        session: Optional[requests.Session] = None,
    ):
        self._session = session or create_login_session(
            username=username,
            password=password,
            tfa_callback=tfa_callback,
            session=session,
        )

    def request_usage(self, enterprise_name: str, days: int = 30) -> requests.Response:
        """Requests a GitHub usage report.

        Github will send an email link when the report is available.
        """

        def _request_usage_callback(data):
            data["days"] = days

        action = f"/enterprises/{enterprise_name}/settings/metered_exports"
        url = url = _REQUEST_USAGE_URL.format(enterprise_name=enterprise_name)
        return _get_and_submit_form(
            session=self._session,
            url=url,
            data_callback=_request_usage_callback,
            form_matcher=lambda form: form.attrib.get("action") == action,
        )

    def create_organization(
        self,
        org_name: str,
        contact_email: str,
        org_usage: OrganizationUsage,
        business_name: Optional[str] = None,
    ) -> requests.Response:
        """Create the specified GitHub organization.

        Right now, only creates free tier organizations.
        """

        def _create_org_callback(data):
            data["organization[profile_name]"] = org_name
            data["organization[login]"] = org_name
            data["organization[billing_email]"] = contact_email
            data["terms_of_service_type"] = org_usage.value
            data["agreed_to_terms"] = "yes"
            if org_usage == OrganizationUsage.BUSINESS:
                data["organization[company_name]"] = business_name

        return _get_and_submit_form(
            session=self._session,
            url=_CREATE_ORG_URL,
            data_callback=_create_org_callback,
            form_matcher=lambda f: f.attrib.get("id") == "org-new-form",
        )

    def install_application_in_organization(
        self, app_name: str, org_id: int
    ) -> requests.Response:
        """Installs the specified app on the given organization."""
        url = _INSTALL_APP_URL.format(app_name=app_name, org_id=org_id)

        def _install_app_callback(data):
            data["install_target"] = "all"

        return _get_and_submit_form(
            session=self._session,
            url=url,
            data_callback=_install_app_callback,
            form_matcher=lambda form: app_name in form.attrib.get("action"),
        )

    def toggle_app_suspended(
        self, org_name: str, app_install_id: int
    ) -> requests.Response:
        """Set this applicaiton install to be suspended or not."""
        url = _APP_SUSPEND_URL.format(org_name=org_name, app_install_id=app_install_id)

        return _get_and_submit_form(
            session=self._session,
            url=url,
            form_matcher=lambda f: "suspended" in f.attrib.get("action"),
        )

    def download_usage_report(
        self, enterprise_name: str, report_id: int
    ) -> requests.Response:
        """Download a usage report based on an id recieved in an email"""
        url = _USAGE_REPORT_URL.format(
            enterprise_name=enterprise_name, report_id=report_id
        )
        return _get_url_with_session(session=self._session, url=url)

    def approve_updated_app_permissions(
        self,
        org_name: str,
        app_install_id: str,
    ) -> requests.Response:
        """Approve a request for updated permissions for a GitHub app installation in an org"""

        return _get_and_submit_form(
            session=self._session,
            url=_UPDATE_APP_INSTALL_URL.format(
                org_name=org_name,
                app_install_id=app_install_id,
            ),
            form_matcher=lambda f: f.attrib.get("class")
            == "js-integrations-install-form",
        )

    def update_security_analysis_settings(
        self,
        org_name: str,
        code_scanning_autofix_third_party_tools: Optional[bool] = None,
        code_scanning_autofix: Optional[bool] = None,
    ):
        request = dict()
        if code_scanning_autofix is not None:
            request['code_scanning_autofix'] = 'enabled' if code_scanning_autofix else 'disabled'
        if code_scanning_autofix_third_party_tools is not None:
            request['code_scanning_autofix_third_party_tools'] = 'enabled' if code_scanning_autofix_third_party_tools else 'disabled'

        return _get_and_submit_form(
            session=self._session,
            url=_UPDATE_SECURITY_ANALYSIS_URL.format(
                org_name=org_name,
            ),
            # This is kinda hacky but should work
            form_matcher=lambda f: "js-setting-toggle" in f.attrib.get("class")
        )


if __name__ == "__main__":
    config = ConfigObj(os.path.expanduser("~/github.ini"), _inspec=True)

    api = Api(
        config["username"],
        config["password"],
        tfa_callback=lambda: pyotp.TOTP(config["otp_seed"]).now(),
    )
