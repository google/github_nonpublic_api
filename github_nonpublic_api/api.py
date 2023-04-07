from configobj import ConfigObj
import html5lib
import logging
import os.path
import pyotp
import requests

from urllib.parse import urljoin


def _get_and_submit_form(session, url, data_callback):
    response = session.get(url)
    response.raise_for_status()

    doc = html5lib.parse(response.text, namespaceHTMLElements=False)
    form = doc.findall('.//form')
    inputs = doc.findall('.//form//input')

    action_url = form[0].attrib['action']

    data = dict()
    for input in inputs:
        value = input.attrib.get('value')
        if value:
            data[input.attrib['name']] = value

    # Have the caller provide additional data
    data_callback(data)

    response = session.post(urljoin(url, action_url), data=data)
    response.raise_for_status()
    return response


def _create_login_session(username, password, tfa_callback, session=None):
    session = session or requests.Session()

    def _LoginCallback(data):
        data.update(dict(login=username, password=password))
    _get_and_submit_form(
        session=session, url='https://github.com/login', data_callback=_LoginCallback)

    def _TfaCallback(data):
        data.update(dict(otp=tfa_callback()))
    _get_and_submit_form(
        session=session, url='https://github.com/sessions/two-factor', data_callback=_TfaCallback)

    return session


class Api(object):
    def __init__(self, username, password, tfa_callback, session=None):
        self._session = _create_login_session(
            username=username, password=password, tfa_callback=tfa_callback, session=session)


config = ConfigObj(os.path.expanduser('~/github.ini'), _inspec=True)

api = Api(config['username'], config['password'],
          tfa_callback=lambda: pyotp.TOTP(config['otp_seed']).now())
resp = api._session.get('https://github.com/billnapier/aoc2021/settings')
resp.raise_for_status()
