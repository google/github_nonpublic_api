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
import functools
from enum import Enum
import re
from urllib.parse import parse_qs, urljoin, urlparse

import html5lib
import pyotp
import requests
from absl import logging
from configobj import ConfigObj
from typing import Optional

from github_nonpublic_api import forms

GITHUB_URL = "https://github.com"


class LoginChallengeError(forms.FormError, RuntimeError):
    """Login did not complete: GitHub rejected the credentials or the
    two-factor code, showed a captcha or device verification page, or asked
    for a two-factor code that no callback can provide. Or GitHub answered a
    request with its sign-in or sudo confirmation page instead of acting on
    it."""


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
    page_forms = doc.findall(".//form")

    submit_form = None
    for form in page_forms:
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


def _is_login_page(url: str) -> bool:
    """True for GitHub's sign-in, failed sign-in, two-factor and device
    verification pages (/login..., /session, /sessions/...)."""
    path = urlparse(url).path.rstrip("/")
    return path in ("/login", "/session") or path.startswith(("/login/", "/sessions/"))


def _normalize_base_url(base_url: str) -> str:
    """`base_url` without a trailing slash.

    Raises:
        ValueError: `base_url` is not the absolute http(s) URL of a web root,
            for example a bare host name such as "github.example.com", or a
            URL with a path, query or fragment such as
            "https://github.example.com/prefix". Page paths are appended to
            `base_url`, and sign-in pages are recognized by their path from
            the host's root, so a path, query or fragment would break both.
    """
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.path.strip("/")
        or parsed.params
        # Not parsed.query or parsed.fragment: those are "" for a bare "?" or "#".
        or "?" in base_url
        or "#" in base_url
    ):
        raise ValueError(
            f"base_url must be an absolute http(s) URL of the web root, such as "
            f"'https://github.example.com', with no path, query or fragment, got [{base_url}]"
        )
    return base_url.rstrip("/")


def create_login_session(
    username: str,
    password: str,
    tfa_callback=None,
    session: Optional[requests.Session] = None,
    base_url: str = GITHUB_URL,
) -> requests.Session:
    """Create a requests.Session object with logged in GitHub cookies for the user.

    Args:
        username: The account to sign in as.
        password: The account's password.
        tfa_callback: Returns the current TOTP code. If None, the account must
            not require two-factor authentication.
        session: Session to sign in; a new one is created if None. Its cookies
            are cleared and transport retries are mounted on it (see
            `forms.configure_session_adapters`).
        base_url: The GitHub web root, for example "https://github.com" or a
            GitHub Enterprise Server URL such as "https://github.example.com".

    Raises:
        LoginChallengeError: GitHub did not sign the user in: it rejected the
            credentials or the two-factor code, stopped at a captcha or
            device verification page, or asked for a two-factor code without
            a `tfa_callback`.
        ValueError: `base_url` is not the absolute http(s) URL of a web root.
    """
    base_url = _normalize_base_url(base_url)
    base_host = urlparse(base_url).netloc
    session = session or requests.Session()
    forms.configure_session_adapters(session)

    # Clear cookies before re-authentication
    session.cookies.clear()

    def _login_callback(data):
        data.update(dict(login=username, password=password))

    res = _get_and_submit_form(
        session=session, url=f"{base_url}/login", data_callback=_login_callback
    )
    asked_for_code = "two-factor" in res.url
    if tfa_callback is None:
        # Without a TOTP callback the sign-in must be complete now: a sign-in,
        # two-factor or device verification page means it is not.
        if asked_for_code:
            raise LoginChallengeError(
                f"Login requires two-factor authentication, but no tfa_callback was given. "
                f"Stuck at: {res.url}"
            )
        if _is_login_page(res.url):
            message = (
                f"Sign-in did not complete (wrong credentials, captcha or device verification). "
                f"Stuck at: {res.url}"
            )
            logging.error("%s", message)
            raise LoginChallengeError(message)
        return session

    # With a TOTP callback GitHub must ask for the code next; any other page on
    # the same host is a challenge.
    if not asked_for_code and base_host in res.url:
        message = (
            f"Login challenged by GitHub: expected a two-factor prompt after the password "
            f"(wrong credentials, captcha or device verification?). Stuck at: {res.url}"
        )
        logging.error("%s", message)
        raise LoginChallengeError(message)

    def _tfa_callback(data):
        data.update(dict(otp=tfa_callback()))

    res = _get_and_submit_form(
        session=session,
        url=f"{base_url}/sessions/two-factor",
        data_callback=_tfa_callback,
    )
    # GitHub sends a session past the sign-in pages once it accepts the code;
    # still being on one (such as the two-factor page) means it did not.
    if urlparse(res.url).netloc.lower() == base_host.lower() and _is_login_page(res.url):
        message = f"Two-factor code was not accepted. Stuck at: {res.url}"
        logging.error("%s", message)
        raise LoginChallengeError(message)

    return session


