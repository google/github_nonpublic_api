from github_nonpublic_api import api

import os
from truth.truth import AssertThat
import unittest
from unittest import mock

GITHUB_FORM_HTML = os.path.join(os.path.dirname(__file__), 'github_form.html')


class TestApi(unittest.TestCase):
    def test_get_and_submit_form(self):
        session = mock.MagicMock()
        with open(GITHUB_FORM_HTML) as fp:
            session.get.return_value.text = fp.read()

        def DataCallback(data):
            data['add'] = 'yes'

        api._get_and_submit_form(
            session=session, url='http://github.com', data_callback=DataCallback)

        AssertThat(session.post).WasCalled().Once().With(
            'http://github.com/foo', data=dict(add='yes', key='value'))
