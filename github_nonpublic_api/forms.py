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

"""Reads and submits GitHub web forms exactly like a browser does.

GitHub's web UI posts HTML forms. To automate a page reliably, a client has to
send the same fields a browser sends: every enabled control, the state of each
checkbox and radio, selected options, empty text fields, controls associated
through `form="..."`, and the submit button. This module provides:

*   `parse_page`: parses HTML the way a browser builds its forms.
*   `find_form_by_action`: picks a form by its action path and Rails `_method`.
*   `browser_form_data`: the entry list a browser's `FormData` would submit,
    optionally after simulated user edits.
*   `page_errors` and `response_summary`: what went wrong when GitHub rejects a
    submission, without returning the page body.
*   `redacted_body_excerpt`: a short excerpt of a page with its tokens
    redacted, for diagnosing a page that changed shape.
*   `describe_forms`: a redacted description of a page's forms, for verifying
    that a page still has the expected shape.
*   `request_with_retry` and `configure_session_adapters`: retries for rate
    limits, server errors and transport failures that never resend a
    non-idempotent request by default.
*   `submit_once`: sends a state-changing submission once and reports when its
    outcome is unknown (`OutcomeUnknownError`).

Failures are reported as `FormError` subclasses that carry the visible errors
and a response summary, so callers can tell a rejected submission
(`FormRejectedError`) from a page that changed shape (`PageShapeError`). A
`PageShapeError` raised for a response also carries a redacted excerpt of it,
outside the error message.
"""

from __future__ import annotations

import random
import re
import time
from typing import Optional
from urllib.parse import urlparse

import html5lib
import requests
from absl import logging
from requests.adapters import HTTPAdapter
from requests.exceptions import InvalidHeader, InvalidSchema, InvalidURL, MissingSchema
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError
from urllib3.util import Retry

MAX_RETRIES = 5
BASE_DELAY_SECONDS = 2.0
MAX_DELAY_SECONDS = 120.0
JITTER_MIN_SECONDS = 0.1
JITTER_MAX_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 30

# Methods that a server must be able to receive twice without a second effect.
# Only these are resent on a retryable status unless the caller opts in.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class FormError(Exception):
    """A GitHub web form could not be read or submitted as expected.

    Attributes:
        summary: Status, final URL, redirect chain and page title of the
            response involved (see `response_summary`), or "" if none.
        errors: Visible error texts GitHub rendered on that response (see
            `page_errors`).
        body_excerpt: A redacted, truncated excerpt of that response's body
            (see `redacted_body_excerpt`), or "" if none. It is never part of
            the message, so `str(error)` and logs of it never contain page
            HTML. Only `PageShapeError`s raised for a response set it: the
            page is what needs diagnosing when its shape changed, while the
            other errors are explained by `summary` and `errors`.
    """

    def __init__(
        self,
        message: str,
        summary: str = "",
        errors: Optional[list[str]] = None,
        body_excerpt: str = "",
    ):
        super().__init__(message)
        self.summary = summary
        self.errors = list(errors or [])
        self.body_excerpt = body_excerpt


class PageShapeError(FormError, ValueError):
    """The page lacks an expected form, control or submit button, or GitHub
    answered with a page or redirect the caller did not expect."""


class FormRejectedError(FormError, ValueError):
    """GitHub rejected a submission and rendered visible errors explaining why
    (for example, a name that is already taken)."""


class OutcomeUnknownError(FormError):
    """A request that changes state may have reached GitHub, but no
    definitive response came back (timeout, dropped connection or 5xx).

    The change may or may not have been applied; resending could apply it
    twice. Callers should read the current state before retrying.
    """