_CREATE_ORG_URL = "{base_url}/account/organizations/new?plan=free"
_INSTALL_APP_URL = "{base_url}/apps/{app_name}/installations/new/permissions?target_id={org_id}"
_APP_SUSPEND_URL = "{base_url}/organizations/{org_name}/settings/installations/{app_install_id}"
_REQUEST_USAGE_URL = "{base_url}/enterprises/{enterprise_name}/settings/billing"
_USAGE_REPORT_URL = "{base_url}/enterprises/{enterprise_name}/settings/metered_exports/{report_id}"
_REQUEST_DORMANTUSERS_URL = (
    "{base_url}/enterprises/{enterprise_name}/settings/compliance"
)
_UPDATE_APP_INSTALL_URL = "{base_url}/organizations/{org_name}/settings/installations/{app_install_id}/permissions/update"
_UPDATE_SECURITY_ANALYSIS_URL = (
    "{base_url}/organizations/{org_name}/settings/security_analysis"
)
_UPDATE_GHAS_SETTINGS_URL = (
    "{base_url}/organizations/{org_name}/settings/security_analysis/ghas_settings"
)
# Apps owned by the signed-in user live under _USER_APPS_PATH; apps owned by an
# organization under _ORG_APPS_PATH. Each has a "new" page that accepts a
# manifest.
_USER_APPS_PATH = "/settings/apps"
_ORG_APPS_PATH = "/organizations/{org_name}/settings/apps"
# The manifest confirmation form's field naming the account that will own the
# app. It identifies the form and its owner, not the form's action: GitHub
# answers a manifest posted to an organization's new-app page with a redirect
# to /settings/apps/manifest, whose form posts to /settings/apps as the user's
# own confirmation form does, and answers one posted to an organization whose
# apps the user can't create with the user's form, which would make the app
# owned by the signed-in user instead.
_MANIFEST_OWNER_FIELD = "integration_manifest[owner_login]"


def _signed_in_login(doc) -> str:
    """The login of the signed-in user, from the `user-login` `<meta>` tag
    GitHub puts on every page, or "" if the page has none."""
    meta = doc.find(".//meta[@name='user-login']")
    return (meta.attrib.get("content") or "").strip() if meta is not None else ""


def _offered_owner_logins(doc, form) -> list:
    """The values a browser would submit for `form`'s owner field: the
    account GitHub offers to create the app for."""
    return [value for name, value in forms.browser_form_data(doc, form) if name == _MANIFEST_OWNER_FIELD]


def _manifest_confirmation_candidates(doc, page: requests.Response) -> list:
    """The POST forms on `page` that submit to `page`'s host and have an
    owner field, wherever they submit to on that host."""
    page_host = urlparse(page.url).netloc.lower()
    candidates = []
    for form in doc.findall(".//form"):
        action = urlparse(urljoin(page.url, form.attrib.get("action") or page.url))
        is_post = (form.attrib.get("method") or "get").lower() == "post"
        has_owner_field = any(
            c.attrib.get("name") == _MANIFEST_OWNER_FIELD for c in forms.browser_controls(doc, form)[0]
        )
        if is_post and action.netloc.lower() == page_host and has_owner_field:
            candidates.append(form)
    return candidates


