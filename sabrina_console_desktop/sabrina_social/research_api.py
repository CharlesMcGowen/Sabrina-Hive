"""Standalone content research dashboard. No publishing/browser endpoints."""
import argparse
import os
from pathlib import Path
from urllib.error import URLError

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .experiments import ExperimentLab
from .research_model import LabResearchModel


class EvidenceBody(BaseModel):
    evidence: dict = Field(default_factory=dict)


def create_app(lab=None):
    app = FastAPI(title="Sabrina Content Research", version="1.0")
    app.state.lab = lab or ExperimentLab(
        Path(os.environ.get("SABRINA_SOCIAL_DATA_DIR", "data/social")) / "experiments.sqlite3",
        model=LabResearchModel())

    def invoke(fn, *args):
        try:
            return fn(*args)
        except KeyError as exc:
            raise HTTPException(400, "Missing or unknown field/id: " + str(exc)) from exc
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise HTTPException(503, "Research model unavailable or timed out; inspect the recorded failed experiment") from exc

    @app.get("/health")
    def health():
        return {"service": "sabrina-content-research", "publishing_enabled": False}

    @app.get("/api/experiments")
    def experiments():
        return app.state.lab.list()

    @app.post("/api/experiments")
    def create(body: EvidenceBody):
        return invoke(app.state.lab.create, body.evidence)

    @app.get("/api/experiments/{exp}")
    def get(exp: str):
        return invoke(app.state.lab.get, exp)

    @app.post("/api/experiments/{exp}/publication")
    def publication(exp: str, body: EvidenceBody):
        return invoke(app.state.lab.publish_evidence, exp, body.evidence)

    @app.post("/api/experiments/{exp}/snapshots")
    def snapshot(exp: str, body: EvidenceBody):
        return invoke(app.state.lab.snapshot, exp, body.evidence)

    @app.get("/api/experiments/{exp}/policy")
    def policy(exp: str):
        record = invoke(app.state.lab.get, exp)
        return app.state.lab.policy(record["context"])

    @app.get("/api/due")
    def due():
        return app.state.lab.due()

    @app.post("/api/observations")
    def observe(body: EvidenceBody):
        return invoke(app.state.lab.observe, body.evidence)

    @app.get("/", response_class=HTMLResponse)
    def dashboard():
        return DASHBOARD

    return app


DASHBOARD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Sabrina Content Research</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font:16px system-ui;max-width:960px;margin:36px auto;padding:0 20px;background:#122031;color:#edf2f7}
button,textarea,input,select{font:inherit;padding:10px;margin:5px 0}textarea{width:95%;min-height:90px}input{width:95%}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#1c3045;padding:18px}button{cursor:pointer}
label{display:block;margin-top:14px}small{color:#b8ccdf}</style></head><body>
<h1>Sabrina Content Research</h1><p>Draft competing ideas, record outcomes, and learn what resonates.</p>
<small>Research previews stay here. Creating an experiment does not post to a social account.</small>
<form id="create"><label>Platform <select id="platform"><option>linkedin</option><option>facebook</option></select></label>
<label>Audience<input id="audience" value="Security engineers" required></label>
<label>Topic<input id="topic" value="AI security" required></label>
<label>Your idea<textarea id="idea" required></textarea></label>
<label>Source URL<input id="source" type="url" required></label>
<label>Facts supported by that source<textarea id="facts" required></textarea></label>
<button id="generate">Generate and compare drafts</button></form>
<p id="status" role="status"></p><div id="drafts"></div>
<h2>Experiments</h2><button id="refresh">Refresh list</button> <button id="due">Check outcome windows</button>
<div id="list"></div>
<details><summary>Import recorded evidence</summary><p>Use the documented JSON format for platform analytics or publication evidence.</p>
<label>Experiment ID<input id="experiment"></label><select id="kind"><option value="publication">Publication</option><option value="snapshots">Analytics snapshot</option></select>
<textarea id="evidence" placeholder="JSON evidence"></textarea><button id="import">Import evidence</button></details>
<pre id="output">No experiment selected.</pre>
<script>
const byId=id=>document.getElementById(id);
async function request(url,body){const res=await fetch(url,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({evidence:body})});const data=await res.json();if(!res.ok)throw Error(JSON.stringify(data));return data}
function output(data){byId('output').textContent=JSON.stringify(data,null,2)}
function show(data){output(data);byId('experiment').value=data.id;byId('drafts').replaceChildren();for(const c of data.candidates||[]){const heading=document.createElement('h3');heading.textContent=c.strategy+(c.id===data.selection?.candidate_id?' · selected':'');const text=document.createElement('pre');text.textContent=c.body.content;const review=document.createElement('p');review.textContent='Critic: '+c.review.reason;byId('drafts').append(heading,text,review)}}
async function refresh(){const data=await request('/api/experiments');byId('list').replaceChildren();for(const row of data){const b=document.createElement('button');b.textContent=row.id.slice(0,10)+' · '+row.status;b.onclick=()=>request('/api/experiments/'+row.id).then(show).catch(err=>output(err.message));byId('list').append(b)}}
byId('create').onsubmit=async e=>{e.preventDefault();byId('generate').disabled=true;byId('status').textContent='Models are analyzing, drafting, and reviewing. Each call is recorded.';try{show(await request('/api/experiments',{platform:byId('platform').value,audience:byId('audience').value,topic:byId('topic').value,idea:byId('idea').value,sources:[{id:'source-1',url:byId('source').value,facts:byId('facts').value}]}));byId('status').textContent='Draft comparison ready.';await refresh()}catch(err){byId('status').textContent=err.message;await refresh()}finally{byId('generate').disabled=false}};
byId('refresh').onclick=()=>refresh().catch(err=>output(err.message));byId('due').onclick=()=>request('/api/due').then(output);
byId('import').onclick=async()=>{try{output(await request('/api/experiments/'+encodeURIComponent(byId('experiment').value)+'/'+byId('kind').value,JSON.parse(byId('evidence').value)))}catch(err){output(err.message)}};
refresh().catch(err=>output(err.message));</script></body></html>"""


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8088)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
