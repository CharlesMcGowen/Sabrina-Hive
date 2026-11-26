"""Resume external authors and their ego evaluations without replaying requests."""
import json
from pathlib import Path
import subprocess
import time

from .common import digest, encoded, load, save
from .recruit import BRIEF, pollinations_exchange

REMOTE = "/home/ego/bot/Sabrina-windows-sync/data/architecture_research/math-proof-v1"


def remote_read(relative):
    if not all(c.isalnum() or c in "-_./" for c in relative) or ".." in relative:
        raise ValueError("Invalid receipt path")
    proc = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "ego@ego.local",
                           f"cat {REMOTE}/{relative}"], capture_output=True, timeout=20)
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def upload(folder):
    dest = f"{REMOTE}/submissions/{folder.name}"
    subprocess.run(["ssh", "-o", "BatchMode=yes", "ego@ego.local", f"mkdir -p {dest}"], check=True, timeout=15)
    subprocess.run(["scp", "-q", str(folder / "generator.json"),
                    f"ego@ego.local:{dest}/generator.json"], check=True, timeout=25)
    subprocess.run(["scp", "-q", str(folder / "generator.py"),
                    f"ego@ego.local:{dest}/.generator.pending"], check=True, timeout=25)
    subprocess.run(["ssh", "-o", "BatchMode=yes", "ego@ego.local",
                    f"mv {dest}/.generator.pending {dest}/generator.py"], check=True, timeout=15)


def run_collective(run):
    if (run / "completed.json").exists():
        print(load(run / "completed.json"))
        if not (run / "published" / "bundle-hashes.json").exists():
            from .publish import publish
            publish(run)
        return
    from tools.portal_policy import PortalPolicy
    from tools import portal_transport
    from tools.recruited_ssm_campaign import request
    records = sorted((run / "recruits").glob("*.json"))
    successful = [load(p) for p in records if not p.name.endswith(("-intent.json", "-diagnostic.json"))
                  and load(p).get("response", {}).get("ok")]
    policy = PortalPolicy()
    previous_source = ""
    for record in successful:
        portal = record["portal"]
        key = digest(portal)[:20]
        folder = run / "submissions" / key
        if not (folder / "generator.py").exists():
            continue
        previous_source = (folder / "generator.py").read_text(encoding="utf-8")
        seen = {digest(previous_source)}
        for round_number in range(1, 14):
            item = folder if round_number == 1 else run / "submissions" / f"{key}-r{round_number}"
            if not (item / "generator.py").exists():
                break
            upload(item)
            while True:
                completed = remote_read("outcomes/" + item.name + ".json")
                if completed:
                    break
                save(run / "controller.json", dict(state="awaiting_development", candidate=item.name, at=time.time()))
                time.sleep(30)
            save(run / "outcomes" / f"{item.name}.json", completed)
            if completed.get("summary", {}).get("passes") or round_number == 13:
                break
            prompt = BRIEF + "\nYour own previous generator:\n" + previous_source + "\nPublic development feedback:\n" + json.dumps(completed)
            request_path = run / "repairs" / f"{key}-{round_number+1}.json"
            if request_path.exists():
                response = load(request_path)
            else:
                intent = request_path.with_suffix(".intent")
                if intent.exists():
                    save(run / "controller.json", dict(state="uncertain_request_requires_reconciliation", portal=portal))
                    return
                time.sleep(50)  # Documented anonymous Pollinations spacing, also safe for other portals.
                eligibility = policy.eligibility(portal, "code_generation", ())
                if not eligibility["eligible"]:
                    break
                save(intent, dict(portal=portal, at=time.time()))
                if portal == "pollinations-openai-fast":
                    purpose = "research_candidate" if eligibility["responsibility"] == "experimental_research" else "research"
                    response = portal_transport.request(policy, portal, prompt, purpose=purpose,
                                exchange=pollinations_exchange, max_tokens=2500)
                else:
                    response = request(portal, prompt, f"math-proof-{key}-repair-{round_number+1}")
                save(request_path, response)
            if not response.get("ok"):
                save(run / "controller.json", dict(state="provider_deferred", response=response))
                return
            try:
                reply = response["reply"].strip()
                if reply.startswith("```"):
                    reply = "\n".join(reply.splitlines()[1:-1])
                doc = json.loads(reply)
                previous_source = doc["source"]
                if not isinstance(previous_source, str):
                    raise ValueError("Generator source must be text")
            except (ValueError, KeyError, TypeError):
                previous_source = response.get("reply", "")
                doc = dict(hypothesis="Unparsed repair; requires compiler feedback")
            if digest(previous_source) in seen:
                save(run / "controller.json", dict(state="duplicate_repair", portal=portal))
                break
            seen.add(digest(previous_source))
            following = run / "submissions" / f"{key}-r{round_number+1}"
            following.mkdir(parents=True, exist_ok=True)
            (following / "generator.py").write_text(previous_source, encoding="utf-8")
            save(following / "generator.json", dict(portal=portal, request_id=response.get("request_id"),
                 hypothesis=doc.get("hypothesis"), reported_model=response.get("observed_model") or response.get("actual_model")))
    # Critique uses a separate currently eligible recruit when available.
    author_portals = {r["portal"] for r in successful}
    with policy.db() as db:
        portals = [r[0] for r in db.execute("SELECT portal_id FROM assistant_external_portals")]
    critics = [p for p in portals if p not in author_portals and policy.eligibility(p, "critique", ())["eligible"]]
    critique = load(run / "critique.json") if (run / "critique.json").exists() else dict(
        state="unavailable", reason="No other eligible critique author")
    if critics and previous_source and not (run / "critique.json").exists():
        critique = request(critics[0], "Critique this printed mathematics policy's assumptions and failure modes. "
                           "No holdout data is available.\n" + previous_source, "math-proof-critique", role="critique")
    save(run / "critique.json", critique)
    marker = run / "dispatch-closed.json"
    save(marker, dict(at=time.time(), successful_author_portals=sorted(author_portals)))
    subprocess.run(["scp", "-q", str(marker), f"ego@ego.local:{REMOTE}/"], check=True, timeout=20)
    save(run / "controller.json", dict(state="awaiting_holdout", at=time.time()))
    while not remote_read("completed.json"):
        time.sleep(30)
    save(run / "completed.json", remote_read("completed.json"))
    from .publish import publish
    publish(run)