def _unexpected_page_error(message: str, response: requests.Response) -> forms.FormError:
    """`FormRejectedError` if `response` shows visible errors, else
    `PageShapeError` with a redacted excerpt of the body as `body_excerpt`.
    The message carries those errors and the response summary, never the
    body."""
    summary = forms.response_summary(response)
    errors = forms.page_errors(response.text)
    if errors:
        return forms.FormRejectedError(
            f"{message}: {summary}. Visible errors: {'; '.join(errors)}.",
            summary=summary,
            errors=errors,
        )
    return forms.PageShapeError(
        f"{message}: {summary}. Visible errors: none.",
        summary=summary,
        body_excerpt=forms.redacted_body_excerpt(response.text),
    )


def _find_manifest_confirmation_form(doc, page: requests.Response, org_name: Optional[str]):
    """Returns the form on `page` that confirms a posted manifest for
    `org_name`, or for the signed-in user if `org_name` is None.

    GitHub does not document that form, and its action need not be under the
    path the manifest was posted to, so this accepts exactly one POST form to
    `page`'s host with an `integration_manifest[owner_login]` field. A browser
    must submit exactly one value for that field, equal to `org_name` or the
    signed-in user's login (ignoring case: GitHub logins are
    case-insensitive), and the form must have a submit button.

    Raises:
        forms.FormRejectedError: There is no such form and the page shows
            visible errors (for example, an invalid manifest).
        forms.PageShapeError: There is no such form or more than one, it
            offers to create the app for another account (for example the
            signed-in user, who can't create `org_name`'s apps), the
            signed-in user can't be told from the page, or the form has no
            submit button. Its `body_excerpt` is a redacted excerpt of
            `page`.
    """
    summary = forms.response_summary(page)

    def page_shape_error(message: str) -> forms.PageShapeError:
        return forms.PageShapeError(
            f"{message}: {summary}.", summary=summary, body_excerpt=forms.redacted_body_excerpt(page.text)
        )

    candidates = _manifest_confirmation_candidates(doc, page)
    if len(candidates) != 1:
        if not candidates and forms.page_errors(page.text):
            raise _unexpected_page_error("GitHub rejected the manifest", page)
        actions = [urlparse(f.attrib.get("action", "")).path for f in doc.findall(".//form")]
        raise page_shape_error(
            f"Expected one manifest confirmation form with an {_MANIFEST_OWNER_FIELD} field, found "
            f"{len(candidates)}. Form actions on the page: {actions}"
        )

    form = candidates[0]
    offered = _offered_owner_logins(doc, form)
    if len(offered) != 1:
        raise page_shape_error(
            f"The manifest confirmation form submits {len(offered)} values for {_MANIFEST_OWNER_FIELD}, "
            f"not one"
        )
    signed_in = _signed_in_login(doc)
    if org_name:
        if offered[0].lower() != org_name.lower():
            user = f"[{signed_in}]" if signed_in else "the signed-in user"
            raise page_shape_error(
                f"GitHub offered to create the app for [{offered[0]}], not [{org_name}] (is {user} "
                f"an owner of [{org_name}]?)"
            )
    elif not signed_in:
        raise page_shape_error(
            f"GitHub offered to create the app for [{offered[0]}], but the page doesn't say which "
            f"user is signed in (no user-login <meta> tag), so it can't be checked"
        )
    elif offered[0].lower() != signed_in.lower():
        raise page_shape_error(
            f"GitHub offered to create the app for [{offered[0]}], not for the signed-in user "
            f"[{signed_in}]"
        )

    if not any(forms.is_submit_button(c) for c in forms.browser_controls(doc, form)[0]):
        raise page_shape_error(
            "The manifest confirmation form has no submit button, so a browser couldn't submit it either"
        )
    return form


