"""Utility functions for strike selection and IV interpolation."""

from typing import Dict, List, Tuple, Any, Optional
from datetime import datetime, timedelta
import logging

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
