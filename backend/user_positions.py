"""
User positions management and caching for Polymarket.
"""

import os
import json
import requests
import time
import tempfile
import shutil
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import logging

logger = logging.getLogger(__name__)

class UserPosition:
    """Represents a user's position in a specific token"""
    def __init__(self, data: dict):
        self.asset = data.get("asset", "")  # This is the token ID
        self.size = float(data.get("size", 0))
        self.avg_price = float(data.get("avgPrice", 0))
        self.current_value = float(data.get("currentValue", 0))
        self.cash_pnl = float(data.get("cashPnl", 0))
        self.percent_pnl = float(data.get("percentPnl", 0))
        self.title = data.get("title", "Unknown Market")
        self.outcome = data.get("outcome", "Unknown")
        self.slug = data.get("slug", "")
        self.redeemable = data.get("redeemable", False)
        self.mergeable = data.get("mergeable", False)
        # New: condition and outcome index from Data-API
        self.condition_id = data.get("conditionId", "")
        self.outcome_index = data.get("outcomeIndex")
        
    def to_dict(self) -> dict:
        """Convert to dictionary for caching"""
        return {
            "asset": self.asset,
            "size": self.size,
            "avg_price": self.avg_price,
            "current_value": self.current_value,
            "cash_pnl": self.cash_pnl,
            "percent_pnl": self.percent_pnl,
            "title": self.title,
            "outcome": self.outcome,
            "slug": self.slug,
            "redeemable": self.redeemable,
            "mergeable": self.mergeable,
            "conditionId": self.condition_id,
            "outcomeIndex": self.outcome_index,
        }

