# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development Commands

### Running Locally
```bash
# Install dependencies
uv sync

# Run the bot
uv run python -m src.bot

# Run debug scripts
uv run python debug_api.py
uv run python debug_timestamps.py
uv run python debug_filtering.py
uv run python test_fetch.py
```

### Docker Deployment
```bash
# Build and start (quick script)
./run.sh

# Or manually
docker-compose up -d --build

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

## Architecture Overview

This is a Discord bot that generates implied volatility (IV) charts for stock options. It creates dual-axis visualizations showing both price movement (OHLC candles) and IV over time, supporting lookback periods from 1 day to 1 year.

### Dual-Mode Data Fetching

The bot operates in two modes based on the requested lookback period:

**Intraday Mode (≤ 7 days)**:
- Uses 1-minute granularity data
- Stock OHLC: `/api/stock/{ticker}/ohlc/1m` endpoint
- Option IV: `/api/option-contract/{id}/intraday` endpoint
- High resolution, minute-by-minute data
- Limited to 7 trading days due to API restrictions

**Historic Mode (> 7 days, up to ~250 days)**:
- Uses 4-hour granularity for stock data
- Stock OHLC: `/api/stock/{ticker}/ohlc/4h` endpoint (271 days available)
- Option IV: `/api/option-contract/{id}/historic` endpoint (~250 days available)
- Lower resolution, but extends history significantly
- Effective limit: ~250 days (constrained by option historic data availability)

### Data Flow

**Intraday Mode (≤ 7 days)**:
1. User invokes `/iv_chart` slash command with ticker, expiration, option_type, and days
2. `UnusualWhalesClient` fetches 1-minute OHLC candles for the underlying ticker
3. Fetch option chains and filter by expiration date and option type (call/put)
4. `identify_required_strikes_by_date()` determines which strikes become ATM during the time period
5. Fetch 1-minute IV data for each required strike across all trading dates
6. `align_data_by_timestamp()` merges OHLC and IV data, interpolating IV based on spot price
7. `create_iv_chart()` generates matplotlib chart with dual axes
8. Bot sends chart to Discord with refresh/delete buttons
9. `ChartDatabase` stores message metadata for button persistence

**Historic Mode (> 7 days)**:
1. User invokes `/iv_chart` with days > 7
2. Fetch 4-hour OHLC candles for underlying ticker (up to 271 days available)
3. Fetch option chains and filter by expiration/type
4. Identify required strikes from 4h candle data
5. Fetch historic EOD data for each required strike using `/api/option-contract/{id}/historic`
6. `align_historic_data()` merges 4h OHLC with historic IV, interpolating based on spot price
7. Generate chart with 4h candlesticks and IV overlay
8. Send to Discord with persistent buttons

### Core Modules

**src/bot.py**
- Discord bot initialization and event handlers
- `/iv_chart` slash command implementation
- `ChartControlView` class for persistent button UI (refresh, delete)
- Orchestrates data fetching, alignment, and chart generation
- Uses `ChartDatabase` to persist message metadata for button actions

**src/data_fetcher.py**
- `UnusualWhalesClient` class for API interactions
- Rate limiting: 150ms delay between requests to avoid API limits
- Endpoints:
  - `get_ohlc_data()`: Fetch OHLC candles (supports 1m, 4h, etc. - larger candles = more history)
  - `get_option_chains()`: Fetch available option contracts
  - `get_option_intraday()`: Fetch 1-minute IV data for specific contract (≤7 days)
  - `get_option_historic()`: Fetch EOD historic data for contract (~250 days, uses `chains` key not `data`)
  - `filter_contracts_by_expiration_and_type()`: Filter chains by expiration and call/put

**src/utils.py**
- `find_closest_strikes()`: Find strikes nearest to spot price
- `identify_required_strikes()`: [Legacy] Scan OHLC data to determine all needed strikes
- `identify_required_strikes_by_date()`: [Optimized] Identify strikes needed per trading date
  - Groups OHLC candles by date (YYYY-MM-DD)
  - Returns `Dict[str, List[float]]` mapping dates to required strikes
  - Logs optimization stats (API calls saved vs naive approach)
- `interpolate_iv()`: Linear interpolation between strikes
  - Formula: `IV(spot) = IV(lower) + weight * (IV(upper) - IV(lower))`
  - Where `weight = (spot - lower_strike) / (upper_strike - lower_strike)`
- `align_data_by_timestamp()`: Merge intraday OHLC and IV data by timestamp (for ≤7 day mode)
- `align_historic_data()`: Merge 4h OHLC with historic IV data by date (for >7 day mode)
- `get_trading_dates()`: Generate list of trading dates for fetching

**src/chart_generator.py**
- `create_iv_chart()`: Generate dual-axis matplotlib chart
  - Left axis: OHLC candlesticks (green=up, red=down)
  - Right axis: Smoothed IV line (15-minute rolling average)
  - Filters to regular market hours only (uses `market_time='r'`)
  - Filters to last N trading days
  - Returns PNG as BytesIO buffer
- `plot_candlesticks_indexed()`: Render candlesticks using indexed x-axis to eliminate gaps
- `create_error_chart()`: Generate error message chart when data unavailable

**src/database.py**
- `ChartDatabase`: SQLite database for persisting chart message metadata
- Stores message_id, channel_id, user_id, ticker, expiration, option_type, days
- Enables button persistence across bot restarts
- Methods: `store_chart()`, `get_chart()`, `delete_chart()`

## Key Implementation Details

### Rate Limiting Strategy
The `UnusualWhalesClient` implements request throttling with `_rate_limit()` method:
- Tracks `_last_request_time` between calls
- Enforces 150ms minimum delay between requests
- Uses `asyncio.sleep()` to wait if needed

### Strike Selection Algorithm (Optimized)
The bot uses a date-aware strike selection algorithm to minimize API calls:

1. **Per-Date Analysis**: `identify_required_strikes_by_date()` groups OHLC candles by trading date
2. **Date-Specific Strikes**: For each date, finds only the 3 closest strikes to that day's price range
3. **Optimized Fetching**: Only fetches IV data for strikes that are relevant on each specific date
4. **Example Optimization**: If GOLD trades 370-380 on day 1 but 350-400 over 5 days, strike 350 is NOT fetched for day 1
5. **Typical Savings**: 40-70% reduction in API calls vs naive approach (all strikes × all days)

The algorithm logs optimization statistics showing total API calls vs naive approach.

### IV Interpolation
Linear interpolation between bracketing strikes:
- If spot is exactly at a strike, use that strike's IV directly
- If spot is between strikes, interpolate based on distance ratio
- If spot is outside available strikes, use nearest strike's IV
- This prevents jarring jumps in the IV line as ATM strike changes

### Chart Generation Optimizations
- **15-minute rolling average** on IV for smoothing
- **Indexed x-axis** eliminates gaps from non-trading hours
- **Market hours filter** removes pre/post-market candles
- **Date-based filtering** ensures only last N trading days are shown
- **Timezone handling** converts UTC to US/Pacific for date filtering

### Button Persistence
- `ChartControlView` uses `timeout=None` for persistent buttons
- Database stores chart metadata by message_id
- On bot restart, buttons still work via database lookup
- Refresh button re-fetches data and regenerates chart
- Delete button removes message and database entry

## API Integration

### Unusual Whales Endpoints
```
BASE_URL = "https://api.unusualwhales.com"

