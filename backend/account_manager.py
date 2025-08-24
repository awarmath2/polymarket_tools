"""
Multi-account management.
"""

import os
import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
from py_clob_client.clob_types import AssetType

logger = logging.getLogger(__name__)


@dataclass
class AccountConfig:
    """Configuration for a single trading account"""
    account_id: str
    private_key: str
    proxy_address: str
    enabled: bool = True
    balance_usd: float = 0.0
    balance_fetched: bool = False  # Track if balance has been fetched from API
    last_used: Optional[float] = None  # timestamp


class AccountManager:
    """Manages multiple Polymarket accounts"""
    
    def __init__(self):
        self.accounts: Dict[str, AccountConfig] = {}
        self.clients: Dict[str, ClobClient] = {}
        self.auth_data: Dict[str, dict] = {}
        self.host = "https://clob.polymarket.com"
        self.chain_id = 137
        
    def _validate_and_clean_private_key(self, private_key: str) -> str:
        """Clean and validate private key format"""
        if not private_key:
            raise ValueError("Private key cannot be empty")
        
        # Remove whitespace
        private_key = private_key.strip()
        
        # Remove 0x prefix if present
        if private_key.startswith("0x") or private_key.startswith("0X"):
            private_key = private_key[2:]
        
        # Check if it's a valid hex string of correct length
        if not re.match(r'^[0-9a-fA-F]{64}$', private_key):
            raise ValueError(f"Invalid private key format. Expected 64 hex characters, got {len(private_key)} characters")
        
        return private_key.lower()
    
    def _validate_proxy_address(self, proxy_address: str) -> str:
        """Validate proxy address format"""
        if not proxy_address:
            raise ValueError("Proxy address cannot be empty")
        
        proxy_address = proxy_address.strip()
        
        # Check if it's a valid Ethereum address
        if not re.match(r'^0x[0-9a-fA-F]{40}$', proxy_address):
            raise ValueError(f"Invalid proxy address format. Expected 0x followed by 40 hex characters")
        
        return proxy_address
        
    def load_accounts_from_env(self) -> int:
        """Load accounts from environment variables"""
        loaded_count = 0
        
        # Check for generic pattern accounts (XXXX_PRIVATE_KEY, XXXX_PROXY_ADDRESS)
        env_vars = os.environ.keys()
        potential_accounts = set()
        
        for var in env_vars:
            if var.endswith("_PRIVATE_KEY"):
                account_prefix = var[:-12]  # Remove "_PRIVATE_KEY"
                potential_accounts.add(account_prefix)
            elif var.endswith("_PROXY_ADDRESS"):
                account_prefix = var[:-14]  # Remove "_PROXY_ADDRESS"
                potential_accounts.add(account_prefix)
        
        # Validate each potential account has both required variables
        for account_name in potential_accounts:            
            private_key = os.environ.get(f"{account_name}_PRIVATE_KEY")
            proxy_address = os.environ.get(f"{account_name}_PROXY_ADDRESS")
            
            if private_key and proxy_address:
                try:
                    # Clean and validate the credentials
                    cleaned_private_key = self._validate_and_clean_private_key(private_key)
                    cleaned_proxy_address = self._validate_proxy_address(proxy_address)
                    
                    # Preserve existing balance and settings if account already exists
                    if account_name in self.accounts:
                        existing_config = self.accounts[account_name]
                        account_config = AccountConfig(
                            account_id=account_name,
                            private_key=cleaned_private_key,
                            proxy_address=cleaned_proxy_address,
                            enabled=existing_config.enabled,
                            balance_usd=existing_config.balance_usd,
                            balance_fetched=existing_config.balance_fetched,
                            last_used=existing_config.last_used
                        )
                    else:
                        account_config = AccountConfig(
                            account_id=account_name,
                            private_key=cleaned_private_key,
                            proxy_address=cleaned_proxy_address
                        )
                    
                    self.accounts[account_name] = account_config
                    loaded_count += 1
                    logger.info(f"Loaded account {account_name} with valid credentials")
                except ValueError as e:
                    logger.error(f"Invalid credentials for account {account_name}: {e}")
                    continue
        
        logger.info(f"Loaded {loaded_count} total accounts: {list(self.accounts.keys())}")
        return loaded_count
    
    def add_account(self, account_id: str, private_key: str, proxy_address: str) -> bool:
        """Manually add an account"""
        if account_id in self.accounts:
            logger.warning(f"Account {account_id} already exists")
            return False
        
        try:
            # Clean and validate the credentials
            cleaned_private_key = self._validate_and_clean_private_key(private_key)
            cleaned_proxy_address = self._validate_proxy_address(proxy_address)
            
            account_config = AccountConfig(
                account_id=account_id,
                private_key=cleaned_private_key,
                proxy_address=cleaned_proxy_address
            )
            self.accounts[account_id] = account_config
            logger.info(f"Added account {account_id}")
            return True
        except ValueError as e:
            logger.error(f"Invalid credentials for account {account_id}: {e}")
            return False
    
    def remove_account(self, account_id: str) -> bool:
        """Remove an account"""
        if account_id not in self.accounts:
            return False
        
        # Clean up client if it exists
        if account_id in self.clients:
            del self.clients[account_id]
        if account_id in self.auth_data:
            del self.auth_data[account_id]
        
        del self.accounts[account_id]
        logger.info(f"Removed account {account_id}")
        return True
    
    def get_client(self, account_id: str) -> Optional[ClobClient]:
        """Get or create a client for the specified account"""
        if account_id not in self.accounts:
            logger.error(f"Account {account_id} not found")
            return None
        
        # Return existing client if available
        if account_id in self.clients:
            return self.clients[account_id]
        
        # Create new client
        account = self.accounts[account_id]
        try:
            client = ClobClient(
                self.host,
                key=account.private_key,
                chain_id=self.chain_id,
                signature_type=2,
                funder=account.proxy_address
            )
            
            # Set API credentials
            client.set_api_creds(client.create_or_derive_api_creds())
            
            # Store client and auth data
            self.clients[account_id] = client
            self.auth_data[account_id] = {
                "apiKey": client.creds.api_key,
                "secret": client.creds.api_secret,
                "passphrase": client.creds.api_passphrase
            }
            
            logger.info(f"Created client for account {account_id}")
            return client
            
        except Exception as e:
            logger.error(f"Failed to create client for account {account_id}: {e}")
            return None
    
    def get_auth_data(self, account_id: str) -> Optional[dict]:
        """Get authentication data for the specified account"""
        if account_id not in self.accounts:
            return None
        
        # Ensure client exists to generate auth data
        if self.get_client(account_id):
            return self.auth_data.get(account_id)
        return None
    
    def get_enabled_accounts(self) -> List[str]:
        """Get list of enabled account IDs"""
        return [
            account_id for account_id, config in self.accounts.items()
            if config.enabled
        ]
    
    def enable_account(self, account_id: str, enabled: bool = True) -> bool:
        """Enable or disable an account"""
        if account_id not in self.accounts:
            return False
        
        self.accounts[account_id].enabled = enabled
        logger.info(f"Account {account_id} {'enabled' if enabled else 'disabled'}")
        return True
    
    def get_account_info(self) -> Dict[str, dict]:
        """Get summary information about all accounts"""
        return {
            account_id: {
                "enabled": config.enabled,
                "proxy_address": config.proxy_address,
                "balance_usd": config.balance_usd,
                "balance_fetched": config.balance_fetched,
                "last_used": config.last_used,
                "has_client": account_id in self.clients
            }
            for account_id, config in self.accounts.items()
        }
    
    async def update_balances(self) -> None:
        """Update USD balances for enabled accounts only"""
        logger.info("Updating balances for enabled accounts...")
        
        enabled_accounts = self.get_enabled_accounts()
        logger.info(f"Updating balances for {len(enabled_accounts)} enabled accounts: {enabled_accounts}")
        
        for account_id in enabled_accounts:  # Only update enabled accounts
            try:
                # Get client for this account
                client = self.get_client(account_id)
                if not client:
                    logger.warning(f"No client available for account {account_id}")
                    continue
                
                # Fetch collateral balance (USDC)
                balance_params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL) # type: ignore
                collateral_balance = client.get_balance_allowance(params=balance_params)
                
                # Extract balance from response - handle both dict and object formats
                balance_raw = None
                if collateral_balance:
                    if isinstance(collateral_balance, dict) and 'balance' in collateral_balance:
                        balance_raw = float(collateral_balance['balance'])
                    elif hasattr(collateral_balance, 'balance'):
                        balance_raw = float(collateral_balance.balance) # type: ignore
                
                if balance_raw is not None:
                    # Balance is typically returned in wei (6 decimals for USDC)
                    balance_usd = balance_raw / 1_000_000  # Convert from 6 decimal places
                    
                    # Update account balance and mark as fetched
                    self.accounts[account_id].balance_usd = balance_usd
                    self.accounts[account_id].balance_fetched = True
                    logger.info(f"Updated balance for {account_id}: ${balance_usd:.2f}")
                else:
                    logger.warning(f"Could not extract balance from response for account {account_id}: {collateral_balance}")
                    
            except Exception as e:
                logger.error(f"Failed to update balance for account {account_id}: {e}")
                # Don't update balance on error, keep existing value
        
        logger.info("Balance update completed for enabled accounts")
    
    def get_account_count(self) -> int:
        """Get total number of accounts"""
        return len(self.accounts)
    
    def get_enabled_account_count(self) -> int:
        """Get number of enabled accounts"""
        return len(self.get_enabled_accounts()) 