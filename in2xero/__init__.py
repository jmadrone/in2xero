"""in2xero - Invoice Ninja -> Xero backfill and incremental sync.

Built for Emerald Security LLC. The design notes live in the Claude project doc
"invoice-ninja-to-xero-design"; the three things most likely to bite are repeated
here because they are invisible in the code otherwise:

1.  Invoice Ninja's `status=active` index filter silently drops ARCHIVED records.
    Archiving is a tidying action, not a void. 318 of this org's 327 invoices are
    archived. Never send that filter - see ninja.Ninja.paginate.

2.  Payments settle to a CLEARING account, never to the fed bank account. The real
    bank feed already carries the Stripe/Venmo deposits; posting payments to the
    same account doubles cash. The clearing balance that remains is processor fees.

3.  Expenses are deliberately not implemented. Xero's bank feed already books more
    expense than Invoice Ninja holds, several Invoice Ninja categories are equity or
    balance-sheet items (Owners Draw, loan principal), and every expense record has a
    blank payment_date. Importing them would double-count and misstate.
"""

__version__ = "3.7.0"
