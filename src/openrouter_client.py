"""OpenRouter API client for AI-powered news summarization."""

import httpx
import asyncio
import time
import logging
from typing import List, Dict, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class OpenRouterClient:
    """Client for interacting with OpenRouter API using Claude Haiku 4.5."""

    BASE_URL = "https://openrouter.ai/api/v1"
    MODEL = "anthropic/claude-haiku-4.5"
    REQUEST_DELAY = 0.1  # 100ms between requests

    def __init__(self, api_key: str):
        """Initialize the OpenRouter client.

        Args:
            api_key: OpenRouter API key
        """
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/DigiBugCat/GLDfish",
            "X-Title": "GLDfish Discord Bot"
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

    def filter_news_by_time(
        self,
        news_items: List[Dict[str, Any]],
        hours: int
    ) -> List[Dict[str, Any]]:
        """Filter news items by timestamp.

        Args:
            news_items: List of news items with created_at timestamps
            hours: Number of hours to look back

        Returns:
            Filtered list of news items within the time window
        """
        cutoff_time = datetime.now() - timedelta(hours=hours)
        filtered_items = []

        for item in news_items:
            created_at_str = item.get("created_at", "")
            if not created_at_str:
                continue

            try:
                # Parse ISO 8601 timestamp
                created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))
                created_at = created_at.replace(tzinfo=None)  # Remove timezone for comparison

                if created_at >= cutoff_time:
                    filtered_items.append(item)
            except Exception as e:
                logger.warning(f"Could not parse timestamp {created_at_str}: {e}")
                continue

        logger.info(f"Filtered {len(filtered_items)} news items from last {hours} hours (out of {len(news_items)} total)")
        return filtered_items

    async def summarize_news(
        self,
        news_items: List[Dict[str, Any]],
        user_query: str = None,
        hours: int = 4
    ) -> str:
        """Summarize market news using Claude Haiku 4.5.

        Args:
            news_items: List of news headline dictionaries from UW API
            user_query: Optional user question/query
            hours: Number of hours of news being analyzed

        Returns:
            AI-generated summary string
        """
        await self._rate_limit()

        # Format news items for the prompt
        formatted_news = self._format_news_for_prompt(news_items)

        # Build the prompt
        prompt = self._build_prompt(formatted_news, user_query, hours)

        # Call OpenRouter API
        url = f"{self.BASE_URL}/chat/completions"

        payload = {
            "model": self.MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 1000,
            "temperature": 0.7
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers=self.headers,
                    json=payload
                )
                response.raise_for_status()
                data = response.json()

                # Extract the summary from response
                summary = data["choices"][0]["message"]["content"]

                logger.info(f"Generated news summary using {self.MODEL}")
                return summary

        except Exception as e:
            logger.error(f"Error calling OpenRouter API: {e}", exc_info=True)
            raise

    def _format_news_for_prompt(self, news_items: List[Dict[str, Any]]) -> str:
        """Format news items into a readable string for the AI prompt.

        Args:
            news_items: List of news dictionaries

        Returns:
            Formatted string with all news items
        """
        formatted_lines = []

        for i, item in enumerate(news_items, 1):
            tickers = ", ".join(item.get("tickers", []))
            headline = item.get("headline", "")
            is_major = "⭐ MAJOR" if item.get("is_major", False) else ""
            source = item.get("source", "Unknown")
            created_at = item.get("created_at", "")

            # Format each news item
            line = f"{i}. "
            if is_major:
                line += f"{is_major} "
            if tickers:
                line += f"[{tickers}] "
            line += f"{headline}"
            line += f" ({source}, {created_at})"

            formatted_lines.append(line)

        return "\n".join(formatted_lines)

    def _build_prompt(
        self,
        formatted_news: str,
        user_query: str = None,
        hours: int = 4
    ) -> str:
        """Build the AI prompt for news summarization.

        Args:
            formatted_news: Formatted string of all news items
            user_query: Optional user question
            hours: Number of hours of news

        Returns:
            Complete prompt string
        """
        query_text = user_query if user_query else "What's happening in the market right now?"

        prompt = f"""You are a financial news analyst. Your job is to summarize what's actually in the headlines, not add speculation.

User Question: {query_text}

News Headlines (last {hours} hours):
{formatted_news}

Instructions:
1. Report what's ACTUALLY in the headlines - stick to the facts
2. Answer the user's question directly using only information from the news
3. If making connections between events, clearly state "This connection might suggest..." or "These events could be related because..."
4. Avoid speculative language like "typically", "usually", "may indicate" unless you're explicitly noting a potential connection
5. Focus on: What happened? What do the headlines say? What connections exist between the events?

Keep it factual, direct, and concise (2-3 paragraphs max). Report the news, don't interpret beyond what's explicitly stated."""

        return prompt

    async def generate_prophecy(
        self,
        news_items: List[Dict[str, Any]],
        user_question: str = None
    ) -> str:
        """Generate a mystical financial prophecy based on recent news.

        Args:
            news_items: List of news headline dictionaries from UW API
            user_question: Optional user question (e.g., "Should I buy TSLA?")

        Returns:
            A cryptic, vague prophecy string
        """
        import random

        await self._rate_limit()

        # Format news items for the prompt
        formatted_news = self._format_news_for_prompt(news_items)

        # Randomly select a prophecy style
        styles = [
            {
                "name": "classic_8ball",
                "description": "Classic magic 8-ball style - short, cryptic one-liners",
                "examples": [
                    "The charts whisper of turbulence ahead",
                    "Fortune favors the patient holder",
                    "Uncertainty clouds the crystal ball",
                    "The market spirits are restless tonight"
                ]
            },
            {
                "name": "fortune_cookie",
                "description": "Fortune cookie style - pithy financial wisdom",
                "examples": [
                    "When the Fed speaks softly, the market carries a big stick",
                    "He who chases every dip finds his portfolio depleted",
                    "The wise trader reads the tea leaves, the foolish one reads only the ticker"
                ]
            },
            {
                "name": "oracle",
                "description": "Oracle/tarot reader style - mystical and dramatic",
                "examples": [
                    "I see great volatility in your future. The spirits of earnings reports stir uneasily...",
                    "The cards reveal a path shrouded in uncertainty. Forces beyond mortal ken shape the market's destiny",
                    "Beware the full moon earnings call. The auguries speak of turbulent waters ahead"
                ]
            }
        ]

        selected_style = random.choice(styles)

        # Build the prophecy prompt
        question_text = user_question if user_question else "What does the market hold?"

        prompt = f"""You are a mystical financial oracle with the gift of vague prophecy. Based on recent market headlines, you will generate ONE cryptic prophecy.

User's Question: {question_text}

Recent Major Market Headlines (last 8 hours):
{formatted_news}

Style: {selected_style['name']} - {selected_style['description']}
Examples of this style:
{chr(10).join(f'- "{ex}"' for ex in selected_style['examples'])}

Instructions:
1. Generate ONE prophecy in the selected style based on the headlines
2. Be vague and cryptic - DO NOT give explicit buy/sell advice
3. Reference themes from the news without being specific (e.g., "tech giants" not "Apple")
4. Use mystical, fortune-teller language
5. DO NOT be explicitly bullish or bearish - be enigmatic and open to interpretation
6. Keep it short (1-3 sentences max)
7. Channel the energy of a cryptic fortune teller who has glimpsed market secrets

Generate your prophecy now (ONLY output the prophecy, no explanation):"""

        # Call OpenRouter API
        url = f"{self.BASE_URL}/chat/completions"

        payload = {
            "model": self.MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": 200,
            "temperature": 0.9  # Higher temperature for more creative/mystical responses
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    url,
                    headers=self.headers,
                    json=payload
                )
                response.raise_for_status()
                data = response.json()

                # Extract the prophecy from response
                prophecy = data["choices"][0]["message"]["content"].strip()
                # Remove any quotes if the AI wrapped the response
                prophecy = prophecy.strip('"\'')

                logger.info(f"Generated prophecy in {selected_style['name']} style")
                return prophecy

        except Exception as e:
            logger.error(f"Error calling OpenRouter API for prophecy: {e}", exc_info=True)
            # Return a fallback mystical message
            return "🔮 The spirits are silent... the market's mysteries remain veiled for now."