class UserPositionsCache:
    """Manages caching and fetching of user positions from Polymarket API with multi-account support"""
    
    def __init__(self, proxy_address: str, cache_duration_minutes: int = 1):
        self.cache_duration = timedelta(minutes=cache_duration_minutes)
        self.cache_file = "__cache__/user_positions_shared.json"  # Shared file for all accounts
        self.positions_cache: Dict[str, UserPosition] = {}
        self.last_update: Optional[datetime] = None
        
        # Store proxy address for API calls - now required
        if not proxy_address:
            raise ValueError("proxy_address is required")
        
        self.proxy_address = proxy_address
        
        # Ensure cache directory exists
        os.makedirs("__cache__", exist_ok=True)
        
        # Load cached data if available
        self._load_from_cache()
    
    def _load_from_cache(self):
        """Load positions from cache file for this proxy address if recent enough"""
        try:
            if not os.path.exists(self.cache_file):
                logger.debug(f"Cache file doesn't exist: {self.cache_file}")
                return
            
            with open(self.cache_file, 'r') as f:
                try:
                    all_cache_data = json.load(f)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in cache file, starting fresh")
                    return
            
            # Handle both new nested structure and legacy flat structure for backward compatibility
            proxy_cache_data = None
            
            if isinstance(all_cache_data, dict):
                if self.proxy_address in all_cache_data:
                    # New nested structure
                    proxy_cache_data = all_cache_data[self.proxy_address]
                    logger.debug(f"Found data for proxy {self.proxy_address[:10]}... in nested structure")
                elif 'timestamp' in all_cache_data and 'positions' in all_cache_data:
                    # Legacy flat structure - assume it's for this proxy address
                    proxy_cache_data = all_cache_data
                    logger.info(f"Loading legacy cache structure for {self.proxy_address[:10]}...")
                    
                    # Migrate to new structure by saving it properly
                    self.positions_cache = {
                        asset_id: UserPosition(pos_data) 
                        for asset_id, pos_data in proxy_cache_data.get('positions', {}).items()
                    }
                    self.last_update = datetime.fromisoformat(proxy_cache_data['timestamp'])
                    self._save_to_cache()  # Save in new format
                    logger.info(f"Migrated legacy cache to new structure for {self.proxy_address[:10]}...")
                    return
            
            if not proxy_cache_data:
                logger.debug(f"No cached data found for proxy {self.proxy_address[:10]}...")
                return
            
            # Load positions from cache regardless of freshness so UI can show something immediately
            cache_time_str = proxy_cache_data.get('timestamp')
            if cache_time_str:
                cache_time = datetime.fromisoformat(cache_time_str)
                positions_data = proxy_cache_data.get('positions', {})
                self.positions_cache = {
                    asset_id: UserPosition(pos_data) 
                    for asset_id, pos_data in positions_data.items()
                }
                self.last_update = cache_time
                if datetime.now() - cache_time < self.cache_duration:
                    logger.info(f"Loaded {len(self.positions_cache)} positions from cache for {self.proxy_address[:10]}...")
                else:
                    logger.info(f"Loaded {len(self.positions_cache)} stale cached positions for {self.proxy_address[:10]}...; will refresh on demand")
                return
            
            logger.info(f"Cache exists but missing timestamp for {self.proxy_address[:10]}...")
            
        except Exception as e:
            logger.warning(f"Error loading positions cache for {self.proxy_address[:10]}...: {e}")
    
    def _save_to_cache(self):
        """Save current positions to cache file, preserving data for other proxy addresses"""
        try:
            current_data = {
                'timestamp': datetime.now().isoformat(),
                'positions': {
                    asset_id: position.to_dict() 
                    for asset_id, position in self.positions_cache.items()
                }
            }
            
            # Use atomic write with file locking to prevent race conditions
            temp_file = None
            try:
                # Load existing data from all accounts
                all_cache_data = {}
                if os.path.exists(self.cache_file):
                    try:
                        with open(self.cache_file, 'r') as f:
                            all_cache_data = json.load(f)
                    except (json.JSONDecodeError, Exception) as e:
                        logger.warning(f"Error reading existing cache, starting fresh: {e}")
                        all_cache_data = {}
                
                # Ensure all_cache_data is a dict
                if not isinstance(all_cache_data, dict):
                    all_cache_data = {}
                
                # Update data for this proxy address
                all_cache_data[self.proxy_address] = current_data
                
                # Write atomically using temporary file
                with tempfile.NamedTemporaryFile(mode='w', dir='__cache__', delete=False) as temp_file:
                    json.dump(all_cache_data, temp_file, indent=2)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                
                # Atomic move
                shutil.move(temp_file.name, self.cache_file)
                temp_file = None  # Successfully moved
                
                logger.info(f"Saved {len(self.positions_cache)} positions to cache for {self.proxy_address[:10]}...")
                
            except Exception as e:
                logger.error(f"Error during atomic write: {e}")
                # Clean up temp file if it exists
                if temp_file and os.path.exists(temp_file.name):
                    try:
                        os.unlink(temp_file.name)
                    except:
                        pass
                raise
            
        except Exception as e:
            logger.error(f"Error saving positions cache for {self.proxy_address[:10]}...: {e}")
    
    def _fetch_fresh_positions(self) -> bool:
        """Fetch fresh positions from the API"""
        try:
            if not self.proxy_address:
                logger.error("No proxy address provided - cannot fetch positions")
                return False
            
            url = "https://data-api.polymarket.com/positions"
            querystring = {
                "limit": "500",
                "sortDirection": "DESC", 
                "user": self.proxy_address
            }
            
            logger.info(f"Fetching positions for user {self.proxy_address[:10]}...")
            response = requests.get(url, params=querystring, timeout=10)
            response.raise_for_status()
            
            positions_data = response.json()
            
            if len(positions_data) == 500:
                logger.warning("Received 500 positions (API limit) - some positions may be missing")
            
            # Clear old cache and rebuild
            self.positions_cache.clear()
            
            for pos_data in positions_data:
                position = UserPosition(pos_data)
                # Only include positions with non-zero size
                if position.size > 0:
                    self.positions_cache[position.asset] = position
            
            self.last_update = datetime.now()
            self._save_to_cache()
            
            logger.info(f"Successfully fetched and cached {len(self.positions_cache)} positions for {self.proxy_address[:10]}...")
            return True
            
        except Exception as e:
            logger.error(f"Error fetching positions for {self.proxy_address[:10]}...: {e}")
            return False
    
    def get_position_for_token(self, token_id: str) -> Optional[UserPosition]:
        """Get user's position for a specific token ID"""
        # Check if we need to refresh the cache
        if (self.last_update is None or 
            datetime.now() - self.last_update >= self.cache_duration):
            logger.info(f"Cache is stale, refreshing positions for {self.proxy_address[:10]}...")
            self._fetch_fresh_positions()
        
        return self.positions_cache.get(token_id)
    
    def get_all_positions(self) -> Dict[str, UserPosition]:
        """Get all cached positions (refreshes if needed)"""
        # Check if we need to refresh the cache
        if (self.last_update is None or 
            datetime.now() - self.last_update >= self.cache_duration):
            logger.info(f"Cache is stale, refreshing positions for {self.proxy_address[:10]}...")
            self._fetch_fresh_positions()
        
        return self.positions_cache.copy()
    
    def force_refresh(self) -> bool:
        """Force refresh of positions regardless of cache age"""
        logger.info(f"Force refreshing user positions cache for {self.proxy_address[:10]}...")
        return self._fetch_fresh_positions()
    
    def get_cache_info(self) -> dict:
        """Get information about the cache state"""
        return {
            "proxy_address": self.proxy_address[:10] + "...",
            "positions_count": len(self.positions_cache),
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "cache_age_seconds": (datetime.now() - self.last_update).total_seconds() if self.last_update else None,
            "is_stale": (datetime.now() - self.last_update >= self.cache_duration) if self.last_update else True
        } 
    
    @classmethod
    def get_all_cached_accounts(cls) -> List[str]:
        """Get a list of all proxy addresses that have cached data"""
        cache_file = "__cache__/user_positions_shared.json"
        try:
            if not os.path.exists(cache_file):
                return []
            
            with open(cache_file, 'r') as f:
                all_cache_data = json.load(f)
            
            if isinstance(all_cache_data, dict):
                # Filter out legacy flat structure entries
                proxy_addresses = []
                for key, value in all_cache_data.items():
                    if isinstance(value, dict) and 'timestamp' in value and 'positions' in value:
                        proxy_addresses.append(key)
                return proxy_addresses
            
            return []
            
        except Exception as e:
            logger.error(f"Error reading cached accounts: {e}")
            return []
    
    @classmethod
    def clear_cache_for_account(cls, proxy_address: str) -> bool:
        """Clear cached data for a specific proxy address"""
        cache_file = "__cache__/user_positions_shared.json"
        try:
            if not os.path.exists(cache_file):
                return True
            
            with open(cache_file, 'r') as f:
                all_cache_data = json.load(f)
            
            if isinstance(all_cache_data, dict) and proxy_address in all_cache_data:
                del all_cache_data[proxy_address]
                
                # Write back atomically
                with tempfile.NamedTemporaryFile(mode='w', dir='__cache__', delete=False) as temp_file:
                    json.dump(all_cache_data, temp_file, indent=2)
                    temp_file.flush()
                    os.fsync(temp_file.fileno())
                
                shutil.move(temp_file.name, cache_file)
                logger.info(f"Cleared cache for proxy address {proxy_address[:10]}...")
                return True
            
            return True  # Nothing to clear
            
        except Exception as e:
            logger.error(f"Error clearing cache for {proxy_address[:10]}...: {e}")
            return False

    def get_cached_positions(self) -> Dict[str, UserPosition]:
        """Return cached positions without triggering a refresh."""
        return self.positions_cache.copy() 