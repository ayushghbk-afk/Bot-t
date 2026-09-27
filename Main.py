#!/usr/bin/env python3

import os
import re
import json
import time
import shlex
import signal
import subprocess
from pathlib import Path

import requests


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    "YOUR_NEW_TELEGRAM_BOT_TOKEN"
)

AI_PROXY = "https://groq-proxy.mr-hackerdon808.workers.dev/"
MODEL = "openai/gpt-oss-120b"

BASE_DIR = Path.home()

DATA_DIR = BASE_DIR / "bot_data"
USERS_DIR = DATA_DIR / "users"
GROUPS_DIR = DATA_DIR / "groups"
TASKS_DIR = DATA_DIR / "tasks"

WORKSPACE = BASE_DIR / "telegram_agent_workspace"

LOCK_FILE = DATA_DIR / "telegram_bot.lock"

MAX_AGENT_STEPS = 12
REQUEST_TIMEOUT = 90
COMMAND_TIMEOUT = 60

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


# ============================================================
# DIRECTORIES
# ============================================================

for directory in [
    DATA_DIR,
    USERS_DIR,
    GROUPS_DIR,
    TASKS_DIR,
    WORKSPACE,
]:
    directory.mkdir(parents=True, exist_ok=True)


# ============================================================
# SINGLE INSTANCE LOCK
# ============================================================

def acquire_lock():
    if LOCK_FILE.exists():
        try:
            old_pid = LOCK_FILE.read_text().strip()

            if old_pid:
                try:
                    os.kill(int(old_pid), 0)

                    print(
                        f"\nAnother bot instance is already running "
                        f"(PID {old_pid})."
                    )
                    print("Stop it before starting another instance.\n")
                    return False

                except ProcessLookupError:
                    pass
                except ValueError:
                    pass
                except PermissionError:
                    print("Cannot inspect existing bot process.")
                    return False

        except Exception:
            pass

    try:
        LOCK_FILE.write_text(str(os.getpid()))
        return True

    except Exception as e:
        print("Could not create lock:", e)
        return False


def release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


def cleanup(*args):
    release_lock()
    raise SystemExit


signal.signal(signal.SIGINT, cleanup)
signal.signal(signal.SIGTERM, cleanup)


# ============================================================
# TELEGRAM
# ============================================================

def telegram(method, payload=None):
    url = f"{TELEGRAM_API}/{method}"

    try:
        response = requests.post(
            url,
            json=payload or {},
            timeout=REQUEST_TIMEOUT,
        )

        try:
            data = response.json()
        except Exception:
            data = {
                "ok": False,
                "description": response.text[:1000],
            }

        return response.status_code, data

    except requests.RequestException as e:
        print("Telegram request error:", e)

        return 0, {
            "ok": False,
            "description": str(e),
        }


def send_message(chat_id, text, reply_to=None):
    if not text:
        return

    # Telegram max message size is around 4096 characters.
    chunks = []

    while text:
        chunks.append(text[:4000])
        text = text[4000:]

    for chunk in chunks:

        payload = {
            "chat_id": chat_id,
            "text": chunk,
        }

        if reply_to:
            payload["reply_to_message_id"] = reply_to

        status, data = telegram("sendMessage", payload)

        if status != 200 or not data.get("ok"):
            print("sendMessage error:", status, data)


def typing(chat_id):
    telegram(
        "sendChatAction",
        {
            "chat_id": chat_id,
            "action": "typing",
        },
    )


# ============================================================
# JSON HELPERS
# ============================================================

def load_json(path, default):
    try:
        if not path.exists():
            return default

        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    except Exception:
        return default


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)

    temp = path.with_suffix(path.suffix + ".tmp")

    with temp.open("w", encoding="utf-8") as f:
        json.dump(
            data,
            f,
            indent=2,
            ensure_ascii=False,
        )

    temp.replace(path)


# ============================================================
# USER MEMORY
# ============================================================

def user_memory_path(user_id):
    return USERS_DIR / f"{user_id}.json"


def get_user_memory(user_id):
    return load_json(
        user_memory_path(user_id),
        {
            "user_id": user_id,
            "messages": [],
            "facts": [],
        },
    )


def save_user_memory(user_id, memory):
    save_json(
        user_memory_path(user_id),
        memory,
    )


def remember_user_message(user_id, role, content):
    memory = get_user_memory(user_id)

    memory.setdefault("messages", []).append(
        {
            "role": role,
            "content": content,
            "time": int(time.time()),
        }
    )

    # Keep memory manageable.
    memory["messages"] = memory["messages"][-30:]

    save_user_memory(
        user_id,
        memory,
    )


# ============================================================
# GROUP MEMORY
# ============================================================

