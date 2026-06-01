"""Kestrel as a code-execution tool for an OpenAI function-calling agent.

    pip install -r requirements.txt
    export OPENAI_API_KEY=sk-...
    export KESTREL_URL=http://localhost:8000
    export KESTREL_API_KEY=kestrel_...
    python openai_function_calling.py "What is the 20th Fibonacci number?"
"""

from __future__ import annotations

import json
import os
import sys

from openai import OpenAI

from kestrel_client import KestrelClient

MODEL = "gpt-4o"  # swap for your preferred model

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_python",
            "description": "Execute Python 3 code in a secure sandbox and return its stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The Python source to run. Use print() to return results.",
                    }
                },
                "required": ["code"],
            },
        },
    }
]

SYSTEM = (
    "You are a data assistant. Use the run_python tool to compute answers; "
    "never guess at arithmetic. print() whatever you want to read back."
)


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Compute the sum of the first 100 primes."
    openai_client = OpenAI()  # reads OPENAI_API_KEY
    kestrel = KestrelClient(
        os.environ.get("KESTREL_URL", "http://localhost:8000"),
        api_key=os.environ.get("KESTREL_API_KEY"),
    )

    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": question},
    ]

    with kestrel:
        while True:
            response = openai_client.chat.completions.create(
                model=MODEL, messages=messages, tools=TOOLS
            )
            message = response.choices[0].message
            messages.append(message)

            if not message.tool_calls:
                print(message.content)
                return

            for tool_call in message.tool_calls:
                code = json.loads(tool_call.function.arguments)["code"]
                print(f"\n--- running ---\n{code}\n---------------")
                result = kestrel.execute(code)
                output = result.stdout or result.stderr or "(no output)"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": output,
                    }
                )


if __name__ == "__main__":
    main()