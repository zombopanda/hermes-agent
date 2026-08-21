from pathlib import Path


def read(path: str) -> str:
    return Path(path).read_text()


def write(path: str, text: str) -> None:
    Path(path).write_text(text)


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"{path}: expected exactly one match, found {count}: {old[:120]!r}"
        )
    write(path, text.replace(old, new, 1))


def wrap_reasoning_refresh(path: str, anchor: str, indent: int) -> None:
    text = read(path)
    lines = text.splitlines(keepends=True)
    anchor_i = next((i for i, line in enumerate(lines) if anchor in line), None)
    if anchor_i is None:
        raise RuntimeError(f"{path}: anchor not found: {anchor}")

    prefix = " " * indent
    try_i = next(
        (i for i in range(anchor_i + 1, len(lines)) if lines[i] == prefix + "try:\n"),
        None,
    )
    if try_i is None:
        raise RuntimeError(f"{path}: reasoning try block not found")

    except_i = next(
        (
            i
            for i in range(try_i + 1, len(lines))
            if lines[i].startswith(prefix + "except Exception as _reasoning_err:")
        ),
        None,
    )
    if except_i is None:
        raise RuntimeError(f"{path}: reasoning except block not found")

    end_i = except_i + 1
    while end_i < len(lines):
        line = lines[end_i]
        if not line.strip():
            end_i += 1
            break
        leading = len(line) - len(line.lstrip(" "))
        if leading <= indent:
            break
        end_i += 1

    block = lines[try_i:end_i]
    guarded = [
        prefix + 'if getattr(agent, "_reasoning_config_fixed", False) is not True:\n'
    ]
    guarded.extend(("    " + line if line.strip() else line) for line in block)
    lines[try_i:end_i] = guarded
    write(path, "".join(lines))


# Default is unpinned. Gateway turn setup below refreshes this on every turn.
replace_once(
    "agent/agent_init.py",
    "    agent.reasoning_config = reasoning_config\n    agent.service_tier = service_tier\n",
    "    agent.reasoning_config = reasoning_config\n"
    "    # Gateway sessions can pin reasoning for a whole turn so a fallback or\n"
    "    # same-turn model switch cannot replace an explicit scoped value.\n"
    "    agent._reasoning_config_fixed = False\n"
    "    agent.service_tier = service_tier\n",
)

wrap_reasoning_refresh(
    "agent/agent_runtime_helpers.py",
    "Re-resolve reasoning_config from per-model override",
    4,
)
wrap_reasoning_refresh(
    "agent/chat_completion_helpers.py",
    "Re-resolve reasoning_config for the new fallback model",
    8,
)

# Platform-generic scoped pin detection. It deliberately uses the same
# _get_channel_override lookup as reasoning resolution, so Telegram topics,
# Discord threads and other platforms keep one precedence implementation.
run_path = "gateway/run.py"
run_text = read(run_path)
resolver_marker = "    def _set_session_reasoning_override("
if resolver_marker not in run_text:
    raise RuntimeError("gateway/run.py: session reasoning setter marker missing")

scoped_method = '''    def _has_scoped_reasoning_override(
        self,
        *,
        source: Optional[SessionSource] = None,
        session_key: Optional[str] = None,
    ) -> bool:
        """Return whether reasoning is explicitly scoped for this gateway turn."""
        resolved_session_key = session_key
        if not resolved_session_key and source is not None:
            try:
                resolved_session_key = self._session_key_for_source(source)
            except Exception:
                resolved_session_key = None

        if resolved_session_key:
            state = self._peek_session_state(resolved_session_key)
            if state is not None and state.conversation.reasoning_override is not None:
                return True

        config = getattr(self, "config", None)
        if config is None or source is None:
            return False
        try:
            override = _get_channel_override(
                config,
                source.platform,
                str(source.chat_id) if source.chat_id else "",
                thread_id=(
                    str(source.thread_id)
                    if getattr(source, "thread_id", None)
                    else None
                ),
                parent_id=(
                    str(source.parent_chat_id)
                    if getattr(source, "parent_chat_id", None)
                    else None
                ),
            )
            channel_effort = getattr(override, "reasoning_effort", None)
            if channel_effort is None:
                return False

            from hermes_constants import parse_reasoning_effort

            return parse_reasoning_effort(channel_effort) is not None
        except Exception:
            logger.debug(
                "Failed to detect scoped channel reasoning override",
                exc_info=True,
            )
            return False

'''
run_text = run_text.replace(resolver_marker, scoped_method + resolver_marker, 1)

# Cached agents are reused across lanes. Refresh the pin on every normal turn,
# including setting it back to False for a subsequent unscoped turn.
normal_anchor = "reasoning_config = self._runner._resolve_session_reasoning_config("
normal_i = run_text.find(normal_anchor)
if normal_i < 0:
    raise RuntimeError("gateway/run.py: normal reasoning resolution missing")
normal_assign = "        agent.reasoning_config = reasoning_config\n"
normal_assign_i = run_text.find(normal_assign, normal_i)
if normal_assign_i < 0 or normal_assign_i - normal_i > 12000:
    raise RuntimeError("gateway/run.py: normal agent reasoning assignment missing")
normal_repl = (
    normal_assign
    + "        agent._reasoning_config_fixed = "
    + "self._runner._has_scoped_reasoning_override(\n"
    + "            source=ctx.source,\n"
    + "            session_key=ctx.session_key,\n"
    + "        )\n"
)
run_text = (
    run_text[:normal_assign_i]
    + normal_repl
    + run_text[normal_assign_i + len(normal_assign):]
)

