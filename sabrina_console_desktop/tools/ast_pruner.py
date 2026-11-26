import ast

class ASTSkeletonizer(ast.NodeTransformer):
    def visit_Module(self, node):
        # Filter top-level statements to prevent context overflow from large lists/dicts/main blocks
        new_body = []
        for stmt in node.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                new_body.append(self.visit(stmt))
            elif isinstance(stmt, ast.Assign):
                # Keep the assignment but truncate the value to prevent giant payload dicts
                stmt.value = ast.Constant(value=Ellipsis)
                new_body.append(stmt)
            elif isinstance(stmt, ast.AnnAssign):
                if stmt.value is not None:
                    stmt.value = ast.Constant(value=Ellipsis)
                new_body.append(stmt)
            # We explicitly drop Expr (docstrings at module level are okay to drop, or arbitrary calls),
            # If (e.g. if __name__ == '__main__'), For, While, etc.
        node.body = new_body
        return node

    def visit_FunctionDef(self, node):
        # We need to keep the function signature, decorators, arguments, and return types.
        # But we replace the body with the docstring and an Ellipsis (...).
        self.generic_visit(node)
        docstring = ast.get_docstring(node)
        new_body = []
        if docstring:
            new_body.append(ast.Expr(value=ast.Constant(value=docstring)))
        new_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
        node.body = new_body
        return node
        
    def visit_AsyncFunctionDef(self, node):
        self.generic_visit(node)
        docstring = ast.get_docstring(node)
        new_body = []
        if docstring:
            new_body.append(ast.Expr(value=ast.Constant(value=docstring)))
        new_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
        node.body = new_body
        return node

    def visit_ClassDef(self, node):
        self.generic_visit(node)
        # If class body is empty after pruning (e.g. it only had methods that were pruned... wait, methods are kept but their bodies are pruned).
        # We don't prune class bodies entirely, we just let generic_visit handle methods.
        return node

def skeletonize_code(source_code: str) -> str:
    """
    Parses Python source code, strips out function/method bodies, 
    and returns a skeletonized version with signatures and docstrings.
    This condenses large files into interfaces so they fit in small LLM context windows.
    """
    try:
        tree = ast.parse(source_code)
        skeletonizer = ASTSkeletonizer()
        pruned_tree = skeletonizer.visit(tree)
        ast.fix_missing_locations(pruned_tree)
        
        # Fallback for Python < 3.9 where ast.unparse does not exist
        if hasattr(ast, "unparse"):
            return ast.unparse(pruned_tree)
        else:
            try:
                import astor
                return astor.to_source(pruned_tree)
            except ImportError:
                print("[AST Pruner] Warning: ast.unparse not found and astor is not installed. Returning original source.")
                return source_code
    except Exception as e:
        # Fallback to original code if syntax error or unparse failure
        print(f"[AST Pruner] Failed to skeletonize code: {e}")
        return source_code
