TOOL_NAME = 'paper_2611_25387'
TOOL_PROMPT = 'success rate'
PARALLEL_SAFE = True

def execute_plugin(action, loop=None):
    from tools.sabrina_tool_forge import invoke
    return invoke(TOOL_NAME, action['rows'])

execute = execute_plugin
