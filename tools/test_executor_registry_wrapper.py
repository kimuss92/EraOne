from core.tool_registry_validator import extract_tool_names_from_executor_dispatch


def test_registry_follows_real_executor_delegation():
    source = """
def _call_tool_result(tool, parameters, speak):
    return _call_tool_result_inner(tool, parameters, speak)

def _call_tool_result_inner(tool, parameters, speak):
    if tool == "real_tool":
        return "ok"

def unrelated(tool):
    if tool == "unrelated_tool":
        return "ok"
"""
    assert extract_tool_names_from_executor_dispatch(source) == {"real_tool"}


def test_unwired_inner_handler_cannot_make_registry_check_pass():
    source = """
def _call_tool_result(tool, parameters, speak):
    return "unknown"

def _call_tool_result_inner(tool, parameters, speak):
    if tool == "unwired_tool":
        return "ok"
"""
    assert extract_tool_names_from_executor_dispatch(source) == set()
