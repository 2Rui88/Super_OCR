"""业务装配包 —— 把知识库业务注入 agent 内核。

routes / evalservice 使用前 import 本包并调用 setup()（幂等）。
内核(app/agent)不 import 本包，方向永远单向：业务 → 内核。
"""
from __future__ import annotations

from app.agent import engine, prompts, registry
from app.agent_business import prompt_business, tools_knowledge

_setup_done = False


def setup() -> None:
    global _setup_done
    if _setup_done:
        return
    registry.register(*tools_knowledge.SPECS)
    prompts.register_section(prompt_business.SECTION)
    prompts.register_refusal(prompt_business.REFUSAL)
    prompts.register_direct_reply(prompt_business.DIRECT_REPLY_RULES)
    engine.register_no_lookup_policy("search_chunks", prompt_business.classify_no_lookup)
    _setup_done = True
