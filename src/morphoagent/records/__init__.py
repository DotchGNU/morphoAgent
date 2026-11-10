"""Researcher-in-the-loop records: proposals, decisions, snapshots and reports.

An agent writes a *proposal* (candidate options with figures, tables and
numbers) for a pipeline stage; the researcher picks an option or rejects the
proposal on the review page, which writes a *decision* and a *snapshot* of the
figure behind it. ``morphoagent report`` turns the snapshots into a slide deck
and a Markdown report. The file formats are specified in docs/protocol.md.
"""

from .protocol import (
    DECISION_SCHEMA,
    PROPOSAL_SCHEMA,
    STAGES,
    Decision,
    DecisionExistsError,
    DecisionRequiredError,
    Evidence,
    OptionIn,
    Proposal,
    Provenance,
    RecordsError,
    StaleProposalError,
    UnknownProposalError,
    decide,
    get_decision,
    import_decision,
    list_pending,
    list_proposals,
    load_proposal,
    manual_snapshot,
    propose,
    records_dir,
    require_decision,
)

__all__ = [
    "DECISION_SCHEMA", "PROPOSAL_SCHEMA", "STAGES", "Decision", "DecisionExistsError", "DecisionRequiredError",
    "Evidence", "OptionIn", "Proposal", "Provenance", "RecordsError", "StaleProposalError", "UnknownProposalError",
    "decide", "get_decision", "import_decision", "list_pending", "list_proposals", "load_proposal", "manual_snapshot",
    "propose", "records_dir", "require_decision",
]
