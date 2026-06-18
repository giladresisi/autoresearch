# broker_recon — broker↔strategy reconciliation (GIL-36).
#
# Detects and corrects the two live failure modes where the strategy phantom-fills /
# leaves an unprotected position because PickMyTrade exposes no fill-confirmation:
#   1. entry filled but its protective S/L leg was rejected (wrong-side / missing stop)
#   2. the entry order itself was rejected (no position at the broker)
#
# Layers:
#   - tradovate_login.py — shared Playwright login/trading-mode/account-select flow
#     (also used by reports/get_tradovate_orders.py).
#   - reader.py          — persistent, READ-ONLY headed Playwright orders-blotter reader.
#   - reconcile.py       — pure classify(entry, fill, orders) + daemon-thread orchestration
#     that corrects via PMT (live_orders), never via the browser.
