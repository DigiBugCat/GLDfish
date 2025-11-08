"""Chart generation for dual-axis IV and price visualization."""

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import pandas as pd
from datetime import datetime
from typing import List, Dict, Any
import logging
import io
import pytz

logger = logging.getLogger(__name__)


def create_iv_chart(
    data: List[Dict[str, Any]],
    ticker: str,
    expiration: str,
    option_type: str,
    days: int = 2,
    earnings_dates: List[str] = None
) -> io.BytesIO:
    """Create a dual-axis chart with OHLC candles and IV line.

    Args:
        data: List of aligned data points with OHLC and IV
        ticker: Stock ticker symbol
        expiration: Option expiration date
        option_type: "call" or "put"
        days: Number of trading days to chart (default: 2)
        earnings_dates: Optional list of earnings report dates (YYYY-MM-DD format)

    Returns:
        BytesIO object containing the PNG chart
    """
    if not data:
        raise ValueError("No data to plot")

    # Convert to DataFrame
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp")

    # Filter to only regular market hours using market_time field
    if 'market_time' in df.columns:
        df = df[df['market_time'] == 'r'].copy()
        logger.info(f"Filtered to {len(df)} regular market hour candles")

    if df.empty:
        raise ValueError("No data after filtering to regular market hours")

    # Convert to PT for date filtering
    if df['timestamp'].dt.tz is None:
        df_pt = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert('US/Pacific')
    else:
        df_pt = df['timestamp'].dt.tz_convert('US/Pacific')

    # Get only the last N trading days
    df['date'] = df_pt.dt.date
    unique_dates = sorted(df['date'].unique(), reverse=True)

    if len(unique_dates) > days:
        last_n_dates = unique_dates[:days]
        df = df[df['date'].isin(last_n_dates)].copy()
        logger.info(f"Filtered to last {days} trading days: {last_n_dates}")
    else:
        logger.info(f"Showing all {len(unique_dates)} trading days available")

    # Filter out rows with missing IV
    df_with_iv = df[df["iv"].notna()].copy()

    if df_with_iv.empty:
        raise ValueError("No IV data available to plot")

    # Convert IV to percentage (multiply by 100)
    df_with_iv["iv_pct"] = df_with_iv["iv"] * 100

    # Apply smoothing to IV - adaptive based on data frequency
    # For intraday (1m candles): use 15-min rolling average
    # For historic (4h candles): use 3-point rolling average (lighter smoothing)
    df_with_iv = df_with_iv.set_index("timestamp")

    # Detect if we're using large candles (4h mode) by checking candle count
    # If < 100 candles for the period, likely using 4h candles
    if len(df_with_iv) < 100:
        # Historic mode: use simple 3-point rolling average
        df_with_iv["iv_pct_smoothed"] = df_with_iv["iv_pct"].rolling(
            window=3,
            min_periods=1,
            center=True
        ).mean()
        smoothing_label = "IV (smoothed)"
    else:
        # Intraday mode: use 15-min rolling average
        df_with_iv["iv_pct_smoothed"] = df_with_iv["iv_pct"].rolling(
            window="15min",
            min_periods=1
        ).mean()
        smoothing_label = "Implied Volatility (15-min avg)"

    df_with_iv = df_with_iv.reset_index()

    # Create figure with dual axes
    fig, ax1 = plt.subplots(figsize=(16, 9))
    ax2 = ax1.twinx()

    # Create a numerical index for X-axis to eliminate gaps
    df['x_index'] = range(len(df))
    df_with_iv_merged = df_with_iv.merge(df[['timestamp', 'x_index']], on='timestamp', how='left')

    # Plot OHLC candles on left axis using index
    plot_candlesticks_indexed(ax1, df)

    # Plot IV line on right axis (using smoothed values)
    ax2.plot(
        df_with_iv_merged["x_index"],
        df_with_iv_merged["iv_pct_smoothed"],
        color='#ff9800',
        linewidth=2.5,
        label=smoothing_label,
        alpha=0.9
    )
    ax2.fill_between(
        df_with_iv_merged["x_index"],
        df_with_iv_merged["iv_pct_smoothed"],
        alpha=0.15,
        color='#ff9800'
    )

    # Auto-scale IV axis with small padding
    iv_min = df_with_iv["iv_pct_smoothed"].min()
    iv_max = df_with_iv["iv_pct_smoothed"].max()
    iv_range = iv_max - iv_min
    iv_padding = iv_range * 0.1  # 10% padding
    ax2.set_ylim(iv_min - iv_padding, iv_max + iv_padding)

    # Formatting
    ax1.set_xlabel("Time (PT)", fontsize=13, fontweight='bold')
    ax1.set_ylabel("Price ($)", fontsize=13, color='black', fontweight='bold')
    ax2.set_ylabel("Implied Volatility (%)", fontsize=13, color='#ff9800', fontweight='bold')

    ax1.tick_params(axis="y", labelcolor="black", labelsize=11)
    ax2.tick_params(axis="y", labelcolor='#ff9800', labelsize=11)

    # Format x-axis using custom labels at regular intervals
    # Show labels every hour in PT
    pt_tz = pytz.timezone('US/Pacific')
    df['timestamp_pt'] = pd.to_datetime(df['timestamp']).dt.tz_convert(pt_tz)

    # Select tick positions every ~60 candles (roughly hourly)
    tick_interval = 60
    tick_positions = list(range(0, len(df), tick_interval))
    if tick_positions[-1] != len(df) - 1:
        tick_positions.append(len(df) - 1)

    tick_labels = []
    prev_date = None
    for pos in tick_positions:
        ts = df.iloc[pos]['timestamp_pt']
        curr_date = ts.date()

        # Show date on first tick or when date changes
        if prev_date is None or curr_date != prev_date:
            tick_labels.append(f"{ts.strftime('%m/%d')}\n{ts.strftime('%H:%M')}")
        else:
            tick_labels.append(ts.strftime('%H:%M'))

        prev_date = curr_date

    ax1.set_xticks(tick_positions)
    ax1.set_xticklabels(tick_labels, fontsize=10, ha="center")

    # Add vertical lines to separate days (only for short timeframes to avoid clutter)
    df['date_pt'] = df['timestamp_pt'].dt.date
    unique_dates = df['date_pt'].unique()

    # Only show day separators for ≤10 days (intraday mode), skip for historic mode
    if len(unique_dates) > 1 and len(unique_dates) <= 10:
        for i in range(1, len(unique_dates)):
            # Find first index of new day
            day_change_idx = df[df['date_pt'] == unique_dates[i]].index[0]
            x_pos = df.loc[day_change_idx, 'x_index']
            ax1.axvline(x=x_pos - 0.5, color='gray', linestyle='--', linewidth=1.5, alpha=0.5, zorder=1)

    # Draw earnings date markers
    if earnings_dates:
        from datetime import datetime as dt

        # Convert earnings dates to datetime for comparison
        earnings_dt_list = []
        for date_str in earnings_dates:
            try:
                earnings_dt_list.append(dt.strptime(date_str, "%Y-%m-%d").date())
            except:
                logger.warning(f"Could not parse earnings date: {date_str}")

        # Find x_index positions for earnings dates that fall within chart range
        for earnings_date in earnings_dt_list:
            # Find rows matching this date
            matching_rows = df[df['date_pt'] == earnings_date]

            if not matching_rows.empty:
                # Use the first candle of the earnings day (or middle if multiple)
                if len(matching_rows) > 1:
                    # Use middle candle for better visual placement
                    x_pos = matching_rows.iloc[len(matching_rows) // 2]['x_index']
                else:
                    x_pos = matching_rows.iloc[0]['x_index']

                # Draw vertical line for earnings
                ax1.axvline(
                    x=x_pos,
                    color='#9c27b0',  # Purple/magenta color
                    linestyle='--',
                    linewidth=2.5,
                    alpha=0.8,
                    zorder=5,
                    label='Earnings' if earnings_date == earnings_dt_list[0] else ""  # Only label first one
                )

                # Add small text annotation above the line
                y_pos = ax1.get_ylim()[1] * 0.98  # Near top of chart
                ax1.text(
                    x_pos,
                    y_pos,
                    'E',
                    ha='center',
                    va='top',
                    fontsize=10,
                    fontweight='bold',
                    color='#9c27b0',
                    bbox=dict(boxstyle='circle,pad=0.3', facecolor='white', edgecolor='#9c27b0', linewidth=1.5),
                    zorder=6
                )

                logger.info(f"Added earnings marker for {earnings_date} at x_index {x_pos}")

    # Set X-axis limits
    ax1.set_xlim(-1, len(df))

    # Grid
    ax1.grid(True, alpha=0.3, linestyle="--", linewidth=0.5)

    # Title
    title = f"{ticker} - {option_type.capitalize()} Options IV (Exp: {expiration})"
    plt.title(title, fontsize=16, fontweight="bold", pad=20)

    # Legends - combine legends from both axes
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()

    # Combine legends from both axes
    all_lines = lines1 + lines2
    all_labels = labels1 + labels2

    if all_lines:
        ax2.legend(all_lines, all_labels, loc="upper right", fontsize=11, framealpha=0.9)

    # Tight layout
    plt.tight_layout()

    # Save to BytesIO
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=200, bbox_inches="tight", facecolor='white')
    buf.seek(0)
    plt.close(fig)

    logger.info(f"Generated chart for {ticker} {option_type} {expiration}")
    return buf


