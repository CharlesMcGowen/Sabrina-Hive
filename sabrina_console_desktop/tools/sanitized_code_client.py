"""Authenticated Sabrina coordinator client for anonymous structural search."""
from urllib.parse import urlencode
from tools.control_client import configured

def search_code(query_text, limit=2):
    q=query_text.lower()
    language=next((v for keys,v in [(('python',),'python'),(('go','golang'),'go'),(('javascript',' js '),'javascript'),(('yaml','yml'),'yaml')]
                   if any(k in q for k in keys)),None)
    kind=next((k for k in ('class','function','method') if k in q),None)
    if language is None and kind is None: return []
    if kind=='method': kind='function'
    params={'limit':max(1,min(int(limit),5))}
    if language: params['language']=language
    if kind: params['kind']=kind
    client=configured()
    if client is None: return []
    try:
        result=client.request('/v1/code/search?'+urlencode(params))
        return [{'topic':'Anonymous '+r['language']+' code structure',
                 'content':'SANITIZED STRUCTURE; original names and source text are unavailable. '+__import__('json').dumps(r),
                 'score':0.0} for r in result.get('records',[])]
    except Exception:
        return []
