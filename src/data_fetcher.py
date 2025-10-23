"""Unusual Whales API client for fetching market data."""

import httpx
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional
import logging
import asyncio
import time

logger = logging.getLogger(__name__)


class UnusualWhalesClient:
    """Client for interacting with Unusual Whales API."""

    BASE_URL = "https://api.unusualwhales.com"
    # Rate limiting: delay between requests (in seconds)
    REQUEST_DELAY = 0.15  # 150ms between requests to avoid rate limits

    def __init__(self, api_key: str):
        """Initialize the API client.

        Args:
            api_key: Unusual Whales API key
        """
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json"
        }
        self._last_request_time = 0.0

    async def _rate_limit(self):
        """Apply rate limiting between API requests."""
        current_time = time.time()
        time_since_last_request = current_time - self._last_request_time

        if time_since_last_request < self.REQUEST_DELAY:
            delay = self.REQUEST_DELAY - time_since_last_request
            await asyncio.sleep(delay)

        self._last_request_time = time.time()

    async def get_ohlc_data(
        self,
        ticker: str,
        candle_size: str = "1m",
        days_back: int = 2
    ) -> List[Dict[str, Any]]:
        """Fetch OHLC candle data for a ticker.

        Args:
            ticker: Stock symbol (e.g., "AAPL")
            candle_size: Candle interval (default: "1m")
            days_back: Number of days to look back (default: 2)

        Returns:
            List of OHLC candle dictionaries
        """
        url = f"{self.BASE_URL}/api/stock/{ticker}/ohlc/{candle_size}"

        # Calculate date range - include today to support realtime data during market hours
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days_back + 3)  # Add buffer for weekends

        params = {
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": end_date.strftime("%Y-%m-%d")
        }

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()

        all_data = data.get("data", [])

        # Filter to only last N trading days since API may return more
        if all_data:
            # Parse timestamps and filter
            cutoff_time = datetime.now() - timedelta(days=days_back)
            filtered_data = []

            for candle in all_data:
                timestamp_str = candle.get("start_time") or candle.get("timestamp")
                if timestamp_str:
                    try:
                        candle_time = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                        if candle_time >= cutoff_time:
                            filtered_data.append(candle)
                    except:
                        # If we can't parse, include it to be safe
                        filtered_data.append(candle)

            logger.info(f"Fetched {len(all_data)} OHLC candles, filtered to {len(filtered_data)} for last {days_back} days")
            return filtered_data

        logger.info(f"Fetched {len(all_data)} OHLC candles for {ticker}")
        return all_data

    async def get_option_chains(
        self,
        ticker: str,
        date: Optional[str] = None
    ) -> List[str]:
        """Fetch available option contracts for a ticker.

        Args:
            ticker: Stock symbol
            date: Optional date in YYYY-MM-DD format

        Returns:
            List of option contract IDs (symbols)
        """
        url = f"{self.BASE_URL}/api/stock/{ticker}/option-chains"
        params = {}
        if date:
            params["date"] = date

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()

        contracts = data.get("data", [])
        logger.info(f"Fetched {len(contracts)} option contracts for {ticker}")
        return contracts

    async def get_option_intraday(
        self,
        contract_id: str,
        date: str
    ) -> List[Dict[str, Any]]:
        """Fetch 1-minute intraday data for an option contract.

        Args:
            contract_id: Option contract symbol (e.g., "AAPL251017C00150000")
            date: Date in YYYY-MM-DD format

        Returns:
            List of intraday data points with IV
        """
        url = f"{self.BASE_URL}/api/option-contract/{contract_id}/intraday"
        params = {"date": date}

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()

        intraday_data = data.get("data", [])
        logger.info(f"Fetched {len(intraday_data)} intraday points for {contract_id} on {date}")
        return intraday_data

    def parse_option_symbol(self, option_symbol: str) -> Dict[str, Any]:
        """Parse an option symbol to extract components.

        Option symbol format: TICKER + YYMMDD + C/P + STRIKE (8 digits)
        Example: AAPL251017C00150000
        - Ticker: AAPL
        - Expiration: 2025-10-17
        - Type: Call
        - Strike: $150.00

        Args:
            option_symbol: Option contract symbol

        Returns:
            Dictionary with ticker, expiration, type, and strike
        """
        # Find where the date starts (6 digits)
        # Work backwards from the end: 8 digits strike, 1 char type, 6 digits date
        if len(option_symbol) < 15:
            raise ValueError(f"Invalid option symbol format: {option_symbol}")

        # Extract components from the end
        strike_str = option_symbol[-8:]  # Last 8 digits
        option_type = option_symbol[-9]  # C or P
        date_str = option_symbol[-15:-9]  # YYMMDD
        ticker = option_symbol[:-15]  # Everything before date

        # Parse strike (divide by 1000 to get dollar amount)
        strike = float(strike_str) / 1000.0

        # Parse date
        year = 2000 + int(date_str[:2])
        month = int(date_str[2:4])
        day = int(date_str[4:6])
        expiration = f"{year:04d}-{month:02d}-{day:02d}"

        return {
            "ticker": ticker,
            "expiration": expiration,
            "type": "call" if option_type == "C" else "put",
            "strike": strike,
            "symbol": option_symbol
        }

    def filter_contracts_by_expiration_and_type(
        self,
        contracts: List[str],
        expiration_date: str,
        option_type: str
    ) -> Dict[float, str]:
        """Filter option contracts and create strike-to-symbol mapping.

        Args:
            contracts: List of option contract symbols
            expiration_date: Target expiration in YYYY-MM-DD format
            option_type: "call" or "put"

        Returns:
            Dictionary mapping strike prices to contract symbols
        """
        strike_map = {}

        for contract in contracts:
            try:
                parsed = self.parse_option_symbol(contract)

                # Filter by expiration and type
                if (parsed["expiration"] == expiration_date and
                    parsed["type"] == option_type.lower()):
                    strike_map[parsed["strike"]] = contract
            except (ValueError, IndexError) as e:
                logger.warning(f"Failed to parse contract {contract}: {e}")
                continue

        logger.info(f"Filtered to {len(strike_map)} strikes for {expiration_date} {option_type}s")
        return strike_map

    async def get_earnings(self, ticker: str) -> List[Dict[str, Any]]:
        """Fetch earnings data for a ticker.

        Args:
            ticker: Stock symbol

        Returns:
            List of earnings events with dates, estimates, and expected moves
        """
        url = f"{self.BASE_URL}/api/earnings/{ticker}"

        await self._rate_limit()
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            data = response.json()

        earnings = data.get("data", [])
        logger.info(f"Fetched {len(earnings)} earnings records for {ticker}")
        return earnings
