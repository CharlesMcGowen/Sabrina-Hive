import os

root_dir = "."
matches = []

for root, dirs, files in os.walk(root_dir):
    # filter out unwanted dirs
    dirs[:] = [d for d in dirs if d not in [".venv", "venv", ".git", "node_modules", "artifacts", "__pycache__", ".pytest_cache"]]
    for f in files:
        if f.endswith((".py", ".json", ".md", ".yml", ".yaml", ".sh", ".ps1", ".toml")):
            fpath = os.path.join(root, f)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as file:
                    content = file.read()
                    if "tread" in content.lower():
                        matches.append(fpath)
            except Exception:
                pass

print("Files containing 'tread':", matches)
