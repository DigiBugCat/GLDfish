"""Utility functions for strike selection and IV interpolation."""

from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime, timedelta, date
import logging
import asyncio

logger = logging.getLogger(__name__)


def find_closest_strikes(
    spot_price: float,
    available_strikes: List[float],
    num_strikes: int = 2
) -> List[float]:
    """Find the closest strikes to the spot price.

    Args:
        spot_price: Current underlying price
        available_strikes: List of available strike prices
        num_strikes: Number of closest strikes to return (default: 2)

    Returns:
        List of closest strikes, sorted
    """
    if not available_strikes:
        return []

    sorted_strikes = sorted(available_strikes)

    # Find strikes on either side of spot price
    lower_strikes = [s for s in sorted_strikes if s <= spot_price]
    upper_strikes = [s for s in sorted_strikes if s > spot_price]

    # Get closest from each side
    closest = []
    if lower_strikes:
        closest.append(lower_strikes[-1])
    if upper_strikes:
        closest.append(upper_strikes[0])

    # If we need more strikes and don't have enough
    if len(closest) < num_strikes:
        if len(lower_strikes) > 1:
            closest.insert(0, lower_strikes[-2])
        if len(upper_strikes) > 1 and len(closest) < num_strikes:
            closest.append(upper_strikes[1])

    return sorted(closest)


def identify_required_strikes(
    ohlc_data: List[Dict[str, Any]],
    available_strikes: List[float]
) -> List[float]:
    """Identify which strikes will be needed for the entire time series.

    Args:
        ohlc_data: List of OHLC candles
        available_strikes: List of available strike prices

    Returns:
        List of unique strikes needed (sorted)
    """
    required_strikes = set()

    for candle in ohlc_data:
        # Use close price to determine ATM
        close_price = float(candle.get("close", 0))
        if close_price > 0:
            closest = find_closest_strikes(close_price, available_strikes, num_strikes=3)
            required_strikes.update(closest)

    strikes_list = sorted(list(required_strikes))
    logger.info(f"Identified {len(strikes_list)} required strikes: {strikes_list}")
    return strikes_list


def identify_required_strikes_by_date(
    ohlc_data: List[Dict[str, Any]],
    available_strikes: List[float]
) -> Dict[str, List[float]]:
    """Identify which strikes are needed for each specific trading date.

    This is an optimization over identify_required_strikes() - instead of fetching
    all strikes for all days, we only fetch strikes relevant to each day's price range.

    Example: If GOLD trades 370-380 on day 1 but 350-400 over 5 days, we don't need
    to fetch strike 350 for day 1, saving unnecessary API calls.

    Args:
        ohlc_data: List of OHLC candles with timestamps
        available_strikes: List of available strike prices

    Returns:
        Dictionary mapping date strings (YYYY-MM-DD) to list of required strikes
        Example: {"2025-10-20": [370.0, 375.0, 380.0], "2025-10-21": [365.0, 370.0, 375.0]}
    """
    from datetime import datetime

    # Group candles by date
    candles_by_date: Dict[str, List[Dict[str, Any]]] = {}

    for candle in ohlc_data:
        timestamp = candle.get("start_time") or candle.get("timestamp")
        if not timestamp:
            continue

        # Parse timestamp and extract date
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            date_str = dt.strftime("%Y-%m-%d")

            if date_str not in candles_by_date:
                candles_by_date[date_str] = []
            candles_by_date[date_str].append(candle)
        except Exception as e:
            logger.warning(f"Could not parse timestamp {timestamp}: {e}")
            continue

    # For each date, identify required strikes based on that day's price range
    strikes_by_date: Dict[str, List[float]] = {}

    for date_str, candles in candles_by_date.items():
        required_strikes = set()

        # Find price range for this specific day
        for candle in candles:
            close_price = float(candle.get("close", 0))
            if close_price > 0:
                closest = find_closest_strikes(close_price, available_strikes, num_strikes=3)
                required_strikes.update(closest)

        strikes_by_date[date_str] = sorted(list(required_strikes))
        logger.info(
            f"Date {date_str}: {len(strikes_by_date[date_str])} strikes needed "
            f"({strikes_by_date[date_str]})"
        )

    # Log optimization stats
    total_combinations = sum(len(strikes) for strikes in strikes_by_date.values())
    naive_combinations = len(identify_required_strikes(ohlc_data, available_strikes)) * len(strikes_by_date)
    saved = naive_combinations - total_combinations
    if naive_combinations > 0:
        saved_pct = (saved / naive_combinations) * 100
        logger.info(
            f"Optimization: {total_combinations} strike/date combinations needed "
            f"(vs {naive_combinations} naively) - saved {saved} API calls ({saved_pct:.1f}%)"
        )

    return strikes_by_date