def group_memory_path(group_id):
    safe_id = str(group_id).replace("-", "minus")

    return GROUPS_DIR / f"{safe_id}.json"


def get_group_memory(group_id):
    return load_json(
        group_memory_path(group_id),
        {
            "group_id": group_id,
            "messages": [],
            "members_seen": {},
        },
    )


def save_group_memory(group_id, memory):
    save_json(
        group_memory_path(group_id),
        memory,
    )


def remember_group_message(
    group_id,
    user_id,
    username,
    content,
):
    memory = get_group_memory(group_id)

    memory.setdefault("messages", []).append(
        {
            "user_id": user_id,
            "username": username,
            "content": content,
            "time": int(time.time()),
        }
    )

    memory["messages"] = memory["messages"][-50:]

    memory.setdefault("members_seen", {})[
        str(user_id)
    ] = {
        "username": username,
        "last_seen": int(time.time()),
    }

    save_group_memory(
        group_id,
        memory,
    )


# ============================================================
# TASK STATE
# ============================================================

def task_path(chat_id):
    safe_id = str(chat_id).replace("-", "minus")

    return TASKS_DIR / f"{safe_id}.json"


def get_task(chat_id):
    return load_json(
        task_path(chat_id),
        {
            "pending_confirmation": None,
            "history": [],
        },
    )


def save_task(chat_id, task):
    save_json(
        task_path(chat_id),
        task,
    )


def clear_pending(chat_id):
    task = get_task(chat_id)
    task["pending_confirmation"] = None
    save_task(chat_id, task)


def add_task_history(chat_id, role, content):
    task = get_task(chat_id)

    task.setdefault("history", []).append(
        {
            "role": role,
            "content": content,
            "time": int(time.time()),
        }
    )

    task["history"] = task["history"][-40:]

    save_task(chat_id, task)


# ============================================================
# WORKSPACE SECURITY
# ============================================================

def workspace_root():
    return WORKSPACE.resolve()


def safe_workspace_path(user_path):
    root = workspace_root()

    if not user_path:
        user_path = "."

    candidate = Path(user_path)

    if not candidate.is_absolute():
        candidate = root / candidate

    candidate = candidate.resolve()

    try:
        candidate.relative_to(root)
    except ValueError:
        raise PermissionError(
            "Access denied: path is outside the agent workspace."
        )

    return candidate


def relative_workspace_path(path):
    root = workspace_root()

    try:
        return str(path.resolve().relative_to(root))
    except ValueError:
        return str(path)


# ============================================================
# TOOL: LIST FILES
# ============================================================

def tool_list_files(path="."):
    target = safe_workspace_path(path)

    if not target.exists():
        return f"Path does not exist: {path}"

    if not target.is_dir():
        return f"Not a directory: {path}"

    entries = []

    try:
        for item in sorted(
            target.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        ):
            prefix = "[DIR]" if item.is_dir() else "[FILE]"

            entries.append(
                f"{prefix} {relative_workspace_path(item)}"
            )

    except Exception as e:
        return f"Could not list directory: {e}"

    if not entries:
        return "(empty directory)"

    return "\n".join(entries)


# ============================================================
# TOOL: READ FILE
# ============================================================

def tool_read_file(path):
    target = safe_workspace_path(path)

    if not target.exists():
        return f"File does not exist: {path}"

    if target.is_dir():
        return f"Path is a directory: {path}"

    try:
        size = target.stat().st_size

        if size > 500_000:
            return (
                f"File is too large to read directly "
                f"({size} bytes)."
            )

        return target.read_text(
            encoding="utf-8",
            errors="replace",
        )

    except Exception as e:
        return f"Could not read file: {e}"


# ============================================================
# TOOL: WRITE FILE
# ============================================================

def tool_write_file(path, content):
    target = safe_workspace_path(path)

    target.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        target.write_text(
            content,
            encoding="utf-8",
        )

        return (
            f"Successfully wrote "
            f"{relative_workspace_path(target)} "
            f"({len(content)} characters)."
        )

    except Exception as e:
        return f"Could not write file: {e}"


# ============================================================
# DANGEROUS COMMAND CHECK
# ============================================================

DANGEROUS_COMMAND_PATTERNS = [
    r"\brm\s+-rf\b",
    r"\brm\s+-r\b",
    r"\bmkfs\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bkillall\b",
    r"\bpkill\b",
    r"\bsudo\b",
    r"\bsu\b",
    r"\bcurl\b.*\|\s*(sh|bash)",
    r"\bwget\b.*\|\s*(sh|bash)",
    r"\bchmod\s+777\b",
    r"\bchown\b",
    r"\biptables\b",
    r"\btermux-reset\b",
]

DANGEROUS_REGEX = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in DANGEROUS_COMMAND_PATTERNS
]


