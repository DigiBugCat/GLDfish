"""Comprehensive test of all UW endpoints for historical stock prices."""

import asyncio
import os
from dotenv import load_dotenv
import httpx
import yaml
from datetime import datetime, timedelta

load_dotenv()


async def comprehensive_stock_price_test():
    """Test all possible ways to get historical stock prices from UW API."""
    api_key = os.getenv("UNUSUAL_WHALES_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"}

    print("=" * 80)
    print("COMPREHENSIVE STOCK PRICE DATA SEARCH")
    print("=" * 80)

    # Step 1: Get all stock-related endpoints from OpenAPI
    print("\nStep 1: Finding all stock-related endpoints from OpenAPI spec...")
    print("-" * 80)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            "https://api.unusualwhales.com/api/openapi",
            headers=headers
        )
        spec = yaml.safe_load(response.text)

    stock_endpoints = {}
    for path, details in spec['paths'].items():
        if '/stock/' in path.lower() and 'price' in path.lower() or 'ohlc' in path.lower():
            stock_endpoints[path] = details

    print(f"Found {len(stock_endpoints)} stock/price related endpoints:")
    for path in sorted(stock_endpoints.keys()):
        print(f"  - {path}")

    # Step 2: Test OHLC endpoint variations thoroughly
    print(f"\n{'='*80}")
    print("Step 2: Testing /api/stock/SPY/ohlc variations")
    print("-" * 80)

    candle_sizes = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    test_date_30d = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    test_date_365d = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")

    for candle_size in candle_sizes:
        print(f"\nTesting: {candle_size} candles")

        # Test 1: No params
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://api.unusualwhales.com/api/stock/SPY/ohlc/{candle_size}",
                    headers=headers
                )

                if response.status_code == 200:
                    data = response.json().get("data", [])
                    if data:
                        dates = set([d.get('start_time', '')[:10] for d in data])
                        print(f"  ✅ No params: {len(data)} candles, {len(dates)} unique dates")
                        print(f"     Date range: {min(dates)} to {max(dates)}")
                    else:
                        print(f"  ⚠️  No params: 0 candles")
                elif response.status_code == 401:
                    print(f"  ❌ No params: 401 Unauthorized")
                else:
                    print(f"  ❌ No params: HTTP {response.status_code}")

        except Exception as e:
            print(f"  ❌ No params: {str(e)[:60]}")

        await asyncio.sleep(0.2)

        # Test 2: With 30-day old dates
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://api.unusualwhales.com/api/stock/SPY/ohlc/{candle_size}",
                    headers=headers,
                    params={
                        "start_date": test_date_30d,
                        "end_date": today
                    }
                )

                if response.status_code == 200:
                    data = response.json().get("data", [])
                    if data:
                        dates = set([d.get('start_time', '')[:10] for d in data])
                        print(f"  ✅ 30-day range: {len(data)} candles, {len(dates)} unique dates")
                        print(f"     Date range: {min(dates)} to {max(dates)}")
                    else:
                        print(f"  ⚠️  30-day range: 0 candles")
                elif response.status_code == 401:
                    print(f"  ❌ 30-day range: 401 Unauthorized")
                elif response.status_code == 403:
                    error_data = response.json()
                    msg = error_data.get('message', '')[:100]
                    print(f"  ❌ 30-day range: 403 - {msg}")
                else:
                    print(f"  ❌ 30-day range: HTTP {response.status_code}")

        except Exception as e:
            print(f"  ❌ 30-day range: {str(e)[:60]}")

        await asyncio.sleep(0.2)

    # Step 3: Check for other stock price endpoints
    print(f"\n{'='*80}")
    print("Step 3: Testing other potential stock price endpoints")
    print("-" * 80)

    test_endpoints = [
        "/api/stock/SPY/historical",
        "/api/stock/SPY/prices",
        "/api/stock/SPY/daily",
        "/api/stock/SPY/eod",
        "/api/stock/SPY/candles",
        "/api/stock/SPY",
    ]

    for endpoint in test_endpoints:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"https://api.unusualwhales.com{endpoint}",
                    headers=headers
                )

                if response.status_code == 200:
                    data = response.json()
                    print(f"✅ {endpoint}: SUCCESS")
                    print(f"   Keys: {list(data.keys())}")
                    if 'data' in data:
                        sample = data['data']
                        if isinstance(sample, list) and sample:
                            print(f"   Sample: {sample[0]}")
                        elif isinstance(sample, dict):
                            print(f"   Sample: {sample}")
                elif response.status_code == 404:
                    print(f"❌ {endpoint}: 404 Not Found")
                elif response.status_code == 401:
                    print(f"❌ {endpoint}: 401 Unauthorized")
                else:
                    print(f"⚠️  {endpoint}: HTTP {response.status_code}")

        except Exception as e:
            print(f"❌ {endpoint}: {str(e)[:60]}")

        await asyncio.sleep(0.2)

    # Step 4: Check the iv-rank endpoint more carefully (it had stock close prices)
    print(f"\n{'='*80}")
    print("Step 4: Re-testing /api/stock/SPY/iv-rank (had stock close prices)")
    print("-" * 80)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://api.unusualwhales.com/api/stock/SPY/iv-rank",
                headers=headers
            )

            if response.status_code == 200:
                data = response.json().get("data", [])
                print(f"✅ iv-rank: {len(data)} records")

                if data:
                    dates = [r['date'] for r in data]
                    print(f"   Date range: {min(dates)} to {max(dates)}")
                    print(f"   Sample: {data[0]}")
                    print(f"   Fields: {list(data[0].keys())}")
            elif response.status_code == 401:
                print(f"❌ iv-rank: 401 Unauthorized")
            else:
                print(f"⚠️  iv-rank: HTTP {response.status_code}")

    except Exception as e:
        print(f"❌ iv-rank: {str(e)[:60]}")

    # Step 5: Check what the data_fetcher's get_ohlc_data actually does
    print(f"\n{'='*80}")
    print("Step 5: Testing data_fetcher.get_ohlc_data() method directly")
    print("-" * 80)

    from src.data_fetcher import UnusualWhalesClient
    client = UnusualWhalesClient(api_key)

    print("\nTest with days_back=30:")
    try:
        data = await client.get_ohlc_data(
            ticker="SPY",
            candle_size="1m",
            days_back=30
        )

        if data:
            dates = set([d.get('start_time', '')[:10] for d in data])
            print(f"  ✅ {len(data)} candles returned")
            print(f"     Unique dates: {sorted(dates)}")
        else:
            print(f"  ⚠️  No data returned")

    except Exception as e:
        print(f"  ❌ Error: {e}")

    print(f"\n{'='*80}")
    print("SUMMARY")
    print("=" * 80)
    print("\nWill analyze results and determine best approach for stock prices...")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(comprehensive_stock_price_test())
