from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime

TARGET = Path('src/api/openai_routes.py')
MARK = 'CATGPT_TOOLCALL_LOOP_FIX_V2'


def fail(msg):
    print(f'[ERROR] {msg}')
    raise SystemExit(1)


if not TARGET.exists():
    fail('请把本脚本放到 CatGPT 源码项目根目录。')

src = TARGET.read_text(encoding='utf-8')
if MARK in src:
    print('[OK] V2 补丁已经应用。')
    raise SystemExit(0)

backup = TARGET.with_name(TARGET.name + f'.backup-{datetime.now():%Y%m%d-%H%M%S}')
shutil.copy2(TARGET, backup)
print(f'[OK] 已备份原文件: {backup}')

# 1. 保留完整的“最后一个 user 回合”上下文，包括 assistant.tool_calls 和 tool results。
old = '''def _latest_turn_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Keep system prompts plus the latest user turn and its tool results.

    ChatGPT already has prior turns when we stay on a thread, so resending the
    client's full history only bloats the composer.
    """
    if not messages:
        return []
    systems = [message for message in messages if message.role == "system"]
    latest: list[ChatMessage] = []
    for message in reversed(messages):
        if message.role in {"user", "tool"}:
            latest.insert(0, message)
            if message.role == "user":
                break
    if not latest:
        non_system = [message for message in messages if message.role != "system"]
        latest = non_system[-1:]
    return systems + latest
'''
new = '''def _latest_turn_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Keep system prompts plus the complete latest user turn.

    For tool-calling follow-ups, the assistant tool-call message is part of the
    same logical turn as the following tool result. Dropping it makes the next
    request look like a fresh user request and can cause the model to repeat the
    exact same tool call.
    """
    if not messages:
        return []
    systems = [message for message in messages if message.role == "system"]
    last_user_index = -1
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            last_user_index = index
            break

    if last_user_index >= 0:
        latest = [message for message in messages[last_user_index:] if message.role != "system"]
        return systems + latest

    non_system = [message for message in messages if message.role != "system"]
    return systems + non_system[-1:]
'''
if old not in src:
    fail('找不到 _latest_turn_messages()，源码版本可能已改变。')
src = src.replace(old, new, 1)

# 2. Tool prompt：识别已经存在的 tool result，明确切换到 follow-up/final-answer 模式。
old_sig = '''def _build_tool_system_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None = None,
) -> str:'''
new_sig = '''def _build_tool_system_prompt(
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None = None,
    *,
    has_tool_results: bool = False,
    executed_tool_calls: list[str] | None = None,
) -> str:'''
if old_sig not in src:
    fail('找不到 _build_tool_system_prompt() 签名。')
src = src.replace(old_sig, new_sig, 1)

needle = '''    elif isinstance(tool_choice, dict):
        selected = tool_choice.get("function")
        selected_name = str(selected.get("name") or "").strip() if isinstance(selected, dict) else ""
        if selected_name and selected_name in available_names:
            choice_rule = f"The JSON name value MUST be {selected_name!r}. Do not answer with prose."

    return f"""Convert the latest request into a JSON data document when it matches one of the
'''
repl = '''    elif isinstance(tool_choice, dict):
        selected = tool_choice.get("function")
        selected_name = str(selected.get("name") or "").strip() if isinstance(selected, dict) else ""
        if selected_name and selected_name in available_names:
            choice_rule = f"The JSON name value MUST be {selected_name!r}. Do not answer with prose."

    # CATGPT_TOOLCALL_LOOP_FIX_V2
    followup_rule = ""
    if has_tool_results:
        executed = "; ".join(executed_tool_calls or []) or "(previous tool call details unavailable)"
        followup_rule = (
            "\nFollow-up rules for an already-executed tool result (IMPORTANT):\n"
            "- A tool result is already present in the conversation and is authoritative.\n"
            "- Do NOT repeat a tool call that has already been executed for the same request.\n"
            "- Prefer answering the user directly using the existing tool result.\n"
            "- Only request another tool when a genuinely new piece of information is required.\n"
            f"- Previously executed tool call(s): {executed}\n"
        )

    return f"""Convert the latest request into a JSON data document when it matches one of the
'''
if needle not in src:
    fail('找不到 tool prompt choice_rule 区域。')
src = src.replace(needle, repl, 1)