# Background turns construct an agent after resolving the same source-scoped
# reasoning config. Pin it before the conversation starts.
bg_anchor = (
    "reasoning_config = self._resolve_session_reasoning_config(\n"
    "                source=source, model=model\n"
    "            )"
)
bg_i = run_text.find(bg_anchor)
if bg_i < 0:
    raise RuntimeError("gateway/run.py: background reasoning resolution missing")
bg_agent_i = run_text.find("                agent = AIAgent(\n", bg_i)
if bg_agent_i < 0 or bg_agent_i - bg_i > 12000:
    raise RuntimeError("gateway/run.py: background AIAgent construction missing")
bg_try_i = run_text.find("                try:\n", bg_agent_i)
if bg_try_i < 0 or bg_try_i - bg_agent_i > 12000:
    raise RuntimeError("gateway/run.py: background run try marker missing")
run_text = (
    run_text[:bg_try_i]
    + "                agent._reasoning_config_fixed = "
    + "self._has_scoped_reasoning_override(source=source)\n"
    + run_text[bg_try_i:]
)
write(run_path, run_text)

# Provider fallback regression.
replace_once(
    "tests/run_agent/test_provider_fallback.py",
    "\n\n    def test_skips_unconfigured_provider_to_next(self):\n",
    '''

    def test_scoped_reasoning_survives_fallback(self):
        agent = _make_agent(
            fallback_model=[{"provider": "openai", "model": "gpt-4o"}]
        )
        agent.reasoning_config = {"enabled": False}
        agent._reasoning_config_fixed = True

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(_mock_client(), "gpt-4o"),
            ),
            patch("hermes_constants.resolve_reasoning_config") as resolve_reasoning,
        ):
            assert agent._try_activate_fallback() is True

        assert agent.reasoning_config == {"enabled": False}
        resolve_reasoning.assert_not_called()


    def test_skips_unconfigured_provider_to_next(self):
''',
)

# Same-turn /model regression.
replace_once(
    "tests/run_agent/test_switch_model_reasoning_override.py",
    "\n\n    def test_restore_primary_runtime_restores_reasoning(self):\n",
    '''

    def test_scoped_reasoning_survives_model_switch(self):
        """A session/channel reasoning override stays fixed for the whole turn."""
        from agent.agent_runtime_helpers import switch_model

        agent = self._make_fake_agent()
        agent.reasoning_config = {"enabled": False}
        agent._reasoning_config_fixed = True
        fake_cfg = {
            "agent": {
                "reasoning_effort": "high",
                "reasoning_overrides": {"claude-opus-4.5": "xhigh"},
            },
        }

        with patch("hermes_cli.config.load_config", return_value=fake_cfg):
            try:
                switch_model(
                    agent,
                    new_model="claude-opus-4.5",
                    new_provider="anthropic",
                    base_url="https://api.anthropic.com",
                    api_mode="anthropic_messages",
                )
            except Exception:
                pass

        assert agent.reasoning_config == {"enabled": False}


    def test_restore_primary_runtime_restores_reasoning(self):
''',
)

# Gateway pin detection regressions. A syntactically invalid channel value
# must fall through to model/global defaults and therefore must not pin.
gateway_tests = "tests/gateway/test_channel_overrides.py"
tests_text = read(gateway_tests)
tests_text += '''


class TestScopedReasoningPin:
    def test_valid_channel_reasoning_marks_turn_scoped(self):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "-100123:188": ChannelOverride(reasoning_effort="high"),
                    },
                ),
            },
        )
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100123",
            chat_type="forum",
            thread_id="188",
            user_id="u1",
        )

        assert runner._has_scoped_reasoning_override(source=source) is True

    def test_invalid_channel_reasoning_does_not_pin_turn(self):
        runner = object.__new__(GatewayRunner)
        runner.config = GatewayConfig(
            platforms={
                Platform.TELEGRAM: PlatformConfig(
                    enabled=True,
                    channel_overrides={
                        "-100123:188": ChannelOverride(reasoning_effort="bogus"),
                    },
                ),
            },
        )
        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-100123",
            chat_type="forum",
            thread_id="188",
            user_id="u1",
        )

        assert runner._has_scoped_reasoning_override(source=source) is False
'''
write(gateway_tests, tests_text)

# Messaging docs are the user-facing config contract, not optional garnish.
docs = "website/docs/user-guide/messaging/index.md"
replace_once(
    docs,
    "## Per-Channel Model & System Prompt Overrides",
    "## Per-Channel Model, Reasoning & System Prompt Overrides",
)
replace_once(
    docs,
    "Different channels can run different models and personas from a **single gateway**",
    "Different channels can run different models, reasoning levels, and personas from a **single gateway**",
)
replace_once(
    docs,
    '        provider: anthropic\n        system_prompt: "You are the #dev channel code-review specialist."',
    '        provider: anthropic\n        reasoning_effort: high\n        system_prompt: "You are the #dev channel code-review specialist."',
)
replace_once(
    docs,
    "- All three keys are optional — set only `model`, only `system_prompt`, or any combination. Unset fields fall back to the global defaults.\n",
    "- All four keys are optional — set `model`, `provider`, `reasoning_effort`, `system_prompt`, or any combination. Unset fields fall back to the global defaults.\n"
    "- `reasoning_effort` accepts `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, or `ultra`; YAML `false` also disables reasoning.\n",
)
replace_once(
    docs,
    "- Resolution priority for the model is: session `/model` override → `channel_overrides` → global config. A user running `/model` in a chat still wins over the channel default.\n",
    "- Resolution priority for the model is: session `/model` override → `channel_overrides` → global config. A user running `/model` in a chat still wins over the channel default.\n"
    "- Resolution priority for reasoning is: session `/reasoning` override → `channel_overrides` → `agent.reasoning_overrides` for the effective model → global `agent.reasoning_effort` → provider default. A scoped session/channel value stays fixed across same-turn model switches and provider fallback.\n",
)