def command_needs_confirmation(command):
    for pattern in DANGEROUS_REGEX:
        if pattern.search(command):
            return True

    return False


# ============================================================
# TOOL: TERMINAL
# ============================================================

def tool_terminal_exec(
    command,
    timeout=COMMAND_TIMEOUT,
):
    if not command:
        return "No command provided."

    if command_needs_confirmation(command):
        return (
            "COMMAND_REQUIRES_CONFIRMATION\n"
            f"{command}"
        )

    try:
        timeout = int(timeout)
    except Exception:
        timeout = COMMAND_TIMEOUT

    timeout = max(
        1,
        min(timeout, 300),
    )

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(WORKSPACE),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )

        output = []

        output.append(
            f"Exit code: {result.returncode}"
        )

        if result.stdout:
            output.append(
                "STDOUT:\n" +
                result.stdout[-12000:]
            )

        if result.stderr:
            output.append(
                "STDERR:\n" +
                result.stderr[-12000:]
            )

        return "\n\n".join(output)

    except subprocess.TimeoutExpired:
        return (
            f"Command timed out after {timeout} seconds."
        )

    except Exception as e:
        return f"Command failed: {e}"


# ============================================================
# TOOL DISPATCH
# ============================================================

def execute_tool(tool_name, arguments):
    arguments = arguments or {}

    if tool_name == "list_files":
        return tool_list_files(
            arguments.get("path", ".")
        )

    if tool_name == "read_file":
        return tool_read_file(
            arguments.get("path", "")
        )

    if tool_name == "write_file":
        return tool_write_file(
            arguments.get("path", ""),
            arguments.get("content", ""),
        )

    if tool_name == "terminal_exec":
        return tool_terminal_exec(
            arguments.get("command", ""),
            arguments.get(
                "timeout",
                COMMAND_TIMEOUT,
            ),
        )

    return f"Unknown tool: {tool_name}"


# ============================================================
# AI PROMPT
# ============================================================

AGENT_SYSTEM_PROMPT = r"""
You are a Telegram coding assistant and workspace agent.

You help the user inspect and modify files inside the dedicated workspace.

IMPORTANT INTENT RULES:
- Simple greetings such as "hi", "hello", "hey", "good morning", "thanks", or similar casual conversation MUST NEVER use a tool.
- For casual conversation, answer normally in plain text.
- Only use a tool when the user's current request explicitly or clearly requires file inspection, file creation/editing, directory listing, or command execution.
- Do not continue an old tool task just because it appears in conversation history. Follow the user's CURRENT request.
- Never create a file merely because a previous request mentioned one.

AVAILABLE TOOLS:

1. list_files
Arguments:
{"path":"."}

2. read_file
Arguments:
{"path":"filename"}

3. write_file
Arguments:
{
  "path":"filename",
  "content":"file contents"
}

4. terminal_exec
Arguments:
{
  "command":"python filename.py",
  "timeout":60
}

CRITICAL TOOL FORMAT:

When you need a tool, ALWAYS return EXACTLY one JSON object.

CORRECT:
{"tool":"list_files","arguments":{"path":"."}}

CORRECT:
{"tool":"read_file","arguments":{"path":"main.py"}}

CORRECT:
{"tool":"write_file","arguments":{"path":"main.py","content":"print('hello')"}}

CORRECT:
{"tool":"terminal_exec","arguments":{"command":"python main.py","timeout":60}}

WRONG:
{"path":"."}

WRONG:
{"path":"main.py"}

WRONG:
{"command":"python main.py"}

Never return only tool arguments.

Never write:
TOOL: list_files

Never wrap tool JSON in markdown.

Never return multiple tool calls in one response.

If you do not need a tool, respond normally in plain text.

After receiving a TOOL RESULT, decide what to do next.

If the task is complete, provide a concise final answer.

IMPORTANT:

- Work only inside the supplied workspace.
- Do not invent files or tool results.
- Do not claim a command succeeded unless its result says it succeeded.
- If you need information about a file, use read_file.
- If you need to see directory contents, use list_files.
- If you need to create/change a file, use write_file.
- If you need to run something, use terminal_exec.
"""


# ============================================================
# AI RESPONSE EXTRACTION
# ============================================================