def configure_session_adapters(session: requests.Session) -> None:
    """Mounts transport-level retries onto `session`.

    Connection failures (refused, DNS failure, connect timeout) are retried
    for every method, since no request was sent. Read errors are retried
    only for idempotent methods: urllib3's default `allowed_methods` excludes
    POST. Other errors, such as an SSL error, are never retried, because
    urllib3 cannot tell whether the request was sent and would otherwise
    resend it for every method, POST included. HTTP statuses are not retried
    here; see `request_with_retry`.
    """
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        other=0,
        backoff_factor=0.5,
        status=0,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def parse_retry_delay(
    response: requests.Response,
    attempt: int,
    base_delay: float = BASE_DELAY_SECONDS,
    max_delay: float = MAX_DELAY_SECONDS,
) -> float:
    """Seconds to wait before retrying `response`, plus jitter.

    Honors `Retry-After` (seconds), then `X-RateLimit-Reset` (epoch seconds),
    then falls back to exponential backoff from `base_delay`. Never exceeds
    `max_delay` before jitter.
    """
    jitter = random.uniform(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(max_delay, max(0.0, float(retry_after))) + jitter
        except (ValueError, TypeError):
            pass

    reset_epoch = response.headers.get("X-RateLimit-Reset")
    if reset_epoch:
        try:
            return min(max_delay, max(0.0, float(reset_epoch) - time.time())) + jitter
        except (ValueError, TypeError):
            pass

    return min(max_delay, base_delay * (2**attempt)) + jitter


def is_retryable(response: requests.Response) -> bool:
    """True for rate-limit responses (429, or 403 with rate-limit signals) and
    for 500, 502, 503 and 504."""
    if response.status_code == 429:
        return True
    if response.status_code == 403:
        if "Retry-After" in response.headers:
            return True
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        return "rate limit" in response.text.lower()
    return response.status_code in (500, 502, 503, 504)


def request_with_retry(
    session: requests.Session,
    method: str,
    url: str,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY_SECONDS,
    max_delay: float = MAX_DELAY_SECONDS,
    retry_unsafe_methods: bool = False,
    **kwargs,
) -> requests.Response:
    """Sends a request, retrying rate-limit and server-error responses.

    By default only GET, HEAD and OPTIONS are resent. A 5xx to a POST can
    arrive after GitHub already acted on it, so resending could repeat the
    change. Pass `retry_unsafe_methods=True` only for requests that change
    nothing on the server, such as a POST that renders a preview page.

    Extra keyword arguments are passed to `session.request`.

    Returns the response when its status is below 400 (including an
    unfollowed 3xx). Raises `requests.HTTPError` otherwise.
    """
    may_resend = retry_unsafe_methods or method.upper() in _SAFE_METHODS
    for attempt in range(max(0, max_retries) + 1):
        response = session.request(method=method, url=url, **kwargs)
        if response.status_code < 400:
            return response
        if may_resend and attempt < max_retries and is_retryable(response):
            sleep_duration = parse_retry_delay(
                response, attempt, base_delay=base_delay, max_delay=max_delay
            )
            logging.warning(
                "Rate limit/transient error (HTTP [%s]) on [%s] [%s]. "
                "Retrying in [%.2fs] (attempt [%d]/[%d])...",
                response.status_code,
                method,
                url,
                sleep_duration,
                attempt + 1,
                max_retries,
            )
            time.sleep(sleep_duration)
            continue
        break
    response.raise_for_status()
    return response


# Raised while preparing a request, before any connection is made.
_PRE_SEND_ERRORS = (InvalidHeader, InvalidSchema, InvalidURL, MissingSchema)


def _never_sent(error: requests.RequestException) -> bool:
    """True if `error` proves the request never reached the server: the
    connection itself could not be established."""
    if isinstance(error, (requests.ConnectTimeout, *_PRE_SEND_ERRORS)):
        return True
    if isinstance(error, requests.ConnectionError) and error.args:
        cause = error.args[0]
        # NewConnectionError (refused, DNS failure) subclasses ConnectTimeoutError.
        return isinstance(cause, MaxRetryError) and isinstance(cause.reason, ConnectTimeoutError)
    return False


def submit_once(
    session: requests.Session, url: str, data, **kwargs
) -> requests.Response:
    """POSTs a state-changing form submission exactly once.

    Never resends. Returns any response with a status below 500, for the
    caller to judge. Extra keyword arguments are passed to `session.request`.

    Raises:
        OutcomeUnknownError: GitHub may have acted on the request: it answered
            with a 5xx, or the request failed after it may have been sent
            (read timeout, dropped connection).
        requests.RequestException: The request was never sent (the
            connection could not be established).
    """
    try:
        response = session.request(method="POST", url=url, data=data, **kwargs)
    except requests.RequestException as e:
        if _never_sent(e):
            raise
        raise OutcomeUnknownError(
            f"POST {url} may have reached GitHub, but no response arrived: {e!r}"
        ) from e
    if response.status_code >= 500:
        summary = response_summary(response)
        raise OutcomeUnknownError(
            f"POST {url} may have been applied; GitHub answered: {summary}", summary=summary
        )
    return response


# Matches a <template> element that contains no other <template>.
_INNERMOST_TEMPLATE = re.compile(
    r"<template\b[^>]*>(?:(?!<template\b).)*?</template\s*>", re.I | re.S
)
_TEMPLATE_PLACEHOLDER = "\x00github-nonpublic-api-template\x00"


def _strip_template_contents(html: str) -> str:
    """Empties every <template> element, innermost first so nesting works.

    html5lib 1.1 mis-parses a <form> inside a <template>: the inner </form>
    closes the *outer* form, so every control after it is dropped. GitHub's
    New GitHub App page has such a template, and the parsed form lost its
    submit button. Browsers parse template contents as a separate fragment
    and never submit them, so emptying templates first yields the form a
    browser sees.
    """
    previous = None
    while previous != html:
        previous = html
        html = _INNERMOST_TEMPLATE.sub(_TEMPLATE_PLACEHOLDER, html)
    return html.replace(_TEMPLATE_PLACEHOLDER, "<template></template>")


def parse_page(html: str):
    """Parses a GitHub page into an ElementTree the way a browser builds its
    forms (see `_strip_template_contents`)."""
    return html5lib.parse(_strip_template_contents(html), namespaceHTMLElements=False)


def find_form_by_action(doc, action_path: str, method: Optional[str] = None):
    """Returns the first form whose action path equals `action_path`, or None.

    The query string and a trailing slash are ignored. If `method` is set
    (for example "delete"), the form must also carry a Rails `_method` input
    with that value, case-insensitively.
    """
    for form in doc.findall(".//form"):
        if urlparse(form.attrib.get("action", "")).path.rstrip("/") != action_path.rstrip("/"):
            continue
        if method is not None:
            method_inputs = [
                i for i in form.findall(".//input") if i.attrib.get("name") == "_method"
            ]
            if not any(i.attrib.get("value", "").lower() == method.lower() for i in method_inputs):
                continue
        return form
    return None


_NON_SUBMITTED_INPUT_TYPES = frozenset({"button", "reset", "image"})


def _parent_map(doc) -> dict:
    return {child: parent for parent in doc.iter() for child in parent}


def browser_controls(doc, form) -> tuple[list, dict]:
    """The controls associated with `form`, in tree order, as a browser sees
    them, and a child -> parent map of `doc`.

    Includes controls elsewhere in the document with `form="<form id>"`, and
    excludes controls inside the form that name another form. <template>
    contents are not part of the document, so they are skipped.
    """
    form_id = form.attrib.get("id")
    parents = _parent_map(doc)
    out = []

    def owner(element):
        ref = element.attrib.get("form")
        if ref is not None:
            return form if form_id and ref == form_id else None
        node = parents.get(element)
        while node is not None and node.tag != "form":
            node = parents.get(node)
        return node

    def walk(element) -> None:
        for child in element:
            if child.tag == "template":
                continue
            if child.tag in ("input", "button", "select", "textarea") and owner(child) is form:
                out.append(child)
            walk(child)

    walk(doc)
    return out, parents


def _is_descendant(element, ancestor, parents) -> bool:
    node = element
    while node is not None:
        if node is ancestor:
            return True
        node = parents.get(node)
    return False


def _is_disabled(element, parents) -> bool:
    """True if `element` or a <fieldset disabled> ancestor disables it.

    Controls inside the first <legend> of a disabled fieldset stay enabled.
    """
    if "disabled" in element.attrib:
        return True
    node, child = parents.get(element), element
    while node is not None:
        if node.tag == "fieldset" and "disabled" in node.attrib:
            legends = [c for c in node if c.tag == "legend"]
            if not (legends and _is_descendant(child, legends[0], parents)):
                return True
        child, node = node, parents.get(node)
    return False


def is_submit_button(element) -> bool:
    """True for <button> of type submit (the default) and <input> of type
    submit or image."""
    kind = (element.attrib.get("type") or "").lower()
    return (element.tag == "button" and kind in ("", "submit")) or (
        element.tag == "input" and kind in ("submit", "image")
    )


def _option_value(option) -> str:
    if "value" in option.attrib:
        return option.attrib["value"]
    return " ".join("".join(option.itertext()).split())


def browser_form_data(
    doc,
    form,
    values: Optional[dict] = None,
    checked: Optional[dict] = None,
    submitter=None,
) -> list[tuple[str, str]]:
    """The entries a browser submits for `form`.

    Implements the HTML "constructing the entry list" algorithm, which is what
    `new FormData(form, submitter)` returns, for urlencoded forms.

    Simulated user edits are applied before serializing:

    *   `values`: name -> value for text-like inputs, hidden inputs, textareas
        and selects (every control of that name that isn't a checkbox or
        radio).
    *   `checked`: name -> bool for checkboxes, or name -> value to select a
        radio button.

    `submitter` defaults to the form's default button (its first submit
    button).

    Raises:
        PageShapeError: an edit names a control the form doesn't have.
    """
    values, checked = values or {}, checked or {}
    controls, parents = browser_controls(doc, form)
    if submitter is None:
        submitter = next((c for c in controls if is_submit_button(c)), None)
    entries: list[tuple[str, str]] = []
    for control in controls:
        if _is_disabled(control, parents):
            continue
        name = control.attrib.get("name")
        kind = (control.attrib.get("type") or "").lower()
        if is_submit_button(control) or control.tag == "button":
            if control is submitter and name and kind != "image":
                entries.append((name, control.attrib.get("value", "")))
            continue
        if not name:
            continue
        if control.tag == "input" and kind in _NON_SUBMITTED_INPUT_TYPES:
            continue
        if control.tag == "input" and kind == "file":
            # No file chosen: urlencoded forms send the (empty) file name.
            entries.append((name, ""))
            continue
        if control.tag == "input" and kind in ("checkbox", "radio"):
            value = control.attrib.get("value", "on")
            if name in checked:
                want = checked[name]
                is_checked = (value == want) if kind == "radio" else bool(want)
            else:
                is_checked = "checked" in control.attrib
            if is_checked:
                entries.append((name, value))
            continue
        if control.tag == "select":
            if name in values:
                entries.append((name, values[name]))
                continue
            options = [o for o in control.iter() if o.tag == "option"]
            selected = [o for o in options if "selected" in o.attrib]
            if not selected and "multiple" not in control.attrib and options:
                selected = [o for o in options if "disabled" not in o.attrib][:1]
            for option in selected:
                if "disabled" not in option.attrib:
                    entries.append((name, _option_value(option)))
            continue
        if control.tag == "textarea":
            entries.append((name, values.get(name, control.text or "")))
            continue
        entries.append((name, values.get(name, control.attrib.get("value", ""))))
    unknown = (set(values) | set(checked)) - {c.attrib.get("name") for c in controls}
    if unknown:
        raise PageShapeError(f"Form has no controls named {sorted(unknown)}")
    return entries


# Elements a browser does not render, so their error texts aren't visible.
_HIDDEN_CLASSES = frozenset({"d-none", "ajax-error-message", "js-flash-template"})


def _is_hidden(element) -> bool:
    classes = set((element.attrib.get("class") or "").split())
    return (
        element.tag in ("template", "script", "style")
        or (element.tag == "dialog" and "open" not in element.attrib)
        or "hidden" in element.attrib
        or element.attrib.get("aria-hidden") == "true"
        or bool(classes & _HIDDEN_CLASSES)
    )


def page_errors(html: str, limit: int = 5) -> list[str]:
    """The *visible* error texts GitHub rendered after a rejected form post.

    Every GitHub page ships hidden error markup, such as the ajax-error banner
    ("You can't perform that action at this time."), `js-flash-template`, and
    on the New GitHub App page `hidden` or `d-none` field notes. Reporting
    those would blame the wrong cause, so anything inside a hidden element is
    skipped. Flash banners (`flash-error`) are preferred; inline field errors
    are returned only when there is no flash banner. Each text is truncated
    to 300 characters.
    """
    doc = parse_page(html)
    flash: list[str] = []
    inline: list[str] = []

    def walk(element, hidden: bool) -> None:
        hidden = hidden or _is_hidden(element)
        if not hidden:
            classes = set((element.attrib.get("class") or "").split())
            if "flash-error" in classes:
                bucket = flash
            elif classes & {"error", "FormControl-inlineValidation"}:
                bucket = inline
            else:
                bucket = None
            if bucket is not None:
                text = " ".join("".join(element.itertext()).split())[:300]
                if text and text not in bucket:
                    bucket.append(text)
        for child in element:
            walk(child, hidden)

    walk(doc, False)
    return (flash or inline)[:limit]


def response_summary(response: requests.Response) -> str:
    """Status, URL, redirect chain and <title> of `response`, to diagnose an
    unexpected page without including its body."""
    chain = " -> ".join(
        f"{r.status_code} {r.headers.get('Location', '')}".strip() for r in response.history
    )
    title = ""
    try:
        node = parse_page(response.text).find(".//title")
        title = " ".join("".join(node.itertext()).split()) if node is not None else ""
    except Exception:  # Best effort: the summary must never mask the real error.
        pass
    redirects = f" after redirects [{chain}]" if chain else " (no redirect)"
    page_title = f"; page title {title!r}" if title else ""
    return f"HTTP {response.status_code} at {response.url}{redirects}{page_title}"


MAX_BODY_EXCERPT_BYTES = 4096
REDACTED = "[REDACTED]"

# Names whose value is a credential: Rails' authenticity_token, GitHub's
# per-form CSRF and timestamp secrets, and anything else named like a token,
# or a session identifier such as the data-session-id of GitHub's live-update
# socket link.
_SECRET_NAME = re.compile(r"token|secret|csrf|nonce|password|session", re.I)
# Inputs GitHub marks as holding a CSRF token by class or id instead of name.
_CSRF_MARKER = re.compile(r"csrf|authenticity", re.I)
# Shorter attribute values are flags such as data-csrf="true", not
# credentials; replacing them everywhere would blank out unrelated text.
_MIN_SECRET_ATTRIBUTE_LENGTH = 8
# Script contents: inline JS and JSON payloads such as the React app's
# embedded "csrf_tokens" map, whose tokens no name-based rule can find.
_SCRIPT_CONTENTS = re.compile(r"(<script\b[^>]*>).*?(?=</script\s*>|\Z)", re.I | re.S)
# A token-named key or attribute with its value, in JSON (plain,
# backslash-escaped or entity-quoted, as in a data-* attribute), JS, query
# strings, attributes such as csrf="..." and text such as "token: x". Keys
# start at a word boundary, which keeps matching linear on long runs of word
# characters.
_QUOTE = r"""(?:\\?["']|&quot;|&#0*34;|&#[xX]0*22;|&#0*39;|&#[xX]0*27;)?"""
_TOKEN_NAMED_VALUE = re.compile(
    r"(?<![\w.-])((?=[\w.-]*?(?:token|secret|csrf|nonce|password))"
    rf"[\w.-]+{_QUOTE}\s*[:=]\s*{_QUOTE})"
    r"""([^"'\\&\s<>,;:={}\[\]]+)""",
    re.I,
)
# The single-use `code` of a manifest flow redirect URL, which GitHub
# converts into the app's private key and secrets.
_CODE_PARAMETER = re.compile(r"""((?:[?&]|&amp;|&#0*38;|&#[xX]0*26;)code=)([^&"'\s<>#]+)""", re.I)
# A value signed by Rails' MessageVerifier ("<base64 data>--<hex digest>") or
# encrypted by its MessageEncryptor ("<data>--<iv>--<auth tag>"), wherever it
# appears and whatever it is called. GitHub signs, for example, the session
# parameter of its live-update socket URL and the data-channel of its
# notification indicator. The data often encodes user and session IDs, and
# the whole value authenticates to GitHub, so all of it is redacted. Base64
# characters may be percent-encoded, as in a query parameter. A match starts
# only where a run of base64 characters starts (after a character that is
# not part of one, or after a percent-encoded character other than "+" and
# "/"), and the standard base64 alphabet has no "-", so every "--" is a
# separator and matching stays linear.
_BASE64_RUN = r"(?:[A-Za-z0-9+/]|%2[BbFf])+(?:=|%3[Dd]){0,2}"
_RAILS_SIGNED_VALUE = re.compile(
    r"(?:(?<![A-Za-z0-9+/%])|(?<=%[0-9A-Fa-f]{2})(?<!%2[BbFf]))"
    rf"{_BASE64_RUN}--"
    rf"(?:[0-9a-fA-F]{{40,}}|{_BASE64_RUN}--[A-Za-z0-9+/]{{22}}(?:==|%3[Dd]%3[Dd]))"
    r"(?![A-Za-z0-9+/])"
)
# Where an excerpt starts: GitHub's first few KB are <head> and site header,
# which never explain a changed page.
_EXCERPT_START = (re.compile(r"<main[\s>]", re.I), re.compile(r"<body[\s>]", re.I))
_NAMED_CHARACTER_REFERENCES = {"&": "amp", "<": "lt", ">": "gt", '"': "quot", "'": "apos"}


def _secret_values(html: str) -> set[str]:
    """Values of credential-like controls, `<meta>` tags and attributes
    anywhere in `html`, including in <template> contents (which html5lib
    keeps as ordinary children)."""
    values = set()
    for element in html5lib.parse(html, namespaceHTMLElements=False).iter():
        if not isinstance(element.tag, str):
            continue  # Comments.
        attrib = element.attrib
        name = attrib.get("name") or ""
        marker = f"{attrib.get('class', '')} {attrib.get('id', '')}"
        if element.tag == "meta" and _SECRET_NAME.search(name):
            values.add(attrib.get("content") or "")
        elif _SECRET_NAME.search(name) or "data-csrf" in attrib or _CSRF_MARKER.search(marker):
            values.add(attrib.get("value") or "")
            if element.tag == "textarea":
                values.add(element.text or "")
        for attribute, value in attrib.items():
            if _SECRET_NAME.search(str(attribute)) and len(value) >= _MIN_SECRET_ATTRIBUTE_LENGTH:
                values.add(value)
    values.discard("")
    return values


def _raw_html_pattern(value: str) -> str:
    """A regex for `value` as it may appear in raw HTML: each character
    literal or as a decimal, hexadecimal or named character reference."""

    def character(c: str) -> str:
        code, hex_digits = ord(c), f"{ord(c):x}"
        options = [
            re.escape(c),
            f"&#0*{code};",
            f"&#[xX]0*(?:{hex_digits}|{hex_digits.upper()});",
        ]
        if c in _NAMED_CHARACTER_REFERENCES:
            options.append(f"&{_NAMED_CHARACTER_REFERENCES[c]};")
        return f"(?:{'|'.join(options)})"

    return "".join(character(c) for c in value)


def redacted_body_excerpt(html: str, limit: int = MAX_BODY_EXCERPT_BYTES) -> str:
    """At most `limit` UTF-8 bytes of `html` with credentials replaced by
    "[REDACTED]", for diagnosing a page that no longer has the expected shape.

    Redacted before truncating, so a token cut at the limit is not leaked:

    *   every occurrence, literal or as character references, of the value
        of an `authenticity_token`, CSRF, secret, session or other
        token-named control, `<meta>` tag or attribute;
    *   the contents of every `<script>`, where inline JSON and JS keep
        tokens;
    *   every value signed or encrypted by Rails (see `_RAILS_SIGNED_VALUE`),
        such as a live-update session or notification channel;
    *   values of token-named keys, query parameters and attributes, and the
        `code` query parameter of manifest redirect URLs.

    The excerpt starts at `<main>`, else at `<body>`, else at the start of
    `html`.

    Returns "" if `html` cannot be scanned, rather than risk an unredacted
    excerpt.
    """
    try:
        secrets = sorted(_secret_values(html), key=len, reverse=True)
        redacted = _SCRIPT_CONTENTS.sub(lambda m: m.group(1) + REDACTED, html)
        if secrets:
            redacted = re.sub("|".join(map(_raw_html_pattern, secrets)), REDACTED, redacted)
        # Before the token-named rule, which stops at "=" and would leave the
        # padding and digest of a token-named signed value behind.
        redacted = _RAILS_SIGNED_VALUE.sub(REDACTED, redacted)
        for pattern in (_TOKEN_NAMED_VALUE, _CODE_PARAMETER):
            redacted = pattern.sub(lambda m: m.group(1) + REDACTED, redacted)
    except Exception:  # Fail closed: never return what could not be redacted.
        return ""
    start = next((m.start() for p in _EXCERPT_START for m in [p.search(redacted)] if m), 0)
    return redacted[start:].encode("utf-8")[: max(0, limit)].decode("utf-8", "ignore")


# Values that describe_forms may show verbatim: short words and flags only.
_SAFE_DESCRIBE_VALUE = re.compile(
    r"^(|[01]|true|false|none|read|write|admin|[a-z][a-z_ ]{0,31})$", re.I
)


def _describe_value(value: str) -> str:
    return value if _SAFE_DESCRIBE_VALUE.fullmatch(value) else f"<redacted len={len(value)}>"


def describe_document_forms(doc) -> list[dict]:
    """Describes each form in a parsed page, with token-like values redacted.

    For every form: action, method, id, class, its controls in tree order and
    the names `browser_form_data` would submit. For every control: tag, type,
    name, value (harmless literals only; `authenticity_token` and anything
    token-like is redacted), the checked/disabled/required/form attributes,
    select options (first 10), and flags for a <template> or hidden ancestor.
    Unnamed `type="button"` buttons are counted instead of listed.

    Use this to compare a live page with saved fixtures, or to diagnose a
    form that no longer matches. Pass the output of `parse_page`; for a page
    fetched with GET, `describe_forms` does both steps.
    """
    parents = _parent_map(doc)

    def flags(element) -> list[str]:
        out, node = [], parents.get(element)
        while node is not None:
            if node.tag == "template":
                out.append("in-template")
            elif _is_hidden(node):
                cls = " ".join((node.attrib.get("class") or "").split()[:2])
                out.append(f"hidden-ancestor:{node.tag}.{cls}".rstrip("."))
            node = parents.get(node)
        return out

    forms = []
    for form in doc.findall(".//form"):
        controls = []
        unnamed_buttons = 0
        for e in form.iter():
            if e.tag not in ("input", "button", "select", "textarea"):
                continue
            name = e.attrib.get("name", "")
            if e.tag == "button" and not name and (e.attrib.get("type") or "").lower() == "button":
                unnamed_buttons += 1
                continue
            value = e.attrib.get("value", "") if e.tag != "textarea" else (e.text or "")
            entry = {
                "tag": e.tag,
                "type": e.attrib.get("type", ""),
                "name": name,
                "value": "<redacted>" if name == "authenticity_token" else _describe_value(value),
            }
            for attr in ("checked", "disabled", "required", "form"):
                if attr in e.attrib:
                    entry[attr] = e.attrib[attr] or True
            if e.tag == "select":
                entry["options"] = [
                    (_describe_value(_option_value(o)), "selected" in o.attrib)
                    for o in e.iter()
                    if o.tag == "option"
                ][:10]
            entry["flags"] = flags(e)
            controls.append(entry)
        try:
            submitted = [k for k, _ in browser_form_data(doc, form)]
        except ValueError as err:
            submitted = [f"error: {err}"]
        forms.append(
            {
                "action": form.attrib.get("action", ""),
                "method": form.attrib.get("method", ""),
                "id": form.attrib.get("id", ""),
                "class": form.attrib.get("class", ""),
                "controls": controls,
                "unnamed_type_button": unnamed_buttons,
                "browser_submits": submitted,
            }
        )
    return forms


def describe_forms(
    session: requests.Session, url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS
) -> list[dict]:
    """GETs `url` and describes its forms; see `describe_document_forms`."""
    response = request_with_retry(session, "GET", url, timeout=timeout)
    return describe_document_forms(parse_page(response.text))
