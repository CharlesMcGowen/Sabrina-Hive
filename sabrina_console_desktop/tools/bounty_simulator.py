import sqlite3

DB_PATH = 'data/probe_runs.db'


def read_file(path):
    with open(path, 'r') as f:
        return f.read()


def get_tool_scores():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute('SELECT tool_name, status, response FROM crew_tasks')
    tasks = cursor.fetchall()

    tool_scores = {}
    for tool_name, status, response in tasks:
        if 'PLAN_STEP_WORKED' in response:
            worked = int(response.split('PLAN_STEP_WORKED:')[1].split()[0])
            if tool_name not in tool_scores:
                tool_scores[tool_name] = {'worked': 0, 'total': 0}
            tool_scores[tool_name]['worked'] += worked
            tool_scores[tool_name]['total'] += 1

    for tool_name in tool_scores:
        tool_scores[tool_name]['score'] = tool_scores[tool_name]['worked'] / tool_scores[tool_name]['total']

    conn.close()
    return tool_scores


def guidance_line():
    tool_scores = get_tool_scores()
    best_tool = max(tool_scores, key=lambda k: tool_scores[k]['score'])
    print(f'Best tool to use: {best_tool} (score: {tool_scores[best_tool]['score']:.2f})')
    return f'Best tool to use: {best_tool} (score: {tool_scores[best_tool]['score']:.2f})'