def extract_ai_text(data):
    """
    Extract text from multiple common OpenAI/proxy response formats.
    """

    if data is None:
        return None

    # Direct string
    if isinstance(data, str):
        return data.strip() or None

    if not isinstance(data, dict):
        return None

    # --------------------------------------------------------
    # 1. output_text
    # --------------------------------------------------------

    value = data.get("output_text")

    if isinstance(value, str) and value.strip():
        return value.strip()

    # --------------------------------------------------------
    # 2. Responses API:
    #
    # {
    #   "output": [
    #      {
    #        "content": [
    #           {
    #             "type": "output_text",
    #             "text": "Hello"
    #           }
    #        ]
    #      }
    #   ]
    # }
    # --------------------------------------------------------

    output = data.get("output")

    if isinstance(output, list):

        collected = []

        for item in output:

            if not isinstance(item, dict):
                continue

            # Some proxies put text directly here.
            text = item.get("text")

            if isinstance(text, str) and text.strip():
                collected.append(text.strip())

            content = item.get("content")

            if isinstance(content, list):

                for content_item in content:

                    if isinstance(content_item, dict):

                        text = content_item.get("text")

                        if isinstance(text, str) and text.strip():
                            collected.append(text.strip())

                        # Some APIs use output_text.
                        text = content_item.get(
                            "output_text"
                        )

                        if (
                            isinstance(text, str)
                            and text.strip()
                        ):
                            collected.append(
                                text.strip()
                            )

        if collected:
            return "\n".join(collected)

    # --------------------------------------------------------
    # 3. OpenAI chat completion format
    # --------------------------------------------------------

    choices = data.get("choices")

    if isinstance(choices, list):

        for choice in choices:

            if not isinstance(choice, dict):
                continue

            message = choice.get("message")

            if isinstance(message, dict):

                content = message.get("content")

                if isinstance(content, str):
                    if content.strip():
                        return content.strip()

                # Some providers return content as parts.
                if isinstance(content, list):

                    parts = []

                    for part in content:

                        if isinstance(part, dict):

                            text = part.get("text")

                            if (
                                isinstance(text, str)
                                and text.strip()
                            ):
                                parts.append(
                                    text.strip()
                                )

                    if parts:
                        return "\n".join(parts)

            text = choice.get("text")

            if isinstance(text, str) and text.strip():
                return text.strip()

    # --------------------------------------------------------
    # 4. Generic proxy formats
    # --------------------------------------------------------

    for key in [
        "content",
        "text",
        "response",
        "message",
        "answer",
    ]:

        value = data.get(key)

        if isinstance(value, str) and value.strip():
            return value.strip()

    return None


def is_commands_request(text):
    """
    Detect requests that should be answered locally
    instead of wasting an AI request.
    """

    normalized = text.lower().strip()

    command_requests = {
        "list all commands",
        "list commands",
        "show all commands",
        "show commands",
        "all commands",
        "commands",
        "help",
        "show help",
        "list me all commands",
        "what are the commands",
        "what commands can you use",
    }

    return normalized in command_requests
# ============================================================
# AI CALL
# ============================================================

def call_ai(messages):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    payload = {
        "model": MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 3000,
    }

    try:
        response = requests.post(
            AI_PROXY,
            headers=headers,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )

        print("AI HTTP:", response.status_code)

        try:
            data = response.json()
        except Exception:
            data = None

        if response.status_code == 200:
            text = extract_ai_text(data)

            if text:
                return text

            print("AI response had no readable text.")

        else:
            print(
                "AI error:",
                data if data is not None
                else response.text[:1000]
            )

    except requests.RequestException as e:
        print("AI request error:", e)

    # --------------------------------------------------------
    # COMPACT RETRY
    # --------------------------------------------------------

    print(
        "Retrying AI request with compact payload..."
    )

    try:
        compact_messages = []

        for msg in messages[-12:]:
            role = msg.get("role")
            content = msg.get("content")

            if isinstance(content, str):
                compact_messages.append(
                    {
                        "role": role,
                        "content": content[:6000],
                    }
                )

        compact_payload = {
            "model": MODEL,
            "messages": compact_messages,
            "temperature": 0.1,
            "max_tokens": 2000,
        }

        response = requests.post(
            AI_PROXY,
            headers=headers,
            json=compact_payload,
            timeout=REQUEST_TIMEOUT,
        )

        print(
            "AI retry HTTP:",
            response.status_code,
        )

        try:
            data = response.json()
        except Exception:
            data = None

        if response.status_code == 200:
            text = extract_ai_text(data)

            if text:
                return text

        else:
            print(
                "AI retry error:",
                data if data is not None
                else response.text[:1000]
            )

    except requests.RequestException as e:
        print(
            "AI retry request error:",
            e,
        )

    return None


# ============================================================
# TOOL CALL PARSER
# ============================================================

def extract_json_object(text):
    if not text:
        return None

    text = text.strip()

    # Direct JSON
    try:
        obj = json.loads(text)

        if isinstance(obj, dict):
            return obj

    except Exception:
        pass

    # Remove markdown fences
    cleaned = re.sub(
        r"```(?:json)?",
        "",
        text,
        flags=re.IGNORECASE,
    )

    cleaned = cleaned.replace(
        "```",
        "",
    ).strip()

    try:
        obj = json.loads(cleaned)

        if isinstance(obj, dict):
            return obj

    except Exception:
        pass

    # Find first {...}
    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start >= 0 and end > start:
        candidate = cleaned[start:end + 1]

        try:
            obj = json.loads(candidate)

            if isinstance(obj, dict):
                return obj

        except Exception:
            pass

    return None


