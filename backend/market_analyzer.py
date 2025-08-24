"""
Market analysis for identifying markets of interest.
"""

import requests, math
import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple, Any
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class MarketVolume:
    """Market volume information"""
    market_slug: str
    question: str
    condition_id: str
    volume_24h: float
    volume_1w: float
    liquidity: float
    spread: float
    best_bid: float
    best_ask: float
    token_ids: List[str]
    end_date: str
    active: bool


class MarketAnalyzer:
    """Analyzes markets to identify candidate markets"""
    
    def __init__(self, debug_mode: bool = False):
        self.markets_cache: List[MarketVolume] = []
        self.cache_timestamp: Optional[datetime] = None
        self.cache_duration = timedelta(minutes=2)  # Cache for 2 minutes (reduced for fresher data)
        self.debug_mode = debug_mode
        
        if self.debug_mode:
            logger.info("MarketAnalyzer initialized with debug mode enabled")
    
    async def get_market_analysis(self, market_slug: str) -> Optional[MarketVolume]:
        """Get detailed analysis for a specific market"""
        try:
            if self.debug_mode:
                logger.info(f"Getting market analysis for: {market_slug}")
            
            # Ensure we have fresh data
            if self._is_cache_stale():
                await self._fetch_markets()
            
            # Find the market in our cache
            for market in self.markets_cache:
                if market.market_slug == market_slug:
                    if self.debug_mode:
                        logger.info(f"Found market {market_slug} in cache")
                    return market
            
            if self.debug_mode:
                logger.warning(f"Market {market_slug} not found in cache, trying single fetch")
            
        except Exception as e:
            logger.error(f"Error analyzing market {market_slug}: {e}")
            return None
    
    async def get_markets_by_slugs(self, market_slugs: List[str]) -> List[MarketVolume]:
        """Get market data for specific market slugs"""
        try:
            if self.debug_mode:
                logger.info(f"Getting markets by slugs: {market_slugs}")
            
            # Ensure we have fresh data
            if self._is_cache_stale():
                await self._fetch_markets()
            
            found_markets = []
            missing_slugs = []
            
            # Find markets in cache
            for slug in market_slugs:
                found = False
                for market in self.markets_cache:
                    if market.market_slug == slug:
                        found_markets.append(market)
                        found = True
                        break
                
                if not found:
                    missing_slugs.append(slug)
            
            if missing_slugs:
                logger.warning(f"Markets not found in cache: {missing_slugs}")
                # Attempt to fetch each missing market directly from API
                for slug in missing_slugs:
                    try:
                        fresh = await self.refresh_market_data(slug)
                        if fresh:
                            found_markets.append(fresh)
                            # Ensure it's in cache for future lookups
                            if not any(m.market_slug == slug for m in self.markets_cache):
                                self.markets_cache.append(fresh)
                        else:
                            logger.warning(f"Unable to fetch market by slug: {slug}")
                    except Exception as e:
                        logger.error(f"Error fetching missing market {slug}: {e}")
            
            if self.debug_mode:
                logger.info(f"Found {len(found_markets)} markets out of {len(market_slugs)} requested")
            
            return found_markets
            
        except Exception as e:
            logger.error(f"Error getting markets by slugs: {e}")
            return []
    
    async def _fetch_markets(self) -> None:
        """Fetch market data from Polymarket API"""
        try:
            if self.debug_mode:
                logger.info("Fetching market data from Polymarket API")
            
            # Get markets from Gamma API
            markets_url = "https://gamma-api.polymarket.com/markets"
            params = {
                "archived": "false",
                "closed": "false",
                "liquidity_num_max": 10000, # limit to low-liquidity markets
                "order": "volume24hr", # sort by volume
                "ascending": "false", # high volume first (descending order)
                "limit": 500  # maximum 500
            }
            
            if self.debug_mode:
                logger.info(f"API params: {params}")
            
            response = requests.get(markets_url, params=params, timeout=15)
            response.raise_for_status()
            markets_data = response.json()
            
            if self.debug_mode:
                logger.info(f"API returned {len(markets_data)} markets")
            
            self.markets_cache.clear()
            parse_errors = 0
            
            for i, market_data in enumerate(markets_data):
                if self.debug_mode and i < 3:  # Log first 3 raw market data
                    logger.debug(f"Raw market data {i}: {market_data}")
                
                market_volume = await self._parse_market_data(market_data)
                if market_volume:
                    self.markets_cache.append(market_volume)
                else:
                    parse_errors += 1
                    if self.debug_mode and parse_errors <= 3:
                        logger.warning(f"Failed to parse market data {i}: {market_data.get('slug', 'unknown')}")
            
            self.cache_timestamp = datetime.now()
            
            if self.debug_mode:
                logger.info(f"Successfully parsed {len(self.markets_cache)} markets, {parse_errors} parse errors")
                if self.markets_cache:
                    sample_market = self.markets_cache[0]
                    logger.info(f"Sample parsed market: {sample_market.market_slug} - Volume: {sample_market.volume_24h}, Liquidity: {sample_market.liquidity}, Spread: {sample_market.spread:.4f}")
            
            logger.info(f"Cached {len(self.markets_cache)} markets")
            
        except Exception as e:
            logger.error(f"Error fetching markets: {e}")
            if self.debug_mode:
                logger.exception("Full traceback:")
            raise
    
    async def _parse_market_data(self, market_data: dict) -> Optional[MarketVolume]:
        """Parse market data from API response"""
        try:
            # Extract basic info
            market_slug = market_data.get('slug', '')
            question = market_data.get('question', '')
            condition_id = market_data.get('conditionId', '')
            
            # Extract volume and liquidity data
            volume_24h = float(market_data.get('volume24hr', 0))
            volume_1w = float(market_data.get('volume1wk', 0))
            liquidity = float(market_data.get('liquidity', 0))
            
            # Extract price data
            best_bid = float(market_data.get('bestBid', 0))
            best_ask = float(market_data.get('bestAsk', 1))
            spread = best_ask - best_bid if best_ask > best_bid else 0
            
            # Extract token IDs
            token_ids = []
            if 'clobTokenIds' in market_data:
                token_ids = eval(market_data['clobTokenIds'])  # List in string format
            
            market_volume = MarketVolume(
                market_slug=market_slug,
                question=question,
                condition_id=condition_id,
                volume_24h=volume_24h,
                volume_1w=volume_1w,
                liquidity=liquidity,
                spread=spread,
                best_bid=best_bid,
                best_ask=best_ask,
                token_ids=token_ids,
                end_date=market_data.get('endDate', ''),
                active=market_data.get('active', False),
            )
            
            if self.debug_mode and len(self.markets_cache) < 3:  # Log first 3 parsed markets
                logger.debug(f"Parsed market: {market_volume}")
            
            return market_volume
            
        except Exception as e:
            if self.debug_mode:
                logger.warning(f"Error parsing market data for {market_data.get('slug', 'unknown')}: {e}")
                logger.debug(f"Problem market data: {market_data}")
            return None
    
    def _is_cache_stale(self) -> bool:
        """Check if the market cache needs refreshing"""
        if not self.cache_timestamp:
            if self.debug_mode:
                logger.debug("Cache is stale: no timestamp")
            return True
        
        is_stale = datetime.now() - self.cache_timestamp > self.cache_duration
        if self.debug_mode and is_stale:
            logger.debug(f"Cache is stale: age {datetime.now() - self.cache_timestamp}")
        return is_stale
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """Get statistics about the cached market data"""
        if not self.markets_cache:
            return {"cached_markets": 0}
        
        volumes_24h = [m.volume_24h for m in self.markets_cache]
        spreads = [m.spread for m in self.markets_cache if m.spread > 0]
        
        stats = {
            "cached_markets": len(self.markets_cache),
            "cache_age_minutes": (datetime.now() - self.cache_timestamp).total_seconds() / 60 if self.cache_timestamp else None,
            "avg_volume_24h": sum(volumes_24h) / len(volumes_24h) if volumes_24h else 0,
            "median_volume_24h": sorted(volumes_24h)[len(volumes_24h)//2] if volumes_24h else 0,
            "avg_spread": sum(spreads) / len(spreads) if spreads else 0,
            "low_volume_count": len([m for m in self.markets_cache if m.volume_24h < 1000]),
            "active_markets": len([m for m in self.markets_cache if m.active])
        }
        
        if self.debug_mode:
            logger.info(f"Cache stats: {stats}")
        
        return stats
    
    async def refresh_market_data(self, market_slug: str) -> Optional[MarketVolume]:
        """Refresh market data for a specific market to get current bid/ask prices"""
        try:
            logger.info(f"Refreshing market data for {market_slug}")
            
            # Get fresh market data from API
            markets_url = "https://gamma-api.polymarket.com/markets"
            params = {
                "archived": "false",
                "closed": "false",
                "slug": market_slug,
                "limit": 1
            }
            
            response = requests.get(markets_url, params=params, timeout=10)
            response.raise_for_status()
            markets_data = response.json()
            
            if not markets_data:
                logger.warning(f"No data returned for market {market_slug}")
                return None
            
            # Parse the fresh market data
            fresh_market = await self._parse_market_data(markets_data[0])
            
            if fresh_market:
                # Update our cache with the fresh data
                for i, cached_market in enumerate(self.markets_cache):
                    if cached_market.market_slug == market_slug:
                        self.markets_cache[i] = fresh_market
                        logger.info(f"Updated cached market {market_slug}: bid={fresh_market.best_bid:.4f}, ask={fresh_market.best_ask:.4f}, spread={fresh_market.spread:.4f}")
                        break
                
                return fresh_market
            
            return None
            
        except Exception as e:
            logger.error(f"Error refreshing market data for {market_slug}: {e}")
            return None