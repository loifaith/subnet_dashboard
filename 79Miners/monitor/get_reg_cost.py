#!/usr/bin/env python3

"""
Simple utility script to check the current registration price for a Bittensor subnet.
"""

import sys
import argparse
import json
import os
import time
import bittensor as bt
from typing import Dict, Iterable, Optional, Set

ARCHIVE_ENDPOINT = "wss://archive.chain.opentensor.ai:443"
DEFAULT_ENDPOINT = "finney"
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REG_COST_CACHE_DIR = os.path.join(PROJECT_ROOT, "miner_status")


def get_current_subnet_reg_cost(
    netuid: int,
    endpoint: str = DEFAULT_ENDPOINT,
) -> Optional[dict]:
    """Return current registration recycle cost for a subnet (current block)."""
    sub = connect_subtensor(endpoint, try_limit=3, verbose=False)
    if sub is None:
        return None
    try:
        return get_registration_price_from_subtensor(sub, netuid, block=None, verbose=False)
    finally:
        try:
            sub.close()
        except Exception:
            pass


def reg_cost_cache_path(netuid: int) -> str:
    return os.path.join(REG_COST_CACHE_DIR, f"reg_cost_by_block_{netuid}.json")


def load_reg_cost_cache(netuid: int) -> Dict[str, dict]:
    path = reg_cost_cache_path(netuid)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {str(k): v for k, v in data.items() if isinstance(v, dict)}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return {}


def save_reg_cost_cache(netuid: int, cache: Dict[str, dict]) -> None:
    os.makedirs(REG_COST_CACHE_DIR, exist_ok=True)
    path = reg_cost_cache_path(netuid)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp_path, path)


def connect_subtensor(
    endpoint: str,
    try_limit: int = 5,
    base_delay_sec: float = 2.0,
    verbose: bool = True,
) -> Optional[bt.Subtensor]:
    """
    Open a Subtensor connection with retries (WebSocket timeouts, etc.).
    Uses exponential backoff capped at 60s between attempts.
    """
    last_err: Optional[BaseException] = None
    for attempt in range(try_limit):
        try:
            if verbose:
                if attempt == 0:
                    print(f"🔗 Connecting to {endpoint}...")
                else:
                    print(f"🔗 Retry {attempt + 1}/{try_limit} connecting to {endpoint}...")
            return bt.Subtensor(network=endpoint)
        except Exception as e:
            last_err = e
            if verbose:
                print(f"⚠️  Connection failed (attempt {attempt + 1}/{try_limit}): {e}")
            if attempt + 1 < try_limit:
                # Rate limit: back off much harder so the endpoint forgives us.
                is_rate_limited = "429" in str(e) or "Too Many Requests" in str(e)
                if is_rate_limited:
                    delay = min(max(base_delay_sec, 15.0) * (2**attempt), 120.0)
                    if verbose:
                        print(f"⏳ Rate limited (HTTP 429); sleeping {delay:.1f}s before retry")
                else:
                    delay = min(base_delay_sec * (2**attempt), 60.0)
                time.sleep(delay)
    if verbose:
        print(f"❌ Giving up after {try_limit} connection attempt(s)")
        if last_err is not None:
            import traceback

            traceback.print_exception(type(last_err), last_err, last_err.__traceback__)
    return None


def get_current_block(sub: bt.Subtensor, try_limit: Optional[int] = 5) -> Optional[int]:
    """Get the current block number with retry logic."""
    try_counter = 0
    while try_counter < try_limit:
        try:
            current_block = sub.get_current_block()
            return current_block
        except Exception as e:
            print(f"⚠️  Error getting current block (attempt {try_counter + 1}/{try_limit}): {e}")
            try_counter += 1
            if try_counter < try_limit:
                time.sleep(1)
    return None


def get_balance_for_address(
    address: str,
    endpoint: str = DEFAULT_ENDPOINT,
) -> Optional[dict]:
    """Return the free TAO balance for a coldkey ss58 address."""
    sub = connect_subtensor(endpoint, try_limit=3, verbose=False)
    if sub is None:
        return None
    try:
        balance = sub.get_balance(address)
        rao = int(balance)
        return {"address": address, "rao": rao, "tao": float(rao) / pow(10, 9)}
    except Exception:
        return None
    finally:
        try:
            sub.close()
        except Exception:
            pass


