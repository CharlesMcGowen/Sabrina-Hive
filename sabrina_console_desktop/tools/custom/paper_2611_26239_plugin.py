TOOL_NAME = 'paper_2611_26239'
TOOL_PROMPT = 'mean absolute error'
PARALLEL_SAFE = True

def execute_plugin(action, loop=None):
    from tools.sabrina_tool_forge import invoke
    return invoke(TOOL_NAME, action['rows'])

execute = execute_plugin