def infer_shorthand_tool(obj):
    """
    Handles models that incorrectly return only arguments.

    Examples:

    {"path":"."}
        -> list_files

    {"path":"main.py"}
        -> read_file

    {"path":"main.py","content":"..."}
        -> write_file

    {"command":"python main.py"}
        -> terminal_exec
    """

    if not isinstance(obj, dict):
        return None

    # Already proper format
    if "tool" in obj:
        return None

    # Write file
    if (
        "path" in obj
        and "content" in obj
    ):
        return {
            "tool": "write_file",
            "arguments": {
                "path": obj.get("path"),
                "content": obj.get("content", ""),
            },
        }

    # Terminal
    if "command" in obj:
        return {
            "tool": "terminal_exec",
            "arguments": {
                "command": obj.get("command"),
                "timeout": obj.get(
                    "timeout",
                    COMMAND_TIMEOUT,
                ),
            },
        }

    # Path-only shorthand
    if "path" in obj:
        path = str(obj.get("path", "."))

        # "." / directory-like paths are probably list requests.
        if (
            path in ("", ".")
            or path.endswith("/")
        ):
            return {
                "tool": "list_files",
                "arguments": {
                    "path": path or "."
                },
            }

        # Check workspace to distinguish directory/file.
        try:
            target = safe_workspace_path(path)

            if target.exists() and target.is_dir():
                return {
                    "tool": "list_files",
                    "arguments": {
                        "path": path
                    },
                }

        except Exception:
            pass

        # Otherwise assume file read.
        return {
            "tool": "read_file",
            "arguments": {
                "path": path
            },
        }

    return None


def extract_tool_call(text):
    obj = extract_json_object(text)

    if not obj:
        return None

    # Proper tool object
    if isinstance(obj.get("tool"), str):
        return {
            "tool": obj["tool"],
            "arguments": obj.get(
                "arguments",
                {},
            ),
        }

    # Shorthand recovery
    return infer_shorthand_tool(obj)


# ============================================================
# AGENT MESSAGES
# ============================================================

def build_agent_messages(
    user_text,
    user_info,
    chat_type,
    history,
    tool_results,
):
    messages = [
        {
            "role": "system",
            "content": AGENT_SYSTEM_PROMPT,
        }
    ]

    # Recent conversation
    for item in history[-12:]:
        role = item.get("role")
        content = item.get("content")

        if role in ("user", "assistant") and content:
            messages.append(
                {
                    "role": role,
                    "content": content,
                }
            )

    # Tool interaction history
    for item in tool_results:
        messages.append(
            {
                "role": "assistant",
                "content": (
                    "ASSISTANT TOOL CALL:\n" +
                    json.dumps(
                        item["tool_call"],
                        ensure_ascii=False,
                    )
                ),
            }
        )

        messages.append(
            {
                "role": "user",
                "content": (
                    "TOOL RESULT:\n" +
                    item["result"]
                ),
            }
        )

    identity = (
        f"User: {user_info.get('name', 'Unknown')}\n"
        f"User ID: {user_info.get('id')}\n"
        f"Chat type: {chat_type}\n"
    )

    messages.append(
        {
            "role": "user",
            "content": (
                identity +
                "\nCurrent request:\n" +
                user_text
            ),
        }
    )

    return messages


# ============================================================
# CASUAL MESSAGE GUARD
# ============================================================

def is_casual_message(text):
    """Return True for messages that should never trigger workspace tools."""
    if not text:
        return False

    normalized = re.sub(r"\\s+", " ", text.lower().strip())
    casual = {
        "hi", "hello", "hey", "hiya", "yo",
        "good morning", "good afternoon", "good evening",
        "thanks", "thank you", "thx", "ok", "okay",
        "nice", "cool", "great",
    }
    return normalized in casual


# ============================================================
# AGENT RUNNER
# ============================================================