def interpolate_iv(
    spot_price: float,
    strike_to_iv: Dict[float, float]
) -> Optional[float]:
    """Interpolate IV based on spot price and available strike IVs.

    Args:
        spot_price: Current underlying price
        strike_to_iv: Dictionary mapping strike prices to IV values

    Returns:
        Interpolated IV value, or None if insufficient data
    """
    if not strike_to_iv:
        return None

    strikes = sorted(strike_to_iv.keys())

    # Find bracketing strikes
    lower_strike = None
    upper_strike = None

    for strike in strikes:
        if strike <= spot_price:
            lower_strike = strike
        if strike > spot_price and upper_strike is None:
            upper_strike = strike

    # Edge cases
    if lower_strike is None:
        # Spot below all strikes - use lowest strike IV
        return strike_to_iv[strikes[0]]

    if upper_strike is None:
        # Spot above all strikes - use highest strike IV
        return strike_to_iv[strikes[-1]]

    if lower_strike == upper_strike:
        # Exact match
        return strike_to_iv[lower_strike]

    # Linear interpolation
    lower_iv = strike_to_iv[lower_strike]
    upper_iv = strike_to_iv[upper_strike]

    weight = (spot_price - lower_strike) / (upper_strike - lower_strike)
    interpolated_iv = lower_iv + weight * (upper_iv - lower_iv)

    return interpolated_iv


