"""Export only current recruiter questions for the Windows desktop notifier."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tools.messaging_store import ROOT, digest
from tools.messaging_skills import load_profile
from tools.recruiter_conversation_swarm import MODELS


def pending_questions(board_path: Path, messaging_path: Path, profile_hash: str,
                      models: dict, limit: int = 100) -> list[dict]:
    questions = []
    seen = set()
    with sqlite3.connect(board_path, timeout=5) as board, sqlite3.connect(messaging_path, timeout=5) as messages:
        board.row_factory = sqlite3.Row
        rows = board.execute(
            "SELECT account,thread,context_version,profile_hash,models,status,decision,packet "
            "FROM conversation_review_runs ORDER BY updated DESC LIMIT 500"
        )
        for row in rows:
            identity = (row['account'], row['thread'])
            if identity in seen:
                continue
            seen.add(identity)
            if (row['status'] != 'needs_input' or row['profile_hash'] != profile_hash
                    or json.loads(row['models']) != models):
                continue
            current = messages.execute(
                'SELECT version FROM threads WHERE account=? AND thread=?', identity
            ).fetchone()
            if not current or current[0] != row['context_version']:
                continue
            decision = json.loads(row['decision'] or '{}')
            if decision.get('action') != 'ask_human':
                continue
            packet = json.loads(row['packet'])
            thread = packet['thread']
            missing = [str(item)[:160] for item in decision.get('missing_information', [])[:5]]
            if missing and all(item in {'prior_requests', 'open_questions', 'missing_information'} for item in missing):
                topics = [str(item).replace('_', ' ')[:80] for item in decision.get('role_questions', [])[:3]]
                missing = ["Please review the recruiter's question about " + topic + "." for topic in topics]
            if not missing:
                missing = ['Please review the outstanding recruiter request in this conversation.']
            latest = (thread.get('messages') or [{}])[-1]
            questions.append({
                'snapshot': {
                    'account_id': row['account'], 'thread_id': row['thread'],
                    'peer_name': str(thread.get('peer_name') or 'Recruiter')[:70],
                    'url': str(thread.get('url') or '')[:300],
                    'version': row['context_version'],
                    'messages': [{'id': str(latest.get('id') or '')[:250]}],
                },
                'decision': {
                    'action': 'ask_human', 'missing_information': missing,
                    'reason': str(decision.get('reason') or '')[:200],
                    'next_action': str(decision.get('next_action') or '')[:200],
                },
            })
            if len(questions) >= limit:
                break
    return questions


def main() -> None:
    profile_hash = digest(load_profile())
    questions = pending_questions(
        ROOT / 'data/messaging_blackboard.sqlite3', ROOT / 'data/messaging.sqlite3',
        profile_hash, MODELS,
    )
    print(json.dumps(questions, ensure_ascii=False))


if __name__ == '__main__':
    main()
