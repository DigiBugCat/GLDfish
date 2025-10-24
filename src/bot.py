"""Discord bot for IV chart generation."""

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
import os
import logging
from typing import Optional
from dotenv import load_dotenv
import asyncio
from dateutil import parser as date_parser
from datetime import datetime

from .data_fetcher import UnusualWhalesClient
from .openrouter_client import OpenRouterClient
from .utils import (
    identify_required_strikes,
    identify_required_strikes_by_date,
    align_data_by_timestamp,
    align_historic_data,
    get_trading_dates,
    collect_earnings_iv_data
)
from .chart_generator import create_iv_chart, create_error_chart, create_earnings_iv_chart
from .database import ChartDatabase

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class ChartControlView(View):
    """View with buttons for chart refresh and delete."""

    def __init__(self, ticker: str, expiration: str, option_type: str, days: int, user_id: int, bot_instance):
        super().__init__(timeout=None)  # Never timeout
        self.ticker = ticker
        self.expiration = expiration
        self.option_type = option_type
        self.days = days
        self.user_id = user_id
        self.bot = bot_instance

    @discord.ui.button(label="◀ Prev Exp", style=discord.ButtonStyle.secondary)
    async def prev_expiration_button(self, interaction: discord.Interaction, button: Button):
        """Navigate to previous expiration date."""
        await interaction.response.defer()

        try:
            # Update message to show we're working
            await interaction.edit_original_response(
                content=f"🔍 Finding previous expiration for {self.ticker}..."
            )

            # Fetch all available expirations
            expirations = await self.bot.uw_client.get_expiry_breakdown(ticker=self.ticker)

            if not expirations:
                await interaction.followup.send("❌ Could not fetch available expirations", ephemeral=True)
                return

            # Sort expirations chronologically
            expirations = sorted(expirations)

            # Find current expiration index
            try:
                current_index = expirations.index(self.expiration)
            except ValueError:
                await interaction.followup.send("❌ Current expiration not found in available expirations", ephemeral=True)
                return

            # Check if we can go to previous
            if current_index == 0:
                await interaction.followup.send("⚠️ Already at the earliest expiration", ephemeral=True)
                return

            # Get previous expiration
            new_expiration = expirations[current_index - 1]

            logger.info(f"Navigating from {self.expiration} to previous expiration {new_expiration}")

            await interaction.edit_original_response(
                content=f"📊 Fetching data for {self.ticker} {self.option_type} {new_expiration}..."
            )

            # Fetch data and regenerate chart (same logic as refresh)
            use_historic_mode = self.days > 7
            candle_size = "4h" if use_historic_mode else "1m"

            ohlc_data = await self.bot.uw_client.get_ohlc_data(
                ticker=self.ticker,
                candle_size=candle_size,
                days_back=self.days
            )

            contracts = await self.bot.uw_client.get_option_chains(ticker=self.ticker)

            strike_map = self.bot.uw_client.filter_contracts_by_expiration_and_type(
                contracts=contracts,
                expiration_date=new_expiration,
                option_type=self.option_type
            )

            if not strike_map:
                await interaction.followup.send(f"❌ No {self.option_type} contracts found for {new_expiration}", ephemeral=True)
                return

            available_strikes = list(strike_map.keys())
            strikes_by_date = identify_required_strikes_by_date(ohlc_data, available_strikes)

            if use_historic_mode:
                required_strikes = set()
                for strikes_list in strikes_by_date.values():
                    required_strikes.update(strikes_list)
                required_strikes = sorted(list(required_strikes))

                async def fetch_historic_for_strike(strike: float, contract_id: str):
                    try:
                        historic_records = await self.bot.uw_client.get_option_historic(contract_id)
                        historic_records = [r for r in historic_records if r.get("implied_volatility")]
                        return (strike, historic_records)
                    except Exception as e:
                        logger.warning(f"Failed to fetch historic data for {contract_id}: {e}")
                        return (strike, [])

                tasks = [fetch_historic_for_strike(strike, strike_map[strike]) for strike in required_strikes]
                results = await asyncio.gather(*tasks)

                historic_iv_by_strike = {strike: records for strike, records in results}
                aligned_data = align_historic_data(ohlc_data, historic_iv_by_strike)
            else:
                iv_data_by_strike = {}

                async def fetch_iv_for_strike_date(strike: float, date: str, contract_id: str):
                    try:
                        intraday_data = await self.bot.uw_client.get_option_intraday(
                            contract_id=contract_id,
                            date=date
                        )
                        return (strike, intraday_data)
                    except Exception as e:
                        logger.warning(f"Failed to fetch IV for {contract_id} on {date}: {e}")
                        return (strike, [])

                tasks = []
                for date, strikes_for_date in strikes_by_date.items():
                    for strike in strikes_for_date:
                        contract_id = strike_map[strike]
                        tasks.append(fetch_iv_for_strike_date(strike, date, contract_id))

                results = await asyncio.gather(*tasks)

                for strike, intraday_data in results:
                    if strike not in iv_data_by_strike:
                        iv_data_by_strike[strike] = []
                    iv_data_by_strike[strike].extend(intraday_data)

                aligned_data = align_data_by_timestamp(ohlc_data, iv_data_by_strike)

            if not aligned_data:
                await interaction.followup.send("❌ Could not align data for new expiration", ephemeral=True)
                return

            # Fetch earnings dates
            earnings_dates = []
            try:
                earnings_data = await self.bot.uw_client.get_earnings(self.ticker)
                if earnings_data and ohlc_data:
                    from datetime import datetime as dt

                    ohlc_timestamps = []
                    for candle in ohlc_data:
                        timestamp_str = candle.get("start_time") or candle.get("timestamp")
                        if timestamp_str:
                            try:
                                ts = dt.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                                ohlc_timestamps.append(ts)
                            except:
                                continue

                    if ohlc_timestamps:
                        chart_start = min(ohlc_timestamps).replace(tzinfo=None)
                        chart_end = max(ohlc_timestamps).replace(tzinfo=None)

                        for event in earnings_data:
                            report_date_str = event.get("report_date")
                            if report_date_str:
                                try:
                                    report_date = dt.strptime(report_date_str, "%Y-%m-%d")
                                    if chart_start <= report_date <= chart_end:
                                        earnings_dates.append(report_date_str)
                                except:
                                    continue
            except Exception as e:
                logger.warning(f"Could not fetch earnings data: {e}")

            # Generate chart
            await interaction.edit_original_response(
                content=f"📈 Generating chart for {new_expiration}..."
            )

            chart_buffer = create_iv_chart(
                data=aligned_data,
                ticker=self.ticker,
                expiration=new_expiration,
                option_type=self.option_type,
                days=self.days,
                earnings_dates=earnings_dates if earnings_dates else None
            )

            # Update database
            self.bot.db.update_chart(
                message_id=interaction.message.id,
                expiration=new_expiration
            )

            # Update instance variable
            self.expiration = new_expiration

            # Edit message
            file = discord.File(chart_buffer, filename="iv_chart.png")
            await interaction.edit_original_response(
                content=f"**{self.ticker} {self.option_type.capitalize()} IV Chart** (Exp: {new_expiration})",
                attachments=[file]
            )

            logger.info(f"Successfully navigated to previous expiration {new_expiration}")

        except Exception as e:
            logger.error(f"Error navigating to previous expiration: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Swap Call/Put", style=discord.ButtonStyle.secondary)
    async def swap_option_type_button(self, interaction: discord.Interaction, button: Button):
        """Swap between call and put options."""
        await interaction.response.defer()

        try:
            # Toggle option type
            new_option_type = "put" if self.option_type == "call" else "call"

            logger.info(f"Swapping from {self.option_type} to {new_option_type}")

            await interaction.edit_original_response(
                content=f"🔄 Switching to {new_option_type}s for {self.ticker} {self.expiration}..."
            )

            # Fetch data and regenerate chart (same logic as refresh)
            use_historic_mode = self.days > 7
            candle_size = "4h" if use_historic_mode else "1m"

            ohlc_data = await self.bot.uw_client.get_ohlc_data(
                ticker=self.ticker,
                candle_size=candle_size,
                days_back=self.days
            )

            contracts = await self.bot.uw_client.get_option_chains(ticker=self.ticker)

            strike_map = self.bot.uw_client.filter_contracts_by_expiration_and_type(
                contracts=contracts,
                expiration_date=self.expiration,
                option_type=new_option_type
            )

            if not strike_map:
                await interaction.followup.send(f"❌ No {new_option_type} contracts found for {self.expiration}", ephemeral=True)
                return

            available_strikes = list(strike_map.keys())
            strikes_by_date = identify_required_strikes_by_date(ohlc_data, available_strikes)

            if use_historic_mode:
                required_strikes = set()
                for strikes_list in strikes_by_date.values():
                    required_strikes.update(strikes_list)
                required_strikes = sorted(list(required_strikes))

                async def fetch_historic_for_strike(strike: float, contract_id: str):
                    try:
                        historic_records = await self.bot.uw_client.get_option_historic(contract_id)
                        historic_records = [r for r in historic_records if r.get("implied_volatility")]
                        return (strike, historic_records)
                    except Exception as e:
                        logger.warning(f"Failed to fetch historic data for {contract_id}: {e}")
                        return (strike, [])

                tasks = [fetch_historic_for_strike(strike, strike_map[strike]) for strike in required_strikes]
                results = await asyncio.gather(*tasks)

                historic_iv_by_strike = {strike: records for strike, records in results}
                aligned_data = align_historic_data(ohlc_data, historic_iv_by_strike)
            else:
                iv_data_by_strike = {}

                async def fetch_iv_for_strike_date(strike: float, date: str, contract_id: str):
                    try:
                        intraday_data = await self.bot.uw_client.get_option_intraday(
                            contract_id=contract_id,
                            date=date
                        )
                        return (strike, intraday_data)
                    except Exception as e:
                        logger.warning(f"Failed to fetch IV for {contract_id} on {date}: {e}")
                        return (strike, [])

                tasks = []
                for date, strikes_for_date in strikes_by_date.items():
                    for strike in strikes_for_date:
                        contract_id = strike_map[strike]
                        tasks.append(fetch_iv_for_strike_date(strike, date, contract_id))

                results = await asyncio.gather(*tasks)

                for strike, intraday_data in results:
                    if strike not in iv_data_by_strike:
                        iv_data_by_strike[strike] = []
                    iv_data_by_strike[strike].extend(intraday_data)

                aligned_data = align_data_by_timestamp(ohlc_data, iv_data_by_strike)

            if not aligned_data:
                await interaction.followup.send(f"❌ Could not align data for {new_option_type}", ephemeral=True)
                return

            # Fetch earnings dates
            earnings_dates = []
            try:
                earnings_data = await self.bot.uw_client.get_earnings(self.ticker)
                if earnings_data and ohlc_data:
                    from datetime import datetime as dt

                    ohlc_timestamps = []
                    for candle in ohlc_data:
                        timestamp_str = candle.get("start_time") or candle.get("timestamp")
                        if timestamp_str:
                            try:
                                ts = dt.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                                ohlc_timestamps.append(ts)
                            except:
                                continue

                    if ohlc_timestamps:
                        chart_start = min(ohlc_timestamps).replace(tzinfo=None)
                        chart_end = max(ohlc_timestamps).replace(tzinfo=None)

                        for event in earnings_data:
                            report_date_str = event.get("report_date")
                            if report_date_str:
                                try:
                                    report_date = dt.strptime(report_date_str, "%Y-%m-%d")
                                    if chart_start <= report_date <= chart_end:
                                        earnings_dates.append(report_date_str)
                                except:
                                    continue
            except Exception as e:
                logger.warning(f"Could not fetch earnings data: {e}")

            # Generate chart
            await interaction.edit_original_response(
                content=f"📈 Generating {new_option_type} chart..."
            )

            chart_buffer = create_iv_chart(
                data=aligned_data,
                ticker=self.ticker,
                expiration=self.expiration,
                option_type=new_option_type,
                days=self.days,
                earnings_dates=earnings_dates if earnings_dates else None
            )

            # Update database
            self.bot.db.update_chart(
                message_id=interaction.message.id,
                option_type=new_option_type
            )

            # Update instance variable
            self.option_type = new_option_type

            # Edit message
            file = discord.File(chart_buffer, filename="iv_chart.png")
            await interaction.edit_original_response(
                content=f"**{self.ticker} {new_option_type.capitalize()} IV Chart** (Exp: {self.expiration})",
                attachments=[file]
            )

            logger.info(f"Successfully swapped to {new_option_type}")

        except Exception as e:
            logger.error(f"Error swapping option type: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Next Exp ▶", style=discord.ButtonStyle.secondary)
    async def next_expiration_button(self, interaction: discord.Interaction, button: Button):
        """Navigate to next expiration date."""
        await interaction.response.defer()

        try:
            # Update message to show we're working
            await interaction.edit_original_response(
                content=f"🔍 Finding next expiration for {self.ticker}..."
            )

            # Fetch all available expirations
            expirations = await self.bot.uw_client.get_expiry_breakdown(ticker=self.ticker)

            if not expirations:
                await interaction.followup.send("❌ Could not fetch available expirations", ephemeral=True)
                return

            # Sort expirations chronologically
            expirations = sorted(expirations)

            # Find current expiration index
            try:
                current_index = expirations.index(self.expiration)
            except ValueError:
                await interaction.followup.send("❌ Current expiration not found in available expirations", ephemeral=True)
                return

            # Check if we can go to next
            if current_index >= len(expirations) - 1:
                await interaction.followup.send("⚠️ Already at the latest expiration", ephemeral=True)
                return

            # Get next expiration
            new_expiration = expirations[current_index + 1]

            logger.info(f"Navigating from {self.expiration} to next expiration {new_expiration}")

            await interaction.edit_original_response(
                content=f"📊 Fetching data for {self.ticker} {self.option_type} {new_expiration}..."
            )

            # Fetch data and regenerate chart (same logic as refresh)
            use_historic_mode = self.days > 7
            candle_size = "4h" if use_historic_mode else "1m"

            ohlc_data = await self.bot.uw_client.get_ohlc_data(
                ticker=self.ticker,
                candle_size=candle_size,
                days_back=self.days
            )

            contracts = await self.bot.uw_client.get_option_chains(ticker=self.ticker)

            strike_map = self.bot.uw_client.filter_contracts_by_expiration_and_type(
                contracts=contracts,
                expiration_date=new_expiration,
                option_type=self.option_type
            )

            if not strike_map:
                await interaction.followup.send(f"❌ No {self.option_type} contracts found for {new_expiration}", ephemeral=True)
                return

            available_strikes = list(strike_map.keys())
            strikes_by_date = identify_required_strikes_by_date(ohlc_data, available_strikes)

            if use_historic_mode:
                required_strikes = set()
                for strikes_list in strikes_by_date.values():
                    required_strikes.update(strikes_list)
                required_strikes = sorted(list(required_strikes))

                async def fetch_historic_for_strike(strike: float, contract_id: str):
                    try:
                        historic_records = await self.bot.uw_client.get_option_historic(contract_id)
                        historic_records = [r for r in historic_records if r.get("implied_volatility")]
                        return (strike, historic_records)
                    except Exception as e:
                        logger.warning(f"Failed to fetch historic data for {contract_id}: {e}")
                        return (strike, [])

                tasks = [fetch_historic_for_strike(strike, strike_map[strike]) for strike in required_strikes]
                results = await asyncio.gather(*tasks)

                historic_iv_by_strike = {strike: records for strike, records in results}
                aligned_data = align_historic_data(ohlc_data, historic_iv_by_strike)
            else:
                iv_data_by_strike = {}

                async def fetch_iv_for_strike_date(strike: float, date: str, contract_id: str):
                    try:
                        intraday_data = await self.bot.uw_client.get_option_intraday(
                            contract_id=contract_id,
                            date=date
                        )
                        return (strike, intraday_data)
                    except Exception as e:
                        logger.warning(f"Failed to fetch IV for {contract_id} on {date}: {e}")
                        return (strike, [])

                tasks = []
                for date, strikes_for_date in strikes_by_date.items():
                    for strike in strikes_for_date:
                        contract_id = strike_map[strike]
                        tasks.append(fetch_iv_for_strike_date(strike, date, contract_id))

                results = await asyncio.gather(*tasks)

                for strike, intraday_data in results:
                    if strike not in iv_data_by_strike:
                        iv_data_by_strike[strike] = []
                    iv_data_by_strike[strike].extend(intraday_data)

                aligned_data = align_data_by_timestamp(ohlc_data, iv_data_by_strike)

            if not aligned_data:
                await interaction.followup.send("❌ Could not align data for new expiration", ephemeral=True)
                return

            # Fetch earnings dates
            earnings_dates = []
            try:
                earnings_data = await self.bot.uw_client.get_earnings(self.ticker)
                if earnings_data and ohlc_data:
                    from datetime import datetime as dt

                    ohlc_timestamps = []
                    for candle in ohlc_data:
                        timestamp_str = candle.get("start_time") or candle.get("timestamp")
                        if timestamp_str:
                            try:
                                ts = dt.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                                ohlc_timestamps.append(ts)
                            except:
                                continue

                    if ohlc_timestamps:
                        chart_start = min(ohlc_timestamps).replace(tzinfo=None)
                        chart_end = max(ohlc_timestamps).replace(tzinfo=None)

                        for event in earnings_data:
                            report_date_str = event.get("report_date")
                            if report_date_str:
                                try:
                                    report_date = dt.strptime(report_date_str, "%Y-%m-%d")
                                    if chart_start <= report_date <= chart_end:
                                        earnings_dates.append(report_date_str)
                                except:
                                    continue
            except Exception as e:
                logger.warning(f"Could not fetch earnings data: {e}")

            # Generate chart
            await interaction.edit_original_response(
                content=f"📈 Generating chart for {new_expiration}..."
            )

            chart_buffer = create_iv_chart(
                data=aligned_data,
                ticker=self.ticker,
                expiration=new_expiration,
                option_type=self.option_type,
                days=self.days,
                earnings_dates=earnings_dates if earnings_dates else None
            )

            # Update database
            self.bot.db.update_chart(
                message_id=interaction.message.id,
                expiration=new_expiration
            )

            # Update instance variable
            self.expiration = new_expiration

            # Edit message
            file = discord.File(chart_buffer, filename="iv_chart.png")
            await interaction.edit_original_response(
                content=f"**{self.ticker} {self.option_type.capitalize()} IV Chart** (Exp: {new_expiration})",
                attachments=[file]
            )

            logger.info(f"Successfully navigated to next expiration {new_expiration}")

        except Exception as e:
            logger.error(f"Error navigating to next expiration: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, emoji="🔄")
    async def refresh_button(self, interaction: discord.Interaction, button: Button):
        """Refresh the chart with latest data."""
        await interaction.response.defer()

        try:
            # Determine mode based on days (same as main command)
            use_historic_mode = self.days > 7
            candle_size = "4h" if use_historic_mode else "1m"

            logger.info(f"Refreshing chart: {self.ticker} ({self.days} days, {candle_size} mode)")

            # Fetch fresh data and regenerate chart (same logic as main command)
            ohlc_data = await self.bot.uw_client.get_ohlc_data(
                ticker=self.ticker,
                candle_size=candle_size,
                days_back=self.days
            )

            contracts = await self.bot.uw_client.get_option_chains(ticker=self.ticker)

            strike_map = self.bot.uw_client.filter_contracts_by_expiration_and_type(
                contracts=contracts,
                expiration_date=self.expiration,
                option_type=self.option_type
            )

            if not strike_map:
                await interaction.followup.send("❌ Could not refresh: No contracts found", ephemeral=True)
                return

            available_strikes = list(strike_map.keys())
            strikes_by_date = identify_required_strikes_by_date(ohlc_data, available_strikes)

            if use_historic_mode:
                # HISTORIC MODE
                # Get unique strikes
                required_strikes = set()
                for strikes_list in strikes_by_date.values():
                    required_strikes.update(strikes_list)
                required_strikes = sorted(list(required_strikes))

                # Fetch historic data for each strike CONCURRENTLY
                async def fetch_historic_for_strike(strike: float, contract_id: str):
                    """Fetch historic data for a single strike."""
                    try:
                        historic_records = await self.bot.uw_client.get_option_historic(contract_id)
                        historic_records = [r for r in historic_records if r.get("implied_volatility")]
                        return (strike, historic_records)
                    except Exception as e:
                        logger.warning(f"Failed to fetch historic data for {contract_id}: {e}")
                        return (strike, [])

                # Build list of all tasks
                tasks = []
                for strike in required_strikes:
                    contract_id = strike_map[strike]
                    tasks.append(fetch_historic_for_strike(strike, contract_id))

                # Execute all tasks concurrently
                results = await asyncio.gather(*tasks)

                # Build strike map from results
                historic_iv_by_strike = {}
                for strike, historic_records in results:
                    historic_iv_by_strike[strike] = historic_records

                aligned_data = align_historic_data(ohlc_data, historic_iv_by_strike)

            else:
                # INTRADAY MODE
                iv_data_by_strike = {}

                # Use concurrent fetching for refresh (same as main command)
                async def fetch_iv_for_strike_date(strike: float, date: str, contract_id: str):
                    """Fetch IV data for a single strike/date combination."""
                    try:
                        intraday_data = await self.bot.uw_client.get_option_intraday(
                            contract_id=contract_id,
                            date=date
                        )
                        return (strike, intraday_data)
                    except Exception as e:
                        logger.warning(f"Failed to fetch IV for {contract_id} on {date}: {e}")
                        return (strike, [])

                # Build list of all tasks
                tasks = []
                for date, strikes_for_date in strikes_by_date.items():
                    for strike in strikes_for_date:
                        contract_id = strike_map[strike]
                        tasks.append(fetch_iv_for_strike_date(strike, date, contract_id))

                # Execute all tasks concurrently
                logger.info(f"Fetching {len(tasks)} strike/date combinations concurrently for refresh...")
                results = await asyncio.gather(*tasks)

                # Organize results by strike
                for strike, intraday_data in results:
                    if strike not in iv_data_by_strike:
                        iv_data_by_strike[strike] = []
                    iv_data_by_strike[strike].extend(intraday_data)

                aligned_data = align_data_by_timestamp(ohlc_data, iv_data_by_strike)

            if not aligned_data:
                await interaction.followup.send("❌ Could not refresh: Data alignment failed", ephemeral=True)
                return

            # Fetch earnings dates (optional - don't fail if unavailable)
            earnings_dates = []
            try:
                earnings_data = await self.bot.uw_client.get_earnings(self.ticker)
                if earnings_data and ohlc_data:
                    # Extract report dates that fall within ACTUAL OHLC data range
                    from datetime import datetime as dt

                    # Get actual date range from OHLC data
                    ohlc_timestamps = []
                    for candle in ohlc_data:
                        timestamp_str = candle.get("start_time") or candle.get("timestamp")
                        if timestamp_str:
                            try:
                                ts = dt.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                                ohlc_timestamps.append(ts)
                            except:
                                continue

                    if ohlc_timestamps:
                        chart_start = min(ohlc_timestamps).replace(tzinfo=None)
                        chart_end = max(ohlc_timestamps).replace(tzinfo=None)

                        logger.info(f"Refresh - Chart date range: {chart_start.date()} to {chart_end.date()}")

                        for event in earnings_data:
                            report_date_str = event.get("report_date")
                            if report_date_str:
                                try:
                                    report_date = dt.strptime(report_date_str, "%Y-%m-%d")
                                    # Only include earnings within actual chart range
                                    if chart_start <= report_date <= chart_end:
                                        earnings_dates.append(report_date_str)
                                except:
                                    continue

                        if earnings_dates:
                            logger.info(f"Found {len(earnings_dates)} earnings dates for refresh: {earnings_dates}")
            except Exception as e:
                logger.warning(f"Could not fetch earnings data for refresh: {e}")

            # Generate new chart
            chart_buffer = create_iv_chart(
                data=aligned_data,
                ticker=self.ticker,
                expiration=self.expiration,
                option_type=self.option_type,
                days=self.days,
                earnings_dates=earnings_dates if earnings_dates else None
            )

            # Edit message with new chart
            file = discord.File(chart_buffer, filename="iv_chart.png")
            await interaction.edit_original_response(
                content=f"**{self.ticker} {self.option_type.capitalize()} IV Chart** (Exp: {self.expiration}) [Refreshed]",
                attachments=[file]
            )

            logger.info(f"Successfully refreshed chart for {self.ticker}")

        except Exception as e:
            logger.error(f"Error refreshing chart: {e}", exc_info=True)
            await interaction.followup.send(f"❌ Error refreshing chart: {str(e)}", ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def delete_button(self, interaction: discord.Interaction, button: Button):
        """Delete the chart (only original requester can delete)."""
        # Only allow the user who requested the chart to delete it
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Only the person who requested this chart can delete it.", ephemeral=True)
            return

        try:
            # Delete from database
            self.bot.db.delete_chart(interaction.message.id)

            # Delete the message
            await interaction.message.delete()

            logger.info(f"Successfully deleted chart {interaction.message.id}")
        except Exception as e:
            logger.error(f"Error deleting chart: {e}", exc_info=True)
            await interaction.response.send_message(f"❌ Error deleting chart: {str(e)}", ephemeral=True)


class IVBot(commands.Bot):
    """Discord bot for generating IV charts."""

    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(command_prefix="!", intents=intents)

        # Initialize API client
        api_key = os.getenv("UNUSUAL_WHALES_API_KEY")
        if not api_key:
            raise ValueError("UNUSUAL_WHALES_API_KEY not found in environment")

        self.uw_client = UnusualWhalesClient(api_key)

        # Initialize OpenRouter client
        openrouter_key = os.getenv("OPENROUTER_API_KEY")
        if openrouter_key:
            self.openrouter_client = OpenRouterClient(openrouter_key)
            logger.info("OpenRouter client initialized for AI news summarization")
        else:
            self.openrouter_client = None
            logger.warning("OPENROUTER_API_KEY not found - /market_news command will be disabled")

        # Initialize database
        os.makedirs("data", exist_ok=True)
        self.db = ChartDatabase("data/charts.db")

        # Load whitelisted users for DM access
        whitelist_str = os.getenv("WHITELISTED_USERS", "")
        self.whitelisted_users = set()
        if whitelist_str:
            for user_id in whitelist_str.split(","):
                user_id = user_id.strip()
                if user_id:
                    try:
                        self.whitelisted_users.add(int(user_id))
                    except ValueError:
                        logger.warning(f"Invalid user ID in WHITELISTED_USERS: {user_id}")

        if self.whitelisted_users:
            logger.info(f"DM whitelist enabled with {len(self.whitelisted_users)} user(s)")
        else:
            logger.info("DM whitelist disabled (all users can use bot in DMs)")

    async def setup_hook(self):
        """Setup hook called when bot is ready."""
        await self.tree.sync()
        logger.info("Command tree synced")

    async def on_ready(self):
        """Called when bot is ready."""
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info("------")


# Initialize bot
bot = IVBot()


def is_dm_whitelisted():
    """Check decorator to enforce DM whitelist."""
    async def predicate(interaction: discord.Interaction) -> bool:
        # If in a guild (server), allow everyone
        if interaction.guild is not None:
            return True

        # If in DM and whitelist is empty, allow everyone
        if not bot.whitelisted_users:
            return True

        # If in DM and whitelist is enabled, check if user is whitelisted
        if interaction.user.id in bot.whitelisted_users:
            return True

        # User not whitelisted for DMs
        await interaction.response.send_message(
            "❌ You are not authorized to use this bot in DMs. "
            "Please use the bot in a server or contact the bot owner for access.",
            ephemeral=True
        )
        return False

    return app_commands.check(predicate)


@bot.tree.command(
    name="iv_chart",
    description="Generate an IV chart for an option (intraday ≤7d, historic >7d)"
)
@app_commands.describe(
    ticker="Stock ticker symbol (e.g., AAPL)",
    expiration="Option expiration date (YYYY-MM-DD)",
    option_type="Call or Put",
    days="Lookback period in days: 1-365 (≤7d=1m candles, >7d=4h candles)"
)
@app_commands.choices(option_type=[
    app_commands.Choice(name="Call", value="call"),
    app_commands.Choice(name="Put", value="put")
])
@app_commands.rename(days="days")
@is_dm_whitelisted()
async def iv_chart(
    interaction: discord.Interaction,
    ticker: str,
    expiration: str,
    option_type: app_commands.Choice[str],
    days: app_commands.Range[int, 1, 365] = 2
):
    """Generate IV chart command handler."""
    await interaction.response.defer()

    try:
        logger.info(
            f"Processing IV chart request: {ticker} {option_type.value} "
            f"{expiration} ({days} days)"
        )

        # Validate and parse inputs
        ticker = ticker.upper()
        option_type_str = option_type.value

        # Parse expiration date - handle multiple formats
        try:
            # Try to parse various date formats
            parsed_date = date_parser.parse(expiration, fuzzy=True)
            expiration_formatted = parsed_date.strftime("%Y-%m-%d")
            logger.info(f"Parsed expiration '{expiration}' as {expiration_formatted}")
        except Exception as e:
            await interaction.edit_original_response(
                content=f"❌ Could not parse expiration date '{expiration}'. "
                f"Please use format like: 2026-03-31, 3/31/2026, March 31 2026, etc."
            )
            return

        # Find closest available expiration to the requested date
        await interaction.edit_original_response(
            content=f"Finding closest expiration to {expiration_formatted}..."
        )

        # Store the original requested expiration for comparison
        requested_expiration = expiration_formatted
        expiration_note = ""

        try:
            available_expirations = await bot.uw_client.get_expiry_breakdown(ticker=ticker)

            if not available_expirations:
                await interaction.edit_original_response(
                    content=f"❌ No option expirations found for {ticker}"
                )
                return

            # Find the closest expiration date
            from datetime import datetime as dt
            target_date = dt.strptime(expiration_formatted, "%Y-%m-%d")

            closest_expiration = min(
                available_expirations,
                key=lambda exp: abs((dt.strptime(exp, "%Y-%m-%d") - target_date).days)
            )

            # Calculate the difference in days
            closest_date = dt.strptime(closest_expiration, "%Y-%m-%d")
            days_diff = (closest_date - target_date).days

            if closest_expiration != expiration_formatted:
                diff_str = f"{abs(days_diff)} day{'s' if abs(days_diff) != 1 else ''} {'later' if days_diff > 0 else 'earlier'}"
                logger.info(f"Using closest expiration {closest_expiration} ({diff_str}) instead of exact {expiration_formatted}")
                expiration_note = f" *(closest to {requested_expiration}, {diff_str})*"
                expiration_formatted = closest_expiration
            else:
                logger.info(f"Found exact match for expiration {expiration_formatted}")

        except Exception as e:
            logger.error(f"Error finding closest expiration: {e}", exc_info=True)
            await interaction.edit_original_response(
                content=f"❌ Error finding available expirations: {str(e)}"
            )
            return

        # Determine which mode to use based on lookback period
        use_historic_mode = days > 7

        if use_historic_mode:
            logger.info(f"Using HISTORIC mode for {days} days (4h candles + historic IV)")
            candle_size = "4h"
        else:
            logger.info(f"Using INTRADAY mode for {days} days (1m candles + intraday IV)")
            candle_size = "1m"

        # Step 1: Fetch OHLC data (candle size depends on mode)
        await interaction.edit_original_response(
            content=f"Fetching {days} days of price data for {ticker}... ({candle_size} candles)"
        )
        ohlc_data = await bot.uw_client.get_ohlc_data(
            ticker=ticker,
            candle_size=candle_size,
            days_back=days
        )

        if not ohlc_data:
            await interaction.edit_original_response(
                content=f"❌ No price data found for {ticker}"
            )
            return

        # Step 2: Fetch option chains
        await interaction.edit_original_response(
            content=f"Fetching option chains for {ticker}..."
        )
        contracts = await bot.uw_client.get_option_chains(ticker=ticker)

        if not contracts:
            await interaction.edit_original_response(
                content=f"❌ No option contracts found for {ticker}"
            )
            return

        # Step 3: Filter contracts by expiration and type
        strike_map = bot.uw_client.filter_contracts_by_expiration_and_type(
            contracts=contracts,
            expiration_date=expiration_formatted,
            option_type=option_type_str
        )

        if not strike_map:
            await interaction.edit_original_response(
                content=f"❌ No {option_type_str} contracts found for expiration {expiration_formatted}"
            )
            return

        # Step 4-6: Fetch IV data and align (different logic for intraday vs historic)
        if use_historic_mode:
            # HISTORIC MODE: Use historic endpoint for EOD IV data
            await interaction.edit_original_response(
                content=f"Identifying required strikes..."
            )

            # Identify required strikes from 4h candles
            available_strikes = list(strike_map.keys())
            strikes_by_date = identify_required_strikes_by_date(ohlc_data, available_strikes)

            if not strikes_by_date:
                await interaction.edit_original_response(
                    content="❌ Could not identify required strikes"
                )
                return

            # Get unique strikes (historic endpoint returns all dates at once)
            required_strikes = set()
            for strikes_list in strikes_by_date.values():
                required_strikes.update(strikes_list)
            required_strikes = sorted(list(required_strikes))

            logger.info(f"Historic mode: fetching IV for {len(required_strikes)} strikes")

            await interaction.edit_original_response(
                content=f"Fetching historic IV data for {len(required_strikes)} strikes..."
            )

            # Fetch historic data for each strike CONCURRENTLY
            async def fetch_historic_for_strike(strike: float, contract_id: str):
                """Fetch historic data for a single strike."""
                try:
                    historic_records = await bot.uw_client.get_option_historic(contract_id)
                    # Filter out records without IV data
                    historic_records = [r for r in historic_records if r.get("implied_volatility")]
                    logger.info(f"Strike ${strike}: {len(historic_records)} historic records with IV")
                    return (strike, historic_records)
                except Exception as e:
                    logger.warning(f"Failed to fetch historic data for {contract_id}: {e}")
                    return (strike, [])

            # Build tasks for concurrent execution
            tasks = []
            for strike in required_strikes:
                contract_id = strike_map[strike]
                tasks.append(fetch_historic_for_strike(strike, contract_id))

            # Execute all historic fetches concurrently
            logger.info(f"Fetching {len(tasks)} strikes concurrently...")
            results = await asyncio.gather(*tasks)

            # Organize results
            historic_iv_by_strike = {}
            for strike, historic_records in results:
                historic_iv_by_strike[strike] = historic_records

            # Align 4h OHLC with historic IV
            aligned_data = align_historic_data(ohlc_data, historic_iv_by_strike)

        else:
            # INTRADAY MODE: Use existing intraday logic (unchanged)
            await interaction.edit_original_response(
                content=f"Identifying required strikes..."
            )

            available_strikes = list(strike_map.keys())
            strikes_by_date = identify_required_strikes_by_date(ohlc_data, available_strikes)

            if not strikes_by_date:
                await interaction.edit_original_response(
                    content="❌ Could not identify required strikes"
                )
                return

            # Calculate total API calls for progress message
            total_calls = sum(len(strikes) for strikes in strikes_by_date.values())
            await interaction.edit_original_response(
                content=f"Fetching intraday IV data ({total_calls} optimized API calls)..."
            )

            # Fetch IV data for required strike/date combinations (optimized with concurrency)
            iv_data_by_strike = {}

            # Create all fetch tasks upfront for concurrent execution
            async def fetch_iv_for_strike_date(strike: float, date: str, contract_id: str):
                """Fetch IV data for a single strike/date combination."""
                try:
                    intraday_data = await bot.uw_client.get_option_intraday(
                        contract_id=contract_id,
                        date=date
                    )
                    return (strike, intraday_data)
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch IV for {contract_id} on {date}: {e}"
                    )
                    return (strike, [])

            # Build list of all tasks
            tasks = []
            for date, strikes_for_date in strikes_by_date.items():
                logger.info(f"Queuing {len(strikes_for_date)} strikes for {date}")
                for strike in strikes_for_date:
                    contract_id = strike_map[strike]
                    tasks.append(fetch_iv_for_strike_date(strike, date, contract_id))

            # Execute all tasks concurrently (rate limiting handled by _rate_limit())
            logger.info(f"Fetching {len(tasks)} strike/date combinations concurrently...")
            results = await asyncio.gather(*tasks)

            # Organize results by strike
            for strike, intraday_data in results:
                if strike not in iv_data_by_strike:
                    iv_data_by_strike[strike] = []
                iv_data_by_strike[strike].extend(intraday_data)

            # Align intraday data
            aligned_data = align_data_by_timestamp(ohlc_data, iv_data_by_strike)

        if not aligned_data:
            await interaction.edit_original_response(
                content="❌ Could not align price and IV data"
            )
            return

        # Step 7: Fetch earnings dates (optional - don't fail if unavailable)
        earnings_dates = []
        try:
            earnings_data = await bot.uw_client.get_earnings(ticker)
            if earnings_data and ohlc_data:
                # Extract report dates that fall within ACTUAL OHLC data range
                from datetime import datetime as dt

                # Get actual date range from OHLC data
                ohlc_timestamps = []
                for candle in ohlc_data:
                    timestamp_str = candle.get("start_time") or candle.get("timestamp")
                    if timestamp_str:
                        try:
                            ts = dt.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                            ohlc_timestamps.append(ts)
                        except:
                            continue

                if ohlc_timestamps:
                    chart_start = min(ohlc_timestamps).replace(tzinfo=None)
                    chart_end = max(ohlc_timestamps).replace(tzinfo=None)

                    logger.info(f"Chart date range: {chart_start.date()} to {chart_end.date()}")

                    for event in earnings_data:
                        report_date_str = event.get("report_date")
                        if report_date_str:
                            try:
                                report_date = dt.strptime(report_date_str, "%Y-%m-%d")
                                # Only include earnings within actual chart range
                                if chart_start <= report_date <= chart_end:
                                    earnings_dates.append(report_date_str)
                            except:
                                continue

                    if earnings_dates:
                        logger.info(f"Found {len(earnings_dates)} earnings dates within chart range: {earnings_dates}")
        except Exception as e:
            logger.warning(f"Could not fetch earnings data for {ticker}: {e}")

        # Step 8: Generate chart
        await interaction.edit_original_response(
            content="Generating chart..."
        )

        chart_buffer = create_iv_chart(
            data=aligned_data,
            ticker=ticker,
            expiration=expiration_formatted,
            option_type=option_type_str,
            days=days,
            earnings_dates=earnings_dates if earnings_dates else None
        )

        # Create view with buttons for refresh and delete
        view = ChartControlView(
            ticker=ticker,
            expiration=expiration_formatted,
            option_type=option_type_str,
            days=days,
            user_id=interaction.user.id,
            bot_instance=bot
        )

        # Send chart with final update to original response
        file = discord.File(chart_buffer, filename="iv_chart.png")
        await interaction.edit_original_response(
            content=f"**{ticker} {option_type.name} IV Chart** (Exp: {expiration_formatted}){expiration_note}",
            attachments=[file],
            view=view
        )

        # Get message ID to store in database
        original_message = await interaction.original_response()

        # Store chart metadata in database
        bot.db.store_chart(
            message_id=original_message.id,
            channel_id=original_message.channel.id,
            user_id=interaction.user.id,
            ticker=ticker,
            expiration=expiration_formatted,
            option_type=option_type_str,
            days=days
        )

        logger.info(f"Successfully generated chart for {ticker}")

    except Exception as e:
        logger.error(f"Error generating chart: {e}", exc_info=True)

        # Send error chart
        error_buffer = create_error_chart(str(e))
        file = discord.File(error_buffer, filename="error.png")
        await interaction.edit_original_response(
            content="❌ An error occurred while generating the chart:",
            attachments=[file]
        )


@bot.tree.command(
    name="earnings",
    description="Get upcoming earnings date and expected move for a ticker"
)
@app_commands.describe(
    ticker="Stock ticker symbol (e.g., AAPL, TSLA)"
)
@is_dm_whitelisted()
async def earnings(
    interaction: discord.Interaction,
    ticker: str
):
    """Get earnings information command handler."""
    await interaction.response.defer()

    try:
        ticker = ticker.upper()
        logger.info(f"Processing earnings request for {ticker}")

        # Fetch earnings data
        earnings_data = await bot.uw_client.get_earnings(ticker)

        if not earnings_data:
            await interaction.edit_original_response(
                content=f"❌ No earnings data found for {ticker}"
            )
            return

        # Find the next upcoming earnings (report_date in the future or today)
        from datetime import datetime
        today = datetime.now().date()

        upcoming_earnings = None
        past_earnings = []

        for event in earnings_data:
            report_date_str = event.get("report_date")
            if report_date_str:
                report_date = datetime.strptime(report_date_str, "%Y-%m-%d").date()
                if report_date >= today:
                    if upcoming_earnings is None or report_date < datetime.strptime(upcoming_earnings["report_date"], "%Y-%m-%d").date():
                        upcoming_earnings = event
                else:
                    past_earnings.append(event)

        # Sort past earnings by date descending
        past_earnings.sort(key=lambda x: x.get("report_date", ""), reverse=True)

        # Build response embed
        embed = discord.Embed(
            title=f"📊 {ticker} Earnings Information",
            color=0x00ff00 if upcoming_earnings else 0xff9800
        )

        if upcoming_earnings:
            report_date = upcoming_earnings.get("report_date", "N/A")
            report_time = upcoming_earnings.get("report_time", "N/A").capitalize()
            expected_move = upcoming_earnings.get("expected_move")
            expected_move_perc = upcoming_earnings.get("expected_move_perc")
            street_est = upcoming_earnings.get("street_mean_est")

            # Format expected move
            move_str = "N/A"
            if expected_move and expected_move_perc:
                try:
                    move_pct = float(expected_move_perc) * 100
                    move_str = f"${expected_move} ({move_pct:.2f}%)"
                except:
                    move_str = f"${expected_move}"

            embed.add_field(
                name="📅 Next Earnings Date",
                value=f"**{report_date}** ({report_time})",
                inline=False
            )
            embed.add_field(
                name="📈 Expected Move",
                value=move_str,
                inline=True
            )
            if street_est:
                embed.add_field(
                    name="💰 Street EPS Estimate",
                    value=f"${street_est}",
                    inline=True
                )
        else:
            embed.add_field(
                name="⚠️ No Upcoming Earnings",
                value="No future earnings dates found",
                inline=False
            )

        # Show most recent past earnings if available
        if past_earnings:
            recent = past_earnings[0]
            actual_eps = recent.get("actual_eps")
            post_move_1d = recent.get("post_earnings_move_1d")

            recent_info = f"**Date:** {recent.get('report_date', 'N/A')}"
            if actual_eps:
                recent_info += f"\n**Actual EPS:** ${actual_eps}"
            if post_move_1d:
                try:
                    move_pct = float(post_move_1d) * 100
                    recent_info += f"\n**1D Post Move:** {move_pct:+.2f}%"
                except:
                    pass

            embed.add_field(
                name="📝 Most Recent Earnings",
                value=recent_info,
                inline=False
            )

        embed.set_footer(text=f"Data from Unusual Whales • Total records: {len(earnings_data)}")

        await interaction.edit_original_response(embed=embed)
        logger.info(f"Successfully sent earnings info for {ticker}")

    except Exception as e:
        logger.error(f"Error fetching earnings: {e}", exc_info=True)
        await interaction.edit_original_response(
            content=f"❌ Error fetching earnings data: {str(e)}"
        )


@bot.tree.command(
    name="earnings_iv",
    description="Analyze how ATM IV behaves around earnings for different DTE buckets (14D/30D/60D/90D/180D)"
)
@app_commands.describe(
    ticker="Stock ticker symbol (e.g., NVDA, AAPL)",
    option_type="Type of options to analyze (call=default, put, or both)"
)
@app_commands.choices(option_type=[
    app_commands.Choice(name="Call (default)", value="call"),
    app_commands.Choice(name="Put", value="put"),
    app_commands.Choice(name="Both", value="both")
])
@is_dm_whitelisted()
async def earnings_iv(
    interaction: discord.Interaction,
    ticker: str,
    option_type: app_commands.Choice[str] = None
):
    """Analyze ATM IV behavior around earnings command handler."""
    await interaction.response.defer()

    try:
        ticker = ticker.upper()

        # Extract option_type value (default to "call")
        opt_type = option_type.value if option_type else "call"

        logger.info(f"Processing earnings IV analysis request for {ticker}, option_type={opt_type}")

        # Step 1: Collect earnings IV data
        await interaction.edit_original_response(
            content=f"🔍 Fetching earnings dates for {ticker}..."
        )

        await interaction.edit_original_response(
            content=f"📊 Analyzing ATM {opt_type.upper()} IV around last 3 earnings for {ticker}...\n"
                    f"This may take a few minutes (fetching historic IV data)..."
        )

        try:
            earnings_iv_data = await collect_earnings_iv_data(
                client=bot.uw_client,
                ticker=ticker,
                num_earnings=3,
                days_window=7,
                option_type=opt_type
            )
        except ValueError as e:
            await interaction.edit_original_response(
                content=f"❌ {str(e)}"
            )
            return

        # Check if we got any data
        if not earnings_iv_data['data'] or all(not v for v in earnings_iv_data['data'].values()):
            await interaction.edit_original_response(
                content=f"❌ No IV data available for {ticker} around recent earnings dates. "
                        f"This could mean:\n"
                        f"• Options didn't exist far enough back\n"
                        f"• Recent earnings are too recent (need historical data)\n"
                        f"• Try a more liquid ticker with longer option history"
            )
            return

        # Step 2: Generate chart
        await interaction.edit_original_response(
            content=f"📈 Generating earnings IV chart for {ticker}..."
        )

        chart_buffer = create_earnings_iv_chart(
            earnings_iv_data=earnings_iv_data,
            ticker=ticker
        )

        # Step 3: Send chart
        file = discord.File(chart_buffer, filename="earnings_iv_chart.png")

        earnings_dates_str = ", ".join(earnings_iv_data['earnings_dates'])
        await interaction.edit_original_response(
            content=f"**{ticker} - ATM IV Around Earnings**\n"
                    f"Analyzed earnings: {earnings_dates_str}\n"
                    f"Shows how IV for different expiration windows (14D/30D/60D/90D/180D) "
                    f"changes in the week before and after earnings.",
            attachments=[file]
        )

        logger.info(f"Successfully generated earnings IV chart for {ticker}")

    except Exception as e:
        logger.error(f"Error generating earnings IV chart: {e}", exc_info=True)

        # Send error chart
        error_buffer = create_error_chart(str(e))
        file = discord.File(error_buffer, filename="error.png")
        await interaction.edit_original_response(
            content="❌ An error occurred while generating the earnings IV chart:",
            attachments=[file]
        )


@bot.tree.command(
    name="market_news",
    description="Get AI-powered market news summary from recent headlines"
)
@app_commands.describe(
    question="Optional question about market events (e.g., 'Why is GLD dropping?')",
    hours="Hours to look back (1-24, default: 4)",
    major_only="Only include major market-moving news"
)
@app_commands.rename(hours="hours")
@is_dm_whitelisted()
async def market_news(
    interaction: discord.Interaction,
    question: Optional[str] = None,
    hours: app_commands.Range[int, 1, 24] = 4,
    major_only: bool = False
):
    """Generate AI-powered market news summary."""
    await interaction.response.defer()

    try:
        # Check if OpenRouter client is available
        if not bot.openrouter_client:
            await interaction.edit_original_response(
                content="❌ AI news summarization is not available. OPENROUTER_API_KEY is not configured."
            )
            return

        logger.info(
            f"Processing market news request: hours={hours}, major_only={major_only}, "
            f"question='{question or 'none'}'"
        )

        # Fetch news headlines with pagination
        await interaction.edit_original_response(
            content=f"🔍 Fetching market news from the last {hours} hours..."
        )

        news_items = await bot.uw_client.get_news_headlines(
            major_only=major_only,
            hours_back=hours
        )

        if not news_items:
            await interaction.edit_original_response(
                content=f"❌ No news items found in the last {hours} hours."
            )
            return

        # Filter by time window
        await interaction.edit_original_response(
            content=f"⏰ Filtering {len(news_items)} items by {hours}-hour time window..."
        )

        filtered_news = bot.openrouter_client.filter_news_by_time(news_items, hours)

        if not filtered_news:
            await interaction.edit_original_response(
                content=f"❌ No news items found within the last {hours} hours after filtering."
            )
            return

        # Generate AI summary
        await interaction.edit_original_response(
            content=f"🤖 Analyzing {len(filtered_news)} headlines with Claude Haiku 4.5..."
        )

        summary = await bot.openrouter_client.summarize_news(
            news_items=filtered_news,
            user_query=question,
            hours=hours
        )

        # Count major news items
        major_count = sum(1 for item in filtered_news if item.get("is_major", False))

        # Create Discord embed
        embed = discord.Embed(
            title="📰 Market News Summary",
            description=summary,
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )

        if question:
            embed.add_field(
                name="Your Question",
                value=question,
                inline=False
            )

        embed.add_field(
            name="Time Range",
            value=f"Last {hours} hour{'s' if hours != 1 else ''}",
            inline=True
        )

        embed.add_field(
            name="Headlines Analyzed",
            value=f"{len(filtered_news)} items",
            inline=True
        )

        if major_count > 0:
            embed.add_field(
                name="Major Events",
                value=f"{major_count} major",
                inline=True
            )

        embed.set_footer(text="Powered by Claude Haiku 4.5 via OpenRouter")

        await interaction.edit_original_response(
            content=None,
            embed=embed
        )

        logger.info(
            f"Successfully generated market news summary: {len(filtered_news)} headlines, "
            f"{major_count} major events"
        )

    except Exception as e:
        logger.error(f"Error in market_news command: {e}", exc_info=True)
        await interaction.edit_original_response(
            content=f"❌ Error generating news summary: {str(e)}"
        )


@bot.tree.command(
    name="8ball",
    description="🔮 Consult the mystical financial oracle for cryptic prophecies"
)
@app_commands.describe(
    question="Your question for the oracle (e.g., 'Should I buy TSLA?')"
)
@is_dm_whitelisted()
async def eight_ball(
    interaction: discord.Interaction,
    question: Optional[str] = None
):
    """Generate mystical financial prophecy based on recent major news."""
    await interaction.response.defer()

    try:
        # Check if OpenRouter client is available
        if not bot.openrouter_client:
            await interaction.edit_original_response(
                content="❌ The oracle is unavailable. OPENROUTER_API_KEY is not configured."
            )
            return

        logger.info(f"Processing 8ball prophecy request: question='{question or 'none'}'")

        # Fetch recent news headlines from last 8 hours
        await interaction.edit_original_response(
            content="🔮 The oracle peers into the market's soul..."
        )

        news_items = await bot.uw_client.get_news_headlines(
            major_only=False,  # All news - fresh news often isn't flagged as major
            hours_back=8       # Last 8 hours
        )

        if not news_items:
            # No news available - return fallback prophecy
            await interaction.edit_original_response(
                content="🔮 *The spirits are silent... the market's mysteries remain veiled for now.*"
            )
            return

        # Filter by time window
        filtered_news = bot.openrouter_client.filter_news_by_time(news_items, hours=8)

        if not filtered_news:
            # No news in time window - return fallback prophecy
            await interaction.edit_original_response(
                content="🔮 *The spirits are silent... the market's mysteries remain veiled for now.*"
            )
            return

        # Generate prophecy
        await interaction.edit_original_response(
            content="🔮 The oracle channels the market spirits..."
        )

        prophecy = await bot.openrouter_client.generate_prophecy(
            news_items=filtered_news,
            user_question=question
        )

        # Create mystical embed
        embed = discord.Embed(
            title="🔮 The Oracle Speaks",
            description=f"*{prophecy}*",
            color=discord.Color.purple(),
            timestamp=datetime.now()
        )

        if question:
            embed.add_field(
                name="Your Question",
                value=question,
                inline=False
            )

        embed.add_field(
            name="Divination Source",
            value=f"Last 8 hours of market events",
            inline=True
        )

        embed.add_field(
            name="Omens Consulted",
            value=f"{len(filtered_news)} headlines",
            inline=True
        )

        embed.set_footer(text="⚠️ For mystical entertainment purposes only • Not financial advice")

        await interaction.edit_original_response(
            content=None,
            embed=embed
        )

        logger.info(f"Successfully generated 8ball prophecy based on {len(filtered_news)} headlines")

    except Exception as e:
        logger.error(f"Error in 8ball command: {e}", exc_info=True)
        await interaction.edit_original_response(
            content=f"❌ The oracle's vision is clouded: {str(e)}"
        )


def main():
    """Main entry point for the bot."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment")

    bot.run(token)


if __name__ == "__main__":
    main()