def _has_code(url: str) -> bool:
    """True if `url` has a `code` query parameter that isn't blank."""
    return any(code.strip() for code in parse_qs(urlparse(url).query).get("code", []))


# A non-empty `code` query parameter's name and value in a URL.
_URL_CODE_PARAMETER = re.compile(r"([?&]code=)([^&#]+)", re.I)


def _redact_code(url: str) -> str:
    """`url` with the value of each `code` query parameter replaced by
    "[REDACTED]": the code converts into the app's private key."""
    return _URL_CODE_PARAMETER.sub(lambda m: m.group(1) + forms.REDACTED, url)


def _manifest_redirect_error(response: requests.Response, submit_url: str) -> forms.FormError:
    """The error for a redirect that answered a manifest confirmation. GitHub
    delivers the callback URL on a continue-setup page, not by a redirect
    (see `_manifest_callback_from_page`). The message never carries the
    redirect's `code`, if it has one.

    Returns:
        LoginChallengeError: GitHub redirected to its sign-in or sudo
            confirmation page on `submit_url`'s host, so it did not act on
            the confirmation.
        forms.OutcomeUnknownError: Any other redirect, even to a URL with a
            `code`. GitHub may have accepted the confirmation.
    """
    location = response.headers.get("Location", "")
    target_url = urljoin(submit_url, location)
    summary = forms.response_summary(response)
    if (
        location
        and urlparse(target_url).netloc.lower() == urlparse(submit_url).netloc.lower()
        and _is_login_page(target_url)
    ):
        return LoginChallengeError(
            f"The manifest confirmation was redirected to GitHub's sign-in or sudo confirmation "
            f"page [{_redact_code(location)}], so GitHub did not act on it. {summary}",
            summary=summary,
        )
    return forms.OutcomeUnknownError(
        f"The manifest confirmation was answered with a redirect to [{_redact_code(location)}] "
        f"instead of a page with the callback URL, so whether GitHub accepted it is unknown. "
        f"{summary}",
        summary=summary,
    )


# The id of the link on the page GitHub answers a manifest confirmation with
# ("You are being redirected to continue setup of your GitHub App. If your
# browser does not redirect you automatically, please visit this setup
# page"). Its href is the callback URL with the code.
_MANIFEST_CALLBACK_LINK_ID = "redirect"
# The content of a <meta http-equiv="refresh">, as HTML parses it: a delay,
# then optionally ";" or ",", an optional "url=" and the URL, optionally
# quoted. For example "0; url='/callback?code=...'" or "0;/callback?code=...".
_META_REFRESH_CONTENT = re.compile(
    r"""\s*[\d.]*\s*[;,]?\s*(?:url\s*=\s*)?(?:'([^']*)'?|"([^"]*)"?|(.*))""", re.I | re.S
)


def _meta_refresh_target(content: str) -> str:
    """The URL of a `<meta http-equiv="refresh">` with `content`, or ""."""
    match = _META_REFRESH_CONTENT.match(content)
    return next((group for group in match.groups() if group is not None), "") if match else ""


def _page_callback_urls(response: requests.Response) -> list:
    """The distinct absolute URLs with a `code` that `response`'s page sends
    a browser to: the href of its `a#redirect` link and the target of a
    `<meta http-equiv="refresh">`."""
    doc = forms.parse_page(response.text)
    targets = [
        a.attrib.get("href") or ""
        for a in doc.iter("a")
        if a.attrib.get("id") == _MANIFEST_CALLBACK_LINK_ID
    ]
    targets += [
        _meta_refresh_target(meta.attrib.get("content") or "")
        for meta in doc.iter("meta")
        if (meta.attrib.get("http-equiv") or "").strip().lower() == "refresh"
    ]
    urls = []
    for target in targets:
        url = urljoin(response.url, target.strip())
        if target.strip() and _has_code(url) and url not in urls:
            urls.append(url)
    return urls


