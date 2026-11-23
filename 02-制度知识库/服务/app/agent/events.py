"""事件协议 —— engine 产出的全部事件类型（SSE 与内部消费方共用）。

事件顺序契约：thinking → (tool_call|tool_result)* → citations → delta* → done
refuse / error 可在任意点终止本轮。业务自定义事件用 "business." 前缀透传。
"""
from __future__ import annotations

THINKING = "thinking"        # {round}
TOOL_CALL = "tool_call"      # {id, name, args}
TOOL_RESULT = "tool_result"  # {id, name, count, summary, result(仅内部消费)}
CITATIONS = "citations"      # {items, tool_calls}
DELTA = "delta"              # {text}
DONE = "done"                # {duration_ms, tool_calls}
REFUSE = "refuse"            # {message}
ERROR = "error"              # {code, message}
