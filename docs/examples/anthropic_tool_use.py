"""Kestrel as a code-execution tool for a Claude (Anthropic) tool-use agent.

    pip install -r requirements.txt
    export ANTHROPIC_API_KEY=sk-ant-...
    export KESTREL_URL=http://localhost:8000
    export KESTREL_API_KEY=kestrel_...
    python anthropic_tool_use.py "What is the 20th Fibonacci number?"
"""

from __future__ import annotations

import os
import sys

import anthropic

from kestrel_client import KestrelClient

MODEL = "claude-sonnet-4-6"  # swap for your preferred model

TOOLS = [
    {
        "name": "run_python",
        "description": "Execute Python 3 code in a secure sandbox and return its stdout/stderr.",
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The Python source to run. Use print() to return results.",
                }
            },
            "required": ["code"],
        },
    }
]

SYSTEM = (
    "You are a data assistant. Use the run_python tool to compute answers; "
    "never guess at arithmetic. print() whatever you want to read back."
)


def main() -> None:
    question = " ".join(sys.argv[1:]) or "Compute the sum of the first 100 primes."
    claude = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    kestrel = KestrelClient(
        os.environ.get("KESTREL_URL", "http://localhost:8000"),
        api_key=os.environ.get("KESTREL_API_KEY"),
    )

    messages = [{"role": "user", "content": question}]

    with kestrel:
        while True:
            response = claude.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                print(text)
                return

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    code = block.input["code"]
                    print(f"\n--- running ---\n{code}\n---------------")
                    result = kestrel.execute(code)
                    output = result.stdout or result.stderr or "(no output)"
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": output,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    main()