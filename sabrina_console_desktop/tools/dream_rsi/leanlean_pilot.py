"""Coordinate a two-attempt LeanLean pilot. Run on the lab with python -m."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from tools.control_client import caller, configured
from tools.dream_rsi.research_blackboard import ResearchBlackboard

DIRECTOR = "huihui_ai/qwen3-abliterated:14b"
DIRECTOR_THINK = True
DIRECTOR_NUM_PREDICT = 3200
WORKER = "qwen2.5-coder:14b"
ACTOR = "sabrina-leanlean"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def put(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def revision(root):
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise ValueError("Unexpected source symlink")
        if p.is_file():
            files[p.relative_to(root).as_posix()] = digest(p)
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def proof_options(root, protected):
    """Offer real proof bodies with their read-only declarations."""
    choices = []
    for path in sorted(root.rglob("*.lean")):
        rel = path.relative_to(root).as_posix()
        if rel == protected or path.name == "lakefile.lean" or any(x.startswith(".") for x in Path(rel).parts):
            continue
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        matches = []
        for i, line in enumerate(lines):
            if not line.rstrip().endswith(":= by"):
                continue
            j = i + 1
            while j < len(lines) and lines[j].startswith((" ", "\t")):
                j += 1
            body = "".join(lines[i+1:j])
            if not 2 <= j-i-1 <= 18 or not 35 <= len(body) <= 1000:
                continue
            header = i
            while header > 0 and lines[header][:1].isspace() and i-header < 8:
                header -= 1
            if re.search(r"\b(theorem|lemma)\b", lines[header]):
                matches.append(dict(file=rel, start=i+1, end=j, declaration="".join(lines[header:i+1]), body=body))
        choices.extend(sorted(matches,key=lambda one:len(one["body"]))[:2])
    return {f"P{i+1}":opt for i,opt in enumerate(choices[:12])}


def apply_edit(base, destination, target, new_body):
    if not isinstance(new_body,str) or not new_body.strip() or len(new_body)>2000:
        raise ValueError("Missing or oversized proof body")
    if re.search(r"(?i)\b(sorry|admit|axiom|unsafe|implemented_by|run_tac|set_option|import|elab|macro|syntax|initialize)\b|#|\x60",new_body):
        raise ValueError("Replacement exceeds tactic-only scope")
    path = Path(target["file"])
    if path.is_absolute() or ".." in path.parts or path.suffix != ".lean":
        raise ValueError("Invalid tactic source path")
    old = (base/path).read_text(encoding="utf-8").splitlines(keepends=True)
    first,last = target["start"],target["end"]
    if "".join(old[first:last]) != target["body"]:
        raise ValueError("Selected proof no longer matches frozen source")
    body = new_body.strip("\n").splitlines()
    indent = min(len(x)-len(x.lstrip(" ")) for x in body if x.strip())
    code = "".join("  "+x[indent:]+"\n" for x in body)
    if code == target["body"]:
        raise ValueError("Worker returned the original proof body")
    shutil.copytree(base,destination)
    (destination/path).write_text("".join(old[:first])+code+"".join(old[last:]),encoding="utf-8")


def schema(**fields):
    return {"type":"object","properties":fields,"required":list(fields),"additionalProperties":False}


def consult(run, label, model, prompt, fields, board):
    out = run/(label+".json")
    inputs = run/(label+"-input.json")
    receipt = run/(label+"-job.json")
    messages = prompt if isinstance(prompt, list) else [{"role":"user","content":prompt}]
    if not messages or any(m.get("role") not in {"system","user","assistant"} or
                           not isinstance(m.get("content"),str) for m in messages):
        raise ValueError("Invalid model messages")
    if model == DIRECTOR:
        messages = messages + [{"role":"user","content":
            "After thinking, return your final answer as exactly one JSON object matching this schema. "
            "Use the required property names and permitted values. "+
            json.dumps(fields,ensure_ascii=False)}]
    req = {"client":ACTOR,"project":"model-research","operation_id":run.name+"-"+label,
           "device":"gpu","path":"/api/chat","task_label":"LeanLean "+label,"request_seconds":240,
           "payload":{"model":model,"stream":False,"messages":messages,
                      "format":fields,"options":{"temperature":0,"num_ctx":8192,"num_predict":1100}}}
    if model==DIRECTOR:
        req["payload"]["think"] = DIRECTOR_THINK
        req["payload"]["options"]["num_predict"] = DIRECTOR_NUM_PREDICT
    if inputs.exists() and read(inputs)!=req:
        raise ValueError("Cannot reuse an operation ID with changed input")
    put(inputs,req)
    if not out.exists():
        with caller(ACTOR,"model-research"):
            client=configured()
            if client is None:
                raise RuntimeError("Lab coordinator unavailable")
            if not receipt.exists():
                job=client.request("/v1/inference",req)
                put(receipt,{"id":job["id"],"submitted":time.time()})
            job=read(receipt)
            reply=client.wait(job["id"],seconds=900)
        if reply["model"]!=model:
            raise RuntimeError("Coordinator returned a different model")
        raw_path = run/(label+"-response.json")
        put(raw_path,reply)
        content=reply["message"]["content"].rsplit("</think>",1)[-1].strip()
        put(out,{"model":model,"job":job["id"],"content":content,"input_tokens":reply.get("prompt_eval_count"),
                 "output_tokens":reply.get("eval_count"),"elapsed_seconds":time.time()-job["submitted"],
                 "done_reason":reply.get("done_reason"),
                 "thinking_requested":req["payload"].get("think"),
                 "thinking_returned":bool(reply["message"].get("thinking")) or "</think>" in reply["message"]["content"],
                 "response_sha256":digest(raw_path)})
        board.record_job_event(run.name,job_id=job["id"],state=label,evidence={"receipt":str(out),"sha256":digest(out)})
    saved = read(out)
    if saved.get("done_reason") == "length":
        raise ValueError("Model response exhausted its generation budget")
    return json.loads(saved["content"])


def forecast_result(prediction, facts):
    """Compute forecast reporting solely from an independently verified grade."""
    verified = facts.get("build_and_preservation_passed") is True
    saved = facts.get("tokens_saved", 0) if verified else 0
    met = saved >= prediction
    report = (f"Forecast {'met' if met else 'missed'}. Predicted {prediction} source tokens saved; "
              f"verified saving {saved}; shortfall {max(0, prediction-saved)}.")
    return {"predicted_tokens_saved":prediction,"verified_tokens_saved":saved,
            "forecast_met":met,"shortfall":max(0,prediction-saved),
            "source":"independent_measurements","report":report}


def body_key(body):
    lines = body.strip("\n").splitlines()
    indent = min((len(line)-len(line.lstrip(" ")) for line in lines if line.strip()), default=0)
    normalized = "\n".join(line[indent:].rstrip() for line in lines)
    return hashlib.sha256(normalized.encode()).hexdigest()


def compiler_feedback(facts):
    """Keep exact diagnostics prominent and omit repeated build command lines."""
    blocks = []
    for log in facts.get("diagnostics", []):
        matches = list(re.finditer(r"(?m)^error: ", log))
        if not matches and log.strip() and log.strip() not in blocks:
            blocks.append(log.strip())
        for match in matches:
            lines = log[match.start():].splitlines()
            selected = [lines[0]]
            for line in lines[1:]:
                if re.match(r"^(error:|trace:|✔|✖|Some required|Building |- )", line):
                    break
                selected.append(line)
            block = "\n".join(selected).strip()
            if block not in blocks:
                blocks.append(block)
    errors = [block.splitlines()[0] for block in blocks]
    source_errors = [line for line in errors if re.match(r"error: .*\.lean:\d+",line)]
    return {"errors": source_errors or errors,
            "diagnostics": blocks, "rejection": facts.get("proposal_error", facts.get("rejection")),
            "verified": facts.get("build_and_preservation_passed", False)}


def worker_messages(target, assignment, history, keep_steps=()):
    """Append actual rejected replies followed by their independent diagnostics."""
    messages = [{"role":"user","content":
        "You are the delegated Lean 4 worker. Return a shorter proof body after ':= by'. "
        "Only this body may change. Preserve the declaration and each locked tactic. "
        "Respond with your complete corrected body and a brief reason.\n"+
        json.dumps({"original_proof":target,"locked_tactics":list(keep_steps)},ensure_ascii=False)}]
    for attempt in history:
        messages.append({"role":"assistant","content":attempt["worker_output"]})
        messages.append({"role":"user","content":
            "Independent verification rejected this submission. Use the compiler's feedback to correct it. "
            "Repeating the same body will be rejected before grading.\n"+
            json.dumps({"rejected_body":attempt["body"],"feedback":compiler_feedback(attempt["grade"])},
                       ensure_ascii=False)})
    messages.append({"role":"user","content":"Sabrina's current assignment\n"+
                     json.dumps(assignment,ensure_ascii=False)})
    return messages


def grade_facts(job, baseline_tokens, key, archive_sha):
    if job["key"]!=key or job["submission_sha256"]!=archive_sha:
        raise ValueError("Grading result belongs to a different artifact or problem")
    before=job["metrics"]["before"]["lean_tokens"]
    after=job["metrics"]["after"]["lean_tokens"]
    if before!=baseline_tokens or type(after) is not int or after<0:
        raise ValueError("Baseline token mismatch or invalid candidate measurement")
    checks=job.get("checks",{})
    if set(checks)!={"before","after"} or checks["before"].get("status")!="passed" or checks["before"].get("container_returncode")!=0:
        raise RuntimeError("Official baseline verification unavailable or failed")
    after_check=checks["after"]
    if after_check.get("status") not in {"passed","failed"}:
        raise RuntimeError("Candidate verification incomplete")
    verified=after_check["status"]=="passed" and after_check.get("container_returncode")==0
    if job.get("verified") is not verified or job["status"]!=("verified" if verified else "verification_failed"):
        raise ValueError("Contradictory official verdict")
    return {"baseline_tokens":before,"candidate_tokens":after,"tokens_saved":before-after,
            "build_and_preservation_passed":verified,"improved":verified and after<before,
            "eligible_compression_pct":100*(before-after)/before if verified else 0}


def official_grade(args, run, frozen, meta, n):
    bundle=args.bundle.resolve()
    manifest=read(bundle/"manifest.json")
    if manifest["status"]!="complete" or read(bundle/"eval-runtime.json")["bundle_sha256"]!=digest(bundle/"manifest.json"):
        raise RuntimeError("Official verified bundle or evaluation image is incomplete")
    hits=[(i,e) for i,e in enumerate(manifest["projects"],1) if e["key"]==meta["id"]]
    if len(hits)!=1:
        raise ValueError("Problem missing or ambiguous in official bundle")
    idx,entry=hits[0]
    if entry["status"]!="verified" or entry["commit"]!=meta["commit"]:
        raise ValueError("Official source revision mismatch")
    output=run/f"grade-{n}"
    candidate=run/f"candidate-{n}"
    changed=read(run/f"edit-{n}.json")
    if revision(candidate)!=changed["revision"]:
        raise ValueError("Edited source changed after collection")
    base=bundle/entry["project_directory"]
    rel=Path(entry["project_path"])/changed["file"]
    if digest(base/rel)!=digest(frozen/changed["file"]):
        raise ValueError("Selected source differs from the prepared baseline")
    submitted=run/f"submission-{n}"
    binding=run/f"submission-{n}.json"
    if not binding.exists():
        if submitted.exists():
            raise RuntimeError("Partial submission exists; inspect before resuming")
        shutil.copytree(base,submitted)
        shutil.copy2(candidate/changed["file"],submitted/rel)
        put(binding,{"revision":revision(submitted),"bundle":digest(bundle/"manifest.json"),
                     "runtime":digest(bundle/"eval-runtime.json"),"candidate":changed["revision"]})
    expected={"revision":revision(submitted),"bundle":digest(bundle/"manifest.json"),
              "runtime":digest(bundle/"eval-runtime.json"),"candidate":changed["revision"]}
    if read(binding)!=expected:
        raise RuntimeError("Frozen submission or grader inputs changed")
    env=dict(os.environ,PYTHONPATH=str(args.upstream.resolve()/"src"),PYTHONNOUSERSITE="1")
    cmd=[str(args.grader_python.absolute()),"-m","leanlean.postprocess","--submission",str(submitted),
         "--problem",str(idx),"--bundle",str(bundle),"--output",str(output),"--workers","1"]
    if (output/"plan.json").exists():
        cmd.append("--resume")
    with (run/f"grade-{n}.log").open("ab") as log:
        proc=subprocess.run(cmd,env=env,stdout=log,stderr=subprocess.STDOUT)
    jp=output/"repositories"/meta["id"]/"result.json"
    if not jp.exists():
        raise RuntimeError(f"Official grader has no result (exit {proc.returncode}); see {log.name}")
    job=read(jp)
    plan=read(output/"plan.json")
    archive=Path(job["submission_archive"])
    if not archive.is_relative_to(output) or plan["bundle_sha256"]!=expected["bundle"]:
        raise ValueError("Grade references a different artifact or bundle")
    archive_sha=digest(archive)
    if archive_sha!=plan["submission_input"]["sha256"]:
        raise ValueError("Official archive checksum mismatch")
    facts=grade_facts(job,meta["baseline_tokens"],meta["id"],archive_sha)
    if not facts["build_and_preservation_passed"]:
        stage=jp.parent/"after"
        files=[stage/(name+suffix+".log") for name in ("build","comparator") for suffix in (".stdout",".stderr")]
        facts["diagnostics"]=[p.read_text(encoding="utf-8",errors="replace")[-1800:] for p in files if p.is_file() and p.stat().st_size]
    if proc.returncode!=int(not facts["build_and_preservation_passed"]):
        raise RuntimeError("Official process status disagrees with the result")
    return facts


def commit(root):
    return subprocess.check_output(["git","-C",str(root),"rev-parse","HEAD"],text=True).strip()


def failed_handoff(parent, contract):
    """Bind a new experiment to one completed attempt on the same frozen project."""
    original = read(parent/"contract.json")
    for name, value in contract.items():
        if original.get(name) != value:
            raise ValueError(f"Previous pilot differs in {name}")
    final = read(parent/"final.json")
    if final["state"] != "closed_without_acceptance" or not final["outcomes"]:
        raise ValueError("A correction needs a closed, unsuccessful pilot")
    last = final["outcomes"][-1]
    n = last["attempt"]
    if type(n) is not int or n not in (1, 2):
        raise ValueError("Invalid parent attempt")
    grade_path = parent/f"measurement-{n}.json"
    if read(grade_path) != last["grade"] or last["grade"].get("improved") is not False:
        raise ValueError("Parent measurement does not confirm a failed attempt")
    reply_path = parent/f"worker-{n}.json"
    reply = read(reply_path)
    if reply["model"] != contract["worker"]:
        raise ValueError("Parent reply belongs to a different worker model")
    if "correct_from" in original:
        target = original["correct_from"]["target"]
    else:
        target = json.loads(read(parent/"assignment.json")["content"])["target"]
    link = {"episode":parent.name, "attempt":n, "target":target, "contract":digest(parent/"contract.json"),
            "final":digest(parent/"final.json"), "measurement":digest(grade_path), "worker_reply":digest(reply_path)}
    evidence = {"worker_output":reply["content"], "measurement":last["grade"], "director_review":last["director"]}
    return link, evidence


def correction_brief(run, n, target, previous, board):
    steps = [line.strip() for line in target["body"].splitlines() if line.strip()]
    text = {"type":"string","minLength":12}
    fields = {"step":{"type":"string","enum":steps}, "mathematical_role":text,
              "why_shortenable":text, "worker_instruction":text}
    if n == 1:
        fields["forecast_tokens_saved"] = {"type":"integer","minimum":1}
    prompt = (
        "You are Sabrina, directing a Lean 4 mathematician after a failed attempt. "
        "Select one exact step in the baseline proof. Explain what that step establishes here, "
        "why it could be eliminated or combined with another step, and what fact the worker must "
        "still establish. Name the relevant hypotheses or expressions in your instructions. "
        "If a candidate failed compilation, address the specific Lean diagnostic. "
        "Give the mathematical assignment in words. The worker chooses and writes its own Lean tactics. "
        "Forecast savings in scored Lean source tokens before the first attempt.\n"
        + json.dumps({"proof":target,"previous_attempt":previous},ensure_ascii=False)
    )
    brief = consult(run,f"brief-{n}",DIRECTOR,prompt,schema(**fields),board)
    if brief.get("step") not in steps:
        raise ValueError("Director's cited step is absent from the baseline proof")
    for key in ("mathematical_role","why_shortenable","worker_instruction"):
        if not isinstance(brief.get(key),str) or len(brief[key].strip()) < 12:
            raise ValueError(f"Director's brief is missing {key}")
    return brief


def diagnose_brief(run, n, target, previous, board, evidence):
    """Let the director compare its earlier claim with an observed Lean goal transition."""
    text = {"type":"string"}
    fields = {k:text for k in ("mistaken_assumption","observed_equality","goal_before","goal_after",
                               "remaining_obligation","revised_worker_task")}
    if n == 1:
        fields["forecast_tokens_saved"] = {"type":"integer","minimum":1}
    msg = (
        "You are Sabrina, the research director. Reassess your own previous claim about the cited "
        "Lean tactic. You have the real error from a failed edit and Lean's recorded local states "
        "on the accepted original. Read the two turnstiles in execution order. State what you "
        "previously got wrong, quote the local equality and both specific goals, and explain what "
        "any correct proof must do. Now give the coding worker a revised mathematical task. "
        "The worker may change a different step of the same proof if appropriate. Prioritize "
        "a correct proof with fewer scored tokens. Leave the Lean implementation to the worker. "
        "On a retry, account for the most recent worker output and verifier evidence as well."
    )
    inputs = {"original_proof":target, "failure_evidence":evidence, "latest_attempt":previous}
    brief = consult(run,f"diagnosis-{n}",DIRECTOR,msg+"\n"+json.dumps(inputs,ensure_ascii=False),schema(**fields),board)
    if not all(isinstance(brief.get(k),str) and brief[k].strip() for k in fields if k!="forecast_tokens_saved"):
        raise ValueError("Incomplete director diagnosis")
    return brief


def repair_evidence(path, contract, choices, meta):
    evidence = read(path)
    if any(evidence[key] != contract[key] for key in ("source_sha256", "matharena_commit")):
        raise ValueError("Repair evidence source or grader mismatch")
    target = choices[evidence["target"]]
    history = []
    for sample in evidence["samples"]:
        folder, n = Path(sample["run"]), sample["attempt"]
        for relative, sha in sample["hashes"].items():
            if digest(folder/relative) != sha:
                raise ValueError("Repair receipt changed")
        reply = read(folder/f"worker-{n}.json")
        if reply["model"] != contract["worker"]:
            raise ValueError("Repair worker mismatch")
        candidate = folder/f"candidate-{n}"
        if revision(candidate) != read(folder/f"edit-{n}.json")["revision"]:
            raise ValueError("Rejected candidate changed")
        options = proof_options(candidate, meta["challenge_path"])
        matches = [value for value in options.values() if value["file"]==target["file"]
                   and value["declaration"]==target["declaration"]]
        if len(matches) != 1:
            raise ValueError("Rejected proof is ambiguous")
        job = read(folder/f"grade-{n}"/"repositories"/meta["id"]/"result.json")
        facts = grade_facts(job, meta["baseline_tokens"], meta["id"], digest(Path(job["submission_archive"])))
        measured = read(folder/f"measurement-{n}.json")
        if facts["build_and_preservation_passed"] or any(measured.get(key) != value for key,value in facts.items()):
            raise ValueError("Expected a matching failed official grade")
        history.append({"worker_output":reply["content"],"body":matches[0]["body"],"grade":measured})
    if not history:
        raise ValueError("Repair requires a verified failure")
    return target, history, evidence.get("keep_steps", [])


def repair_brief(run, n, target, history, keep_steps, board):
    errors = compiler_feedback(history[-1]["grade"])["errors"]
    fields = {"diagnosis":{"type":"string"}, "worker_instruction":{"type":"string"}}
    if errors:
        fields["observed_error"] = {"type":"string","enum":errors}
    return consult(run,f"repair-brief-{n}",DIRECTOR,
        "You are Sabrina. Diagnose the last rejected Lean proof using the exact compiler feedback. "
        "Explain why that edit fails and delegate a specific correction. Keep the locked tactics. "
        "Use the latest error, account for prior rejected bodies and avoid repeating them.\n"+
        json.dumps({"baseline":target,"keep_steps":keep_steps,"last_rejected_body":history[-1]["body"],
                    "last_feedback":compiler_feedback(history[-1]["grade"])},ensure_ascii=False),
        schema(**fields),board)


def workflow(args):
    run=args.run.resolve()
    project=args.published.resolve()
    frozen=project/"base"
    meta=read(project/"problem.json")
    contract={"project":meta["id"],"published_problem":meta["problem_idx"],"source_sha256":revision(frozen),
              "registry":digest(project/"problem.json"),"published_commit":commit(project),
              "matharena_commit":commit(args.upstream),"max_attempts":2,"director":DIRECTOR,"worker":args.worker,
              "goal":"Reduce scored Lean source tokens, retain all protected results and permitted axioms."}
    contract["feedback_version"] = 2
    contract["director_think"] = DIRECTOR_THINK
    contract["director_num_predict"] = DIRECTOR_NUM_PREDICT
    contract["forecast_reporting_version"] = 3
    excluded_targets = sorted(set(getattr(args,"exclude_target",[]) or []))
    if excluded_targets:
        contract["excluded_targets"] = excluded_targets
    repair_path = getattr(args,"repair_evidence",None)
    if repair_path:
        contract["repair_evidence_sha256"] = digest(repair_path)
    parent = getattr(args,"correct_from",None)
    if parent:
        if parent.resolve()==run:
            raise ValueError("Use a fresh run directory for a correction")
        runtime_fields = {"feedback_version","director_think","director_num_predict","forecast_reporting_version"}
        parent_contract = {key:value for key,value in contract.items() if key not in runtime_fields}
        contract["correct_from"], previous_evidence = failed_handoff(parent,parent_contract)
    evidence_path = getattr(args,"lean_evidence",None)
    if evidence_path:
        evidence = read(evidence_path)
        link = contract.get("correct_from",{})
        if not parent or (evidence["parent"],evidence["parent_measurement_sha256"],evidence["target"]) != (
            parent.name,link["measurement"],link["target"]
        ):
            raise ValueError("Diagnostic trace belongs to another failed attempt")
        if evidence["exit_code"] != 0 or not evidence["exact_error"] or not any(
            evidence["exact_error"] in line for line in previous_evidence["measurement"].get("diagnostics",[])
        ):
            raise ValueError("Diagnostic trace does not match the recorded Lean error")
        contract["lean_evidence_sha256"] = digest(evidence_path)
    if (run/"contract.json").exists() and read(run/"contract.json")!=contract:
        raise RuntimeError("Run contract changed. Use a fresh run directory.")
    put(run/"contract.json",contract)
    board=ResearchBlackboard()
    if not board.get_episode(run.name):
        board.create_episode(episode_id=run.name,goal=contract["goal"],challenge=meta["title"],scope=contract,
                             rubric={"verifier":"MathArena LeanLean postprocess","required":"verified positive savings"},
                             budget={"worker_attempts":2},dm_model=DIRECTOR,worker_model=args.worker,
                             base_revision=contract["source_sha256"],
                             parent_episode_id=parent.name if parent and board.get_episode(parent.name) else None)
    episode=board.get_episode(run.name)
    if episode["scope"]!=contract:
        raise RuntimeError("Research episode ID already belongs to another contract")
    if (run/"final.json").exists():
        return read(run/"final.json")
    if episode["status"]=="draft":
        board.publish(run.name)
    if board.get_episode(run.name)["status"]=="ready":
        board._transition(run.name,"ready","claimed",ACTOR,"claimed",extra={"claimed_by":ACTOR,"lease_until":None})
    if board.get_episode(run.name)["claimed_by"]!=ACTOR:
        raise RuntimeError("Research episode belongs to another worker")
    text={"type":"string"}
    enum=lambda values: {"type":"string","enum":list(values)}
    choices=proof_options(frozen,meta["challenge_path"])
    if excluded_targets:
        unknown = set(excluded_targets)-choices.keys()
        if unknown:
            raise ValueError(f"Unknown excluded targets: {sorted(unknown)}")
        choices = {key:value for key,value in choices.items() if key not in excluded_targets}
    if not choices:
        raise ValueError("No eligible proof bodies in selected baseline")
    history = []
    keep_steps = []
    if repair_path:
        target, history, keep_steps = repair_evidence(repair_path,contract,choices,meta)
        if any(step not in (line.strip() for line in target["body"].splitlines()) for step in keep_steps):
            raise ValueError("Locked tactic absent from baseline")
        assignment = repair_brief(run,1,target,history,keep_steps,board)
        prediction = 1
    elif parent:
        target=choices[contract["correct_from"]["target"]]
        if evidence_path:
            if digest(frozen/target["file"])!=evidence["source_sha256"] or evidence["baseline_step"] not in (
                line.strip() for line in target["body"].splitlines()
            ):
                raise ValueError("Lean trace does not match the selected baseline proof")
            assignment=diagnose_brief(run,1,target,previous_evidence,board,evidence)
        else:
            assignment=correction_brief(run,1,target,previous_evidence,board)
        prediction=assignment["forecast_tokens_saved"]
    else:
        plan=consult(run,"assignment",DIRECTOR,
            "Manage a Lean 4 proof-compression worker. Choose one target, explain the mathematical opportunity, "
            "delegate one proof-body rewrite and forecast positive Lean source tokens saved across the project. "
            "Protect all statements and mathematical meaning. These are actual excerpts: "+json.dumps(choices,ensure_ascii=False),
            schema(target=enum(choices),assignment=text,forecast_tokens_saved={"type":"integer","minimum":1}),board)
        target=choices[plan["target"]]
        prediction=plan["forecast_tokens_saved"]
        assignment=plan["assignment"]
    if type(prediction) is not int or prediction<1:
        raise ValueError("Invalid forecast")
    if parent and not history:
        old_body = json.loads(previous_evidence["worker_output"]).get("body", "")
        history = [{"worker_output":previous_evidence["worker_output"],"body":old_body,
                    "grade":previous_evidence["measurement"]}]
    previous=""
    seen_bodies = {body_key(target["body"]), *(body_key(item["body"]) for item in history)}
    outcomes=[]
    for n in (1,2):
        edit=run/f"edit-{n}.json"
        if repair_path and n==2:
            assignment = repair_brief(run,n,target,history,keep_steps,board)
        elif parent and n==2:
            if evidence_path:
                assignment=diagnose_brief(run,n,target,json.loads(previous),board,evidence)
            else:
                assignment=correction_brief(run,n,target,json.loads(previous),board)
        elif n==2:
            assignment = {"original_assignment":assignment,
                          "sabrina_correction":outcomes[-1]["director"]["reason"]}
        if not edit.exists():
            try:
                messages = worker_messages(target,assignment,history,keep_steps)
                put(run/f"feedback-{n}.json",{"history_count":len(history),
                    "latest_feedback":compiler_feedback(history[-1]["grade"]) if history else {},
                    "messages_sha256":hashlib.sha256(json.dumps(messages,ensure_ascii=False).encode()).hexdigest()})
                fields = {"body":text,"reason":text}
                errors = compiler_feedback(history[-1]["grade"])["errors"] if history else []
                if errors:
                    fields["error_addressed"] = {"type":"string","enum":errors}
                proposal=consult(run,f"worker-{n}",args.worker,messages,schema(**fields),board)
                if body_key(proposal["body"]) == body_key(target["body"]):
                    raise ValueError("Worker returned the original proof body")
                if body_key(proposal["body"]) in seen_bodies:
                    raise ValueError("Previously rejected proof body")
                if any(sum(line.strip()==step for line in proposal["body"].splitlines()) != 1
                       for step in keep_steps):
                    raise ValueError("Locked tactic missing or repeated")
                dest=run/f"candidate-{n}"
                apply_edit(frozen,dest,target,proposal["body"])
                put(edit,{"file":target["file"],"revision":revision(dest)})
            except (ValueError,KeyError) as exc:
                put(edit,{"invalid":str(exc)})
        item=read(edit)
        if "invalid" in item:
            facts={"improved":False,"proposal_error":item["invalid"]}
        else:
            if args.stage=="propose" or not (args.bundle/"eval-runtime.json").is_file():
                return {"state":"awaiting_official_grader","attempt":n,"candidate":item}
            facts=official_grade(args,run,frozen,meta,n)
        put(run/f"measurement-{n}.json",facts)
        worker_output = read(run/f"worker-{n}.json")["content"]
        try:
            returned_body = json.loads(worker_output).get("body","")
        except (ValueError, AttributeError):
            returned_body = ""
        history.append({"worker_output":worker_output,"body":returned_body,"grade":facts})
        seen_bodies.add(body_key(returned_body))
        forecast = forecast_result(prediction,facts)
        put(run/f"forecast-{n}.json",forecast)
        raw_review=consult(run,f"review-{n}",DIRECTOR,
            "Inspect the independent measurements and authoritative forecast result. Choose "
            "accept, revise or stop and say exactly why. If revising, give the worker actionable feedback. "+
            "A passing edit can be accepted even when it misses the forecast. The computed forecast result "
            "must be reported accurately. "+
            json.dumps({"forecast":forecast,"attempts_left":2-n,"evidence":facts},ensure_ascii=False),
            schema(action=enum(("accept","revise","stop")),
                   forecast_met={"type":"boolean","enum":[forecast["forecast_met"]]},reason=text),board)
        met = forecast["forecast_met"]
        review = dict(raw_review,forecast_met=met,forecast_source="independent_measurements")
        outcomes.append({"attempt":n,"grade":facts,"director":review,"forecast_actually_met":met,
                         "forecast":forecast,"director_reported_forecast_met":raw_review.get("forecast_met"),
                         "director_forecast_correct":raw_review.get("forecast_met") is met})
        previous=json.dumps({"original_worker_output":read(run/f"worker-{n}.json")["content"],
                             "grade":facts,"director":review},ensure_ascii=False)
        if review["action"]!="revise" or n==2:
            break
    accepted=outcomes[-1]["grade"]["improved"] and outcomes[-1]["director"]["action"]=="accept"
    summary={"state":"accepted" if accepted else "closed_without_acceptance","outcomes":outcomes,
             "accepted_candidate":str(run/f"candidate-{n}") if accepted else None,"activated":False}
    board.record_job_event(run.name,job_id=run.name,state="leanlean_finished",evidence=summary)
    if board.get_episode(run.name)["status"]=="claimed":
        board.submit_attempt(run.name,worker_id=ACTOR,summary=summary["state"],attempt_ref=str(run),
                             evidence_refs=[str(run/f"measurement-{n}.json"),str(run/f"review-{n}.json")],
                             model_revision=args.worker)
    if board.get_episode(run.name)["status"]=="attempted":
        board.evaluate(run.name,evaluator="matharena-leanlean",evaluator_version=contract["matharena_commit"],
                       score=float(accepted),accepted=accepted,rationale="Independent grade and director decision")
    put(run/"final.json",summary)
    return summary


def main():
    import fcntl
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("published","upstream","bundle","grader-python","run"):
        p.add_argument("--"+name,type=Path,required=True)
    p.add_argument("--stage",choices=["propose","full"],default="full")
    p.add_argument("--worker",default=WORKER)
    p.add_argument("--correct-from",type=Path,help="Pin the failed pilot's target and delegate a proof-specific correction")
    p.add_argument("--lean-evidence",type=Path,help="Reassess the failed edit against Lean states captured around the disputed tactic")
    p.add_argument("--repair-evidence",type=Path,help="Verified failed candidates for an explicit compiler-feedback correction")
    p.add_argument("--exclude-target",action="append",default=[],help="Exclude a previously attempted proof from fresh selection")
    args=p.parse_args()
    if args.repair_evidence and (args.correct_from or args.lean_evidence):
        p.error("--repair-evidence starts a separate repair from its recorded failures")
    if args.exclude_target and (args.repair_evidence or args.correct_from or args.lean_evidence):
        p.error("--exclude-target applies only to fresh proof selection")
    args.run.mkdir(parents=True,exist_ok=True)
    with (args.run/".pilot.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            state=workflow(args)
        except Exception as exc:
            put(args.run/"status.json",{"state":"blocked","error":f"{type(exc).__name__}: {exc}"})
            raise
        put(args.run/"status.json",state)
        print(json.dumps(state,ensure_ascii=False))


if __name__=="__main__":
    main()
