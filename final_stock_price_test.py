"""Final test to determine how to get historical stock prices."""

import asyncio
import os
from dotenv import load_dotenv
from src.data_fetcher import UnusualWhalesClient
from datetime import datetime, timedelta

load_dotenv()


async def test_stock_price_options():
    """Test different ways to get historical stock prices."""
    api_key = os.getenv("UNUSUAL_WHALES_API_KEY")
    client = UnusualWhalesClient(api_key)

    print("=" * 80)
    print("Testing stock price data availability for daily charts")
    print("=" * 80)

    # Test 1: Can we get stock OHLC for a single old day?
    print("\nTest 1: Fetch 1m OHLC for single day 30 days ago")
    try:
        old_date = datetime.now() - timedelta(days=30)
        data = await client.get_ohlc_data(
            ticker="SPY",
            candle_size="1m",
            days_back=1  # Just 1 day, but 30 days ago
        )
        print(f"  Result: {len(data)} candles")
        if data:
            # Check dates
            dates = set([d.get('start_time', '')[:10] for d in data])
            print(f"  Dates: {sorted(dates)}")
    except Exception as e:
        print(f"  Error: {e}")

    # Test 2: Aggregate approach - fetch recent days only
    print("\nTest 2: For daily chart, fetch last candle of each recent day")
    try:
        data = await client.get_ohlc_data(
            ticker="SPY",
            candle_size="1m",
            days_back=7  # Within allowed window
        )
        print(f"  Result: {len(data)} candles")

        if data:
            # Group by date and get last candle per date
            from collections import defaultdict
            candles_by_date = defaultdict(list)

            for candle in data:
                date_str = candle.get('start_time', '')[:10]
                candles_by_date[date_str].append(candle)

            print(f"  Unique dates: {len(candles_by_date)}")

            # Show last candle per date (EOD proxy)
            for date in sorted(candles_by_date.keys()):
                last_candle = candles_by_date[date][-1]
                close = last_candle.get('close')
                print(f"    {date}: close=${close} ({len(candles_by_date[date])} candles)")

    except Exception as e:
        print(f"  Error: {e}")

    # Test 3: Check what the historic option data includes
    print("\nTest 3: Check if historic option data has any stock price info")
    try:
        import httpx

        contract = "SPY260116C00380000"
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            response = await http_client.get(
                f"https://api.unusualwhales.com/api/option-contract/{contract}/historic",
                headers={"Authorization": f"Bearer {api_key}"}
            )

            data = response.json()
            records = data.get("chains", [])

            if records:
                sample = records[-10]  # Get older record
                print(f"  Sample historic record (date: {sample.get('date')}):")
                print(f"    Fields: {list(sample.keys())}")

                # Check for anything that might be stock price
                stock_fields = ['ticker_vol', 'last_price', 'nbbo_bid', 'nbbo_ask']
                print(f"    Possible stock-related fields:")
                for field in stock_fields:
                    if field in sample:
                        print(f"      {field}: {sample.get(field)}")

                # ticker_vol is stock volume - but where's stock price?
                print(f"\n    Note: ticker_vol is underlying stock volume, but no stock price field found")

    except Exception as e:
        print(f"  Error: {e}")

    print("\n" + "=" * 80)
    print("CONCLUSION")
    print("=" * 80)
    print("\nFor daily charts beyond 7 days:")
    print("  - Option IV: Use /api/option-contract/{id}/historic ✅")
    print("  - Stock OHLC: Limited to 7-day window ❌")
    print("\nRecommended approach:")
    print("  - For ≤ 7 days: Current intraday approach (1m candles)")
    print("  - For > 7 days: Aggregate 1m candles from last 7 days to daily,")
    print("                  then show IV-only for older periods")
    print("  - OR: Just show IV trend without stock overlay for long-term")

    print("\n" + "=" * 80)


if __name__ == "__main__":
    asyncio.run(test_stock_price_options())
