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
from .utils import (
    identify_required_strikes,
    identify_required_strikes_by_date,
    align_data_by_timestamp,
    align_historic_data,
    get_trading_dates
)
from .chart_generator import create_iv_chart, create_error_chart
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

            # Generate new chart
            chart_buffer = create_iv_chart(
                data=aligned_data,
                ticker=self.ticker,
                expiration=self.expiration,
                option_type=self.option_type,
                days=self.days
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

        # Step 7: Generate chart
        await interaction.edit_original_response(
            content="Generating chart..."
        )

        chart_buffer = create_iv_chart(
            data=aligned_data,
            ticker=ticker,
            expiration=expiration_formatted,
            option_type=option_type_str,
            days=days
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
            content=f"**{ticker} {option_type.name} IV Chart** (Exp: {expiration_formatted})",
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


def main():
    """Main entry point for the bot."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment")

    bot.run(token)


if __name__ == "__main__":
    main()
