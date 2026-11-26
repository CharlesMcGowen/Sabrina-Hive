TOOL_NAME = 'paper_2611_21885'
TOOL_PROMPT = 'final answer matches the reference'
PARALLEL_SAFE = True

def execute_plugin(action, loop=None):
    from tools.sabrina_tool_forge import invoke
    return invoke(TOOL_NAME, action['rows'])

execute = execute_plugin