def plot_candlesticks_indexed(ax, df: pd.DataFrame):
    """Plot OHLC candlesticks using index positions (eliminates gaps).

    Args:
        ax: Matplotlib axis
        df: DataFrame with OHLC data and x_index column
    """
    width = 0.8  # Candle width in index units

    for idx, row in df.iterrows():
        x_pos = row["x_index"]
        open_price = row["open"]
        high_price = row["high"]
        low_price = row["low"]
        close_price = row["close"]

        # Determine color (green for up, red for down)
        color = '#26a69a' if close_price >= open_price else '#ef5350'

        # Draw high-low line
        ax.plot(
            [x_pos, x_pos],
            [low_price, high_price],
            color=color,
            linewidth=1,
            solid_capstyle="round"
        )

        # Draw open-close rectangle
        body_height = abs(close_price - open_price)
        body_bottom = min(open_price, close_price)

        rect = Rectangle(
            (x_pos - width / 2, body_bottom),
            width,
            body_height,
            facecolor=color,
            edgecolor=color,
            alpha=0.8
        )
        ax.add_patch(rect)

    # Set y-axis limits with some padding
    price_min = df["low"].min()
    price_max = df["high"].max()
    price_range = price_max - price_min
    ax.set_ylim(price_min - price_range * 0.05, price_max + price_range * 0.05)


