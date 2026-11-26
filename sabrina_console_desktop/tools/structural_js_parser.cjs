const readline = require('readline');
const acorn = require('acorn');

function parse(source) {
  let tree;
  try { tree = acorn.parse(source, { ecmaVersion: 'latest', sourceType: 'module', allowHashBang: true }); }
  catch (_) {
    try { tree = acorn.parse(source, { ecmaVersion: 'latest', sourceType: 'script', allowHashBang: true }); }
    catch (_) { return { error: true }; }
  }
  const symbols = [];
  function walk(node, parent, depth) {
    if (!node || typeof node !== 'object') return;
    let current = parent;
    if (['ClassDeclaration', 'ClassExpression'].includes(node.type)) {
      current = `symbol_${symbols.length + 1}`;
      symbols.push({ id: current, parent, depth, kind: 'class' });
    } else if (['FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression'].includes(node.type)) {
      current = `symbol_${symbols.length + 1}`;
      symbols.push({ id: current, parent, depth, kind: 'function', parameters: Array.isArray(node.params) ? node.params.map((_, i) => ({id:`parameter_${i+1}`,kind:'positional_or_keyword'})) : [],
        async_function: node.async === true });
    }
    for (const [key, value] of Object.entries(node)) {
      if (['start', 'end', 'loc', 'range', 'comments', 'tokens'].includes(key)) continue;
      if (Array.isArray(value)) {
        for (const child of value) if (child && typeof child === 'object') walk(child, current, depth + (current !== parent ? 1 : 0));
      } else if (value && typeof value === 'object') {
        walk(value, current, depth + (current !== parent ? 1 : 0));
      }
    }
  }
  walk(tree, null, 0);
  return { schema_version: 1, language: 'javascript', symbols };
}

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on('line', line => {
  try { process.stdout.write(JSON.stringify(parse(JSON.parse(line).source)) + '\n'); }
  catch (_) { process.stdout.write('{"error":true}\n'); }
});
