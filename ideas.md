# Motivation 

The SMT strategy as it was didn't work well enough, this document defines the concepts for rebuilding it

# Goal

Define how the SMT strategy would work such that a planning agent could create an implementation plan for rebuilding it

# High-Level Concepts

The SMT strategy would generally work as follows:
1. It'd be applied in an optimization / backtesting path (using 1m historical bars) and a realtime path (using 1s realtime bars) sharing most of the code
2. It'd be ran every day during the running period starting 9:20am NY time until end of trading day
3. It'd rely on always-on orchestrator daemons running in the background, each in its own path, and calling various scripts / modules in dedicated times and conditions
4. The exported functions in the scripts / modules shall have a finite run duration
5. Both optimization / backtesting and realtime paths shall make the necessary calculations internally given the outputs from the strategy
6. All scripts / modules shall communicate using various json files that would contain data from the ongoing trading session
7. All graph / bar referenes in this file are of the MNQ1! ticker, in cases where the 2nd ticker is required it's MES1!

# The JSON Files

The json files the modules / scripts shall communicate by:
1. global.json - global data that may be relevant to timeframes spanning multiple days
2. daily.json - daily data that shouldn't be changed for the entire daily session after it was written once
3. hypothesis.json - data related to the next move we're expecting to see, be it the big move or a smaller pre-move
4. position.json - data related to the current open position or to pending limit signals for entering a new position

# The Modules / Scripts

## Daily - daily.py

A script that's called once per daily trading session by the relevant orchestrator daemon at a specific time (9:20am NY time) and does several things:
1. Reads the 'liquidities' property in daily.json, makes several 1-time price calculations of following liquidities from the MNQ1! graph based on today's up-to-date 1m bars data and updates both json files - TDO, TWO, current week's high/low, today's high/low, current 6hr session (Asia, London, NY-morning, NY-evening) high/low, fair-value-gaps from recent trading days (e.g. 3) which are still unvisited (i.e. the graph never went there again since they formed) as can be seen in 1hr bars in TradingView (round bar-init times e.g. 13:00, 14:00 etc.), then updates them in the 'liquidities' property of daily.json
2. Reads the 'all_time_high' property from global.json, compares to today's high and updates in global.json if necessary
3. Estimates what direction will the big move take today (TBD, for now read the 'trend' property in global.json), writes either 'up' / 'down' to the 'estimated_dir' property in daily.json
4. Estimates whether we can expect a smaller pre-move in the opposite direction before the big move (TBD, for now use hardcoded 'no'), write either 'yes' / 'no' to the 'opposite_premove' property of daily.json
5. Writes 'none' for the 'direction' property in hypothesis.json

## Next Move's Hypothesis - hypothesis.py

