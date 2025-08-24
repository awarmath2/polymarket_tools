"""
Token metadata management for Polymarket markets.
"""

import requests
import json
from typing import Dict, Optional, Tuple, List
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)

@dataclass
class TokenMetadata:
    """Token metadata information"""
    token_id: str
    market_name: str
    condition_id: str
    question_id: str  
    current_bid: Optional[float] = None
    current_ask: Optional[float] = None
    suggested_tick_size: float = 0.001
    last_updated: Optional[datetime] = None
    active: bool = True
    volume_24h: Optional[float] = None


class TokenManager:
    """Manages token metadata and market information"""
    
    def __init__(self):
        self.metadata_cache: Dict[str, TokenMetadata] = {}
        self.cache_duration = timedelta(minutes=5)  # Cache for 5 minutes
        
    async def get_token_metadata(self, token_id: str) -> Optional[TokenMetadata]:
        """Get metadata for a token ID, using cache when possible"""
        
        # Check cache first
        if token_id in self.metadata_cache:
            metadata = self.metadata_cache[token_id]
            if metadata.last_updated and datetime.now() - metadata.last_updated < self.cache_duration:
                return metadata
        
        # Fetch fresh data
        try:
            metadata = await self._fetch_token_metadata(token_id)
            if metadata:
                self.metadata_cache[token_id] = metadata
            return metadata
        except Exception as e:
            logger.error(f"Error fetching metadata for token {token_id}: {e}")
            # Return cached version if available, even if stale
            return self.metadata_cache.get(token_id)
    
    async def _fetch_token_metadata(self, token_id: str) -> Optional[TokenMetadata]:
        """Fetch token metadata from Polymarket API"""
        try:
            # Try multiple query strategies to find the market that contains this token
            target_market = await self._find_market_containing_token(token_id)
            if not target_market:
                logger.warning(f"Token {token_id} not found in any markets (including closed/archived)")
                return None
            
            # Get current price data
            current_bid, current_ask = await self._get_current_prices(token_id)
            
            # Determine suggested tick size based on price levels
            suggested_tick_size = self._suggest_tick_size(current_bid, current_ask)
            
            metadata = TokenMetadata(
                token_id=token_id,
                market_name=target_market.get('question', 'Unknown Market'),
                # Correct field names from Gamma API
                condition_id=target_market.get('conditionId', '') or target_market.get('condition_id', ''),
                question_id=target_market.get('questionId', '') or target_market.get('question_id', ''),
                current_bid=current_bid,
                current_ask=current_ask,
                suggested_tick_size=suggested_tick_size,
                last_updated=datetime.now(),
                active=target_market.get('active', True),
                volume_24h=target_market.get('volume24hr') or target_market.get('volume_24hr')
            )
            
            logger.info(f"Fetched metadata for token {token_id}: {metadata.market_name} (condition {metadata.condition_id})")
            return metadata
            
        except Exception as e:
            logger.error(f"Error fetching token metadata: {e}")
            return None
    
    async def _find_market_containing_token(self, token_id: str) -> Optional[dict]:
        """Locate a market that includes the given token_id. Tries active first, then no filters, then archived/closed."""
        markets_url = "https://gamma-api.polymarket.com/markets"
        def extract_token_ids(market: dict) -> List[str]:
            ids: List[str] = []
            try:
                if 'clobTokenIds' in market:
                    raw = market['clobTokenIds']
                    ids = eval(raw) if isinstance(raw, str) else list(raw)
                elif 'tokens' in market and isinstance(market['tokens'], list):
                    # Some responses may embed tokens
                    for t in market['tokens']:
                        tid = t.get('token_id') or t.get('tokenId')
                        if tid:
                            ids.append(tid)
            except Exception:
                pass
            return ids
        
        # 1) Active markets only
        try_sets = [
            {"archived": "false", "closed": "false", "limit": 500},
            {},  # no filters
            {"archived": "true", "closed": "true", "limit": 500},
        ]
        
        for params in try_sets:
            try:
                resp = requests.get(markets_url, params=params, timeout=10)
                resp.raise_for_status()
                markets_data = resp.json()
                for item in markets_data:
                    # Some endpoints may return events with nested 'markets'
                    if isinstance(item, dict) and 'markets' in item and isinstance(item['markets'], list):
                        for market in item['markets']:
                            token_ids = extract_token_ids(market)
                            if token_id in token_ids:
                                return market
                    else:
                        market = item
                        token_ids = extract_token_ids(market)
                        if token_id in token_ids:
                            return market
            except Exception as e:
                logger.warning(f"Market query failed for params {params}: {e}")
        
        return None
    
    async def _get_current_prices(self, token_id: str) -> Tuple[Optional[float], Optional[float]]:
        """Get current bid/ask prices for a token"""
        try:
            # Get orderbook
            orderbook_url = f"https://clob.polymarket.com/book"
            params = {"token_id": token_id}
            
            response = requests.get(orderbook_url, params=params, timeout=5)
            response.raise_for_status()
            book_data = response.json()
            
            bids = book_data.get('bids', [])
            asks = book_data.get('asks', [])
            
            current_bid = float(bids[0]['price']) if bids else None
            current_ask = float(asks[0]['price']) if asks else None
            
            return current_bid, current_ask
            
        except Exception as e:
            logger.warning(f"Could not fetch current prices for {token_id}: {e}")
            return None, None
    
    def _suggest_tick_size(self, bid: Optional[float], ask: Optional[float]) -> float:
        """Suggest appropriate tick size based on current prices"""
        if bid is None and ask is None:
            return 0.001  # Default to fine granularity
        
        # Use the higher of bid/ask for decision
        price = max(bid or 0, ask or 0)
        
        # For very low prices (< 0.05), use fine tick size
        if price < 0.05:
            return 0.001
        # For moderate prices, use standard tick size  
        else:
            return 0.01
    
    def get_cached_tokens(self) -> Dict[str, TokenMetadata]:
        """Get all cached token metadata"""
        return self.metadata_cache.copy()
    
    def clear_cache(self):
        """Clear the metadata cache"""
        self.metadata_cache.clear()
        logger.info("Token metadata cache cleared") 