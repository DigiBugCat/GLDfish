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
    # Concurrency limit: maximum number of simultaneous requests
    MAX_CONCURRENT_REQUESTS = 4  # Limit to 4 concurrent requests to avoid overwhelming API

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
        # Semaphore to limit concurrent requests
        self._semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_REQUESTS)

    async def _rate_limit(self):
        """Apply rate limiting between API requests."""
        current_time = time.time()
        time_since_last_request = current_time - self._last_request_time

        if time_since_last_request < self.REQUEST_DELAY:
            delay = self.REQUEST_DELAY - time_since_last_request
            await asyncio.sleep(delay)

        self._last_request_time = time.time()

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        base_delay: float = 30.0
    ) -> httpx.Response:
        """Make HTTP request with exponential backoff retry on 429 errors.

        Args:
            method: HTTP method (GET, POST, etc.)
            url: Request URL
            params: Query parameters
            max_retries: Maximum number of retry attempts
            base_delay: Base delay in seconds for exponential backoff (default 30s, then 60s, 120s)

        Returns:
            HTTP response

        Raises:
            httpx.HTTPStatusError: If request fails after all retries
        """
        # Use semaphore to limit concurrent requests
        async with self._semaphore:
            await self._rate_limit()

            for attempt in range(max_retries):
                try:
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        response = await client.request(
                            method=method,
                            url=url,
                            headers=self.headers,
                            params=params
                        )
                        response.raise_for_status()
                        return response

                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 429:
                        # Rate limit exceeded
                        if attempt < max_retries - 1:
                            # Calculate exponential backoff: 30s, 60s, 120s
                            delay = base_delay * (2 ** attempt)
                            logger.warning(f"Rate limit hit (429), waiting {delay:.0f}s before retry {attempt + 1}/{max_retries}")
                            await asyncio.sleep(delay)
                            continue
                        else:
                            logger.error(f"Rate limit hit (429), exhausted all {max_retries} retries")
                            raise
                    else:
                        # Other HTTP error, don't retry
                        raise

            # Should never reach here, but just in case
            raise Exception("Request failed after all retries")

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

        # For 4h and 1d candles, the API doesn't accept date range parameters (422 error - future date issue)
        # and returns all available data automatically (~2500 candles max)
        # For other candle sizes (1m, 5m, etc), we need to specify date range
        if candle_size in ["4h", "1d"]:
            # Don't send date parameters - API returns all available data
            params = {}
            logger.info(f"Fetching {candle_size} candles for {ticker} (no date params - API returns all available data)")
        else:
            # Calculate date range - include today to support realtime data during market hours
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days_back + 3)  # Add buffer for weekends

            params = {
                "start_date": start_date.strftime("%Y-%m-%d"),
                "end_date": end_date.strftime("%Y-%m-%d")
            }
            logger.info(f"Fetching {candle_size} candles for {ticker} from {params['start_date']} to {params['end_date']}")

        response = await self._request_with_retry("GET", url, params=params)
        data = response.json()

        all_data = data.get("data", [])

        # Filter to only last N trading days since API may return more
        if all_data:
            # Parse timestamps and filter
            cutoff_time = datetime.now() - timedelta(days=days_back)
            filtered_data = []

            for candle in all_data:
                # 1d candles use 'date' field (YYYY-MM-DD), intraday candles use 'start_time'/'timestamp'
                date_str = candle.get("date")
                if date_str:
                    # Parse date for 1d candles (format: YYYY-MM-DD)
                    try:
                        candle_time = datetime.strptime(date_str, "%Y-%m-%d")
                        if candle_time >= cutoff_time:
                            filtered_data.append(candle)
                    except:
                        # If we can't parse, include it to be safe
                        filtered_data.append(candle)
                else:
                    # Parse timestamp for intraday candles
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

        response = await self._request_with_retry("GET", url, params=params)
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

        response = await self._request_with_retry("GET", url, params=params)
        data = response.json()

        intraday_data = data.get("data", [])
        logger.info(f"Fetched {len(intraday_data)} intraday points for {contract_id} on {date}")
        return intraday_data

    async def get_option_historic(
        self,
        contract_id: str
    ) -> List[Dict[str, Any]]:
        """Fetch historic EOD data for an option contract.

        This endpoint provides ~250 days of end-of-day historical data for an option contract,
        bypassing the 7-day limit of the intraday endpoint.

        Args:
            contract_id: Option contract symbol (e.g., "AAPL251017C00150000")

        Returns:
            List of historic daily records with IV, OI, volume, prices, etc.
            Each record includes: date, implied_volatility, open_interest, volume,
            nbbo_bid, nbbo_ask, and more.

        Note:
            - Response uses 'chains' key, NOT 'data' key!
            - Not all dates may have IV data (contracts exist before being actively traded)
            - Typical history: 125-262 days depending on when contract was created
        """
        url = f"{self.BASE_URL}/api/option-contract/{contract_id}/historic"

        response = await self._request_with_retry("GET", url)
        data = response.json()

        # IMPORTANT: Historic endpoint uses 'chains' key, not 'data'!
        historic_data = data.get("chains", [])
        logger.info(f"Fetched {len(historic_data)} historic records for {contract_id}")
        return historic_data

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

        response = await self._request_with_retry("GET", url)
        data = response.json()

        earnings = data.get("data", [])
        logger.info(f"Fetched {len(earnings)} earnings records for {ticker}")
        return earnings

    async def get_expiry_breakdown(
        self,
        ticker: str,
        date: Optional[str] = None
    ) -> List[str]:
        """Fetch all available option expiration dates for a ticker.

        Args:
            ticker: Stock symbol
            date: Optional date in YYYY-MM-DD format (defaults to last trading date)

        Returns:
            List of expiration dates in YYYY-MM-DD format
        """
        url = f"{self.BASE_URL}/api/stock/{ticker}/expiry-breakdown"
        params = {}
        if date:
            params["date"] = date

        response = await self._request_with_retry("GET", url, params=params)
        data = response.json()

        # Extract expiry dates from response (API uses "expires" not "expiry")
        expirations = [item.get("expires") for item in data.get("data", []) if item.get("expires")]
        logger.info(f"Fetched {len(expirations)} available expirations for {ticker}")
        return expirations
