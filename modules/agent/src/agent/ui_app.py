# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Serve Chainlit without the CLI's Python 3.14-incompatible loop patch."""

from pathlib import Path

from chainlit.utils import mount_chainlit
from fastapi import FastAPI

app = FastAPI()
mount_chainlit(app, target=str(Path(__file__).with_name("chainlit_ui.py")), path="/")