needle = '''- Escape line feeds, carriage returns, tabs, quotes, and backslashes inside JSON strings.
- Never place literal control characters inside a JSON string.
- {choice_rule}
"""'''
repl = '''- Escape line feeds, carriage returns, tabs, quotes, and backslashes inside JSON strings.
- Never place literal control characters inside a JSON string.
- {choice_rule}
{followup_rule}
"""'''
if needle not in src:
    fail('找不到 tool prompt 规则结尾。')
src = src.replace(needle, repl, 1)

# 3. 增加重复工具调用判断辅助函数。
needle = '''def _tool_choice_requires_call(tool_choice: str | dict[str, Any] | None) -> bool:
    return tool_choice == "required" or isinstance(tool_choice, dict)
'''
repl = '''# CATGPT_TOOLCALL_LOOP_FIX_V2
def _normalized_tool_call_key(name: str, arguments: str) -> tuple[str, str]:
    """Normalize tool arguments so equivalent JSON calls compare equal."""
    try:
        parsed = json.loads(arguments)
        normalized = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        normalized = (arguments or "").strip()
    return name, normalized


def _executed_tool_call_keys(messages: list[ChatMessage]) -> set[tuple[str, str]]:
    """Return tool calls from assistant messages that already have tool results."""
    keys: set[tuple[str, str]] = set()
    for index, message in enumerate(messages):
        if message.role != "assistant" or not message.tool_calls:
            continue
        for call in message.tool_calls:
            call_id = getattr(call, "id", None)
            if not call_id:
                continue
            has_result = any(
                later.role == "tool" and getattr(later, "tool_call_id", None) == call_id
                for later in messages[index + 1 :]
            )
            if has_result:
                keys.add(_normalized_tool_call_key(call.function.name, call.function.arguments))
    return keys


def _repeated_tool_calls(
    tool_calls: list[ToolCall] | None,
    executed_keys: set[tuple[str, str]],
) -> list[ToolCall]:
    if not tool_calls or not executed_keys:
        return []
    return [
        call
        for call in tool_calls
        if _normalized_tool_call_key(call.function.name, call.function.arguments) in executed_keys
    ]


def _build_tool_result_followup_prompt(repeated: list[ToolCall]) -> str:
    names = ", ".join(call.function.name for call in repeated)
    return (
        "The requested tool operation has already been executed and its result is already present in the conversation. "
        f"You attempted to repeat the same completed tool call ({names}). "
        "Do not call that tool again. Use the existing tool result and answer the user directly in natural language. "
        "Do not output JSON and do not output a tool_calls object."
    )


def _tool_choice_requires_call(tool_choice: str | dict[str, Any] | None) -> bool:
    return tool_choice == "required" or isinstance(tool_choice, dict)
'''
if needle not in src:
    fail('找不到 _tool_choice_requires_call()。')
src = src.replace(needle, repl, 1)

# 4. recovery：接受 messages，上游已有 tool result 时不强制再调工具；重复调用则内部转最终回答。
old_sig = '''async def _parse_tool_calls_with_recovery(
    client: ProviderClient,
    response_text: str,
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None,
    model_id: str,
    reasoning_kwargs: dict[str, Any],
) -> tuple[list[ToolCall] | None, str, Any | None]:'''
new_sig = '''async def _parse_tool_calls_with_recovery(
    client: ProviderClient,
    response_text: str,
    tools: list[ToolDefinition],
    tool_choice: str | dict[str, Any] | None,
    model_id: str,
    reasoning_kwargs: dict[str, Any],
    messages: list[ChatMessage] | None = None,
) -> tuple[list[ToolCall] | None, str, Any | None]:'''
if old_sig not in src:
    fail('找不到 recovery 函数签名。')
src = src.replace(old_sig, new_sig, 1)

