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
            # Fetch fresh data and regenerate chart (same logic as main command)
            ohlc_data = await self.bot.uw_client.get_ohlc_data(
                ticker=self.ticker,
                candle_size="1m",
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


@bot.tree.command(
    name="iv_chart",
    description="Generate an intraday IV chart for an option"
)
@app_commands.describe(
    ticker="Stock ticker symbol (e.g., AAPL)",
    expiration="Option expiration date (YYYY-MM-DD)",
    option_type="Call or Put",
    days="Number of days to look back (default: 2)"
)
@app_commands.choices(option_type=[
    app_commands.Choice(name="Call", value="call"),
    app_commands.Choice(name="Put", value="put")
])
async def iv_chart(
    interaction: discord.Interaction,
    ticker: str,
    expiration: str,
    option_type: app_commands.Choice[str],
    days: Optional[int] = 2
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

        # Step 1: Fetch OHLC data
        await interaction.edit_original_response(
            content=f"Fetching {days} days of price data for {ticker}..."
        )
        ohlc_data = await bot.uw_client.get_ohlc_data(
            ticker=ticker,
            candle_size="1m",
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

        # Step 4: Identify required strikes per date (optimized)
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
            content=f"Fetching IV data ({total_calls} optimized API calls)..."
        )

        # Step 5: Fetch IV data for required strike/date combinations (optimized with concurrency)
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

        # Step 6: Align data
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


def main():
    """Main entry point for the bot."""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN not found in environment")

    bot.run(token)


if __name__ == "__main__":
    main()
