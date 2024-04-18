# GitHub NonPublic API

This project is developing a python library that can be used to access "NonPublic" parts of the GitHub API.  These are URL endpoints that are used as part of the UI, for which there is no Public API endpoint in the GitHub REST API or GitHub GraphQL API.

## Source Code Headers

Every file containing source code must include copyright and license
information. This includes any JS/CSS files that you might be serving out to
browsers. (This is to help well-intentioned people avoid accidental copying that
doesn't comply with the license.)

Apache header:

    Copyright 2022 Google LLC

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

        https://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

## For Developers

Create a new virtual environment and install the required tools.

```bash
python -m venv ~/{some envpath}/github_nonpublic

pip install --upgrade setuptools,build,pip

pip install -r requirements.txt
```

Testing:

```bash
python -m pytest
```

Linting:

```bash
ruff --output-format=github --select=E9,F63,F7,F82 --target-version=py37 .

ruff --output-format=github --target-version=py37 .
```

Adding license headers:

```bash
go run github.com/google/addlicense@v1.1.1 -c "The Authors (see AUTHORS file)" -l apache -v ./**/*.py
```