def get_balance_and_stake_for_address(
    address: str,
    netuid: int,
    endpoint: str = DEFAULT_ENDPOINT,
) -> Optional[dict]:
    """Free TAO balance plus total staked value for a coldkey on one netuid
    (summed across every hotkey) — one connection for both, so callers that
    need both don't pay for two separate connect/close cycles.

    Stake is held in the subnet's alpha token, not TAO — alpha floats
    against TAO via the subnet's dynamic pool rather than being 1:1, so the
    raw alpha amount (`staked_alpha`) is converted to its actual TAO value
    (`staked_tao`) using the live exchange rate from get_subnet_price."""
    sub = connect_subtensor(endpoint, try_limit=3, verbose=False)
    if sub is None:
        return None
    try:
        balance = sub.get_balance(address)
        rao = int(balance)
        staked_rao = sum(
            int(stakeinfo.stake)
            for stakeinfo in sub.get_stake_info_for_coldkey(address)
            if stakeinfo.netuid == netuid
        )
        staked_alpha = float(staked_rao) / pow(10, 9)
        alpha_price_tao = float(int(sub.get_subnet_price(netuid))) / pow(10, 9)
        return {
            "address": address,
            "rao": rao,
            "tao": float(rao) / pow(10, 9),
            "staked_rao": staked_rao,
            "staked_alpha": staked_alpha,
            "alpha_price_tao": alpha_price_tao,
            "staked_tao": staked_alpha * alpha_price_tao,
        }
    except Exception:
        return None
    finally:
        try:
            sub.close()
        except Exception:
            pass


def get_registration_price_from_subtensor(
    sub: bt.Subtensor,
    netuid: int,
    block: Optional[int] = None,
    verbose: bool = True
) -> Optional[dict]:
    """
    Get the current registration price for a subnet using an existing Subtensor instance.
    
    Args:
        sub: Existing Subtensor instance
        netuid: Subnet ID
        block: Specific block number (default: current block)
        verbose: Whether to print status messages
    
    Returns:
        dict with keys: 'rao', 'tao', 'block', 'netuid'
        or None if error
    """
    try:
        # Get current block if not provided
        if block is None:
            current_block = get_current_block(sub, try_limit=3)
            if current_block is None:
                if verbose:
                    print("❌ Failed to get current block")
                return None
        else:
            current_block = block
        
        # Get registration fee (recycle amount)
        if verbose:
            print(f"💰 Fetching registration price for subnet {netuid}...")
        recycle_amount = sub.recycle(netuid=netuid, block=current_block)
        
        if recycle_amount is None:
            if verbose:
                print("❌ Failed to get registration price")
            return None
        
        # Convert Balance object to integer (rao)
        recycle_rao = int(recycle_amount)
        
        # Convert rao to tao
        recycle_tao = float(recycle_rao) / pow(10, 9)
        
        result = {
            'rao': recycle_rao,
            'tao': recycle_tao,
            'block': current_block,
            'netuid': netuid
        }
        
        return result
        
    except Exception as e:
        if verbose:
            print(f"❌ Error getting registration price: {e}")
        return None


def _pick_endpoint(block: int, current_block: Optional[int] = None) -> str:
    """Use finney for very recent blocks; archive for historical burn queries."""
    if current_block is not None and current_block - block <= 900:
        return DEFAULT_ENDPOINT
    return ARCHIVE_ENDPOINT


def fetch_reg_costs_by_block(
    netuid: int,
    blocks: Iterable[int],
    endpoint: Optional[str] = None,
    current_block: Optional[int] = None,
    max_new: Optional[int] = None,
) -> Dict[int, dict]:
    """Return {reg_block: {rao, tao, block, netuid}} for each unique registration block."""
    unique_blocks: Set[int] = set()
    for block in blocks:
        try:
            unique_blocks.add(int(block))
        except (TypeError, ValueError):
            continue
    if not unique_blocks:
        return {}

    cache = load_reg_cost_cache(netuid)
    out: Dict[int, dict] = {}
    missing: list[int] = []
    for block in sorted(unique_blocks):
        cached = cache.get(str(block))
        if cached:
            out[block] = cached
        else:
            missing.append(block)

    if missing and max_new is not None:
        missing = missing[:max_new]

    if missing:
        by_endpoint: Dict[str, list[int]] = {}
        for block in missing:
            ep = endpoint or _pick_endpoint(block, current_block)
            by_endpoint.setdefault(ep, []).append(block)

        for ep, ep_blocks in by_endpoint.items():
            sub = connect_subtensor(ep, try_limit=3, verbose=False)
            if sub is None:
                continue
            try:
                for block in ep_blocks:
                    price = get_registration_price_from_subtensor(
                        sub, netuid, block=block, verbose=False
                    )
                    if price is not None:
                        out[block] = price
                        cache[str(block)] = price
                        save_reg_cost_cache(netuid, cache)
            finally:
                try:
                    sub.close()
                except Exception:
                    pass

    return out