def create_error_chart(error_message: str) -> io.BytesIO:
    """Create a simple error chart with a message.

    Args:
        error_message: Error message to display

    Returns:
        BytesIO object containing the PNG chart
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    ax.text(
        0.5,
        0.5,
        f"Error generating chart:\n\n{error_message}",
        ha="center",
        va="center",
        fontsize=14,
        color="red",
        wrap=True
    )
    ax.axis("off")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight")
    buf.seek(0)
    plt.close(fig)

    return buf


def plot_earnings_candlesticks(ax, ohlc_data: List[Dict[str, Any]], ticker: str):
    """Plot candlesticks for earnings analysis chart.

    Args:
        ax: Matplotlib axis (secondary axis for price)
        ohlc_data: List of dicts with 'day', 'open', 'high', 'low', 'close'
        ticker: Stock ticker symbol for labeling
    """
    width = 0.6  # Candlestick width

    for candle in ohlc_data:
        x_pos = candle['day']
        open_price = candle['open']
        high_price = candle['high']
        low_price = candle['low']
        close_price = candle['close']

        # Determine color
        color = '#26a69a' if close_price >= open_price else '#ef5350'

        # Draw high-low line
        ax.plot(
            [x_pos, x_pos],
            [low_price, high_price],
            color=color,
            linewidth=1.5,
            solid_capstyle='round',
            zorder=1,
            alpha=0.8
        )

        # Draw open-close rectangle
        body_height = abs(close_price - open_price)
        body_bottom = min(open_price, close_price)

        rect = Rectangle(
            (x_pos - width / 2, body_bottom),
            width,
            body_height,
            facecolor=color,
            edgecolor=color,
            alpha=0.7,
            zorder=1
        )
        ax.add_patch(rect)


def create_earnings_iv_chart(
    earnings_iv_data: Dict[str, Any],
    ticker: str
) -> io.BytesIO:
    """Create chart showing ATM IV behavior around earnings for different DTEs.

    Args:
        earnings_iv_data: Data structure from collect_earnings_iv_data()
        ticker: Stock ticker symbol

    Returns:
        BytesIO object containing the PNG chart
    """
    earnings_dates = earnings_iv_data['earnings_dates']
    data_by_earnings = earnings_iv_data['data']

    # DTE colors
    dte_colors = {
        14: '#e74c3c',    # Red
        30: '#f39c12',    # Orange
        60: '#3498db',    # Blue
        90: '#2ecc71',    # Green
        180: '#9b59b6'    # Purple
    }

    dte_labels = {
        14: '14 DTE',
        30: '30 DTE',
        60: '60 DTE',
        90: '90 DTE',
        180: '180 DTE'
    }

    # Create subplots for each earnings event
    num_earnings = len(earnings_dates)
    fig, axes = plt.subplots(num_earnings, 1, figsize=(14, 5 * num_earnings), sharex=True)

    # Handle single subplot case
    if num_earnings == 1:
        axes = [axes]

    for idx, (earnings_date_str, ax) in enumerate(zip(earnings_dates, axes)):
        earnings_data_points = data_by_earnings[earnings_date_str]

        if not earnings_data_points:
            ax.text(0.5, 0.5, f"No data available for {earnings_date_str}",
                    ha='center', va='center', transform=ax.transAxes)
            ax.set_title(f"Earnings: {earnings_date_str}")
            continue

        # Create secondary axis for price
        ax2 = ax.twinx()

        # Organize data by DTE and collect OHLC data
        dte_series = {dte: {'days': [], 'ivs': []} for dte in [14, 30, 60, 90, 180]}
        ohlc_data = []

        for day_offset, day_data in sorted(earnings_data_points.items()):
            # Extract IV data
            dte_ivs = day_data.get('ivs', {})
            for dte, iv_value in dte_ivs.items():
                dte_series[dte]['days'].append(day_offset)
                dte_series[dte]['ivs'].append(iv_value)

            # Extract OHLC data for candlesticks
            ohlc = day_data.get('ohlc')
            if ohlc:
                ohlc_data.append({
                    'day': day_offset,
                    'open': ohlc['open'],
                    'high': ohlc['high'],
                    'low': ohlc['low'],
                    'close': ohlc['close']
                })

        # Plot candlesticks on secondary axis (first, so they appear behind IV lines)
        if ohlc_data:
            plot_earnings_candlesticks(ax2, ohlc_data, ticker)


        # Plot each DTE series on primary axis
        for dte in [14, 30, 60, 90, 180]:
            if dte_series[dte]['days']:
                ax.plot(
                    dte_series[dte]['days'],
                    dte_series[dte]['ivs'],
                    marker='o',
                    markersize=6,
                    linewidth=2.5,
                    color=dte_colors[dte],
                    label=dte_labels[dte],
                    alpha=0.9,
                    zorder=2
                )

        # Add vertical line at earnings date (day 0)
        ax.axvline(x=0, color='black', linestyle='--', linewidth=2, alpha=0.7, label='Earnings Date', zorder=3)

        # Formatting for primary axis (IV)
        ax.set_ylabel("ATM IV (%)", fontsize=12, fontweight='bold', color='black')
        ax.tick_params(axis='y', labelcolor='black')

        # Formatting for secondary axis (Price)
        ax2.set_ylabel(f"{ticker} Price ($)", fontsize=12, fontweight='bold', color='#95a5a6')
        ax2.tick_params(axis='y', labelcolor='#95a5a6')

        ax.set_title(f"Earnings: {earnings_date_str}", fontsize=13, fontweight='bold')
        ax.grid(True, alpha=0.3, linestyle='--', zorder=0)

        # Combine legends from both axes
        lines1, labels1 = ax.get_legend_handles_labels()
        lines2, labels2 = ax2.get_legend_handles_labels()
        ax.legend(lines1 + lines2, labels1 + labels2, loc='best', fontsize=10, framealpha=0.9)

        # Set x-axis to show all days
        if earnings_data_points:
            all_days = sorted(set(day for day in earnings_data_points.keys()))
            ax.set_xticks(all_days)
            ax.set_xlabel("Days from Earnings", fontsize=12, fontweight='bold')

    # Overall title
    fig.suptitle(f"{ticker} - ATM IV Around Earnings", fontsize=16, fontweight='bold', y=0.995)

    plt.tight_layout()

    # Save to BytesIO
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=200, bbox_inches="tight", facecolor='white')
    buf.seek(0)
    plt.close(fig)

    logger.info(f"Generated earnings IV chart for {ticker}")
    return buf


def create_atm_premium_chart(
    data: List[Dict[str, Any]],
    ticker: str,
    dte: int,
    option_type: str,
    days: int
) -> io.BytesIO:
    """Create a dual-axis chart with stock price and ATM option premium at constant DTE.

    Args:
        data: List of aligned data points from align_constant_dte_premium_data()
        ticker: Stock ticker symbol
        dte: Days to expiration being tracked
        option_type: "call" or "put"
        days: Number of trading days to chart

    Returns:
        BytesIO object containing the PNG chart
    """
    if not data:
        raise ValueError("No data to plot")

    # Convert to DataFrame
    df = pd.DataFrame(data)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp")

    # Filter to only regular market hours if market_time field exists
    if 'market_time' in df.columns:
        df = df[df['market_time'] == 'r'].copy()
        logger.info(f"Filtered to {len(df)} regular market hour candles")

    if df.empty:
        raise ValueError("No data after filtering")

    # Convert to PT for date filtering
    if df['timestamp'].dt.tz is None:
        df_pt = df['timestamp'].dt.tz_localize('UTC').dt.tz_convert('US/Pacific')
    else:
        df_pt = df['timestamp'].dt.tz_convert('US/Pacific')

    # Get only the last N trading days
    df['date'] = df_pt.dt.date
    unique_dates = sorted(df['date'].unique(), reverse=True)

    if len(unique_dates) > days:
        last_n_dates = unique_dates[:days]
        df = df[df['date'].isin(last_n_dates)].copy()
        logger.info(f"Filtered to last {days} trading days: {last_n_dates}")
    else:
        logger.info(f"Showing all {len(unique_dates)} trading days available")

    # Filter to rows with premium data
    df_with_premium = df[df["option_premium"].notna()].copy()

    if df_with_premium.empty:
        raise ValueError("No option premium data available to plot")

    # Create figure with dual axes
    fig, ax1 = plt.subplots(figsize=(16, 9))
    ax2 = ax1.twinx()

    # Create a numerical index for X-axis to eliminate gaps
    df['x_index'] = range(len(df))
    df_with_premium = df_with_premium.merge(df[['timestamp', 'x_index']], on='timestamp', how='left')

    # Plot stock price line on left axis
    ax1.plot(
        df["x_index"],
        df["stock_price"],
        color='#2196f3',  # Blue
        linewidth=2.5,
        label=f'{ticker} Stock Price',
        alpha=0.9,
        zorder=2
    )
    ax1.fill_between(
        df["x_index"],
        df["stock_price"],
        alpha=0.1,
        color='#2196f3'
    )

    # Plot option premium line on right axis
    ax2.plot(
        df_with_premium["x_index"],
        df_with_premium["option_premium"],
        color='#ff5722',  # Deep Orange
        linewidth=2.5,
        label=f'{dte} DTE ATM {option_type.capitalize()} Premium',
        alpha=0.9,
        zorder=2
    )
    ax2.fill_between(
        df_with_premium["x_index"],
        df_with_premium["option_premium"],
        alpha=0.15,
        color='#ff5722'
    )

    # Auto-scale axes with padding
    stock_min = df["stock_price"].min()
    stock_max = df["stock_price"].max()
    stock_range = stock_max - stock_min
    stock_padding = stock_range * 0.05
    ax1.set_ylim(stock_min - stock_padding, stock_max + stock_padding)

    premium_min = df_with_premium["option_premium"].min()
    premium_max = df_with_premium["option_premium"].max()
    premium_range = premium_max - premium_min
    premium_padding = premium_range * 0.05
    ax2.set_ylim(premium_min - premium_padding, premium_max + premium_padding)

    # Formatting
    ax1.set_xlabel("Time (PT)", fontsize=13, fontweight='bold')
    ax1.set_ylabel("Stock Price ($)", fontsize=13, color='#2196f3', fontweight='bold')
    ax2.set_ylabel(f"Option Premium ($)", fontsize=13, color='#ff5722', fontweight='bold')

    ax1.tick_params(axis="y", labelcolor='#2196f3', labelsize=11)
    ax2.tick_params(axis="y", labelcolor='#ff5722', labelsize=11)

    # Format x-axis using custom labels at regular intervals
    pt_tz = pytz.timezone('US/Pacific')
    df['timestamp_pt'] = pd.to_datetime(df['timestamp']).dt.tz_convert(pt_tz)

    # Detect if we're in intraday or historic mode
    if len(df) < 100:
        # Historic mode: show labels every 5 data points
        tick_interval = max(5, len(df) // 20)
    else:
        # Intraday mode: show labels every ~60 candles (roughly hourly)
        tick_interval = 60

    tick_positions = list(range(0, len(df), tick_interval))
    if tick_positions and tick_positions[-1] != len(df) - 1:
        tick_positions.append(len(df) - 1)

    tick_labels = []
    prev_date = None
    for pos in tick_positions:
        ts = df.iloc[pos]['timestamp_pt']
        curr_date = ts.date()

        # Show date on first tick or when date changes
        if prev_date is None or curr_date != prev_date:
            if len(df) < 100:
                # Historic mode: show date only
                tick_labels.append(ts.strftime('%m/%d'))
            else:
                # Intraday mode: show date and time
                tick_labels.append(f"{ts.strftime('%m/%d')}\n{ts.strftime('%H:%M')}")
        else:
            if len(df) < 100:
                tick_labels.append('')
            else:
                tick_labels.append(ts.strftime('%H:%M'))

        prev_date = curr_date

    ax1.set_xticks(tick_positions)
    ax1.set_xticklabels(tick_labels, fontsize=10, ha="center")

    # Add vertical lines to separate days (only for intraday mode)
    df['date_pt'] = df['timestamp_pt'].dt.date
    unique_dates_chart = df['date_pt'].unique()

    if len(unique_dates_chart) > 1 and len(df) >= 100:
        for i in range(1, len(unique_dates_chart)):
            day_change_idx = df[df['date_pt'] == unique_dates_chart[i]].index[0]
            x_pos = df.loc[day_change_idx, 'x_index']
            ax1.axvline(x=x_pos - 0.5, color='gray', linestyle='--', linewidth=1.5, alpha=0.5, zorder=1)

    # Set X-axis limits
    ax1.set_xlim(-1, len(df))

    # Grid
    ax1.grid(True, alpha=0.3, linestyle="--", linewidth=0.5)

    # Title
    title = f"{ticker} Stock Price vs {dte} DTE ATM {option_type.capitalize()} Premium"
    plt.title(title, fontsize=16, fontweight="bold", pad=20)

    # Legends - combine legends from both axes
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()

    all_lines = lines1 + lines2
    all_labels = labels1 + labels2

    if all_lines:
        ax1.legend(all_lines, all_labels, loc="upper left", fontsize=11, framealpha=0.9)

    # Add data coverage info
    valid_count = len(df_with_premium)
    total_count = len(df)
    coverage_pct = (valid_count / total_count * 100) if total_count > 0 else 0

    # Add footnote with coverage stats
    footnote = f"Data coverage: {valid_count}/{total_count} points ({coverage_pct:.1f}%)"
    if df_with_premium['actual_dte'].notna().any():
        avg_dte = df_with_premium['actual_dte'].mean()
        footnote += f" | Avg DTE: {avg_dte:.1f} days"

    fig.text(0.99, 0.01, footnote, ha='right', va='bottom', fontsize=9, color='gray', style='italic')

    # Tight layout
    plt.tight_layout()

    # Save to BytesIO
    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=200, bbox_inches="tight", facecolor='white')
    buf.seek(0)
    plt.close(fig)

    logger.info(f"Generated ATM premium chart for {ticker} {dte}DTE {option_type}")
    return buf