def run_agent(
    chat_id,
    user_text,
    user_info,
    chat_type="private",
):
    # Never allow casual greetings to enter the coding/tool loop.
    if is_casual_message(user_text):
        casual_messages = [
            {
                "role": "system",
                "content": (
                    "You are a friendly Telegram assistant. "
                    "Reply naturally and briefly. "
                    "Do not use tools, create files, inspect files, "
                    "or execute commands for casual conversation."
                ),
            },
            {
                "role": "user",
                "content": user_text,
            },
        ]
        response = call_ai(casual_messages)
        return response or "Hi! How can I help you?"

    task = get_task(chat_id)

    history = task.setdefault(
        "history",
        [],
    )

    tool_results = []

    current_request = user_text

    for step in range(1, MAX_AGENT_STEPS + 1):

        messages = build_agent_messages(
            current_request,
            user_info,
            chat_type,
            history,
            tool_results,
        )

        response = call_ai(messages)

        print()
        print(
            f"AGENT STEP {step}:"
        )
        print(
            response if response
            else "(no AI response)"
        )

        if not response:
            return (
                "⚠️ AI agent failed to generate "
                "a response. Please try again."
            )

        tool_call = extract_tool_call(response)

        # Defense-in-depth: a casual request must never execute a tool,
        # even if the model accidentally emits tool JSON.
        if is_casual_message(user_text):
            return response if not tool_call else (
                "Hi! How can I help you?"
            )

        if not tool_call:
            return response

        tool_name = tool_call.get("tool")
        arguments = tool_call.get(
            "arguments",
            {},
        )

        if tool_name not in {
            "list_files",
            "read_file",
            "write_file",
            "terminal_exec",
        }:
            return (
                "⚠️ AI requested an unknown tool: "
                f"{tool_name}"
            )

        # ----------------------------------------------------
        # DANGEROUS COMMAND CONFIRMATION
        # ----------------------------------------------------

        if tool_name == "terminal_exec":
            command = str(
                arguments.get(
                    "command",
                    "",
                )
            )

            if command_needs_confirmation(command):

                task["pending_confirmation"] = {
                    "tool": tool_name,
                    "arguments": arguments,
                    "user_info": user_info,
                    "request": user_text,
                }

                save_task(
                    chat_id,
                    task,
                )

                return (
                    "⚠️ This command requires "
                    "confirmation:\n\n"
                    f"`{command}`\n\n"
                    "Use /confirm to run it.\n"
                    "Use /deny to cancel it."
                )

        # ----------------------------------------------------
        # EXECUTE TOOL
        # ----------------------------------------------------

        print(
            f"EXECUTING TOOL: {tool_name} "
            f"{json.dumps(arguments, ensure_ascii=False)}"
        )

        result = execute_tool(
            tool_name,
            arguments,
        )

        print()
        print("TOOL:", tool_name)
        print("RESULT:")
        print(result[:5000])

        tool_results.append(
            {
                "tool_call": tool_call,
                "result": result[:12000],
            }
        )

        # Tell the next model step what happened.
        current_request = (
            "Continue the task using the TOOL RESULT "
            "above. If another tool is needed, return "
            "the complete tool JSON object. Otherwise "
            "give the final answer."
        )

    return (
        "⚠️ Agent reached the maximum number of "
        f"steps ({MAX_AGENT_STEPS})."
    )


# ============================================================
# CONFIRMATION
# ============================================================

def confirm_pending(chat_id):
    task = get_task(chat_id)

    pending = task.get(
        "pending_confirmation"
    )

    if not pending:
        return (
            "There is no command waiting "
            "for confirmation."
        )

    tool_name = pending.get("tool")
    arguments = pending.get(
        "arguments",
        {},
    )

    result = execute_tool(
        tool_name,
        arguments,
    )

    task["pending_confirmation"] = None

    task.setdefault(
        "history",
        [],
    ).append(
        {
            "role": "assistant",
            "content": (
                f"Confirmed tool {tool_name}: "
                f"{result}"
            ),
            "time": int(time.time()),
        }
    )

    task["history"] = task["history"][-40:]

    save_task(
        chat_id,
        task,
    )

    return (
        "✅ Command executed.\n\n" +
        result
    )


def deny_pending(chat_id):
    task = get_task(chat_id)

    if not task.get(
        "pending_confirmation"
    ):
        return (
            "There is no command waiting "
            "for confirmation."
        )

    task["pending_confirmation"] = None

    save_task(
        chat_id,
        task,
    )

    return "❌ Pending command cancelled."


# ============================================================
# COMMANDS
# ============================================================

def commands_text():
    return """
🤖 Telegram AI Agent

Basic:
/start - Start the bot
/clear - Clear conversation history
/memory - Show memory status
/workspace - Show workspace path

Agent:
/agent on - Enable agent mode
/agent off - Disable agent mode
/agent status - Show agent status

Safety:
/confirm - Confirm dangerous command
/deny - Cancel dangerous command

Group:
/groupinfo - Show group information

In groups you can:
/ask <question>

Or mention the bot:
@Botfortst_bot your question
"""


# ============================================================
# BOT USERNAME
# ============================================================

BOT_USERNAME = None


