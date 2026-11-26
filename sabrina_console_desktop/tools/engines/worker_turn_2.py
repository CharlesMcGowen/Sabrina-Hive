import ast
import os
import re
import sys

# Regex pattern for basic hardcoded secret detection
SECRET_PATTERN = re.compile(
    r'(?i)(api[_\-]?key|secret|password|auth[_\-]?token)\s*=\s*[\'"][A-Za-z0-9_\-]{8,}[\'"]'
)

class SecurityASTVisitor(ast.NodeVisitor):
    def __init__(self, filename):
        self.filename = filename
        self.issues = []

    def visit_Call(self, node):
        # 1. Check for unsafe deserialization (pickle.loads)
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in ['loads', 'load'] and getattr(node.func.value, 'id', None) == 'pickle':
                self.issues.append({
                    'file': self.filename,
                    'line': node.lineno,
                    'type': 'Unsafe Deserialization',
                    'detail': 'Use of pickle.load/loads detected.'
                })

        # 2. Check for shell injection (subprocess with shell=True)
        if isinstance(node.func, ast.Attribute) and getattr(node.func.value, 'id', None) == 'subprocess':
            for keyword in node.keywords:
                if keyword.arg == 'shell' and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                    self.issues.append({
                        'file': self.filename,
                        'line': node.lineno,
                        'type': 'Shell Injection',
                        'detail': 'subprocess call with shell=True detected.'
                    })

        self.generic_visit(node)

def audit_file(filepath):
    issues = []

    # AST analysis
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()

        # Check for hardcoded secrets via regex
        for line_num, line in enumerate(content.splitlines(), 1):
            if SECRET_PATTERN.search(line):
                issues.append({
                    'file': filepath,
                    'line': line_num,
                    'type': 'Hardcoded Secret',
                    'detail': 'Possible hardcoded secret or API key found.'
                })

        # Parse AST for structural checks
        tree = ast.parse(content, filename=filepath)
        visitor = SecurityASTVisitor(filepath)
        visitor.visit(tree)
        issues.extend(visitor.issues)

    except SyntaxError:
        pass  # Skip non-valid python files
    except Exception as e:
        print(f"Error analyzing {filepath}: {e}")

    return issues

def generate_report(findings, output_filename="audit_report.md"):
    with open(output_filename, 'w', encoding='utf-8') as f:
        f.write("# Static Security Audit Report\n\n")
        if not findings:
            f.write("No security issues detected.\n")
            return

        f.write("| File | Line | Issue Type | Details |\n")
        f.write("| --- | --- | --- | --- |\n")
        for issue in findings:
            f.write(f"| `{issue['file']}` | {issue['line']} | {issue['type']} | {issue['detail']} |\n")

def main(repo_url, target_dir="./cloned_repo"):
    if os.path.exists(target_dir):
        subprocess.run(["rm", "-rf", target_dir], check=True)

    print(f"Cloning {repo_url}...")
    subprocess.run(["git", "clone", "--depth", "1", repo_url, target_dir], check=True)

    all_findings = []
    for root, _, files in os.walk(target_dir):
        for file in files:
            if file.endswith('.py'):
                full_path = os.path.join(root, file)
                all_findings.extend(audit_file(full_path))

    generate_report(all_findings)
    print("Audit complete. Results saved to audit_report.md.")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python audit.py <github_repo_url>")
        sys.exit(1)
    main(sys.argv[1])