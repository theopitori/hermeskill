"""Shared dev-only API key constants for control-plane tests."""

DEV_DEVELOPER_KEY = "sk_dev_developer_local_only_do_not_ship"
DEV_OPERATOR_KEY = "sk_dev_operator_local_only_do_not_ship"
DEV_HEADERS = {"Authorization": f"Bearer {DEV_DEVELOPER_KEY}"}
OP_HEADERS = {"Authorization": f"Bearer {DEV_OPERATOR_KEY}"}