A script that's called once per 5m during the trading session by the relevant orchestrator daemon and does several things:
1. Exits early if the 'direction' property in hypothesis.json is not 'none'
2. Exits early if both the high and low (edges of top and bottom wicks) of the current 5m bar are above the all-time-high as written in daily.json
3. Based on the bars data:
3.1. Calculates hether we're above or below the mid price (between the extreme high/low) on weekly and daily POV, writes either 'above', 'mid' or 'below' to the 'weekly_mid' and 'daily_mid' properties of hypothesis.json, a <= 10pts diff from the mid price should be considered 'mid'
3.2. Checks which meaningful liquidity (i.e. weekly or daily high/low) did the graph visit last, writes either 'week_high' / 'day_low' etc. to the 'last_liquidity' property of hypothesis.json
3.3. Looks up for 15m / 30m SMT divs between the MNQ1! and MES1! tickers (would be visible in 15m / 30m views in TradingView, round bar-init times e.g. 13:00, 13:15 etc.), SMTs could be either regular (wick-based), hidden (bar-body based) or fill (difference in whether or how both tickers fill a 15m / 30m FVG), writes a divs list in the 'divs' property of hypothesis.json
3.4. Estimates which direction will the next move take (TBD, for now set this to hardcoded 'up'), write either 'up' / 'down' to the 'direction' property of hypothesis.json
3.5. Filters the items in the 'liquidities' property in daily.json leaving only those which are in the direction of the estimated next-move's direction (see 3.4) from the current price, writes these to the 'targets' property of hypothesis.json
3.6. Estimates the target price for the next move (TBD, for now set '', I'll set this myself) in-which we'll change the trading to cautious mode, writes to the 'cautious_price' property of hypothesis.json
3.7. Looks up discount or premium wicks of a 15m / 30m bar from 12hr ago or from a same-time bar exactly a week ago, writes the wicks' price ranges to the 'entry_ranges' property of hypothesis.json

## Trading Strategy - strategy.py

A script that contains the trading strategy itself and is being called by the backtesting / realtime paths on every new bars data after starting after the daily module was called and finished:
1. It reads the expected direction from hypothesis.json (the 'direction' property, either 'up' or 'down')
2. When no open position exists (the 'active' property of position.json is empty) then:
2.1. If we don't have an expected direction (the 'direction' property of hypothesis.json is 'none') or we have 2 failed entries or more so far (the 'failed_entries' property of position.json is > 2) then exit early
2.2. Otherwise (we have an expected direction and less than 2 failed entries):
2.2.1. It checks if there's a new completed 5m bar (accumulate prev 1m bars, use the bars time like the 5m view in TradingView, round bar-init times e.g. 10:00, 13:05 etc.) and whether its body went opposite from the expected direction, if so write it (high/low/body-high/body-low/time) in the 'confirmation_bar' property of position.json (override if a previous bar is already there, it wasn't confirmed)
2.2.2. If such new opposite 5m bar exists then a limit entry order should be placed or the current limit entry order should be moved (if one exists), output either a 'new-limit-entry' signal or a 'move-limit-entry' signal respectfully with the relevant body-end price of the new 5m bar (body-low for shorts, body-high for longs) as an extra param, also update the 'limit-entry' property of position.json with the new price (override if wasn't empty)
2.2.3. If during the current bar we surpassed the current limit entry order, output a 'limit-entry-filled' signal and write the active position's info: [bar's time, fill price == limit-entry price, direction == from hypothesis.json, stop == 5m confirmation bar's opposite wick's end, contracts == 2 (at least for now), cautious == 'no'] to the 'active' property of position.json and clear its 'limit-entry' property
3. When an open position does exist then:
3.1. If we don't have an expected direction (the 'direction' property of hypothesis.json is 'none') or the position's direction (in the 'active' property of position.json) is different from the one in hypothesis.json then hen output a 'market-close' signal with the necessary data for the caller to calculate the p&l and trade record, then clear the 'active' and 'limit-entry' properties in position.json
3.2. Otherwise (same direction), if during the current bar we surpassed the stop price (from the 'active' property in position.json) then output a 'stopped-out' signal with the necessary data for the caller to calculate the p&l and trade record, then clear the 'active' and 'limit-entry' properties in position.json and increment its 'failed_entries' property

## Trend-Change Detection - trend.py

A script that contains the trading strategy itself and is being called by the backtesting / realtime paths on every new bars data after starting after the daily module was called and finished:
1. If we don't have an expected direction (the 'direction' property of hypothesis.json is 'none') then exit early
2. Otherwise (we have an expected direction), if we have an open position  (the 'active' property of position.json isn't empty) then:
2.1. If we are not in cautious mode yet (the cautious field in the 'active' property in position.json is 'no') and during the current bar we surpassed the price in the 'cautious_price' property in hypothesis.json then:
2.1.1. If the closing price of this bar is beyond the 'cautious_price' then update the cautious field in the 'active' property in position.json to 'yes'
2.1.2. Otherwise (closing price returned to pre-cautious), then output a 'market-close' signal with the necessary data for the caller to calculate the p&l and trade record, then clear the 'active' and 'limit-entry' properties in position.json and update the 'direction' property in hypothesis.json to 'none'
2.2. Otherwise (we're in cautious mode):
2.2.1. If the price crossed back to pre-cautious, then output a 'market-close' signal with the necessary data for the caller to calculate the p&l and trade record, then clear the 'active' and 'limit-entry' properties in position.json and update the 'direction' property in hypothesis.json to 'none'
2.2.2. Otherwise (didn't cross back to pre-cautious), check if we surpassed the last opposite 1m bar (like the 5m entry confirmation logic, this time no accumulation needed since it's a 1m bar logic) and if we did then output a 'market-close' signal with the necessary data for the caller to calculate the p&l and trade record, then clear the 'active' and 'limit-entry' properties in position.json and update the 'direction' property in hypothesis.json to 'none'
3. Otherwise (we have no open position), if during the current bar we surpassed a liquidity from the 'liquidities' property in daily.json that's in the opposite direction from the 'direction' property in hypothesis.json then change the 'direction' property in hypothesis.json to 'none'

# Regression Specific-Day Testing

The revised project should have a way to regression-test trading sessions in specific dates or date-ranges (assume their 1m bars data is included in the parquet files in the data/ folder) using the updated strategy and all the new / revised modules, such that given a list of dates / date-ranges (in some regression.md file) the system has a way to run the strategy on trading sessions from the specific requested days using their 1m data from the parquet files, check the results and compare with previous runs on these specific days (i.e. regression testing)