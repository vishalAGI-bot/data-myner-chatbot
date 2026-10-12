"""Shared test set-up.

With the OpenAI provider every question is first read by OpenAI's understand_question tool. Tests that are about
what happens after that reading get a stand-in that returns the message as written with intent "analysis";
tests about the reading itself patch ``llm.call_tool`` with their own.
"""

import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def understood_as_written(user: str = "", **_) -> dict:
    message = re.search(r"Latest message: (.*)\Z", user, re.S)
    return {"rephrased_question": message.group(1).strip() if message else "", "intent": "analysis"}


@pytest.fixture(autouse=True)
def openai_reads_the_question_as_written():
    with patch("llm.call_tool", side_effect=understood_as_written):
        yield