def _manifest_callback_from_page(response: requests.Response) -> str:
    """The callback URL with the conversion `code` on the page that answered
    a manifest confirmation without a redirect.

    GitHub answers the confirmation with a 200 page that says it is
    redirecting the browser to continue setting up the app, and puts the
    callback URL in a `<meta http-equiv="refresh">` and in its `a#redirect`
    link. A single URL with a code (from either) is returned whatever the
    page's status and visible errors, because the code is the only way to
    the app's credentials.

    Raises:
        forms.FormRejectedError: The page has no URL with a code and shows
            visible errors.
        forms.PageShapeError: The page has no URL with a code or visible
            errors, and a client error status (4xx), which GitHub returns
            when it refuses a request. Its `body_excerpt` holds a redacted
            excerpt of the page.
        forms.OutcomeUnknownError: The page has a 2xx status, no URL with a
            code and no visible errors, or it has several different URLs
            with a code, so whether GitHub accepted the confirmation is
            unknown. Its `body_excerpt` holds a redacted excerpt of the page.
    """
    urls = _page_callback_urls(response)
    if len(urls) == 1:
        return urls[0]
    errors = forms.page_errors(response.text)
    if not urls and (errors or not response.ok):
        raise _unexpected_page_error("GitHub did not accept the manifest confirmation", response)
    summary = forms.response_summary(response)
    if urls:
        problem = f"links to {len(urls)} different URLs with a code"
    else:
        problem = (
            f"has no a#{_MANIFEST_CALLBACK_LINK_ID} link or meta refresh to a URL with a code"
        )
    raise forms.OutcomeUnknownError(
        f"The manifest confirmation was answered without a redirect by a page that {problem}, "
        f"so whether GitHub accepted it is unknown: {summary}. "
        f"Visible errors: {'; '.join(errors) or 'none'}.",
        summary=summary,
        errors=errors,
        body_excerpt=forms.redacted_body_excerpt(response.text),
    )


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
        base_url: str = GITHUB_URL,
    ):
        """Signs in, unless a session is given.

        `base_url` is the GitHub web root: "https://github.com" (the default)
        or a GitHub Enterprise Server URL such as "https://github.example.com".

        Raises:
            ValueError: `base_url` is not the absolute http(s) URL of a web
                root.
        """
        self._base_url = _normalize_base_url(base_url)
        self._session = session or create_login_session(
            username=username,
            password=password,
            tfa_callback=tfa_callback,
            session=session,
            base_url=self._base_url,
        )

    def request_usage(self, enterprise_name: str, days: int = 30) -> requests.Response:
        """Requests a GitHub usage report.

        Github will send an email link when the report is available.
        """

        def _request_usage_callback(data):
            data["days"] = days

        action = f"/enterprises/{enterprise_name}/settings/metered_exports"
        url = _REQUEST_USAGE_URL.format(
            base_url=self._base_url, enterprise_name=enterprise_name
        )
        return _get_and_submit_form(
            session=self._session,
            url=url,
            data_callback=_request_usage_callback,
            form_matcher=lambda form: form.attrib.get("action") == action,
        )

    def create_app_from_manifest(
        self,
        manifest_payload: str,
        state: str,
        org_name: Optional[str] = None,
        timeout: int = forms.DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        """Confirms a GitHub App manifest; returns the callback URL with its code.

        Follows GitHub's manifest flow
        (https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest)
        the way a browser does:

        1.  POST `manifest_payload` with `state` to the new-app page of the
            signed-in user, or of `org_name` if set. This changes nothing;
            GitHub answers with a redirect to a confirmation page.
        2.  Check that the confirmation form makes `org_name` (or the
            signed-in user) the app's owner, then submit it exactly as a
            browser would, without following any redirect.
        3.  Return the URL GitHub sends the browser to next: the manifest's
            `redirect_url` with the single-use `code` and `state` query
            parameters. GitHub answers the confirmation with a 200 page
            saying it is redirecting to continue setting up the app, whose
            `<meta http-equiv="refresh">` and `a#redirect` link hold it.

        Exchange the code for the app's ID and private key with the REST API
        (`POST /app-manifests/{code}/conversions`), and check that `state`
        matches. The code is delivered only here. The exchange is what
        registers the app: confirming the manifest does not, so if the code is
        lost after the confirmation, no app is left behind.

        Args:
            manifest_payload: The manifest, JSON-encoded.
            state: Opaque value GitHub echoes back in the callback URL.
            org_name: Organization that will own the app; the signed-in user
                if None. The signed-in user must be allowed to create the
                organization's apps (an organization owner).
            timeout: Seconds to wait for each HTTP response.

        Raises:
            LoginChallengeError: GitHub sent the session to its sign-in or
                sudo confirmation page. No app was created.
            forms.FormRejectedError: GitHub rendered visible errors instead of
                the confirmation page, or answered the confirmation with
                visible errors and no URL with a `code`.
            forms.PageShapeError: The manifest was answered with an error
                status or without a single confirmation form with a submit
                button, or the confirmation form would make another account
                the app's owner (as GitHub offers when the signed-in user
                can't create `org_name`'s apps); the confirmation was not
                sent. Or the confirmation was answered with a client error
                status (4xx) and no URL with a `code`. Its `body_excerpt`
                holds a redacted excerpt of the page.
            forms.OutcomeUnknownError: The confirmation may have reached
                GitHub, but no definitive answer came back; GitHub answered
                with a redirect (even to a URL with a `code`) to anywhere
                but its sign-in or sudo confirmation page; or it answered
                with a page that links to several different URLs with a
                `code`, or with a 2xx page that has no URL with a `code` and
                no visible errors (for these pages, `body_excerpt` holds a
                redacted excerpt). Whether GitHub accepted the confirmation
                is unknown; no app was registered either way, since that
                takes exchanging a code and the caller gets none.
            requests.RequestException: Step 1 failed, or the confirmation was
                never sent or was rate limited. No app was created. Step 1 is
                retried on rate limits and server errors. It is not retried
                on read errors (a read timeout or dropped connection), since
                the transport retries don't resend a POST after one. With a
                session passed to `Api(session=...)` it has no transport
                retries at all, not even for connection failures, unless the
                caller mounted them (see `forms.configure_session_adapters`).
        """
        apps_path = _ORG_APPS_PATH.format(org_name=org_name) if org_name else _USER_APPS_PATH
        try:
            preview = forms.request_with_retry(
                self._session,
                "POST",
                f"{self._base_url}{apps_path}/new",
                params={"state": state},
                data={"manifest": manifest_payload},
                # Posting a manifest only renders the confirmation page.
                retry_unsafe_methods=True,
                timeout=timeout,
            )
        except requests.HTTPError as e:
            if e.response is None or forms.is_retryable(e.response):
                raise
            # For example a 404 for an organization the user doesn't own:
            # retrying cannot help.
            raise _unexpected_page_error(
                "GitHub did not render the manifest confirmation page", e.response
            ) from e
        if _is_login_page(preview.url):
            raise LoginChallengeError(
                f"The manifest was answered with GitHub's sign-in or sudo confirmation page. "
                f"{forms.response_summary(preview)}",
                summary=forms.response_summary(preview),
            )

        doc = forms.parse_page(preview.text)
        form = _find_manifest_confirmation_form(doc, preview, org_name)
        data = forms.browser_form_data(doc, form)
        submit_url = urljoin(preview.url, form.attrib.get("action") or preview.url)
        logging.info("Confirming GitHub App manifest at %s", submit_url)
        response = forms.submit_once(
            self._session, submit_url, data=data, allow_redirects=False, timeout=timeout
        )

        if 300 <= response.status_code < 400:
            raise _manifest_redirect_error(response, submit_url)
        if forms.is_retryable(response):
            # Rate limited before GitHub acted on the confirmation.
            response.raise_for_status()
        return _manifest_callback_from_page(response)

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
            url=_CREATE_ORG_URL.format(base_url=self._base_url),
            data_callback=_create_org_callback,
            form_matcher=lambda f: f.attrib.get("id") == "org-new-form",
        )

    def install_application_in_organization(
        self, app_name: str, org_id: int
    ) -> requests.Response:
        """Installs the specified app on the given organization."""
        url = _INSTALL_APP_URL.format(base_url=self._base_url, app_name=app_name, org_id=org_id)

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
        url = _APP_SUSPEND_URL.format(
            base_url=self._base_url, org_name=org_name, app_install_id=app_install_id
        )

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
            base_url=self._base_url, enterprise_name=enterprise_name, report_id=report_id
        )
        return _get_url_with_session(session=self._session, url=url)

    def request_dormant_users_report(self, enterprise_name: str) -> requests.Response:
        """Requests a GitHub dormant users report.

        Github will send an email link when the report is available.
        """

        action = f"/enterprises/{enterprise_name}/settings/dormant-users/exports"
        url = _REQUEST_DORMANTUSERS_URL.format(
            base_url=self._base_url, enterprise_name=enterprise_name
        )
        return _get_and_submit_form(
            session=self._session,
            url=url,
            form_matcher=lambda form: form.attrib.get("action") == action,
        )

    def download_dormant_users_report(self, enterprise_name: str) -> requests.Response:
        """Download a usage report based on an id received in an email"""
        url = _REQUEST_DORMANTUSERS_URL.format(
            base_url=self._base_url, enterprise_name=enterprise_name
        )
        page = _get_url_with_session(session=self._session, url=url)
        try:
            page_content = page.content.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Unable to decode page content as UTF-8.")
        link = re.search(
            re.escape(self._base_url)
            + "/enterprises/"
            + re.escape(enterprise_name)
            + r"/settings/dormant-users/exports/[0-9A-Fa-f]{8}\-[0-9A-Fa-f]{4}\-[0-9A-Fa-f]{4}\-[0-9A-Fa-f]{4}\-[0-9A-Fa-f]{12}",
            page_content,
        )
        if link is None:
            raise ValueError("Unable to find dormant users report link in content.")
        return _get_url_with_session(session=self._session, url=link.group(0))

    def approve_updated_app_permissions(
        self,
        org_name: str,
        app_install_id: str,
    ) -> requests.Response:
        """Approve a request for updated permissions for a GitHub app installation in an org"""

        return _get_and_submit_form(
            session=self._session,
            url=_UPDATE_APP_INSTALL_URL.format(
                base_url=self._base_url,
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
        def _form_matcher(doc, name):
            return doc.findall(".//input[@name='%s']" % name)
        
        if code_scanning_autofix is not None:
            def _data_callback(data):
                data['code_scanning_autofix'] = 'enabled' if code_scanning_autofix else 'disabled'
            _get_and_submit_form(
                session=self._session,
                url=_UPDATE_GHAS_SETTINGS_URL.format(
                    base_url=self._base_url,
                    org_name=org_name,
                ),
                form_matcher=functools.partial(_form_matcher, name='code_scanning_autofix'),
                data_callback=_data_callback
            )
        if code_scanning_autofix_third_party_tools is not None:
            def _data_callback(data):
                data['code_scanning_autofix_third_party_tools'] = 'enabled' if code_scanning_autofix_third_party_tools else 'disabled'
            _get_and_submit_form(
                session=self._session,
                url=_UPDATE_GHAS_SETTINGS_URL.format(
                    base_url=self._base_url,
                    org_name=org_name,
                ),
                form_matcher=functools.partial(_form_matcher, name='code_scanning_autofix_third_party_tools'),
                data_callback=_data_callback
            )

if __name__ == "__main__":
    config = ConfigObj(os.path.expanduser("~/github.ini"), _inspec=True)  # type: ignore[not-callable]
    api = Api(
        username=config["username"],
        password=config["password"],
        tfa_callback=lambda: pyotp.TOTP(config["otp_seed"]).now(),
    )
