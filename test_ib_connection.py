"""Quick test: IB Gateway connection + real-time quotes for MES1! and MNQ1!"""
from ib_insync import IB, Future, util

util.startLoop()

ib = IB()
ib.connect('127.0.0.1', 4002, clientId=1)
print(f"Connected: {ib.isConnected()}")
print(f"Account: {ib.managedAccounts()}")

# 3 = delayed, 4 = delayed-frozen (works without live data subscription)
ib.reqMarketDataType(3)

contracts = [
    Future('MES', exchange='CME', currency='USD'),
    Future('MNQ', exchange='CME', currency='USD'),
]

for c in contracts:
    details = ib.reqContractDetails(c)
    if not details:
        print(f"{c.symbol}: no contract details found")
        continue
    qualified = details[0].contract
    print(f"\n{qualified.symbol} — localSymbol={qualified.localSymbol} conId={qualified.conId}")

    ticker = ib.reqMktData(qualified, '', False, False)
    ib.sleep(2)  # wait for snapshot to arrive
    print(f"  [realtime] bid={ticker.bid}  ask={ticker.ask}  last={ticker.last}  close={ticker.close}")
    ib.cancelMktData(qualified)

    bars = ib.reqHistoricalData(
        qualified,
        endDateTime='',
        durationStr='5 D',
        barSizeSetting='1 hour',
        whatToShow='TRADES',
        useRTH=False,
        formatDate=1,
    )
    if bars:
        print(f"  [historical 1H, last 5 bars]")
        for b in bars[-5:]:
            print(f"    {b.date}  O={b.open} H={b.high} L={b.low} C={b.close} V={b.volume}")

ib.disconnect()
