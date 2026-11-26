"""Windows-authoritative recruitment. Requests remain text-only and attributable."""
import json
import time

from .common import digest, load, save
from .goals import freeze

BRIEF = """You are designing Sabrina's original printed mathematics proof policy.
Return exactly JSON with hypothesis (why your action rankings will help) and source
(Python defining build(spec) returning five weight arrays). No training, randomness,
pretrained weights, I/O, examples fitted to labels, or executable operations outside
functions. import math is allowed. Derive weights algorithmically from your proof
strategy. Shapes: up [16,18], up_bias [16], down [6,16], down_bias [6], decay [6].
FP32 finite values bounded by 100; decay in [0,1].
Input: 12 syntactic features followed by 6 decayed counts of failed actions.
Features in order: bias, has_hypotheses, has_recurrence, equality, inequality,
degree0, degree1, degree2, degree3, has_nat, has_power, has_add.
Actions in order: rfl, simp_all, rewrite_assumptions_then_ring, ring, omega,
induction_then_simp_and_linear_combination.
Architecture: relu(input @ up.T + up_bias) @ down.T + down_bias.
The state decays by your decay weights after a failed attempt and that action's
failure count receives +1. The policy reranks untried actions.
Supported tasks are polynomial identities, substitutions, linear integer
inequalities, and additive recurrences. Induction is only meaningful with a
recurrence; rewriting uses supplied assumptions. Choose the rankings yourself,
including how failure history changes them. The trusted search has a finite budget.
Your source executes offline using just spec, never any theorem answers.
Keep the generator compact, using loops to print arrays. No markdown.
"""


def pollinations_exchange(adapter, prompt, model, limit):
    from tools.portal_transport import read_json
    if adapter.get("url") != "https://text.pollinations.ai/openai" or adapter.get("kind") != "pollinations_openai":
        raise ValueError("Unexpected registered adapter")
    body = read_json(adapter["url"], dict(model=model, messages=[dict(role="user", content=prompt)],
                                        max_tokens=limit, stream=False))
    first = (body.get("choices") or [{}])[0].get("message", {})
    text = first.get("content", "") if first.get("role") == "assistant" else ""
    return dict(raw_answer=text, delivery="observed" if text else "missing",
                observed_model=body.get("model"), failure="" if text else "unrecognized_response")


def campaign(run):
    from tools.portal_policy import PortalPolicy
    from tools.recruited_ssm_campaign import request
    from tools import portal_transport
    policy = PortalPolicy()
    freeze(run)
    with policy.db() as db:
        rows = [dict(x) for x in db.execute("""SELECT d.portal_id,p.verified_model,p.provider
            FROM assistant_external_portals d LEFT JOIN portal_profiles p USING(portal_id)
            ORDER BY d.portal_id""")]
    roster = []
    eligible = []
    for row in rows:
        evidence = policy.eligibility(row["portal_id"], "code_generation", ())
        roster.append({**row, "eligibility": evidence["reason"], "eligible": evidence["eligible"]})
        if evidence["eligible"]:
            eligible.append(row)
    save(run / "roster.json", roster)
    for row in eligible:
        portal = row["portal_id"]
        key = digest(portal)[:20]
        receipt = run / "recruits" / f"{key}.json"
        if receipt.exists():
            continue
        intent = run / "recruits" / f"{key}-intent.json"
        if intent.exists():
            # A client interruption after dispatch is an uncertain outcome.
            continue
        save(intent, dict(portal=portal, at=time.time(), state="dispatching"))
        if portal == "pollinations-openai-fast":
            evidence = policy.eligibility(portal, "code_generation", ())
            purpose = "research_candidate" if evidence["responsibility"] == "experimental_research" else "research"
            response = portal_transport.request(policy, portal, BRIEF, purpose=purpose,
                         exchange=pollinations_exchange, max_tokens=2500)
        else:
            response = request(portal, BRIEF, "math-proof-v1-" + key)
        record = dict(portal=portal, prompt=BRIEF, response=response, received_at=time.time())
        save(receipt, record)
        print(portal, response.get("ok"), response.get("failure"), flush=True)
        if not response.get("ok"):
            continue
        if not response.get("request_id") or response.get("execution_mode") != "live":
            raise ValueError("Missing live attribution")
        text = response.get("reply", "").strip()
        if text.startswith("```"):
            text = "\n".join(text.splitlines()[1:-1])
        try:
            doc = json.loads(text)
            if set(doc) != {"hypothesis", "source"} or not all(isinstance(v, str) for v in doc.values()):
                raise ValueError("Expected hypothesis and source")
        except (ValueError, TypeError):
            save(run / "recruits" / f"{key}-diagnostic.json", dict(error="Malformed generator response"))
            doc = dict(hypothesis="Unparsed draft; requires repair", source=text)
        candidate = run / "submissions" / key
        candidate.mkdir(parents=True, exist_ok=True)
        (candidate / "generator.py").write_text(doc["source"], encoding="utf-8")
        save(candidate / "generator.json", dict(portal=portal, request_id=response["request_id"],
             reported_model=response.get("observed_model") or response.get("actual_model"),
             provenance_ref=response.get("provenance_ref"), hypothesis=doc["hypothesis"]))
