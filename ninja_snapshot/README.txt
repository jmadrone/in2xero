ninja_snapshot.py - read-only Invoice Ninja extract
===================================================

Reads your Invoice Ninja instance over the API and prints a month-by-month
picture of invoiced revenue, cash received and expenses, plus AR and
unpaid-bill positions as of today. Writes ninja_snapshot.json.

It makes GET requests only. It does not write to Invoice Ninja, and it does
not touch Xero at all.

REQUIREMENTS
  Python 3.8+  and  requests   ->   pip3 install requests

GET AN API TOKEN
  Invoice Ninja -> Settings -> Account Management -> Integrations -> API tokens
  One token covers one company. Read access is all this needs.

RUN IT (on the Invoice Ninja server, or anywhere that can reach it)

  export IN_BASE_URL=https://invoices.yourhost.com     # no trailing slash
  export IN_API_TOKEN=paste_token_here
  python3 ninja_snapshot.py --start 2025-01-01

OPTIONS
  --start 2025-01-01     window start (default 2025-01-01)
  --end   2026-07-29     window end (default: today)
  --out   somefile.json  output path (default ninja_snapshot.json)
  --page-size 200        records per API page
  --insecure             skip TLS verification (self-signed certs only)

SEND BACK
  ninja_snapshot.json  - that is what gets compared against Xero.
  The printed table is the same data in human-readable form.

TROUBLESHOOTING
  "403 ... token is not valid"
      Invoice Ninja returns 403, not 401, for a bad token. Check the token
      is for the right company and was copied whole.

  "redirected to ..."
      IN_BASE_URL is wrong - usually http instead of https, or a trailing
      slash. Redirects drop the auth header, so the script refuses to follow
      them rather than failing confusingly later.

  SSL certificate errors
      Self-signed cert: add --insecure.

  ModuleNotFoundError: requests
      pip3 install requests   (or: python3 -m pip install --user requests)
