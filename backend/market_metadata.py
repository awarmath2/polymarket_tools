"""
Market metadata fetcher for Polymarket tokens.
Fetches market information by token ID using the Gamma API.
"""

import requests
import json
import asyncio
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime
import logging

logger = logging.getLogger(__name__)
# logger.setLevel(logging.DEBUG)  # Enable debug logging temporarily

# Add console handler for debug output
if not logger.handlers:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

@dataclass
class MarketMetadata:
    """Market metadata for a specific token"""
    token_id: str
    market_slug: str
    market_title: str
    market_question: str
    outcome: str
    outcome_index: int
    current_price: Optional[float] = None
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    order_price_min_tick_size: float = 0.001
    outcomes: List[str] = None # type: ignore
    outcome_prices: List[float] = None # type: ignore
    active: bool = True
    volume_24hr: Optional[float] = None
    liquidity: Optional[float] = None
    last_updated: Optional[datetime] = None

    def __post_init__(self):
        if self.outcomes is None:
            self.outcomes = []
        if self.outcome_prices is None:
            self.outcome_prices = []


class MarketMetadataFetcher:
    """Fetches market metadata using the Polymarket Gamma API"""
    
    def __init__(self, timeout: int = 10):
        self.gamma_api_url = "https://gamma-api.polymarket.com/markets"
        self.timeout = timeout
        
    async def fetch_metadata_by_token_id(self, token_id: str) -> Optional[MarketMetadata]:
        """
        Fetch market metadata for a given token ID.
        
        Args:
            token_id: The CLOB token ID to look up
            
        Returns:
            MarketMetadata object if found, None otherwise
        """
        try:
            logger.info(f"Fetching market metadata for token ID: {token_id}")
            
            # Use asyncio to run the synchronous request in a thread pool
            loop = asyncio.get_event_loop()
            metadata = await loop.run_in_executor(None, self._fetch_metadata_sync, token_id)
            
            if metadata:
                logger.info(f"Successfully fetched metadata for token {token_id}: {metadata.market_title}")
            else:
                logger.warning(f"No metadata found for token {token_id}")
                
            return metadata
            
        except Exception as e:
            logger.error(f"Error fetching metadata for token {token_id}: {e}")
            return None
    
    def _fetch_metadata_sync(self, token_id: str) -> Optional[MarketMetadata]:
        """Synchronous version of metadata fetching"""
        try:
            # Query gamma API by token ID
            params = {"clob_token_ids": token_id}
            response = requests.get(self.gamma_api_url, params=params, timeout=self.timeout)
            response.raise_for_status()
            
            data = response.json()
            if not data or len(data) == 0:
                return None
            
            # Parse the response to extract metadata
            return self._parse_gamma_response(token_id, data[0])
            
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP error fetching token metadata: {e}")
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSON decode error: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return None
    
    def _parse_gamma_response(self, token_id: str, market_data: dict) -> Optional[MarketMetadata]:
        """Parse gamma API response to extract relevant metadata"""
        try:
            logger.info(f"Starting to parse gamma response for token {token_id}")
            logger.debug(f"Market data keys: {list(market_data.keys())}")
            
            # The API returns a single market object with all data at the root level
            market_slug = market_data.get('slug', '')
            market_title = market_data.get('question', 'Unknown Market')
            market_question = market_data.get('question', market_title)
            
            logger.debug(f"Extracted basic info: slug={market_slug}, title={market_title}")
            
            # Parse clobTokenIds to find token index
            clob_token_ids_raw = market_data.get('clobTokenIds', '[]')
            logger.debug(f"Raw clobTokenIds: {clob_token_ids_raw}")
            
            if isinstance(clob_token_ids_raw, str):
                try:
                    clob_token_ids = json.loads(clob_token_ids_raw)
                    logger.debug(f"Parsed clobTokenIds: {clob_token_ids}")
                except json.JSONDecodeError:
                    logger.error(f"Could not parse clobTokenIds: {clob_token_ids_raw}")
                    return None
            else:
                clob_token_ids = clob_token_ids_raw
                logger.debug(f"clobTokenIds already parsed: {clob_token_ids}")
            
            # Find token index
            if token_id not in clob_token_ids:
                logger.warning(f"Token {token_id} not found in clobTokenIds: {clob_token_ids}")
                return None
            
            token_outcome_index = clob_token_ids.index(token_id)
            logger.debug(f"Found token at index {token_outcome_index}")
            
            # Parse outcomes and prices
            outcomes_raw = market_data.get('outcomes', '[]')
            outcome_prices_raw = market_data.get('outcomePrices', '[]')
            
            logger.debug(f"Raw outcomes: {outcomes_raw}")
            logger.debug(f"Raw outcome prices: {outcome_prices_raw}")
            
            outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
            outcome_prices_str = json.loads(outcome_prices_raw) if isinstance(outcome_prices_raw, str) else outcome_prices_raw
            outcome_prices = [float(price) for price in outcome_prices_str]
            
            logger.debug(f"Parsed outcomes: {outcomes}")
            logger.debug(f"Parsed outcome prices: {outcome_prices}")
            
            # Get the specific outcome for this token
            if token_outcome_index >= len(outcomes):
                logger.error(f"Token outcome index {token_outcome_index} out of range for outcomes {outcomes}")
                return None
                
            outcome = outcomes[token_outcome_index]
            current_price = outcome_prices[token_outcome_index] if token_outcome_index < len(outcome_prices) else None
            
            logger.debug(f"Selected outcome: {outcome}, price: {current_price}")
            
            # Extract pricing information (from YES perspective)
            raw_best_bid = market_data.get('bestBid')
            raw_best_ask = market_data.get('bestAsk')
            order_price_min_tick_size = market_data.get('orderPriceMinTickSize', 0.001)
            
            # Adjust bid/ask prices based on outcome
            # API returns bid/ask from YES perspective (outcome index 0)
            # For NO outcome (outcome index 1), we need to flip: 1 - price
            if token_outcome_index == 1:
                # For NO outcome: flip the prices (handle None values)
                best_bid = 1.0 - raw_best_ask if raw_best_ask is not None else None  # NO best_bid = 1 - YES best_ask
                best_ask = 1.0 - raw_best_bid if raw_best_bid is not None else None  # NO best_ask = 1 - YES best_bid
                logger.debug(f"NO outcome detected - flipped pricing: raw_bid={raw_best_bid}, raw_ask={raw_best_ask} -> best_bid={best_bid}, best_ask={best_ask}")
            else:
                # For YES outcome: use prices as-is
                best_bid = raw_best_bid
                best_ask = raw_best_ask
                logger.debug(f"YES outcome - using raw pricing: best_bid={best_bid}, best_ask={best_ask}")
            
            logger.debug(f"Final pricing: best_bid={best_bid}, best_ask={best_ask}, tick_size={order_price_min_tick_size}")
            
            # Extract additional market info
            active = market_data.get('active', True)
            volume_24hr = market_data.get('volume24hr')
            liquidity = market_data.get('liquidity')
            
            # Convert string values to floats where needed
            if isinstance(liquidity, str):
                try:
                    liquidity = float(liquidity)
                except (ValueError, TypeError):
                    liquidity = None
            
            metadata = MarketMetadata(
                token_id=token_id,
                market_slug=market_slug,
                market_title=market_title,
                market_question=market_question,
                outcome=outcome,
                outcome_index=token_outcome_index,
                current_price=current_price,
                best_bid=best_bid,
                best_ask=best_ask,
                order_price_min_tick_size=order_price_min_tick_size,
                outcomes=outcomes,
                outcome_prices=outcome_prices,
                active=active,
                volume_24hr=volume_24hr,
                liquidity=liquidity,
                last_updated=datetime.now()
            )
            
            logger.info(f"Successfully created MarketMetadata for token {token_id}")
            return metadata
            
        except Exception as e:
            logger.error(f"Error parsing gamma response: {e}")
            logger.error(f"Exception type: {type(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return None
    
    def suggest_limit_price(self, metadata: MarketMetadata, side: str = "BUY", 
                           improve_by_ticks: int = 0) -> Optional[float]:
        """
        Suggest a limit price based on best bid/ask and side.
        
        Args:
            metadata: Market metadata containing pricing info
            side: "BUY" or "SELL"
            improve_by_ticks: Number of ticks to improve price by (default: 1)
            
        Returns:
            Suggested limit price or None if cannot determine
        """
        try:
            tick_size = metadata.order_price_min_tick_size
            
            if side.upper() == "BUY":
                # For buying, we want to bid higher than current best bid
                if metadata.best_bid is not None:
                    suggested_price = metadata.best_bid + (improve_by_ticks * tick_size)
                    return min(suggested_price, 1.0)  # Cap at 1.0
                elif metadata.current_price is not None:
                    # Fallback to slightly below current price
                    suggested_price = metadata.current_price - tick_size
                    return max(suggested_price, tick_size)  # Floor at one tick
            else:  # SELL
                # For selling, we want to ask lower than current best ask
                if metadata.best_ask is not None:
                    suggested_price = metadata.best_ask - (improve_by_ticks * tick_size)
                    return max(suggested_price, tick_size)  # Floor at one tick
                elif metadata.current_price is not None:
                    # Fallback to slightly above current price
                    suggested_price = metadata.current_price + tick_size
                    return min(suggested_price, 1.0)  # Cap at 1.0
            
            return None
            
        except Exception as e:
            logger.error(f"Error suggesting limit price: {e}")
            return None


# Convenience function for direct usage
async def get_market_metadata(token_id: str, timeout: int = 10) -> Optional[MarketMetadata]:
    """
    Convenience function to fetch market metadata for a token ID.
    
    Args:
        token_id: The CLOB token ID to look up
        timeout: Request timeout in seconds
        
    Returns:
        MarketMetadata object if found, None otherwise
    """
    fetcher = MarketMetadataFetcher(timeout=timeout)
    return await fetcher.fetch_metadata_by_token_id(token_id)


# Synchronous convenience function for GUI usage
def get_market_metadata_sync(token_id: str, timeout: int = 10) -> Optional[MarketMetadata]:
    """
    Synchronous convenience function to fetch market metadata for a token ID.
    Useful for GUI applications that need to fetch metadata without async/await.
    
    Args:
        token_id: The CLOB token ID to look up
        timeout: Request timeout in seconds
        
    Returns:
        MarketMetadata object if found, None otherwise
    """
    try:
        # Run async function in new event loop
        return asyncio.run(get_market_metadata(token_id, timeout))
    except Exception as e:
        logger.error(f"Error in synchronous metadata fetch: {e}")
        return None 