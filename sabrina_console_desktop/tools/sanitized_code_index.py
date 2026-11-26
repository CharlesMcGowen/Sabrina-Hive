"""SQLite search over validated anonymous code structures only."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3

DEFAULT_DB = Path.home() / '.local/share/sabrina-code-catalog/sanitized.sqlite3'


def build(export_dir, destination=DEFAULT_DB):
    from tools.validate_structural_export import validate
    export_dir, destination = Path(export_dir), Path(destination)
    validate(export_dir)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = destination.with_suffix('.sqlite3.new')
    if temp.exists():
        temp.unlink()
    db = sqlite3.connect(temp)
    try:
        db.executescript('''
          PRAGMA journal_mode=DELETE;
          CREATE TABLE records(id TEXT PRIMARY KEY, language TEXT NOT NULL, body TEXT NOT NULL);
          CREATE TABLE symbols(record_id TEXT, language TEXT, symbol_id TEXT, parent_id TEXT, kind TEXT,
              depth INTEGER, parameter_count INTEGER, async_function INTEGER);
          CREATE INDEX symbol_filters ON symbols(language,kind,depth,parameter_count,async_function);
          CREATE VIRTUAL TABLE structure_fts USING fts5(record_id UNINDEXED, terms, tokenize='unicode61');
          CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        count = 0
        with (export_dir / 'structures.jsonl').open(encoding='utf-8') as source:
            for line in source:
                record = json.loads(line)
                record_id, language = record['id'], record['language']
                body = json.dumps(record, separators=(',', ':'))
                db.execute('INSERT INTO records VALUES(?,?,?)', (record_id, language, body))
                terms = [language]
                for item in record.get('symbols',[]):
                    params=item.get('parameters',[])
                    n=len(params) if isinstance(params,list) else int(params or 0)
                    db.execute('INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)',
                        (record_id,language,item['id'],item.get('parent'),item['kind'],item.get('depth',0),n,int(item.get('async_function',False))))
                    terms.extend((item['kind'], 'nested' if item.get('parent') else 'top_level', 'async' if item.get('async_function') else 'sync', 'arity_'+str(n)))
                for item in record.get('nodes',[]):
                    db.execute('INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?)',
                        (record_id,language,item['id'],item.get('parent'),item['kind'],item.get('depth',0),0,0))
                    terms.extend((item['kind'], 'nested' if item.get('parent') else 'top_level'))
                db.execute('INSERT INTO structure_fts VALUES(?,?)',(record_id,' '.join(terms)))
                count += 1
        summary=json.loads((export_dir/'summary.json').read_text())
        db.executemany('INSERT INTO metadata VALUES(?,?)', [('record_count',str(count)),('source_export',export_dir.name),('summary',json.dumps(summary))])
        db.commit()
    finally:
        db.close()
    if os.name != 'nt':
        temp.chmod(0o600)
    os.replace(temp,destination)
    return {'records':count,'database':str(destination)}


def search(language=None, kind=None, nested=None, min_parameters=None, max_parameters=None,
           async_function=None, limit=20, db_path=None):
    path=Path(db_path or os.environ.get('SABRINA_SANITIZED_CODE_DB',DEFAULT_DB)).absolute()
    if not path.exists(): return []
    conditions=[]; params=[]
    for col,value in [('r.language',language),('s.kind',kind)]:
        if value is not None: conditions.append(col+'=?'); params.append(value)
    if nested is not None: conditions.append("(s.parent_id IS NOT NULL)=?"); params.append(int(nested))
    if min_parameters is not None: conditions.append('s.parameter_count>=?'); params.append(int(min_parameters))
    if max_parameters is not None: conditions.append('s.parameter_count<=?'); params.append(int(max_parameters))
    if async_function is not None: conditions.append('s.async_function=?'); params.append(int(async_function))
    if not conditions: return []
    sql='SELECT DISTINCT r.body FROM records r JOIN symbols s ON r.id=s.record_id WHERE '+' AND '.join(conditions)+' ORDER BY r.language LIMIT ?'
    params.append(max(1,min(int(limit),50)))
    with closing(sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True,timeout=5)) as db:
        return [json.loads(row[0]) for row in db.execute(sql,params)]


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    sub=parser.add_subparsers(dest='command',required=True)
    b=sub.add_parser('build'); b.add_argument('export_dir'); b.add_argument('--db',default=str(DEFAULT_DB))
    s=sub.add_parser('search'); s.add_argument('--language'); s.add_argument('--kind'); s.add_argument('--nested',choices=['true','false']); s.add_argument('--min-parameters',type=int); s.add_argument('--max-parameters',type=int); s.add_argument('--async-function',choices=['true','false']); s.add_argument('--limit',type=int,default=20); s.add_argument('--db',default=str(DEFAULT_DB))
    a=parser.parse_args()
    if a.command=='build': result=build(a.export_dir,a.db)
    else: result=search(a.language,a.kind,None if a.nested is None else a.nested=='true',a.min_parameters,a.max_parameters,None if a.async_function is None else a.async_function=='true',a.limit,a.db)
    print(json.dumps(result,indent=2))
