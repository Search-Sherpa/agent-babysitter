"""Process exit codes shared by every agentsitter command."""

OK = 0                  # done; the command may proceed
USAGE = 2               # bad arguments, unusable config or missing tool
RECURSION = 4           # called from inside an advisor; consults do not nest
CONSULT_REQUIRED = 10   # lane 2: a consult must be on record first
CONSULT_FAILED = 11     # the advisor failed or gave no verdict
BUDGET_EXHAUSTED = 12   # a consult budget cap was hit; the approver must reset it
SELF_FAMILY = 13        # the advisor is the builder's own model family
FAMILY_UNKNOWN = 14     # the builder's model family cannot be established
APPROVAL_REQUIRED = 20  # lane 3: a signed human approval is required
APPROVAL_INVALID = 21   # the approval is missing, expired, spent or badly signed
APPROVE_REFUSED = 22    # the approve command refused to sign
