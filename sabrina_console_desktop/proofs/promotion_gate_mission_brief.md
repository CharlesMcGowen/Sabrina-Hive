Implementation mission: build the promotion ledger. No orders, no credentials, no network. execution_authorized=false.

Deliverable
One file: sabrina_console_desktop/outputs/promotion_gate/promotion_ledger.py
Python 3.12, standard library only. Writes anywhere else in sabrina_console_desktop are blocked by the Engine Self-Protection Guard, so do not try.

Definition of done
All 13 tests in sabrina_console_desktop/test_promotion_ledger_acceptance.py pass. Read that file first: it is the contract, and it cannot be edited. Run from sabrina_console_desktop:
python -m unittest test_promotion_ledger_acceptance

Purpose
Swarm output lands in a speculative log. Only a deterministic checker can move an artifact to the finalized log. Votes, author counts and model agreement never finalize anything, because agents on the same model make the same mistakes together.

Contract

class PromotionLedger:
    def __init__(self, root, checkers)
        root: directory. Create it if missing. Holds speculative.jsonl and finalized.jsonl, one JSON object per line, UTF-8.
        checkers: dict mapping kind -> callable(payload) -> (ok: bool, detail: str).

    def append(self, kind, payload, *, author, slot, parents=()) -> str
        Returns artifact_id: the sha256 hex digest of the canonical JSON (sorted keys) of kind, slot, payload and sorted parents. author is not part of the id.
        Appending the same content again returns the same id and writes nothing.
        Raises ValueError and writes nothing if: payload is not a dict, payload is not JSON-serializable, kind or slot is empty, or any parent id is not already in the speculative log.

    def promote(self, artifact_id) -> dict
        Returns {"ok": bool, "artifact_id": str, "error": str}. Never raises for a bad artifact or a bad checker.
        Refuse, in this order, with the reason in "error":
        1. unknown artifact_id.
        2. another artifact already finalized in the same slot. The error must contain the word "slot" and the id of the finalized artifact.
        3. a parent that is not finalized. The error must contain that parent's id.
        4. no checker registered for the kind.
        5. the checker raised. The error must contain the exception message.
        6. the checker returned ok=False. The error must contain the checker's detail.
        Already finalized: return ok=True and write nothing.
        Every refusal of a known artifact is appended to speculative.jsonl as a rejection record, so the author can retry with the exact error.
        On success append one record to finalized.jsonl with at least: artifact_id, kind, slot, payload, parents, detail, and the fields needed for verify().

    def is_finalized(self, artifact_id) -> bool
    def finalized(self) -> list[dict]          # in promotion order
    def rejections(self, artifact_id) -> list[dict]   # each has "error"
    def verify(self) -> dict                   # {"ok": bool, "error": str}
        finalized.jsonl is a hash chain: each record stores the previous record's hash and its own. verify() returns ok=False if any record was edited, removed or reordered.

All state is read from the two files, so a new PromotionLedger on the same root sees everything an earlier one wrote.

Rules
- Do not report that tests pass unless you ran them and are quoting the output.
- Do not add voting, quorum counting or any promotion path that skips the checker.
- Keep it under 200 lines. No dependencies, no network, no subprocesses.

Output
The file, then at most 100 words on any contract point you could not meet.