def get_registration_price(
    netuid: int,
    endpoint: str = "finney",
    block: Optional[int] = None,
    connect_retries: int = 5,
    connect_retry_base_sec: float = 2.0,
) -> Optional[dict]:
    """
    Get the current registration price for a subnet.
    
    Returns:
        dict with keys: 'rao', 'tao', 'block', 'netuid', 'endpoint'
        or None if error
    """
    sub = None
    try:
        sub = connect_subtensor(
            endpoint,
            try_limit=connect_retries,
            base_delay_sec=connect_retry_base_sec,
            verbose=True,
        )
        if sub is None:
            return None
        print(f"✅ Connected to {sub.network} at {sub.chain_endpoint}")
        
        # Use the shared function
        result = get_registration_price_from_subtensor(sub, netuid, block, verbose=True)
        
        if result is not None:
            result['endpoint'] = endpoint
        
        return result

    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback

        traceback.print_exc()
        return None
    finally:
        if sub is not None:
            try:
                sub.close()
            except Exception:
                pass


def format_price(price_dict: dict) -> str:
    """Format the price information for display."""
    if price_dict is None:
        return "❌ No price data available"
    
    rao = price_dict['rao']
    tao = price_dict['tao']
    block = price_dict['block']
    netuid = price_dict['netuid']
    endpoint = price_dict['endpoint']
    
    output = []
    output.append("=" * 60)
    output.append(f"📊 Registration Price for Subnet {netuid}")
    output.append("=" * 60)
    output.append(f"🌐 Network: {endpoint}")
    output.append(f"🔢 Block: {block}")
    output.append(f"💰 Price: {int(rao):,} rao")
    output.append(f"💰 Price: {tao:.9f} TAO")
    output.append("=" * 60)
    
    return "\n".join(output)


def main():
    parser = argparse.ArgumentParser(
        description="Get the current registration price for a Bittensor subnet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Get price for subnet 1 on finney network
  python3 get_reg_price.py --netuid 1
  
  # Get price for subnet 79 on finney network
  python3 get_reg_price.py --netuid 79
  
  # Get price using custom endpoint
  python3 get_reg_price.py --netuid 1 --endpoint wss://entrypoint-finney.opentensor.ai:443
  
  # Get price at specific block
  python3 get_reg_price.py --netuid 1 --block 12345678
        """
    )
    
    parser.add_argument(
        "--netuid",
        type=int,
        required=True,
        help="The Bittensor subnet number (e.g., 1, 18, 79)"
    )
    
    parser.add_argument(
        "--endpoint",
        type=str,
        default="finney",
        help="Subtensor network or endpoint URL (default: finney)"
    )
    
    parser.add_argument(
        "--block",
        type=int,
        default=None,
        help="Specific block number to query (default: current block)"
    )

    parser.add_argument(
        "--connect-retries",
        type=int,
        default=5,
        help="Max attempts when opening Subtensor connection (default: 5)",
    )

    parser.add_argument(
        "--connect-retry-base",
        type=float,
        default=2.0,
        help="Base delay in seconds for exponential backoff between connection retries (default: 2.0)",
    )
    
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output result as JSON"
    )
    
    args = parser.parse_args()
    
    # Get registration price
    price_dict = get_registration_price(
        netuid=args.netuid,
        endpoint=args.endpoint,
        block=args.block,
        connect_retries=args.connect_retries,
        connect_retry_base_sec=args.connect_retry_base,
    )
    
    if price_dict is None:
        sys.exit(1)
    
    # Output result
    if args.json:
        import json
        print(json.dumps(price_dict, indent=2))
    else:
        print(format_price(price_dict))
    
    sys.exit(0)


if __name__ == '__main__':
    main()

