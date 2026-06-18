"""
News Engine - AI-powered market news aggregator | Bộ tổng hợp tin tức thị trường AI.
Fetches from top Vietnamese and international crypto sources.
Filters for 48H relevant news and provides AI-style bilingual summaries.
"""

import asyncio
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List

import aiohttp
from loguru import logger

from core.event_bus import Event, EventBus
from core.events.topics import EventTopic
from core.task_watcher import TaskWatcher


class NewsEngine:
    """
    Aggregates news from various RSS feeds and provides AI-powered bilingual summaries.
    Sources: 3 Vietnamese + 3 International | Nguồn: 3 tiếng Việt + 3 quốc tế
    """

    SOURCES = [
        # ── Tiếng Việt ────────────────────────────────────────────────
        {"name": "CoinVN", "url": "https://coin68.com/feed/", "lang": "vi", "flag": "🇻🇳"},
        {"name": "TienAo", "url": "https://blogtienao.com/feed/", "lang": "vi", "flag": "🇻🇳"},
        # ── International ─────────────────────────────────────────────
        {"name": "CoinTelegraph", "url": "https://cointelegraph.com/rss", "lang": "en", "flag": "🌐"},
        {"name": "CoinDesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "lang": "en", "flag": "🌐"},
        {"name": "Decrypt", "url": "https://decrypt.co/feed", "lang": "en", "flag": "🌐"},
        {"name": "TheBlock", "url": "https://www.theblock.co/rss.xml", "lang": "en", "flag": "🌐"},
    ]

    # Từ khóa nhận dạng cảm xúc thị trường | Sentiment keyword dictionary
    _BULLISH_KEYS = [
        "tăng", "bull", "up", "breakout", "vọt", "xanh", "etf", "hợp tác",
        "surge", "rally", "gain", "record", "all-time high", "adoption",
        "partnership", "approval", "launch", "pump", "moon", "buy",
        "buy", "accumulate", "inflow", "institutional", "bullish",
    ]
    _BEARISH_KEYS = [
        "giảm", "bear", "down", "crash", "bán tháo", "đỏ", "fud",
        "sell", "dump", "drop", "plunge", "fear", "regulation",
        "ban", "hack", "exploit", "scam", "liquidation", "outflow",
        "bearish", "recession", "inflation", "lawsuit", "penalty",
    ]

    # Coin phổ biến để detect đề cập | Popular coins for mention detection
    _COIN_KEYWORDS = {
        "BTC": ["bitcoin", "btc", "₿"],
        "ETH": ["ethereum", "eth", "ether"],
        "SOL": ["solana", "sol"],
        "BNB": ["bnb", "binance"],
        "XRP": ["xrp", "ripple"],
        "DOGE": ["doge", "dogecoin"],
        "ADA": ["cardano", "ada"],
        "AVAX": ["avalanche", "avax"],
    }

    def __init__(self, event_bus: EventBus):
        self.event_bus = event_bus
        self._news_cache: List[Dict[str, Any]] = []
        self._last_update = 0
        self._running = False
        self.watcher = TaskWatcher()
        logger.info("NewsEngine initialized | Khởi tạo xong")

    async def start(self) -> None:
        """Start the news engine | Khởi động bộ máy tin tức."""
        if self._running:
            return
        self._running = True

        self.event_bus.subscribe(
            self._handle_news_request,
            [EventTopic.TELEGRAM_REQUEST_NEWS_DATA],
            handler_id="news_engine_handler",
        )

        await self._fetch_all_news()
        self.watcher.watch(self._update_loop, "news_update_worker")
        logger.info("NewsEngine started | Bộ máy tin tức đã sẵn sàng")

    async def stop(self) -> None:
        """Stop the news engine | Tắt bộ máy tin tức."""
        self._running = False
        self.event_bus.unsubscribe(handler_id="news_engine_handler")
        self.watcher.stop_all()
        logger.info("NewsEngine stopped")

    async def _update_loop(self):
        """Periodically update news every 30 minutes | Cập nhật mỗi 30 phút."""
        try:
            while self._running:
                await asyncio.sleep(1800)
                await self._fetch_all_news()
        except asyncio.CancelledError:
            logger.info("[NewsEngine] Update loop cancelled safely.")
            raise

    async def _fetch_all_news(self):
        """Fetch news from all sources concurrently | Lấy tin từ tất cả nguồn song song."""
        logger.info("Fetching market news | Đang tải tin tức thị trường...")
        tasks = [self._fetch_source(source) for source in self.SOURCES]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_news = []
        source_counts = {}
        for i, res in enumerate(results):
            if isinstance(res, list):
                src_name = self.SOURCES[i]["name"]
                source_counts[src_name] = len(res)
                all_news.extend(res)

        # Sort newest first | Sắp xếp mới nhất trước
        all_news.sort(key=lambda x: x.get("pub_date_ts", 0), reverse=True)

        # Filter 48H | Lọc 48 giờ qua
        cutoff = time.time() - (48 * 3600)
        self._news_cache = [n for n in all_news if n.get("pub_date_ts", 0) > cutoff][:25]
        self._last_update = time.time()

        logger.info(
            f"News cache updated | Cập nhật xong: {len(self._news_cache)} items "
            f"from {sum(source_counts.values())} total | Sources: {source_counts}"
        )

    async def _fetch_source(self, source: Dict[str, str]) -> List[Dict[str, Any]]:
        """Fetch and parse RSS from a single source with 2-layer resilience.

        Layer 1: Strip invalid XML control characters.
        Layer 2: Regex fallback if ET.fromstring still fails.
        """
        import re
        import email.utils

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    source["url"], timeout=aiohttp.ClientTimeout(total=12)
                ) as response:
                    if response.status != 200:
                        return []
                    raw_text = await response.text(errors="replace")

            # Layer 1: Sanitize invalid XML control chars
            sanitized = re.sub(r'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', '', raw_text)

            try:
                root = ET.fromstring(sanitized)
            except ET.ParseError as xml_err:
                logger.warning(
                    f"XML parse failed for {source['name']} ({xml_err}), "
                    f"switching to regex fallback."
                )
                return self._regex_extract_items(sanitized, source)

            channel = root.find("channel")
            if channel is None:
                return []

            items = []
            for item in channel.findall("item"):
                title_el = item.find("title")
                title = title_el.text if title_el is not None and title_el.text else ""

                link_el = item.find("link")
                link = link_el.text if link_el is not None and link_el.text else ""

                pub_date_el = item.find("pubDate")
                pub_date_str = pub_date_el.text if pub_date_el is not None and pub_date_el.text else ""

                desc_el = item.find("description")
                desc = desc_el.text if desc_el is not None and desc_el.text else ""

                pub_date_ts = self._parse_date(pub_date_str)

                items.append({
                    "title": title,
                    "link": link,
                    "description": self._clean_html(desc),
                    "source": source["name"],
                    "lang": source["lang"],
                    "flag": source.get("flag", "🌐"),
                    "pub_date_ts": pub_date_ts,
                    "pub_date_str": pub_date_str,
                    "coins": self._detect_coins(title + " " + desc),
                    "sentiment": self._score_sentiment(title + " " + desc),
                })
            return items

        except Exception as e:
            logger.error(f"Error fetching news from {source['name']}: {e}")
            return []

    def _detect_coins(self, text: str) -> List[str]:
        """Detect which coins are mentioned in text | Phát hiện coin được đề cập."""
        text_lower = text.lower()
        mentioned = []
        for coin, keywords in self._COIN_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                mentioned.append(coin)
        return mentioned

    def _score_sentiment(self, text: str) -> str:
        """Score sentiment of text: bullish/bearish/neutral | Đánh giá cảm xúc."""
        text_lower = text.lower()
        bull = sum(1 for k in self._BULLISH_KEYS if k in text_lower)
        bear = sum(1 for k in self._BEARISH_KEYS if k in text_lower)
        if bull > bear:
            return "bullish"
        elif bear > bull:
            return "bearish"
        return "neutral"

    def _parse_date(self, pub_date_str: str) -> float:
        """Parse RFC 822 date string to timestamp | Chuyển ngày giờ sang timestamp."""
        import email.utils
        try:
            parsed = email.utils.parsedate_tz(pub_date_str)
            if parsed is not None:
                return float(email.utils.mktime_tz(parsed))
        except Exception as e:
            logger.debug(f"Failed to parse pubDate '{pub_date_str}': {e}")
        return time.time()

    def _regex_extract_items(self, text: str, source: Dict[str, str]) -> List[Dict[str, Any]]:
        """Fallback regex-based RSS item extractor for malformed XML feeds."""
        import re
        items = []
        item_blocks = re.findall(r'<item[^>]*>(.*?)</item>', text, re.DOTALL)
        for block in item_blocks[:20]:
            def extract_tag(tag: str) -> str:
                m = re.search(rf'<{tag}[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</{tag}>', block, re.DOTALL)
                return m.group(1).strip() if m else ""

            title = extract_tag("title")
            link = re.search(r'<link[^>]*/?>([^<]+)', block)
            link = link.group(1).strip() if link else extract_tag("link")
            pub_date_str = extract_tag("pubDate")
            desc = extract_tag("description")

            if not title:
                continue

            full_text = title + " " + desc
            items.append({
                "title": self._clean_html(title),
                "link": link,
                "description": self._clean_html(desc),
                "source": source["name"],
                "lang": source["lang"],
                "flag": source.get("flag", "🌐"),
                "pub_date_ts": self._parse_date(pub_date_str),
                "pub_date_str": pub_date_str,
                "coins": self._detect_coins(full_text),
                "sentiment": self._score_sentiment(full_text),
            })
        logger.info(f"Regex fallback extracted {len(items)} items from {source['name']}")
        return items

    def _clean_html(self, html_text: str) -> str:
        """Remove HTML tags from text | Xóa thẻ HTML."""
        if not html_text:
            return ""
        import re
        text = re.sub(r'<.*?>', '', html_text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:200] + "..." if len(text) > 200 else text

    async def _handle_news_request(self, event: Event) -> None:
        """Handle request for news data | Xử lý yêu cầu tin tức từ Telegram."""
        try:
            if not self._news_cache or (time.time() - self._last_update > 3600):
                await self._fetch_all_news()

            data = {
                "action": event.data.get("action"),
                "query_id": event.data.get("query_id"),
                "message_id": event.data.get("message_id"),
                "news": self._news_cache,
                "last_update": self._last_update,
                "ai_summary": self._generate_ai_summary(),
                "coin_mentions": self._get_coin_mentions(),
                "source_stats": self._get_source_stats(),
            }

            await self.event_bus.publish(
                Event(
                    event_type=EventTopic.TELEGRAM_RESPONSE_NEWS_DATA,
                    data=data,
                    source="news_engine",
                )
            )
        except Exception as e:
            logger.error("Error handling news request: {}", e, exc_info=True)
            try:
                await self.event_bus.publish(
                    Event(
                        event_type=EventTopic.TELEGRAM_RESPONSE_NEWS_DATA,
                        data={
                            "success": False,
                            "error": str(e),
                            "message_id": event.data.get("message_id") if isinstance(event.data, dict) else None,
                            "action": event.data.get("action") if isinstance(event.data, dict) else None,
                        },
                        source="news_engine",
                    )
                )
            except Exception:
                pass

    def _get_coin_mentions(self) -> Dict[str, Dict[str, int]]:
        """Count bullish/bearish mentions per coin | Đếm đề cập tích cực/tiêu cực mỗi coin."""
        result = {}
        for news in self._news_cache:
            sentiment = news.get("sentiment", "neutral")
            for coin in news.get("coins", []):
                if coin not in result:
                    result[coin] = {"bullish": 0, "bearish": 0, "neutral": 0}
                result[coin][sentiment] = result[coin].get(sentiment, 0) + 1
        # Sort by total mentions descending
        return dict(sorted(result.items(), key=lambda x: sum(x[1].values()), reverse=True))

    def _get_source_stats(self) -> Dict[str, int]:
        """Count items per source | Thống kê số bài mỗi nguồn."""
        stats: Dict[str, int] = {}
        for news in self._news_cache:
            src = news.get("source", "N/A")
            stats[src] = stats.get(src, 0) + 1
        return stats

    def _generate_ai_summary(self) -> Dict[str, str]:
        """
        Generate bilingual (VI/EN) professional market sentiment summary.
        Returns dict with 'vi' and 'en' keys.
        """
        if not self._news_cache:
            return {
                "vi": "📭 Chưa có tin tức nổi bật trong 48h qua.",
                "en": "📭 No notable news found in the last 48 hours.",
            }

        all_text = " ".join([n["title"] + " " + n.get("description", "") for n in self._news_cache]).lower()

        bull_score = sum(1 for k in self._BULLISH_KEYS if k in all_text)
        bear_score = sum(1 for k in self._BEARISH_KEYS if k in all_text)
        total = bull_score + bear_score or 1
        bull_pct = round(bull_score / total * 100)
        bear_pct = round(bear_score / total * 100)

        if bull_score > bear_score * 1.3:
            sentiment_vi = f"🐂 <b>Tích cực (Bullish)</b> — Tỷ lệ: 📈{bull_pct}% / 📉{bear_pct}%"
            sentiment_en = f"🐂 <b>Bullish Sentiment</b> — Score: 📈{bull_pct}% / 📉{bear_pct}%"
            body_vi = "Tin tức 48h nghiêng về chiều tích cực. Dòng tiền và tâm lý nhà đầu tư đang cải thiện. Xu hướng tăng được hỗ trợ bởi nhiều luồng tin thuận lợi."
            body_en = "48h news tilts positive. Money flow and investor sentiment improving. Uptrend supported by multiple favorable developments."
        elif bear_score > bull_score * 1.3:
            sentiment_vi = f"🐻 <b>Tiêu cực (Bearish)</b> — Tỷ lệ: 📉{bear_pct}% / 📈{bull_pct}%"
            sentiment_en = f"🐻 <b>Bearish Sentiment</b> — Score: 📉{bear_pct}% / 📈{bull_pct}%"
            body_vi = "Nhiều tin tức tiêu cực trong 48h qua. Áp lực bán gia tăng, nên thận trọng với các lệnh Long. Ưu tiên quản lý rủi ro chặt chẽ."
            body_en = "Heavy negative news in the last 48h. Selling pressure rising, caution advised for long positions. Prioritize tight risk management."
        else:
            sentiment_vi = f"⚖️ <b>Trung lập (Neutral)</b> — Tỷ lệ: 📈{bull_pct}% / 📉{bear_pct}%"
            sentiment_en = f"⚖️ <b>Neutral Sentiment</b> — Score: 📈{bull_pct}% / 📉{bear_pct}%"
            body_vi = "Thị trường đang nhận tín hiệu trái chiều từ các luồng tin tức. Chưa có xu hướng rõ ràng — nên chờ xác nhận trước khi vào lệnh."
            body_en = "Market receiving mixed signals. No clear directional bias — wait for confirmation before entering positions."

        return {
            "vi": f"<b>🧠 Phân tích AI — Tâm lý thị trường:</b>\n{sentiment_vi}\n\n<i>{body_vi}</i>",
            "en": f"<b>🧠 AI Analysis — Market Sentiment:</b>\n{sentiment_en}\n\n<i>{body_en}</i>",
        }
