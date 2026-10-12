# ApprovalBridge

Retailers sometimes pay a supplier less than an invoice and claim that a pricing discount was approved. An analyst must find the approval evidence, check its scope and any later correction, and keep permission separate from recorded settlement. ApprovalBridge investigates that evidence and prepares a source-linked dossier for human review. It does not send disputes, approve its own findings or move money.

## Run locally

Use macOS or Linux with Python 3.12 or newer.

```sh
git clone https://github.com/M4verrick/approvalbridge-neathack.git
cd approvalbridge-neathack/approvalbridge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m pip install --no-deps --no-build-isolation -e .
.venv/bin/approvalbridge --db .local/demo.db serve --port 8765
```

Open `http://127.0.0.1:8765`. Choose **Open sample**, investigate its fictional evidence, apply the available frozen replies, and investigate again when evidence changes. Review the resulting dossier before accepting its exact hash. Use the sample correction to check that an old proposal cannot be accepted after its evidence changes.

The default mode is offline and makes no model API calls. Cases, replies and comparisons are synthetic. Stop the server with Ctrl+C; the SQLite database remains in `.local/` for the next run.

Optional live inference is disabled until the server operator supplies a verified organizer-funded grant, an allowed model route and acknowledged neatlogs delivery. The example grant is deliberately expired, unverified and zero-budget. Do not use a personal funded key or a paid fallback. The app does not automatically load `.env`. Keys must be supplied explicitly through the server process environment and private operator configuration. Keep credentials outside Git; private runtime files require mode `600`. Mail access is also disabled by default. Keep this local prototype bound to localhost.
