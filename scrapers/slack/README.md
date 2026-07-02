# Slack Email Extractor
Download a list of all users and their email addresses in a Slack workspace.

# Requirements
Python 3.6+

# Installation
```
pip install -r requirements.txt
```

# Authentication
The script needs a Slack token with the `users:read` and `users:read.email`
scopes. Create an app at https://api.slack.com/apps, add those scopes, install
it to your workspace, and copy the token.

Provide the token via the `SLACK_TOKEN` environment variable:

```
export SLACK_TOKEN=xoxb-your-token   # Windows PowerShell: $env:SLACK_TOKEN="xoxb-your-token"
```

or pass it on the command line with `--token`.

# Usage
From the command line, browse to the script directory and run:

```
python emails.py
```

The output is printed to stdout in CSV format (`"real_name",email`). Redirect it
to a file if you want to save it:

```
python emails.py > emails.csv
```

# Notes
This will only return users who have their email address available in their profile.