def align_data_by_timestamp(
    ohlc_data: List[Dict[str, Any]],
    iv_data_by_strike: Dict[float, List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Align OHLC and IV data by timestamp with interpolation.

    Args:
        ohlc_data: List of OHLC candles with timestamps
        iv_data_by_strike: Dictionary mapping strikes to their IV time series

    Returns:
        List of aligned data points with interpolated IV
    """
    aligned_data = []

    # Create lookup for IV by timestamp and strike
    iv_lookup: Dict[str, Dict[float, float]] = {}

    for strike, iv_series in iv_data_by_strike.items():
        for point in iv_series:
            # Use start_time field from API response
            timestamp = point.get("start_time") or point.get("timestamp")
            # Try multiple IV fields - APIs may return different formats
            iv_value = point.get("iv") or point.get("iv_high") or point.get("iv_low")

            if timestamp and iv_value is not None:
                iv_str = str(iv_value)
                try:
                    iv_float = float(iv_str)

                    if timestamp not in iv_lookup:
                        iv_lookup[timestamp] = {}
                    iv_lookup[timestamp][strike] = iv_float
                except (ValueError, TypeError):
                    continue

    # Align with OHLC data
    for candle in ohlc_data:
        # Use start_time field from API response
        timestamp = candle.get("start_time") or candle.get("timestamp")
        close_price = candle.get("close")

        if not timestamp or close_price is None:
            continue

        try:
            close_float = float(close_price)
        except (ValueError, TypeError):
            continue

        # Get IV data for this timestamp
        strike_to_iv = iv_lookup.get(timestamp, {})

        if strike_to_iv:
            interpolated_iv = interpolate_iv(close_float, strike_to_iv)
        else:
            interpolated_iv = None

        aligned_data.append({
            "timestamp": timestamp,
            "open": float(candle.get("open", 0)),
            "high": float(candle.get("high", 0)),
            "low": float(candle.get("low", 0)),
            "close": close_float,
            "volume": int(candle.get("volume", 0)),
            "iv": interpolated_iv,
            "market_time": candle.get("market_time")  # Preserve market_time field
        })

    logger.info(f"Aligned {len(aligned_data)} data points with IV")
    return aligned_data


def align_historic_data(
    ohlc_data: List[Dict[str, Any]],
    historic_iv_by_strike: Dict[float, List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Align 4h OHLC candles with historic option IV data by date.

    This function is used for lookback periods > 7 days where we use:
    - 4h stock candles (up to 271 days available)
    - Historic option data (EOD IV snapshots, ~250 days available)

    Args:
        ohlc_data: List of 4h OHLC candles with timestamps
        historic_iv_by_strike: Dictionary mapping strikes to their historic daily records

    Returns:
        List of aligned data points with interpolated IV
    """
    aligned_data = []

    # Create lookup for IV by date and strike
    # Historic data has 'date' field (YYYY-MM-DD) not timestamp
    iv_lookup: Dict[str, Dict[float, float]] = {}

    for strike, historic_records in historic_iv_by_strike.items():
        for record in historic_records:
            date = record.get("date")
            # Use implied_volatility field from historic endpoint
            iv_value = record.get("implied_volatility")

            if date and iv_value is not None:
                try:
                    iv_float = float(iv_value)

                    if date not in iv_lookup:
                        iv_lookup[date] = {}
                    iv_lookup[date][strike] = iv_float
                except (ValueError, TypeError):
                    logger.warning(f"Could not parse IV value for strike {strike} on {date}: {iv_value}")
                    continue

    # Align with OHLC data
    for candle in ohlc_data:
        timestamp = candle.get("start_time") or candle.get("timestamp")
        close_price = candle.get("close")

        if not timestamp or close_price is None:
            continue

        # Extract date from timestamp (for matching with historic data)
        try:
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            date_str = dt.strftime("%Y-%m-%d")
        except Exception as e:
            logger.warning(f"Could not parse timestamp {timestamp}: {e}")
            continue

        try:
            close_float = float(close_price)
        except (ValueError, TypeError):
            continue

        # Get IV data for this date
        strike_to_iv = iv_lookup.get(date_str, {})

        if strike_to_iv:
            interpolated_iv = interpolate_iv(close_float, strike_to_iv)
        else:
            interpolated_iv = None

        aligned_data.append({
            "timestamp": timestamp,
            "open": float(candle.get("open", 0)),
            "high": float(candle.get("high", 0)),
            "low": float(candle.get("low", 0)),
            "close": close_float,
            "volume": int(candle.get("volume", 0)),
            "iv": interpolated_iv,
            "market_time": candle.get("market_time")  # Preserve market_time field
        })

    logger.info(f"Aligned {len(aligned_data)} 4h candles with historic IV data")
    return aligned_data


def get_trading_dates(days_back: int) -> List[str]:
    """Generate list of trading dates going back N days.

    Args:
        days_back: Number of days to go back

    Returns:
        List of dates in YYYY-MM-DD format
    """
    dates = []
    # Start from today to support realtime data during market hours
    current_date = datetime.now()

    for i in range(days_back + 5):  # Add buffer for weekends
        date = current_date - timedelta(days=i)
        # Skip weekends (roughly - doesn't account for holidays)
        if date.weekday() < 5:
            dates.append(date.strftime("%Y-%m-%d"))

        if len(dates) >= days_back:
            break

    return sorted(dates)


def find_nearest_expiration(target_date: date) -> date:
    """Find nearest standard option expiration (Friday).

    Options typically expire on Fridays. This function rounds to the nearest Friday.

    Args:
        target_date: Desired expiration date

    Returns:
        Nearest Friday to target_date
    """
    # Calculate days to next Friday (Friday = 4 in weekday())
    days_until_friday = (4 - target_date.weekday()) % 7
    if days_until_friday == 0:
        # Already Friday
        return target_date

    next_friday = target_date + timedelta(days=days_until_friday)

    # Calculate days since last Friday
    days_since_friday = (target_date.weekday() - 4) % 7
    if days_since_friday == 0:
        days_since_friday = 7
    prev_friday = target_date - timedelta(days=days_since_friday)

    # Return closest Friday
    if abs((next_friday - target_date).days) <= abs((prev_friday - target_date).days):
        return next_friday
    return prev_friday


def generate_smart_strikes(spot_price: float, max_strikes: int = 6) -> List[float]:
    """Generate intelligent strike selection prioritizing round numbers.

    Args:
        spot_price: Current stock price
        max_strikes: Maximum number of strikes to generate

    Returns:
        List of strike prices sorted by likelihood (nearest ATM first)
    """
    # Determine strike interval based on stock price
    if spot_price < 50:
        interval = 2.5
    elif spot_price < 200:
        interval = 5
    elif spot_price < 500:
        interval = 10
    else:
        interval = 25

    # Round spot to nearest interval for ATM strike
    atm_strike = round(spot_price / interval) * interval

    # Generate strikes centered on ATM
    strikes = [atm_strike]
    offset = 1

    while len(strikes) < max_strikes:
        # Add strike above ATM
        upper = atm_strike + (offset * interval)
        if upper > 0:
            strikes.append(upper)

        # Add strike below ATM
        if len(strikes) < max_strikes:
            lower = atm_strike - (offset * interval)
            if lower > 0:
                strikes.append(lower)

        offset += 1

    # Sort by distance from spot (prioritize closest to ATM)
    strikes.sort(key=lambda s: abs(s - spot_price))

    return strikes[:max_strikes]


async def try_fetch_contract_iv(
    client,
    contract_id: str,
    analysis_date_str: str,
    ticker: str,
    exp_date: date,
    option_type: str,
    strike: float
) -> Optional[tuple]:
    """Try to fetch IV for a single contract.

    Args:
        client: UnusualWhalesClient instance
        contract_id: Contract symbol to try
        analysis_date_str: Date we need IV for
        ticker: Stock ticker
        exp_date: Expiration date
        option_type: "call" or "put"
        strike: Strike price

    Returns:
        Tuple of (contract_id, parsed_data, iv_value) if successful, None otherwise
    """
    try:
        historic_records = await client.get_option_historic(contract_id)

        # Find IV for the specific analysis date
        for record in historic_records:
            if record.get('date') == analysis_date_str:
                iv_str = record.get('implied_volatility')
                if iv_str:
                    try:
                        iv_value = float(iv_str) * 100
                        parsed = {
                            'ticker': ticker,
                            'expiration': exp_date.strftime("%Y-%m-%d"),
                            'type': option_type,
                            'strike': float(strike),
                            'symbol': contract_id
                        }
                        return (contract_id, parsed, iv_value)
                    except:
                        pass
    except:
        # Contract doesn't exist or has no data
        pass

    return None


async def brute_force_find_contract(
    client,
    ticker: str,
    target_exp_date: date,
    spot_price: float,
    analysis_date_str: str,
    option_types: List[str] = None,
    available_expirations: List[str] = None,
    logger = None
) -> Optional[tuple]:
    """Try to find a contract using intelligent concurrent search.

    Args:
        client: UnusualWhalesClient instance
        ticker: Stock ticker
        target_exp_date: Target expiration date
        spot_price: Current stock price to find ATM strikes
        analysis_date_str: Date we need IV data for (YYYY-MM-DD)
        option_types: List of option type codes to try (e.g., ['C'], ['P'], or ['C', 'P'])
        available_expirations: Optional list of available expiration dates (from API)
        logger: Logger instance

    Returns:
        Tuple of (contract_id, parsed_data, iv_value) if found, None otherwise
    """
    if option_types is None:
        option_types = ['C']  # Default to calls only

    # For past dates, don't use current available_expirations since they exclude expired contracts
    analysis_date = datetime.strptime(analysis_date_str, "%Y-%m-%d").date()
    today = datetime.now().date()
    use_available_expirations = available_expirations and analysis_date >= today

    # Use smart expiration selection and generate nearby alternatives
    if use_available_expirations:
        # Find closest available expiration
        exp_dates = [datetime.strptime(exp, "%Y-%m-%d").date() for exp in available_expirations]
        primary_exp = min(exp_dates, key=lambda d: abs((d - target_exp_date).days))
        expirations_to_try = [primary_exp]
    else:
        # Fall back to nearest Friday heuristic + nearby Fridays
        primary_exp = find_nearest_expiration(target_exp_date)
        # For past dates, also try ±1 week to increase chances of finding historic data
        expirations_to_try = [
            primary_exp - timedelta(weeks=1),
            primary_exp,
            primary_exp + timedelta(weeks=1)
        ]

    # Generate smart strikes (only ~6 instead of 13+)
    strikes = generate_smart_strikes(spot_price, max_strikes=6)

    # Try each expiration until we find one with data
    for exp_date in expirations_to_try:
        # Build all candidate contracts for this expiration
        tasks = []
        for strike in strikes:
            for opt_type_code in option_types:
                # Construct contract symbol
                exp_str = exp_date.strftime("%y%m%d")
                strike_code = f"{int(strike * 1000):08d}"
                contract_id = f"{ticker}{exp_str}{opt_type_code}{strike_code}"

                # Determine option type name
                opt_type_name = "call" if opt_type_code == 'C' else "put"

                # Create task to fetch this contract
                tasks.append(try_fetch_contract_iv(
                    client, contract_id, analysis_date_str,
                    ticker, exp_date, opt_type_name, strike
                ))

        # Fetch ALL candidates concurrently
        if logger:
            logger.info(f"  Trying {len(tasks)} contracts concurrently (exp={exp_date})")

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Return first successful result
        for result in results:
            if result is not None and not isinstance(result, Exception):
                if logger:
                    contract_id, parsed, iv_value = result
                    logger.info(f"  Found contract: {contract_id} (strike=${parsed['strike']}, IV={iv_value:.1f}%)")
                return result

    return None


async def discover_contracts_for_period(
    client,
    ticker: str,
    reference_date_str: str,
    spot_price: float,
    dte_buckets: List[int],
    option_types: List[str],
    logger = None
) -> Dict[int, Optional[tuple]]:
    """Discover which contracts existed and have data for a specific time period.

    Args:
        client: UnusualWhalesClient instance
        ticker: Stock ticker
        reference_date_str: Reference date for this period (YYYY-MM-DD)
        spot_price: Stock price for ATM calculation
        dte_buckets: List of DTEs to discover (e.g., [14, 30, 60, 90, 180])
        option_types: List of option type codes (e.g., ['C'] or ['P'])
        logger: Logger instance

    Returns:
        Dict mapping DTE -> (contract_id, parsed, expiration_date) or None
    """
    if logger:
        logger.info(f"Discovering contracts for period starting {reference_date_str}")

    reference_date = datetime.strptime(reference_date_str, "%Y-%m-%d").date()
    discovered = {}

    for target_dte in dte_buckets:
        target_exp_date = reference_date + timedelta(days=target_dte)

        # Use Friday heuristic for past dates (no current expiry breakdown)
        primary_exp = find_nearest_expiration(target_exp_date)

        # Try multiple nearby expirations to find one with historic data
        # Reduced to ±1 week to avoid excessive API calls
        expirations_to_try = [
            primary_exp - timedelta(weeks=1),
            primary_exp,
            primary_exp + timedelta(weeks=1),
        ]

        # Generate smart strikes
        strikes = generate_smart_strikes(spot_price, max_strikes=6)

        # Try each expiration until we find one with data
        found = False
        for i, exp_date in enumerate(expirations_to_try):
            # Add small delay between expiration attempts to avoid rate limiting
            if i > 0:
                await asyncio.sleep(0.3)

            # Try first strike only to check if this expiration has historic data
            test_strike = strikes[0]
            for opt_type_code in option_types:
                exp_str = exp_date.strftime("%y%m%d")
                strike_code = f"{int(test_strike * 1000):08d}"
                contract_id = f"{ticker}{exp_str}{opt_type_code}{strike_code}"

                try:
                    historic_records = await client.get_option_historic(contract_id)

                    # Check if this contract has data for our reference date
                    has_data = any(record.get('date') == reference_date_str for record in historic_records)

                    if has_data:
                        # This expiration works! Store it
                        opt_type_name = "call" if opt_type_code == 'C' else "put"
                        discovered[target_dte] = (exp_date, opt_type_name)
                        found = True
                        if logger:
                            logger.info(f"  DTE {target_dte}: Using exp={exp_date} (verified historic data)")
                        break
                except:
                    continue

            if found:
                break

        if not found:
            if logger:
                logger.warning(f"  DTE {target_dte}: No contracts found with historic data for {reference_date_str}")
            discovered[target_dte] = None

    return discovered


async def collect_earnings_iv_data(
    client,  # UnusualWhalesClient instance
    ticker: str,
    num_earnings: int = 3,
    days_window: int = 7,
    option_type: str = "call"
) -> Dict[str, Any]:
    """Collect ATM IV data around earnings dates for different DTE buckets.

    Args:
        client: UnusualWhalesClient instance
        ticker: Stock ticker symbol
        num_earnings: Number of past earnings to analyze
        days_window: Number of days before/after earnings to analyze
        option_type: Type of options to analyze ("call", "put", or "both")

    Returns:
        Dictionary with earnings IV data structure:
        {
            'earnings_dates': [list of earnings dates analyzed],
            'data': {
                'earnings_date_1': {
                    days_from_earnings: {14: IV, 30: IV, 60: IV, 90: IV, 180: IV}
                },
                ...
            }
        }
    """
    logger.info(f"Collecting earnings IV data for {ticker}, last {num_earnings} earnings, ±{days_window} days, option_type={option_type}")

    # Convert option_type to list of codes for brute force
    if option_type == "call":
        option_types = ['C']
    elif option_type == "put":
        option_types = ['P']
    else:  # "both"
        option_types = ['C', 'P']

    # Step 1: Get earnings dates
    earnings_data = await client.get_earnings(ticker)
    if not earnings_data:
        raise ValueError(f"No earnings data found for {ticker}")

    # Filter to past earnings only
    past_earnings = []
    today = datetime.now().date()

    for event in earnings_data:
        report_date_str = event.get("report_date")
        if report_date_str:
            report_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()
            if report_date < today:
                past_earnings.append(report_date_str)

    if len(past_earnings) < num_earnings:
        logger.warning(f"Only {len(past_earnings)} past earnings found, requested {num_earnings}")

    # Take the most recent N earnings
    past_earnings = sorted(past_earnings, reverse=True)[:num_earnings]
    logger.info(f"Analyzing earnings dates: {past_earnings}")

    # Step 2: Get OHLC data for spot prices (need ~250 days to cover all earnings)
    ohlc_data = await client.get_ohlc_data(ticker=ticker, candle_size="1d", days_back=300)

    # Create OHLC lookup by date
    ohlc_by_date = {}
    for candle in ohlc_data:
        # 1d candles use 'date' field, intraday candles use 'start_time' or 'timestamp'
        date_str = candle.get("date")
        if date_str:
            # Already in YYYY-MM-DD format for 1d candles
            ohlc_by_date[date_str] = {
                'open': float(candle.get("open", 0)),
                'high': float(candle.get("high", 0)),
                'low': float(candle.get("low", 0)),
                'close': float(candle.get("close", 0))
            }
        else:
            # Parse timestamp for intraday candles
            timestamp_str = candle.get("start_time") or candle.get("timestamp")
            if timestamp_str:
                try:
                    dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    date_str = dt.strftime("%Y-%m-%d")
                    ohlc_by_date[date_str] = {
                        'open': float(candle.get("open", 0)),
                        'high': float(candle.get("high", 0)),
                        'low': float(candle.get("low", 0)),
                        'close': float(candle.get("close", 0))
                    }
                except:
                    continue

    logger.info(f"Loaded OHLC data for {len(ohlc_by_date)} days")

    # Step 3: Get current option chains
    contracts = await client.get_option_chains(ticker=ticker)
    logger.info(f"Found {len(contracts)} total option contracts")

    # Parse contracts to get expiration/strike/type info
    parsed_contracts = {}
    for contract_id in contracts:
        try:
            parsed = client.parse_option_symbol(contract_id)
            parsed_contracts[contract_id] = parsed
        except:
            continue

    logger.info(f"Parsed {len(parsed_contracts)} contracts")

    # Step 3.5: Fetch available expirations for smart matching
    try:
        available_expirations = await client.get_expiry_breakdown(ticker)
        logger.info(f"Fetched {len(available_expirations)} available expirations for smart matching")
    except Exception as e:
        logger.warning(f"Could not fetch expiry breakdown: {e}. Will use Friday heuristic.")
        available_expirations = None

    # DTE buckets to analyze
    dte_buckets = [14, 30, 60, 90, 180]

    # Step 4: For each earnings date, collect IV data
    results = {
        'earnings_dates': past_earnings,
        'data': {}
    }

    for earnings_date_str in past_earnings:
        logger.info(f"Processing earnings date: {earnings_date_str}")
        earnings_date = datetime.strptime(earnings_date_str, "%Y-%m-%d").date()

        # Determine strategy based on earnings age
        today = datetime.now().date()
        earnings_age_days = (today - earnings_date).days
        use_discovery = earnings_age_days > 60  # Use discovery for old earnings (> 60 days)

        discovered_contracts = None

        if use_discovery:
            # OLD EARNINGS (> 60 days): Current chains won't have expired contracts
            # Run discovery phase upfront
            logger.info(f"Earnings is {earnings_age_days} days old, using DISCOVERY mode")

            first_analysis_date = earnings_date - timedelta(days=days_window)
            first_analysis_date_str = first_analysis_date.strftime("%Y-%m-%d")

            # Get spot price for discovery
            first_ohlc = ohlc_by_date.get(first_analysis_date_str)
            if not first_ohlc:
                logger.warning(f"No OHLC data for first analysis date {first_analysis_date_str}, skipping earnings period")
                continue

            discovery_spot = first_ohlc['close']

            # Discover contracts for this entire earnings period
            discovered_contracts = await discover_contracts_for_period(
                client=client,
                ticker=ticker,
                reference_date_str=first_analysis_date_str,
                spot_price=discovery_spot,
                dte_buckets=dte_buckets,
                option_types=option_types,
                logger=logger
            )
        else:
            # RECENT EARNINGS (< 60 days): Contracts still in current chains
            # Will try current chains first in the date loop
            logger.info(f"Earnings is {earnings_age_days} days old, using CURRENT CHAINS mode")

        earnings_data_points = {}

        # Generate date range: ±days_window around earnings
        for day_offset in range(-days_window, days_window + 1):
            analysis_date = earnings_date + timedelta(days=day_offset)
            analysis_date_str = analysis_date.strftime("%Y-%m-%d")

            # Skip weekends
            if analysis_date.weekday() >= 5:
                continue

            # Get OHLC data for this date
            ohlc = ohlc_by_date.get(analysis_date_str)
            if not ohlc:
                logger.warning(f"No OHLC data for {analysis_date_str}, skipping")
                continue

            spot_price = ohlc['close']

            # For each DTE bucket, find IV data using appropriate strategy
            dte_ivs = {}

            for target_dte in dte_buckets:
                iv_value = None

                if use_discovery:
                    # DISCOVERY MODE: Use pre-discovered contracts
                    discovered_info = discovered_contracts.get(target_dte) if discovered_contracts else None
                    if not discovered_info:
                        # No contract found during discovery phase
                        continue

                    exp_date, opt_type_name = discovered_info

                    # Find ATM strike for current spot price
                    strikes = generate_smart_strikes(spot_price, max_strikes=6)

                    # Try each strike to find one with IV data
                    for strike in strikes:
                        opt_type_code = 'C' if opt_type_name == 'call' else 'P'
                        exp_str = exp_date.strftime("%y%m%d")
                        strike_code = f"{int(strike * 1000):08d}"
                        contract_id = f"{ticker}{exp_str}{opt_type_code}{strike_code}"

                        try:
                            historic_records = await client.get_option_historic(contract_id)

                            # Find IV for this specific analysis date
                            for record in historic_records:
                                if record.get('date') == analysis_date_str:
                                    iv_str = record.get('implied_volatility')
                                    if iv_str:
                                        try:
                                            iv_value = float(iv_str) * 100
                                            dte_ivs[target_dte] = iv_value
                                            logger.debug(f"  {analysis_date_str} DTE{target_dte}: {iv_value:.1f}% (discovery, strike=${strike})")
                                            break
                                        except:
                                            pass

                            if iv_value is not None:
                                break

                        except Exception as e:
                            continue

                else:
                    # CURRENT CHAINS MODE: Try current chains first
                    target_exp_date = analysis_date + timedelta(days=target_dte)

                    # Find contracts with expiration close to target (±3 days tolerance)
                    candidate_contracts = []
                    for contract_id, parsed in parsed_contracts.items():
                        exp_date = datetime.strptime(parsed['expiration'], "%Y-%m-%d").date()
                        days_diff = abs((exp_date - target_exp_date).days)

                        if days_diff <= 3:
                            candidate_contracts.append((contract_id, parsed, days_diff))

                    if candidate_contracts:
                        # Sort by expiration match quality
                        candidate_contracts.sort(key=lambda x: x[2])

                        # Find ATM strike among candidates
                        for contract_id, parsed, _ in candidate_contracts:
                            strike = parsed['strike']
                            strike_diff = abs(strike - spot_price)

                            # Only consider strikes within ±10% of spot
                            if strike_diff / spot_price <= 0.10:
                                try:
                                    historic_records = await client.get_option_historic(contract_id)

                                    # Find IV for this specific analysis date
                                    for record in historic_records:
                                        if record.get('date') == analysis_date_str:
                                            iv_str = record.get('implied_volatility')
                                            if iv_str:
                                                try:
                                                    iv_value = float(iv_str) * 100
                                                    dte_ivs[target_dte] = iv_value
                                                    logger.debug(f"  {analysis_date_str} DTE{target_dte}: {iv_value:.1f}% (current chains, strike=${strike})")
                                                    break
                                                except:
                                                    pass

                                    if iv_value is not None:
                                        break

                                except Exception as e:
                                    continue

                if iv_value is None:
                    logger.debug(f"  {analysis_date_str} DTE{target_dte}: No IV data found")

            # Store this day's data if we got at least some DTEs or OHLC
            if dte_ivs or ohlc:
                earnings_data_points[day_offset] = {
                    'ivs': dte_ivs,
                    'ohlc': ohlc,
                    'spot_price': spot_price  # Keep for backward compatibility
                }

        results['data'][earnings_date_str] = earnings_data_points
        logger.info(f"Collected {len(earnings_data_points)} data points for {earnings_date_str}")

    return results
