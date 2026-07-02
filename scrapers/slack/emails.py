#!/usr/bin/env python3
"""Download all users and their full profile info from a Slack workspace.

Output is written as a JSON array of Slack member objects (each includes the
full `profile` block and a `workspace` block describing the team the user
belongs to) to `users/<workspace name>.json`. By default only users that have
an email address are included; pass --all to include every member.

Provide a Slack token via the SLACK_TOKEN environment variable, or pass it
with --token. See https://api.slack.com/authentication/token-types for the
token types Slack supports (a user or bot token with the users:read and
users:read.email scopes is required to read email addresses).
"""

import argparse
import json
import os
import re
import sys

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

try:
    # Load a project-local .env file (SLACK_TOKEN=...) if python-dotenv is
    # installed. Existing environment variables take precedence.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def iter_members(client):
    """Yield every member across all paginated pages of users.list."""
    cursor = None
    while True:
        response = client.users_list(cursor=cursor, limit=200)
        for member in response["members"]:
            yield member
        cursor = response.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break


def get_workspace(client):
    """Return workspace (team) info for the token's workspace.

    Prefers team.info (richer: domain, icon, ...) and falls back to auth.test
    when the token lacks the team:read scope.
    """
    try:
        return client.team_info()["team"]
    except SlackApiError:
        auth = client.auth_test()
        return {
            "id": auth.get("team_id"),
            "name": auth.get("team"),
            "url": auth.get("url"),
        }


def safe_filename(name):
    """Turn a workspace name into a safe file name, preserving Unicode letters."""
    # Drop characters that are invalid in file names on common platforms.
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    # Collapse whitespace runs to single underscores.
    cleaned = re.sub(r"\s+", "_", cleaned)
    return cleaned or "workspace"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token",
        default=os.environ.get("SLACK_TOKEN"),
        help="Slack API token (defaults to the SLACK_TOKEN environment variable).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Include all members, not just those with an email address.",
    )
    parser.add_argument(
        "--out-dir",
        default="users",
        help="Directory to write the JSON file into (default: users).",
    )
    args = parser.parse_args()

    if not args.token:
        sys.exit(
            "Error: no Slack token provided. Set the SLACK_TOKEN environment "
            "variable or pass --token."
        )

    client = WebClient(token=args.token)

    try:
        workspace = get_workspace(client)
        members = []
        for user in iter_members(client):
            if args.all or user.get("profile", {}).get("email"):
                user["workspace"] = workspace
                members.append(user)
    except SlackApiError as exc:
        sys.exit(f"Slack API error: {exc.response['error']}")

    os.makedirs(args.out_dir, exist_ok=True)
    filename = safe_filename(workspace.get("name") or workspace.get("id") or "workspace")
    out_path = os.path.join(args.out_dir, f"{filename}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(members, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print(f"Wrote {len(members)} members to {out_path}")


if __name__ == "__main__":
    main()
