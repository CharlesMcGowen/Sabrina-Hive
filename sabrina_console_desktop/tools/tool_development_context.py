"""Complete bounded documentation packet and provenance for engineering tasks."""
import hashlib
import json
from pathlib import Path
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ('skills/sandbox-tool-development/SKILL.md',
             'docs/TOOL_DEVELOPMENT_LIFECYCLE.md', 'docs/LAB_COORDINATOR.md')


def required_documents(*, root=ROOT, max_bytes=32000):
    """Return the complete required documents and the hashes of their source bytes."""
    root = Path(root)
    documents, total = [], 0
    for name in DOCUMENTS:
        raw = (root/name).read_bytes()
        total += len(raw)
        if total > max_bytes:
            raise ValueError('Required engineering documents exceed context budget; split the reading task, never silently truncate')
        text = raw.decode('utf-8')
        documents.append({'path': name, 'sha256': hashlib.sha256(raw).hexdigest(),
                          'bytes': len(raw), 'lines': len(text.splitlines()), 'text': text})
    return documents


def development_context(*, root=ROOT, max_bytes=32000):
    root = Path(root)
    parts, sources = [], []
    for document in required_documents(root=root, max_bytes=max_bytes):
        text = document.pop('text')
        name = document['path']
        sources.append({**document, 'complete_text_supplied': True})
        parts.append(f'BEGIN REQUIRED DOCUMENT {name}\n{text}\nEND REQUIRED DOCUMENT {name}')
    ident = 'context_'+uuid.uuid4().hex
    out = root/'artifacts/tool_development_context'/f'{ident}.json'; out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'id': ident, 'at': time.time(), 'sources': sources,
        'meaning': 'Complete source text supplied as task context; not proof of comprehension.'}, indent=2), encoding='utf-8')
    return ('Required engineering context. Follow the procedure and cite its evidence; record missing source reads.\n'
            + f'Context receipt: {out}\n' + '\n\n'.join(parts))
