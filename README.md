# Discord IV Bot

Discord bot that generates intraday implied volatility charts for stock options. Shows price (OHLC candles) and IV on a dual-axis chart.

## What it does

Fetches 1-minute option data from Unusual Whales API and generates charts showing how IV changes throughout the trading day alongside price movement. Uses linear interpolation to track IV smoothly as the underlying price moves between strikes.

## Setup

Requires:
- Unusual Whales API key
- Discord bot token
- Python 3.11+ or Docker

Quick start with Docker:
```bash
cp .env.example .env
# Add your API keys to .env
./run.sh
```

Or locally with UV:
```bash
uv sync
uv run python -m src.bot
```

## Example

Command in Discord:
```
/iv_chart ticker:AAPL expiration:2025-11-15 option_type:Call days:2
```

This fetches the last 2 days of 1-minute OHLC data for AAPL, finds the relevant strikes (those that were ATM during that period), gets IV data for those strikes, interpolates IV based on spot price, and returns a chart.

Parameters:
- `ticker`: Stock symbol (AAPL, SPY, etc)
- `expiration`: Option expiration date (YYYY-MM-DD format)
- `option_type`: Call or Put
- `days`: Number of trading days to look back (default: 2)

The bot includes refresh and delete buttons on each chart.

## How it works

1. Gets 1-minute OHLC candles for the ticker
2. Fetches option chains for the expiration
3. For each trading day, identifies which strikes were ATM during that day
4. Fetches IV data only for the strikes that matter on each specific day
5. Interpolates IV between strikes based on spot price
6. Generates matplotlib chart with dual axes
7. Stores chart metadata in SQLite for button persistence

If a stock trades 370-380 on day 1 but 350-400 over 5 days, it only fetches strikes 370-380 for day 1, not the full 350-400 range.

## API Configuration

Create `.env` from template:
```bash
UNUSUAL_WHALES_API_KEY=your_key
DISCORD_BOT_TOKEN=your_token
```

Discord bot needs these permissions:
- Send Messages
- Attach Files
- Use Slash Commands

## License

MIT
