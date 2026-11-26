"""Attended entry point for the existing Sabrina desktop broker.

Loads the same operator configuration as MCP in a fresh process, so a running
MCP connection need not be killed to adopt model/device/state-directory changes.
"""
import argparse
import json
import os
import sys
from pathlib import Path
import tomllib

WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['run','resume','status','pending','approve','reject','verify','block','audit','decisions','host-audit','performance','list-skills','approve-skill','reject-skill','hydrate-skill'])
    parser.add_argument('--run-id', default='')
    parser.add_argument('--proposal-id', default='')
    parser.add_argument('--skill-name', default='', help='Target skill name for hydration or proposal actions.')
    parser.add_argument('--query', default='', help='Search query for skill hydration.')
    parser.add_argument('--task', default='')
    parser.add_argument('--context-file', default='', help='UTF-8 evidence context for the run; it is copied into the retained audit.')
    parser.add_argument('--reason', default='')
    parser.add_argument('--guidance', default='')
    parser.add_argument('--model', default='', help='Installed Ollama model for Sabrina (defaults to the configured smart 14B model).')
    parser.add_argument('--audit-retention-days', type=int, default=None, help='Desktop audit retention in days (1-365; default 7).')
    parser.add_argument('--evidence', default='')
    parser.add_argument('--after-id', type=int, default=0)
    parser.add_argument('--limit', type=int, default=1)
    parser.add_argument('--think', default='', choices=['true','false',''], help='Explicit think setting applied after loading operator configuration (defaults to true).')
    parser.add_argument('--host-access', default='none', choices=['none','enabled'], help='Opt-in per-run host execution access.')
    parser.add_argument('--cross-app-review', action='store_true', help='Start the measured, read-only Claude/Antigravity/Codex pilot.')
    parser.add_argument('--antigravity-cli', action='store_true', help='Use opt-in Antigravity CLI text consultation instead of desktop interaction.')
    parser.add_argument('--antigravity-cli-suite', default='', choices=['', 'duration-v1'], help='Enable the frozen scratch coding contract and independent verifier.')
    parser.add_argument('--desktop-app', action='append', default=None, help='Exact per-run desktop app identity; may be repeated.')
    parser.add_argument('--category', default='coding_executable_checks', help='Task category for performance lookup.')
    parser.add_argument('--expected-version', default='', help='Exact expected skill version for proposal approval.')
    parser.add_argument('--expected-sha256', default='', help='Full 64-character hex SHA-256 hash for proposal approval.')
    parser.add_argument('--full-desktop', action='store_true', help='Enable the version-pinned command catalog; sensitive facilities remain opt-in.')
    parser.add_argument('--desktop-permission', action='append', choices=['physical_input','visual_observation','clipboard','notifications','app_closure'], default=None)
    args = parser.parse_args()
    if args.antigravity_cli_suite and not args.antigravity_cli:
        parser.error('--antigravity-cli-suite requires --antigravity-cli')
    if args.antigravity_cli and (args.cross_app_review or args.host_access != 'none' or args.desktop_app or args.full_desktop or args.desktop_permission):
        parser.error('--antigravity-cli cannot be combined with desktop or host access')
    config = tomllib.loads((Path.home()/'.codex/config.toml').read_text(encoding='utf-8'))
    env = config['mcp_servers']['sabrina-lab-assistant']['env']
    os.environ.update({k:str(v) for k,v in env.items() if k.startswith('SABRINA_')})
    os.environ['SABRINA_LAB_ASSISTANT_THINK'] = args.think or 'true'
    model = (args.model or os.environ.get('SABRINA_DESKTOP_REVIEW_MODEL')
             or 'auto')
    from tools import sabrina_lab_assistant_mcp as bridge
    if args.action in {'run','resume'}:
        # Guard: check if already active before attempting resume
        if args.action == 'resume' and args.run_id:
            st = bridge._get_coordination().status(args.run_id)
            if st.get('active'):
                result = {
                    "ok": False,
                    "status": "busy",
                    "error": "RUN_ALREADY_ACTIVE",
                    "run_id": args.run_id,
                    "lease_owner": st.get("lease_owner"),
                    "lease_until": st.get("lease_until"),
                    "retry_after_seconds": st.get("retry_after_seconds", 0.0),
                    "message": f"Run {args.run_id} is already active; do not resume concurrently.",
                }
                print(json.dumps(result, indent=2), flush=True)
                return
        selected_apps = args.desktop_app
        if args.cross_app_review and selected_apps is None:
            selected_apps = ['Claude.exe', 'Antigravity.exe', 'Codex.exe']
        if selected_apps is None:
            selected_apps = ['Antigravity.exe','Antigravity IDE.exe','Claude.exe','Codex.exe']
        run_context = Path(args.context_file).read_text(encoding='utf-8') if args.context_file else ''
        result = bridge.ask_sabrina_lab_assistant(task=args.task, run_id=args.run_id,
            desktop_access='none' if args.antigravity_cli else 'autonomous', desktop_apps=[] if args.antigravity_cli else selected_apps, max_tokens=1800,
            context=run_context,
            desktop_permissions=({p: True for p in (args.desktop_permission or [])} if args.full_desktop or args.desktop_permission else None),
            guidance=args.guidance, model=model, desktop_audit_retention_days=args.audit_retention_days,
            host_access=args.host_access, cross_app_review=args.cross_app_review,
            antigravity_cli_access='enabled' if args.antigravity_cli else '',
            antigravity_cli_suite=args.antigravity_cli_suite)
    elif args.action == 'status':
        result = bridge._get_coordination().status(args.run_id)
    elif args.action == 'decisions':
        result = bridge.sabrina_decision_audit(args.run_id, args.after_id, args.limit)
    elif args.action == 'audit':
        result = bridge.sabrina_desktop_audit(args.run_id, args.after_id, args.limit)
    elif args.action == 'host-audit':
        result = bridge.sabrina_host_audit(args.run_id, args.after_id, args.limit)
    elif args.action == 'performance':
        result = bridge.sabrina_performance_lookup(category=args.category, run_id=args.run_id)
    elif args.action == 'block':
        result = bridge._get_coordination().pause_for_operator(args.run_id,args.reason)
    elif args.action == 'verify':
        result = bridge._get_coordination().verify_external_result(args.run_id,args.reason,args.evidence)
    elif args.action == 'pending':
        result = bridge.sabrina_desktop_pending(args.run_id)
    elif args.action == 'list-skills':
        from tools.sabrina_skill_hydration import SabrinaSkillHydration
        result = SabrinaSkillHydration().list_proposals()
    elif args.action == 'approve-skill':
        import re, hashlib
        from tools.sabrina_skill_hydration import SabrinaSkillHydration
        hydrator = SabrinaSkillHydration()
        prop = hydrator.get_proposal(args.proposal_id)
        if not prop:
            result = {'ok': False, 'error': f'Proposal {args.proposal_id} not found'}
        else:
            exp_ver = args.expected_version
            exp_sha = args.expected_sha256
            if not exp_ver or not exp_sha:
                content = prop.get('skill_content', '')
                m_ver = re.search(r"(?m)^version:\s*['\"]?([0-9A-Za-z_.-]+)['\"]?", content)
                calc_ver = m_ver.group(1).strip() if m_ver else '1.0.0'
                calc_sha = hashlib.sha256(content.encode('utf-8')).hexdigest()
                result = {
                    'ok': False,
                    'error': 'MISSING_MANDATORY_VERIFICATION',
                    'detail': f'Approval requires exact --expected-version and full 64-char --expected-sha256. Displayed proposal has version={calc_ver!r}, sha256={calc_sha!r}',
                }
            else:
                result = hydrator.approve_proposal(
                    args.proposal_id,
                    expected_version=exp_ver,
                    expected_sha256=exp_sha,
                    reviewer='operator',
                    feedback=args.reason or 'Approved via CLI',
                )
    elif args.action == 'reject-skill':
        from tools.sabrina_skill_hydration import SabrinaSkillHydration
        result = SabrinaSkillHydration().reject_proposal(args.proposal_id, reason=args.reason or 'Rejected via CLI', reviewer='operator')
    elif args.action == 'hydrate-skill':
        from tools.sabrina_skill_hydration import SabrinaSkillHydration
        hydrator = SabrinaSkillHydration()
        q = args.query or args.task or 'documentation and operational patterns'
        s_name = args.skill_name or 'custom-skill'
        result = hydrator.hydrate_skill_from_search(
            query=q,
            skill_name=s_name,
            description=args.reason or f'Hydrated skill for: {q}',
            task_context=args.task or q,
            trigger_reason='manual_cli_hydration',
            run_id=args.run_id or None,
        )
    else:
        result = bridge.sabrina_desktop_decide(args.run_id,args.proposal_id,
            args.action=='approve',args.reason or 'Exact action reviewed by supervisor for the operator-authorized scheduler review.')
    print(result if isinstance(result,str) else json.dumps(result,indent=2), flush=True)


if __name__ == '__main__': main()
