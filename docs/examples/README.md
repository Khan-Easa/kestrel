# Kestrel integration examples

Runnable examples showing Kestrel as the **code-execution tool** for an LLM
agent. Each script gives a model a single `run_python(code)` tool whose body
runs the code in a Kestrel sandbox (via the [`kestrel-client`](../../clients/python)
SDK) and feeds the output back — the core "agent that can safely run code" loop.

| Example | Framework | Streaming model API |
|---|---|---|
| [`openai_function_calling.py`](openai_function_calling.py) | OpenAI Python SDK | function/tool calling |
| [`anthropic_tool_use.py`](anthropic_tool_use.py) | Anthropic Python SDK | Claude tool use |
| [`langchain_tool.py`](langchain_tool.py) | LangChain | `@tool` + `bind_tools` |

## The shared pattern

All three are the same loop:

1. Describe a `run_python` tool to the model (name, description, a `code` string parameter).
2. Send the user's question.
3. If the model asks to call the tool, run the `code` in Kestrel and return its stdout/stderr.
4. Loop until the model returns a final text answer.

The only thing Kestrel-specific is step 3 — one call to `KestrelClient.execute(code)`.
Everything else is ordinary framework usage, so you can lift the pattern into your
own agent.

## Running them

First, have a Kestrel server running and an API key minted. The quickest way is the
one-command stack from the repo root:

```bash
docker build -t kestrel-runtime:0.5.0 docker/executor/   # once
docker compose up -d --build
docker compose exec api kestrel-keys create examples --scope admin   # prints a token
```

Then install the example dependencies and set your keys:

```bash
cd docs/examples
pip install -r requirements.txt

export KESTREL_URL=http://localhost:8000
export KESTREL_API_KEY=kestrel_...        # the token printed above
export OPENAI_API_KEY=sk-...              # for the OpenAI + LangChain examples
export ANTHROPIC_API_KEY=sk-ant-...       # for the Anthropic example

python openai_function_calling.py "What is the 20th Fibonacci number?"
python anthropic_tool_use.py "Sum the first 100 primes."
python langchain_tool.py "How many days between 2000-01-01 and 2030-06-01?"
```

Each script takes the question as command-line arguments (and falls back to a
default if none are given). The model id is a `MODEL = "..."` constant at the top
of each file — swap it for whichever model you prefer.

## Note on safety

These examples run **model-generated code**. That is exactly what Kestrel is for:
the code executes in a network-isolated, resource-capped, throwaway container, not
on your machine. Never run untrusted model output through a bare `exec()` or
`subprocess` — route it through Kestrel (or an equivalent sandbox).