def get_bot_username():
    global BOT_USERNAME

    if BOT_USERNAME:
        return BOT_USERNAME

    status, data = telegram(
        "getMe",
        {},
    )

    if status == 200 and data.get("ok"):
        BOT_USERNAME = data["result"].get(
            "username"
        )

    return BOT_USERNAME


# ============================================================
# PRIVATE MESSAGE HANDLER
# ============================================================

def handle_private_message(message):
    chat = message.get("chat", {})
    user = message.get("from", {})

    chat_id = chat.get("id")

    text = message.get("text", "").strip()

    if not text:
        return

    user_id = user.get("id", chat_id)

    first_name = user.get("first_name", "User")
    last_name = user.get("last_name", "")
    full_name = f"{first_name} {last_name}".strip()
    username = user.get("username", "")

    user_info = {
        "id": user_id,
        "name": full_name,
        "username": username,
    }

    print(f"\n[{chat_id}] {full_name}: {text}")

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    if text == "/start":
        send_message(
            chat_id,
            (
                f"👋 Hi {first_name}!\n\n"
                "I'm your Telegram AI agent.\n\n"
                + commands_text()
            ),
        )
        return

    if text in ("/help", "/commands") or is_commands_request(text):
        send_message(chat_id, commands_text())
        return

    if text in ("/clear", "/forgot"):
        task = get_task(chat_id)
        task["history"] = []
        task["pending_confirmation"] = None
        save_task(chat_id, task)

        memory = get_user_memory(user_id)
        memory["messages"] = []
        save_user_memory(user_id, memory)

        send_message(chat_id, "🧹 Conversation memory cleared.")
        return

    if text == "/memory":
        memory = get_user_memory(user_id)
        count = len(memory.get("messages", []))

        send_message(
            chat_id,
            (
                "🧠 Memory status\n\n"
                f"Stored messages: {count}"
            ),
        )
        return

    if text == "/workspace":
        send_message(
            chat_id,
            f"📁 Agent workspace:\n\n{WORKSPACE}",
        )
        return

    if text == "/agent on":
        task = get_task(chat_id)
        task["agent_enabled"] = True
        save_task(chat_id, task)

        send_message(chat_id, "🤖 Agent mode enabled.")
        return

    if text == "/agent off":
        task = get_task(chat_id)
        task["agent_enabled"] = False
        save_task(chat_id, task)

        send_message(chat_id, "🤖 Agent mode disabled.")
        return

    if text == "/agent status":
        task = get_task(chat_id)
        enabled = task.get("agent_enabled", True)
        pending = bool(task.get("pending_confirmation"))

        send_message(
            chat_id,
            (
                "🤖 Agent status\n\n"
                f"Enabled: {enabled}\n"
                f"Pending confirmation: {pending}\n"
                f"Workspace: {WORKSPACE}"
            ),
        )
        return

    if text == "/confirm":
        result = confirm_pending(chat_id)
        send_message(chat_id, result)
        return

    if text == "/deny":
        result = deny_pending(chat_id)
        send_message(chat_id, result)
        return

    # --------------------------------------------------------
    # NORMAL AI REQUEST
    # --------------------------------------------------------

    remember_user_message(user_id, "user", text)

    task = get_task(chat_id)

    if not task.get("agent_enabled", True):
        send_message(
            chat_id,
            "Agent mode is disabled. Use /agent on.",
        )
        return

    typing(chat_id)

    response = run_agent(
        chat_id=chat_id,
        user_text=text,
        user_info=user_info,
        chat_type="private",
    )

    remember_user_message(user_id, "assistant", response)

    add_task_history(chat_id, "user", text)
    add_task_history(chat_id, "assistant", response)

    send_message(chat_id, response)


# ============================================================
# GROUP MESSAGE HANDLER
# ============================================================