old = '''    if not parse_error and not (
        tool_calls is None and _tool_choice_requires_call(tool_choice)
    ):
        return tool_calls, response_text, None
'''
new = '''    has_tool_results = any(message.role == "tool" for message in (messages or []))
    if not parse_error and not (
        tool_calls is None and _tool_choice_requires_call(tool_choice) and not has_tool_results
    ):
        executed_keys = _executed_tool_call_keys(messages or [])
        repeated = _repeated_tool_calls(tool_calls, executed_keys)
        if repeated:
            log.warning(
                "Preventing repeated tool-call loop: %s",
                ", ".join(call.function.name for call in repeated),
            )
            try:
                final_result = await client.send_message(
                    _build_tool_result_followup_prompt(repeated),
                    model=model_id,
                    **reasoning_kwargs,
                )
                final_text = final_result.message
                if _decode_tool_payload(final_text) is not None:
                    final_result = await client.send_message(
                        "Use the already-present tool result and provide the final answer now. "
                        "Do not call tools and do not output JSON.",
                        model=model_id,
                        **reasoning_kwargs,
                    )
                    final_text = final_result.message
                return None, final_text, final_result
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Could not recover repeated tool-call loop: {exc}",
                ) from exc
        return tool_calls, response_text, None
'''
if old not in src:
    fail('找不到 recovery 正常返回分支。')
src = src.replace(old, new, 1)

old = '''    retry_reason = parse_error or ToolCallParseError(
        "A tool call was required but none was returned"
    )
    log.warning("Tool response was not usable; retrying once: %s", retry_reason)
'''
new = '''    # A follow-up containing tool results should normally produce the final answer,
    # even when the original request carried tool_choice=required. Do not force a
    # second JSON tool call merely because the client kept the original flag.
    if has_tool_results and parse_error is None and tool_calls is None:
        return None, response_text, None

    retry_reason = parse_error or ToolCallParseError(
        "A tool call was required but none was returned"
    )
    log.warning("Tool response was not usable; retrying once: %s", retry_reason)
'''
if old not in src:
    fail('找不到 retry_reason 区域。')
src = src.replace(old, new, 1)

# 5. tool-call retry 路径也拦截已执行的同调用。
needle = '''        tool_calls = _parse_tool_calls(response_text, tools)
    except ToolCallParseError as exc:
'''
repl = '''        tool_calls = _parse_tool_calls(response_text, tools)
        executed_keys = _executed_tool_call_keys(messages or [])
        repeated = _repeated_tool_calls(tool_calls, executed_keys)
        if repeated:
            log.warning(
                "Preventing repeated tool-call loop after retry: %s",
                ", ".join(call.function.name for call in repeated),
            )
            final_result = await client.send_message(
                _build_tool_result_followup_prompt(repeated),
                model=model_id,
                **reasoning_kwargs,
            )
            return None, final_result.message, final_result
    except ToolCallParseError as exc:
'''
pos = src.find('async def _parse_tool_calls_with_recovery(')
segment = src[pos:]
if needle not in segment:
    fail('找不到 recovery retry parse 区域。')
segment = segment.replace(needle, repl, 1)
src = src[:pos] + segment

# 6. 调用 tool prompt 时传入 follow-up 状态。
old = '''            if request.tools and request.tool_choice != "none":
                tool_system = _build_tool_system_prompt(request.tools, request.tool_choice)
                if tool_system:
                    messages = _apply_tool_prompt_to_messages(messages, tool_system)
'''
new = '''            if request.tools and request.tool_choice != "none":
                has_tool_results = any(message.role == "tool" for message in messages)
                executed_tool_descriptions = []
                for message in messages:
                    if message.role == "assistant" and message.tool_calls:
                        for call in message.tool_calls:
                            executed_tool_descriptions.append(
                                f"{call.function.name}({call.function.arguments})"
                            )
                tool_system = _build_tool_system_prompt(
                    request.tools,
                    request.tool_choice,
                    has_tool_results=has_tool_results,
                    executed_tool_calls=executed_tool_descriptions,
                )
                if tool_system:
                    messages = _apply_tool_prompt_to_messages(messages, tool_system)
'''
if old not in src:
    fail('找不到 create_chat_completion 中的 tool prompt 调用。')
src = src.replace(old, new, 1)

# 7. 将当前 messages 传入 recovery。
old = '''                    request.tool_choice,
                    model_id,
                    reasoning_kwargs,
                )
'''
new = '''                    request.tool_choice,
                    model_id,
                    reasoning_kwargs,
                    messages=messages,
                )
'''
if old not in src:
    fail('找不到 recovery 调用参数列表。')
src = src.replace(old, new, 1)

TARGET.write_text(src, encoding='utf-8')

subprocess.run([sys.executable, '-m', 'py_compile', str(TARGET)], check=True)
print('[OK] Python 语法检查通过。')
print('[OK] Tool Calling V2 修复完成。')
print(f'[OK] 备份文件: {backup}')