GET /api/stock/{ticker}/ohlc/{candle_size}
  - Params: start_date, end_date
  - Candle sizes: 1m, 5m, 15m, 30m, 1h, 4h, 1d
  - Data availability varies by candle size (API returns ~2500 candles max):
    * 1m: 4 days
    * 4h: 271 days (~9 months)
  - Returns: List of OHLC candles with timestamps

GET /api/stock/{ticker}/option-chains
  - Returns: All available option contracts with contract_id, strike, expiration, type

GET /api/option-contract/{contract_id}/intraday
  - Params: date (YYYY-MM-DD)
  - Limited to last 7 trading days
  - Returns: 1-minute IV data for specific contract on specific date

GET /api/option-contract/{contract_id}/historic
  - No params required
  - Returns: ~250 days of EOD (end-of-day) historic data for option contract
  - **IMPORTANT**: Response uses 'chains' key, NOT 'data'!
  - Response format: {"chains": [{"date": "YYYY-MM-DD", "implied_volatility": "0.XX", ...}, ...]}
  - Fields include: date, implied_volatility, open_interest, volume, nbbo_bid, nbbo_ask,
    last_price, iv_low, iv_high, and more
  - Not all dates have IV data (contracts may exist before being actively traded)
```

### Authentication
All requests include:
```python
headers = {
    "Authorization": f"Bearer {api_key}",
    "Accept": "application/json"
}
```

## Environment Configuration

Required environment variables in `.env`:
```
UNUSUAL_WHALES_API_KEY=your_api_key_here
DISCORD_BOT_TOKEN=your_discord_bot_token_here
```

Copy from `.env.example` and fill in values.

## Discord Bot Setup Requirements

The bot requires these Discord intents and permissions:
- Message Content Intent
- Send Messages permission
- Attach Files permission
- Use Slash Commands permission

## Project Structure Notes

- `main.py`: Simple entry point that imports and runs `src.bot`
- `data/`: SQLite database storage (created at runtime)
- `logs/`: Log file storage (Docker volume mount)
- `debug_*.py` and `test_*.py`: Standalone scripts for testing API and data processing
- `.python-version`: Python 3.11 (Docker uses 3.11-slim)
- `pyproject.toml`: UV project config, requires Python 3.13+ locally

## Common Patterns

### Adding New Chart Features
1. Modify `create_iv_chart()` in `chart_generator.py` for visual changes
2. Update `align_data_by_timestamp()` in `utils.py` if changing data structure
3. Adjust slash command parameters in `bot.py` if adding user options

### Adding New API Endpoints
1. Add method to `UnusualWhalesClient` in `data_fetcher.py`
2. Include `await self._rate_limit()` before HTTP request
3. Use `httpx.AsyncClient` with 30s timeout
4. Log data counts for debugging

### Error Handling
- API failures fall back to `create_error_chart()` with descriptive message
- Missing IV data is logged with warnings but doesn't crash
- Empty data after filtering raises `ValueError` with specific message

## Performance Optimizations

### API Call Reduction
The bot uses `identify_required_strikes_by_date()` to minimize API calls:
- **Old approach**: Fetch all strikes across all days (strikes × days API calls)
- **New approach**: Only fetch strikes relevant to each day's price range
- **Savings**: Typically 40-70% fewer API calls
- **Implementation**: Loop structure is `for date -> for strikes` instead of `for strikes -> for days`
- **Monitoring**: Check logs for "Optimization: X strike/date combinations needed" messages

Example:
```
5-day query for GOLD (range 350-400):
- Naive: 10 strikes × 5 days = 50 API calls
- Optimized: ~3 strikes/day × 5 days = ~15 API calls (70% reduction)
```