def handle_group_message(message):
    chat = message.get(
        "chat",
        {},
    )

    user = message.get(
        "from",
        {},
    )

    chat_id = chat.get("id")

    text = message.get(
        "text",
        "",
    ).strip()

    if not text:
        return

    user_id = user.get(
        "id"
    )

    first_name = user.get(
        "first_name",
        "User",
    )

    last_name = user.get(
        "last_name",
        "",
    )

    full_name = (
        f"{first_name} {last_name}"
    ).strip()

    username = user.get(
        "username",
        "",
    )

    # Track group activity
    remember_group_message(
        chat_id,
        user_id,
        username or full_name,
        text,
    )

    bot_username = get_bot_username()

    is_command = text.startswith("/")

    mentioned = False

    if bot_username:
        mentioned = (
            f"@{bot_username.lower()}"
            in text.lower()
        )

    reply = message.get(
        "reply_to_message"
    )

    replied_to_bot = False

    if reply:
        replied_from = reply.get(
            "from",
            {}
        )

        replied_username = (
            replied_from.get(
                "username"
            )
        )

        if (
            bot_username
            and replied_username
            and replied_username.lower()
            == bot_username.lower()
        ):
            replied_to_bot = True

    should_answer = (
        mentioned
        or replied_to_bot
        or text.startswith("/ask ")
    )

    if not should_answer:
        return

    # --------------------------------------------------------
    # GROUP COMMANDS
    # --------------------------------------------------------

    if text == "/groupinfo":
        memory = get_group_memory(
            chat_id
        )

        members_seen = len(
            memory.get(
                "members_seen",
                {},
            )
        )

        send_message(
            chat_id,
            (
                "👥 Group info\n\n"
                f"Chat ID: {chat_id}\n"
                f"Type: {chat.get('type')}\n"
                f"Members seen by bot: "
                f"{members_seen}"
            ),
        )
        return

    # --------------------------------------------------------
    # CLEAN QUESTION
    # --------------------------------------------------------

    question = text

    if bot_username:
        question = re.sub(
            rf"@{re.escape(bot_username)}",
            "",
            question,
            flags=re.IGNORECASE,
        ).strip()

    if question.startswith("/ask"):
        question = question[4:].strip()

    if not question:
        send_message(
            chat_id,
            "Yes? Ask me something.",
        )
        return

    user_info = {
        "id": user_id,
        "name": full_name,
        "username": username,
    }

    print(
        f"\n[{chat_id}] "
        f"{full_name}: {question}"
    )

    typing(chat_id)

    response = run_agent(
        chat_id=chat_id,
        user_text=question,
        user_info=user_info,
        chat_type="group",
    )

    send_message(
        chat_id,
        response,
        reply_to=message.get(
            "message_id"
        ),
    )


# ============================================================
# UPDATE HANDLER
# ============================================================

def handle_update(update):
    message = update.get(
        "message"
    )

    if not message:
        return

    chat = message.get(
        "chat",
        {},
    )

    chat_type = chat.get(
        "type"
    )

    if chat_type == "private":
        handle_private_message(
            message
        )

    elif chat_type in (
        "group",
        "supergroup",
    ):
        handle_group_message(
            message
        )


# ============================================================
# MAIN POLLING LOOP
# ============================================================

def main():
    if BOT_TOKEN == "YOUR_NEW_TELEGRAM_BOT_TOKEN":
        print(
            "\nERROR: Set your new bot token first."
        )
        print(
            "Example:"
        )
        print(
            'export TELEGRAM_BOT_TOKEN="YOUR_NEW_TOKEN"'
        )
        return

    if not acquire_lock():
        return

    print()
    print("=" * 60)
    print("       TELEGRAM TERMUX AI AGENT")
    print("=" * 60)
    print(
        f"Model: {MODEL}"
    )
    print(
        f"Proxy: {AI_PROXY}"
    )
    print(
        f"Workspace: {WORKSPACE}"
    )
    print(
        f"Task state: {TASKS_DIR}"
    )
    print()

    status, data = telegram(
        "getMe",
        {},
    )

    if status != 200 or not data.get("ok"):
        print(
            "Telegram bot authentication failed:"
        )
        print(data)
        release_lock()
        return

    bot = data["result"]

    global BOT_USERNAME

    BOT_USERNAME = bot.get(
        "username"
    )

    print(
        f"Bot: @{BOT_USERNAME}"
    )
    print(
        f"Workspace: {WORKSPACE}"
    )
    print()
    print(
        "Bot is running..."
    )
    print(
        "Press CTRL+C to stop."
    )
    print()

    offset = None

    while True:
        try:
            payload = {
                "timeout": 30,
                "allowed_updates": [
                    "message"
                ],
            }

            if offset is not None:
                payload["offset"] = offset

            status, data = telegram(
                "getUpdates",
                payload,
            )

            if status != 200:
                print(
                    "getUpdates HTTP:",
                    status,
                )

                time.sleep(3)
                continue

            if not data.get("ok"):
                print(
                    "getUpdates error:",
                    data,
                )

                # Telegram 409 means another polling
                # instance is active.
                if data.get("error_code") == 409:
                    print(
                        "Another getUpdates instance "
                        "appears to be active."
                    )

                    time.sleep(5)

                continue

            updates = data.get(
                "result",
                [],
            )

            for update in updates:
                update_id = update.get(
                    "update_id"
                )

                if update_id is not None:
                    offset = update_id + 1

                try:
                    handle_update(
                        update
                    )

                except Exception as e:
                    print(
                        "Update handler error:",
                        repr(e),
                    )

        except KeyboardInterrupt:
            break

        except Exception as e:
            print(
                "Polling error:",
                repr(e),
            )

            time.sleep(3)

    release_lock()

    print(
        "\nBot stopped."
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
