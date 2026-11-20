"""Prints a refreshed (another 60 days) Instagram long-lived token. Used by the weekly workflow."""
import os
import sys

from instagram import refresh_long_lived_token

token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
if not token:
    sys.exit("IG_ACCESS_TOKEN not set")
new, expires = refresh_long_lived_token(token, os.environ.get("IG_GRAPH_HOST") or "graph.instagram.com")
print(f"::add-mask::{new}")
print(f"refreshed, valid for {int(expires or 0)//86400} days", file=sys.stderr)
with open(os.environ.get("GITHUB_OUTPUT", os.devnull), "a") as f:
    f.write(f"token={new}\n")
