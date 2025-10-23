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
    days: int = 2
) -> io.BytesIO:
    """Create a dual-axis chart with OHLC candles and IV line.

    Args:
        data: List of aligned data points with OHLC and IV
        ticker: Stock ticker symbol
        expiration: Option expiration date
        option_type: "call" or "put"
        days: Number of trading days to chart (default: 2)

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

    # Apply 15-minute rolling average to smooth IV
    df_with_iv = df_with_iv.set_index("timestamp")
    df_with_iv["iv_pct_smoothed"] = df_with_iv["iv_pct"].rolling(
        window="15min",
        min_periods=1
    ).mean()
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
        label="Implied Volatility (15-min avg)",
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

    # Add vertical lines to separate days
    df['date_pt'] = df['timestamp_pt'].dt.date
    unique_dates = df['date_pt'].unique()

    if len(unique_dates) > 1:
        for i in range(1, len(unique_dates)):
            # Find first index of new day
            day_change_idx = df[df['date_pt'] == unique_dates[i]].index[0]
            x_pos = df.loc[day_change_idx, 'x_index']
            ax1.axvline(x=x_pos - 0.5, color='gray', linestyle='--', linewidth=1.5, alpha=0.5, zorder=1)

    # Set X-axis limits
    ax1.set_xlim(-1, len(df))

    # Grid
    ax1.grid(True, alpha=0.3, linestyle="--", linewidth=0.5)

    # Title
    title = f"{ticker} - {option_type.capitalize()} Options IV (Exp: {expiration})"
    plt.title(title, fontsize=16, fontweight="bold", pad=20)

    # Legends
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax2.legend(lines2, labels2, loc="upper right", fontsize=11, framealpha=0.9)

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
