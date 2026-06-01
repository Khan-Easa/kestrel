"""Kestrel as a LangChain tool, driven by a tool-calling chat model.

    pip install -r requirements.txt
    export OPENAI_API_KEY=sk-...
    export KESTREL_URL=http://localhost:8000
    export KESTREL_API_KEY=kestrel_...
    python langchain_tool.py "What is the 20th Fibonacci number?"
"""

from __future__ import annotations

import os
import sys

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from kestrel_client import KestrelClient

MODEL = "gpt-4o"  # swap for your preferred tool-calling chat model

_kestrel = KestrelClient(
    os.environ.get("KESTREL_URL", "http://localhost:8000"),
    api_key=os.environ.get("KESTREL_API_KEY"),
)


@tool
def run_python(code: str) -> str:
    """Execute Python 3 code in a secure sandbox and return its stdout/stderr."""
    result = _kestrel.execute(code)
    return result.stdout or result.stderr or "(no output)"


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Compute the sum of the first 100 primes."
    model = ChatOpenAI(model=MODEL).bind_tools([run_python])

    messages = [
        SystemMessage("Use the run_python tool to compute answers; never guess at arithmetic."),
        HumanMessage(question),
    ]

    while True:
        ai_message = model.invoke(messages)
        messages.append(ai_message)

        if not ai_message.tool_calls:
            print(ai_message.content)
            return

        for call in ai_message.tool_calls:
            output = run_python.invoke(call["args"])
            messages.append(ToolMessage(output, tool_call_id=call["id"]))


if __name__ == "__main__":
    main()