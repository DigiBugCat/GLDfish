"""Research the historic endpoint to understand its capabilities."""

import asyncio
import os
from dotenv import load_dotenv
import httpx
import json
from datetime import datetime, timedelta
from src.data_fetcher import UnusualWhalesClient

load_dotenv()


async def research_historic_endpoint():
    """Comprehensive research on the historic endpoint."""
    api_key = os.getenv("UNUSUAL_WHALES_API_KEY")
    headers = {"Authorization": f"Bearer {api_key}"}
    client = UnusualWhalesClient(api_key)

    print("=" * 80)
    print("RESEARCH: /api/option-contract/{id}/historic endpoint")
    print("=" * 80)

    # Get SPY contracts
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        response = await http_client.get(
            "https://api.unusualwhales.com/api/stock/SPY/option-chains",
            headers=headers
        )
        all_contracts = response.json().get("data", [])

    # Categorize contracts by expiration
    near_term = []  # < 30 days
    medium_term = []  # 30-90 days
    far_term = []  # > 90 days

    for contract in all_contracts[:200]:
        try:
            parsed = client.parse_option_symbol(contract)
            exp_date = datetime.strptime(parsed['expiration'], '%Y-%m-%d')
            days_to_exp = (exp_date - datetime.now()).days

            if days_to_exp < 30:
                near_term.append((contract, parsed))
            elif days_to_exp < 90:
                medium_term.append((contract, parsed))
            else:
                far_term.append((contract, parsed))
        except:
            continue

    # Select test contracts
    test_contracts = []
    if near_term:
        test_contracts.append(("Near-term (< 30d)", near_term[0]))
    if medium_term:
        test_contracts.append(("Medium-term (30-90d)", medium_term[0]))
    if far_term:
        test_contracts.append(("Far-term (> 90d)", far_term[0]))

    print(f"\n{'='*80}")
    print("QUESTION 1: How far back does historic data go for different contract types?")
    print(f"{'='*80}\n")

    for label, (contract, parsed) in test_contracts:
        print(f"{label}: {contract}")
        print(f"  {parsed['type'].upper()} ${parsed['strike']} exp {parsed['expiration']}")

        try:
            async with httpx.AsyncClient(timeout=30.0) as http_client:
                response = await http_client.get(
                    f"https://api.unusualwhales.com/api/option-contract/{contract}/historic",
                    headers=headers
                )
                data = response.json()
                records = data.get("chains", [])

                if records:
                    dates = sorted([r['date'] for r in records if r.get('date')])
                    print(f"  ✅ {len(records)} records")
                    print(f"     Earliest: {dates[0]}")
                    print(f"     Latest: {dates[-1]}")
                    print(f"     Days of history: {len(dates)}")

                    # Check when contract was created
                    first_non_zero_oi = next((r for r in records if r.get('open_interest', 0) > 0), None)
                    if first_non_zero_oi:
                        print(f"     First non-zero OI: {first_non_zero_oi['date']} (OI: {first_non_zero_oi['open_interest']})")
                else:
                    print(f"  ⚠️  No records")

        except Exception as e:
            print(f"  ❌ Error: {e}")

        print()
        await asyncio.sleep(0.3)

    print(f"\n{'='*80}")
    print("QUESTION 2: What IV fields are available? Which should we use?")
    print(f"{'='*80}\n")

    # Get a sample contract with data
    sample_contract = test_contracts[0][1][0] if test_contracts else all_contracts[0]

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        response = await http_client.get(
            f"https://api.unusualwhales.com/api/option-contract/{sample_contract}/historic",
            headers=headers
        )
        data = response.json()
        records = data.get("chains", [])

    if records:
        print(f"Sample contract: {sample_contract}")
        print(f"\nAll available fields:")
        all_keys = set()
        for record in records:
            all_keys.update(record.keys())
        print(f"  {sorted(all_keys)}")

        # Look at records with actual IV data
        records_with_iv = [r for r in records if r.get('implied_volatility')]
        if records_with_iv:
            sample = records_with_iv[-1]  # Most recent with IV
            print(f"\nSample record with IV (date: {sample.get('date')}):")
            print(f"  implied_volatility: {sample.get('implied_volatility')}")
            print(f"  iv_low: {sample.get('iv_low')}")
            print(f"  iv_high: {sample.get('iv_high')}")
            print(f"  volume: {sample.get('volume')}")
            print(f"  open_interest: {sample.get('open_interest')}")
            print(f"  last_price: {sample.get('last_price')}")
            print(f"  nbbo_bid: {sample.get('nbbo_bid')}")
            print(f"  nbbo_ask: {sample.get('nbbo_ask')}")

            # Calculate midpoint if we have both
            if sample.get('iv_low') and sample.get('iv_high'):
                try:
                    midpoint = (float(sample['iv_low']) + float(sample['iv_high'])) / 2
                    print(f"\n  Midpoint IV (iv_low + iv_high)/2: {midpoint}")
                    print(f"  Main implied_volatility: {sample['implied_volatility']}")
                    print(f"  Difference: {abs(float(sample['implied_volatility']) - midpoint)}")
                except:
                    pass

        print(f"\nIV availability statistics:")
        total = len(records)
        with_iv = len([r for r in records if r.get('implied_volatility')])
        with_iv_range = len([r for r in records if r.get('iv_low') and r.get('iv_high')])
        print(f"  Total records: {total}")
        print(f"  With implied_volatility: {with_iv} ({with_iv/total*100:.1f}%)")
        print(f"  With iv_low/iv_high: {with_iv_range} ({with_iv_range/total*100:.1f}%)")

    print(f"\n{'='*80}")
    print("QUESTION 3: Can we get daily OHLC for stock prices too?")
    print(f"{'='*80}\n")

    try:
        async with httpx.AsyncClient(timeout=30.0) as http_client:
            # Test 1d candles
            response = await http_client.get(
                "https://api.unusualwhales.com/api/stock/SPY/ohlc/1d",
                headers=headers,
                params={
                    "start_date": (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d"),
                    "end_date": datetime.now().strftime("%Y-%m-%d")
                }
            )

            if response.status_code == 200:
                data = response.json()
                candles = data.get("data", [])
                print(f"✅ Daily OHLC available: {len(candles)} daily candles fetched")

                if candles:
                    dates = [c.get('start_time', c.get('timestamp', ''))[:10] for c in candles]
                    dates = sorted([d for d in dates if d])
                    print(f"   Date range: {dates[0]} to {dates[-1]}")
                    print(f"   Sample candle: {candles[0]}")
            else:
                print(f"❌ Daily OHLC: HTTP {response.status_code}")

    except Exception as e:
        print(f"❌ Error testing daily OHLC: {e}")

    print(f"\n{'='*80}")
    print("QUESTION 4: What happens for dates before contract was created?")
    print(f"{'='*80}\n")

    # Get a recently created contract (near-term)
    if near_term:
        recent_contract, parsed = near_term[0]
        print(f"Recent contract: {recent_contract}")
        print(f"  Expiration: {parsed['expiration']}")

        async with httpx.AsyncClient(timeout=30.0) as http_client:
            response = await http_client.get(
                f"https://api.unusualwhales.com/api/option-contract/{recent_contract}/historic",
                headers=headers
            )
            data = response.json()
            records = data.get("chains", [])

        if records:
            # Find first record with non-zero OI or volume
            first_active = None
            for record in sorted(records, key=lambda x: x.get('date', '')):
                if record.get('open_interest', 0) > 0 or record.get('volume', 0) > 0:
                    first_active = record
                    break

            print(f"\n  Total historic records: {len(records)}")
            print(f"  Date range: {records[-1]['date']} to {records[0]['date']}")

            if first_active:
                print(f"\n  First active date (OI > 0 or Vol > 0):")
                print(f"    Date: {first_active['date']}")
                print(f"    OI: {first_active.get('open_interest')}")
                print(f"    Volume: {first_active.get('volume')}")
                print(f"    IV: {first_active.get('implied_volatility')}")

            # Check records before first active
            if first_active:
                before_active = [r for r in records if r['date'] < first_active['date']]
                print(f"\n  Records before first active date: {len(before_active)}")
                if before_active:
                    sample = before_active[-1]  # Most recent before active
                    print(f"    Sample (date: {sample['date']}):")
                    print(f"      IV: {sample.get('implied_volatility')}")
                    print(f"      OI: {sample.get('open_interest')}")
                    print(f"      Volume: {sample.get('volume')}")

    print(f"\n{'='*80}")
    print("SUMMARY & RECOMMENDATIONS")
    print(f"{'='*80}\n")

    print("Based on this research:")
    print("1. Historic endpoint provides ~250-260 days of EOD data")
    print("2. Use 'implied_volatility' field as the main IV metric")
    print("3. Daily OHLC (1d) is available for stock prices")
    print("4. Contracts may have records before they were actively traded")
    print("   (zero OI/volume but with theoretical IV)")
    print("\nRecommendation:")
    print("- For lookback > 7 days: switch to daily data")
    print("- Use historic endpoint for EOD IV per contract")
    print("- Use 'implied_volatility' field (not midpoint of iv_low/iv_high)")
    print("- Filter contracts by OI > 0 to avoid theoretical-only data")

    print(f"\n{'='*80}")


if __name__ == "__main__":
    asyncio.run(research_historic_endpoint())
